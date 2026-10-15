<?php
/** Checks legacy S3 credential reads and whole-payload writes with additional namespaces. */
use Illuminate\Encryption\Encrypter;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Bootstrap\LoadConfiguration;
use Illuminate\Foundation\Bootstrap\LoadEnvironmentVariables;
use Paymenter\Extensions\Others\Backups\PrivateStore;

umask(0077);
require_once __DIR__ . '/PrivateStore.php';
require_once __DIR__ . '/Settings.php';
try {
    $outputStat = fstat(STDOUT);
    if (PHP_SAPI !== 'cli' || !$outputStat || !in_array($outputStat['mode'] & 0170000, [0010000, 0140000], true)) {
        throw new RuntimeException('private-pipe-required');
    }
    $verb = $argv[1] ?? '';
    $arguments = [];
    for ($i = 2; $i < count($argv); $i += 2) {
        $arguments[$argv[$i]] = $argv[$i + 1] ?? '';
    }
    $base = realpath($arguments['--app'] ?? '');
    if (!$base || !is_file($base . '/vendor/autoload.php')) { throw new RuntimeException('application-unavailable'); }
    require $base . '/vendor/autoload.php';
    $app = new Application($base);
    $app->bootstrapWith([LoadEnvironmentVariables::class, LoadConfiguration::class]);
    $config = $app->make('config');
    $db = (new \Illuminate\Database\ConfigurationUrlParser)->parseConfiguration($config->get('database.connections.' . $config->get('database.default')));
    $tlsOptions = [PDO::MYSQL_ATTR_SSL_CA, PDO::MYSQL_ATTR_SSL_CAPATH, PDO::MYSQL_ATTR_SSL_CERT, PDO::MYSQL_ATTR_SSL_KEY, PDO::MYSQL_ATTR_SSL_CIPHER, PDO::MYSQL_ATTR_SSL_VERIFY_SERVER_CERT];
    if (array_intersect(array_keys($db['options'] ?? []), $tlsOptions)) { throw new RuntimeException('database-tls-unsupported'); }
    if (!in_array($db['driver'] ?? '', ['mysql', 'mariadb'], true)) { throw new RuntimeException('database-driver-unsupported'); }
    $dsn = !empty($db['unix_socket']) ? 'mysql:unix_socket=' . $db['unix_socket'] : 'mysql:host=' . $db['host'] . ';port=' . ($db['port'] ?? 3306);
    $pdo = new PDO($dsn . ';dbname=' . $db['database'] . ';charset=utf8mb4', $db['username'], $db['password'], array_replace($db['options'] ?? [], [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]));
    $prefix = $db['prefix'] ?? '';
    if (!preg_match('/^[a-zA-Z0-9_]*$/D', $prefix)) { throw new RuntimeException('database-prefix-unsupported'); }
    $statement = $pdo->query("SELECT id, enabled FROM {$prefix}extensions WHERE extension='Backups' AND type='other' AND deleted_at IS NULL ORDER BY id DESC LIMIT 1");
    $extension = $statement->fetch(PDO::FETCH_ASSOC);
    if (!$extension) { throw new RuntimeException('extension-uninstalled'); }
    $query = $pdo->prepare("SELECT `key`,value,encrypted FROM {$prefix}settings WHERE settingable_id=? AND settingable_type=?");
    $query->execute([$extension['id'], (new App\Models\Extension)->getMorphClass()]);
    $settings = [];
    foreach ($query->fetchAll(PDO::FETCH_ASSOC) as $row) {
        if ($row['encrypted'] || preg_match('/access_key|secret_key|session_token|password/i', $row['key'])) { throw new RuntimeException('native-secret-row-refused'); }
        $settings[$row['key']] = $row['value'];
    }
    $settings = \Paymenter\Extensions\Others\Backups\Settings::normalize($settings);
    $root = PrivateStore::root($base, $arguments['--private-root'] ?? ($settings['private_root'] ?? null), false, $settings['extra_docroots']);
    $billingRows = $pdo->query("SELECT `key`,value FROM {$prefix}settings WHERE settingable_type IS NULL AND `key` IN ('cronjob_time','timezone','app_url')")->fetchAll(PDO::FETCH_KEY_PAIR);
    if (!isset($billingRows['cronjob_time'])) { throw new RuntimeException('billing-settings-unreadable'); }
    $billing = ['daily_time' => $billingRows['cronjob_time'], 'timezone' => $billingRows['timezone'] ?? $config->get('app.timezone', 'UTC')];
    $identityPath = $root . '/installation.json';
    $identity = ['app' => $base, 'owner_uid' => posix_geteuid(), 'installation_uuid' => null];
    if (file_exists($identityPath)) {
        $identity = PrivateStore::read($identityPath);
        if (($identity['app'] ?? null) !== $base || ($identity['owner_uid'] ?? null) !== posix_geteuid() || !is_string($identity['installation_uuid'] ?? null)) { throw new RuntimeException('installation-mismatch'); }
    }
    $decodeKey = static fn ($key) => str_starts_with($key, 'base64:') ? base64_decode(substr($key, 7), true) : $key;
    $key = $decodeKey($config->get('app.key', ''));
    $cipher = $config->get('app.cipher', 'AES-256-CBC');
    $previous = array_map($decodeKey, $config->get('app.previous_keys', []));
    $encrypter = new Encrypter($key, $cipher);
    $encrypter->previousKeys($previous);
    $fingerprint = static fn ($candidate) => hash_hmac('sha256', 'paymenter-backups-key-v1:' . $identity['installation_uuid'], $candidate);
    $credentialPath = $root . '/credentials.json';
    $credentials = [];
    if (file_exists($credentialPath)) {
        try {
            $stored = PrivateStore::read($credentialPath);
            if (($stored['schema_version'] ?? null) !== 1 || ($stored['installation_uuid'] ?? null) !== $identity['installation_uuid'] || ($stored['cipher'] ?? null) !== $cipher || !in_array($stored['key_fingerprint'] ?? '', array_map($fingerprint, [$key, ...$previous]), true)) {
                throw new RuntimeException('credentials-unreadable');
            }
            $credentials = json_decode($encrypter->decryptString($stored['payload']), true, 8, JSON_THROW_ON_ERROR);
        } catch (Throwable $error) { throw new RuntimeException('credentials-unreadable'); }
    }
    if ($verb === 'credentials-write') {
        if (!$identity['installation_uuid']) { throw new RuntimeException('installation-missing'); }
        $input = stream_get_contents(STDIN, 65537);
        if (strlen($input) > 65536) { throw new RuntimeException('credentials-invalid'); }
        $new = json_decode($input, true, 8, JSON_THROW_ON_ERROR);
        if (!is_array($new) || array_diff(array_keys($new), ['access_key', 'secret_key', 'session_token']) || empty($new['access_key']) || empty($new['secret_key'])) { throw new RuntimeException('credentials-invalid'); }
        foreach ($new as $value) { if (!is_string($value) || strlen($value) > 8192) { throw new RuntimeException('credentials-invalid'); } }
        PrivateStore::atomic($credentialPath, ['schema_version' => 1, 'installation_uuid' => $identity['installation_uuid'], 'cipher' => $cipher, 'key_fingerprint' => $fingerprint($key), 'payload' => $encrypter->encryptString(json_encode($new, JSON_THROW_ON_ERROR))]);
        echo json_encode(['ok' => true]) . "\n"; exit;
    }
    if ($verb !== 'read') { throw new RuntimeException('bridge-command-invalid'); }
    echo json_encode([
        'schema_version' => 1, 'app' => $base, 'owner_uid' => posix_geteuid(), 'private_root' => $root, 'installation_uuid' => $identity['installation_uuid'], 'docroots' => array_values(array_unique([$base . '/public', ...array_map('realpath', $settings['extra_docroots'])])),
        'database' => ['driver' => $db['driver'], 'host' => $db['host'] ?? 'localhost', 'port' => $db['port'] ?? 3306, 'socket' => $db['unix_socket'] ?? '', 'name' => $db['database'], 'user' => $db['username'], 'password' => $db['password']],
        'settings' => array_replace($settings, ['extension_enabled' => (bool) $extension['enabled']]), 'credentials' => $credentials,
        'framework' => ['maintenance_driver' => $config->get('app.maintenance.driver', 'file'), 'cache_driver' => $config->get('cache.default'), 'session_driver' => $config->get('session.driver'), 'queue_driver' => $config->get('queue.default'), 'filesystem_driver' => $config->get('filesystems.default'), 'app_url' => $billingRows['app_url'] ?? $config->get('app.url'), 'timezone' => $billingRows['timezone'] ?? $config->get('app.timezone'), 'app_key' => $config->get('app.key'), 'cipher' => $cipher],
        'billing' => $billing, 'tools' => ['php' => $settings['php'] ?: PHP_BINARY, 'python' => $settings['python'] ?? '/usr/bin/python3', 'dump' => $settings['dump'] ?? '/usr/bin/mariadb-dump', 'mysql' => $settings['mysql'] ?? '/usr/bin/mariadb', 'tar' => $settings['tar'] ?? '/usr/bin/tar', 'gzip' => $settings['gzip'] ?? '/usr/bin/gzip'],
    ], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
} catch (Throwable $error) {
    $safe = ['private-pipe-required', 'application-unavailable', 'database-driver-unsupported', 'database-tls-unsupported', 'database-prefix-unsupported', 'extension-uninstalled', 'native-secret-row-refused', 'private-root-invalid', 'document-roots-invalid', 'private-root-unavailable', 'billing-settings-unreadable', 'installation-mismatch', 'installation-missing', 'credentials-unreadable', 'credentials-invalid', 'bridge-command-invalid', 'private-file-invalid', 'private-write-failed'];
    $code = in_array($error->getMessage(), $safe, true) ? $error->getMessage() : 'bridge-unavailable';
    fwrite(STDERR, json_encode(['error' => $code]) . "\n"); exit(1);
}
