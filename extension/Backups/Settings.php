<?php
namespace Paymenter\Extensions\Others\Backups;

final class Settings
{
    /** Bytes in one GiB, the unit the page uses for space settings. */
    public const GIB = 1073741824;

    /**
     * Native setting keys and new-install defaults. settings_version 2 keys
     * come first, then shared keys, then 0.3 keys that stay readable for
     * installations that have not saved settings in this version yet.
     */
    public const DEFAULTS = [
        'settings_version' => 2,
        // Schedules
        'full_schedule' => 'off', 'full_time' => '03:00', 'full_weekdays' => [6],
        'database_schedule' => 'off', 'database_every_hours' => 1, 'database_minute' => 0, 'database_time' => '02:00', 'database_weekdays' => [0, 1, 2, 3, 4, 5, 6],
        'schedule_timezone' => 'UTC',
        // Storage and retention
        'keep_local' => true, 'upload_s3' => false,
        'retention_keep_full' => 7, 'retention_keep_database' => 24,
        's3_retention_policy_full' => 'local-count', 's3_retention_keep_full' => 30, 's3_retention_days_full' => 90,
        's3_retention_policy_database' => 'local-count', 's3_retention_keep_database' => 168, 's3_retention_days_database' => 90,
        'automatic_retention' => true,
        'local_path' => '', 'local_byte_budget' => 10737418240, 'minimum_free_bytes' => 1073741824,
        's3_region' => 'us-east-1', 's3_bucket' => '', 's3_endpoint' => '', 's3_prefix' => 'paymenter', 's3_path_style' => false,
        's3_encryption' => true, 'plaintext_offsite_acknowledged' => false,
        // Confirmations
        'full_host_assumptions_acknowledged' => false, 'no_concurrent_ddl_acknowledged' => false,
        // Advanced
        'capture_timeout' => 300, 'drain_seconds' => 30, 'lock_timeout' => 15, 'billing_margin_seconds' => 120, 'billing_guard_max_age' => 180,
        'request_max_age' => 86400, 's3_timeout' => 30, 's3_retries' => 3,
        'job_lock_paths' => [], 'extra_docroots' => [], 'private_root' => '',
        'python' => '', 'php' => '', 'dump' => '', 'mysql' => '', 'tar' => '', 'gzip' => '', 'gpg' => '',
        // Legacy (settings_version 1)
        'enabled' => false, 'mode' => 'full-quiesced', 'destination' => 'local',
        'schedule_frequency' => 'daily', 'schedule_time' => '02:00', 'schedule_weekdays' => [0], 'retention_keep' => 7,
        's3_retention_policy' => 'local-count', 's3_retention_keep' => 30, 's3_retention_days' => 90,
        'cooperative_writers_acknowledged' => false, 'external_state_acknowledged' => false,
    ];

    /** Keys only the legacy interpretation reads. */
    public const LEGACY = [
        'enabled', 'mode', 'destination', 'schedule_frequency', 'schedule_time', 'schedule_weekdays', 'retention_keep',
        's3_retention_policy', 's3_retention_keep', 's3_retention_days', 'cooperative_writers_acknowledged', 'external_state_acknowledged',
    ];

    /** The engine requires these integers to be greater than zero; the rest may be zero. */
    public const POSITIVE = [
        'local_byte_budget', 'capture_timeout', 'billing_guard_max_age', 'request_max_age', 's3_timeout', 's3_retries',
        'retention_keep_full', 'retention_keep_database', 's3_retention_keep_full', 's3_retention_days_full',
        's3_retention_keep_database', 's3_retention_days_database', 'database_every_hours',
        'retention_keep', 's3_retention_keep', 's3_retention_days',
    ];

    public const PATHS = ['private_root', 'local_path', 'php', 'python', 'dump', 'mysql', 'tar', 'gzip', 'gpg'];

    public const WEEKDAY_KEYS = ['full_weekdays', 'database_weekdays', 'schedule_weekdays'];

    public const TIME_KEYS = ['full_time', 'database_time', 'schedule_time'];

    public const EVERY_HOURS = [1, 2, 3, 4, 6, 8, 12];

    private const S3_POLICIES = ['local-count' => 'Same number as on this server', 'count' => 'Last N backups', 'age' => 'For N days', 'keep-all' => 'All'];

    public const SELECTS = [
        'full_schedule' => ['off' => 'Off', 'daily' => 'Daily', 'weekly' => 'Weekly'],
        'database_schedule' => ['off' => 'Off', 'hourly' => 'Hourly', 'daily' => 'Daily', 'weekly' => 'Weekly'],
        'database_every_hours' => [1 => 'Every hour', 2 => 'Every 2 hours', 3 => 'Every 3 hours', 4 => 'Every 4 hours', 6 => 'Every 6 hours', 8 => 'Every 8 hours', 12 => 'Every 12 hours'],
        's3_retention_policy_full' => self::S3_POLICIES,
        's3_retention_policy_database' => self::S3_POLICIES,
        'mode' => ['full-quiesced' => 'Full backup', 'database-online' => 'Database backup'],
        'destination' => ['local' => 'This server', 's3' => 'This server and S3'],
        'schedule_frequency' => ['daily' => 'Daily', 'weekly' => 'Weekly'],
        's3_retention_policy' => self::S3_POLICIES,
    ];

    /** [label, help] for the native extension editor, a fallback for the Backups page. Help stays short. */
    public const LABELS = [
        'settings_version' => ['Settings version', 'Set by the Backups page. 1 keeps 0.3 behaviour until settings are saved there once.'],
        'full_schedule' => ['Full backups', 'off, daily or weekly.'],
        'full_time' => ['Full backup time', '24-hour time, HH:MM.'],
        'full_weekdays' => ['Full backup days', 'JSON list of day numbers, Monday 0 to Sunday 6. Example: [6]'],
        'database_schedule' => ['Database backups', 'off, hourly, daily or weekly.'],
        'database_every_hours' => ['Database backups every (hours)', 'For hourly: 1, 2, 3, 4, 6, 8 or 12.'],
        'database_minute' => ['Database backup minute', 'For hourly: minute past the hour, 0 to 59.'],
        'database_time' => ['Database backup time', 'For daily or weekly: 24-hour time, HH:MM.'],
        'database_weekdays' => ['Database backup days', 'JSON list of day numbers, Monday 0 to Sunday 6.'],
        'schedule_timezone' => ['Time zone', 'For example UTC or Europe/Berlin. UTC avoids daylight-saving gaps.'],
        'keep_local' => ['Keep backups on this server', 'Keep backups on this server, or at least one of this and Upload to S3.'],
        'upload_s3' => ['Upload to S3', 'Upload each backup to the S3 bucket below.'],
        'retention_keep_full' => ['Full backups to keep on this server', 'The newest verified backup is always kept.'],
        'retention_keep_database' => ['Database backups to keep on this server', 'The newest verified backup is always kept.'],
        's3_retention_policy_full' => ['Keep full backups in S3', 'local-count, count, age or keep-all.'],
        's3_retention_keep_full' => ['Full backups to keep in S3', 'Used by the count policy.'],
        's3_retention_days_full' => ['Days to keep full backups in S3', 'Used by the age policy. The newest backup in S3 is always kept.'],
        's3_retention_policy_database' => ['Keep database backups in S3', 'local-count, count, age or keep-all.'],
        's3_retention_keep_database' => ['Database backups to keep in S3', 'Used by the count policy.'],
        's3_retention_days_database' => ['Days to keep database backups in S3', 'Used by the age policy. The newest backup in S3 is always kept.'],
        'automatic_retention' => ['Delete old backups automatically', 'After each backup, delete backups beyond the numbers to keep.'],
        'local_path' => ['Backup directory', 'Full path. Leave empty for the default, DATA_DIR/backups.'],
        'local_byte_budget' => ['Backup space limit (bytes)', 'New backups stop when backups on this server reach this size.'],
        'minimum_free_bytes' => ['Minimum free disk space (bytes)', 'Backups stop if the disk would drop below this.'],
        's3_region' => ['S3 region', 'For example us-east-1.'],
        's3_bucket' => ['S3 bucket', 'Bucket name. It must already exist.'],
        's3_endpoint' => ['S3 endpoint', 'Leave empty for AWS. Other providers need their https:// API URL.'],
        's3_prefix' => ['S3 folder in bucket', 'Backups go to FOLDER/INSTALLATION-ID/.'],
        's3_path_style' => ['S3 path-style addressing', 'Turn on for MinIO and some other S3-compatible providers.'],
        's3_encryption' => ['Encrypt backups in S3', 'Uploads need a saved recovery key. Set it up on the Backups page.'],
        'plaintext_offsite_acknowledged' => ['Unencrypted S3 backups confirmed', 'Needed only with encryption off. Backups hold customer data, .env and APP_KEY.'],
        'full_host_assumptions_acknowledged' => ['No other writers', 'Confirm no other program writes to Paymenter\'s database or files.'],
        'no_concurrent_ddl_acknowledged' => ['No updates during backups', 'Confirm Paymenter isn\'t updated and no migrations run during a backup.'],
        'capture_timeout' => ['Backup time limit (seconds)', 'Longest the database export and file copy may take. Greater than zero.'],
        'drain_seconds' => ['Wait for open requests (seconds)', 'After maintenance mode starts, wait this long for page loads to finish.'],
        'lock_timeout' => ['Wait for running cron jobs (seconds)', 'If a Paymenter cron job is running, wait this long before trying next minute.'],
        'billing_margin_seconds' => ['Billing safety margin (seconds)', 'Extra time kept clear before and after Paymenter\'s daily billing run.'],
        'billing_guard_max_age' => ['Billing time check max age (seconds)', 'Full backups need the cron job to have recorded the billing time within this.'],
        'request_max_age' => ['Queued task expiry (seconds)', 'Tasks that haven\'t started after this long are dropped. Greater than zero.'],
        's3_timeout' => ['S3 timeout (seconds)', 'Per request. Greater than zero.'],
        's3_retries' => ['S3 retries', 'Total attempts per request, including the first.'],
        'job_lock_paths' => ['Lock files override', 'JSON list. Leave [] to use the lock files your cron jobs use.'],
        'extra_docroots' => ['Other public web folders', 'JSON list of full paths that other sites on this account serve.'],
        'private_root' => ['Data directory', 'Full path. Leave empty for paymenter-backups-private next to Paymenter.'],
        'python' => ['Python 3 binary', 'Full path. Leave empty to detect.'],
        'php' => ['PHP binary', 'Full path. Leave empty to detect.'],
        'dump' => ['mysqldump or mariadb-dump binary', 'Full path. Leave empty to detect.'],
        'mysql' => ['mysql or mariadb client binary', 'Full path. Leave empty to detect.'],
        'tar' => ['tar binary', 'Full path to GNU tar. Leave empty to detect.'],
        'gzip' => ['gzip binary', 'Full path. Leave empty to detect.'],
        'gpg' => ['GnuPG (gpg) binary', 'Full path. Leave empty to detect.'],
        'enabled' => ['Legacy: automatic backups', 'Read until settings are saved on the Backups page.'],
        'mode' => ['Legacy: backup type', 'Read until settings are saved on the Backups page. Also the CLI default.'],
        'destination' => ['Legacy: storage', 'Read until settings are saved on the Backups page.'],
        'schedule_frequency' => ['Legacy: frequency', null],
        'schedule_time' => ['Legacy: time', '24-hour time, HH:MM.'],
        'schedule_weekdays' => ['Legacy: days for weekly backups', 'JSON list of day numbers, Monday 0 to Sunday 6. Example: [0,3]'],
        'retention_keep' => ['Legacy: backups to keep', 'Read until settings are saved on the Backups page.'],
        's3_retention_policy' => ['Legacy: keep in S3', null],
        's3_retention_keep' => ['Legacy: backups to keep in S3', null],
        's3_retention_days' => ['Legacy: days to keep in S3', null],
        'cooperative_writers_acknowledged' => ['Legacy: no other writers', null],
        'external_state_acknowledged' => ['Legacy: no outside data', null],
    ];

    /** JSON list of weekday integers 0-6. */
    public const WEEKDAY_LIST = '/^\[\s*(?:[0-6](?:\s*,\s*[0-6])*)?\s*\]$/D';

    /** 24-hour HH:MM. */
    public const TIME = '/^([01][0-9]|2[0-3]):[0-5][0-9]$/D';

    /** Object prefix parts separated by /, no empty, "." or ".." parts and no backslash. Matches the engine. */
    public const S3_PREFIX = '/^\/*+(?!\.\.?(?:\/|$))[^\/\\\\]++(?:\/(?!\.\.?(?:\/|$))[^\/\\\\]++)*+\/*+$/D';

    /** Form text with one absolute path per non-blank line, as the engine accepts. */
    public const PATH_LINES = '/^(?:\s*+\/[^\x00\r\n]*+)*+\s*+$/D';

    /** JSON list of absolute path strings. */
    public const PATH_LIST = '/^\[\s*(?:"(?:\/|\\\\\/)(?:[^"\\\\\x00-\x1f]|\\\\.)*"(?:\s*,\s*"(?:\/|\\\\\/)(?:[^"\\\\\x00-\x1f]|\\\\.)*")*)?\s*\]$/D';

    /** Keys a settings_version 2 save writes. */
    public static function v2Keys(): array
    {
        return array_values(array_filter(array_keys(self::DEFAULTS), static fn ($key) => $key !== 'settings_version' && !in_array($key, self::LEGACY, true)));
    }

    public static function normalize(array $values, bool $preserveInvalidJson = false): array
    {
        foreach (self::DEFAULTS as $key => $default) {
            if (!array_key_exists($key, $values)) { $values[$key] = $default; continue; }
            if (is_bool($default)) { $values[$key] = filter_var($values[$key], FILTER_VALIDATE_BOOLEAN); }
            elseif (is_int($default)) { $values[$key] = preg_match('/^[0-9]+$/D', (string) $values[$key]) ? (int) $values[$key] : $values[$key]; }
            elseif (is_array($default) && is_string($values[$key])) {
                $raw = $values[$key];
                try { $values[$key] = json_decode($values[$key], true, 16, JSON_THROW_ON_ERROR); }
                catch (\JsonException $error) {
                    if ($key === 'extra_docroots') { throw new \RuntimeException('document-roots-invalid'); }
                    // The cold read must carry these invalid rows to Python so
                    // config check can merge a validated repair before parsing.
                    // Document roots stay strict because PHP guards the private root.
                    if (!$preserveInvalidJson || !in_array($key, [...self::WEEKDAY_KEYS, 'job_lock_paths'], true)) { throw $error; }
                }
                if ($key === 'extra_docroots' && (!is_array($values[$key]) || !array_is_list($values[$key]) || !str_starts_with(ltrim($raw), '['))) {
                    throw new \RuntimeException('document-roots-invalid');
                }
            }
            if ($key === 'extra_docroots' && (!is_array($values[$key]) || !array_is_list($values[$key]))) {
                throw new \RuntimeException('document-roots-invalid');
            }
        }
        return $values;
    }

    /** Field rules for stored string values, matching the engine's checks. */
    public static function rules(string $key): array
    {
        $default = self::DEFAULTS[$key];
        if ($key === 'settings_version') { return ['required', 'integer', 'in:1,2']; }
        if (isset(self::SELECTS[$key])) { return ['required', 'in:' . implode(',', array_keys(self::SELECTS[$key]))]; }
        if ($key === 'database_minute') { return ['required', 'integer', 'min:0', 'max:59']; }
        if (is_int($default)) { return ['required', 'integer', 'min:' . (in_array($key, self::POSITIVE, true) ? 1 : 0)]; }
        if (in_array($key, self::TIME_KEYS, true)) { return ['required', 'regex:' . self::TIME]; }
        if ($key === 'schedule_timezone') { return ['required', 'timezone']; }
        if (in_array($key, self::WEEKDAY_KEYS, true)) { return ['required', 'json', 'regex:' . self::WEEKDAY_LIST]; }
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
            $type = is_bool($default) ? 'checkbox' : (isset(self::SELECTS[$key]) ? 'select' : (is_int($default) ? 'number' : 'text'));
            [$label, $help] = self::LABELS[$key];
            $field = ['name' => $key, 'type' => $type, 'label' => $label, 'default' => is_array($default) ? json_encode($default) : $default, 'encrypted' => false];
            if ($help !== null) { $field['description'] = $help; }
            if (isset(self::SELECTS[$key])) { $field['options'] = self::SELECTS[$key]; }
            elseif (is_int($default)) { $field['min_value'] = in_array($key, self::POSITIVE, true) ? 1 : 0; }
            if ($rules = self::rules($key)) { $field['validation'] = $rules; }
            $fields[] = $field;
        }
        return $fields;
    }

    /**
     * One stored row as its typed value, or null when the row is absent or
     * invalid. Never throws; callers decide whether an invalid row matters.
     */
    public static function parse(string $key, mixed $raw): mixed
    {
        if ($raw === null) { return null; }
        $default = self::DEFAULTS[$key];
        if (is_bool($default)) {
            if (is_bool($raw)) { return $raw; }
            return filter_var($raw, FILTER_VALIDATE_BOOLEAN, FILTER_NULL_ON_FAILURE);
        }
        if (is_int($default)) {
            if (!is_int($raw) && !preg_match('/^[0-9]+$/D', (string) $raw)) { return null; }
            $value = (int) $raw;
            if (in_array($key, self::POSITIVE, true) && $value < 1) { return null; }
            if ($key === 'database_every_hours' && !in_array($value, self::EVERY_HOURS, true)) { return null; }
            if ($key === 'database_minute' && $value > 59) { return null; }
            if ($key === 'settings_version' && !in_array($value, [1, 2], true)) { return null; }
            return $value;
        }
        if (is_array($default)) {
            if (is_string($raw)) {
                try { $raw = json_decode($raw, true, 16, JSON_THROW_ON_ERROR); } catch (\JsonException $error) { return null; }
            }
            if (!is_array($raw) || !array_is_list($raw)) { return null; }
            if (in_array($key, self::WEEKDAY_KEYS, true)) {
                foreach ($raw as $day) { if (!is_int($day) || $day < 0 || $day > 6) { return null; } }
                $raw = array_values(array_unique($raw));
                sort($raw);
                return $raw;
            }
            foreach ($raw as $path) { if (!is_string($path) || !str_starts_with($path, '/')) { return null; } }
            return $raw;
        }
        $value = (string) $raw;
        if (isset(self::SELECTS[$key]) && !array_key_exists($value, self::SELECTS[$key])) { return null; }
        if (in_array($key, self::TIME_KEYS, true) && !preg_match(self::TIME, $value)) { return null; }
        return $value;
    }

    /**
     * Settings version 2 values for native rows saved by 0.3 (settings_version
     * absent or 1). The Python engine applies the same mapping; shared test
     * vectors in tests/fixtures/settings-migration.json keep them identical.
     * Invalid or absent legacy rows use their 0.3 defaults.
     *
     * @param array<string, mixed> $rows native rows, key => stored string
     * @return array<string, mixed> every settings_version 2 key, typed
     */
    public static function fromLegacy(array $rows): array
    {
        $legacy = static function (string $key) use ($rows): mixed {
            $value = array_key_exists($key, $rows) ? self::parse($key, $rows[$key]) : null;
            return $value ?? self::DEFAULTS[$key];
        };
        $values = [];
        foreach (self::v2Keys() as $key) {
            $value = array_key_exists($key, $rows) ? self::parse($key, $rows[$key]) : null;
            $values[$key] = $value ?? self::DEFAULTS[$key];
        }
        // Tool paths and lock files carry over unchanged, as pins.
        foreach (['python', 'php', 'dump', 'mysql', 'tar', 'gzip', 'job_lock_paths'] as $key) { $values[$key] = $legacy($key); }

        $kind = $legacy('mode') === 'database-online' ? 'database' : 'full';
        $values['full_schedule'] = 'off';
        $values['database_schedule'] = 'off';
        if ($legacy('enabled')) { $values[$kind . '_schedule'] = $legacy('schedule_frequency'); }
        $values[$kind . '_time'] = $legacy('schedule_time');
        $values[$kind . '_weekdays'] = $legacy('schedule_weekdays');

        $s3 = $legacy('destination') === 's3';
        $values['keep_local'] = true;
        $values['upload_s3'] = $s3;

        foreach (['full', 'database'] as $type) {
            $values['retention_keep_' . $type] = $legacy('retention_keep');
            $values['s3_retention_policy_' . $type] = $legacy('s3_retention_policy');
            $values['s3_retention_keep_' . $type] = $legacy('s3_retention_keep');
            $values['s3_retention_days_' . $type] = $legacy('s3_retention_days');
        }

        $values['full_host_assumptions_acknowledged'] = $legacy('cooperative_writers_acknowledged') && $legacy('external_state_acknowledged');
        $plaintext = (bool) $legacy('plaintext_offsite_acknowledged');
        $values['plaintext_offsite_acknowledged'] = $plaintext;
        // Uploads that were plaintext stay plaintext until settings are saved in version 2.
        $values['s3_encryption'] = !($s3 && $plaintext);
        return $values;
    }
}
