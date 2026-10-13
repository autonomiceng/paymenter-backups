<?php
namespace Paymenter\Extensions\Others\Backups;

final class Settings
{
    public const DEFAULTS = [
        'enabled' => false, 'mode' => 'full-quiesced', 'destination' => 'local',
        'schedule_frequency' => 'daily', 'schedule_time' => '02:00', 'schedule_weekdays' => [0], 'schedule_timezone' => 'UTC',
        'local_byte_budget' => 10737418240, 'minimum_free_bytes' => 1073741824, 'capture_timeout' => 300,
        'drain_seconds' => 30, 'lock_timeout' => 15, 'billing_margin_seconds' => 120, 'billing_guard_max_age' => 180,
        'request_max_age' => 86400, 'automatic_retention' => true, 'job_lock_paths' => [], 'retention_keep' => 7, 'plaintext_offsite_acknowledged' => false,
        's3_region' => 'us-east-1', 's3_bucket' => '', 's3_endpoint' => '', 's3_prefix' => 'paymenter', 's3_path_style' => false,
        's3_timeout' => 30, 's3_retries' => 3, 'cooperative_writers_acknowledged' => false,
        'external_state_acknowledged' => false, 'no_concurrent_ddl_acknowledged' => false, 'local_path' => '',
        'extra_docroots' => [], 'private_root' => '', 'php' => '', 'python' => '/usr/bin/python3', 'dump' => '/usr/bin/mariadb-dump', 'mysql' => '/usr/bin/mariadb', 'tar' => '/usr/bin/tar', 'gzip' => '/usr/bin/gzip',
    ];

    public static function normalize(array $values): array
    {
        foreach (self::DEFAULTS as $key => $default) {
            if (!array_key_exists($key, $values)) { $values[$key] = $default; continue; }
            if (is_bool($default)) { $values[$key] = filter_var($values[$key], FILTER_VALIDATE_BOOLEAN); }
            elseif (is_int($default)) { $values[$key] = preg_match('/^[0-9]+$/D', (string) $values[$key]) ? (int) $values[$key] : $values[$key]; }
            elseif (is_array($default) && is_string($values[$key])) { $values[$key] = json_decode($values[$key], true, 16, JSON_THROW_ON_ERROR); }
        }
        return $values;
    }

    public static function fields(): array
    {
        $select = ['mode' => ['full-quiesced', 'database-online'], 'destination' => ['local', 's3'], 'schedule_frequency' => ['daily', 'weekly']];
        $fields = [];
        foreach (self::DEFAULTS as $key => $default) {
            $type = is_bool($default) ? 'checkbox' : (is_int($default) ? 'number' : (isset($select[$key]) ? 'select' : 'text'));
            $field = ['name' => $key, 'type' => $type, 'label' => ucfirst(str_replace('_', ' ', $key)), 'default' => is_array($default) ? json_encode($default) : $default, 'encrypted' => false];
            if (isset($select[$key])) { $field['options'] = $select[$key]; $field['validation'] = ['required', 'in:' . implode(',', $select[$key])]; }
            if (is_int($default)) { $field['min_value'] = 0; $field['validation'] = ['integer', 'min:0']; }
            if ($key === 'schedule_time') { $field['validation'] = ['required', 'regex:/^([01][0-9]|2[0-3]):[0-5][0-9]$/']; }
            if ($key === 'schedule_timezone') { $field['validation'] = ['required', 'timezone']; }
            if (is_array($default)) { $field['description'] = $key === 'extra_docroots' ? 'JSON list of all additional document root paths. Private state and outputs must be outside them.' : ($key === 'schedule_weekdays' ? 'JSON weekday numbers: Monday 0 through Sunday 6.' : 'JSON list of existing owner-controlled cron flock paths. Full backups require verified cron use.'); $field['validation'] = ['json']; }
            $fields[] = $field;
        }
        return $fields;
    }
}
