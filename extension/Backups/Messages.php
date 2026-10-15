<?php
namespace Paymenter\Extensions\Others\Backups;

/** Every administrator-facing sentence for engine codes, statuses and tasks. */
final class Messages
{
    private const ERRORS = [
        // Scheduling and admission
        'busy' => 'Another backup task was running. This one will retry automatically.',
        'billing-guard-stale' => 'Paymenter\'s scheduler has not run recently. Check its cron job on the Help tab.',
        'billing-window-protected' => 'Paused to stay clear of Paymenter\'s daily billing run. It will retry automatically.',
        'schedule-overlaps-billing' => 'This time is too close to Paymenter\'s daily billing run. Pick a time at least 10 minutes away.',
        'job-lock-busy' => 'A Paymenter cron job was still running. The backup will retry automatically.',
        'job-locks-unverified' => 'Full backups need the lock files that your Paymenter cron jobs use. Add them in Settings, under Host checks.',
        'cooperative-writers-acknowledged' => 'Confirm that no other program writes to Paymenter\'s files or database during a backup.',
        'external-state-acknowledged' => 'Confirm that Paymenter keeps no data outside its folder and database.',
        'no-concurrent-ddl-acknowledgement-required' => 'Confirm that database schema changes never run during a backup.',
        'recovery-required' => 'An earlier backup did not finish cleanly. Resolve it from the terminal before starting another. See Help.',
        'installation-not-initialized' => 'Review the settings and save them once. This creates the private folder for backups.',
        'initial-maintenance-scheduled-skip' => 'Skipped because the site was already in maintenance mode.',
        'worker-interrupted' => 'The task stopped unexpectedly. It will retry.',
        'request-not-cancellable' => 'This task has already started and can no longer be cancelled.',
        'request-action-invalid' => 'This task type is not supported.',
        'request-arguments-invalid' => 'This task request is not valid.',

        // Configuration
        'config-invalid' => 'Some settings are not valid. Check the highlighted fields.',
        'config-write-failed' => 'Settings could not be saved.',
        'tool-unavailable' => 'A required program was not found. Check the program paths under Advanced.',
        'tool-path-invalid' => 'Program paths must be absolute, for example /usr/bin/tar.',
        'plaintext-offsite-acknowledgement-required' => 'S3 storage needs a bucket name and your confirmation that archives are stored unencrypted.',
        's3-tls-required' => 'The S3 endpoint must start with https://.',
        's3-endpoint-invalid' => 'Enter the S3 endpoint as https://host, without a path, user name or query.',
        's3-prefix-invalid' => 'The folder prefix cannot contain empty parts, "." or "..", or backslashes.',
        'private-root-invalid' => 'The private folder must be an absolute path outside Paymenter and its public folders, owned by the site user.',
        'private-root-inside-app' => 'The private folder must be outside Paymenter\'s folder.',
        'private-root-inside-docroot' => 'The private folder is inside a public web folder. Choose a folder that no site serves.',
        'private-root-unsafe' => 'The private folder must not be a link.',
        'private-root-unavailable' => 'The web server cannot create or open the private folder. Check its owner and the PHP open_basedir setting.',
        'document-roots-invalid' => 'Each public web folder must be an existing absolute path.',
        'owner-mismatch' => 'The private folder belongs to a different user than the site.',
        'private-state-permissions' => 'Files in the private folder have the wrong owner or permissions.',
        'private-file-invalid' => 'Files in the private folder have the wrong owner or permissions.',
        'private-write-failed' => 'The private folder is not writable.',
        'state-unreadable' => 'A file in the private folder could not be read.',
        'installation-mismatch' => 'The private folder belongs to a different Paymenter installation.',
        'installation-missing' => 'Save settings once to set up private storage, then save the keys.',
        'extension-uninstalled' => 'The Backups extension is not installed.',
        'native-secret-row-refused' => 'An extension setting row looks like a secret. Remove it from the extension\'s native settings.',
        'billing-settings-unreadable' => 'Paymenter\'s billing time could not be read.',
        'database-tls-unsupported' => 'Database connections that require TLS are not supported yet.',
        'database-driver-unsupported' => 'Only MySQL and MariaDB databases are supported.',
        'database-prefix-unsupported' => 'The database table prefix contains unsupported characters.',
        'application-unavailable' => 'Paymenter\'s files could not be read by the backup engine.',
        'python-binary-invalid' => 'The Python path is not a working Python 3.10 or newer program. Check it under Advanced.',
        'php-binary-mismatch' => 'The PHP path is not a working PHP command-line program. Check it under Advanced.',
        'engine-unavailable' => 'The backup engine could not run from the web server.',
        'bridge-failed' => 'The backup engine could not read Paymenter\'s configuration.',
        'bridge-identity-mismatch' => 'The backup engine read a different Paymenter installation than expected.',

        // Credentials
        'credentials-unreadable' => 'Saved keys cannot be decrypted with the current APP_KEY. Restore the previous APP_KEY, or move the old key file aside from the terminal, then save new keys.',
        'credentials-invalid' => 'Enter both the access key ID and the secret access key.',
        'credentials-save-failed' => 'The keys could not be saved. Check that the web server can write to the private folder.',
        'maintenance-probe-credentials-invalid' => 'Enter a user name without a colon and a password.',
        'maintenance-probe-origin-mismatch' => 'Paymenter\'s address changed. Reload the page and save the site password again.',
        'maintenance-probe-https-required' => 'A site password is only sent over https://. Switch Paymenter\'s address to https:// first.',
        's3-credentials-required' => 'Save S3 access keys before copying backups to S3.',

        // Capture
        'maintenance-probe-failed' => 'The site did not show Paymenter\'s maintenance page during the check. If a password prompt protects the site, save it under Site password.',
        'maintenance-unsafe' => 'Paymenter\'s maintenance mode was not in the expected state, so the backup did not continue.',
        'maintenance-driver-unsupported' => 'Full backups need Paymenter\'s file-based maintenance mode.',
        'maintenance-changed' => 'Maintenance mode changed during the backup.',
        'maintenance-ownership-uncertain' => 'The backup could not confirm it was safe to reopen the site. Check the site, then resolve it from the terminal. See Help.',
        'process-state-uncertain' => 'A backup process may still be running. Check the server, then resolve it from the terminal. See Help.',
        'external-durable-state-unsupported' => 'Full backups need file or database drivers for Paymenter\'s queue, sessions, cache and storage.',
        'external-symlink-target' => 'Paymenter\'s folder links to files outside it, which a full backup cannot include.',
        'unsupported-db-objects' => 'The database has views, triggers, routines, events or non-InnoDB tables, which these backups do not support.',
        'unsupported-database' => 'Only MySQL and MariaDB databases are supported.',
        'database-inventory-failed' => 'The database could not be inspected. Check the database user\'s permissions.',
        'database-changed-during-capture' => 'The database changed while the site was paused. Check for jobs that ignore maintenance mode.',
        'capture-timeout' => 'The backup took longer than its time limit. Raise the capture time limit under Advanced.',
        'child-timeout' => 'A backup step took longer than its time limit. Raise the capture time limit under Advanced.',
        'child-failed' => 'A backup step failed. Check the program paths and database access.',
        'child-output-limit' => 'The backup reached the space limit while writing. Raise the space limit or free disk space.',
        'capture-failed' => 'The backup failed before it finished.',
        'capture-interrupted' => 'The backup was stopped before it finished.',
        'dump-empty' => 'The database export was empty.',
        'disk-reserve-exceeded' => 'There is not enough free disk space. Free some space or lower the minimum free space.',
        'local-byte-budget-exceeded' => 'Backups have reached the space limit. Delete older backups or raise the limit.',
        'destination-unsafe' => 'The backup folder must be private, owned by the site user, and outside Paymenter and its public folders.',
        'destination-inside-docroot' => 'The backup folder is inside a public web folder. Choose a folder that no site serves.',
        'private-root-move-refused' => 'The private folder holds this installation\'s ID, keys and backup history, so it cannot be moved from this page. Move it from the terminal.',

        // Archives
        'archive-checksum-mismatch' => 'The archive does not match its checksum. Do not restore from it.',
        'archive-payload-mismatch' => 'Files inside the archive do not match their checksums. Do not restore from it.',
        'archive-layout-invalid' => 'The archive contains unexpected files. Do not restore from it.',
        'archive-manifest-mismatch' => 'The archive\'s manifest does not match. Do not restore from it.',
        'archive-unreadable' => 'The archive is damaged or incomplete. Do not restore from it.',
        'archive-changed-during-verification' => 'The archive changed while it was being checked. Verify it again.',
        'archive-changed-during-download' => 'The archive changed while it was being read. Verify it again.',
        'archive-changed-during-export' => 'The archive changed while it was being copied. Verify it again.',
        'archive-changed-during-delivery' => 'The archive changed while it was being copied to S3. Verify it again.',
        'artifact-changed' => 'The backup files changed while they were being read. Try again.',
        'artifact-missing' => 'The archive file is missing from storage.',
        'artifact-unsafe' => 'The archive file has unexpected permissions or links, so it will not be used. Check the backup folder.',
        'artifact-location-invalid' => 'The archive is outside this installation\'s private backup folder, so it will not be used.',
        'artifact-manifest-invalid' => 'The backup\'s manifest is damaged.',
        'artifact-identity-mismatch' => 'This backup belongs to a different installation.',
        'artifact-deleted' => 'This backup was deleted.',
        'verification-required' => 'The archive changed since it was last checked. Verify it before downloading.',
        'artifact-stream-timeout' => 'The download did not start in time. Try again. If it keeps failing, use the export command on the Help tab.',
        'run-incomplete' => 'This backup did not finish.',
        'run-not-found' => 'This backup does not exist.',
        'invalid-run-id' => 'This backup does not exist.',

        // Storage, delivery and deletion
        's3-client-failed' => 'The S3 client could not start. Check the region and endpoint.',
        'isolated-s3-sdk-missing' => 'The bundled S3 library is missing. Reinstall the extension ZIP.',
        's3-readback-mismatch' => 'The copy in S3 did not match after upload. The local backup is kept. Retry the copy.',
        's3-manifest-readback-mismatch' => 'The manifest in S3 did not match after upload. The local backup is kept. Retry the copy.',
        's3-delivery-failed' => 'Copying to S3 failed. The local backup is kept. Check the S3 settings and keys, then retry.',
        'multipart-destination-mismatch' => 'An unfinished upload belongs to a different S3 location. Restore the earlier S3 settings, or finish it from the terminal.',
        'destination-probe-failed' => 'The storage test could not write and read back a test file.',
        'destination-readback-mismatch' => 'The storage test read back different data than it wrote.',
        'destination-test-failed' => 'The storage test could not reach the bucket. Check the endpoint, region, bucket and keys.',
        'destination-probe-cleanup-failed' => 'The storage test file could not be removed from S3. Check that the keys allow deletes.',
        'delete-last-backup' => 'This is the only usable backup. Create another one before deleting it.',
        'delete-latest-remote-copy' => 'This is the newest backup in S3. Keep it until a newer backup is copied there.',
        'delete-no-verified-survivor' => 'No other backup passed a full check, so this one was kept.',
        'delete-conflict' => 'Another task is using this backup. Wait for it to finish.',
        'delete-confirmation-required' => 'Confirm the deletion first.',
        'delete-remote-scope-changed' => 'This backup\'s S3 copy is in a different bucket or prefix than the current settings. Delete it from the terminal.',
        'delete-ownership-mismatch' => 'These files are not in this installation\'s backup folder or S3 prefix, so they were kept.',
        'delete-remote-verification-failed' => 'The copy in S3 did not match this backup, so it was kept.',
        'delete-remote-delete-failed' => 'Deleting the S3 copy failed. Retry. Files already removed stay removed.',
        'delete-local-delete-failed' => 'Deleting the local files failed. Retry.',
        'delete-local-verification-failed' => 'The local files changed since the backup was made, so they were kept.',
        'retention-ownership-mismatch' => 'Retention found files outside this installation\'s backup folder or S3 prefix and kept them.',
        'retention-remote-verification-failed' => 'Retention found an S3 copy that did not match its backup and kept it.',
        'retention-remote-delete-failed' => 'Retention could not delete an S3 copy. It will retry with the next backup.',
        'retention-local-delete-failed' => 'Retention could not delete a local file. It will retry with the next backup.',
        'retention-local-verification-failed' => 'Retention found local files that changed since the backup and kept them.',
    ];

    public static function error(?string $code): string
    {
        if ($code === null || $code === '') { return ''; }
        return self::ERRORS[$code] ?? 'The task failed. Error code ' . (preg_match('/^[a-z0-9-]{1,64}$/D', $code) ? $code : 'unknown') . '.';
    }

    public static function known(string $code): bool { return isset(self::ERRORS[$code]); }

    /** Settings field that an engine configuration refusal points at. */
    public static function field(string $code): ?string
    {
        return [
            'schedule-overlaps-billing' => 'schedule_time', 's3-tls-required' => 's3_endpoint', 's3-endpoint-invalid' => 's3_endpoint',
            's3-prefix-invalid' => 's3_prefix', 'plaintext-offsite-acknowledgement-required' => 'plaintext_offsite_acknowledged',
            'billing-guard-stale' => 'enabled', 'private-root-invalid' => 'private_root', 'private-root-inside-docroot' => 'private_root',
            'private-root-inside-app' => 'private_root', 'private-root-unsafe' => 'private_root', 'owner-mismatch' => 'private_root',
            'installation-mismatch' => 'private_root', 'document-roots-invalid' => 'extra_docroots', 'tool-path-invalid' => 'python',
            'python-binary-invalid' => 'python', 'php-binary-mismatch' => 'php', 'destination-unsafe' => 'local_path',
            'destination-inside-docroot' => 'local_path', 'private-root-move-refused' => 'private_root',
        ][$code] ?? null;
    }

    public const MODES = [
        'full-quiesced' => 'Full backup',
        'database-online' => 'Database only',
    ];

    public static function mode(?string $mode): string { return self::MODES[$mode] ?? 'Backup'; }

    /** [label, Filament color] for a backup row. */
    public static function runStatus(array $run): array
    {
        return match ($run['status']) {
            'ready' => match ($run['download']) {
                'ready' => ['Verified', 'success'],
                'verify' => ['Check needed', 'warning'],
                default => ['Unavailable', 'danger'],
            },
            'damaged' => ['Damaged', 'danger'],
            'running' => ['Creating', 'info'],
            'failed' => ['Failed', 'danger'],
            'attention' => ['Needs attention', 'warning'],
            'interrupted' => ['Interrupted', 'danger'],
            'deleting' => ['Deleting', 'info'],
            'delete-interrupted' => ['Delete interrupted', 'warning'],
            'deleted' => ['Deleted', 'gray'],
            default => ['Unknown', 'gray'],
        };
    }

    /** One short sentence under a backup's status, or null. */
    public static function runDetail(array $run): ?string
    {
        return match ($run['status']) {
            'ready' => match ($run['download']) {
                'ready' => $run['remote'] === 'failed' ? self::error($run['delivery_error'] ?: 's3-delivery-failed') : null,
                'verify' => 'The file changed since it was last checked. Verify it before downloading.',
                default => self::error($run['download']),
            },
            'damaged' => self::error($run['verification_error']),
            'running' => 'The backup is being written now.',
            'failed' => self::error($run['error'] ?: 'capture-failed'),
            'attention' => 'The site may still be in maintenance mode. ' . self::error($run['recovery_error'] ?: 'maintenance-ownership-uncertain'),
            'interrupted' => 'The backup process stopped before it finished.',
            'deleting' => 'Waiting for the scheduler to delete the files.',
            'delete-interrupted' => 'Some files may remain. Delete it again to finish.',
            'deleted' => $run['deletion_reason'] === 'manual' ? 'Deleted by an administrator.' : 'Removed by retention.',
            default => null,
        };
    }

    public static function location(array $run, array $configuration): string
    {
        if (!$run['local']) {
            return $run['status'] === 'deleted' ? 'Removed' : 'Not stored';
        }
        return match ($run['remote']) {
            'delivered' => 'This server and S3',
            'failed' => 'This server, S3 copy failed',
            default => (($configuration['destination']['type'] ?? 'local') === 's3') ? 'This server, not yet in S3' : 'This server',
        };
    }

    public const TASKS = [
        'run' => 'Create backup', 'verify' => 'Verify backup', 'deliver' => 'Copy to S3',
        'retention' => 'Apply retention', 'destination-test' => 'Test storage', 'delete' => 'Delete backup',
    ];

    public static function task(array $request): string
    {
        if ($request['action'] === 'run') {
            $label = $request['scheduled'] ? 'Scheduled backup' : 'Create backup';
            return $request['mode'] ? $label . ', ' . strtolower(self::mode($request['mode'])) : $label;
        }
        return self::TASKS[$request['action']] ?? 'Backup task';
    }

    /** [label, Filament color] for a task row. */
    public static function taskStatus(string $status): array
    {
        return match ($status) {
            'pending' => ['Waiting for scheduler', 'info'],
            'deferred' => ['Will retry', 'warning'],
            'launching' => ['Starting', 'info'],
            'running' => ['Running', 'info'],
            'complete' => ['Done', 'success'],
            'failed' => ['Failed', 'danger'],
            'cancelled' => ['Cancelled', 'gray'],
            'expired' => ['Expired', 'gray'],
            'skipped' => ['Skipped', 'gray'],
            default => ['Unknown', 'gray'],
        };
    }

    public static function bytes(?int $bytes): string
    {
        if ($bytes === null) { return 'Unknown'; }
        $units = ['bytes', 'KiB', 'MiB', 'GiB', 'TiB'];
        $value = (float) $bytes;
        $unit = 0;
        while ($value >= 1024 && $unit < count($units) - 1) { $value /= 1024; $unit++; }
        if ($unit === 0) { return $bytes . ' bytes'; }
        $number = number_format($value, $value < 10 ? 1 : 0, '.', ',');
        // Trim only a fractional zero; 10 GiB must never read as 1 GiB.
        if (str_ends_with($number, '.0')) { $number = substr($number, 0, -2); }
        return $number . ' ' . $units[$unit];
    }

    public const WEEKDAYS = [0 => 'Monday', 1 => 'Tuesday', 2 => 'Wednesday', 3 => 'Thursday', 4 => 'Friday', 5 => 'Saturday', 6 => 'Sunday'];
}
