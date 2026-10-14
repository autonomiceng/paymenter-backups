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

    /** The engine requires these integers to be greater than zero; the rest may be zero. */
    public const POSITIVE = ['local_byte_budget', 'capture_timeout', 'billing_guard_max_age', 'request_max_age', 's3_timeout', 's3_retries', 'retention_keep'];

    public const PATHS = ['private_root', 'local_path', 'php', 'python', 'dump', 'mysql', 'tar', 'gzip'];

    public const SELECTS = [
        'mode' => ['full-quiesced' => 'Full backup (maintenance page while copying)', 'database-online' => 'Database only (site stays online)'],
        'destination' => ['local' => 'This server', 's3' => 'S3-compatible storage'],
        'schedule_frequency' => ['daily' => 'Daily', 'weekly' => 'Weekly'],
    ];

    /** [label, help] for the native extension editor. Help stays under 20 words. */
    public const LABELS = [
        'enabled' => ['Automatic backups', 'Create backups on this schedule. Enabling the extension alone does not schedule backups.'],
        'mode' => ['Backup type', 'Full backups show the maintenance page while copying. Database-only backups keep the site online but skip files.'],
        'destination' => ['Storage', 'Where finished archives are kept.'],
        'schedule_frequency' => ['Frequency', null],
        'schedule_time' => ['Time', '24-hour time, HH:MM.'],
        'schedule_weekdays' => ['Days for weekly backups', 'JSON list of day numbers, Monday 0 to Sunday 6. Example: [0,3]'],
        'schedule_timezone' => ['Time zone', 'For example UTC or Europe/Berlin. UTC avoids daylight saving gaps.'],
        'local_byte_budget' => ['Space limit (bytes)', 'Most the private folder may use, including failed attempts. Greater than zero.'],
        'minimum_free_bytes' => ['Minimum free disk space (bytes)', 'Backups stop before free disk space drops below this.'],
        'capture_timeout' => ['Capture time limit (seconds)', 'Longest time allowed to export the database and copy files. Greater than zero.'],
        'drain_seconds' => ['Wait before capture (seconds)', 'Time for open requests to finish after the site pauses.'],
        'lock_timeout' => ['Cron lock wait (seconds)', 'How long to wait for running Paymenter cron jobs.'],
        'billing_margin_seconds' => ['Billing safety margin (seconds)', 'Gap kept between a full backup and Paymenter\'s daily billing run.'],
        'billing_guard_max_age' => ['Scheduler report age (seconds)', 'How recent the scheduler\'s billing-time report must be. Greater than zero.'],
        'request_max_age' => ['Task expiry (seconds)', 'Queued tasks older than this are dropped. Greater than zero.'],
        'automatic_retention' => ['Delete old backups automatically', 'After each new backup, keep only the newest backups.'],
        'job_lock_paths' => ['Cron lock files', 'JSON list of absolute lock file paths that your Paymenter cron jobs use with flock.'],
        'retention_keep' => ['Backups to keep', 'Newest verified backups that retention keeps. Greater than zero.'],
        'plaintext_offsite_acknowledged' => ['S3 archives are unencrypted', 'Confirm you accept this. Archives hold customer data, .env and APP_KEY.'],
        's3_region' => ['S3 region', null],
        's3_bucket' => ['S3 bucket', null],
        's3_endpoint' => ['S3 endpoint', 'Leave empty for AWS. Other providers need an https:// address.'],
        's3_prefix' => ['S3 folder prefix', 'The installation ID is added after this prefix.'],
        's3_path_style' => ['Path-style S3 URLs', 'Turn on for providers that need the bucket name in the path.'],
        's3_timeout' => ['S3 timeout (seconds)', 'Greater than zero.'],
        's3_retries' => ['S3 attempts per request', 'Includes the first attempt. Greater than zero.'],
        'cooperative_writers_acknowledged' => ['No other writers', 'Confirm only Paymenter and its cron jobs write to its files and database.'],
        'external_state_acknowledged' => ['No outside data', 'Confirm Paymenter keeps no data in Redis, object storage or other outside stores.'],
        'no_concurrent_ddl_acknowledged' => ['No schema changes during backups', 'Confirm migrations and other schema changes never run during a backup.'],
        'local_path' => ['Backup folder', 'Absolute path. Leave empty to use the private folder.'],
        'extra_docroots' => ['Other public web folders', 'JSON list of absolute paths that any site on this account serves.'],
        'private_root' => ['Private folder', 'Absolute path. Leave empty for paymenter-backups-private next to Paymenter.'],
        'php' => ['PHP program', 'Absolute path to command-line PHP. Leave empty to detect it.'],
        'python' => ['Python program', 'Absolute path to Python 3.10 or newer.'],
        'dump' => ['Database export program', 'Absolute path, for example /usr/bin/mariadb-dump.'],
        'mysql' => ['Database client program', 'Absolute path, for example /usr/bin/mariadb.'],
        'tar' => ['tar program', 'Absolute path to GNU tar.'],
        'gzip' => ['gzip program', 'Absolute path to gzip.'],
    ];

    /** JSON list of weekday integers 0-6. */
    public const WEEKDAY_LIST = '/^\[\s*(?:[0-6](?:\s*,\s*[0-6])*)?\s*\]$/D';

    /** Folder names separated by /, no empty, "." or ".." parts and no backslash. Matches the engine. */
    public const S3_PREFIX = '/^\/*+(?!\.\.?(?:\/|$))[^\/\\\\]++(?:\/(?!\.\.?(?:\/|$))[^\/\\\\]++)*+\/*+$/D';

    /** Form text with one absolute path per non-blank line, as the engine accepts. */
    public const PATH_LINES = '/^(?:\s*+\/[^\x00\r\n]*+)*+\s*+$/D';

    /** JSON list of absolute path strings. */
    public const PATH_LIST = '/^\[\s*(?:"(?:\/|\\\\\/)(?:[^"\\\\\x00-\x1f]|\\\\.)*"(?:\s*,\s*"(?:\/|\\\\\/)(?:[^"\\\\\x00-\x1f]|\\\\.)*")*)?\s*\]$/D';

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

    /** Field rules for stored string values, matching the engine's checks. */
    public static function rules(string $key): array
    {
        $default = self::DEFAULTS[$key];
        if (isset(self::SELECTS[$key])) { return ['required', 'in:' . implode(',', array_keys(self::SELECTS[$key]))]; }
        if (is_int($default)) { return ['required', 'integer', 'min:' . (in_array($key, self::POSITIVE, true) ? 1 : 0)]; }
        if ($key === 'schedule_time') { return ['required', 'regex:/^([01][0-9]|2[0-3]):[0-5][0-9]$/']; }
        if ($key === 'schedule_timezone') { return ['required', 'timezone']; }
        if ($key === 'schedule_weekdays') { return ['required', 'json', 'regex:' . self::WEEKDAY_LIST]; }
        if (is_array($default)) { return ['required', 'json', 'regex:' . self::PATH_LIST]; }
        if (in_array($key, self::PATHS, true)) { return ['nullable', 'string', 'regex:/^\/[^\x00]*$/']; }
        if ($key === 's3_endpoint') { return ['nullable', 'string', 'regex:/^https:\/\/[^\s\/?#@]+(?:\/[^\s?#]*)?$/D']; }
        if ($key === 's3_prefix') { return ['required', 'string', 'regex:' . self::S3_PREFIX]; }
        if (is_bool($default)) { return ['boolean']; }
        return [];
    }

    public static function fields(): array
    {
        $fields = [];
        foreach (self::DEFAULTS as $key => $default) {
            $type = is_bool($default) ? 'checkbox' : (is_int($default) ? 'number' : (isset(self::SELECTS[$key]) ? 'select' : 'text'));
            [$label, $help] = self::LABELS[$key];
            $field = ['name' => $key, 'type' => $type, 'label' => $label, 'default' => is_array($default) ? json_encode($default) : $default, 'encrypted' => false];
            if ($help !== null) { $field['description'] = $help; }
            if (isset(self::SELECTS[$key])) { $field['options'] = self::SELECTS[$key]; }
            if (is_int($default)) { $field['min_value'] = in_array($key, self::POSITIVE, true) ? 1 : 0; }
            if ($rules = self::rules($key)) { $field['validation'] = $rules; }
            $fields[] = $field;
        }
        return $fields;
    }
}
