<?php
namespace Paymenter\Extensions\Others\Backups;

/** Every administrator-facing sentence for engine codes, statuses, tasks and setup items. */
final class Messages
{
    private const ERRORS = [
        // Scheduling and admission
        'busy' => 'Another backup task was running. This one will try again next minute.',
        'config-changed' => 'Settings changed while Save was in progress. Refresh and enter your changes again.',
        'config-save-outcome-unknown' => 'A previous Save could not be confirmed. Check settings and enter replacement keys again to complete Save before backup work resumes.',
        'config-save-failed' => 'Setup could not finish. Refresh and check the saved settings.',
        'config-coordination-required' => 'The configuration writer could not prove that Save owns its locks. Run the ordinary Save command again.',
        'config-save-incomplete' => 'The configuration save did not finish. Refresh and check the saved settings and keys.',
        'config-write-outcome-unknown' => 'The setting write could not be confirmed. Check settings and complete Save before backup work resumes.',
        'billing-guard-stale' => 'Full backups need Paymenter\'s cron job to have run in the last few minutes, so they can avoid the daily billing run. Check the cron job on the Backups tab.',
        'billing-window-protected' => 'Waiting until Paymenter\'s daily billing run is over.',
        'schedule-overlaps-billing' => 'This time is too close to Paymenter\'s daily billing run. Pick a time outside the window shown under the time.',
        'schedule-field-invalid' => 'A schedule setting is invalid. Review the schedule settings and try again.',
        'schedule-kind-required' => 'Name the schedule: add --kind full or --kind database.',
        'scheduler-missing' => 'No cron job runs Paymenter\'s scheduler. Add the cron job shown on the Backups tab.',
        'job-lock-busy' => 'A Paymenter cron job was still running. The backup will try again next minute.',
        'job-locks-unverified' => 'A Paymenter cron job or queue worker has no lock file. The Backups tab shows which one and the line to use instead.',
        'job-uncoordinated' => 'A Paymenter cron job has no lock file. Replace it with the line shown on the Backups tab.',
        'job-unparseable' => 'Backups can\'t read one of Paymenter\'s cron jobs. Replace it with the line shown on the Backups tab.',
        'worker-continuous' => 'A queue worker runs all the time. Full backups need queue jobs to run from cron with a lock file.',
        'discovery-failed' => 'Backups couldn\'t read the crontab or systemd units for this site\'s user.',
        'discovery-stale' => 'The cron job check is out of date. It refreshes when Paymenter\'s cron job runs.',
        'continuous-worker-present' => 'A queue worker runs all the time. Full backups need queue jobs to run from cron with a lock file.',
        'job-command-unsupported' => 'Backups can\'t read one of Paymenter\'s cron jobs. Replace it with the line shown on the Backups tab.',
        'job-discovery-failed' => 'Backups couldn\'t read the crontab or systemd units for this site\'s user.',
        'job-discovery-incomplete' => 'Backups couldn\'t read every crontab entry or systemd unit for this site\'s user.',
        'job-discovery-stale' => 'The cron job check is out of date. It refreshes when Paymenter\'s cron job runs.',
        'job-discovery-unknown' => 'Cron jobs haven\'t been checked yet. The check runs from Paymenter\'s cron job.',
        'job-lock-unsafe' => 'A lock file is missing or unsafe. It must be a regular file owned by the site user, with permissions 600.',
        'job-lock-override-mismatch' => 'The lock files under Advanced don\'t match the ones your cron jobs use. Clear the field to use the detected ones.',
        'job-workers-running' => 'A Paymenter writer is still running after its lock files were acquired. Full backups wait until it stops.',
        'tool-capability-unsupported' => 'A binary is installed but lacks a feature backups need. Check its version under Advanced, Binary paths.',
        'cooperative-writers-acknowledged' => 'Confirm in Settings that no other program writes to Paymenter\'s database or files.',
        'external-state-acknowledged' => 'Confirm in Settings that no other program writes to Paymenter\'s database or files.',
        'full-host-assumptions-acknowledgement-required' => 'Confirm in Settings that no other program writes to Paymenter\'s database or files.',
        'no-concurrent-ddl-acknowledgement-required' => 'Confirm in Settings that updates and migrations don\'t run during backups.',
        'recovery-required' => 'A full backup didn\'t finish cleanly. Run paymenter_backups recover to see what to fix (Help, Recovery).',
        'installation-not-initialized' => 'Save settings once to finish setup.',
        'initial-maintenance-scheduled-skip' => 'Skipped: the site was already in maintenance mode.',
        'worker-interrupted' => 'The task stopped unexpectedly. It will try again next minute.',
        'request-not-cancellable' => 'This task has already started and can\'t be cancelled.',
        'request-action-invalid' => 'This task type isn\'t supported.',
        'request-arguments-invalid' => 'This task request isn\'t valid.',
        'request-not-launchable' => 'This task is no longer waiting to start. Refresh the task list before trying again.',

        // Configuration
        'config-invalid' => 'A saved setting is invalid. Review the settings and try again.',
        'timezone-data-unavailable' => 'The time zone database is unavailable. Install the tzdata package.',
        'gzip-busybox' => 'gzip on this server is BusyBox; Backups needs GNU gzip',
        'storage-required' => 'Keep backups on this server, upload them to S3, or enable both under Storage.',
        'storage-untested' => 'Test storage with the current settings and recovery key before enabling automatic uploads.',
        'config-write-failed' => 'Paymenter couldn\'t save settings to its database. Check the database connection and try again.',
        'command-not-registered' => 'Paymenter did not load the Backups settings command. Check that CLI PHP can connect to Paymenter\'s database, then save again.',
        'tool-unavailable' => 'A required binary wasn\'t found. Fix it under Advanced, Binary paths, or leave the path empty to detect it.',
        'tool-path-invalid' => 'Enter a full path, for example /usr/bin/tar.',
        'plaintext-offsite-acknowledgement-required' => 'To upload unencrypted backups to S3, confirm that backups in S3 won\'t be encrypted.',
        'remote-configuration-required' => 'Enter a bucket name to upload backups to S3.',
        's3-tls-required' => 'The endpoint must start with https://.',
        's3-endpoint-invalid' => 'Enter the provider\'s https:// API URL, without a username or query.',
        's3-prefix-invalid' => 'The folder in bucket can\'t contain "//", ".", ".." or "\\".',
        'private-root-invalid' => 'The data directory must be a full path outside Paymenter and any public web folder, owned by the site user, with permissions 700. If a saved path no longer works, follow the terminal instructions to point Backups to its existing data directory.',
        'private-root-inside-app' => 'The data directory must be outside Paymenter\'s folder.',
        'private-root-inside-docroot' => 'The data directory is inside a public web folder. Choose a folder that no site serves.',
        'private-root-unsafe' => 'The data directory can\'t be a symlink. Enter its real path.',
        'private-root-unavailable' => 'The web server can\'t create or open the data directory. Check its owner and PHP\'s open_basedir.',
        'document-roots-invalid' => 'The saved list of other public web folders is damaged. In Backups settings, enter every other public web folder, one full path per line, and confirm the complete list. Backups stay blocked until the list is corrected.',
        'owner-mismatch' => 'The data directory and Paymenter must belong to the same user. Set the data directory\'s permissions to 700.',
        'private-state-permissions' => 'A file in the data directory has the wrong owner or permissions. As the site user, set the data directory to 700 and its files to 600.',
        'private-file-invalid' => 'A file in the data directory has the wrong owner or permissions. As the site user, set the data directory to 700 and its files to 600.',
        'private-write-failed' => 'The data directory isn\'t writable. Check its owner and free disk space.',
        'state-unreadable' => 'A file in the data directory couldn\'t be read. It may be damaged or incomplete.',
        'installation-mismatch' => 'This data directory belongs to another Paymenter installation. Use this site\'s original data directory.',
        'installation-missing' => 'Save settings once to finish setup.',
        'extension-uninstalled' => 'The Backups extension isn\'t installed.',
        'native-secret-row-refused' => 'A Backups setting in Paymenter\'s database looks like a secret. Ask your server administrator to remove that setting. Enter keys in Backups settings only.',
        'billing-settings-unreadable' => 'Paymenter\'s daily billing time isn\'t set. Save "Cron Job Time" in Paymenter\'s settings.',
        'database-connection-invalid' => 'Paymenter\'s active database connection could not be read.',
        'database-pdo-driver-unavailable' => 'The configured PHP CLI needs the PDO MySQL driver to read Paymenter\'s database.',
        'database-connection-failed' => 'The configured PHP CLI could not connect to Paymenter\'s database. Check the active database settings and network access.',
        'database-tls-unsupported' => 'Databases that require TLS aren\'t supported yet.',
        'database-driver-unsupported' => 'The active database driver must be MySQL or MariaDB.',
        'database-prefix-unsupported' => 'The database table prefix contains unsupported characters.',
        'application-unavailable' => 'Backups couldn\'t load Paymenter. Check that the site user can read Paymenter\'s folder.',
        'python-binary-invalid' => 'Python 3.10 or newer wasn\'t found at the configured path. Fix it under Advanced, Binary paths.',
        'php-binary-mismatch' => 'The configured PHP path isn\'t PHP CLI, or isn\'t the PHP version Paymenter runs on. Fix it under Advanced, Binary paths.',
        'engine-unavailable' => 'Backups couldn\'t start Python. Check the Python and PHP binaries under Advanced, Binary paths.',
        'bridge-failed' => 'Backups couldn\'t read Paymenter\'s .env or database.',
        'bridge-identity-mismatch' => 'Backups read a different Paymenter installation than expected.',
        'bridge-required' => 'This command needs the extension\'s configuration bridge. Use the command shown under Help, Command line.',
        'bridge-command-invalid' => 'The configuration bridge does not support this operation. Reinstall a matching extension ZIP.',
        'bridge-unavailable' => 'The configuration bridge could not finish. Check that Paymenter\'s configuration and database are readable by the site user.',
        'private-pipe-required' => 'Runtime configuration must travel through a private pipe. Use the extension page or its documented command.',
        'billing-event-unverified' => 'Backups could not verify Paymenter\'s daily billing schedule. Check the native cron job and billing time before running full backups.',
        'billing-guard-unavailable' => 'The cron job could not record Paymenter\'s billing schedule. Check that the site user can write the data directory.',
        'backup-tick-unavailable' => 'Paymenter\'s scheduler could not start Backups. Check the configured binaries and the data directory.',

        // Credentials
        'credentials-unreadable' => 'Saved credentials or recovery keys could not be read. Restore the matching APP_KEY or APP_PREVIOUS_KEYS and keep credentials.json. Replacing it can lose keys needed for earlier S3 backups.',
        'credentials-invalid' => 'Enter both access key ID and secret access key.',
        'credentials-save-failed' => 'The encrypted keys file couldn\'t be written. Check that the site user can write the data directory.',
        'maintenance-probe-credentials-invalid' => 'Enter both username and password. The username can\'t contain ":".',
        'maintenance-probe-origin-mismatch' => 'The saved site password is for a different App URL. Enter it again or remove it.',
        'maintenance-probe-https-required' => 'A site password needs an https:// App URL.',
        'maintenance-probe-http-saved' => 'App URL now uses http://, so the saved site password can\'t be sent. Full backups can\'t start. Remove the password or change App URL to https://.',
        'maintenance-probe-origin-invalid' => 'App URL isn\'t a valid web address. Fix it in Paymenter\'s settings.',
        's3-credentials-required' => 'Add S3 keys in Settings, Storage, to upload backups.',

        // Encryption
        'archive-encryption-tool-missing' => 'GnuPG (gpg) wasn\'t found. Install it, or set its path under Advanced, Binary paths.',
        'archive-encryption-tool-unusable' => 'GnuPG (gpg) or its gpg-agent and gpgconf helpers aren\'t usable here. Install the complete gnupg package and check the path under Advanced, Binary paths.',
        'archive-key-required' => 'Uploads to S3 need a recovery key. Save settings with "Encrypt backups in S3" on to create one.',
        'archive-key-save-required' => 'Save the recovery key outside this server and confirm it. Uploads to S3 start after that.',
        'archive-key-invalid' => 'The recovery key or its fingerprint is invalid. Passphrases need at least 20 characters, no control characters, and at most 1024 UTF-8 bytes.',
        'archive-key-unavailable' => 'The recovery key for this S3 backup isn\'t on this server. Restore it from the copy you saved.',
        'archive-key-mismatch' => 'This key doesn\'t match the key the backup was encrypted with.',
        'archive-key-change-busy' => 'A backup task is running. Try again in a minute.',
        'archive-key-pending-exists' => 'A new recovery key is already waiting for confirmation. Confirm or discard it first.',
        'archive-key-already-exists' => 'This key is already saved here.',
        'archive-key-limit' => 'This server already keeps 128 recovery keys, the most it can hold. Earlier backups still need them, so none was removed.',
        'archive-key-audit-failed' => 'The recovery-key action stopped because the audit log could not record it. Check that the data directory is writable.',
        'recovery-key-output-invalid' => 'Export the recovery key to a private folder outside Paymenter and any public web folder.',
        'recovery-key-output-exists' => 'A file with that name already exists. Choose a new name.',
        'recovery-key-export-failed' => 'The recovery key file couldn\'t be written.',
        'sensitive-command-refused' => 'This command can\'t handle recovery keys.',
        'command-invalid' => 'This command isn\'t supported.',
        'recovery-file-unsafe' => 'The recovery file isn\'t a regular file.',
        'recovery-file-changed' => 'The recovery file changed while it was read. Copy it again.',
        'recovery-object-mismatch' => 'The downloaded file doesn\'t match its recorded size or checksum. Download it again.',
        'recovery-unreadable' => 'The recovery file is unreadable or damaged.',
        'archive-encryption-failed' => 'Encrypting the backup failed. The copy on this server is kept. Upload again.',
        'archive-decryption-failed' => 'The S3 backup couldn\'t be decrypted with its recovery key. The key may be wrong or the copy damaged.',
        'archive-integrity-failed' => 'The S3 backup failed its integrity check after decryption. Don\'t restore from it.',
        'remote-format-unsupported' => 'This S3 backup uses a format this version doesn\'t read.',
        'remote-descriptor-invalid' => 'The S3 backup\'s description file is invalid or doesn\'t match the backup.',
        'delivery-ciphertext-unavailable' => 'The encrypted bytes for this unfinished upload changed or are missing. An operator must restore those exact bytes or resolve the unfinished upload. Retrying cannot recreate them.',
        'legacy-plaintext-delivery-blocked' => 'This unfinished upload was recorded as unencrypted and is blocked by the current encryption policy. An operator must resolve it, or explicitly authorize plaintext uploads. Retrying cannot change its format.',

        // Capture
        'maintenance-probe-failed' => 'Full backup stopped: App URL didn\'t show Paymenter\'s maintenance page (HTTP 503). Check App URL and any redirect, proxy or CDN in front of the site. If the site asks for a password, add it under Site password.',
        'maintenance-unsafe' => 'Full backups need Paymenter\'s standard maintenance mode, with no secret bypass URL or redirect. Run php artisan up, then try again.',
        'maintenance-driver-unsupported' => 'Full backups need file-based maintenance mode. Set APP_MAINTENANCE_DRIVER=file in .env.',
        'maintenance-changed' => 'Someone turned maintenance mode on or off during the backup. Try again.',
        'maintenance-ownership-uncertain' => 'The site may still be in maintenance mode. Run paymenter_backups recover (Help, Recovery).',
        'process-state-uncertain' => 'A backup process may still be running. Run paymenter_backups recover (Help, Recovery).',
        'external-durable-state-unsupported' => 'Paymenter keeps data outside its database and files, for example in Redis. Full backups need file or database storage. The Backups tab lists the .env changes.',
        'external-symlink-target' => 'Paymenter\'s folder has a symlink to a file outside the folder. Full backups can\'t include it.',
        'unsupported-db-objects' => 'The database has views, triggers, routines, events or non-InnoDB tables. These backups support InnoDB tables only.',
        'unsupported-database' => 'The active database connection has no database name.',
        'database-inventory-failed' => 'The database couldn\'t be inspected. Check the database user\'s permissions.',
        'database-changed-during-capture' => 'The database changed during the full backup. Something outside Paymenter\'s cron jobs wrote to it.',
        'capture-timeout' => 'The backup took longer than the backup time limit. Raise it under Advanced, Time limits.',
        'child-timeout' => 'The backup took longer than the backup time limit. Raise it under Advanced, Time limits.',
        'child-failed' => 'A backup step failed. Check database access and binary paths.',
        'child-output-limit' => 'Not enough space: the backup would go over the space limit or minimum free disk space.',
        'capture-failed' => 'The backup failed unexpectedly.',
        'capture-interrupted' => 'The backup stopped, for example because the server restarted.',
        'failed-work-cleanup-failed' => 'Temporary files from a failed backup could not be removed safely. Check their ownership and the private task record before trying again.',
        'recovery-failed' => 'Backups could not confirm that the site reopened safely. Run paymenter_backups recover and follow the recovery steps in Help.',
        'unresolved-run-not-found' => 'That backup has no unresolved recovery record. Run paymenter_backups recover to see the records that still need attention.',
        'dump-empty' => 'The database export was empty. Check the database name and user in Paymenter\'s .env.',
        'disk-reserve-exceeded' => 'Disk is below the minimum free space. Free space, or lower the minimum in Settings, Storage.',
        'local-byte-budget-exceeded' => 'Backup space is full. Delete old backups or raise the limit in Settings, Storage.',
        'destination-unsafe' => 'The backup directory must be owned by the site user, have permissions 700, and be outside Paymenter and any public web folder.',
        'destination-inside-docroot' => 'The backup directory is inside a public web folder. Choose a folder that no site serves.',
        'private-root-move-refused' => 'The data directory can\'t be changed after setup. It holds your backup history and saved keys.',

        // Archives
        'archive-checksum-mismatch' => 'The backup doesn\'t match its checksum. Don\'t restore from it.',
        'archive-payload-mismatch' => 'Files inside the backup don\'t match their checksums. Don\'t restore from it.',
        'archive-layout-invalid' => 'The backup contains unexpected files. Don\'t restore from it.',
        'archive-manifest-mismatch' => 'The backup\'s manifest doesn\'t match. Don\'t restore from it.',
        'artifact-manifest-mismatch' => 'The backup\'s manifest doesn\'t match. Don\'t restore from it.',
        'archive-unreadable' => 'The backup is damaged or incomplete. Don\'t restore from it.',
        'archive-changed-during-verification' => 'The backup changed while it was being checked. Verify it again.',
        'archive-changed-during-download' => 'The backup changed while it was being read. Verify it again.',
        'archive-changed-during-export' => 'The backup changed while it was being copied. Verify it again.',
        'archive-changed-during-delivery' => 'The backup changed while it was uploading to S3. Verify it again.',
        'artifact-changed' => 'The backup files changed while they were being read. Try again.',
        'artifact-missing' => 'The backup file is missing from the backup directory.',
        'artifact-unsafe' => 'The backup file has unexpected permissions or links, so it isn\'t used. Check the backup directory.',
        'artifact-location-invalid' => 'The backup file is outside this site\'s backup directory, so it isn\'t used.',
        'artifact-manifest-invalid' => 'The backup\'s manifest is damaged.',
        'artifact-identity-mismatch' => 'This backup belongs to a different installation.',
        'artifact-deleted' => 'This backup was deleted.',
        'verification-required' => 'Verify this backup before downloading.',
        'artifact-stream-timeout' => 'The download didn\'t start. Try again, or copy the backup with paymenter_backups export (Help, Command line).',
        'export-destination-unsafe' => 'Export to a new folder that doesn\'t exist yet, outside Paymenter and any public web folder.',
        'run-incomplete' => 'This backup didn\'t finish.',
        'run-not-found' => 'This backup doesn\'t exist.',
        'invalid-run-id' => 'This backup doesn\'t exist.',

        // Storage, uploads and deletion
        'installation-state-orphaned' => 'The data directory has files but no installation.json. Restore that file, or choose a new empty directory.',
        'legacy-local-source-damaged' => 'This older backup failed verification. Use another one.',
        'legacy-manifest-replaced' => 'This older backup\'s record changed. Verify it, then try again.',
        'retrieve-local-copy-changed' => 'The downloaded copy changed before it was ready, so it isn\'t offered. Try again.',
        'retrieve-intent-invalid' => 'An earlier download from S3 left a damaged record. Check the data directory.',
        'retention-pointer-update-failed' => 'Couldn\'t update the "latest backup" file in S3. Check the keys can write, then run cleanup again.',
        'remote-copy-missing' => 'The S3 copy is missing. A bucket lifecycle rule may have removed it. Use another backup.',
        'remote-copy-damaged' => 'The S3 copy doesn\'t match its backup record. Use another backup.',
        'remote-copy-unknown' => 'The S3 copy hasn\'t been checked yet. Verify it.',
        'remote-copy-error' => 'The S3 copy couldn\'t be checked. Check the S3 settings and keys, then try again.',
        'remote-copy-unavailable' => 'No completed S3 copy is recorded for this backup. Check its upload status before downloading from S3.',
        'remote-check-failed' => 'The S3 check couldn\'t finish. Check S3 access and try again.',
        'remote-archive-size-mismatch' => 'The S3 copy has the wrong size, so it isn\'t offered for download.',
        'remote-archive-checksum-mismatch' => 'The S3 copy failed its checksum check, so it isn\'t offered for download.',
        'remote-scope-changed' => 'This copy is in an old S3 bucket or folder. Switch back to those S3 settings to use it.',
        'remote-location-unbound' => 'This older S3 copy hasn\'t been checked against your current S3 settings. Verify it.',
        'delivery-destination-changed' => 'S3 settings changed during an upload. Switch back to the old settings to finish it, or upload again.',
        'delete-no-verified-local-survivor' => 'No other verified backup on this server. Create or verify one first.',
        'delete-no-verified-s3-survivor' => 'No other verified backup in S3. Upload one first.',
        'delete-last-s3-backup' => 'This is the newest backup in S3. Upload a newer one first.',
        'delete-remote-location-unbound' => 'This S3 copy can\'t be deleted here because it hasn\'t been checked. You can still delete the copy on this server.',
        'delete-location-invalid' => 'Choose where to delete from: this server, S3, or both.',
        'delete-location-changed' => 'An earlier deletion of this backup targeted another place. Choose that same place to finish it.',
        'retrieve-local-copy-exists' => 'A file with this backup\'s name already exists on this server. Check the backup directory.',
        'retrieve-spool-changed' => 'An interrupted download from S3 changed on disk. Check the data directory, then try again.',
        'multipart-location-unbound' => 'An old unfinished upload is still in S3. Add a bucket lifecycle rule that deletes incomplete uploads.',
        'multipart-destination-mismatch' => 'An unfinished upload is in an old S3 bucket or folder. Add a bucket lifecycle rule that deletes incomplete uploads.',
        'schedule-superseded' => 'Replaced by a newer scheduled backup, or the schedule changed.',
        'remote-object-too-large' => 'A backup record in S3 is larger than allowed, so it isn\'t used.',
        'retention-remote-scope-changed' => 'Cleanup kept an S3 copy that\'s outside the current bucket or folder.',
        'retention-skipped-damaged' => 'Cleanup kept a damaged backup. Delete it yourself once another backup is verified.',
        'retention-last-good-local-copy' => 'Cleanup kept this verified copy to preserve the required backups on this server.',
        'retention-last-good-s3-copy' => 'Cleanup kept this usable S3 copy as a protected backup.',
        'retention-mode-unknown' => 'Cleanup could not confirm whether this is a full or database backup, so it was kept. Check its backup record and manifest.',
        'delete-mode-unknown' => 'Deletion could not confirm whether this is a full or database backup. Check its backup record and manifest before deleting it.',
        'staging-removal-conflict' => 'Another deletion of this backup is unfinished. Resolve it before removing the temporary copy on this server.',
        'staging-removal-remote-proof-required' => 'The temporary local copy was kept because Backups could not prove its completed S3 copy in the current bucket and folder.',
        'staging-removal-ownership-mismatch' => 'The temporary local files have unexpected ownership, paths or links, so they were kept.',
        'staging-removal-local-verification-failed' => 'The temporary local files changed or failed verification, so they were kept. Verify the backup before resolving the cleanup.',
        'staging-removal-local-delete-failed' => 'The S3 upload completed, but its temporary local files could not be removed. Check the backup directory and retry the upload task to finish cleanup.',
        's3-client-failed' => 'The S3 client couldn\'t start. Check the region and endpoint.',
        'isolated-s3-sdk-missing' => 'The bundled S3 library is missing. Reinstall the extension ZIP.',
        's3-readback-mismatch' => 'The S3 copy didn\'t match after upload. The copy on this server is kept. Upload again.',
        's3-manifest-readback-mismatch' => 'The S3 copy didn\'t match after upload. The copy on this server is kept. Upload again.',
        's3-pointer-readback-mismatch' => 'The S3 latest-backup record didn\'t match after upload. The copy on this server is kept. Check that record in S3 before uploading again.',
        's3-pointer-invalid' => 'The S3 latest-backup record doesn\'t match this installation\'s backup history. The copy on this server is kept. Check the S3 destination and its latest-backup record before uploading again.',
        's3-delivery-failed' => 'Upload to S3 failed. The copy on this server is kept. Check S3 settings and keys, then upload again.',
        'destination-probe-failed' => 'The storage test couldn\'t write and read back a test file.',
        'destination-readback-mismatch' => 'The storage test read back different data than it wrote.',
        'destination-test-failed' => 'Couldn\'t write to the bucket. Check endpoint, region, bucket name and key permissions.',
        'destination-probe-cleanup-failed' => 'The test file couldn\'t be deleted from S3. The keys need delete permission.',
        'delete-last-backup' => 'This is your only usable backup. Create another first.',
        'delete-latest-remote-copy' => 'This is the newest backup in S3. Upload a newer one first.',
        'delete-no-verified-survivor' => 'No other verified backup is left in that place, so nothing was deleted.',
        'delete-conflict' => 'Another task is using this backup. Wait for it to finish.',
        'delete-confirmation-required' => 'Confirm the deletion first.',
        'delete-remote-scope-changed' => 'This S3 copy is in an old bucket or folder. Switch back to those S3 settings or delete it at your provider. You can still delete the copy on this server.',
        'delete-ownership-mismatch' => 'These files aren\'t in this site\'s backup directory or S3 folder, so they were kept.',
        'delete-remote-verification-failed' => 'The S3 copy is missing or didn\'t match the backup, so it was kept.',
        'delete-remote-delete-failed' => 'Deleting the S3 copy failed. Check the keys can delete, then try again.',
        'delete-local-delete-failed' => 'Deleting the files on this server failed. Try again.',
        'delete-local-verification-failed' => 'The files on this server changed after they were checked, so they were kept.',
        'retention-ownership-mismatch' => 'Cleanup found files outside this site\'s backup directory or S3 folder and kept them.',
        'retention-remote-verification-failed' => 'Cleanup couldn\'t verify an S3 copy and kept it. Check that copy, then run cleanup again.',
        'retention-remote-delete-failed' => 'Cleanup couldn\'t delete an S3 copy. Check the keys can delete, then run cleanup again.',
        'retention-local-delete-failed' => 'Cleanup couldn\'t delete a backup on this server. Check the backup directory, then run cleanup again.',
        'retention-local-verification-failed' => 'Cleanup found backup files that changed after the backup and kept them.',

        // Terminal request validation and bounded operations
        'json-stdin-required' => 'Pass the requested JSON through standard input, using the command shown under Help.',
        'json-stdin-invalid' => 'The standard input is not a valid JSON object. Check the command input before trying again.',
        'input-too-large' => 'The command input exceeds its allowed size. Submit only the required settings or credentials.',
        'run-id-required' => 'Name the backup to use. The backup list shows its ID.',
        'request-id-required' => 'Name the task to use. The task list shows its ID.',
        'execute-required' => 'This command needs --execute to perform the requested operation.',
        'artifact-stream-pipe-required' => 'Downloads must use a private stream. Use Download on the page or the documented export command.',
        'operation-interrupted' => 'The command was interrupted. Check its task record before starting it again.',
        'operation-failed' => 'The command could not finish. Check its private task record and the configured binaries before trying again.',
    ];

    public static function error(?string $code): string
    {
        if ($code === null || $code === '') { return ''; }
        return self::ERRORS[$code] ?? 'The task failed with an unrecognized error. Check its private task record from the command line.';
    }

    public static function known(string $code): bool { return isset(self::ERRORS[$code]); }

    /** Append a bounded writer cause to administrator copy when one is available. */
    public static function withWriterCause(string $text, ?string $cause): string
    {
        return $cause === null ? $text : $text . ' Writer cause: ' . $cause . '.';
    }

    /** Format bounded data-directory details for Save and readiness messages. */
    public static function privateRootProblem(array $problem): string
    {
        $code = $problem['code'] ?? null;
        $params = is_array($problem['params'] ?? null) ? $problem['params'] : [];
        $path = self::displayPath($params['path'] ?? null);
        if ($path === '') { return self::error(is_string($code) ? $code : null); }
        if ($code === 'private-root-invalid') {
            return match ($params['reason'] ?? null) {
                'owner' => 'Data directory ' . $path . ' isn\'t owned by the web user. Change its owner to the web user and set mode 0700. Backups keeps this folder selected until it can check for an installation.',
                'mode' => 'Data directory ' . $path . ' must have mode 0700. Set its mode to 0700. Backups keeps this folder selected until it can check for an installation.',
                default => self::error($code),
            };
        }
        if ($code === 'private-root-unavailable') {
            $parent = self::displayPath($params['parent'] ?? null);
            if ($parent === '') { return self::error($code); }
            if (($params['parent_is_folder'] ?? null) === false) {
                return 'Data directory ' . $path . ' can\'t be created: ' . $parent . ' isn\'t a folder. Choose a writable folder outside Paymenter.';
            }
            if (($params['parent_is_folder'] ?? null) === true) {
                return 'Data directory ' . $path . ' can\'t be created: the web user can\'t write to ' . $parent . '. Create it for the web user with mode 0700, or choose another folder outside Paymenter.';
            }
        }
        return self::error(is_string($code) ? $code : null);
    }

    private static function displayPath(mixed $path): string
    {
        if (!is_string($path) || $path === '') { return ''; }
        $path = preg_replace('/[\x00-\x1f\x7f]/', '?', $path) ?? '';
        return strlen($path) > 400 ? substr($path, 0, 397) . '...' : $path;
    }

    /** Waiting copy and its escaped setup link for backup history. */
    public static function schedulerWaitLine(string $url): \Illuminate\Support\HtmlString
    {
        $url = htmlspecialchars($url, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
        return new \Illuminate\Support\HtmlString(
            'Waiting for Paymenter\'s scheduled tasks. Set them up on <a class="underline" href="' . $url . '">the Backups tab</a>.'
        );
    }

    private static function shellWord(string $word): string
    {
        return preg_match('~^[A-Za-z0-9_@%+=:,./-]+$~D', $word) ? $word : escapeshellarg($word);
    }

    /** Settings field that an engine configuration refusal points at, or null. */
    public static function field(string $code): ?string
    {
        return [
            'schedule-overlaps-billing' => 'full_time', 's3-tls-required' => 's3_endpoint', 's3-endpoint-invalid' => 's3_endpoint',
            'timezone-data-unavailable' => 'schedule_timezone', 'gzip-busybox' => 'gzip',
            's3-prefix-invalid' => 's3_prefix', 'plaintext-offsite-acknowledgement-required' => 'plaintext_offsite_acknowledged',
            'remote-configuration-required' => 's3_bucket',
            'private-root-invalid' => 'private_root', 'private-root-inside-docroot' => 'private_root',
            'private-root-inside-app' => 'private_root', 'private-root-unsafe' => 'private_root', 'owner-mismatch' => 'private_root',
            'installation-mismatch' => 'private_root', 'document-roots-invalid' => 'extra_docroots',
            'python-binary-invalid' => 'python', 'php-binary-mismatch' => 'php', 'destination-unsafe' => 'local_path',
            'destination-inside-docroot' => 'local_path', 'private-root-move-refused' => 'private_root',
        ][$code] ?? null;
    }

    public const MODES = [
        'full-quiesced' => 'Full backup',
        'database-online' => 'Database backup',
    ];

    /** Short type names for the history table. */
    public const TYPES = [
        'full-quiesced' => 'Full',
        'database-online' => 'Database',
    ];

    public static function mode(?string $mode): string { return self::MODES[$mode] ?? 'Backup'; }

    public static function type(?string $mode): string { return self::TYPES[$mode] ?? 'Backup'; }

    /** [label, Filament color] for a backup row. */
    public static function runStatus(array $run): array
    {
        if (!$run['local'] && $run['remote'] === 'missing') { return ['S3 copy missing', 'warning']; }
        return match ($run['status']) {
            'ready' => match ($run['download']) {
                'ready' => ['Verified', 'success'],
                'retrieve' => ['In S3', 'info'],
                'verify' => ['Not verified', 'warning'],
                default => match ($run['remote']) {
                    'missing' => ['S3 copy missing', 'warning'], 'damaged' => ['S3 copy damaged', 'danger'],
                    'scope-changed' => ['Old S3 folder', 'warning'], 'unbound' => ['S3 copy unchecked', 'warning'],
                    default => ['Unavailable', 'danger'],
                },
            },
            'damaged' => ['Damaged', 'danger'],
            'running' => ['Running', 'info'],
            'failed' => ['Failed', 'danger'],
            'attention' => ['Needs recovery', 'warning'],
            'interrupted' => ['Interrupted', 'danger'],
            'deleting' => ['Deleting', 'info'],
            'delete-interrupted' => ['Deletion incomplete', 'warning'],
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
                'retrieve' => 'Only in S3. Use Download from S3.',
                'verify' => 'Verify before downloading.',
                default => self::error($run['download']),
            },
            'damaged' => self::error($run['verification_error']),
            'running' => $run['mode'] === 'database-online' ? 'Copying the database. The site stays online.'
                : (in_array($run['phase'] ?? '', ['entering-maintenance', 'draining', 'capturing'], true) ? 'Copying. The site is in maintenance mode.' : 'Compressing. The site is back online.'),
            'failed' => self::error($run['error'] ?: 'capture-failed'),
            'attention' => (($run['maintenance'] ?? false) ? 'Site in maintenance mode. ' : 'Site online. ') . self::error($run['recovery_error'] ?: 'maintenance-ownership-uncertain'),
            'interrupted' => 'The backup stopped before it finished.',
            'deleting' => 'Deletion queued or running.',
            'delete-interrupted' => 'Some files may remain. Delete it again to finish.',
            'deleted' => $run['deletion_reason'] === 'manual' ? 'Deleted manually.' : 'Deleted by cleanup.',
            default => null,
        };
    }

    /** Where a backup's copies are, in a few words. */
    public static function location(array $run, bool $uploadsToS3): string
    {
        if (!$run['local']) {
            return $run['status'] === 'deleted' ? '—' : match ($run['remote']) {
                'delivered', 'present' => 'S3', 'missing' => 'S3 copy missing', 'damaged' => 'S3 copy damaged',
                'scope-changed' => 'Old S3 folder', 'unbound' => 'S3 (unchecked)', default => '—',
            };
        }
        return match ($run['remote']) {
            'delivered', 'present' => 'This server, S3',
            'failed' => 'This server (S3 upload failed)',
            'missing' => 'This server (S3 copy missing)', 'damaged' => 'This server (S3 copy damaged)',
            'scope-changed' => 'This server, old S3 folder', 'unbound' => 'This server, S3 (unchecked)',
            default => $uploadsToS3 ? 'This server (not uploaded yet)' : 'This server',
        };
    }

    /** Label for how an S3 copy is stored, or null when there is none. */
    public static function remoteFormat(?string $format, ?string $keyShort = null): ?string
    {
        return match ($format) {
            'openpgp-symmetric-aes256-seipd-v1' => 'Encrypted' . ($keyShort ? ' with recovery key ' . $keyShort : ''),
            'plaintext-v1' => 'Not encrypted',
            null => null,
            default => 'Unknown format',
        };
    }

    public const TASKS = [
        'run' => 'Create backup', 'verify' => 'Verify', 'deliver' => 'Upload to S3',
        'retrieve' => 'Download from S3', 'retention' => 'Delete old backups', 'destination-test' => 'Test storage', 'delete' => 'Delete backup',
    ];

    public static function task(array $request): string
    {
        if ($request['action'] === 'run') {
            return self::mode($request['mode'] ?? null) . ', ' . (($request['scheduled'] ?? false) ? 'scheduled' : 'manual');
        }
        return self::TASKS[$request['action']] ?? 'Backup task';
    }

    public static function taskProgress(string $action, string $status): string
    {
        $label = match ($action) { 'verify' => 'Verification', 'deliver' => 'Upload to S3', 'retrieve' => 'Download from S3', 'delete' => 'Deletion', 'retention' => 'Cleanup', default => self::TASKS[$action] ?? 'Task' };
        return $label . match ($status) { 'pending' => ' queued.', 'deferred' => ' retrying soon.', 'launching' => ' starting.', default => ' in progress.' };
    }

    /** [label, Filament color] for a task row. */
    public static function taskStatus(string $status): array
    {
        return match ($status) {
            'pending' => ['Queued', 'info'],
            'deferred' => ['Retrying soon', 'warning'],
            'launching' => ['Starting', 'info'],
            'running' => ['Running', 'info'],
            'complete' => ['Done', 'success'],
            'failed' => ['Failed', 'danger'],
            'cancelled' => ['Cancelled', 'gray'],
            'expired' => ['Expired', 'gray'],
            'skipped' => ['Skipped', 'gray'],
            'satisfied' => ['Covered', 'gray'],
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

    /** Names for binaries in Advanced, Binary paths. */
    public const TOOLS = [
        'python' => 'Python 3', 'php' => 'PHP', 'dump' => 'mysqldump or mariadb-dump', 'mysql' => 'mysql or mariadb client',
        'tar' => 'tar', 'gzip' => 'gzip', 'gpg' => 'GnuPG (gpg)',
    ];

    /** Names for the storage drivers that full backups check. */
    public const STORES = [
        'maintenance' => 'Maintenance mode', 'cache' => 'Cache', 'session' => 'Sessions', 'queue' => 'Queue', 'filesystem' => 'File storage',
    ];

    /**
     * A setup item from engine readiness, as words and fixes. Context carries
     * only display data the page already holds: suggestions, detected jobs and
     * stores, the crontab user and the last cron check-in.
     *
     * @return array{title: string, text: string, fixes: list<array{lead: ?string, code: string}>, action: ?array{kind: string, label: string, target: ?string}, tone: string}
     */
    public static function readinessItem(string $code, array $detail, array $context = []): array
    {
        $field = is_string($context['field'] ?? null) ? $context['field'] : null;
        $suggest = $context['suggestions'] ?? [];
        $user = $context['crontab_user'] ?? null;
        $cronLead = 'Add this line to the crontab of ' . ($user ?: 'the site user') . ' with crontab -e. If a schedule:run line is already there, replace it.';
        $scheduler = filled($suggest['scheduler'] ?? null) ? [['lead' => $cronLead, 'code' => $suggest['scheduler']]] : [];
        $settings = static fn (string $label, string $target) => ['kind' => 'settings', 'label' => $label, 'target' => $target];
        $item = static fn (string $title, string $text, array $fixes = [], ?array $action = null, string $tone = 'warning') => compact('title', 'text', 'fixes', 'action', 'tone');
        // Engine names for the same conditions.
        $code = ['continuous-worker-present' => 'worker-continuous', 'job-command-unsupported' => 'job-unparseable', 'job-discovery-failed' => 'discovery-failed',
            'job-discovery-incomplete' => 'discovery-failed', 'job-discovery-stale' => 'discovery-stale'][$code] ?? $code;
        if (in_array($code, ['private-root-invalid', 'private-root-unavailable'], true)
            && is_string($context['private_root_problem'] ?? null)) {
            return $item('Fix the Data directory', $context['private_root_problem'], [], $settings('Go to Data directory', 'first-time-setup'));
        }
        $job = is_string($detail['job'] ?? null) ? ($context['jobs'][$detail['job']] ?? null) : null;
        if ($job === null && $code === 'worker-continuous') {
            foreach ($context['jobs'] ?? [] as $candidate) { if (($candidate['coordination'] ?? null) === 'continuous') { $job = $candidate; break; } }
        }
        $jobName = $job ? ($job['command'] ?: $job['label']) : null;
        $queueFix = filled($suggest['queue'] ?? null) ? [['lead' => 'Run queue jobs from cron instead, with their own lock file:', 'code' => $suggest['queue']]] : [];

        switch ($code) {
            case 'installation-not-initialized':
            case 'installation-missing':
                return $item('Save your settings', 'This creates the private data directory for backup history and saved keys.', [], $settings('Go to Settings', 'settings'), 'info');
            case 'scheduler-missing':
            case 'billing-guard-stale':
                $last = $context['last_tick'] ?? null;
                if ($context['cron_fresh'] ?? false) {
                    return $item('Find Paymenter\'s cron job', 'Paymenter\'s scheduler runs, but not from a cron job Backups can read for ' . ($user ?: 'the site user') . '. Full backups need to pause it with a lock file.', $scheduler);
                }
                return $last
                    ? $item('Paymenter\'s cron job isn\'t running', 'It last ran ' . $last . '. Backups, uploads and checks wait until it runs again, once a minute.', $scheduler)
                    : $item('Add Paymenter\'s cron job', 'Backups, uploads and checks run from it, once a minute. It hasn\'t run yet.', $scheduler);
            case 'storage-untested':
                return $item('Test your storage', ($context['cron_fresh'] ?? false)
                    ? 'Writes, reads and deletes a small test file where backups are stored.'
                    : 'Writes, reads and deletes a small test file where backups are stored. It runs once Paymenter\'s cron job runs.', [], ['kind' => 'test-storage', 'label' => 'Test storage', 'target' => null], 'info');
            case 'job-uncoordinated':
                $isQueue = ($job['kind'] ?? null) === 'queue-worker';
                $fix = $isQueue ? $queueFix : $scheduler;
                if ($fix) { $fix[0]['lead'] = 'Replace it with:'; }
                return $item('Add a lock file to this ' . ($isQueue ? 'queue job' : 'cron job'), 'Full backups use the lock file to pause it while the database and files are copied.',
                    [...($jobName ? [['lead' => 'Now:', 'code' => $jobName]] : []), ...$fix]);
            case 'job-unparseable':
                $fix = (($job['kind'] ?? null) === 'queue-worker') ? $queueFix : $scheduler;
                if ($fix) { $fix[0]['lead'] = 'Backups can\'t read this line. Replace it with:'; }
                return $item('Backups can\'t read a cron job', 'Full backups need to know which lock file each Paymenter job uses.',
                    [...($jobName ? [['lead' => 'Now:', 'code' => $jobName]] : []), ...$fix]);
            case 'worker-continuous':
                $unit = ($job['source'] ?? null) === 'systemd' ? ($job['label'] ?? null) : null;
                $fixes = [];
                if ($unit) { $fixes[] = ['lead' => 'Stop and disable the service:', 'code' => 'systemctl disable --now ' . $unit]; }
                elseif ($jobName) { $fixes[] = ['lead' => 'Now:', 'code' => $jobName]; }
                return $item('A queue worker runs all the time' . ($unit ? ': ' . $unit : ''), 'Full backups can\'t pause a worker that never stops. Database backups aren\'t affected. Queued emails may wait up to a minute after this change.', [...$fixes, ...$queueFix]);
            case 'job-locks-unverified':
                return $item('Add lock files to Paymenter\'s cron jobs', 'Full backups use lock files to pause cron jobs and queue workers while the database and files are copied.', $scheduler);
            case 'discovery-failed':
                return $item('Backups couldn\'t read the crontab', 'Full backups check every Paymenter cron job and worker for ' . ($user ?: 'the site user') . ' first. Check that the site user can run crontab -l.');
            case 'job-discovery-unknown':
                return $item('Cron jobs haven\'t been checked yet', 'The check runs from Paymenter\'s cron job every few minutes. Full backups wait for it.', [], null, 'info');
            case 'job-lock-unsafe':
                $lock = is_string($detail['lock'] ?? null) ? $detail['lock'] : null;
                return $item('Fix a lock file' . ($lock ? ': ' . $lock : ''), 'It must be a regular file owned by the site user, with permissions 600. Create it with:',
                    $lock ? [['lead' => null, 'code' => 'touch ' . self::shellWord($lock) . ' && chmod 600 ' . self::shellWord($lock)]] : []);
            case 'job-lock-override-mismatch':
                return $item('Lock files override doesn\'t match', 'The lock files under Advanced must be exactly the ones your cron jobs use. Clear the field to use the detected ones.', [], $settings('Go to Advanced', 'advanced'));
            case 'tool-capability-unsupported':
                return $item('A binary lacks a needed feature', self::error($code), [], $settings('Go to Binary paths', 'advanced'));
            case 'discovery-stale':
                return $item('The cron job check is out of date', 'It refreshes every few minutes while Paymenter\'s cron job runs.', $scheduler, null, 'info');
            case 'external-durable-state-unsupported':
                $stores = array_values(array_filter((array) ($detail['stores'] ?? ($context['unsupported_stores'] ?? [])), 'is_array'));
                $drivers = array_values(array_unique(array_filter(array_map(static fn ($store) => is_string($store['driver'] ?? null) ? $store['driver'] : null, $stores))));
                $named = $drivers === ['redis'] ? 'Redis' : ($drivers ? implode(', ', $drivers) : 'an outside service');
                $lines = [];
                foreach ($stores as $store) {
                    if (is_string($store['env'] ?? null) && is_string($store['suggest'] ?? null)) { $lines[] = $store['env'] . '=' . $store['suggest']; }
                }
                $fixes = array_map(static fn ($line, $index) => ['lead' => $index === 0 ? 'Change these in ' . ($context['env_path'] ?? '.env') . ', then run php artisan config:cache if you cache config. Users are logged out once.' : null, 'code' => $line], $lines, array_keys($lines));
                return $item('Paymenter stores data in ' . $named, 'Full backups can\'t copy data held outside Paymenter\'s database and files.', $fixes);
            case 'maintenance-driver-unsupported':
                return $item('Use file-based maintenance mode', 'Full backups show Paymenter\'s maintenance page and need it stored as a file.', [['lead' => 'Set this in ' . ($context['env_path'] ?? '.env') . ':', 'code' => 'APP_MAINTENANCE_DRIVER=file']]);
            case 'cooperative-writers-acknowledged':
            case 'external-state-acknowledged':
            case 'full-host-assumptions-acknowledgement-required':
                return $item('Confirm nothing else writes to Paymenter', 'Cron jobs and workers are paused during a full backup. Other programs aren\'t.', [], $settings('Confirm in Settings', 'full-backup-requirements'));
            case 'no-concurrent-ddl-acknowledgement-required':
                return $item('Confirm updates don\'t run during backups', 'A Paymenter update or migration during a backup can leave the copy unusable.', [], $settings('Confirm in Settings', 'before-backups-run'));
            case 's3-credentials-required':
                return $item('Add your S3 access keys', 'Backups can\'t upload to S3 without keys.', [], $settings('Go to Storage settings', 'storage'));
            case 'remote-configuration-required':
                return $item('Enter your S3 bucket', 'Backups can\'t upload to S3 without a bucket name.', [], $settings('Go to Storage settings', 'storage'));
            case 'plaintext-offsite-acknowledgement-required':
                return $item('Confirm unencrypted uploads', 'Encryption is off, so backups in S3 won\'t be encrypted. Confirm this, or turn encryption on.', [], $settings('Go to Storage settings', 'storage'));
            case 'archive-key-required':
                return $item('Create a recovery key', 'Backups in S3 are encrypted with a recovery key. Save settings with "Encrypt backups in S3" on to create one.', [], $settings('Go to Storage settings', 'storage'));
            case 'archive-key-save-required':
                return $item('Save your recovery key', 'Uploads to S3 start after you save it outside this server and confirm. Without it, encrypted backups can\'t be restored if this server is lost.', [], ['kind' => 'recovery-key', 'label' => '', 'target' => null]);
            case 'archive-encryption-tool-missing':
            case 'archive-encryption-tool-unusable':
                $path = $detail['path'] ?? ($context['tools']['gpg']['resolved'] ?? null) ?? ($context['tools']['gpg']['configured'] ?? null);
                return $item('Install GnuPG', 'Backups use gpg with gpg-agent and gpgconf to encrypt uploads to S3' . (is_string($path) && $path !== '' ? '. It wasn\'t usable at ' . $path : ', and it wasn\'t found') . '. Install the complete gnupg package, or set its path under Binary paths.', [], $settings('Go to Binary paths', 'advanced'));
            case 'tool-unavailable':
                $tool = is_string($detail['tool'] ?? null) ? $detail['tool'] : null;
                $name = self::TOOLS[$tool] ?? 'A required binary';
                $path = is_string($detail['path'] ?? null) && $detail['path'] !== '' ? $detail['path'] : null;
                return $item($name . ' not found', ($path ? 'Nothing usable at ' . $path . '. ' : '') . 'Fix the path under Binary paths, or leave it empty to detect it.', [], $settings('Go to Binary paths', 'advanced'));
            case 'gzip-busybox':
                return $item('Install GNU gzip', self::error($code), [], $settings('Go to Binary paths', 'advanced'));
            case 'timezone-data-unavailable':
                return $item('Install time zone data', self::error($code), [], $settings('Go to Settings', 'settings'));
            case 'billing-settings-unreadable':
                return $item('Set Paymenter\'s billing time', 'Full backups avoid Paymenter\'s daily billing run and need its time. Save "Cron Job Time" in Paymenter\'s settings.');
            case 'recovery-required':
            case 'maintenance-ownership-uncertain':
            case 'process-state-uncertain':
                return $item('A full backup didn\'t finish cleanly', 'The site may still be in maintenance mode. As the site user, see what needs fixing (this changes nothing):',
                    [['lead' => null, 'code' => 'paymenter_backups recover']], ['kind' => 'help', 'label' => 'Recovery steps', 'target' => 'command-line'], 'danger');
            case 'maintenance-probe-failed':
                return $item('Add your site password', 'The last full backup couldn\'t see the maintenance page. If your web server asks for a username and password, full backups need them.', [], $settings('Go to Settings', 'site-password'));
            case 'maintenance-probe-http-saved':
            case 'maintenance-probe-origin-mismatch':
            case 'maintenance-probe-origin-invalid':
            case 'maintenance-probe-https-required':
                return $item('Check the site password', self::error($code), [], $settings('Go to Settings', 'site-password'));
            case 'billing-window-protected':
                return $item('Waiting for the billing run', self::error($code), [], null, 'info');
            case 'job-lock-busy':
            case 'busy':
                return $item('Waiting for a running task', self::error($code), [], null, 'info');
            case 'config-invalid':
            case 'schedule-field-invalid':
                return $item('Check your settings', self::error($code), [], $settings('Go to Settings', 'settings'));
        }
        return $item(self::known($code) ? 'Needs attention' : 'Backups can\'t start', self::error($code), [], $field ? $settings('Go to Settings', 'settings') : null);
    }
}
