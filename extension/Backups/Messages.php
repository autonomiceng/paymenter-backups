<?php
namespace Paymenter\Extensions\Others\Backups;

/** Every administrator-facing sentence for engine codes, statuses and tasks. */
final class Messages
{
    private const ERRORS = [
        // Scheduling and admission
        'busy' => 'Another backup task was running. This task retries when the scheduler next runs.',
        'billing-guard-stale' => 'Full backups need a recent daily billing report from Paymenter\'s scheduler. Check its server scheduler on Help.',
        'billing-window-protected' => 'Paused to stay clear of Paymenter\'s daily billing run. It will retry automatically.',
        'schedule-overlaps-billing' => 'This full backup could overlap daily billing. Choose a time outside the window shown under Time.',
        'job-lock-busy' => 'A Paymenter job was still running. The backup will retry automatically.',
        'job-locks-unverified' => 'Full backups could not confirm every Paymenter scheduled job uses a listed flock file. Compare Host checks with the existing systemd services or crontab. Uncoordinated workers also block full backups.',
        'cooperative-writers-acknowledged' => 'Under Settings, Host checks, confirm that only Paymenter and its scheduled jobs write its files and database.',
        'external-state-acknowledged' => 'Under Settings, Host checks, confirm Paymenter keeps its data in its directory and database.',
        'no-concurrent-ddl-acknowledgement-required' => 'Under Settings, Host checks, confirm database schema changes never run during a backup.',
        'recovery-required' => 'An earlier full backup did not finish cleanly. Use the recovery check on Help, then resolve or acknowledge it before new backups.',
        'installation-not-initialized' => 'Save settings once to create the extension data directory used for backup history and encrypted credentials.',
        'initial-maintenance-scheduled-skip' => 'Skipped because the site was already in maintenance mode. Scheduled full backups preserve existing maintenance.',
        'worker-interrupted' => 'The task stopped unexpectedly. It retries when the scheduler next runs.',
        'request-not-cancellable' => 'This task has already started and can no longer be cancelled.',
        'request-action-invalid' => 'This task type is not supported.',
        'request-arguments-invalid' => 'This task request is not valid.',

        // Configuration
        'config-invalid' => 'A saved Backups setting is invalid. Open Settings, review the values and save to correct it.',
        'config-write-failed' => 'Paymenter could not save these settings. Check that php artisan runs for this site from a terminal.',
        'tool-unavailable' => 'A required program was missing or could not run. Check the program paths under Advanced.',
        'tool-path-invalid' => 'Program paths must be absolute, for example /usr/bin/tar.',
        'plaintext-offsite-acknowledgement-required' => 'S3 storage needs a bucket name and your confirmation that archives are stored unencrypted.',
        's3-tls-required' => 'The S3 endpoint must start with https://.',
        's3-endpoint-invalid' => 'Enter an HTTPS S3 API address without a username, query or #.',
        's3-prefix-invalid' => 'The object prefix cannot contain empty parts, "." or "..", or backslashes.',
        'private-root-invalid' => 'The extension data directory must be an absolute path outside Paymenter and public web directories, owned by the site user with mode 700.',
        'private-root-inside-app' => 'The extension data directory must be outside Paymenter\'s directory.',
        'private-root-inside-docroot' => 'The extension data directory is inside a public web directory. Choose a directory that no site serves.',
        'private-root-unsafe' => 'The extension data directory must not be a link. Enter its real path.',
        'private-root-unavailable' => 'The web server cannot create or open the extension data directory. Check its owner and the PHP open_basedir setting.',
        'document-roots-invalid' => 'In Paymenter, open Extensions, edit Backups and correct Other public web directories. Enter a JSON list of existing absolute paths.',
        'owner-mismatch' => 'The extension data and Paymenter directories must belong to the site user. The extension data directory needs mode 700.',
        'private-state-permissions' => 'A file or directory in the extension data directory has the wrong owner or permissions. The site user must own files with mode 600 and directories with mode 700.',
        'private-file-invalid' => 'A file or directory in the extension data directory has the wrong owner or permissions. The site user must own files with mode 600 and directories with mode 700.',
        'private-write-failed' => 'The extension data directory is not writable. Check its owner and free disk space.',
        'state-unreadable' => 'A file in the extension data directory could not be read. It may be damaged or incomplete.',
        'installation-mismatch' => 'This extension data directory belongs to a different Paymenter installation. Use this site\'s original directory.',
        'installation-missing' => 'Save settings once to create the extension data directory before saving credentials.',
        'extension-uninstalled' => 'The Backups extension is not installed.',
        'native-secret-row-refused' => 'An encrypted or secret-named Backups setting exists in Paymenter\'s database. Remove that row. Save credentials through the encrypted sections on this page.',
        'billing-settings-unreadable' => 'Paymenter\'s daily billing time could not be read. Save Cron Job Time in Paymenter\'s settings.',
        'database-tls-unsupported' => 'Database connections that require TLS are not supported yet.',
        'database-driver-unsupported' => 'Only MySQL and MariaDB databases are supported.',
        'database-prefix-unsupported' => 'The database table prefix contains unsupported characters.',
        'application-unavailable' => 'The backup program could not load Paymenter\'s files. Check the site user can read its directory and vendor/autoload.php.',
        'python-binary-invalid' => 'The Python path is not a working Python 3.10 or newer program. Check it under Advanced.',
        'php-binary-mismatch' => 'The PHP path is not a working PHP command-line program. Check it under Advanced.',
        'engine-unavailable' => 'The web server could not start the backup program. Check the Python and PHP paths under Settings, Advanced.',
        'bridge-failed' => 'The backup program could not read Paymenter\'s configuration or database. Check .env and database access.',
        'bridge-identity-mismatch' => 'The backup program read a different Paymenter installation than expected.',

        // Credentials
        'credentials-unreadable' => 'Saved credentials cannot be decrypted. Restore the previous APP_KEY or APP_PREVIOUS_KEYS. If the key is lost, move credentials.json out of the extension data directory, then save credentials again.',
        'credentials-invalid' => 'Enter both the access key ID and the secret access key.',
        'credentials-save-failed' => 'The encrypted credentials file could not be written. Check that the site user can write the extension data directory.',
        'maintenance-probe-credentials-invalid' => 'Enter the HTTP Basic username and password. The username cannot contain a colon or control characters.',
        'maintenance-probe-origin-mismatch' => 'HTTP Basic credentials were saved for another App URL. Save them for the current HTTPS address or remove them before a full backup.',
        'maintenance-probe-https-required' => 'HTTP Basic credentials require HTTPS. Change App URL in Paymenter\'s settings to HTTPS first.',
        's3-credentials-required' => 'Save S3 access keys in Settings before copying backups to S3.',

        // Capture
        'maintenance-probe-failed' => 'The maintenance check did not receive Paymenter\'s exact 503 page. Check App URL, redirects, TLS and proxies. If a browser password prompt protects the site, save its HTTP Basic credentials in Settings.',
        'maintenance-unsafe' => 'Full backups need a plain 503 maintenance page without bypasses, redirects or exceptions. Check Paymenter\'s maintenance configuration before retrying.',
        'maintenance-driver-unsupported' => 'Full backups need file-based maintenance. Set APP_MAINTENANCE_DRIVER=file.',
        'maintenance-changed' => 'Maintenance files changed during the backup. Check whether someone ran artisan up or down before retrying.',
        'maintenance-ownership-uncertain' => 'The backup could not safely reopen the site. Check the site and run the recovery check on Help.',
        'process-state-uncertain' => 'A backup process may still be running. Check the server and run the recovery check on Help.',
        'external-durable-state-unsupported' => 'Full backups require local data: database or sync queues; file or database sessions; file, database or array cache; local or public file storage.',
        'external-symlink-target' => 'Paymenter\'s directory links to files outside it, which a full backup cannot include.',
        'unsupported-db-objects' => 'The database has views, triggers, routines, events or non-InnoDB tables, which these backups do not support.',
        'unsupported-database' => 'Only MySQL and MariaDB databases are supported.',
        'database-inventory-failed' => 'The database could not be inspected. Check the database user\'s permissions.',
        'database-changed-during-capture' => 'The database changed while the site was paused. Check for jobs that ignore maintenance mode.',
        'capture-timeout' => 'The backup exceeded Capture time limit and stopped. Increase that limit under Advanced.',
        'child-timeout' => 'A backup step exceeded its time limit and stopped. Review Capture time limit under Advanced.',
        'child-failed' => 'A backup step failed. Check the program paths and database access.',
        'child-output-limit' => 'The backup would exceed the space limit or disk reserve. Free space or review Retention and space.',
        'capture-failed' => 'An unexpected error stopped the backup before completion. Check the server before retrying.',
        'capture-interrupted' => 'The backup process stopped before completion, for example during a server restart.',
        'dump-empty' => 'The database export was empty. Check the database name and user in Paymenter\'s .env.',
        'disk-reserve-exceeded' => 'The extension data or backup disk is below Keep this much disk free. Free space or lower that reserve.',
        'local-byte-budget-exceeded' => 'Backups reached the space limit or the disk is nearly full. Delete older copies or review Retention and space.',
        'destination-unsafe' => 'The backup directory must be private, owned by the site user, and outside Paymenter and its public directories.',
        'destination-inside-docroot' => 'The backup directory is inside a public web directory. Choose a directory that no site serves.',
        'private-root-move-refused' => 'The extension data directory holds this installation\'s ID, keys and backup history, so it cannot be moved from this page. Keep this path unchanged. Changing it would lose access to saved credentials and history.',

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
        'artifact-missing' => 'The archive is missing from its backup directory.',
        'artifact-unsafe' => 'The archive file has unexpected permissions or links, so it will not be used. Check the backup directory.',
        'artifact-location-invalid' => 'The archive is outside this installation\'s private backup directory, so it will not be used.',
        'artifact-manifest-invalid' => 'The backup\'s manifest is damaged.',
        'artifact-identity-mismatch' => 'This backup belongs to a different installation.',
        'artifact-deleted' => 'This backup was deleted.',
        'verification-required' => 'The archive has no current verification. Verify it before downloading.',
        'artifact-stream-timeout' => 'The download did not start in time. Try again. If it keeps failing, use the export command on the Help tab.',
        'run-incomplete' => 'This backup did not finish.',
        'run-not-found' => 'This backup does not exist.',
        'invalid-run-id' => 'This backup does not exist.',

        // Storage, delivery and deletion
        'installation-state-orphaned' => 'Saved extension data has no valid installation ID. Restore the original extension data before starting backups.',
        'legacy-local-source-damaged' => 'The older local backup did not pass verification. Use another verified copy.',
        'legacy-manifest-replaced' => 'The older backup record changed while being checked. Verify the local copy before retrying.',
        'retrieve-local-copy-changed' => 'The retrieved local copy changed before publication. It was not made available for download.',
        'retrieve-intent-invalid' => 'The unfinished retrieval record is invalid. Check the extension data directory before retrying.',
        'retention-pointer-update-failed' => 'The latest S3 backup record could not be updated. Check S3 write permissions, then apply retention again.',
        'maintenance-probe-http-saved' => 'Saved HTTP Basic credentials require HTTPS, but App URL uses http://. Full backups cannot start. Remove the saved credentials in Settings or restore an HTTPS App URL.',
        'maintenance-probe-origin-invalid' => 'App URL is not valid for this maintenance check. Correct it in Paymenter settings. HTTP Basic credentials require HTTPS.',
        'remote-copy-missing' => 'The S3 copy is missing. A bucket lifecycle rule may have removed it. Use another available copy.',
        'remote-copy-damaged' => 'The S3 copy did not match its backup record. Use another verified copy.',
        'remote-copy-unknown' => 'The S3 copy has not been checked successfully. Verify it before relying on it.',
        'remote-copy-error' => 'The S3 copy could not be checked. Check the S3 settings and keys before retrying.',
        'remote-check-failed' => 'The S3 verification could not finish. Check S3 access and retry.',
        'remote-archive-size-mismatch' => 'The S3 archive has the wrong size. It was not made available for download.',
        'remote-archive-checksum-mismatch' => 'The S3 archive failed its checksum check. It was not made available for download.',
        'remote-scope-changed' => 'This copy belongs to an earlier S3 location. Restore its original S3 settings before checking or retrieving it.',
        'remote-location-unbound' => 'This older S3 copy has no verified destination binding. Check its original S3 settings before relying on it.',
        'delivery-destination-changed' => 'The destination changed since the unfinished upload began. Restore the original S3 settings before retrying.',
        'delete-no-verified-local-survivor' => 'No other local backup passed verification. Create or verify another local backup before deleting this copy.',
        'delete-no-verified-s3-survivor' => 'No other S3 backup passed verification. Copy another verified backup to S3 before deleting this copy.',
        'delete-last-s3-backup' => 'This is the only recorded S3 backup. Copy another verified backup to S3 before deleting it.',
        'delete-remote-location-unbound' => 'This S3 copy has no verified destination binding, so it cannot be deleted here. Local-only deletion can still be selected.',
        'delete-location-invalid' => 'Choose this server, S3 or all copies for deletion.',
        'delete-location-changed' => 'The earlier deletion targeted another location. Select that same location to finish the deletion.',
        'retrieve-local-copy-exists' => 'This backup already has a local copy, or another file occupies its retrieval path. Check local history and the original backup directory before retrying.',
        'retrieve-spool-changed' => 'An interrupted S3 download file changed. Ask the server administrator to inspect it before retrying.',
        'multipart-location-unbound' => 'An older unfinished upload has no recorded S3 location. Ask the storage provider to remove it, or use a rule that removes incomplete uploads. The extension kept it.',
        'installation-state-orphaned' => 'The extension data directory contains files but has no installation record. Restore that record or choose a new empty directory. Existing files were kept.',
        'schedule-superseded' => 'This scheduled task was cancelled because the schedule changed or a newer backup time replaced it.',
        'remote-object-too-large' => 'The S3 backup record exceeds its size limit. It was not used.',
        'retention-remote-scope-changed' => 'Retention kept an S3 copy outside the current bucket or object prefix.',
        'retention-skipped-damaged' => 'Retention kept a damaged backup. Check another verified copy before deleting it manually.',
        's3-client-failed' => 'The S3 client could not start. Check the region and endpoint.',
        'isolated-s3-sdk-missing' => 'The bundled S3 library is missing. Reinstall the extension ZIP.',
        's3-readback-mismatch' => 'The copy in S3 did not match after upload. The local backup is kept. Retry the copy.',
        's3-manifest-readback-mismatch' => 'The manifest in S3 did not match after upload. The local backup is kept. Retry the copy.',
        's3-delivery-failed' => 'Copying to S3 failed. The local backup is kept. Check the S3 settings and keys, then retry.',
        'multipart-destination-mismatch' => 'An unfinished upload belongs to an earlier S3 location. Restore those S3 settings and copy again, or use the provider\'s incomplete-upload cleanup rule.',
        'destination-probe-failed' => 'The storage test could not write and read back a test file.',
        'destination-readback-mismatch' => 'The storage test read back different data than it wrote.',
        'destination-test-failed' => 'The storage test could not write to the bucket. Check the endpoint, region, bucket and key permissions.',
        'destination-probe-cleanup-failed' => 'The storage test file could not be removed from S3. Check that the keys allow deletes.',
        'delete-last-backup' => 'No other usable backup exists. Create another backup before deleting this copy.',
        'delete-latest-remote-copy' => 'This is the newest backup in S3. Keep it until a newer backup is copied there.',
        'delete-no-verified-survivor' => 'No other copy passed verification at the selected location. The selected copies were kept.',
        'delete-conflict' => 'Another task is using this backup. Wait for it to finish.',
        'delete-confirmation-required' => 'Confirm the deletion first.',
        'delete-remote-scope-changed' => 'This S3 copy is outside the current S3 location. Restore its S3 settings or remove it in the provider. Local-only deletion can still be selected.',
        'delete-ownership-mismatch' => 'These files are not in this installation\'s backup directory or S3 prefix, so they were kept.',
        'delete-remote-verification-failed' => 'The S3 copy is missing or did not match the backup. It was kept.',
        'delete-remote-delete-failed' => 'Deleting the S3 copy failed. Check delete permissions and retry. Copies already removed stay removed.',
        'delete-local-delete-failed' => 'Deleting the local files failed. Retry.',
        'delete-local-verification-failed' => 'The local files changed after verification, so they were kept.',
        'retention-ownership-mismatch' => 'Retention found files outside this installation\'s backup directory or S3 prefix and kept them.',
        'retention-remote-verification-failed' => 'Retention could not verify an S3 copy and kept it. Check that copy before applying retention again.',
        'retention-remote-delete-failed' => 'Retention could not delete an S3 copy. Check the keys allow deletes, then use Apply retention.',
        'retention-local-delete-failed' => 'Retention could not delete a local copy. Check its backup directory, then use Apply retention.',
        'retention-local-verification-failed' => 'Retention found local files that changed since the backup and kept them.',
    ];

    public static function error(?string $code): string
    {
        if ($code === null || $code === '') { return ''; }
        return self::ERRORS[$code] ?? 'The task failed with error code ' . (preg_match('/^[a-z0-9-]{1,64}$/D', $code) ? $code : 'unknown') . '.';
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
        if (!$run['local'] && $run['remote'] === 'missing') { return ['S3 copy missing', 'warning']; }
        return match ($run['status']) {
            'ready' => match ($run['download']) {
                'ready' => ['Verified', 'success'],
                'retrieve' => [$run['remote'] === 'present' ? 'Present in S3' : 'In S3', 'info'],
                'verify' => ['Not verified', 'warning'],
                default => match ($run['remote']) {
                    'missing' => ['S3 copy missing', 'warning'], 'damaged' => ['S3 copy damaged', 'danger'],
                    'scope-changed' => ['Previous S3 location', 'warning'], 'unbound' => ['S3 location unverified', 'warning'],
                    default => ['Unavailable', 'danger'],
                },
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
        if ($run['retention_error'] ?? null) { return self::error($run['retention_error']); }
        if (!$run['local'] && in_array($run['remote'], ['missing', 'damaged', 'scope-changed', 'unbound', 'unknown'], true)) {
            return self::error(match ($run['remote']) { 'scope-changed' => 'remote-scope-changed', 'unbound' => 'remote-location-unbound', default => 'remote-copy-' . $run['remote'] });
        }
        return match ($run['status']) {
            'ready' => match ($run['download']) {
                'ready' => $run['remote'] === 'failed' ? self::error($run['delivery_error'] ?: 's3-delivery-failed') : null,
                'retrieve' => 'Stored only in S3. Retrieve it to verify a local copy and make Download available.',
                'verify' => 'No current verification is recorded. Verify this backup before downloading.',
                default => self::error($run['download']),
            },
            'damaged' => self::error($run['verification_error']),
            'running' => $run['mode'] === 'database-online' ? 'Writing the backup. The site stays online.' : (in_array($run['phase'] ?? '', ['entering-maintenance', 'draining', 'capturing'], true) ? 'Copying data while visitors see the maintenance page.' : 'Finishing the archive after the site reopens.'),
            'failed' => self::error($run['error'] ?: 'capture-failed'),
            'attention' => (($run['maintenance'] ?? false) ? 'The site shows its maintenance page. ' : 'The site is online. ') . self::error($run['recovery_error'] ?: 'maintenance-ownership-uncertain'),
            'interrupted' => 'The backup process stopped before it finished.',
            'deleting' => 'Deletion is queued or running.',
            'delete-interrupted' => 'Some files may remain. Delete it again to finish.',
            'deleted' => $run['deletion_reason'] === 'manual' ? 'Deleted by an administrator.' : 'Removed by retention.',
            default => null,
        };
    }

    public static function location(array $run, array $configuration): string
    {
        if (!$run['local']) {
            return $run['status'] === 'deleted' ? 'Removed' : match ($run['remote']) {
                'delivered', 'present' => 'S3 only', 'missing' => 'S3 copy missing', 'damaged' => 'S3 copy damaged',
                'scope-changed' => 'Previous S3 location', 'unbound' => 'S3 location needs checking', default => 'Not stored',
            };
        }
        return match ($run['remote']) {
            'delivered', 'present' => 'This server and S3',
            'failed' => 'This server, S3 copy failed',
            'missing' => 'This server, S3 copy missing', 'damaged' => 'This server, S3 copy damaged',
            'scope-changed' => 'This server, previous S3 location', 'unbound' => 'This server, S3 location needs checking',
            default => (($configuration['destination']['type'] ?? 'local') === 's3') ? 'This server, not yet in S3' : 'This server',
        };
    }

    public const TASKS = [
        'run' => 'Create backup', 'verify' => 'Verify backup', 'deliver' => 'Copy to S3',
        'retrieve' => 'Retrieve from S3', 'retention' => 'Apply retention', 'destination-test' => 'Test storage', 'delete' => 'Delete backup',
    ];

    public static function task(array $request): string
    {
        if ($request['action'] === 'run') {
            $label = $request['scheduled'] ? 'Scheduled backup' : 'Create backup';
            return $request['mode'] ? $label . ', ' . strtolower(self::mode($request['mode'])) : $label;
        }
        return self::TASKS[$request['action']] ?? 'Backup task';
    }

    public static function taskProgress(string $action, string $status): string
    {
        $label = match ($action) { 'verify' => 'Verification', 'deliver' => 'Copy to S3', 'retrieve' => 'S3 retrieval', 'delete' => 'Deletion', default => self::TASKS[$action] ?? 'Task' };
        return $label . match ($status) { 'pending' => ' queued.', 'deferred' => ' waiting to retry.', 'launching' => ' starting.', default => ' in progress.' };
    }

    /** [label, Filament color] for a task row. */
    public static function taskStatus(string $status): array
    {
        return match ($status) {
            'pending' => ['Waiting for scheduler', 'info'],
            'deferred' => ['Waiting to retry', 'warning'],
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
