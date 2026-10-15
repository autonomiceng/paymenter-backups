<?php
namespace Paymenter\Extensions\Others\Backups;

/** Effective cached configuration, reduced to nonsecret store classifications. */
final class FrameworkStores
{
    public static function describe(string $base, object $config): array
    {
        $inside = static function ($path) use ($base): ?bool {
            if (!is_string($path) || $path === '') { return null; }
            $canonical = realpath($path);
            if ($canonical === false) { return null; }
            return $canonical === $base || str_starts_with($canonical, $base . '/');
        };
        $sameDatabase = static fn ($connection): bool => !$connection || $connection === $config->get('database.default');
        $cache = $config->get('cache.stores.' . $config->get('cache.default'), []);
        $queue = $config->get('queue.connections.' . $config->get('queue.default'), []);
        $disk = $config->get('filesystems.disks.' . $config->get('filesystems.default'), []);
        $cacheDriver = $cache['driver'] ?? null;
        $queueDriver = $queue['driver'] ?? null;
        $sessionDriver = $config->get('session.driver');
        $diskDriver = $disk['driver'] ?? null;
        return [
            'maintenance' => ['driver' => $config->get('app.maintenance.driver', 'file'), 'supported' => $config->get('app.maintenance.driver', 'file') === 'file'],
            'cache' => ['driver' => $cacheDriver, 'supported' => match ($cacheDriver) { 'array' => true, 'database' => $sameDatabase($cache['connection'] ?? null) && $sameDatabase($cache['lock_connection'] ?? null), 'file' => $inside($cache['path'] ?? null), null => null, default => false }],
            'session' => ['driver' => $sessionDriver, 'supported' => match ($sessionDriver) { 'database' => $sameDatabase($config->get('session.connection')), 'file' => $inside($config->get('session.files')), null => null, default => false }],
            'queue' => ['driver' => $queueDriver, 'supported' => match ($queueDriver) { 'sync', 'null' => true, 'database' => $sameDatabase($queue['connection'] ?? null), null => null, default => false }],
            'filesystem' => ['driver' => $diskDriver, 'supported' => $diskDriver === 'local' ? $inside($disk['root'] ?? null) : ($diskDriver === null ? null : false)],
        ];
    }
}
