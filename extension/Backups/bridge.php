<?php
/** Internal cold bridge. Runtime secrets travel only through an inherited pipe. */
use Illuminate\Encryption\Encrypter;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Bootstrap\LoadConfiguration;
use Illuminate\Foundation\Bootstrap\LoadEnvironmentVariables;
use Paymenter\Extensions\Others\Backups\PrivateStore;

umask(0077);
require_once __DIR__ . '/PrivateStore.php';
require_once __DIR__ . '/Settings.php';
require_once __DIR__ . '/ProbeCredentials.php';
require_once __DIR__ . '/ArchiveEncryption.php';
require_once __DIR__ . '/CredentialInput.php';
require_once __DIR__ . '/FrameworkStores.php';
require_once __DIR__ . '/DatabaseConnectionContext.php';
try {
    $outputStat = fstat(STDOUT);
    if (PHP_SAPI !== 'cli' || !$outputStat || !in_array($outputStat['mode'] & 0170000, [0010000, 0140000], true)) {
        throw new RuntimeException('private-pipe-required');
    }
    $verb = $argv[1] ?? '';
    $arguments = [];
    $databaseContextStdin = false;
    for ($i = 2; $i < count($argv); $i++) {
        if ($argv[$i] === '--database-connection-stdin') { $databaseContextStdin = true; continue; }
        $key = $argv[$i];
        if (!str_starts_with($key, '--') || !isset($argv[$i + 1])) { throw new RuntimeException('bridge-command-invalid'); }
        $arguments[$key] = $argv[++$i];
    }
    $databaseContext = null;
    $stdinPayload = null;
    $stdinPayloadRead = false;
    if ($databaseContextStdin) {
        $bytes = stream_get_contents(STDIN, 65537);
        if (strlen($bytes) > 65536) { throw new RuntimeException('database-connection-invalid'); }
        try { $request = json_decode($bytes, true, 16, JSON_THROW_ON_ERROR); }
        catch (Throwable $error) { throw new RuntimeException('database-connection-invalid'); }
        if (!is_array($request) || count($request) !== 2 || array_diff(array_keys($request), ['database_connection', 'payload'])
            || !array_key_exists('database_connection', $request) || !array_key_exists('payload', $request)) {
            throw new RuntimeException('database-connection-invalid');
        }
        $databaseContext = DatabaseConnectionContext::validate($request['database_connection']);
        $stdinPayload = $request['payload'];
    }
    $readPayload = static function (string $errorCode = 'credentials-invalid') use (&$stdinPayloadRead, $databaseContextStdin, $stdinPayload) {
        if ($stdinPayloadRead) { throw new RuntimeException($errorCode); }
        $stdinPayloadRead = true;
        if ($databaseContextStdin) {
            if (!is_array($stdinPayload)) { throw new RuntimeException($errorCode); }
            return $stdinPayload;
        }
        $bytes = stream_get_contents(STDIN, 65537);
        if (strlen($bytes) > 65536) { throw new RuntimeException($errorCode); }
        try { $value = json_decode($bytes === '' ? '{}' : $bytes, true, 16, JSON_THROW_ON_ERROR); }
        catch (Throwable $error) { throw new RuntimeException($errorCode); }
        if (!is_array($value)) { throw new RuntimeException($errorCode); }
        return $value;
    };
    $base = realpath($arguments['--app'] ?? '');
    if (!$base || !is_file($base . '/vendor/autoload.php')) { throw new RuntimeException('application-unavailable'); }
    require $base . '/vendor/autoload.php';
    $app = new Application($base);
    $app->bootstrapWith([LoadEnvironmentVariables::class, LoadConfiguration::class]);
    $config = $app->make('config');
    if ($databaseContext !== null) {
        $dbName = $databaseContext['default'];
        $existing = $config->get('database.connections.' . $dbName, []);
        $db = array_replace(is_array($existing) ? $existing : [], $databaseContext['connection']);
    } else {
        $dbName = $config->get('database.default');
        $db = (new \Illuminate\Database\ConfigurationUrlParser)->parseConfiguration($config->get('database.connections.' . $dbName));
    }
    $tlsOptions = [PDO::MYSQL_ATTR_SSL_CA, PDO::MYSQL_ATTR_SSL_CAPATH, PDO::MYSQL_ATTR_SSL_CERT, PDO::MYSQL_ATTR_SSL_KEY, PDO::MYSQL_ATTR_SSL_CIPHER, PDO::MYSQL_ATTR_SSL_VERIFY_SERVER_CERT];
    if (array_intersect(array_keys($db['options'] ?? []), $tlsOptions)) { throw new RuntimeException('database-tls-unsupported'); }
    if (!in_array($db['driver'] ?? '', ['mysql', 'mariadb'], true)) { throw new RuntimeException('database-driver-unsupported'); }
    $databaseName = $db['database'] ?? '';
    if (!is_string($databaseName) || $databaseName === '') { throw new RuntimeException('unsupported-database'); }
    $prefix = $db['prefix'] ?? '';
    if (!is_string($prefix) || !preg_match('/^[a-zA-Z0-9_]*$/D', $prefix)) { throw new RuntimeException('database-prefix-unsupported'); }
    $host = is_string($db['host'] ?? null) && $db['host'] !== '' ? $db['host'] : 'localhost';
    $dsn = !empty($db['unix_socket']) ? 'mysql:unix_socket=' . $db['unix_socket'] : 'mysql:host=' . $host . ';port=' . ($db['port'] ?? 3306);
    $pdo = new PDO($dsn . ';dbname=' . $databaseName . ';charset=utf8mb4', $db['username'] ?? '', $db['password'] ?? '', array_replace($db['options'] ?? [], [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]));
    $statement = $pdo->query("SELECT id, enabled FROM {$prefix}extensions WHERE extension='Backups' AND type='other' AND deleted_at IS NULL ORDER BY id DESC LIMIT 1");
    $extension = $statement->fetch(PDO::FETCH_ASSOC);
    if (!$extension) { throw new RuntimeException('extension-uninstalled'); }
    $query = $pdo->prepare("SELECT `key`,value,encrypted FROM {$prefix}settings WHERE settingable_id=? AND settingable_type=?");
    $query->execute([$extension['id'], (new App\Models\Extension)->getMorphClass()]);
    $settings = [];
    foreach ($query->fetchAll(PDO::FETCH_ASSOC) as $row) {
        if ($row['encrypted'] || preg_match('/access_key|secret_key|session_token|password|maintenance_probe|archive_encryption|passphrase|recovery_key|recovery_secret/i', $row['key'])) { throw new RuntimeException('native-secret-row-refused'); }
        $settings[$row['key']] = $row['value'];
    }
    // Inspect the persisted marker before defaults can mask an existing install.
    $settings['settings_version'] ??= 1;
    $settings = \Paymenter\Extensions\Others\Backups\Settings::normalize($settings, preserveInvalidJson: true);
    $root = PrivateStore::root($base, $arguments['--private-root'] ?? ($settings['private_root'] ?? null), false, $settings['extra_docroots']);
    $identityPath = $root . '/installation.json';
    $identity = ['app' => $base, 'owner_uid' => posix_geteuid(), 'installation_uuid' => null];
    if (file_exists($identityPath)) {
        $identity = PrivateStore::read($identityPath);
        if (($identity['app'] ?? null) !== $base || ($identity['owner_uid'] ?? null) !== posix_geteuid() || !is_string($identity['installation_uuid'] ?? null)) { throw new RuntimeException('installation-mismatch'); }
    }
    if ($verb === 'document-roots') {
        // Export privacy must reflect native rows even if capture init failed.
        // Do not require billing settings, capture tools or decrypt credentials.
        echo json_encode([
            'schema_version' => 1, 'app' => $base, 'owner_uid' => posix_geteuid(),
            'private_root' => $root, 'installation_uuid' => $identity['installation_uuid'],
            'settings' => ['extra_docroots' => array_map('realpath', $settings['extra_docroots'])],
        ], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
        exit;
    }
    $billingRows = [];
    $billing = [];
    // Independent recovery keys and APP_KEY rewrapping need identity/privacy,
    // never billing admission or unrelated global settings.
    if (!str_starts_with($verb, 'archive-key-') && $verb !== 'credentials-rewrap') {
        $billingRows = $pdo->query("SELECT `key`,value FROM {$prefix}settings WHERE settingable_type IS NULL AND `key` IN ('cronjob_time','timezone','app_url')")->fetchAll(PDO::FETCH_KEY_PAIR);
        if (!isset($billingRows['cronjob_time'])) { throw new RuntimeException('billing-settings-unreadable'); }
        $billing = ['daily_time' => $billingRows['cronjob_time'], 'timezone' => $billingRows['timezone'] ?? $config->get('app.timezone', 'UTC')];
    }
    // Settings reads share the envelope but never open or decrypt the credential store.
    $readEnvelope = static function (array $credentials = []) use ($base, $root, $identity, $db, $settings, $extension, $config, $billingRows, $billing): array {
        return [
            'schema_version' => 1, 'app' => $base, 'owner_uid' => posix_geteuid(), 'private_root' => $root, 'installation_uuid' => $identity['installation_uuid'], 'docroots' => array_values(array_unique([$base . '/public', ...array_map('realpath', $settings['extra_docroots'])])),
            'database' => ['driver' => $db['driver'], 'host' => $db['host'] ?? 'localhost', 'port' => $db['port'] ?? 3306, 'socket' => $db['unix_socket'] ?? '', 'name' => $db['database'], 'user' => $db['username'], 'password' => $db['password']],
            'settings' => array_replace($settings, ['extension_enabled' => (bool) $extension['enabled']]), 'credentials' => array_intersect_key($credentials, array_flip(['access_key', 'secret_key', 'session_token'])), 'maintenance_probe_credentials' => $credentials['maintenance_probe'] ?? [], 'archive_encryption_credentials' => \Paymenter\Extensions\Others\Backups\ArchiveEncryption::wire($credentials['archive_encryption'] ?? []),
            'framework' => ['stores' => \Paymenter\Extensions\Others\Backups\FrameworkStores::describe($base, $config), 'maintenance_driver' => $config->get('app.maintenance.driver', 'file'), 'cache_driver' => $config->get('cache.default'), 'session_driver' => $config->get('session.driver'), 'queue_driver' => $config->get('queue.default'), 'filesystem_driver' => $config->get('filesystems.default'), 'app_url' => $billingRows['app_url'] ?? $config->get('app.url'), 'timezone' => $billingRows['timezone'] ?? $config->get('app.timezone'), 'app_key' => $config->get('app.key'), 'cipher' => $config->get('app.cipher', 'AES-256-CBC')],
            'billing' => $billing, 'tools' => ['php' => $settings['php'] ?: PHP_BINARY, 'python' => $settings['python'] ?? '', 'dump' => $settings['dump'] ?? '', 'mysql' => $settings['mysql'] ?? '', 'tar' => $settings['tar'] ?? '', 'gzip' => $settings['gzip'] ?? '', 'gpg' => $settings['gpg'] ?? ''],
        ];
    };
    if ($verb === 'read-settings') {
        echo json_encode($readEnvelope(), JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
        exit;
    }
    if ($verb === 'credentials-check') {
        $input = $readPayload('credentials-invalid');
        \Paymenter\Extensions\Others\Backups\CredentialInput::merge([], $input, $billingRows['app_url'] ?? $config->get('app.url', ''));
        echo json_encode(['valid' => true]) . "\n"; exit;
    }
    $decodeKey = static fn ($key) => str_starts_with($key, 'base64:') ? base64_decode(substr($key, 7), true) : $key;
    $key = $decodeKey($config->get('app.key', ''));
    $cipher = $config->get('app.cipher', 'AES-256-CBC');
    $previous = array_map($decodeKey, $config->get('app.previous_keys', []));
    $encrypter = new Encrypter($key, $cipher);
    $encrypter->previousKeys($previous);
    $fingerprint = static fn ($candidate) => hash_hmac('sha256', 'paymenter-backups-key-v1:' . $identity['installation_uuid'], $candidate);
    $archiveActions = ['generate', 'import', 'acknowledge', 'discard-pending', 'reveal', 'export', 'status'];
    $archiveAction = str_starts_with($verb, 'archive-key-') ? substr($verb, strlen('archive-key-')) : null;
    if ($archiveAction !== null && !in_array($archiveAction, $archiveActions, true)) { throw new RuntimeException('bridge-command-invalid'); }
    // Activation takes run.lock first, so a transfer never receives a new key midway.
    if ($archiveAction === 'acknowledge') {
        if (!$identity['installation_uuid']) { throw new RuntimeException('installation-missing'); }
        $archiveRunLock = \Paymenter\Extensions\Others\Backups\ArchiveEncryption::lock($root . '/run.lock', false);
    }
    // Serialize read/merge/write across all web and CLI credential namespaces.
    if ($archiveAction !== null || $verb === 'credentials-rewrap' || in_array($verb, ['credentials-write', 'maintenance-probe-credentials-write', 'credentials-merge'], true)) {
        if (!$identity['installation_uuid']) { throw new RuntimeException('installation-missing'); }
        $credentialLock = \Paymenter\Extensions\Others\Backups\ArchiveEncryption::lock($root . '/credentials.lock');
    }
    $credentialPath = $root . '/credentials.json';
    $credentials = [];
    if (file_exists($credentialPath)) {
        try {
            $stored = PrivateStore::read($credentialPath);
            if (($stored['schema_version'] ?? null) !== 1 || ($stored['installation_uuid'] ?? null) !== $identity['installation_uuid'] || ($stored['cipher'] ?? null) !== $cipher || !in_array($stored['key_fingerprint'] ?? '', array_map($fingerprint, [$key, ...$previous]), true)) {
                throw new RuntimeException('credentials-unreadable');
            }
            $credentials = json_decode($encrypter->decryptString($stored['payload']), true, 8, JSON_THROW_ON_ERROR);
            if (!is_array($credentials)) { throw new RuntimeException('credentials-unreadable'); }
            if (array_key_exists('archive_encryption', $credentials)) {
                if (!is_array($credentials['archive_encryption'])) { throw new RuntimeException('credentials-unreadable'); }
                \Paymenter\Extensions\Others\Backups\ArchiveEncryption::namespace($credentials['archive_encryption']);
            }
        } catch (Throwable $error) { throw new RuntimeException('credentials-unreadable'); }
    }
    if ($archiveAction !== null || $verb === 'credentials-rewrap') {
        $new = $readPayload('archive-key-invalid');
        if (array_diff(array_keys($new), ['actor', 'passphrase', 'fingerprint'])
            || (isset($new['actor']) && (!is_int($new['actor']) || $new['actor'] < 0))) { throw new RuntimeException('archive-key-invalid'); }
        if (isset($credentials['archive_encryption']) && !is_array($credentials['archive_encryption'])) { throw new RuntimeException('archive-key-invalid'); }
        if ($verb === 'credentials-rewrap') {
            if (!file_exists($credentialPath)) { throw new RuntimeException('credentials-unreadable'); }
            $result = ['ok' => true];
            \Paymenter\Extensions\Others\Backups\ArchiveEncryption::audit($root, 'rewrap', null, $new['actor'] ?? null);
        } else {
            if ($archiveAction === 'reveal') {
                // Match the confirmed identity under credentials.lock, before audit/disclosure.
                $namespace = \Paymenter\Extensions\Others\Backups\ArchiveEncryption::namespace($credentials['archive_encryption'] ?? null);
                $selected = $namespace['pending_fingerprint'] ?? $namespace['active_fingerprint'];
                if ($selected === null) { throw new RuntimeException('archive-key-required'); }
                if (!is_string($new['fingerprint'] ?? null) || !hash_equals($selected, $new['fingerprint'])) {
                    throw new RuntimeException('archive-key-mismatch');
                }
            }
            [$namespace, $result] = \Paymenter\Extensions\Others\Backups\ArchiveEncryption::change($credentials['archive_encryption'] ?? null, $archiveAction, $new);
            if ($archiveAction !== 'status') {
                \Paymenter\Extensions\Others\Backups\ArchiveEncryption::audit($root, $archiveAction, $result['fingerprint'] ?? null, $new['actor'] ?? null);
            }
            if (!in_array($archiveAction, ['reveal', 'export', 'status'], true)) { $credentials['archive_encryption'] = $namespace; }
            if ($archiveAction === 'export') { $result['installation_uuid'] = $identity['installation_uuid']; }
        }
        if ($verb === 'credentials-rewrap' || !in_array($archiveAction, ['reveal', 'export', 'status'], true)) {
            PrivateStore::atomic($credentialPath, ['schema_version' => 1, 'installation_uuid' => $identity['installation_uuid'], 'cipher' => $cipher, 'key_fingerprint' => $fingerprint($key), 'payload' => $encrypter->encryptString(\Paymenter\Extensions\Others\Backups\ArchiveEncryption::payload($credentials))]);
        }
        echo json_encode($result, JSON_THROW_ON_ERROR) . "\n"; exit;
    }
    if (in_array($verb, ['credentials-write', 'maintenance-probe-credentials-write', 'credentials-merge'], true)) {
        if (!$identity['installation_uuid']) { throw new RuntimeException('installation-missing'); }
        $new = $readPayload('credentials-invalid');
        if ($verb === 'credentials-merge') {
            $credentials = \Paymenter\Extensions\Others\Backups\CredentialInput::merge($credentials, $new, $billingRows['app_url'] ?? $config->get('app.url', ''));
        } elseif ($verb === 'credentials-write' && $new === ['clear' => true]) {
            // Explicit S3 removal keeps the independent probe namespace.
            $credentials = array_diff_key($credentials, array_flip(['access_key', 'secret_key', 'session_token']));
        } elseif ($verb === 'credentials-write') {
            if (array_diff(array_keys($new), ['access_key', 'secret_key', 'session_token']) || empty($new['access_key']) || empty($new['secret_key'])) { throw new RuntimeException('credentials-invalid'); }
            foreach ($new as $value) { if (!is_string($value) || strlen($value) > 8192) { throw new RuntimeException('credentials-invalid'); } }
            // Preserve the independent probe namespace and the existing S3 API.
            $credentials = array_replace(array_diff_key($credentials, array_flip(['access_key', 'secret_key', 'session_token'])), $new);
        } elseif ($new === ['clear' => true]) {
            unset($credentials['maintenance_probe']);
        } else {
            $credentials['maintenance_probe'] = \Paymenter\Extensions\Others\Backups\ProbeCredentials::validate($new, $billingRows['app_url'] ?? $config->get('app.url', ''));
        }
        PrivateStore::atomic($credentialPath, ['schema_version' => 1, 'installation_uuid' => $identity['installation_uuid'], 'cipher' => $cipher, 'key_fingerprint' => $fingerprint($key), 'payload' => $encrypter->encryptString(\Paymenter\Extensions\Others\Backups\ArchiveEncryption::payload($credentials))]);
        echo json_encode(['ok' => true]) . "\n"; exit;
    }
    if ($verb !== 'read') { throw new RuntimeException('bridge-command-invalid'); }
    echo json_encode($readEnvelope($credentials), JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
} catch (Throwable $error) {
    $safe = ['private-pipe-required', 'application-unavailable', 'database-connection-invalid', 'database-driver-unsupported', 'database-tls-unsupported', 'database-prefix-unsupported', 'unsupported-database', 'extension-uninstalled', 'native-secret-row-refused', 'private-root-invalid', 'document-roots-invalid', 'private-root-unavailable', 'billing-settings-unreadable', 'installation-mismatch', 'installation-missing', 'credentials-unreadable', 'credentials-invalid', 'bridge-command-invalid', 'private-file-invalid', 'private-write-failed', 'maintenance-probe-credentials-invalid', 'maintenance-probe-origin-invalid', 'maintenance-probe-origin-mismatch', 'archive-key-invalid', 'archive-key-mismatch', 'archive-key-required', 'archive-key-change-busy', 'archive-key-pending-exists', 'archive-key-already-exists', 'archive-key-limit', 'archive-key-audit-failed'];
    $code = in_array($error->getMessage(), $safe, true) ? $error->getMessage() : 'bridge-unavailable';
    fwrite(STDERR, json_encode(['error' => $code]) . "\n"); exit(1);
}
