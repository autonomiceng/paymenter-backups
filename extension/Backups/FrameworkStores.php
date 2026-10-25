<?php
namespace Paymenter\Extensions\Others\Backups;

/** Effective framework configuration, reduced to nonsecret store classifications. */
final class FrameworkStores
{
    private const CONTEXT_FIELDS = [
        'maintenance' => ['driver'],
        'cache' => ['default', 'driver', 'connection', 'lock_connection', 'path'],
        'session' => ['driver', 'connection', 'files'],
        'queue' => ['default', 'driver', 'connection'],
        'filesystem' => ['default', 'driver', 'root'],
    ];

    /** Capture only store identifiers and paths. Never copy a connection definition. */
    public static function capture(object $config): array
    {
        $context = ['schema_version' => 1];
        foreach (self::CONTEXT_FIELDS as $kind => $fields) {
            $values = match ($kind) {
                'maintenance' => ['driver' => $config->get('app.maintenance.driver', 'file')],
                'session' => array_combine($fields, array_map(static fn ($field) => $config->get('session.' . $field), $fields)),
                default => match ($kind) {
                    'cache' => $config->get('cache.stores.' . $config->get('cache.default'), []),
                    'queue' => $config->get('queue.connections.' . $config->get('queue.default'), []),
                    'filesystem' => $config->get('filesystems.disks.' . $config->get('filesystems.default'), []),
                },
            };
            $context[$kind] = [];
            foreach ($fields as $field) {
                $context[$kind][$field] = $field === 'default'
                    ? $config->get(($kind === 'filesystem' ? 'filesystems' : $kind) . '.default')
                    : ($values[$field] ?? null);
            }
        }
        return self::validateContext($context);
    }

    public static function validateContext(mixed $context): array
    {
        $keys = ['schema_version', ...array_keys(self::CONTEXT_FIELDS)];
        if (!is_array($context) || count($context) !== count($keys) || array_diff($keys, array_keys($context))
            || ($context['schema_version'] ?? null) !== 1) {
            throw new \RuntimeException('framework-store-context-invalid');
        }
        foreach (self::CONTEXT_FIELDS as $kind => $fields) {
            $values = $context[$kind];
            if (!is_array($values) || count($values) !== count($fields) || array_diff($fields, array_keys($values))) {
                throw new \RuntimeException('framework-store-context-invalid');
            }
            foreach ($values as $field => $value) {
                $limit = in_array($field, ['path', 'files', 'root'], true) ? 4096 : 256;
                if ($value !== null && (!is_string($value) || strlen($value) > $limit || str_contains($value, "\0"))) {
                    throw new \RuntimeException('framework-store-context-invalid');
                }
            }
        }
        return $context;
    }

    /** Apply after cold configuration loading, before classifying stores. */
    public static function applyContext(object $config, mixed $context): void
    {
        $context = self::validateContext($context);
        $config->set('app.maintenance.driver', $context['maintenance']['driver']);
        foreach ($context['session'] as $field => $value) { $config->set('session.' . $field, $value); }
        foreach (['cache' => 'cache.stores.', 'queue' => 'queue.connections.', 'filesystem' => 'filesystems.disks.'] as $kind => $prefix) {
            $store = $context[$kind];
            $name = $store['default'];
            unset($store['default']);
            $config->set(($kind === 'filesystem' ? 'filesystems' : $kind) . '.default', $name);
            // Classification needs only these fields, not provider credentials.
            $existing = $config->get($prefix . $name, []);
            $config->set($prefix . $name, array_replace(is_array($existing) ? $existing : [], $store));
        }
    }

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
