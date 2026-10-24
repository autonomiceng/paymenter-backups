<?php
namespace Paymenter\Extensions\Others\Backups;

use RuntimeException;

/** Short lived, validated connection settings shared by scrubbed PHP children. */
final class DatabaseConnectionContext
{
    private const FIELDS = [
        'driver', 'host', 'port', 'unix_socket', 'database', 'username', 'password', 'prefix',
        'options', 'charset', 'collation', 'strict', 'engine', 'prefix_indexes',
    ];

    public static function capture(): array
    {
        $config = app('config');
        $default = $config->get('database.default');
        if (!is_string($default) || !preg_match('/^[A-Za-z0-9_-]{1,64}$/D', $default)) {
            throw new RuntimeException('database-connection-invalid');
        }
        $connection = $config->get('database.connections.' . $default);
        if (!is_array($connection)) { throw new RuntimeException('database-connection-invalid'); }
        try {
            $connection = (new \Illuminate\Database\ConfigurationUrlParser)->parseConfiguration($connection);
        } catch (\Throwable $error) {
            throw new RuntimeException('database-connection-invalid');
        }
        $captured = [];
        foreach (self::FIELDS as $field) {
            $captured[$field] = $connection[$field] ?? match ($field) {
                'host' => 'localhost', 'port' => 3306, 'unix_socket', 'database', 'username', 'password', 'prefix', 'charset', 'collation' => '',
                'options' => [], 'strict', 'prefix_indexes' => false, 'engine' => null, default => null,
            };
        }
        return self::validate(['schema_version' => 1, 'default' => $default, 'connection' => $captured]);
    }

    public static function validate(mixed $context): array
    {
        if (!is_array($context) || count($context) !== 3
            || array_diff(array_keys($context), ['schema_version', 'default', 'connection'])
            || array_diff(['schema_version', 'default', 'connection'], array_keys($context))
            || ($context['schema_version'] ?? null) !== 1
            || !is_string($context['default'] ?? null)
            || !preg_match('/^[A-Za-z0-9_-]{1,64}$/D', $context['default'])
            || !is_array($context['connection'] ?? null)) {
            throw new RuntimeException('database-connection-invalid');
        }
        $connection = $context['connection'];
        if (count($connection) !== count(self::FIELDS) || array_diff(array_keys($connection), self::FIELDS)
            || array_diff(self::FIELDS, array_keys($connection))) {
            throw new RuntimeException('database-connection-invalid');
        }
        foreach (['driver' => 64, 'host' => 1024, 'unix_socket' => 4096, 'database' => 256,
                  'username' => 1024, 'password' => 8192, 'prefix' => 128,
                  'charset' => 64, 'collation' => 128] as $field => $limit) {
            $value = $connection[$field];
            if (!is_string($value) || strlen($value) > $limit || str_contains($value, "\0")) {
                throw new RuntimeException('database-connection-invalid');
            }
        }
        if (!preg_match('/^[A-Za-z][A-Za-z0-9_-]{0,63}$/D', $connection['driver'])
            || str_contains($connection['host'], ';') || str_contains($connection['unix_socket'], ';')
            || str_contains($connection['database'], ';')
            || !(is_int($connection['port']) || (is_string($connection['port']) && preg_match('/^[0-9]{1,5}$/D', $connection['port'])))
            || (int) $connection['port'] < 1 || (int) $connection['port'] > 65535
            || !is_array($connection['options']) || count($connection['options']) > 128
            || !is_bool($connection['strict']) || !is_bool($connection['prefix_indexes'])
            || !(is_null($connection['engine']) || is_string($connection['engine']) && strlen($connection['engine']) <= 128 && !str_contains($connection['engine'], "\0"))) {
            throw new RuntimeException('database-connection-invalid');
        }
        if (!in_array($connection['driver'], ['mysql', 'mariadb'], true)) {
            throw new RuntimeException('database-driver-unsupported');
        }
        foreach ($connection['options'] as $key => $value) {
            if (!(is_int($key) || is_string($key) && strlen($key) <= 128)
                || !(is_bool($value) || is_int($value) || is_float($value) && is_finite($value)
                    || is_string($value) && strlen($value) <= 4096 && !str_contains($value, "\0"))) {
                throw new RuntimeException('database-connection-invalid');
            }
        }
        try { $encoded = json_encode($context, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES); }
        catch (\Throwable $error) { throw new RuntimeException('database-connection-invalid'); }
        if (strlen($encoded) > 32768) { throw new RuntimeException('database-connection-invalid'); }
        return $context;
    }

    /** Apply before any Eloquent query, preserving static connector settings. */
    public static function apply(mixed $context): void
    {
        $context = self::validate($context);
        $name = $context['default'];
        $config = app('config');
        $existing = $config->get('database.connections.' . $name, []);
        if (!is_array($existing)) { $existing = []; }
        unset($existing['url']);
        $config->set('database.default', $name);
        $config->set('database.connections.' . $name, array_replace($existing, $context['connection']));
        \Illuminate\Support\Facades\DB::purge($name);
    }
}
