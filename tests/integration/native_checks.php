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
    // Exercise actual Livewire response serialization, including unrelated action.
    $livewire = Livewire\Livewire::test(Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::class);
    $secrets = ['accessKey' => 'ACCESS-CREDENTIAL-CANARY', 'secretKey' => 'SECRET-CREDENTIAL-CANARY', 'sessionToken' => 'TOKEN-CREDENTIAL-CANARY'];
    $livewire->update(calls: [['method' => 'saveCredentials', 'params' => []]], updates: $secrets);
    check(str_contains($livewire->get('message'), 'Credentials saved'), 'bundled credential submit failed');
    check($livewire->get('accessKey') === '' && $livewire->get('secretKey') === '' && $livewire->get('sessionToken') === '', 'write-only secrets not cleared');
    $credentialDigest = hash_file('sha256', $root . '/credentials.json');
    $originalUrl = App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->value('value');
    App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => 'https://fixture.example.invalid']);
    App\Providers\SettingsProvider::flushCache();
    $probeSecrets = ['probeOrigin' => 'https://fixture.example.invalid:443', 'probeUsername' => 'PROBE-USER-CREDENTIAL-CANARY', 'probePassword' => 'PROBE-PASSWORD-CREDENTIAL-CANARY'];
    try {
        $livewire->update(calls: [['method' => 'saveProbeCredentials', 'params' => []]], updates: $probeSecrets);
        $probeResponseStatus = (new ReflectionProperty($livewire, 'lastState'))->getValue($livewire)->getResponse()->getStatusCode();
        check($probeResponseStatus === 200, 'native probe update HTTP status: ' . $probeResponseStatus);
        check(str_contains($livewire->get('message'), 'probe credentials saved'), 'native probe submit failed: ' . $livewire->get('message'));
        foreach (array_keys($probeSecrets) as $field) { check($livewire->get($field) === '', 'probe write-only secret not cleared'); }
        $encryptedBackup = json_decode(file_get_contents($root . '/credentials.json'), true);
        $unreadable = array_replace($encryptedBackup, ['payload' => 'unreadable-fixture-ciphertext']);
        Paymenter\Extensions\Others\Backups\PrivateStore::atomic($root . '/credentials.json', $unreadable);
        $unreadableDigest = hash_file('sha256', $root . '/credentials.json');
        foreach (['saveProbeCredentials', 'clearProbeCredentials'] as $action) {
            $livewire->update(calls: [['method' => $action, 'params' => []]], updates: $probeSecrets);
            check(hash_file('sha256', $root . '/credentials.json') === $unreadableDigest, 'unreadable probe store overwritten');
            foreach (array_keys($probeSecrets) as $field) { check($livewire->get($field) === '', 'unreadable store failure returned secrets'); }
        }
        Paymenter\Extensions\Others\Backups\PrivateStore::atomic($root . '/credentials.json', $encryptedBackup);
        $credentialDigest = hash_file('sha256', $root . '/credentials.json');
        $process->mustRun();
        $bound = json_decode($process->getOutput(), true);
        check($bound['maintenance_probe_credentials'] === ['origin' => 'https://fixture.example.invalid', 'username' => $probeSecrets['probeUsername'], 'password' => $probeSecrets['probePassword']], 'probe namespace not bound/encrypted');
        check($bound['credentials']['access_key'] === $secrets['accessKey'], 'probe save lost S3 credentials');
        check(!str_contains(file_get_contents($root . '/credentials.json'), 'CREDENTIAL-CANARY'), 'private credentials not encrypted');
        foreach (['https://other.invalid', 'http://fixture.example.invalid', 'https://fixture.example.invalid#'] as $badOrigin) {
            $livewire->update(calls: [['method' => 'saveProbeCredentials', 'params' => []]], updates: array_replace($probeSecrets, ['probeOrigin' => $badOrigin]));
            check(hash_file('sha256', $root . '/credentials.json') === $credentialDigest, 'failed probe save changed credentials');
            foreach (array_keys($probeSecrets) as $field) { check($livewire->get($field) === '', 'failed probe save leaked input'); }
        }
        $livewire->update(calls: [['method' => 'refreshStatus', 'params' => []]], updates: $probeSecrets);
        foreach (array_keys($probeSecrets) as $field) { check($livewire->get($field) === '', 'unrelated response returned probe secrets'); }
        check(!str_contains($livewire->html(), 'CREDENTIAL-CANARY'), 'probe snapshot leaked input');
        $livewire->update(calls: [['method' => 'saveCredentials', 'params' => []]], updates: $secrets);
        $process->mustRun();
        check(json_decode($process->getOutput(), true)['maintenance_probe_credentials'] === $bound['maintenance_probe_credentials'], 'S3 save lost probe namespace');
        $credentialDigest = hash_file('sha256', $root . '/credentials.json');
    } finally {
        App\Models\Setting::whereNull('settingable_type')->where('key', 'app_url')->update(['value' => $originalUrl]);
        App\Providers\SettingsProvider::flushCache();
    }

    $livewire->update(calls: [['method' => 'saveCredentials', 'params' => []]], updates: ['secretKey' => 'FAILURE-CREDENTIAL-CANARY']);
    check(hash_file('sha256', $root . '/credentials.json') === $credentialDigest, 'failed save changed credentials');
    check($livewire->get('accessKey') === '' && $livewire->get('secretKey') === '' && $livewire->get('sessionToken') === '', 'write-only secrets not cleared');
    $livewire->update(calls: [['method' => 'refreshStatus', 'params' => []]], updates: $secrets);
    check($livewire->get('accessKey') === '' && $livewire->get('secretKey') === '' && $livewire->get('sessionToken') === '', 'write-only secrets not cleared');
    check(!str_contains($livewire->html(), 'CREDENTIAL-CANARY'), 'credential snapshot leak');
    // Native EditExtension fill/save keeps external secrets and contains no canary.
    $native = Livewire\Livewire::test(App\Admin\Resources\ExtensionResource\Pages\EditExtension::class, ['record' => $record->getRouteKey()]);
    check(!str_contains($native->html(), 'CREDENTIAL-CANARY'), 'native editor secret leak');
    $native->call('save');
    check($native->instance()->getErrorBag()->isEmpty(), 'native edit save has errors: ' . json_encode($native->instance()->getErrorBag()->toArray()));
    check(hash_file('sha256', $root . '/credentials.json') === $credentialDigest, 'native editor changed credentials');
    $process->mustRun();
    check(json_decode($process->getOutput(), true)['credentials'] === ['access_key' => 'ACCESS-CREDENTIAL-CANARY', 'secret_key' => 'SECRET-CREDENTIAL-CANARY', 'session_token' => 'TOKEN-CREDENTIAL-CANARY'], 'native editor lost decrypted credentials');
    $additionalDocroot = dirname($base) . '/additional-site-public';
    if (!is_dir($additionalDocroot)) { mkdir($additionalDocroot, 0700); }
    $native->update(calls: [['method' => 'save', 'params' => []]], updates: ['data.settings.extra_docroots' => json_encode([$additionalDocroot])]);
    check($native->instance()->getErrorBag()->isEmpty(), 'native docroot edit invalid');
    $policy = json_decode(file_get_contents($root . '/policy.json'), true);
    check(in_array(realpath($additionalDocroot), $policy['extra_docroots'], true), 'native GUI docroot did not refresh offline policy');
    check(hash_file('sha256', $root . '/credentials.json') === $credentialDigest, 'docroot edit changed credentials');
    $process->mustRun();
    check(json_decode($process->getOutput(), true)['maintenance_probe_credentials'] === $bound['maintenance_probe_credentials'], 'native editor lost probe namespace');
    $livewire->update(calls: [['method' => 'clearProbeCredentials', 'params' => []]], updates: $probeSecrets);
    foreach (array_keys($probeSecrets) as $field) { check($livewire->get($field) === '', 'clear probe credentials leaked inputs'); }
    $process->mustRun();
    $cleared = json_decode($process->getOutput(), true);
    check($cleared['maintenance_probe_credentials'] === [] && $cleared['credentials']['secret_key'] === $secrets['secretKey'], 'clear lost S3 namespace');
    $configuredPython = $record->settings()->where('key', 'python')->value('value');
    try {
        $record->settings()->where('key', 'python')->update(['value' => 'python3']);
        try {
            Paymenter\Extensions\Others\Backups\Runtime::command(['status']);
            throw new RuntimeException('relative Python command accepted');
        } catch (RuntimeException $error) { check($error->getMessage() === 'python-binary-invalid', 'relative Python refusal not explicit'); }
    } finally {
        $record->settings()->where('key', 'python')->update(['value' => $configuredPython]);
    }
    $beforeRequests = count(glob($root . '/requests/*.json'));
    foreach (['run', 'verify', 'deliver', 'retention', 'destination-test'] as $action) {
        $livewire->update(calls: [['method' => 'requestAction', 'params' => [$action]]], updates: ['runId' => 'fixture-run-id', ...$secrets]);
        check(str_contains($livewire->get('message'), 'Request saved'), 'admin request failed: ' . $action);
        check($livewire->get('secretKey') === '', 'request returned secret input');
    }
    $spooled = array_map(fn ($path) => json_decode(file_get_contents($path), true), glob($root . '/requests/*.json'));
    check(count($spooled) === $beforeRequests + 5, 'admin requests not durable');
    foreach ($spooled as $request) { check($request['status'] === 'pending', 'FPM launched heavy work'); }
    check(!file_exists($base . '/storage/framework/down') && !glob($root . '/runs/*/journal.json'), 'FPM started capture');
    $credentialCanaries = ['PROBE-USER-CREDENTIAL-CANARY', 'PROBE-PASSWORD-CREDENTIAL-CANARY', base64_encode('PROBE-USER-CREDENTIAL-CANARY:PROBE-PASSWORD-CREDENTIAL-CANARY')];
    foreach (new RecursiveIteratorIterator(new RecursiveDirectoryIterator($base . '/storage', FilesystemIterator::SKIP_DOTS)) as $file) {
        if (!$file->isFile()) { continue; }
        $bytes = file_get_contents($file->getPathname());
        foreach ($credentialCanaries as $canary) { check(!str_contains($bytes, $canary), 'probe credential persisted in native storage'); }
    }
    check(config('app.debug') === false, 'native credential fixture debug must be disabled');
    $limited = App\Models\Role::create(['name' => 'no-backups-permission', 'permissions' => ['admin.extensions.viewAny']]);
    $admin->role_id = $limited->id; $admin->save(); $admin->unsetRelation('role');
    check(!Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::canAccess(), 'missing permission admitted');
    $page = new Paymenter\Extensions\Others\Backups\Admin\Pages\Backups;
    foreach (['refreshStatus', 'saveCredentials', 'saveProbeCredentials', 'clearProbeCredentials'] as $action) {
        try { $page->$action(); throw new RuntimeException('missing permission action admitted'); }
        catch (Symfony\Component\HttpKernel\Exception\HttpException $error) { check($error->getStatusCode() === 403, 'unexpected permission status'); }
    }
    try { $page->requestAction('run'); throw new RuntimeException('missing permission request admitted'); }
    catch (Symfony\Component\HttpKernel\Exception\HttpException $error) { check($error->getStatusCode() === 403, 'unexpected request permission status'); }
    echo json_encode(['native_settings' => true, 'cold_matches' => true, 'livewire_secrets_clear' => true, 'permission_denied' => true, 'fpm_actions_spooled' => true, 'gui_docroot_policy' => true, 'relative_python_refused' => true, 'probe_credentials_bound' => true, 'probe_namespace_preserved' => true, 'probe_livewire_secrets_clear' => true]) . "\n";
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
    check($page->get('backupStatus')['initialized'] === true && !str_contains($page->get('message'), 'unavailable'), 'restricted native admin status unavailable');
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
