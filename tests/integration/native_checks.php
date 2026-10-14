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
} elseif ($mode === 'seed') {
    foreach (['cronjob_time' => '23:59', 'timezone' => 'UTC', 'app_url' => $argv[3]] as $key => $value) {
        App\Models\Setting::whereNull('settingable_type')->updateOrCreate(['key' => $key], ['value' => $value, 'type' => 'string', 'encrypted' => false]);
    }
    App\Providers\SettingsProvider::flushCache();
    $role = App\Models\Role::firstOrCreate(['name' => 'integration-admin'], ['permissions' => ['*']]);
    $user = App\Models\User::withoutEvents(fn () => App\Models\User::create(['first_name' => 'Fixture', 'last_name' => 'User', 'email' => 'fixture@example.invalid', 'password' => 'fixture-password', 'role_id' => $role->id, 'tfa_secret' => 'TFA-RESTORE-CANARY']));
    echo json_encode(['id' => $user->id]) . "\n";
} elseif ($mode === 'check') {
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
    $digest = fn () => hash_file('sha256', $root . '/credentials.json');
    $secretFields = fn ($livewire) => array_filter([...array_values($livewire->get('s3Keys') ?? []), $livewire->get('siteAccess.username'), $livewire->get('siteAccess.password')], fn ($value) => $value !== null && $value !== '');

    // Field help uses a native dialog and does not submit or alter settings.
    $help = Livewire\Livewire::test($page);
    $helpRows = $nativeRows();
    $helpDigest = $digest();
    $helpContext = $schemaAction($help, 'cronLockHelp');
    $help->call('mountAction', 'cronLockHelp', [], $helpContext);
    $helpModal = $help->effects['partials']['action-modals'] ?? $help->html();
    check(count($help->get('mountedActions')) === 1 && str_contains($helpModal, 'Use the lock paths') && str_contains($helpModal, 'Close'), 'Job lock help dialog missing from native modal partial');
    check($nativeRows() === $helpRows && $digest() === $helpDigest, 'Opening help changed settings or credentials');
    $window = Livewire\Livewire::test($page);
    $window->set('settings.enabled', true);
    $windowState = $window->get('settings');
    $beforeBilling = $windowState['lock_timeout'] + $windowState['drain_seconds'] + $windowState['capture_timeout'] + 30 + $windowState['billing_margin_seconds'];
    $afterBilling = 60 + $windowState['billing_margin_seconds'];
    check(str_contains($window->html(), $beforeBilling . ' seconds before and ' . $afterBilling . ' seconds after'), 'Billing window does not reflect current time limits');

    // A malformed saved row falls back visibly and must still be repaired by Save.
    $originalKeep = $record->settings()->where('key', 'retention_keep')->value('value');
    $record->settings()->where('key', 'retention_keep')->update(['value' => 'malformed-fixture']);
    $repair = Livewire\Livewire::test($page);
    $repair->call('saveSettings');
    check((int) $record->settings()->where('key', 'retention_keep')->value('value') === Paymenter\Extensions\Others\Backups\Settings::DEFAULTS['retention_keep'], 'Saving defaults did not repair malformed native row');
    $record->settings()->where('key', 'retention_keep')->update(['value' => $originalKeep]);
    // Hidden malformed JSON still reaches authoritative config validation and repair.
    $hiddenOriginal = $record->settings()->whereIn('key', ['schedule_weekdays', 'enabled', 'schedule_frequency'])->pluck('value', 'key')->all();
    $record->settings()->where('key', 'enabled')->update(['value' => '0']);
    $record->settings()->where('key', 'schedule_frequency')->update(['value' => 'daily']);
    $record->settings()->where('key', 'schedule_weekdays')->update(['value' => 'invalid-json']);
    $process->mustRun();
    $invalidCold = json_decode($process->getOutput(), true);
    check($invalidCold['settings']['schedule_weekdays'] === 'invalid-json', 'Cold bridge silently changed malformed weekdays');
    $invalidEngine = new Symfony\Component\Process\Process([
        $invalidCold['tools']['python'], '-I', $base . '/extensions/Others/Backups/runtime/cli.py',
        '--app', $base, '--private-root', $root, '--php', PHP_BINARY, 'config', 'validate',
    ]);
    $invalidEngine->run();
    check($invalidEngine->getExitCode() !== 0 && str_contains($invalidEngine->getOutput() . $invalidEngine->getErrorOutput(), 'config-invalid'), 'Malformed weekdays did not refuse ordinary engine validation');
    $hiddenRepair = Livewire\Livewire::test($page);
    session()->forget(['filament.notifications', 'filament.claimed_notifications']);
    $hiddenRepair->call('saveSettings');
    check($hiddenRepair->instance()->getErrorBag()->isEmpty() && $record->settings()->where('key', 'schedule_weekdays')->value('value') === '[0]', 'Saving defaults did not repair hidden malformed weekdays');
    $notifications = session()->pull('filament.claimed_notifications') ?? session()->pull('filament.notifications') ?? [];
    $repairNotices = array_filter($notifications, static fn ($notice) => ($notice['title'] ?? '') === 'Settings saved with corrections' && str_contains($notice['body'] ?? '', 'Days for weekly backups'));
    check(count($repairNotices) > 0, 'Malformed weekday repair was not disclosed');
    foreach ($hiddenOriginal as $key => $value) { $record->settings()->where('key', $key)->update(['value' => $value]); }

    // Settings: engine-matching field rules refuse before any native write.
    $livewire = Livewire\Livewire::test($page);
    $before = $nativeRows();
    foreach ([['settings.retention_keep', 0, 'settings.retention_keep'], ['settings.local_byte_budget', 0, 'settings.local_byte_budget'],
              ['settings.capture_timeout', '1.5', 'settings.capture_timeout'], ['settings.job_lock_paths', "relative.lock", 'settings.job_lock_paths'],
              ['settings.s3_endpoint', 'http://fixture.invalid', null]] as [$field, $value, $errorKey]) {
        $attempt = Livewire\Livewire::test($page);
        $attempt->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: [$field => $value]);
        if ($errorKey) { check($attempt->instance()->getErrorBag()->has($errorKey), 'invalid setting accepted: ' . $field); }
        check($nativeRows() === $before, 'invalid setting reached native rows: ' . $field);
    }
    $weekly = Livewire\Livewire::test($page);
    $weekly->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings.enabled' => true, 'settings.schedule_frequency' => 'weekly', 'settings.schedule_weekdays' => []]);
    check($weekly->instance()->getErrorBag()->has('settings.schedule_weekdays') && $nativeRows() === $before, 'weekly schedule without days accepted');
    // Engine-only refusal: a full-mode schedule over Paymenter's observed billing minute.
    check(Illuminate\Support\Facades\Artisan::call('backups:guard') === 0, 'billing guard refresh failed');
    $overlap = Livewire\Livewire::test($page);
    $overlap->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings.enabled' => true, 'settings.schedule_frequency' => 'daily', 'settings.schedule_time' => '23:59', 'settings.mode' => 'full-quiesced']);
    check($overlap->instance()->getErrorBag()->has('settings.schedule_time') && $nativeRows() === $before, 'engine schedule refusal not mapped before persistence');

    // Valid save writes only changed native rows and never touches credentials.
    config(['audit.console' => true]); // This CLI process stands in for an admin web request.
    $credentialDigest = $digest();
    $changedKeys = ['enabled', 'schedule_frequency', 'schedule_weekdays', 'retention_keep', 'job_lock_paths'];
    $livewire->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings.enabled' => true, 'settings.schedule_frequency' => 'weekly',
        'settings.schedule_weekdays' => ['6', '0'], 'settings.retention_keep' => 4, 'settings.job_lock_paths' => "/fixture/a.lock\n\n /fixture/b.lock "]);
    check($livewire->instance()->getErrorBag()->isEmpty(), 'valid page settings refused: ' . json_encode($livewire->instance()->getErrorBag()->toArray()));
    $after = $nativeRows();
    check($after['enabled'] === '1' && $after['schedule_frequency'] === 'weekly' && $after['retention_keep'] === '4' && $after['schedule_weekdays'] === '[0,6]'
        && $after['job_lock_paths'] === '["/fixture/a.lock","/fixture/b.lock"]', 'page settings not persisted in engine form: ' . json_encode(array_intersect_key($after, array_flip($changedKeys))));
    check(array_diff_key($after, array_flip($changedKeys)) === array_diff_key($before, array_flip($changedKeys)), 'page save changed unrelated rows');
    check(str_contains($livewire->html(), 'Next automatic backup') && !str_contains($livewire->html(), 'Off. Turn on automatic backups'), 'enabled schedule not summarized');
    $livewire->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings.enabled' => false]);
    check($nativeRows()['enabled'] === '0' && $nativeRows()['schedule_weekdays'] === '[0,6]', 'schedule could not be turned off');
    check($after['local_byte_budget'] === $before['local_byte_budget'], 'unchanged byte limit rewritten');
    check($digest() === $credentialDigest, 'settings save touched credentials');
    check(App\Models\Audit::where('auditable_type', (new App\Models\Setting)->getMorphClass())->where('new_values', 'like', '%[0,6]%')->exists(), 'page settings bypassed native auditing');
    $livewire->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings.job_lock_paths' => '']);
    check($nativeRows()['job_lock_paths'] === '[]', 'line list could not be cleared');

    // S3 keys appear after choosing S3 and are write-only.
    $livewire->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings.destination' => 's3', 'settings.s3_bucket' => 'fixture-bucket', 'settings.plaintext_offsite_acknowledged' => true]);
    check($livewire->instance()->getErrorBag()->isEmpty() && $nativeRows()['destination'] === 's3', 'S3 settings not saved');
    check(str_contains($livewire->html(), 'S3 access keys'), 'S3 key section not revealed');
    $secrets = ['s3Keys.access_key' => 'ACCESS-CREDENTIAL-CANARY', 's3Keys.secret_key' => 'SECRET-CREDENTIAL-CANARY', 's3Keys.session_token' => 'TOKEN-CREDENTIAL-CANARY'];
    $livewire->update(calls: [['method' => 'saveS3Keys', 'params' => []]], updates: ['s3Keys.access_key' => 'FAILURE-CREDENTIAL-CANARY', 's3Keys.secret_key' => '']);
    check($livewire->instance()->getErrorBag()->has('s3Keys.secret_key'), 'missing secret key accepted');
    check($secretFields($livewire) === [] && !str_contains($livewire->html(), 'CREDENTIAL-CANARY'), 'validation error returned secret input');
    check($digest() === $credentialDigest, 'failed key save changed credentials');
    $livewire->update(calls: [['method' => 'saveS3Keys', 'params' => []]], updates: $secrets);
    check($livewire->instance()->getErrorBag()->isEmpty() && $secretFields($livewire) === [], 'S3 key save failed or kept input');
    $process->mustRun();
    $bound = json_decode($process->getOutput(), true);
    check($bound['credentials'] === ['access_key' => 'ACCESS-CREDENTIAL-CANARY', 'secret_key' => 'SECRET-CREDENTIAL-CANARY', 'session_token' => 'TOKEN-CREDENTIAL-CANARY'], 'S3 keys not stored');
    check(!str_contains(file_get_contents($root . '/credentials.json'), 'CREDENTIAL-CANARY'), 'private credentials not encrypted');
    $livewire->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: $secrets);
    check($secretFields($livewire) === [] && !str_contains($livewire->html(), 'CREDENTIAL-CANARY'), 'unrelated response returned S3 input');

    // Site password: origin binding, unreadable-store preservation, explicit removal.
    $originalUrl = App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->value('value');
    App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'https://fixture.example.invalid']);
    App\Providers\SettingsProvider::flushCache();
    try {
        $probe = Livewire\Livewire::test($page);
        check(str_contains(html_entity_decode($probe->html()), 'this server requests a test page under https://fixture.example.invalid'), 'site address not shown from app URL');
        check(str_contains($probe->html(), 'sites protected by a web server password prompt') && str_contains($probe->html(), 'separate from your Paymenter account login'), 'HTTP Basic purpose missing before credentials saved');
        // The browser cannot choose the address; a sent origin is ignored.
        $probeSecrets = ['siteAccess.origin' => 'https://other.invalid', 'siteAccess.username' => 'PROBE-USER-CREDENTIAL-CANARY', 'siteAccess.password' => 'PROBE-PASSWORD-CREDENTIAL-CANARY'];
        $probe->update(calls: [['method' => 'saveSitePassword', 'params' => []]], updates: $probeSecrets);
        check($probe->instance()->getErrorBag()->isEmpty() && $secretFields($probe) === [], 'site password save failed or kept input');
        check(str_contains($probe->html(), 'sites protected by a web server password prompt') && str_contains($probe->html(), 'Replace credentials'), 'Saved HTTP Basic credentials hide purpose or replacement action');
        $process->mustRun();
        $bound = json_decode($process->getOutput(), true);
        check($bound['maintenance_probe_credentials'] === ['origin' => 'https://fixture.example.invalid', 'username' => 'PROBE-USER-CREDENTIAL-CANARY', 'password' => 'PROBE-PASSWORD-CREDENTIAL-CANARY'], 'probe namespace not bound');
        check($bound['credentials']['access_key'] === 'ACCESS-CREDENTIAL-CANARY', 'probe save lost S3 keys');
        $credentialDigest = $digest();
        // Paymenter on plain http: no site password is sent or saved.
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'http://fixture.example.invalid']);
        App\Providers\SettingsProvider::flushCache();
        $plain = Livewire\Livewire::test($page);
        check(str_contains($plain->html(), 'uses http://'), 'http address not explained');
        check(str_contains($plain->html(), 'sites protected by a web server password prompt') && str_contains($plain->html(), 'separate from your Paymenter account login'), 'HTTP Basic purpose missing for HTTP address');
        check(str_contains($plain->html(), 'Remove credentials'), 'Saved HTTP Basic credentials cannot be removed over HTTP');
        $plain->update(calls: [['method' => 'saveSitePassword', 'params' => []]], updates: $probeSecrets);
        check($digest() === $credentialDigest && $secretFields($plain) === [], 'http address changed credentials or kept input');
        $httpRemoval = Livewire\Livewire::test($page);
        $httpContext = $schemaAction($httpRemoval, 'removeSitePassword');
        $httpRemoval->call('mountAction', 'removeSitePassword', [], $httpContext)->call('callMountedAction');
        $process->mustRun();
        $httpCleared = json_decode($process->getOutput(), true);
        check($httpCleared['maintenance_probe_credentials'] === [] && $httpCleared['credentials']['access_key'] === 'ACCESS-CREDENTIAL-CANARY', 'HTTP removal failed or lost S3 keys');
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'https://fixture.example.invalid']);
        App\Providers\SettingsProvider::flushCache();
        $probe = Livewire\Livewire::test($page);
        $probe->update(calls: [['method' => 'saveSitePassword', 'params' => []]], updates: $probeSecrets);
        $encryptedBackup = json_decode(file_get_contents($root . '/credentials.json'), true);
        Paymenter\Extensions\Others\Backups\PrivateStore::atomic($root . '/credentials.json', array_replace($encryptedBackup, ['payload' => 'unreadable-fixture-ciphertext']));
        $unreadable = $digest();
        $probe->update(calls: [['method' => 'saveSitePassword', 'params' => []]], updates: $probeSecrets);
        $probe->update(calls: [['method' => 'saveS3Keys', 'params' => []]], updates: $secrets);
        check($digest() === $unreadable && $secretFields($probe) === [], 'unreadable store overwritten or input kept');
        Paymenter\Extensions\Others\Backups\PrivateStore::atomic($root . '/credentials.json', $encryptedBackup);
        $removal = Livewire\Livewire::test($page);
        $context = $schemaAction($removal, 'removeSitePassword');
        $removal->call('mountAction', 'removeSitePassword', [], $context)->call('callMountedAction');
        $process->mustRun();
        $cleared = json_decode($process->getOutput(), true);
        check($cleared['maintenance_probe_credentials'] === [] && $cleared['credentials']['secret_key'] === 'SECRET-CREDENTIAL-CANARY', 'site password removal lost S3 keys');
        $probe->update(calls: [['method' => 'saveSitePassword', 'params' => []]], updates: $probeSecrets);
    } finally {
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $originalUrl]);
        App\Providers\SettingsProvider::flushCache();
    }
    $removal = Livewire\Livewire::test($page);
    $context = $schemaAction($removal, 'removeS3Keys');
    $removal->call('mountAction', 'removeS3Keys', [], $context)->call('callMountedAction');
    $process->mustRun();
    $removed = json_decode($process->getOutput(), true);
    check($removed['credentials'] === [] && $removed['maintenance_probe_credentials']['username'] === 'PROBE-USER-CREDENTIAL-CANARY', 'S3 key removal lost the probe namespace');
    $livewire->update(calls: [['method' => 'saveS3Keys', 'params' => []]], updates: $secrets);
    $livewire->update(calls: [['method' => 'saveSettings', 'params' => []]], updates: ['settings.destination' => 'local']);
    check($nativeRows()['destination'] === 'local', 'destination not restored');
    $credentialDigest = $digest();

    // Malformed location-guard JSON refuses page repair and points to the native editor.
    $oldDocroots = $record->settings()->where('key', 'extra_docroots')->value('value');
    $record->settings()->where('key', 'extra_docroots')->update(['value' => 'invalid-json']);
    $guardRows = $nativeRows();
    $guardPage = Livewire\Livewire::test($page);
    $guardPage->call('saveSettings');
    check($nativeRows() === $guardRows && $guardPage->instance()->getErrorBag()->has('settings.extra_docroots'), 'Page silently defaulted malformed document-root guard');
    check(str_contains($guardPage->html(), 'Extensions') && str_contains($guardPage->html(), 'edit Backups'), 'Malformed guard repair did not name native editor');
    $process->run();
    check($process->getExitCode() !== 0 && str_contains($process->getErrorOutput(), 'document-roots-invalid'), 'Malformed document-root guard did not return its safe code');
    $guardEditor = Livewire\Livewire::test(App\Admin\Resources\ExtensionResource\Pages\EditExtension::class, ['record' => $record->getRouteKey()]);
    $guardEditor->update(calls: [['method' => 'save', 'params' => []]], updates: ['data.settings.extra_docroots' => $oldDocroots]);
    check($guardEditor->instance()->getErrorBag()->isEmpty() && $record->settings()->where('key', 'extra_docroots')->value('value') === $oldDocroots, 'Native editor could not repair malformed document-root guard');
    check($digest() === $credentialDigest, 'Native location-guard repair changed credentials');

    // Native EditExtension fill/save keeps external secrets and contains no canary.
    $native = Livewire\Livewire::test(App\Admin\Resources\ExtensionResource\Pages\EditExtension::class, ['record' => $record->getRouteKey()]);
    check(!str_contains($native->html(), 'CREDENTIAL-CANARY'), 'native editor secret leak');
    check(str_contains($native->html(), 'Automatic backups') && str_contains($native->html(), 'Days for weekly backups'), 'native fallback labels missing');
    $native->call('save');
    check($native->instance()->getErrorBag()->isEmpty(), 'native edit save has errors: ' . json_encode($native->instance()->getErrorBag()->toArray()));
    check($digest() === $credentialDigest, 'native editor changed credentials');
    foreach (['data.settings.retention_keep' => '0', 'data.settings.schedule_weekdays' => '[7]', 'data.settings.extra_docroots' => '["relative"]'] as $field => $value) {
        $invalid = Livewire\Livewire::test(App\Admin\Resources\ExtensionResource\Pages\EditExtension::class, ['record' => $record->getRouteKey()]);
        $invalid->update(calls: [['method' => 'save', 'params' => []]], updates: [$field => $value]);
        check(!$invalid->instance()->getErrorBag()->isEmpty(), 'native fallback accepted engine-invalid value: ' . $field);
    }
    $additionalDocroot = dirname($base) . '/additional-site-public';
    if (!is_dir($additionalDocroot)) { mkdir($additionalDocroot, 0700); }
    $native->update(calls: [['method' => 'save', 'params' => []]], updates: ['data.settings.extra_docroots' => json_encode([$additionalDocroot])]);
    check($native->instance()->getErrorBag()->isEmpty(), 'native docroot edit invalid');
    $policy = json_decode(file_get_contents($root . '/policy.json'), true);
    check(in_array(realpath($additionalDocroot), $policy['extra_docroots'], true), 'native GUI docroot did not refresh offline policy');
    check($digest() === $credentialDigest, 'docroot edit changed credentials');

    $configuredPython = $record->settings()->where('key', 'python')->value('value');
    try {
        $record->settings()->where('key', 'python')->update(['value' => 'python3']);
        try {
            Paymenter\Extensions\Others\Backups\Runtime::command(['status']);
            throw new RuntimeException('relative Python command accepted');
        } catch (RuntimeException $error) { check($error->getMessage() === 'python-binary-invalid', 'relative Python refusal not explicit'); }
        $broken = Livewire\Livewire::test($page);
        check(str_contains($broken->html(), 'The Python path is not a working Python 3.10 or newer program')
            && str_contains($broken->html(), 'Check it under Advanced') && !str_contains($broken->html(), 'CREDENTIAL-CANARY'), 'engine failure not explained');
    } finally {
        $record->settings()->where('key', 'python')->update(['value' => $configuredPython]);
    }

    // Create backup dialog: blocked types explained once; host checks counted once across types.
    $dialogState = function () use ($page) {
        $dialog = Livewire\Livewire::test($page);
        $dialog->call('mountAction', 'createBackup');
        $instance = $dialog->instance();
        $schema = (new ReflectionMethod($instance, 'getMountedActionSchema'))->invoke($instance);
        $state = ['submit' => (bool) $instance->getMountedAction()->getModalSubmitAction(), 'text' => '', 'disabled' => []];
        foreach ($schema->getFlatComponents() as $component) {
            if ($component instanceof Filament\Schemas\Components\Callout && $component->isVisible()) {
                $description = $component->getDescription();
                $state['text'] .= $description instanceof Illuminate\Contracts\Support\Htmlable ? $description->toHtml() : (string) $description;
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
        check(!$blocked['submit']
            && str_contains($blocked['text'], 'Full backups need 2 items under Settings, Host checks.')
            && !str_contains($blocked['text'], 'that database schema changes never run'),
              'blocked dialog offers submit or counts host checks twice: ' . strip_tags($blocked['text']));
        $record->settings()->where('key', 'no_concurrent_ddl_acknowledged')->update(['value' => '1']);
        $partial = $dialogState();
        check($partial['submit'] && $partial['disabled'] === ['full-quiesced'] && $partial['selected'] === 'database-online'
            && str_contains($partial['text'], 'Full backups need 1 item under Settings, Host checks.'),
              'partially blocked dialog not guided: ' . json_encode(array_diff_key($partial, ['text' => 1])));
    } finally {
        $record->settings()->where('key', 'no_concurrent_ddl_acknowledged')->update(['value' => $ddl]);
    }

    // Requests: queued durably with actor and mode; FPM never captures.
    $beforeRequests = count(glob($root . '/requests/*.json'));
    $queue = Livewire\Livewire::test($page);
    $queue->update(calls: [['method' => 'mountAction', 'params' => ['createBackup']]], updates: $secrets);
    $queue->update(calls: [['method' => 'callMountedAction', 'params' => []]], updates: ['mountedActions.0.data.mode' => 'database-online', ...$secrets]);
    check($secretFields($queue) === [], 'request returned secret input');
    $context = $schemaAction($queue, 'testStorage');
    $queue->call('mountAction', 'testStorage', [], $context);
    // With nothing to delete, retention is not offered; ui-history covers it with real backups.
    check(!str_contains($queue->html(), "mountAction('applyRetention'"), 'retention offered with nothing to delete');
    $spooled = array_map(fn ($path) => json_decode(file_get_contents($path), true), glob($root . '/requests/*.json'));
    check(count($spooled) === $beforeRequests + 2, 'admin requests not durable: ' . count($spooled));
    $actions = array_count_values(array_column($spooled, 'action'));
    check(($actions['run'] ?? 0) === 1 && ($actions['destination-test'] ?? 0) === 1, 'unexpected request actions');
    foreach ($spooled as $request) {
        check($request['status'] === 'pending' && $request['arguments']['actor'] === $admin->id, 'FPM launched heavy work or lost actor');
        if ($request['action'] === 'run') { check($request['arguments']['mode'] === 'database-online', 'per-backup mode lost'); }
    }
    $queuedView = Livewire\Livewire::test($page);
    $queuedText = html_entity_decode($queuedView->html());
    check(str_contains($queuedText, 'Queued') && str_contains($queuedText, "Paymenter's scheduler has not run yet"), 'queued backup or scheduler notice not shown');
    // Phones stack history rows and wrap long commands instead of pushing actions or text off screen.
    check(str_contains($queue->html(), 'fi-ta-table-stacked-on-mobile') && str_contains($queue->html(), 'overflow-wrap: anywhere'), 'mobile history or command wrapping missing');
    // Copy never promises a start time the host has not shown.
    foreach (['within a minute', 'under a minute'] as $promise) { check(!str_contains($queue->html(), $promise), 'unsupported backup-start timing promise: ' . $promise); }
    check(str_contains($queue->html(), 'Host checks') && str_contains($queue->html(), 'paymenter_backups() {') && str_contains($queue->html(), '/usr/bin/flock -n'), 'settings or help guidance missing');
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
    foreach (['mount', 'saveSettings', 'saveS3Keys', 'saveSitePassword'] as $action) {
        try { $denied->$action(); throw new RuntimeException('missing permission action admitted: ' . $action); }
        catch (Symfony\Component\HttpKernel\Exception\HttpException $error) { check($error->getStatusCode() === 403, 'unexpected permission status'); }
    }
    try { (new Paymenter\Extensions\Others\Backups\Http\DownloadController)('fixture-run'); throw new RuntimeException('download admitted without permission'); }
    catch (Symfony\Component\HttpKernel\Exception\HttpException $error) { check($error->getStatusCode() === 403, 'unexpected download permission status'); }
    $route = app('router')->getRoutes()->getByName('filament.admin.pages.backups.download');
    check($route !== null && in_array(Filament\Http\Middleware\Authenticate::class, $route->gatherMiddleware(), true), 'download route outside panel authentication');
    $admin->role_id = App\Models\Role::where('name', 'integration-admin')->value('id'); $admin->save();
    echo json_encode(['native_settings' => true, 'cold_matches' => true, 'page_settings_validated_before_persist' => true, 'engine_refusal_mapped' => true,
        'livewire_secrets_clear' => true, 'permission_denied' => true, 'fpm_actions_spooled' => true, 'gui_docroot_policy' => true,
        'relative_python_refused' => true, 'probe_credentials_bound' => true, 'create_dialog_guided' => true, 'probe_namespace_preserved' => true, 'native_fallback_validated' => true,
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
        $modal = $livewire->effects['partials']['action-modals'] ?? $livewire->html();
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
    check(!str_contains($page->html(), 'The web server could not start the backup program') && str_contains($page->html(), 'Backup history'), 'restricted native admin status unavailable');
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
