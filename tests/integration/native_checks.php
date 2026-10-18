<?php
/** Runs only in the disposable pinned fixture created by native_integration.py. */
try {
$base = $argv[1];
require $base . '/vendor/autoload.php';
$app = require $base . '/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$mode = $argv[2];
function check($test, $message) { if (!$test) { throw new RuntimeException($message); } }
if ($mode === 'upload') {
    $service = new App\Services\Extensions\UploadExtensionService;
    check($service->handle($argv[3]) === 'other', 'native uploader failed');
    $record = App\Models\Extension::firstOrCreate(['extension' => 'Backups', 'type' => 'other'], ['name' => 'Backups', 'enabled' => true]);
    (new Paymenter\Extensions\Others\Backups\Backups)->installed();
    $record->update(['enabled' => true]);
    echo json_encode(['uploaded' => true]) . "\n";
} elseif ($mode === 'legacy-install') {
    // Only the disposable legacy fixture uses this pre-initialization seed.
    check(!App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->exists(), 'legacy identity already installed');
    check(str_starts_with($argv[3], '/') && !file_exists($argv[3]) && basename($base) === 'legacy-app', 'legacy fixture boundary invalid');
    $record = App\Models\Extension::create(['extension' => 'Backups', 'type' => 'other', 'name' => 'Backups legacy fixture', 'enabled' => true]);
    foreach (['settings_version' => '1', 'private_root' => $argv[3], 'python' => $argv[4], 'enabled' => '0'] as $key => $value) {
        $record->settings()->create(['key' => $key, 'value' => $value, 'type' => 'string', 'encrypted' => false]);
    }
    (new Paymenter\Extensions\Others\Backups\Backups)->installed();
    echo json_encode(['legacy_installed' => true]) . "\n";
} elseif ($mode === 'seed') {
    foreach (['cronjob_time' => '23:59', 'timezone' => 'UTC', 'app_url' => $argv[3]] as $key => $value) {
        App\Models\Setting::whereNull('settingable_type')->updateOrCreate(['key' => $key], ['value' => $value, 'type' => 'string', 'encrypted' => false]);
    }
    App\Providers\SettingsProvider::flushCache();
    $role = App\Models\Role::firstOrCreate(['name' => 'integration-admin'], ['permissions' => ['*']]);
    $user = App\Models\User::withoutEvents(fn () => App\Models\User::create(['first_name' => 'Fixture', 'last_name' => 'User', 'email' => 'fixture@example.invalid', 'password' => 'fixture-password', 'role_id' => $role->id, 'tfa_secret' => 'TFA-RESTORE-CANARY']));
    echo json_encode(['id' => $user->id]) . "\n";
} elseif ($mode === 'check') {
    // Many Livewire renders of a large page in one process.
    ini_set('memory_limit', '512M');
    $record = App\Models\Extension::where('extension', 'Backups')->firstOrFail();
    check(!$record->settings()->where('encrypted', true)->exists(), 'encrypted native secret row');
    foreach ($record->settings as $setting) { check(!str_contains($setting->value ?? '', 'CREDENTIAL-CANARY'), 'credential in native row'); }
    $root = $record->settings()->where('key', 'private_root')->value('value');
    $process = new Symfony\Component\Process\Process([PHP_BINARY, $base . '/extensions/Others/Backups/bridge.php', 'read', '--app', $base, '--private-root', $root]);
    $process->mustRun();
    $cold = json_decode($process->getOutput(), true, 32, JSON_THROW_ON_ERROR);
    check($cold['framework']['timezone'] === config('app.timezone'), 'cold timezone differs');
    check($cold['framework']['app_url'] === config('app.url'), 'cold URL differs');
    check($cold['database']['name'] === Illuminate\Support\Facades\DB::connection()->getConfig('database'), 'cold DB differs');
    $extension = App\Helpers\ExtensionHelper::getExtension('other', 'Backups', $record->settings);
    foreach ($record->settings as $setting) { check((string) $setting->value === (string) $extension->config($setting->key), 'native config differs'); }
    $admin = App\Models\User::where('email', 'fixture@example.invalid')->firstOrFail();
    Illuminate\Support\Facades\Auth::login($admin);
    Filament\Facades\Filament::setCurrentPanel(Filament\Facades\Filament::getPanel('admin'));
    check(Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::canAccess(), 'admin denied');
    $page = Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::class;
    $nativeRows = fn () => $record->settings()->pluck('value', 'key')->all();
    // Replay the exact schema action context a browser sends from rendered HTML.
    $schemaAction = function ($livewire, string $name) {
        $html = $livewire->html();
        check((bool) preg_match("/mountAction\('" . preg_quote($name, '/') . "', \{\}, JSON\.parse\('([^']*)'\)\)/", $html, $match), 'rendered action missing: ' . $name);
        return json_decode(json_decode('"' . $match[1] . '"'), true, 8, JSON_THROW_ON_ERROR);
    };
    $digest = fn () => is_file($root . "/credentials.json") ? hash_file("sha256", $root . "/credentials.json") : null;
    $secretFields = fn ($livewire) => array_filter([...array_values($livewire->get('s3Keys') ?? []), ...array_values($livewire->get('siteAccess') ?? []), $livewire->get('encryptionInput.passphrase')], fn ($value) => $value !== null && $value !== '');

    $save = function (array $updates) use ($page, $record) {
        $livewire = Livewire\Livewire::test($page);
        $fullSchedule = $updates['settings.full_schedule'] ?? $record->settings()->where('key', 'full_schedule')->value('value') ?? 'off';
        if ($fullSchedule !== 'off') {
            check(Illuminate\Support\Facades\Artisan::call('backups:guard') === 0, 'billing guard refresh failed');
        }
        $livewire->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: $updates);
        return $livewire;
    };
    $notices = function () {
        $items = array_merge(session()->pull('filament.claimed_notifications') ?? [], session()->pull('filament.notifications') ?? []);
        return array_map(static fn ($item) => ['status' => $item['status'] ?? null, 'title' => $item['title'] ?? '', 'body' => (string) ($item['body'] ?? '')], $items);
    };
    $partials = fn ($livewire) => html_entity_decode(implode("\n", $livewire->effects['partials'] ?? []));
    $text = fn ($livewire) => html_entity_decode($livewire->html());
    $bridgeRead = function () use ($process) { $process->mustRun(); return json_decode($process->getOutput(), true, 32, JSON_THROW_ON_ERROR); };
    session()->forget(['filament.notifications', 'filament.claimed_notifications']);

    // Fields explain themselves; no field opens a help dialog.
    $fresh = Livewire\Livewire::test($page);
    check(!str_contains($fresh->html(), "mountAction('cronLockHelp'") && !str_contains($fresh->html(), 'Help for '), 'field help dialog returned');
    // The billing window is shown as clock times for a scheduled full backup.
    $fresh->set('settings.full_schedule', 'daily');
    check(str_contains($text($fresh), 'Full backups won\'t start between'), 'billing window not shown as clock times');

    // A malformed saved row falls back visibly and is repaired by Save, with a named warning.
    $originalKeep = $record->settings()->where('key', 'retention_keep_full')->value('value');
    $record->settings()->where('key', 'retention_keep_full')->update(['value' => 'malformed-fixture']);
    $save([]);
    check((int) $record->settings()->where('key', 'retention_keep_full')->value('value') === Paymenter\Extensions\Others\Backups\Settings::DEFAULTS['retention_keep_full'], 'Saving defaults did not repair malformed native row');
    check(array_filter($notices(), static fn ($n) => $n['status'] === 'warning' && str_contains($n['body'], Paymenter\Extensions\Others\Backups\Settings::LABELS['retention_keep_full'][0])) !== [], 'Malformed row repair was not disclosed');
    $record->settings()->where('key', 'retention_keep_full')->update(['value' => $originalKeep]);
    // Hidden malformed JSON still reaches authoritative config validation and repair.
    $hiddenOriginal = $record->settings()->whereIn('key', ['full_weekdays', 'full_schedule'])->pluck('value', 'key')->all();
    $record->settings()->where('key', 'full_schedule')->update(['value' => 'off']);
    $record->settings()->where('key', 'full_weekdays')->update(['value' => 'invalid-json']);
    $process->mustRun();
    $invalidCold = json_decode($process->getOutput(), true);
    check($invalidCold['settings']['full_weekdays'] === 'invalid-json', 'Cold bridge silently changed malformed weekdays');
    $invalidEngine = new Symfony\Component\Process\Process([
        $invalidCold['tools']['python'], '-I', $base . '/extensions/Others/Backups/runtime/cli.py',
        '--app', $base, '--private-root', $root, '--php', PHP_BINARY, 'config', 'validate',
    ]);
    $invalidEngine->run();
    check($invalidEngine->getExitCode() !== 0 && str_contains($invalidEngine->getOutput() . $invalidEngine->getErrorOutput(), 'config-invalid'), 'Malformed weekdays did not refuse ordinary engine validation');
    $hiddenRepair = $save([]);
    check($hiddenRepair->instance()->getErrorBag()->isEmpty() && $record->settings()->where('key', 'full_weekdays')->value('value') === '[6]', 'Saving defaults did not repair hidden malformed weekdays');
    check(array_filter($notices(), static fn ($n) => str_contains($n['body'], 'Full backup days')) !== [], 'Malformed weekday repair was not disclosed');
    foreach ($hiddenOriginal as $key => $value) { $record->settings()->where('key', $key)->update(['value' => $value]); }

    // Engine-matching field rules refuse before any native write.
    $before = $nativeRows();
    foreach ([['settings.retention_keep_full', 0, 'settings.retention_keep_full'], ['settings.local_byte_budget', 0, 'settings.local_byte_budget'],
              ['settings.capture_timeout', '1.5', 'settings.capture_timeout'], ['settings.job_lock_paths', 'relative.lock', 'settings.job_lock_paths'],
              ['settings.database_minute', 60, null], ['settings.s3_endpoint', 'http://fixture.invalid', null]] as [$field, $value, $errorKey]) {
        $attempt = $save([$field => $value]);
        if ($errorKey) { check($attempt->instance()->getErrorBag()->has($errorKey), 'invalid setting accepted: ' . $field); }
        check($nativeRows() === $before, 'invalid setting reached native rows: ' . $field);
    }
    $weekly = $save(['settings.full_schedule' => 'weekly', 'settings.full_weekdays' => []]);
    check($weekly->instance()->getErrorBag()->has('settings.full_weekdays') && $nativeRows() === $before, 'weekly schedule without days accepted');
    $neither = $save(['settings.keep_local' => false, 'settings.upload_s3' => false]);
    check($neither->instance()->getErrorBag()->has('settings.keep_local') && $nativeRows() === $before, 'storage with no place accepted');
    // Engine-only refusal: a full backup over Paymenter's observed billing minute.
    $overlap = $save(['settings.full_schedule' => 'daily', 'settings.full_time' => '23:59']);
    check($overlap->instance()->getErrorBag()->has('settings.full_time') && $nativeRows() === $before, 'engine schedule refusal not mapped before persistence');
    $notices();

    // A valid save writes only changed native rows and never touches credentials.
    config(['audit.console' => true]); // This CLI process stands in for an admin web request.
    $credentialDigest = $digest();
    $changedKeys = ['full_schedule', 'full_weekdays', 'retention_keep_database', 'job_lock_paths'];
    $livewire = $save(['settings.full_schedule' => 'weekly', 'settings.full_weekdays' => ['6', '0'], 'settings.retention_keep_database' => 4,
        'settings.job_lock_paths' => "/fixture/a.lock\n\n /fixture/b.lock "]);
    check($livewire->instance()->getErrorBag()->isEmpty(), 'valid page settings refused: ' . json_encode($livewire->instance()->getErrorBag()->toArray()));
    $after = $nativeRows();
    check($after['full_schedule'] === 'weekly' && $after['retention_keep_database'] === '4' && json_decode($after['full_weekdays'], true) === [0, 6]
        && json_decode($after['job_lock_paths'], true) === ['/fixture/a.lock', '/fixture/b.lock'] && $after['settings_version'] === '2', 'page settings not persisted in engine form: ' . json_encode(array_intersect_key($after, array_flip($changedKeys))));
    check(array_diff_key($after, array_flip($changedKeys)) === array_diff_key($before, array_flip($changedKeys)), 'page save changed unrelated rows');
    check(!str_contains(html_entity_decode(Livewire\Livewire::test($page)->html()), 'Next full backup Off'), 'full schedule not summarized');
    $save(['settings.full_schedule' => 'off']);
    check($nativeRows()['full_schedule'] === 'off' && json_decode($nativeRows()['full_weekdays'], true) === [0, 6], 'schedule could not be turned off');
    check($after['local_byte_budget'] === $before['local_byte_budget'], 'unchanged byte limit rewritten');
    // Space settings are entered in GiB and stored in bytes.
    $save(['settings.local_byte_budget' => '1.5']);
    check($nativeRows()['local_byte_budget'] === (string) (int) (1.5 * 1073741824), 'GiB space limit not stored in bytes');
    $save(['settings.local_byte_budget' => $before['local_byte_budget'] / 1073741824]);
    check($digest() === $credentialDigest, 'settings save touched credentials');
    check(App\Models\Audit::where('auditable_type', (new App\Models\Setting)->getMorphClass())->get()->contains(
        static fn ($audit) => json_decode($audit->new_values['value'] ?? '', true) === [0, 6]
    ), 'page settings bypassed native auditing');
    $save(['settings.job_lock_paths' => '']);
    check($nativeRows()['job_lock_paths'] === '[]', 'line list could not be cleared');
    $notices();

    // S3 keys are saved with settings, write-only, and validated as a complete pair before anything is written.
    $secrets = ['s3Keys.access_key' => 'ACCESS-CREDENTIAL-CANARY', 's3Keys.secret_key' => 'SECRET-CREDENTIAL-CANARY', 's3Keys.session_token' => 'TOKEN-CREDENTIAL-CANARY'];
    $s3Settings = ['settings.upload_s3' => true, 'settings.s3_bucket' => 'fixture-bucket', 'settings.s3_encryption' => false, 'settings.plaintext_offsite_acknowledged' => true];
    $rowsBeforeS3 = $nativeRows();
    $partial = $save([...$s3Settings, 's3Keys.access_key' => 'FAILURE-CREDENTIAL-CANARY', 's3Keys.secret_key' => '']);
    check($partial->instance()->getErrorBag()->has('s3Keys.secret_key'), 'missing secret key accepted');
    check($secretFields($partial) === [] && !str_contains($partial->html(), 'CREDENTIAL-CANARY'), 'validation error returned secret input');
    check($nativeRows() === $rowsBeforeS3 && $digest() === $credentialDigest, 'incomplete key pair wrote settings or credentials');
    $unacknowledged = $save([...$s3Settings, 'settings.plaintext_offsite_acknowledged' => false, ...$secrets]);
    check($unacknowledged->instance()->getErrorBag()->has('settings.plaintext_offsite_acknowledged') && $nativeRows() === $rowsBeforeS3, 'unencrypted uploads accepted without confirmation');
    $livewire = $save([...$s3Settings, ...$secrets]);
    check($livewire->instance()->getErrorBag()->isEmpty() && $nativeRows()['upload_s3'] === '1' && $nativeRows()['s3_encryption'] === '0', 'S3 settings not saved');
    check($secretFields($livewire) === [] && !str_contains($livewire->html(), 'CREDENTIAL-CANARY'), 'S3 key save kept input');
    $bound = $bridgeRead();
    check($bound['credentials'] === ['access_key' => 'ACCESS-CREDENTIAL-CANARY', 'secret_key' => 'SECRET-CREDENTIAL-CANARY', 'session_token' => 'TOKEN-CREDENTIAL-CANARY'], 'S3 keys not stored');
    check(!str_contains(file_get_contents($root . '/credentials.json'), 'CREDENTIAL-CANARY'), 'private credentials not encrypted');
    $credentialDigest = $digest();
    // Saved keys are not required again, and a settings-only save leaves them untouched.
    $livewire = $save(['settings.s3_region' => 'eu-west-1']);
    check($livewire->instance()->getErrorBag()->isEmpty() && $digest() === $credentialDigest, 'settings-only save required or rewrote saved keys');
    $livewire->update(calls: [['method' => '$refresh', 'params' => []]], updates: $secrets);
    check($secretFields($livewire) === [] && !str_contains($livewire->html(), 'CREDENTIAL-CANARY'), 'unrelated response returned S3 input');
    $notices();

    // Site password: origin binding, unreadable-store preservation, explicit removal.
    $originalUrl = App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->value('value');
    App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'https://fixture.example.invalid']);
    App\Providers\SettingsProvider::flushCache();
    try {
        $probe = Livewire\Livewire::test($page);
        check(str_contains($text($probe), 'opens https://fixture.example.invalid'), 'site address not shown from app URL');
        check(str_contains($text($probe), 'asks for a username and password') && str_contains($text($probe), 'Not your Paymenter login'), 'site password purpose missing before it is saved');
        // The browser cannot choose the address; a sent origin is ignored.
        $probeSecrets = ['siteAccess.origin' => 'https://other.invalid', 'siteAccess.username' => 'PROBE-USER-CREDENTIAL-CANARY', 'siteAccess.password' => 'PROBE-PASSWORD-CREDENTIAL-CANARY'];
        $probe = $save($probeSecrets);
        check($probe->instance()->getErrorBag()->isEmpty() && $secretFields($probe) === [], 'site password save failed or kept input');
        check(str_contains($text(Livewire\Livewire::test($page)), 'Saved for https://fixture.example.invalid'), 'saved site password state not shown');
        $bound = $bridgeRead();
        check($bound['maintenance_probe_credentials'] === ['origin' => 'https://fixture.example.invalid', 'username' => 'PROBE-USER-CREDENTIAL-CANARY', 'password' => 'PROBE-PASSWORD-CREDENTIAL-CANARY'], 'probe namespace not bound');
        check($bound['credentials']['access_key'] === 'ACCESS-CREDENTIAL-CANARY', 'probe save lost S3 keys');
        $credentialDigest = $digest();
        // Paymenter on plain http: no site password is sent or saved.
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'http://fixture.example.invalid']);
        App\Providers\SettingsProvider::flushCache();
        $plain = Livewire\Livewire::test($page);
        check(str_contains($text($plain), 'uses http://') && str_contains($text($plain), 'asks for a username and password'), 'http address or site password purpose not explained');
        check(str_contains($plain->html(), "mountAction('removeSitePassword'"), 'saved site password cannot be removed over HTTP');
        $plain = $save($probeSecrets);
        check($plain->instance()->getErrorBag()->has('siteAccess.username') && $digest() === $credentialDigest && $secretFields($plain) === [], 'http address saved a site password or kept input');
        $httpRemoval = Livewire\Livewire::test($page);
        $httpRemoval->call('mountAction', 'removeSitePassword', [], $schemaAction($httpRemoval, 'removeSitePassword'))->call('callMountedAction');
        $httpCleared = $bridgeRead();
        check($httpCleared['maintenance_probe_credentials'] === [] && $httpCleared['credentials']['access_key'] === 'ACCESS-CREDENTIAL-CANARY', 'HTTP removal failed or lost S3 keys');
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'https://fixture.example.invalid']);
        App\Providers\SettingsProvider::flushCache();
        $save($probeSecrets);
        $encryptedBackup = json_decode(file_get_contents($root . '/credentials.json'), true);
        Paymenter\Extensions\Others\Backups\PrivateStore::atomic($root . '/credentials.json', array_replace($encryptedBackup, ['payload' => 'unreadable-fixture-ciphertext']));
        $unreadable = $digest();
        $beforeRefusal = $nativeRows();
        check(!is_file($root . '/config-save.json'), 'unreadable-store scenario started with an earlier save guard');
        $notices();
        // S3 replacements remain valid when the unreadable overview cannot
        // supply the site form's HTTPS origin. Reach the real Save coordinator,
        // rather than stopping at unrelated site-field validation.
        $refused = $save([...$secrets, 'settings.retention_keep_full' => ((int) $beforeRefusal['retention_keep_full']) + 1]);
        check($digest() === $unreadable && $secretFields($refused) === [], 'unreadable store overwritten or input kept');
        check($refused->instance()->getErrorBag()->isEmpty(), 'unreadable-store scenario stopped at form validation');
        $refusalNotices = $notices();
        check(array_filter($refusalNotices, static fn ($n) => $n['status'] === 'danger' && $n['title'] === 'Settings not saved'
            && $n['body'] === Paymenter\Extensions\Others\Backups\Messages::error('credentials-unreadable')) !== [], 'unreadable store did not report complete Save refusal');
        check(array_filter($refusalNotices, static fn ($n) => $n['status'] === 'success') === [], 'unreadable-store refusal reported success');
        check($nativeRows() === $beforeRefusal, 'unreadable-store refusal changed native settings');
        check(!is_file($root . '/config-save.json'), 'definite unreadable-store refusal left its new save guard');
        Paymenter\Extensions\Others\Backups\PrivateStore::atomic($root . '/credentials.json', $encryptedBackup);
        $removal = Livewire\Livewire::test($page);
        $removal->call('mountAction', 'removeSitePassword', [], $schemaAction($removal, 'removeSitePassword'))->call('callMountedAction');
        $cleared = $bridgeRead();
        check($cleared['maintenance_probe_credentials'] === [] && $cleared['credentials']['secret_key'] === 'SECRET-CREDENTIAL-CANARY', 'site password removal lost S3 keys');
        $save($probeSecrets);
    } finally {
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $originalUrl]);
        App\Providers\SettingsProvider::flushCache();
    }
    $removal = Livewire\Livewire::test($page);
    $removal->call('mountAction', 'removeS3Keys', [], $schemaAction($removal, 'removeS3Keys'))->call('callMountedAction');
    $removed = $bridgeRead();
    check($removed['credentials'] === [] && $removed['maintenance_probe_credentials']['username'] === 'PROBE-USER-CREDENTIAL-CANARY', 'S3 key removal lost the probe namespace');

    // Encrypted uploads: Save creates a recovery key, shows it only inside its dialog, and confirmation activates it.
    $encrypted = $save([...$s3Settings, 'settings.s3_encryption' => true, 'settings.plaintext_offsite_acknowledged' => false, ...$secrets]);
    check($encrypted->instance()->getErrorBag()->isEmpty() && $nativeRows()['s3_encryption'] === '1' && $nativeRows()['plaintext_offsite_acknowledged'] === '0', 'encrypted S3 settings not saved');
    // The key renders only after the confirmed Show step: a direct mount or ?action= link reveals nothing.
    $direct = Livewire\Livewire::test($page);
    $direct->call('mountAction', 'recoveryKey');
    check(!preg_match('/\b[0-9a-f]{64}\b/', $partials($direct)) && ($direct->get('mountedActions') ?? []) === [], 'recovery key revealed without the Show confirmation');
    $notices();
    // The dialog opens from the browser after the saved page renders.
    check(str_contains(json_encode($encrypted->effects['xjs'] ?? []), 'showRecoveryKey'), 'new recovery key was not offered right after saving');
    $encrypted->call('mountAction', 'showRecoveryKey')->call('callMountedAction');
    $shown = $partials($encrypted);
    check((bool) preg_match('/\b[0-9a-f]{64}\b/', $shown), 'recovery key dialog did not show the key');
    preg_match('/\b[0-9a-f]{64}\b/', $shown, $match);
    $snapshot = json_encode($encrypted->snapshot);
    check(!str_contains($snapshot, $match[0]) || str_contains($snapshot, '"recoveryFingerprint":"' . $match[0] . '"'), 'recovery key reached Livewire state');
    $key = Paymenter\Extensions\Others\Backups\Runtime::command(['overview'])['configuration']['encryption']['key'] ?? [];
    check(($key['state'] ?? null) === 'pending' || filled($key['pending_short'] ?? null), 'generated key is not pending confirmation');
    $confirm = Livewire\Livewire::test($page);
    $confirm->call('mountAction', 'acknowledgeRecoveryKey', [], $schemaAction($confirm, 'acknowledgeRecoveryKey'));
    $confirm->call('callMountedAction');
    check($confirm->instance()->getErrorBag()->has('mountedActions.0.data.saved'), 'recovery key confirmed without the checkbox');
    $confirm->set('mountedActions.0.data.saved', true)->call('callMountedAction');
    $key = Paymenter\Extensions\Others\Backups\Runtime::command(['overview'])['configuration']['encryption']['key'] ?? [];
    check(($key['state'] ?? null) === 'active' && ($key['acknowledged'] ?? false) === true, 'recovery key confirmation did not activate the key');
    $save(['settings.upload_s3' => false]);
    check($nativeRows()['upload_s3'] === '0', 'uploads could not be turned off');
    $credentialDigest = $digest();
    $notices();

    // Ordinary Save must not turn a malformed privacy guard into an empty list.
    $oldDocroots = $record->settings()->where('key', 'extra_docroots')->value('value');
    $record->settings()->where('key', 'extra_docroots')->update(['value' => 'invalid-json']);
    $guardRows = $nativeRows();
    $guardPage = $save([]);
    check($nativeRows() === $guardRows && ($guardPage->instance()->getErrorBag()->has('settings.confirm_public_directories')
        || $guardPage->instance()->getErrorBag()->has('settings.extra_docroots')), 'Page silently defaulted malformed document-root guard');
    check(str_contains($text($guardPage), 'Public web folders need correction'), 'Malformed guard repair not offered in Backups settings');
    $process->run();
    check($process->getExitCode() !== 0 && str_contains($process->getErrorOutput(), 'document-roots-invalid'), 'Malformed document-root guard did not return its safe code');
    $invalidRepair = $save(['settings.extra_docroots' => 'relative', 'settings.confirm_public_directories' => true]);
    check(!$invalidRepair->instance()->getErrorBag()->isEmpty() && $nativeRows() === $guardRows && $digest() === $credentialDigest, 'Invalid privacy repair changed state');
    $guardEditor = $save(['settings.extra_docroots' => implode("\n", json_decode($oldDocroots, true)), 'settings.confirm_public_directories' => true]);
    check($guardEditor->instance()->getErrorBag()->isEmpty() && $record->settings()->where('key', 'extra_docroots')->value('value') === $oldDocroots, 'Explicit confirmed privacy repair failed');
    check($digest() === $credentialDigest, 'Privacy repair changed credentials');

    // The real upstream lifecycle page cannot edit configuration, even with forged state.
    $native = Livewire\Livewire::test(App\Admin\Resources\ExtensionResource\Pages\EditExtension::class, ['record' => $record->getRouteKey()]);
    check(!str_contains($native->html(), 'CREDENTIAL-CANARY'), 'native editor secret leak');
    check(!str_contains($native->html(), 'Full backup days') && !str_contains($native->html(), 'Legacy: automatic backups'), 'native duplicate settings returned');
    check((new Paymenter\Extensions\Others\Backups\Backups)->getConfig() === [], 'Native settings schema is not empty');
    $lifecycleRows = Paymenter\Extensions\Others\Backups\SettingsWriter::snapshot($record);
    $privateHashes = static function () use ($root): array {
        $hashes = [];
        foreach (new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS)) as $file) {
            if ($file->isFile()) { $hashes[$file->getPathname()] = hash_file('sha256', $file->getPathname()); }
        }
        ksort($hashes); return $hashes;
    };
    $lifecyclePrivate = $privateHashes();
    $native->call('save');
    check($native->instance()->getErrorBag()->isEmpty(), 'native edit save has errors: ' . json_encode($native->instance()->getErrorBag()->toArray()));
    check($digest() === $credentialDigest, 'native editor changed credentials');
    foreach (['data.settings.settings_version' => '1', 'data.settings.retention_keep_full' => '0', 'data.settings.full_weekdays' => '[7]', 'data.settings.database_every_hours' => '5', 'data.settings.extra_docroots' => '["relative"]', 'data.settings.private_root' => $base . '/public', 'data.settings.s3_secret_key' => 'FORGED-SECRET'] as $field => $value) {
        $invalid = Livewire\Livewire::test(App\Admin\Resources\ExtensionResource\Pages\EditExtension::class, ['record' => $record->getRouteKey()]);
        $invalid->update(calls: [['method' => 'save', 'params' => []]], updates: [$field => $value]);
        check(Paymenter\Extensions\Others\Backups\SettingsWriter::snapshot($record) === $lifecycleRows
            && $privateHashes() === $lifecyclePrivate && $digest() === $credentialDigest, 'Native lifecycle Save mutated settings/private state: ' . $field);
    }
    foreach ([false, true] as $enabled) {
        Livewire\Livewire::test(App\Admin\Resources\ExtensionResource\Pages\EditExtension::class, ['record' => $record->getRouteKey()])
            ->set('data.enabled', $enabled)->call('save');
        check((bool) $record->fresh()->enabled === $enabled && Paymenter\Extensions\Others\Backups\SettingsWriter::snapshot($record) === $lifecycleRows
            && $privateHashes() === $lifecyclePrivate, 'Native lifecycle toggle changed configuration/private state');
    }
    $additionalDocroot = dirname($base) . '/additional-site-public';
    if (!is_dir($additionalDocroot)) { mkdir($additionalDocroot, 0700); }
    $native = $save(['settings.extra_docroots' => $additionalDocroot]);
    check($native->instance()->getErrorBag()->isEmpty(), 'Backups docroot edit invalid');
    $policy = json_decode(file_get_contents($root . '/policy.json'), true);
    check(in_array(realpath($additionalDocroot), $policy['extra_docroots'], true), 'Backups GUI docroot did not refresh offline policy');
    check($digest() === $credentialDigest, 'docroot edit changed credentials');

    // Standalone CLI can correct an unusable stored pin while explicitly bound
    // to the existing canonical private directory. It cannot select a new one.
    $canonicalIdentity = file_get_contents($root . '/installation.json');
    $rootAlias = $root . '-alias';
    check(!file_exists($rootAlias) && !is_link($rootAlias) && symlink($root, $rootAlias), 'Private-root alias fixture not isolated');
    $cliCommand = [
        $record->settings()->where('key', 'python')->value('value') ?: '/usr/bin/python3', '-I',
        $base . '/extensions/Others/Backups/runtime/cli.py', '--app', $base, '--private-root', $root,
        '--php', PHP_BINARY];
    $cli = new Symfony\Component\Process\Process([...$cliCommand, 'config', 'set'], $base);
    $cli->setTimeout(40);
    try {
        foreach ([$rootAlias, dirname($root) . '/../' . basename(dirname($root)) . '/' . basename($root), '../' . basename(dirname($root)) . '/' . basename($root)] as $invalidRoot) {
            $record->settings()->where('key', 'private_root')->update(['value' => $invalidRoot]);
            try {
                Paymenter\Extensions\Others\Backups\Runtime::privateRoot();
                throw new LogicException('Unusable stored private root became a web authority');
            } catch (RuntimeException $error) { check($error->getMessage() === 'private-root-invalid', 'Unusable root failure was not explicit'); }
            $identified = new Symfony\Component\Process\Process([...$cliCommand, 'status'], $base);
            $identified->setTimeout(10); $identified->run();
            check($identified->isSuccessful() && ($status = json_decode($identified->getOutput(), true))['initialized'] === true
                && $status['installation_uuid'] === json_decode($canonicalIdentity, true)['installation_uuid'],
                'Standalone status did not identify the known existing UUID before correction');
            $beforeRepair = Paymenter\Extensions\Others\Backups\SettingsWriter::snapshot($record);
            $cli->setInput(json_encode(['settings' => ['private_root' => $root . '-different']], JSON_THROW_ON_ERROR)); $cli->run();
            check(!$cli->isSuccessful() && str_contains($cli->getErrorOutput(), 'private-root-move-refused')
                && Paymenter\Extensions\Others\Backups\SettingsWriter::snapshot($record) === $beforeRepair,
                'Explicit CLI root repair allowed a move or changed native rows on refusal');
            $cli->setInput(json_encode(['settings' => ['private_root' => $root]], JSON_THROW_ON_ERROR)); $cli->run();
            check($cli->isSuccessful() && ($result = json_decode($cli->getOutput(), true))['updated'] === true
                && $record->settings()->where('key', 'private_root')->value('value') === $root,
                'Explicit canonical CLI could not repair unusable saved root: ' . $cli->getErrorOutput());
            check(file_get_contents($root . '/installation.json') === $canonicalIdentity && $digest() === $credentialDigest
                && !file_exists($root . '-different'), 'Canonical CLI repair changed identity/credentials or created a new root');
        }
    } finally { unlink($rootAlias); }

    $configuredPython = $record->settings()->where('key', 'python')->value('value');
    try {
        $record->settings()->where('key', 'python')->update(['value' => 'python3']);
        try {
            Paymenter\Extensions\Others\Backups\Runtime::command(['status']);
            throw new RuntimeException('relative Python command accepted');
        } catch (RuntimeException $error) { check($error->getMessage() === 'python-binary-invalid', 'relative Python refusal not explicit'); }
        $broken = Livewire\Livewire::test($page);
        check(str_contains($text($broken), 'Backups can\'t start') && str_contains($text($broken), Paymenter\Extensions\Others\Backups\Messages::error('python-binary-invalid'))
            && str_contains($text($broken), 'paymenter_backups overview --offline') && !str_contains($broken->html(), 'CREDENTIAL-CANARY'), 'engine failure not explained');
    } finally {
        $record->settings()->where('key', 'python')->update(['value' => $configuredPython]);
    }

    // Create backup dialog: blocked types are named, never counted; at most one callout.
    $dialogState = function () use ($page) {
        $dialog = Livewire\Livewire::test($page);
        $dialog->call('mountAction', 'createBackup');
        $instance = $dialog->instance();
        $schema = (new ReflectionMethod($instance, 'getMountedActionSchema'))->invoke($instance);
        $state = ['submit' => (bool) $instance->getMountedAction()->getModalSubmitAction(), 'text' => '', 'callouts' => 0, 'disabled' => []];
        foreach ($schema->getFlatComponents() as $component) {
            if ($component instanceof Filament\Schemas\Components\Callout && $component->isVisible()) {
                $state['callouts']++;
                $state['text'] .= $component->getHeading() . ' ' . $component->getDescription();
            } elseif ($component instanceof Filament\Forms\Components\Radio) {
                foreach ($component->getOptions() as $value => $label) { if ($component->isOptionDisabled($value, $label)) { $state['disabled'][] = $value; } }
                $state['selected'] = $component->getState();
            }
        }
        return $state;
    };
    $ddl = $record->settings()->where('key', 'no_concurrent_ddl_acknowledged')->value('value');
    try {
        $record->settings()->where('key', 'no_concurrent_ddl_acknowledged')->update(['value' => '0']);
        $blocked = $dialogState();
        check(!$blocked['submit'] && $blocked['callouts'] === 1 && str_contains($blocked['text'], 'Confirm updates don\'t run during backups')
            && substr_count($blocked['text'], 'Confirm updates don\'t run during backups') === 1 && !preg_match('/\b\d+ items?\b/', $blocked['text']),
            'blocked dialog offers submit, counts items or repeats them: ' . $blocked['text']);
        $record->settings()->where('key', 'no_concurrent_ddl_acknowledged')->update(['value' => '1']);
        $partial = $dialogState();
        check($partial['submit'] && $partial['disabled'] === ['full-quiesced'] && $partial['selected'] === 'database-online'
            && $partial['callouts'] === 1 && str_contains($partial['text'], 'Full backups aren\'t ready') && !preg_match('/\b\d+ items?\b/', $partial['text']),
            'partially blocked dialog not guided: ' . json_encode($partial));
    } finally {
        $record->settings()->where('key', 'no_concurrent_ddl_acknowledged')->update(['value' => $ddl]);
    }

    // Requests: queued durably with actor and mode; FPM never captures.
    $beforeRequests = glob($root . '/requests/*.json');
    $queue = Livewire\Livewire::test($page);
    $queue->update(calls: [['method' => 'mountAction', 'params' => ['createBackup']]], updates: $secrets);
    $queue->update(calls: [['method' => 'callMountedAction', 'params' => []]], updates: ['mountedActions.0.data.mode' => 'database-online', ...$secrets]);
    check($secretFields($queue) === [], 'request returned secret input');
    $queue->call('mountAction', 'testStorage', [], $schemaAction($queue, 'testStorage'));
    // With nothing to delete, cleanup stays visible but disabled, so the header does not jump.
    check($queue->instance()->applyRetentionAction()->isDisabled(), 'cleanup offered with nothing to delete');
    $spooled = array_map(fn ($path) => json_decode(file_get_contents($path), true), array_values(array_diff(glob($root . '/requests/*.json'), $beforeRequests)));
    check(count($spooled) === 2, 'admin requests not durable: ' . count($spooled));
    $actions = array_count_values(array_column($spooled, 'action'));
    check(($actions['run'] ?? 0) === 1 && ($actions['destination-test'] ?? 0) === 1, 'unexpected request actions');
    foreach ($spooled as $request) {
        check($request['status'] === 'pending' && $request['arguments']['actor'] === $admin->id, 'FPM launched heavy work or lost actor');
        if ($request['action'] === 'run') { check($request['arguments']['mode'] === 'database-online', 'per-backup mode lost'); }
    }
    $queuedView = Livewire\Livewire::test($page);
    $queuedText = $text($queuedView);
    check(str_contains($queuedText, 'Queued') && str_contains($queuedText, 'Waiting for Paymenter\'s cron job'), 'queued backup or cron job notice not shown');
    // Phones stack history rows and wrap long commands instead of pushing actions or text off screen.
    check(str_contains($queue->html(), 'fi-ta-table-stacked-on-mobile') && str_contains($queue->html(), 'overflow-wrap: anywhere'), 'mobile history or command wrapping missing');
    // Without a recent cron check-in the page never promises a start time.
    foreach (['within a minute', 'under a minute'] as $promise) { check(!str_contains($queue->html(), $promise), 'unsupported backup-start timing promise: ' . $promise); }
    check(str_contains($queuedText, 'Full backup requirements') && str_contains($queuedText, 'paymenter_backups() {') && str_contains($queuedText, '/usr/bin/flock -n'), 'settings or help guidance missing');
    // Never a count of setup items.
    check(!preg_match('/need \d+ (items|changes)/', $queuedText), 'setup counts items instead of naming them');
    check(!file_exists($base . '/storage/framework/down') && !glob($root . '/runs/*/journal.json'), 'FPM started capture');
    foreach ($spooled as $request) {
        Paymenter\Extensions\Others\Backups\Runtime::command(['requests', 'cancel', $request['request_id']]);
    }
    $credentialCanaries = ['PROBE-USER-CREDENTIAL-CANARY', 'PROBE-PASSWORD-CREDENTIAL-CANARY', base64_encode('PROBE-USER-CREDENTIAL-CANARY:PROBE-PASSWORD-CREDENTIAL-CANARY'), 'SECRET-CREDENTIAL-CANARY'];
    foreach (new RecursiveIteratorIterator(new RecursiveDirectoryIterator($base . '/storage', FilesystemIterator::SKIP_DOTS)) as $file) {
        if (!$file->isFile()) { continue; }
        $bytes = file_get_contents($file->getPathname());
        foreach ($credentialCanaries as $canary) { check(!str_contains($bytes, $canary), 'credential persisted in native storage'); }
    }
    check(config('app.debug') === false, 'native credential fixture debug must be disabled');
    $limited = App\Models\Role::create(['name' => 'no-backups-permission', 'permissions' => ['admin.extensions.viewAny']]);
    $admin->role_id = $limited->id; $admin->save(); $admin->unsetRelation('role');
    check(!Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::canAccess(), 'missing permission admitted');
    $denied = new Paymenter\Extensions\Others\Backups\Admin\Pages\Backups;
    foreach (['mount', 'saveSettings'] as $action) {
        try { $denied->$action(); throw new RuntimeException('missing permission action admitted: ' . $action); }
        catch (Symfony\Component\HttpKernel\Exception\HttpException $error) { check($error->getStatusCode() === 403, 'unexpected permission status'); }
    }
    try { (new Paymenter\Extensions\Others\Backups\Http\DownloadController)('fixture-run'); throw new RuntimeException('download admitted without permission'); }
    catch (Symfony\Component\HttpKernel\Exception\HttpException $error) { check($error->getStatusCode() === 403, 'unexpected download permission status'); }
    foreach (['download', 'recovery-key'] as $routeName) {
        $route = app('router')->getRoutes()->getByName('filament.admin.pages.backups.' . $routeName);
        check($route !== null && in_array(Filament\Http\Middleware\Authenticate::class, $route->gatherMiddleware(), true), $routeName . ' route outside panel authentication');
    }
    $admin->role_id = App\Models\Role::where('name', 'integration-admin')->value('id'); $admin->save();
    echo json_encode(['native_settings' => true, 'cold_matches' => true, 'page_settings_validated_before_persist' => true, 'engine_refusal_mapped' => true,
        'livewire_secrets_clear' => true, 'one_save_with_secrets' => true, 'recovery_key_flow' => true, 'permission_denied' => true, 'fpm_actions_spooled' => true, 'gui_docroot_policy' => true,
        'relative_python_refused' => true, 'probe_credentials_bound' => true, 'create_dialog_guided' => true, 'probe_namespace_preserved' => true, 'native_lifecycle_preserved' => true,
        'canonical_cli_repairs_unusable_saved_private_root' => true,
        'download_route_authenticated' => true]) . "\n";
} elseif ($mode === 'ui-history') {
    $admin = App\Models\User::where('email', 'fixture@example.invalid')->firstOrFail();
    Illuminate\Support\Facades\Auth::login($admin);
    Filament\Facades\Filament::setCurrentPanel(Filament\Facades\Filament::getPanel('admin'));
    $page = Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::class;
    $livewire = Livewire\Livewire::test($page);
    $html = $livewire->html();
    $action = $argv[3];
    if ($action === 'render') {
        foreach (array_slice($argv, 4) as $expected) { check(str_contains($html, $expected), 'history missing: ' . $expected); }
        foreach (['archive_path', 'manifest_path', '.tar.gz"', 'owned_markers', 'IDENTIFIED'] as $forbidden) { check(!str_contains($html, $forbidden), 'history exposes ' . $forbidden); }
        echo json_encode(['history_rendered' => true]) . "\n";
    } elseif (in_array($action, ['verifyFirst', 'verify', 'delete'], true)) {
        $before = count(glob(Paymenter\Extensions\Others\Backups\Runtime::privateRoot() . '/requests/*.json'));
        // Actions without a confirmation run on mount; confirmed ones wait for submit.
        $livewire->call('mountAction', $action, [], ['table' => true, 'recordKey' => $argv[4]]);
        if ($livewire->get('mountedActions') !== []) { $livewire->call('callMountedAction'); }
        check(count(glob(Paymenter\Extensions\Others\Backups\Runtime::privateRoot() . '/requests/*.json')) === $before + 1, 'table action did not queue: ' . $action);
        echo json_encode(['queued' => $action]) . "\n";
    } elseif ($action === 'retention') {
        // Offered only when verified local backups exceed the number to keep; queues on confirmation.
        check((bool) preg_match("/mountAction\('applyRetention', \{\}, JSON\.parse\('([^']*)'\)\)/", $html, $match), 'retention not offered: ' . $argv[4]);
        $root = Paymenter\Extensions\Others\Backups\Runtime::privateRoot();
        $before = glob($root . '/requests/*.json');
        $livewire->call('mountAction', 'applyRetention', [], json_decode(json_decode('"' . $match[1] . '"'), true, 8, JSON_THROW_ON_ERROR));
        check(glob($root . '/requests/*.json') === $before, 'retention queued before confirmation');
        $livewire->call('callMountedAction');
        $added = array_values(array_diff(glob($root . '/requests/*.json'), $before));
        check(count($added) === 1 && json_decode(file_get_contents($added[0]), true)['action'] === 'retention', 'retention not queued');
        Paymenter\Extensions\Others\Backups\Runtime::command(['requests', 'cancel', json_decode(file_get_contents($added[0]), true)['request_id']]);
        echo json_encode(['retention_offered' => true]) . "\n";
    } elseif ($action === 'delete-refused') {
        $livewire->call('mountAction', 'delete', [], ['table' => true, 'recordKey' => $argv[4]]);
        $mounted = $livewire->instance()->getMountedAction();
        // Filament 5 returns only the modal partial; its key can carry a nesting suffix.
        $modal = implode("\n", $livewire->effects['partials'] ?? []) ?: $livewire->html();
        check(str_contains(html_entity_decode($modal), $argv[5]), 'delete refusal not explained in preview');
        check(!$mounted->getModalSubmitAction(), 'refused delete still offers submit');
        echo json_encode(['delete_refused' => true]) . "\n";
    } elseif ($action === 'download') {
        $response = (new Paymenter\Extensions\Others\Backups\Http\DownloadController)($argv[4]);
        if (!$response instanceof Symfony\Component\HttpFoundation\StreamedResponse) {
            check($response->getStatusCode() === 302, 'refused download did not redirect');
            echo json_encode(['download' => 'refused', 'notice' => session('filament.notifications')[0]['body'] ?? null]) . "\n";
        } else {
            $output = fopen($argv[5], 'xb');
            ob_start(static function ($buffer) use ($output) { fwrite($output, $buffer); return ''; }, 65536);
            $response->sendContent();
            ob_end_clean();
            fclose($output);
            echo json_encode(['download' => 'streamed', 'length' => (int) $response->headers->get('Content-Length'), 'disposition' => $response->headers->get('Content-Disposition'), 'cache' => $response->headers->get('Cache-Control')]) . "\n";
        }
    }
} elseif ($mode === 'open-basedir') {
    check(ini_get('open_basedir') !== '', 'restricted policy missing');
    $record = App\Models\Extension::where('extension', 'Backups')->firstOrFail();
    $configuredPython = $record->settings()->where('key', 'python')->value('value');
    $configuredPhp = $record->settings()->where('key', 'php')->value('value');
    $statBlocked = false;
    try { is_file($configuredPython); }
    catch (ErrorException $error) { $statBlocked = str_contains($error->getMessage(), 'open_basedir'); }
    check($statBlocked, 'fixture policy does not reproduce executable stat denial');
    $status = Paymenter\Extensions\Others\Backups\Runtime::command(['status']);
    check($status['initialized'] === true, 'restricted runtime status unavailable');
    $admin = App\Models\User::where('email', 'fixture@example.invalid')->firstOrFail();
    Illuminate\Support\Facades\Auth::login($admin);
    Filament\Facades\Filament::setCurrentPanel(Filament\Facades\Filament::getPanel('admin'));
    $page = Livewire\Livewire::test(Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::class);
    check(!str_contains($page->html(), 'To see details, run these as the site user') && str_contains($page->html(), 'Backup history'), 'restricted native admin status unavailable');
    try {
        // A PHP alias to the same executable remains valid under the restrictive policy.
        $record->settings()->where('key', 'php')->update(['value' => $argv[3]]);
        check(Paymenter\Extensions\Others\Backups\Runtime::command(['status'])['initialized'] === true, 'canonical PHP alias refused');
        foreach ([['php', '/usr/bin/env', 'php-binary-mismatch'], ['php', '/nonexistent/php', 'php-binary-mismatch'],
                  ['python', 'python3', 'python-binary-invalid'], ['python', '/nonexistent/python', 'python-binary-invalid'],
                  ['python', $argv[4], 'python-binary-invalid'], ['python', dirname($base), 'python-binary-invalid'],
                  ['python', '/usr/bin/true', 'python-binary-invalid']] as [$key, $value, $expected]) {
            $record->settings()->where('key', 'php')->update(['value' => $configuredPhp]);
            $record->settings()->where('key', 'python')->update(['value' => $configuredPython]);
            $record->settings()->where('key', $key)->update(['value' => $value]);
            try { Paymenter\Extensions\Others\Backups\Runtime::command(['status']); throw new RuntimeException('invalid executable accepted'); }
            catch (RuntimeException $error) { check($error->getMessage() === $expected, 'executable refusal changed: '.$error->getMessage()); }
        }
    } finally {
        $record->settings()->where('key', 'php')->update(['value' => $configuredPhp]);
        $record->settings()->where('key', 'python')->update(['value' => $configuredPython]);
    }
    echo json_encode(['open_basedir_status' => true, 'open_basedir_native_admin' => true,
        'canonical_php_alias' => true, 'invalid_executables_refused' => true]) . "\n";
} elseif ($mode === 'app-url') {
    App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $argv[3]]);
    App\Providers\SettingsProvider::flushCache();
    echo json_encode(['app_url_changed' => true]) . "\n";
} elseif ($mode === 'billing') {
    App\Models\Setting::whereNull('settingable_type')->where('key', 'cronjob_time')->update(['value' => $argv[3]]);
    App\Providers\SettingsProvider::flushCache();
    echo json_encode(['billing_changed' => true]) . "\n";
} elseif ($mode === 'restore-prepare') {
    App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $argv[3]]);
    $record = App\Models\Extension::where('extension', 'Backups')->firstOrFail();
    $record->update(['enabled' => false]);
    $record->settings()->where('key', 'private_root')->update(['value' => $argv[4]]);
    $record->settings()->where('key', 'enabled')->update(['value' => '0']);
    $record->settings()->whereIn('key', ['full_schedule', 'database_schedule'])->update(['value' => 'off']);
    App\Providers\SettingsProvider::flushCache();
    echo json_encode(['restored_host_isolated' => true]) . "\n";
} elseif ($mode === 'restore') {
    $user = App\Models\User::where('email', 'fixture@example.invalid')->firstOrFail();
    check($user->tfa_secret === 'TFA-RESTORE-CANARY', 'APP_KEY decrypt failed');
    $key = openssl_pkey_get_private(file_get_contents($base . '/storage/oauth-private.key'));
    check($key !== false, 'Passport private key failed');
    check(openssl_pkey_get_public(file_get_contents($base . '/storage/oauth-public.key')) !== false, 'Passport public key failed');
    echo json_encode(['app_key_decrypted' => true, 'passport_keys_loaded' => true]) . "\n";
}

} catch (Throwable $error) { fwrite(STDERR, get_class($error) . ": " . $error->getMessage() . "\n" . $error->getTraceAsString() . "\n"); exit(1); }
