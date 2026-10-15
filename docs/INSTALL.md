# Installation

Use Linux, Python 3.10 or newer and the application's PHP 8.3 or newer. Release
ZIPs include an isolated, pinned S3 SDK. Do not run pip or Composer in Paymenter
for this extension. Keep Paymenter core and native database definitions stock.

Build on a separate development machine:

```sh
python3 tools/build_release.py --output dist/Backups-0.2.0.zip
(cd dist && sha256sum -c Backups-0.2.0.zip.sha256)
```

The deterministic archive has one top-level `Backups/` directory, compatible
with the native extension uploader. Upload it in the native administrator
extension interface, install and enable Backups. Alternatively extract that
directory to `extensions/Others/Backups` and use the native extension install
command. The private state and remote prefix survive native uninstall.

Select an owner-private directory outside the application and every document
root. The default is a sibling `paymenter-backups-private` directory, mode 0700,
owned by the application UID. Configure `extra_docroots` as a JSON list of every additional document root,
including other sites served by this account. The engine checks private state,
local output and recovery-kit exports against canonical application/docroot paths.
Read-only plan/config/status never create private state; explicit configuration
set or capture execution provisions its immutable installation binding.
Check actual hosting quotas, FPM UID and
`open_basedir`; the web interface reports unavailable private storage and points
to the CLI when it cannot write. Use the application's configured PHP binary,
which may differ from the system CLI PHP. Set the native `php` field to that
absolute CLI path when required. Console ticks cross-check it against PHP_BINARY;
FPM derives PHP_BINDIR/php when the field is blank, never invokes php-fpm.
Executable validation uses a short isolated Python child to check absolute,
regular executable paths and canonical CLI PHP identity. This supports web
pools whose `open_basedir` allows the application/private state while excluding
system executable directories: PHP cannot stat those binaries even when it
can launch them. A Python or PHP symlink to the same executable remains valid;
missing, nonexecutable, relative or mismatched CLI PHP paths refuse. The probe
checks executable paths only; private-root/document-root and credential-file
ownership, modes and binding checks remain unchanged. No hosting-policy change
is required. No path in this guide is deployment specific.

After enabling the extension, open **System > Backups**. The Backups tab lists
what is still needed; the Settings tab edits everything in one form. Set the
private folder and program paths under Advanced before the first save if the
defaults do not fit the host. The first save creates the private folder and
installation ID. Afterwards the private folder can only be moved from the
terminal. The page checks each save with the engine before writing, including
schedule and billing overlap, S3 endpoint and backup folder location.

Non-secret settings are native extension fields, so the native Extensions
editor also works as a fallback with the same field rules. There, weekdays and
path lists are JSON (Monday 0 through Sunday 6); cross-field checks run after
saving and record an advisory result. Defaults are automatic backups off,
full backups, local storage, 02:00 UTC, a 10 GiB private byte budget and seven
retained verified copies. Automatic retention applies after successful
publication and preserves undelivered/unresolved copies. Enabling the
extension does not itself enable automatic backups. Settings saves never touch
the credential file.

Full-quiesced requires explicit no-concurrent-DDL, cooperative-writer and
external-state acknowledgements, existing owner-controlled scheduler/worker
flock files, actual crontab entries using those files, bounded requests/work,
file-backed native maintenance, a fresh observed billing guard and sufficient
space. The runner checks these prerequisites and refuses an incomplete setup.
Configure hosting-specific job coordination with the operator before enabling
capture. It never kills provider work to achieve idle.

```sh
python3 -I /path/to/app/extensions/Others/Backups/runtime/cli.py \
  --app /path/to/app --private-root /path/to/private \
  --bridge /path/to/app/extensions/Others/Backups/bridge.php --php /path/to/php plan
/path/to/php /path/to/app/artisan backups:guard
```

The native every-minute tick observes the actual native billing event and
launches durable work. Configure the ordinary native `schedule:run` cron with
its existing cooperative lock. The tick uses no Laravel cache mutex and runs
in maintenance for recovery. It is the sole detached launcher; terminal capture
is foreground. Administrator heavy actions only write durable requests.

Credentials are write-only on the Backups page or supplied by private JSON stdin
through CLI `config set`. They never enter native setting rows. The encrypted
private file binds the immutable installation UUID, cipher and APP_KEY HMAC
fingerprint, and supports native previous keys. Encrypting this file protects
its separate leakage; the same application UID can read `.env` and decrypt it.
Unreadable credentials fail closed. To re-enter after a lost key, preserve the
old file outside active state with an explicit operator filesystem action,
then save new credentials. Do not erase or overwrite unreadable data silently.
Restore the old APP_KEY/previous-key config first when it is available.

On HTTPS sites protected by an outer HTTP Basic guard, save optional write-only
maintenance probe credentials on the Backups page or through the private JSON
stdin contract in [CLI.md](CLI.md). Enter the exact configured HTTPS origin.
Saving or clearing preserves S3 credentials. Full capture continues to require
an exact native maintenance 503 response without cookies or redirects. An
unauthenticated401, a normal page or an origin mismatch refuses full capture.

S3 requires TLS and an explicit plaintext-offsite acknowledgement. Archives
contain `.env`, APP_KEY, database data and Passport keys, but never S3 credentials.
Give dedicated credentials only the bucket/prefix list, get, put, delete and
multipart permissions needed for this installation. The installation UUID scopes
all objects. Optional versioning/Object Lock are operator-managed; the extension
never changes bucket policy. Run destination-test explicitly before scheduling.
A restored/new host gets a new installation UUID. Adopt an old UUID only after
decommissioning its old host to avoid competing retention against one prefix.

An app/database checkpoint excludes host/Plesk/cron configuration, Redis,
external object stores and remote provider actions. Database-online is an
InnoDB transactional database snapshot under no concurrent DDL and does not
claim an atomic application/files snapshot. Read [RECOVERY](RECOVERY.md) before
using an artifact or acknowledging unresolved service state.

The cold adapter honours native DB_URL resolution, including URL query options.
Configured native PDO database TLS options currently refuse with
`database-tls-unsupported` because dump/client TLS parity is not implemented.
Unix-socket and ordinary native MySQL/MariaDB connections are supported; do not
remove an existing database TLS requirement to bypass this refusal.

The legacy S3-only credential bridge reads the expanded encrypted file, but
its credential save replaces the complete payload and erases the probe namespace.
Before rollback, keep a private copy of the encrypted file and avoid credential
edits with that bridge. Install a version supporting every saved namespace before
entering probe credentials again. Without authenticated maintenance probes,
full mode can refuse with 401.
