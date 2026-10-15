<?php
require __DIR__ . '/../../extension/Backups/FrameworkStores.php';
use Paymenter\Extensions\Others\Backups\FrameworkStores;
final class FixtureConfig {
    public function __construct(public array $values) {}
    public function get(string $key, $default = null) { return $this->values[$key] ?? $default; }
}
$base = realpath($argv[1]);
$config = new FixtureConfig([
    'database.default' => 'mysql', 'app.maintenance.driver' => 'file',
    'cache.default' => 'custom-cache', 'cache.stores.custom-cache' => ['driver' => 'file', 'path' => $base . '/storage'],
    'session.driver' => 'file', 'session.files' => $base . '/storage',
    'queue.default' => 'custom-queue', 'queue.connections.custom-queue' => ['driver' => 'database', 'connection' => 'mysql'],
    'filesystems.default' => 'custom-disk', 'filesystems.disks.custom-disk' => ['driver' => 'local', 'root' => $base . '/storage'],
    'private-canary' => 'secret-canary',
]);
$checks = 0;
$assert = static function ($condition) use (&$checks): void { if (!$condition) { throw new RuntimeException('assertion-failed'); } $checks++; };
$stores = FrameworkStores::describe($base, $config);
$assert(count(array_filter($stores, static fn ($item) => $item['supported'] === true)) === 5);
$assert(!str_contains(json_encode($stores), 'secret-canary') && !str_contains(json_encode($stores), $base));
$config->values['queue.connections.custom-queue']['connection'] = 'other';
$assert(FrameworkStores::describe($base, $config)['queue']['supported'] === false);
$config->values['session.driver'] = 'database';
$config->values['session.connection'] = 'other';
$assert(FrameworkStores::describe($base, $config)['session']['supported'] === false);
$config->values['cache.stores.custom-cache'] = ['driver' => 'database', 'connection' => 'mysql', 'lock_connection' => 'other'];
$assert(FrameworkStores::describe($base, $config)['cache']['supported'] === false);
$config->values['filesystems.disks.custom-disk']['root'] = dirname($base);
$assert(FrameworkStores::describe($base, $config)['filesystem']['supported'] === false);
$config->values['session.driver'] = 'file';
$config->values['session.files'] = $base . '/outside-link';
$assert(FrameworkStores::describe($base, $config)['session']['supported'] === false);
$config->values['cache.stores.custom-cache'] = ['driver' => 'unknown-custom'];
$assert(FrameworkStores::describe($base, $config)['cache']['supported'] === false);
$config->values['filesystems.disks.custom-disk'] = [];
$assert(FrameworkStores::describe($base, $config)['filesystem']['supported'] === null);
$config->values['app.maintenance.driver'] = 'cache';
$assert(FrameworkStores::describe($base, $config)['maintenance']['supported'] === false);
echo json_encode(['checks' => $checks]) . "\n";
