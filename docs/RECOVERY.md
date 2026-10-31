# Recovery and restore

These archives contain private customer data, `.env` with DB credentials and
APP_KEY, and Passport signing keys. Keep artifacts owner-private. Local archives,
admin downloads and ordinary recovery exports remain plaintext. New encrypted S3
copies protect both the archive and the full recovery manifest; their public
descriptor exposes transport metadata only. S3 credential
files live outside the app and are excluded from every archive and recovery kit.
APP_KEY encryption protects a separately leaked credential file; a compromised
application UID can read APP_KEY and has access to the credentials.

## Recovery keys and encrypted S3 copies

Encryption uses the fixed `openpgp-symmetric-aes256-seipd-v1` format: binary
GnuPG AES256, salted iterated SHA256 S2K (mode 3, count 65011712), no additional
compression, and SEIPD v1/MDC. APP_KEY wraps the local credential file; it is
independent of the archive recovery secret. Neither APP_KEY nor S3 credentials
can replace a lost recovery secret.

Generate a recovery key (32 random bytes encoded as 64 lowercase hexadecimal
characters) or import an exact passphrase of at least 20 characters and at most
1024 UTF-8 bytes, without control characters. No trimming or normalization is
performed. On the Backups page, Save creates the first pending key when encrypted
S3 uploads are enabled. Use **Show recovery key** or **Download recovery key**,
save it outside this server, then confirm **I saved this recovery key outside
this server**. Acknowledgement binds the exact fingerprint shown; a changed key
refuses confirmation. A pending first key cannot authorize uploads. Rotation
prepares a new pending key while the acknowledged active key remains in use;
acknowledgement activates the replacement under the short run lock and retains
the old key as retired. Prepared deliveries keep their original key. Keep every
exported recovery key:
older copies, bucket versions and offline exports still need their original
keys. APP_KEY rewrapping preserves all archive keys and other credential
namespaces; complete it before removing a previous APP_KEY.

The standalone CLI provides `encryption-key generate|import|acknowledge|
discard-pending|status`, `recovery-key export --output /absolute/private/file`
with optional `--fingerprint FULL-FINGERPRINT`, and `credentials-rewrap`.
Passphrases retain their exact UTF-8 bytes, including leading and trailing
spaces. The administrator dialog shows and copies those same bytes; preserve
all whitespace when saving or entering a recovery secret.
Import reads JSON stdin containing `passphrase` and optional exported
`fingerprint` parameters; acknowledgement reads JSON stdin containing the full
`fingerprint`. Importing an exported identity preserves its scheme, salt,
iteration count and value. These operations require an initialized private
root and preserve the installation, privacy and credential-store checks without
requiring billing settings. Recovery-key export writes a new owner-private file
and refuses overwrite. The admin download uses a five-minute signed link, checks
extension-edit permission and records secret-free audit evidence before release;
the attachment uses no-store and no-referrer headers.
Both key export and ordinary recovery-kit export check current native document
roots through the cold PHP/PDO bridge and refuse if those boundaries cannot be
read or validated, even when a cached private policy exists. They need no dump
program, billing admission or other capture readiness; the privacy read does
not open credentials. Ordinary CLI JSON returns only public metadata. Do not feed secret-bearing JSON
through shell command arguments or save it in command history.

Each encrypted copy consists of exactly three objects in its recorded provider
scope:

```text
PREFIX/INSTALLATION/RUN.tar.gz.gpg
PREFIX/INSTALLATION/RUN.recovery.json.gpg
PREFIX/INSTALLATION/RUN.manifest.json
```

The final object is a small public commit descriptor, published last and removed
first. The decrypted recovery document binds the full original manifest to the
archive ciphertext name, size, SHA256, installation, run, key fingerprint and
provider scope. The public descriptor and latest pointer alone prove neither
authenticity nor freshness. Recognized old copies remain `plaintext-v1`; their
format does not change when current settings change. Unknown formats refuse.
Missing retired keys preserve the affected copy and never imply that S3 objects
are missing. Encryption failures never fall back to plaintext uploads.

## Encrypted recovery on a fresh machine

This procedure needs only the three downloaded objects, the separately saved
recovery-key attachment, GnuPG, Python 3.10+ and the standalone
`Backups/runtime/verify_recovery.py` from the extension ZIP. It needs no running
Paymenter, original private root, APP_KEY, installation adoption or S3 SDK.
Use your provider's ordinary download tools to obtain all three objects.

Create scratch storage outside every served directory and keep the recovery
secret in your password manager, separate from the downloaded ciphertext:

```sh
umask 077
mkdir restore-private
cd restore-private
mkdir gpg-home
```

Copy the three objects and verifier here. First check bounded descriptor schema,
exact object names and ciphertext sizes/hashes. This transport check treats the
descriptor as untrusted:

```sh
python3 -I verify_recovery.py \
  --descriptor RUN.manifest.json \
  --archive-ciphertext RUN.tar.gz.gpg \
  --recovery-ciphertext RUN.recovery.json.gpg
```

Enter the separately saved secret at GnuPG's prompt. Never put it in a command
argument or environment variable. Decrypt to partial files, checking each
command's successful exit before renaming:

```sh
gpg --no-options --homedir "$PWD/gpg-home" --rfc4880 \
  --no-symkey-cache --pinentry-mode loopback --status-fd 3 \
  --output recovery.json.partial --decrypt RUN.recovery.json.gpg \
  3>recovery.status && mv -n recovery.json.partial recovery.json

gpg --no-options --homedir "$PWD/gpg-home" --rfc4880 \
  --no-symkey-cache --pinentry-mode loopback --status-fd 3 \
  --output backup.tar.gz.partial --decrypt RUN.tar.gz.gpg \
  3>archive.status && mv -n backup.tar.gz.partial backup.tar.gz
```

Require `DECRYPTION_INFO 2 9 0` (or the GnuPG version's equivalent omission of
the zero AEAD field), `DECRYPTION_OKAY` and completed decryption in both status
files, with no integrity/decryption failure. Reject AEAD, missing MDC or other
algorithms. Never use `--ignore-mdc-error`. GnuPG can emit most plaintext before
detecting a damaged final MDC: do not extract partial output or pipe decryption
directly into tar. Delete failed partial outputs before retrying.

After the recovery-document command succeeds, you can check its bindings before
decrypting the larger archive by adding `--recovery recovery.json` to the
transport-check command. This checks the recorded installation, run, key and
provider scope against the descriptor and archive ciphertext.

Then cross-check the decrypted recovery bindings and fully verify the plaintext
archive SHA256/size, gzip trailer, tar layout, embedded manifest and every
payload hash:

```sh
python3 -I verify_recovery.py \
  --descriptor RUN.manifest.json \
  --archive-ciphertext RUN.tar.gz.gpg \
  --recovery-ciphertext RUN.recovery.json.gpg \
  --recovery recovery.json --archive backup.tar.gz
```

The verifier checks content; successful GnuPG completion and the pinned-profile
status checks establish the preceding decryption proof. Proceed with the
isolated restore steps below only after both succeed. Preserve the original
APP_KEY and Passport keys within a full archive, disable copied schedules, and
assign the restored installation a new UUID. A database-only artifact supplies
SQL, not application files. Remove private scratch plaintext when finished;
terminate only the scratch home's agent if cleanup is needed:
`gpgconf --homedir "$PWD/gpg-home" --kill gpg-agent`.

Each backup tracks its local and S3 copies independently. Expiring a local copy
does not expire its S3 copy. Use **Download from S3** on the page, or `retrieve ID
--execute` from the CLI, to verify and restore a local copy before downloading.
Retrieval respects disk limits and protects the resulting local copy from
automatic retention for 24 hours. It never overwrites an existing archive or
manifest. Verification and retrieval require the recorded S3 destination and
working keys. A changed endpoint, region, bucket, prefix or addressing style
preserves the old copy and refuses operations in the new location.

Local-only deletion can leave S3 intact. S3-only deletion can leave the local
Download intact, including after an interruption. Whole-run deletion is recorded
only after all known copies are removed. Each affected location must retain
another fully verified backup. Cleanup reports damaged, missing and out-of-scope
copies rather than treating them as deleted or blocking unrelated retention.

For legacy unbound remote records, an explicit delivery retry with a surviving
verified local archive can establish the current destination. A missing local
manifest can be reconstructed from a recorded legacy deletion intent only after
full archive verification. If neither a safe local source nor a known provider
location survives, preserve the journal and resolve the remote location with
the operator. The extension cannot infer it safely. Fully deleted historical
records remain unchanged and do not cause provider calls.
Legacy unfinished multipart uploads without a provider binding also remain
untouched. Resolve those uploads through their original provider or an
AbortIncompleteMultipartUpload rule. The extension cannot safely abort them
by assuming the current endpoint owns them.

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
run's unchanged markers, down first and early-bootstrap stub second. Historical maintenance captures used this path at normal completion. New full
captures keep the site online and require no marker cleanup. Pre-existing markers remain. Matching
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
   hash. For a recognized plaintext remote copy fetch its full manifest and
   archive; for an encrypted copy complete the fresh-machine decryption and
   verifier procedure above first. Do not use ETag as SHA256.
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
may therefore remain in maintenance intentionally. New full captures keep the site online and create no maintenance markers.
Historical full runs excluded only their newly created markers and declared those paths. No automatic restore
command mutates a destination app or database. The documented restore drill
and CI fixture use disposable applications; real hosts and S3 providers
remain deployment work.
