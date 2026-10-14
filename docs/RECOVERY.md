# Recovery and restore

These archives contain private customer data, `.env` with DB credentials and
APP_KEY, and Passport signing keys. Keep artifacts owner-private. S3 credential
files live outside the app and are excluded from every archive and recovery kit.
APP_KEY encryption protects a separately leaked credential file; a compromised
application UID can read APP_KEY and has access to the credentials.

The engine records capture, service recovery, local completion and remote
delivery independently. A delivery error does not imply the site is down.
Use the independent CLI first:

```sh
python3 -I /path/to/Backups/runtime/cli.py --app /srv/paymenter \
  --private-root /srv/paymenter-backups-private status
python3 -I /path/to/Backups/runtime/cli.py --app /srv/paymenter \
  --private-root /srv/paymenter-backups-private recover
python3 -I /path/to/Backups/runtime/cli.py --app /srv/paymenter \
  --private-root /srv/paymenter-backups-private recover --execute
```

Recovery takes the same private run lock as capture and never relies on PID
alone. It checks recorded boot ID, process group emptiness and every maintenance
file's exact device, inode, size, mtime, ctime and SHA256. It removes only this
run's unchanged markers, down first and early-bootstrap stub second. Normal
completion uses this identical path. Pre-existing markers remain. Matching
bytes on a new inode, an operator rewrite, uncertain process state or a missing
ownership record refuses reopening. There remains a small check/unlink race;
owner-private cooperative administration is a required assumption.

SIGINT/SIGTERM invokes bounded cleanup; Linux subprocesses have parent-death
SIGKILL with a parent recheck. A host SIGKILL can leave descendants or uncertain
ownership; recovery stays closed until verified. The native minute tick attempts
only guarded auto-recovery and skips capture while maintenance persists. It is
not an always-on watchdog; hosting/cron/app failures need an operator.

If recovery refuses, inspect the safe journal and actual processes locally.
Keep private recovery logs outside public documentation. Resolve the site
state deliberately; do not unlink files solely on matching bytes or blindly run
`artisan up`. Once the operator has resolved an uncertain checkpoint:

```sh
python3 -I /path/to/Backups/runtime/cli.py --app /srv/paymenter \
  --private-root /srv/paymenter-backups-private recover acknowledge --run RUN-ID
```

Acknowledgement records operator UID/time, touches no app files and unblocks
future admission for that journal. It does not claim artifact completion.
Failed payloads and journals remain for investigation and quota accounting.

Full restore into an isolated new host/path:

1. Stop outgoing provider operations, cron, queue workers and webhook delivery.
   Preserve the destination's pre-restore files and DB. Do not overwrite the
   only working copy. Confirm this is a full-quiesced artifact; database-online
   artifacts contain only SQL and cannot supply an atomic app/files restore.
2. Run `verify RUN-ID --show` against the original private state, or independently
   validate the recovery kit's archive SHA256 and size from manifest.json.
   An archive downloaded from the admin page arrives only when its streamed
   bytes matched the recorded SHA-256; an interrupted download is incomplete
   and must not be used. Check its size and SHA-256 against the backup's
   manifest before restoring.
   Verification reads all gzip bytes including CRC/trailer and every payload
   hash. Fetch remote manifest and full archive; do not use ETag as SHA256.
3. Extract outer tar.gz into a private scratch directory. It contains
   `database.sql`, `app.tar` for full mode, and embedded `manifest.json`. Extract
   app.tar into a new application path with GNU tar ACL/xattr/numeric-owner
   support. Preserve symlinks, `.env`, APP_KEY, Passport keys, native storage,
   extensions and the complete upstream release. Set ownership to the new
   application UID; never serve raw outer extraction directories over HTTP.
4. Create an isolated database and import database.sql using a matching MariaDB
   client. Recent MariaDB sandbox-mode comment lines can fail with older MariaDB
   or MySQL clients. This engine refuses visible routines, events, views,
   triggers and non-InnoDB tables. Visible-zero under limited current-user
   grants does not establish that hidden objects do not exist. Routines/events
   are not exported. Definer migration is outside this supported restore.
   Native database TLS configuration is presently unsupported and refuses
   capture explicitly; use a supported host/socket fixture for this restore
   drill, and configure any restored host TLS deliberately.
5. Change only environment values required by the new host: database connection,
   APP_URL, local mail/queue/cache endpoints and provider sandbox settings. Keep
   the restored APP_KEY to decrypt native settings and user MFA values; keep
   Passport keys. Validate DB content, encrypted-value decryption, key loading,
   no pending native migrations and native login before opening the site.
   Update native global `app_url` as well as `.env`, because native settings
   override environment configuration. Clear native application/config/route
   caches for the new host. Disable the copied Backups extension record and its
   schedule before booting any scheduler, and update its native `private_root`.
6. Establish the host's native document-root symlink to app/public, scheduler,
   PHP runtime and verified cooperative locks. The archive does not restore
   Plesk/host configuration, cron, Redis, remote object storage or provider state.
   Reconcile provider state before reenabling automation.
7. Give the restored/new host a new owner-private root and installation UUID.
   Run the explicit CLI `init` before enabling the new scheduler; re-enter S3
   credentials. Never copy the old UUID into an independently live
   installation: retention would share a remote prefix. Adoption of old identity
   is permitted only after the old installation is fully decommissioned.
8. Review queued jobs, pending invoices, scheduled billing and webhook receipts
   before enabling workers/cron/webhooks. Queue records may repeat completed
   external actions; provider state is outside the checkpoint. Restore webhook
   idempotency/reconciliation deliberately. Refresh billing guard and run a new
   verified backup. Open service only after this isolated validation.

Full mode preserves safe pre-existing maintenance files in the archive; restore
may therefore remain in maintenance intentionally. A normal full run excludes
only its newly created markers and declares those paths. No automatic restore
command mutates a destination app or database. The documented restore drill
and CI fixture use disposable applications; real hosts and S3 providers
remain deployment work.
