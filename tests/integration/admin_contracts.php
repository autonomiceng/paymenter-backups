<?php
/** Independent checks. Run only against a disposable, installed UI fixture. */
try {
    $base = realpath($argv[1] ?? '');
    $work = $base ? dirname($base) : '';
    $metadataPath = $work . '/ui-fixture.json';
    if (!$base || basename($base) !== 'app' || !str_starts_with(basename($work), 'ui-')
        || basename(dirname($work)) !== '.native-work' || !is_file($metadataPath)) {
        throw new RuntimeException('A disposable ui- fixture is required');
    }
    $metadata = json_decode(file_get_contents($metadataPath), true, 32, JSON_THROW_ON_ERROR);
    if (($metadata['fixture_only'] ?? null) !== true || ($metadata['app'] ?? null) !== $base
        || ($metadata['native_scheduler_started'] ?? null) !== false) {
        throw new RuntimeException('Fixture containment metadata does not match');
    }
    require $base . '/vendor/autoload.php';
    $app = require $base . '/bootstrap/app.php';
    $app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
    $class = Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::class;
    if (!method_exists($class, 'saveSettings') || !method_exists($class, 'encryptionForm')) {
        throw new RuntimeException('Install the frozen UI release before running these checks');
    }
    function testCheck(bool $condition, string $message): void {
        if (!$condition) { throw new RuntimeException($message); }
    }
    $selected = $argv[2] ?? 'all';
    testCheck(in_array($selected, ['all', 'permission', 'validation', 'credentials', 'recovery_key', 'cron_fallback', 'basic_states', 'root', 'baseline', 'repair', 'cli_config', 'download', 'download_anchor', 'delete_gate', 'modals', 'field_help', 'presentation', 'first_use', 'runtime'], true), 'Unknown test phase');
    function testPhase(string $name): bool {
        global $selected;
        return $selected === 'all' || $selected === $name;
    }
    $currentPhase = 'bootstrap';
    function rowValues($record): array {
        $values = $record->settings()->pluck('value', 'key')->all();
        ksort($values);
        return $values;
    }
    function submitSettings($class, array $changes) {
        $page = Livewire\Livewire::test($class);
        $settings = array_replace($page->get('settings'), $changes);
        $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings' => $settings]);
        return $page;
    }
    function testSecretReset($page): void {
        foreach (['access_key', 'secret_key', 'session_token'] as $key) {
            testCheck(empty($page->get('s3Keys')[$key]), 'An S3 input returned in Livewire state');
        }
        foreach (['username', 'password'] as $key) {
            testCheck(empty($page->get('siteAccess')[$key]), 'A site password input returned in Livewire state');
        }
        testCheck(empty($page->get('encryptionInput')['passphrase'] ?? null), 'A passphrase returned in Livewire state');
        testCheck(!str_contains($page->html(), 'TEST-SECRET-CANARY'), 'Secret canary returned in rendered response');
    }
    /** Filament 5 returns only the modal partial; its key can carry a nesting suffix. */
    function modalPartial($page): ?string {
        foreach ($page->effects['partials'] ?? [] as $name => $html) {
            if (str_starts_with((string) $name, 'action-modals')) { return $html; }
        }
        return null;
    }
    function testChildGone(string $pidFile): void {
        testCheck(is_file($pidFile), 'Controlled stream child never recorded its identity');
        $pid = trim(file_get_contents($pidFile));
        testCheck(ctype_digit($pid) && (int) $pid > 1, 'Invalid controlled child identity');
        testCheck(!is_dir('/proc/' . $pid), 'Controlled stream child survived failure or disconnect cleanup');
    }
    $record = App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->firstOrFail();
    $initialRows = rowValues($record);
    $root = Paymenter\Extensions\Others\Backups\Runtime::privateRoot();
    testCheck($root === $metadata['private_root'], 'Use this fixture\'s private folder');
    $credentialFile = $root . '/credentials.json';
    $originalCredentials = is_file($credentialFile) ? file_get_contents($credentialFile) : null;
    $originalUrl = App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->value('value');
    $identity = file_get_contents($root . '/installation.json');
    $admin = App\Models\User::where('email', $metadata['admin_email'])->firstOrFail();
    $limited = App\Models\User::where('email', $metadata['limited_email'])->firstOrFail();
    Filament\Facades\Filament::setCurrentPanel(Filament\Facades\Filament::getPanel('admin'));
    Illuminate\Support\Facades\Auth::login($admin);
    config(['audit.console' => true]);
    $results = [];
    try {
        if (testPhase('runtime')) {
        $currentPhase = 'runtime';
        $runtime = Paymenter\Extensions\Others\Backups\Runtime::class;
        foreach (['openStream', 'readStream', 'stopStream'] as $method) {
            testCheck(method_exists($runtime, $method), 'Stream runtime must provide the required methods');
        }
        testCheck(function_exists('pcntl_signal') && function_exists('stream_socket_pair'), 'Stream checks require Linux pcntl and socket pairs');
        $phaseStart = microtime(true);
        $runtimeWork = $work . '/test-stream-' . bin2hex(random_bytes(4));
        testCheck(mkdir($runtimeWork, 0700), 'Could not create private stream fixture');

        // A real idle socket must fail after a finite deadline.
        $pair = stream_socket_pair(STREAM_PF_UNIX, STREAM_SOCK_STREAM, STREAM_IPPROTO_IP);
        testCheck(is_array($pair), 'Could not create stream fixture socket pair');
        $idleStart = microtime(true);
        try {
            try {
                $runtime::readStream($pair[0], 1);
                throw new RuntimeException('Idle stream did not time out');
            } catch (RuntimeException $error) {
                testCheck($error->getMessage() === 'artifact-stream-timeout', 'Idle stream returned the wrong safe error');
            }
            testCheck(microtime(true) - $idleStart <= 2.5, 'Idle timeout exceeded its bound');
        } finally { fclose($pair[0]); fclose($pair[1]); }

        // This is the real public stream seam, driven by an owned CLI child.
        // It needs no extension/source stub and never starts capture or a provider.
        $childPrefix = 'pcntl_async_signals(true); pcntl_signal(SIGTERM, SIG_IGN); '
            . 'file_put_contents($argv[1], (string) getmypid()); ';
        foreach (['no-header' => '', 'malformed-header' => 'fwrite(STDOUT, "{}\\n"); fflush(STDOUT); '] as $name => $output) {
            $pidFile = $runtimeWork . '/' . $name . '.pid';
            $script = $childPrefix . $output . 'while (true) { usleep(10000); }';
            $started = microtime(true);
            try {
                $runtime::openStream([PHP_BINARY, '-r', $script, $pidFile], 1);
                throw new RuntimeException('Invalid controlled stream unexpectedly started');
            } catch (RuntimeException $error) {
                $expected = $name === 'no-header' ? 'artifact-stream-timeout' : 'engine-unavailable';
                testCheck($error->getMessage() === $expected, 'Startup refusal returned the wrong safe error');
            }
            // One second startup plus the bounded close/TERM/KILL grace periods.
            testCheck(microtime(true) - $started <= 8.0, 'Startup and TERM escalation exceeded their bound');
            testChildGone($pidFile);
        }

        // Header and body arrive in one write. Preserve buffered body bytes,
        // then enforce the idle limit and kill the TERM-ignoring child.
        $pidFile = $runtimeWork . '/partial-body.pid';
        $header = json_encode(['ok' => true, 'size' => 4, 'filename' => 'paymenter-backup-owned.tar.gz'], JSON_THROW_ON_ERROR);
        $script = $childPrefix . 'fwrite(STDOUT, ' . var_export($header . "\nAB", true) . '); fflush(STDOUT); while (true) { usleep(10000); }';
        [$process, $stdout, $stderr, $announced] = $runtime::openStream([PHP_BINARY, '-r', $script, $pidFile], 1);
        try {
            testCheck($announced['size'] === 4, 'Stream header size changed');
            testCheck($runtime::readStream($stdout, 1) === 'AB', 'Header read consumed or lost initial body bytes');
            try {
                $runtime::readStream($stdout, 1);
                throw new RuntimeException('Partial body stall did not time out');
            } catch (RuntimeException $error) {
                testCheck($error->getMessage() === 'artifact-stream-timeout', 'Partial body returned the wrong safe error');
            }
        } finally {
            $stopping = microtime(true);
            $runtime::stopStream($process, [$stdout, $stderr]);
            testCheck(microtime(true) - $stopping <= 7.0, 'Disconnect cleanup exceeded its bound');
        }
        testChildGone($pidFile);
        testCheck(microtime(true) - $phaseStart < 30, 'Runtime phase exceeded thirty seconds');
        $results['stream_idle_startup_partial_output_and_term_cleanup'] = true;
        }
        if (testPhase('first_use')) {
        $currentPhase = 'first_use';
        try {
        $freshRoot = $work . '/test-first-use-' . bin2hex(random_bytes(4));
        testCheck(!file_exists($freshRoot), 'First-use folder must start absent');
        testCheck(mkdir($freshRoot, 0700), 'Could not create empty first-use fixture folder');
        $record->settings()->where('key', 'private_root')->update(['value' => $freshRoot]);
        $record->settings()->whereIn('key', ['full_schedule', 'database_schedule'])->update(['value' => 'off']);
        $record->settings()->where('key', 'upload_s3')->update(['value' => '0']);
        $firstUseRows = rowValues($record);
        $page = Livewire\Livewire::test($class);
        testCheck(!file_exists($freshRoot . '/installation.json'), 'Mount silently initialized first-use state');
        $page->call('saveSettings');
        file_put_contents($freshRoot . '/test-result.json', json_encode([
            'snapshot' => $metadata['release_sha256'], 'save_completed' => true,
            'identity_created' => is_file($freshRoot . '/installation.json'),
            'policy_created' => is_file($freshRoot . '/policy.json'),
            'native_rows_changed' => rowValues($record) !== $firstUseRows,
            'capture_or_schedule_requested' => glob($freshRoot . '/requests/*.json') !== [],
        ], JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR) . "\n");
        chmod($freshRoot . '/test-result.json', 0600);
        testCheck(rowValues($record) === $firstUseRows, 'Unchanged first-use save changed native rows');
        testCheck(is_file($freshRoot . '/installation.json'), 'Unchanged Save settings did not initialize first-use private identity');
        testCheck(is_file($freshRoot . '/policy.json'), 'First-use Save settings did not write private policy');
        testCheck(fileowner($freshRoot) === posix_geteuid() && (fileperms($freshRoot) & 0777) === 0700, 'First-use directory is not owner-private');
        $freshIdentity = json_decode(file_get_contents($freshRoot . '/installation.json'), true, 32, JSON_THROW_ON_ERROR);
        testCheck(($freshIdentity['app'] ?? null) === $base && ($freshIdentity['owner_uid'] ?? null) === posix_geteuid()
                    && is_string($freshIdentity['installation_uuid'] ?? null), 'First-use identity is not bound to fixture');
        testCheck(!is_file($freshRoot . '/credentials.json'), 'First-use save invented credentials');
        testCheck(glob($freshRoot . '/requests/*.json') === [], 'First-use save queued work');
        testCheck(glob($freshRoot . '/runs/*/journal.json') === [], 'First-use save captured a backup');
        testCheck(file_get_contents($root . '/installation.json') === $identity, 'First-use save changed original installation identity');
        testCheck(($originalCredentials === null && !file_exists($credentialFile))
                    || ($originalCredentials !== null && file_get_contents($credentialFile) === $originalCredentials), 'First-use save changed original encrypted credentials');
        $results['unchanged_first_use_save_initializes_only_private_state'] = true;
        } finally {
            foreach (['private_root', 'full_schedule', 'database_schedule', 'upload_s3'] as $key) {
                $record->settings()->where('key', $key)->update(['value' => $initialRows[$key]]);
            }
        }
        }
        if (testPhase('presentation')) {
        $currentPhase = 'presentation';
        foreach ([[0, '0 bytes'], [1024, '1 KiB'], [1572864, '1.5 MiB'],
                  [10485760, '10 MiB'], [20971520, '20 MiB'], [104857600, '100 MiB'],
                  [10737418240, '10 GiB']] as [$bytes, $expected]) {
            testCheck(Paymenter\Extensions\Others\Backups\Messages::bytes($bytes) === $expected,
                        'Incorrect byte display for ' . $bytes . ': expected ' . $expected);
        }
        $results['byte_display_preserves_integer_zeroes'] = true;
        }
        if (testPhase('permission')) {
        $currentPhase = 'permission';
        // Every direct mutator checks permission before reading any submitted state.
        foreach ([$limited, null] as $user) {
            if ($user) { Illuminate\Support\Facades\Auth::login($user); }
            else { Illuminate\Support\Facades\Auth::logout(); }
            testCheck(!$class::canAccess(), 'Insufficient permission admitted the page');
            foreach (['saveSettings'] as $method) {
                try {
                    (new $class)->$method();
                    throw new RuntimeException('Insufficient permission admitted ' . $method);
                } catch (Symfony\Component\HttpKernel\Exception\HttpException $error) {
                    testCheck($error->getStatusCode() === 403, 'Wrong permission refusal');
                }
            }
            try {
                (new Paymenter\Extensions\Others\Backups\Http\DownloadController)('owned');
                throw new RuntimeException('Insufficient permission admitted a download');
            } catch (Symfony\Component\HttpKernel\Exception\HttpException $error) {
                testCheck($error->getStatusCode() === 403, 'Wrong download permission refusal');
            }
        }
        $results['permission_denied'] = true;
        }
        Illuminate\Support\Facades\Auth::login($admin);

        if (testPhase('validation')) {
        $currentPhase = 'validation';
        // Rejected form values must leave native rows byte-for-byte unchanged.
        foreach ([['retention_keep_full' => 0], ['capture_timeout' => 1.5],
                  ['job_lock_paths' => 'relative/lock'], ['local_path' => $base . '/public/backup'],
                  ['upload_s3' => true, 's3_bucket' => '', 's3_encryption' => false, 'plaintext_offsite_acknowledged' => false],
                  ['keep_local' => false, 'upload_s3' => false],
                  ['full_schedule' => 'weekly', 'full_weekdays' => []],
                  ['database_schedule' => 'hourly', 'database_every_hours' => 5]] as $changes) {
            $currentPhase = 'validation:' . implode(',', array_keys($changes));
            $before = rowValues($record);
            $page = submitSettings($class, $changes);
            testCheck(rowValues($record) === $before, 'Invalid settings persisted: ' . implode(',', array_keys($changes)));
            testSecretReset($page);
        }
        $results['validation_before_persistence'] = true;
        $currentPhase = 'validation:native_auditing';
        $auditBefore = App\Models\Audit::count();
        $before = rowValues($record);
        $desired = ((int) $before['retention_keep_full']) + 1;
        $page = submitSettings($class, ['retention_keep_full' => $desired]);
        testCheck((int) $record->settings()->where('key', 'retention_keep_full')->value('value') === $desired, 'Valid setting did not persist');
        testCheck(App\Models\Audit::count() > $auditBefore, 'Native audited setting write did not emit an audit');
        testCheck((int) App\Models\Audit::orderByDesc('id')->first()->user_id === (int) Illuminate\Support\Facades\Auth::id(),
                    'Coordinated native writer lost the authenticated audit actor');
        $writer = Paymenter\Extensions\Others\Backups\SettingsWriter::class;
        $auditBeforeUnchanged = App\Models\Audit::count();
        testCheck($writer::write($record, ['retention_keep_full' => (string) $desired]) === [],
                    'Native writer returned a results for an unchanged row');
        testCheck(App\Models\Audit::count() === $auditBeforeUnchanged, 'Unchanged native row emitted a write audit');
        $databaseBefore = $record->settings()->where('key', 'retention_keep_database')->value('value');
        $gpgBefore = $record->settings()->where('key', 'gpg')->first();
        $gpgAttributes = $gpgBefore?->getAttributes();
        if ($gpgBefore) { $gpgBefore->delete(); }
        try {
            $nativeReceipt = $writer::write($record, ['retention_keep_full' => (string) ($desired + 1),
                'retention_keep_database' => (string) ((int) $databaseBefore + 1), 'gpg' => '/fixture/results-gpg']);
            // The native Extensions editor does not take extension locks. Its
            // later row publication must still survive our rollback.
            $record->settings()->where('key', 'retention_keep_full')->first()->update(['value' => (string) ($desired + 2)]);
            $auditsBeforeRestore = App\Models\Audit::count();
            $skipped = $writer::restore($record, $nativeReceipt);
            testCheck($skipped === ['retention_keep_full'], 'Native compare/restore did not distinguish a concurrent editor');
            testCheck((int) $record->settings()->where('key', 'retention_keep_full')->value('value') === $desired + 2,
                        'Native rollback overwrote a concurrent editor');
            testCheck((string) $record->settings()->where('key', 'retention_keep_database')->value('value') === (string) $databaseBefore,
                        'Native rollback failed to restore an unchanged published row');
            testCheck(!$record->settings()->where('key', 'gpg')->exists(), 'Native rollback manufactured a formerly absent row');
            testCheck(App\Models\Audit::count() > $auditsBeforeRestore, 'Native rollback bypassed Eloquent auditing');
        } finally {
            $writer::write($record, ['retention_keep_full' => (string) $desired]);
            if ($gpgAttributes !== null) {
                $record->settings()->updateOrCreate(['key' => 'gpg'], array_intersect_key($gpgAttributes, array_flip(['value', 'type', 'encrypted'])));
            }
        }
        $results['native_auditing'] = true;
        $results['native_compare_restore_preserves_concurrent_and_absent_rows'] = true;
        $snapshot = Illuminate\Support\Facades\DB::transaction(static fn () => $writer::snapshot($record));
        $editor = null;
        try {
            $writer::finalize($record, $snapshot, function () use (&$editor, $base, $record, $desired) {
                $script = <<<'PHP'
require $argv[1] . '/vendor/autoload.php';
$app = require $argv[1] . '/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$record = App\Models\Extension::findOrFail((int) $argv[2]);
$row = $record->settings()->where('key', 'retention_keep_full')->firstOrFail();
echo "editor-ready\n"; flush();
$row->update(['value' => $argv[3]]);
echo "editor-published\n";
PHP;
                $editor = new Symfony\Component\Process\Process([PHP_BINARY, '-r', $script, $base, (string) $record->id, (string) ($desired + 3)], $base);
                $editor->setTimeout(10);
                $editor->start();
                $output = '';
                $deadline = microtime(true) + 8;
                while (!str_contains($output, 'editor-ready') && $editor->isRunning() && microtime(true) < $deadline) {
                    $output .= $editor->getIncrementalOutput(); usleep(20000);
                }
                testCheck(str_contains($output, 'editor-ready'), 'Concurrent native editor did not reach its row update');
                usleep(150000);
                testCheck($editor->isRunning() && !str_contains($editor->getOutput(), 'editor-published'),
                            'Native editor crossed the locked credential publication callback');
            });
            $editor->wait();
            testCheck($editor->isSuccessful() && str_contains($editor->getOutput(), 'editor-published'),
                        'Concurrent native editor did not resume after credential publication');
            testCheck((int) $record->settings()->where('key', 'retention_keep_full')->value('value') === $desired + 3,
                        'Concurrent native editor publication disappeared after finalization');
        } finally {
            if ($editor?->isRunning()) { $editor->stop(1); }
            $writer::write($record, ['retention_keep_full' => (string) $desired]);
        }
        $results['native_editor_waits_through_credential_publication_callback'] = true;
        }

        if (testPhase('basic_states')) {
        $currentPhase = 'basic_states';
        $runtime = Paymenter\Extensions\Others\Backups\Runtime::class;
        $keys = ['access_key' => 'TEST-SECRET-CANARY-BASIC-ACCESS', 'secret_key' => 'TEST-SECRET-CANARY-BASIC-KEY'];
        $runtime::credentials($keys);
        foreach (['http://fixture-test.example.invalid', 'https://different-test.example.invalid'] as $appUrl) {
            App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'https://fixture-test.example.invalid']);
            App\Providers\SettingsProvider::flushCache();
            $runtime::credentials(['origin' => 'https://fixture-test.example.invalid',
                'username' => 'TEST-SECRET-CANARY-BASIC-USER', 'password' => 'TEST-SECRET-CANARY-BASIC-PASSWORD'], true);
            App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $appUrl]);
            App\Providers\SettingsProvider::flushCache();
            $page = Livewire\Livewire::test($class);
            $page->update(calls: [['method' => '$refresh', 'params' => []]],
                updates: ['settings.full_schedule' => 'off', 'settings.upload_s3' => false]);
            testCheck($page->instance()->removeSitePasswordAction()->isVisible(),
                        'Saved site password removal is unavailable after URL, schedule or storage changes');
            testCheck(str_contains($page->html(), 'HTTP Basic'), 'HTTP Basic purpose disappeared when its form was unavailable');
            $page->update(calls: [['method' => 'mountAction', 'params' => ['removeSitePassword']]], updates: []);
            testCheck(count($page->get('mountedActions') ?? []) === 1, 'HTTP Basic removal confirmation did not mount');
            $partial = modalPartial($page) ?? $page->html();
            testCheck(str_contains($partial, 'Remove the saved site password?'), 'Site password confirmation was not rendered');
            $page->update(calls: [['method' => 'callMountedAction', 'params' => []]], updates: []);
            testSecretReset($page);
            $overview = $runtime::command(['overview']);
            testCheck(($overview['configuration']['saved_keys']['site'] ?? true) === false, 'HTTP Basic removal left credentials configured');
            testCheck(($overview['configuration']['saved_keys']['s3'] ?? false) === true, 'HTTP Basic removal lost S3 keys');
        }
        testCheck(file_get_contents($root . '/installation.json') === $identity, 'HTTP Basic removal changed installation identity');
        $results['basic_removal_available_for_http_mismatch_and_unsaved_switches'] = true;
        }

        if (testPhase('credentials') || testPhase('root')) {
        $currentPhase = 'credentials';
        // Saved credentials are encrypted, write-only and preserve each namespace.
        $page = Livewire\Livewire::test($class);
        $keys = ['access_key' => 'TEST-SECRET-CANARY-ACCESS', 'secret_key' => 'TEST-SECRET-CANARY-KEY', 'session_token' => 'TEST-SECRET-CANARY-TOKEN'];
        // Unencrypted uploads keep this phase about credentials; key setup has its own checks.
        $s3 = ['settings.upload_s3' => true, 'settings.s3_bucket' => 'test-bucket', 'settings.s3_encryption' => false, 'settings.plaintext_offsite_acknowledged' => true];
        $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: [...$s3, 's3Keys' => $keys]);
        testSecretReset($page);
        testCheck(is_file($credentialFile), 'S3 credentials were not saved');
        testCheck(!str_contains(file_get_contents($credentialFile), 'TEST-SECRET-CANARY'), 'Credentials stored as plaintext');
        $httpsOrigin = 'https://fixture-test.example.invalid';
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $httpsOrigin]);
        App\Providers\SettingsProvider::flushCache();
        $site = ['origin' => $httpsOrigin, 'username' => 'TEST-SECRET-CANARY-USER', 'password' => 'TEST-SECRET-CANARY-PASSWORD'];
        $page = Livewire\Livewire::test($class);
        $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['siteAccess' => $site]);
        testSecretReset($page);
        testCheck(!str_contains(file_get_contents($credentialFile), 'TEST-SECRET-CANARY'), 'Site credentials stored as plaintext');
        $bridge = new Symfony\Component\Process\Process([PHP_BINARY, $base . '/extensions/Others/Backups/bridge.php', 'read', '--app', $base, '--private-root', $root]);
        $bridge->mustRun();
        $envelope = json_decode($bridge->getOutput(), true, 32, JSON_THROW_ON_ERROR);
        testCheck(($envelope['credentials']['access_key'] ?? null) === $keys['access_key'], 'Saving site password lost S3 keys');
        testCheck(($envelope['maintenance_probe_credentials']['username'] ?? null) === $site['username'], 'Site password was not saved');
        unset($envelope);
        $encrypted = file_get_contents($credentialFile);
        $page->update(calls: [['method' => '$refresh', 'params' => []]], updates: ['s3Keys' => $keys, 'siteAccess' => $site]);
        testSecretReset($page);
        $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['s3Keys' => ['secret_key' => 'TEST-SECRET-CANARY-FAILURE']]);
        testSecretReset($page);
        testCheck(file_get_contents($credentialFile) === $encrypted, 'Invalid key form changed saved keys');
        $results['encrypted_namespaces_and_secret_reset'] = true;

        // Unreadable stores must survive attempted replacement unchanged.
        $unreadable = json_decode($encrypted, true, 32, JSON_THROW_ON_ERROR);
        $unreadable['payload'] = 'unreadable-test-fixture';
        Paymenter\Extensions\Others\Backups\PrivateStore::atomic($credentialFile, $unreadable);
        $damaged = file_get_contents($credentialFile);
        $page = Livewire\Livewire::test($class);
        $beforeRefusal = rowValues($record);
        testCheck(!is_file($root . '/config-save.json'), 'Unreadable-store scenario started with an earlier save guard');
        session()->forget(['filament.notifications', 'filament.claimed_notifications']);
        $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: [
            'settings.retention_keep_full' => ((int) $beforeRefusal['retention_keep_full']) + 1,
            's3Keys' => $keys,
        ]);
        testSecretReset($page);
        $refusalNotices = array_merge(session()->pull('filament.claimed_notifications') ?? [], session()->pull('filament.notifications') ?? []);
        testCheck($page->instance()->getErrorBag()->isEmpty(), 'Unreadable-store scenario stopped at form validation');
        testCheck(array_filter($refusalNotices, static fn ($n) => ($n['status'] ?? null) === 'danger'
            && ($n['title'] ?? null) === 'Settings not saved'
            && ($n['body'] ?? null) === Paymenter\Extensions\Others\Backups\Messages::error('credentials-unreadable')) !== [],
            'Unreadable store did not report complete Save refusal');
        testCheck(array_filter($refusalNotices, static fn ($n) => ($n['status'] ?? null) === 'success') === [], 'Unreadable-store refusal reported success');
        testCheck(rowValues($record) === $beforeRefusal, 'Unreadable-store refusal changed native settings');
        testCheck(file_get_contents($credentialFile) === $damaged, 'Unreadable bound credentials were replaced');
        testCheck(!is_file($root . '/config-save.json'), 'Definite unreadable-store refusal left an uncertain-save guard');
        file_put_contents($credentialFile, $encrypted);
        chmod($credentialFile, 0600);
        $results['unreadable_store_preserved'] = true;
        testCheck(!str_contains(json_encode(rowValues($record)), 'TEST-SECRET-CANARY'), 'Secret reached native settings');
        }

        if (testPhase('root')) {
        $currentPhase = 'root';
        // A settings edit cannot silently abandon installation identity and secrets.
        $before = rowValues($record);
        $relocated = $work . '/test-new-private';
        submitSettings($class, ['private_root' => $relocated]);
        $effective = Paymenter\Extensions\Others\Backups\Runtime::privateRoot();
        if ($effective !== $root) {
            testCheck(is_file($effective . '/installation.json') && file_get_contents($effective . '/installation.json') === $identity,
                        'Changing initialized private folder abandoned the installation UUID');
            testCheck(is_file($effective . '/credentials.json') && file_get_contents($effective . '/credentials.json') === $encrypted,
                        'Changing initialized private folder abandoned encrypted credentials');
        } else {
            testCheck(rowValues($record) === $before, 'Refused relocation partially changed native settings');
        }
        $results['root_transition_preserves_identity_and_secrets'] = true;
        }

        if (testPhase('repair')) {
        $currentPhase = 'repair';
        $defaults = Paymenter\Extensions\Others\Backups\Settings::DEFAULTS;
        $credentialDigest = is_file($credentialFile) ? hash_file('sha256', $credentialFile) : null;
        $requestCount = count(glob($root . '/requests/*'));
        $runCount = count(glob($root . '/runs/*'));
        foreach (['retention_keep_full' => 'invalid-integer', 'automatic_retention' => 'invalid-boolean',
                  'full_weekdays' => 'invalid-json', 'job_lock_paths' => 'invalid-json'] as $key => $malformed) {
            session()->forget(['filament.notifications', 'filament.claimed_notifications']);
            $record->settings()->where('key', $key)->update(['value' => $malformed]);
            $page = Livewire\Livewire::test($class);
            $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: []);
            testCheck($page->instance()->getErrorBag()->isEmpty(), json_encode($page->instance()->getErrorBag()->toArray()) . " " . 'Visible default could not repair malformed native row: ' . $key);
            $expected = is_array($defaults[$key]) ? json_encode($defaults[$key])
                : (is_bool($defaults[$key]) ? ($defaults[$key] ? '1' : '0') : (string) $defaults[$key]);
            if ($record->settings()->where('key', $key)->value('value') !== $expected) {
                $notifications = array_map(static fn ($item) => array_intersect_key($item, array_flip(['title', 'body', 'status'])),
                    session()->get('filament.notifications', []));
                file_put_contents($work . '/test-repair-effects.json', json_encode(['notifications' => $notifications], JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR));
            }
            testCheck($record->settings()->where('key', $key)->value('value') === $expected,
                        'Unchanged visible default left malformed native row persisted: ' . $key
                        . '; actual=' . json_encode($record->settings()->where('key', $key)->value('value'))
                        . '; expected=' . json_encode($expected));
            $notifications = session()->pull('filament.claimed_notifications') ?? session()->pull('filament.notifications') ?? [];
            testCheck(array_filter($notifications, static fn ($item) => ($item['status'] ?? null) === 'warning'
                && str_contains($item['body'] ?? '', Paymenter\Extensions\Others\Backups\Settings::LABELS[$key][0])) !== [],
                'Repair silently replaced the saved value without warning about its field: ' . $key);
            $record->settings()->where('key', $key)->update(['value' => $initialRows[$key]]);
        }
        // The documented public-directory guard intentionally refuses malformed
        // JSON before a page override. Its supported native editor rules repair it.
        foreach (['invalid-json', '', 'null', 'false', '0', '{}', '{"0":"/tmp"}', '"/tmp"'] as $invalidRoots) {
            $record->settings()->where('key', 'extra_docroots')->update(['value' => $invalidRoots]);
            try {
                Paymenter\Extensions\Others\Backups\Runtime::privateRoot();
                throw new LogicException('Malformed public-directory guard opened private storage');
            } catch (RuntimeException $error) {
                testCheck($error->getMessage() === 'document-roots-invalid', 'Malformed public-directory guard gave a misleading startup error');
            }
            $page = Livewire\Livewire::test($class);
            $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: []);
            testCheck($page->instance()->getErrorBag()->has('settings.extra_docroots'), 'Malformed public-directory guard did not identify its field');
            testCheck($record->settings()->where('key', 'extra_docroots')->value('value') === $invalidRoots, 'Page silently defaulted the public-directory guard');
        }
        $remedy = Paymenter\Extensions\Others\Backups\Messages::error('document-roots-invalid');
        testCheck(str_contains($remedy, 'Extensions') && str_contains($remedy, 'Backups') && str_contains($remedy, 'JSON list'),
                    'Public-directory refusal does not explain the native editor remedy');
        testCheck(Illuminate\Support\Facades\Validator::make(['extra_docroots' => $initialRows['extra_docroots']],
            ['extra_docroots' => Paymenter\Extensions\Others\Backups\Settings::rules('extra_docroots')])->passes(),
            'Original public-directory value is not accepted by native editor validation');
        Paymenter\Extensions\Others\Backups\SettingsWriter::write($record, ['extra_docroots' => $initialRows['extra_docroots']]);
        $page = Livewire\Livewire::test($class);
        $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: []);
        testCheck($page->instance()->getErrorBag()->isEmpty(), 'Explicit native repair did not restore page settings');
        testCheck(file_get_contents($root . '/installation.json') === $identity, 'Repair changed installation identity');
        testCheck(($credentialDigest === null && !is_file($credentialFile))
            || ($credentialDigest !== null && hash_file('sha256', $credentialFile) === $credentialDigest), 'Repair changed encrypted credentials');
        testCheck(count(glob($root . '/requests/*')) === $requestCount && count(glob($root . '/runs/*')) === $runCount,
                    'Repair queued work or captured a backup');
        $results['malformed_native_rows_repaired_by_unchanged_visible_defaults'] = true;
        $results['malformed_public_directory_guard_refuses_with_native_editor_remedy'] = true;
        }

        if (testPhase('cli_config')) {
        $currentPhase = 'cli_config';
        $credentialDigest = is_file($credentialFile) ? hash_file('sha256', $credentialFile) : null;
        $requestsBefore = count(glob($root . '/requests/*'));
        $runsBefore = count(glob($root . '/runs/*'));
        $desired = ((int) $record->settings()->where('key', 'retention_keep_full')->value('value')) + 1;
        $result = Paymenter\Extensions\Others\Backups\Runtime::command(['config', 'set'], ['settings' => ['retention_keep_full' => $desired]]);
        testCheck(($result['updated'] ?? false) === true, 'Actual Python configuration save did not complete its native writer');
        testCheck((int) $record->settings()->where('key', 'retention_keep_full')->value('value') === $desired, 'Actual Python configuration save did not persist the requested value');
        $native = new Symfony\Component\Process\Process([PHP_BINARY, $base . '/artisan', 'backups:config-set'], $base);
        $native->setInput(json_encode(['retention_keep_full' => $desired + 1], JSON_THROW_ON_ERROR));
        $native->setTimeout(30);
        $native->mustRun();
        testCheck((int) $record->settings()->where('key', 'retention_keep_full')->value('value') === $desired + 1,
                    'Default native command did not preserve its ordinary save behavior');
        $advisory = json_decode(file_get_contents($root . '/configuration-advisory.json'), true, 16, JSON_THROW_ON_ERROR);
        testCheck(($advisory['valid'] ?? false) === true, 'Default native command no longer initializes and validates its advisory');
        testCheck(file_get_contents($root . '/installation.json') === $identity, 'Actual CLI configuration save changed installation identity');
        testCheck(($credentialDigest === null && !is_file($credentialFile))
            || ($credentialDigest !== null && hash_file('sha256', $credentialFile) === $credentialDigest), 'Actual CLI configuration save changed encrypted credentials');
        testCheck(count(glob($root . '/requests/*')) === $requestsBefore && count(glob($root . '/runs/*')) === $runsBefore,
                    'Actual CLI configuration save started backup work');
        $results['actual_python_native_configuration_save_completes_without_nested_lock'] = true;
        $results['default_native_configuration_save_retains_init_and_advisory'] = true;
        }

        if (testPhase('baseline')) {
        $currentPhase = 'baseline';
        $before = rowValues($record);
        $desired = ((int) $before['retention_keep_full']) + 2;
        $page = Livewire\Livewire::test($class);
        $settings = array_replace($page->get('settings'), ['retention_keep_full' => $desired]);
        $claimedBaseline = ['retention_keep_full' => $desired];
        if (property_exists($page->instance(), 'loadedSettings')) {
            $claimedBaseline = array_replace($page->get('loadedSettings'), $claimedBaseline);
        }
        $locked = false;
        try {
            $page->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['loadedSettings' => $claimedBaseline, 'settings' => $settings]);
        } catch (Livewire\Features\SupportLockedProperties\CannotUpdateLockedPropertyException | Livewire\Exceptions\PublicPropertyNotFoundException $error) {
            $locked = true;
        }
        if ($locked) {
            testCheck(rowValues($record) === $before, 'Rejected client baseline changed settings');
        } else {
            testCheck((int) $record->settings()->where('key', 'retention_keep_full')->value('value') === $desired,
                        'Client-controlled baseline silently suppressed an explicit setting change');
        }
        $results['client_baseline_cannot_control_persistence'] = true;
        }

        if (testPhase('download')) {
        $currentPhase = 'download';
        foreach (['../owned', '/owned', 'owned/../other', 'owned' . chr(0)] as $run) {
            try {
                (new Paymenter\Extensions\Others\Backups\Http\DownloadController)($run);
                throw new RuntimeException('Download accepted a path-shaped ID');
            } catch (Symfony\Component\HttpKernel\Exception\HttpException $error) {
                testCheck($error->getStatusCode() === 404, 'Wrong malformed download refusal');
            }
        }
        $results['download_path_ids_refused'] = true;
        }

        if (testPhase('download_anchor')) {
        $currentPhase = 'download_anchor';
        // Render the actual native table action with a representative verified
        // record. No archive, request or native row is created for this check.
        Filament\Support\Facades\FilamentView::spa(true);
        $page = Livewire\Livewire::test($class);
        $originalRequest = app('request');
        // Earlier all-phase credential checks may change the fixture app URL.
        // Match the currently generated native link so this remains same-origin.
        $request = Illuminate\Http\Request::create($class::getUrl(), 'GET');
        app()->instance('request', $request);
        app('url')->setRequest($request);
        try {
            testCheck(Filament\Support\Facades\FilamentView::hasSpaMode($class::getUrl()), 'Render fixture did not enable SPA for ordinary admin links');
            $action = $page->instance()->getTable()->getAction('download');
            testCheck($action instanceof Filament\Actions\Action, 'Native download action is missing');
            $action->record(['__key' => 'test-owned', 'key' => 'test-owned', 'kind' => 'run', 'run_id' => 'test-owned',
                             'run' => ['status' => 'ready', 'download' => 'ready']]);
            $html = $action->toHtml();
            $document = new DOMDocument;
            $previousErrors = libxml_use_internal_errors(true);
            try { $document->loadHTML($html); }
            finally { libxml_clear_errors(); libxml_use_internal_errors($previousErrors); }
            $found = null;
            foreach ($document->getElementsByTagName('a') as $anchor) {
                if (str_contains($anchor->getAttribute('href'), '/download/test-owned')) { $found = $anchor; break; }
            }
            testCheck($found !== null, 'Native Download did not render an attachment link');
            foreach ($found->attributes as $attribute) {
                testCheck(!str_starts_with($attribute->name, 'wire:navigate'), 'Download link is intercepted by SPA navigation');
            }
            testCheck(!str_contains(strtolower($found->getAttribute('x-on:click')), 'navigate'), 'Download link has an Alpine SPA handler');
            testCheck($found->getAttribute('target') !== '_blank', 'Download unnecessarily opens a new tab');
            $action->record(['__key' => 'test-owned', 'key' => 'test-owned', 'kind' => 'run', 'run_id' => 'test-owned',
                             'run' => ['status' => 'delete-interrupted', 'download' => 'ready']]);
            testCheck($action->isVisible(), 'Usable local Download is hidden by an unrelated S3-only deletion failure');
            $retrieve = $page->instance()->getTable()->getAction('prepareDownload');
            testCheck($retrieve instanceof Filament\Actions\Action, 'Native S3 retrieval action is missing');
            $retrieve->record(['__key' => 'test-remote-only', 'key' => 'test-remote-only', 'kind' => 'run',
                              'run_id' => 'test-remote-only', 'run' => ['status' => 'ready', 'download' => 'retrieve']]);
            testCheck($retrieve->isVisible() && str_contains($retrieve->toHtml(), 'Download from S3'),
                        'Remote-only verified history has no visible S3 retrieval action');
            testCheck(Filament\Support\Facades\FilamentView::hasSpaMode($class::getUrl()), 'Download disabled ordinary admin SPA navigation');
        } finally {
            app()->instance('request', $originalRequest);
            app('url')->setRequest($originalRequest);
        }
        $results['native_download_anchor_avoids_spa'] = true;
        }

        if (testPhase('delete_gate')) {
        $currentPhase = 'delete_gate';
        // Minimized real Filament action: cached preview proofs separate storage
        // decisions from the live mounted form and its unvalidated Action::$data.
        $page = Livewire\Livewire::test($class);
        $instance = $page->instance();
        $action = $instance->getTable()->getAction('delete');
        testCheck($action instanceof Filament\Actions\Action, 'Native Delete action is missing');
        $runId = 'test-delete-gate';
        $action->record(['__key' => $runId, 'key' => $runId, 'kind' => 'run', 'run_id' => $runId, 'created_at' => time(),
            'run' => ['mode' => 'database-online', 'status' => 'ready', 'local' => true, 'remote' => 'changed']]);
        $action->nestingIndex(0);
        $instance->mountedActions = [['name' => 'delete', 'arguments' => [], 'context' => ['table' => true, 'recordKey' => $runId],
            'data' => ['location' => 'local']]];
        $schema = $action->getSchema(Filament\Schemas\Schema::make($instance)->statePath('mountedActions.0.data'));
        testCheck($schema instanceof Filament\Schemas\Schema, 'Delete confirmation has no native schema');
        $action->schemaContainer($schema);
        $cachedSchemas = new ReflectionProperty($instance, 'cachedSchemas');
        $cachedSchemas->setValue($instance, array_replace($cachedSchemas->getValue($instance), ['mountedActionSchema0' => $schema]));
        $cachedActions = new ReflectionProperty($instance, 'cachedMountedActions');
        $cachedActions->setValue($instance, [$action]);
        $previews = new ReflectionProperty($instance, 'deletePreviews');
        $allowed = ['refusal' => null, 'local_copy' => true, 'remote_copy' => null, 'survivors' => ['test-survivor']];
        $refused = array_replace($allowed, ['refusal' => 'delete-remote-scope-changed']);
        $previews->setValue($instance, [$runId . ':local' => $allowed, $runId . ':all' => $refused, $runId . ':s3' => $refused]);
        $requestsBefore = count(glob($root . '/requests/*'));
        $action->data(['location' => 'local']);
        testCheck($action->getModalSubmitAction() instanceof Filament\Actions\Action,
                    'Control case with matching validated action data does not expose Submit');
        foreach ([[], ['location' => 'all']] as $staleActionData) {
            $action->data($staleActionData);
            $instance->mountedActions[0]['data']['location'] = 'local';
            testCheck(str_contains($schema->toHtml(), 'The backup file on this server'), 'Local form selection did not produce its allowed preview');
            $submit = $action->getModalSubmitAction();
            testCheck($submit instanceof Filament\Actions\Action && str_contains($submit->toHtml(), 'Delete'),
                'Allowed local-only preview hides Submit because unvalidated Action data still defaults to all');
            $instance->mountedActions[0]['data']['location'] = 'all';
            testCheck($action->getModalSubmitAction() === null, 'Refused all-copy selection exposes Submit');
            $instance->mountedActions[0]['data']['location'] = 's3';
            testCheck($action->getModalSubmitAction() === null, 'Refused S3 selection exposes Submit');
        }
        $instance->mountedActions[0]['data']['location'] = 'local';
        $previews->setValue($instance, [$runId . ':local' => array_replace($allowed, ['refusal' => 'delete-last-backup'])]);
        testCheck($action->getModalSubmitAction() === null, 'Last local copy exposes Submit');
        testCheck(count(glob($root . '/requests/*')) === $requestsBefore, 'Rendering the deletion gate queued work');
        $results['delete_confirmation_gate_uses_live_selected_location_before_validation'] = true;
        }

        if (testPhase('field_help')) {
        $currentPhase = 'field_help';
        // Help is inline text, and Help sections carry ids that setup items link to.
        $page = Livewire\Livewire::test($class);
        $html = $page->html();
        testCheck(!str_contains($html, "mountAction('cronLockHelp'") && !str_contains($html, 'Help for '), 'A field still opens a help dialog');
        foreach (['storage', 'full-backup-requirements', 'before-backups-run', 'advanced', 'how-backups-run', 'full-backups', 'restoring', 'command-line'] as $anchor) {
            testCheck(str_contains($html, 'id="' . $anchor . '"'), 'Deep-link target missing: ' . $anchor);
        }
        $results['inline_help_and_deep_link_targets'] = true;
        }

        if (testPhase('cron_fallback')) {
        $currentPhase = 'cron_fallback';
        foreach (['test%cron-private', 'test\\%cron-private'] as $name) {
            $cronRoot = $work . '/' . $name;
            testCheck(!file_exists($cronRoot), 'Cron fallback requires an uninitialized private folder');
            $record->settings()->where('key', 'private_root')->update(['value' => $cronRoot]);
            $page = Livewire\Livewire::test($class);
            $document = new DOMDocument;
            $previousErrors = libxml_use_internal_errors(true);
            try { $document->loadHTML($page->html()); }
            finally { libxml_clear_errors(); libxml_use_internal_errors($previousErrors); }
            $lines = [];
            foreach ((new DOMXPath($document))->query('//*[@data-backups-command]') as $node) {
                $line = trim($node->textContent);
                if (str_starts_with($line, '* * * * * ')) { $lines[] = $line; }
            }
            testCheck(count($lines) >= 2, 'Uninitialized page did not render fallback cron suggestions');
            // Interpret cron's percent handling before shell tokenization. No
            // suggested command executes; the exact two lock paths must survive.
            $python = <<<'PYTHON'
import json, shlex, sys
data = json.load(sys.stdin)
kinds = set()
for line in data['lines']:
    result = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == '%':
            sys.exit(1)
        if char == '\\' and index + 1 < len(line):
            index += 1
            char = line[index]
            if char != '%':
                result.append('\\')
        result.append(char)
        index += 1
    words = shlex.split(''.join(result))
    kind = 'scheduler' if 'schedule:run' in words else 'queue'
    assert words[5:8] == ['/usr/bin/flock', '-n', data['root'] + '/' + kind + '.lock']
    assert words[9] == data['artisan']
    kinds.add(kind)
assert kinds == {'scheduler', 'queue'}
PYTHON;
            $check = new Symfony\Component\Process\Process(['/usr/bin/python3', '-c', $python], $base, null,
                json_encode(['lines' => $lines, 'root' => $cronRoot, 'artisan' => $base . '/artisan'], JSON_THROW_ON_ERROR));
            $check->run();
            testCheck($check->isSuccessful(), 'Fallback cron suggestion changes percent-bearing private path');
            testCheck(!file_exists($cronRoot), 'Rendering fallback cron initialized storage');
        }
        $record->settings()->where('key', 'private_root')->update(['value' => $initialRows['private_root']]);
        $results['cron_fallback_preserves_percent_and_backslash_percent_paths'] = true;
        }

        if (testPhase('recovery_key')) {
        $currentPhase = 'recovery_key';
        // This valid imported passphrase also proves native HTML/JS escaping.
        $passphrase = '  TEST-SECRET-CANARY-<key> "quoted" \\ café  ';
        $runtime = Paymenter\Extensions\Others\Backups\Runtime::class;
        $runtime::encryptionKey('import', ['passphrase' => $passphrase]);
        Illuminate\Support\Facades\URL::forceScheme('https');
        try {
            $page = Livewire\Livewire::test($class);
            $page->call('mountAction', 'showRecoveryKey')->call('callMountedAction');
            testCheck(($page->get('mountedActions')[0]['name'] ?? null) === 'recoveryKey', 'Confirmed Show did not open recovery key modal');
            $document = new DOMDocument;
            $previousErrors = libxml_use_internal_errors(true);
            try { $document->loadHTML('<?xml encoding="UTF-8">' . (modalPartial($page) ?? $page->html())); }
            finally { libxml_clear_errors(); libxml_use_internal_errors($previousErrors); }
            $nodes = (new DOMXPath($document))->query('//*[@data-backups-command]');
            testCheck($nodes->length === 1, 'Recovery key modal has no unique key value');
            $node = $nodes->item(0);
            $valueNode = $node->getElementsByTagName('span')->item(0);
            testCheck($valueNode !== null && $valueNode->textContent === $passphrase,
                        'Recovery key display changed imported passphrase bytes');
            testCheck(str_contains($valueNode->getAttribute('style'), 'white-space: pre-wrap'), 'Recovery key display collapses significant whitespace');
            testCheck($node->getAttribute('role') === 'button' && $node->getAttribute('tabindex') === '0', 'Recovery key Copy is inaccessible');
            testCheck($node->getAttribute('x-on:keydown.enter.prevent') === '$el.click()'
                        && $node->getAttribute('x-on:keydown.space.prevent') === '$el.click()', 'Recovery key keyboard Copy diverges from click');
            $handler = $node->getAttribute('x-on:click');
            testCheck(str_contains($handler, 'clipboard.writeText'), 'Recovery key Copy was not enabled over HTTPS');
            // Execute the exact native click handler with a private clipboard
            // sink. No secret is placed in argv, output or fixture results.
            $javascript = <<<'JS'
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let copied;
const window = {navigator: {clipboard: {writeText: value => { copied = value; }}}};
new Function('window', '$tooltip', '$store', input.handler)(window, () => {}, {theme: 'light'});
if (copied !== input.expected) process.exit(1);
JS;
            $copy = new Symfony\Component\Process\Process(['node', '-e', $javascript], $base, null,
                json_encode(['handler' => $handler, 'expected' => $passphrase], JSON_THROW_ON_ERROR));
            $copy->run();
            testCheck($copy->isSuccessful(), 'Recovery key Copy changed imported passphrase bytes');
            testCheck(!str_contains(json_encode($page->snapshot), 'TEST-SECRET-CANARY'), 'Revealed passphrase entered Livewire state');
            $results['recovery_key_show_and_copy_preserve_exact_passphrase'] = true;
        } finally { Illuminate\Support\Facades\URL::forceScheme(null); }
        }

        if (testPhase('modals')) {
        $currentPhase = 'modals';
        Paymenter\Extensions\Others\Backups\Runtime::credentials([
            'access_key' => 'TEST-SECRET-CANARY-MODAL-ACCESS', 'secret_key' => 'TEST-SECRET-CANARY-MODAL-KEY',
        ]);
        $page = Livewire\Livewire::test($class);
        $page->update(calls: [['method' => 'mountAction', 'params' => ['removeS3Keys']]], updates: ['settings.upload_s3' => true]);
        testCheck(count($page->get('mountedActions') ?? []) === 1, 'Remove S3 keys confirmation did not mount');
        $document = new DOMDocument;
        $previousErrors = libxml_use_internal_errors(true);
        try { $document->loadHTML($page->html()); }
        finally { libxml_clear_errors(); libxml_use_internal_errors($previousErrors); }
        $wrappers = [];
        foreach ($document->getElementsByTagName('div') as $element) {
            if ($element->getAttribute('wire:partial') === 'action-modals') { $wrappers[] = $element; }
        }
        testCheck(count($wrappers) === 1, 'Native action confirmation has missing or duplicate modal wrappers');
        // Filament 5 can return only this native Livewire partial. Testable::html()
        // retains the initial page HTML, so apply the same replacement the browser does.
        $partialHtml = modalPartial($page);
        if (is_string($partialHtml)) {
            $partialDocument = new DOMDocument;
            $previousErrors = libxml_use_internal_errors(true);
            try { $partialDocument->loadHTML($partialHtml); }
            finally { libxml_clear_errors(); libxml_use_internal_errors($previousErrors); }
            $partialWrappers = [];
            foreach ($partialDocument->getElementsByTagName('div') as $element) {
                if ($element->getAttribute('wire:partial') === 'action-modals') { $partialWrappers[] = $element; }
            }
            testCheck(count($partialWrappers) === 1, 'Native modal partial has missing or duplicate wrappers');
            $replacement = $document->importNode($partialWrappers[0], true);
            $wrappers[0]->parentNode->replaceChild($replacement, $wrappers[0]);
            $wrappers = [$replacement];
        }
        foreach ($wrappers as $wrapper) {
            for ($ancestor = $wrapper->parentNode; $ancestor instanceof DOMElement; $ancestor = $ancestor->parentNode) {
                testCheck($ancestor->getAttribute('role') !== 'tabpanel', 'Native action modal is trapped inside a tab panel');
            }
        }
        testCheck(str_contains($wrappers[0]->textContent, 'Remove saved S3 keys?'), 'Confirmation heading was not rendered outside the tabs');
        testSecretReset($page);
        $results['native_action_modals_render_outside_tabs'] = true;
        }
    } finally {
        // Restore fixture configuration and fake encrypted data before the next test.
        if (!in_array($selected, ['permission', 'download', 'download_anchor', 'presentation', 'runtime'], true)) {
        foreach ($initialRows as $key => $value) {
            $record->settings()->where('key', $key)->update(['value' => $value]);
        }
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $originalUrl]);
        App\Providers\SettingsProvider::flushCache();
        if ($originalCredentials === null) { @unlink($credentialFile); }
        else { file_put_contents($credentialFile, $originalCredentials); chmod($credentialFile, 0600); }
        }
    }
    echo json_encode($results, JSON_THROW_ON_ERROR) . "\n";
} catch (Throwable $error) {
    // Inputs and fixture envelopes are never included in failure diagnostics.
    fwrite(STDERR, json_encode(['phase' => $currentPhase ?? 'bootstrap', 'passed' => $results ?? [], 'error_class' => get_class($error), 'error' => $error->getMessage()], JSON_THROW_ON_ERROR) . "\n");
    exit(1);
}
