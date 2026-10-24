# Installation

Use Linux, Python 3.10 or newer and the application's PHP 8.3 or newer. Release
ZIPs include an isolated, pinned S3 SDK. Do not run pip or Composer in Paymenter
for this extension. Keep Paymenter core and native database definitions stock.

The server also needs GNU tar, GNU gzip and a supported MariaDB/MySQL dump client.
Only non-UTC schedules need time zone data (`tzdata`). Minimal images such as
Alpine may need the `tzdata` package installed.
Encrypted S3 uploads need the complete GnuPG package, including gpg, gpg-agent
and gpgconf. GnuPG is a server dependency and is not bundled in the ZIP;
the server administrator maintains its security updates. The Backups setup
checklist reports missing or unusable tools with installation/path guidance.
Local-only backups do not require GnuPG. The extension inspects prerequisites
but does not install packages or change hosting configuration.

Build on a separate development machine:

```sh
python3 tools/build_release.py --output dist/Backups-0.4.1.zip
(cd dist && sha256sum -c Backups-0.4.1.zip.sha256)
```

The deterministic archive has one top-level `Backups/` directory, compatible
with the native extension uploader. Upload it in the native administrator
extension interface, install and enable Backups. Alternatively extract that
directory to `extensions/Others/Backups` and use the native extension install
command. The private state and remote prefix survive native uninstall.

Select an owner-private directory outside the application and every document
root. The default is a sibling `paymenter-backups-private` directory, mode 0700,
owned by the application UID. In Backups settings, list every other public web
folder on this account, one full path per line. CLI `extra_docroots` uses a JSON
list. The engine checks private state,
local output and recovery-kit exports against canonical application/docroot paths.
Read-only plan/config/status never create private state; explicit configuration
set or capture execution provisions its immutable installation binding.
Check actual hosting quotas, FPM UID and
`open_basedir`; the web interface reports unavailable private storage and points
to the CLI when it cannot write. Use the application's configured PHP binary,
which may differ from the system CLI PHP. Set the PHP path in Backups settings to that
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
private directory in First-time setup and program paths under Advanced before
the first save if the defaults do not fit the host. The first save creates the private directory and
installation ID. Afterwards the private directory can only be moved from the
terminal. The page checks each save with the engine before writing, including
schedule and billing overlap, S3 endpoint and backup directory location.

Backups settings is the only web settings editor. The native Extensions page
controls enabling, disabling and uninstalling the extension. CLI configuration
remains available with the same internal validation rules. If the saved public
web folder list is damaged, re-enter its complete list in Backups settings and
confirm it before saving; leave it empty only if there are no other public web
folders on the account. If the saved data-directory path itself is unusable,
follow [the CLI correction instructions](CLI.md) with its existing canonical
directory and known installation UUID. The web page never guesses a replacement
installation. Fresh records write settings_version=2;
records with existing rows receive marker 1 and retain 0.3 behavior until the
Backups page explicitly saves every v2 field. Both new schedules default off,
local storage on, full local count 7 and database local count 24, with a 10-GiB
budget and 1-GiB reserve. Suggested operating cadence is database hourly/full
weekly after readiness passes; enabling the extension alone starts neither.
Settings-only validation/init never access the encrypted credential file. Secret
submissions follow initialization under shared configuration/tick coordination,
using one combined merge while native settings rows are locked. Definite
pre-credential failures restore unchanged setting rows. An uncertain commit
pauses backup work until a complete Save resubmits the intended settings and
uncertain credential groups; partial successes are reported explicitly.

Empty v2 binary paths mean trusted detection (Python, matching hosting PHP,
MariaDB/MySQL dump/client alternatives, GNU tar, GNU gzip; GPG belongs to encryption
setup). Nonempty legacy pins remain pinned. New v2 captures with blank
local_path use PRIVATE_ROOT/backups, including old artifacts bindings. Recorded
old backups and immutable identity stay unchanged and count toward the budget.
Local/S3 choices are independent and at least one is required. S3-only still
requires capture/staging/retrieval capacity; local staging is removed only after
complete remote scope/manifest/checksum/archive proof. Retrieval is queued and
protects the local Download copy for 24 hours.

Both modes require no concurrent updates/migrations/DDL. Full additionally
requires the merged host assumptions confirmation, file maintenance, supported
resolved stores inside the app/default database, and verified job coordination.
The tick caches sanitized account-crontab/systemd diagnostics for the page;
capture rediscovers. Empty v2 lock overrides derive the verified union. No
scheduler, incomplete discovery, unparseable/unlocked/continuous jobs or unsafe
locks refuse. Custom extensions and panel/Supervisor writers remain declared
host assumptions. Full never kills provider work to achieve idle.

```sh
python3 -I /path/to/app/extensions/Others/Backups/runtime/cli.py \
  --app /path/to/app --private-root /path/to/private \
  --bridge /path/to/app/extensions/Others/Backups/bridge.php --php /path/to/php plan
/path/to/php /path/to/app/artisan backups:guard
```

The native every-minute tick observes the actual native billing event and
launches durable work. Paymenter's server setup owns the systemd timer or cron
job invoking `schedule:run` each minute. Keep a working existing launcher and
use exactly one scheduler launcher. Backups never installs, enables or edits
those jobs. Follow [Paymenter's installation guide](https://paymenter.org/docs/installation/install)
for the host setup. Full backups additionally require that scheduler and worker
jobs use the configured cooperative locks. Save Backups settings first: init
creates scheduler.lock and queue.lock as 0600 regular files. Discovered locks
elsewhere must already exist safely. The tick uses no Laravel cache mutex and runs
in maintenance for recovery. It is the sole detached launcher; terminal capture
is foreground. Administrator heavy actions only write durable requests.

Copy the suggested cron lines exactly: cron requires `\%` for literal `%` in
application, PHP and lock paths even inside shell quotes. Suggestions preserve
literal backslashes too. Discovery rejects unescaped `%` because cron splits
the command there and sends the rest as standard input. Terminal commands and
systemd arguments use their ordinary quoting instead of cron escapes.

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
unauthenticated 401, a normal page or an origin mismatch refuses full capture.

S3 requires TLS. V2 encryption defaults on. When **Upload to S3** and **Encrypt
backups in S3** are on with no recovery key, Save generates a pending random key
or imports the exact passphrase entered in the form. Use **Show recovery key**
or **Download recovery key**, save it outside this server, then confirm **I saved
this recovery key outside this server**. The confirmation activates that exact
fingerprint; usable GnuPG and an acknowledged active key are required for new
encrypted delivery. Missing encryption setup blocks uploads while local capture
remains available and failed delivery keeps its local source. A replacement key
stays pending while uploads continue with the active key; activation retains the
old key for older copies. Keep every exported recovery key.

Encrypted S3 copies contain the archive ciphertext, encrypted recovery document
and public descriptor. Retrieval and remote cleanup proofs decrypt and fully
verify them. Local archives, ordinary recovery kits and admin downloads remain
plaintext. Plaintext uploads require an explicit acknowledgement, freshly
submitted when turning encryption off; encryption never falls back to plaintext.
Legacy acknowledged plaintext uploads continue until v2 adoption, and old
copies always retain their recorded format. Archives
contain `.env`, APP_KEY, database data and Passport keys, but never S3 credentials.
Give dedicated credentials only the bucket/prefix list, get, put, delete and
multipart permissions needed for this installation. The installation UUID scopes
all objects. Optional versioning/Object Lock are operator-managed; the extension
never changes bucket policy. Run destination-test explicitly before scheduling;
encrypted tests use the active key for a full round trip. Repeat the test after
changing the destination, enabling encryption or activating a replacement key.
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

The legacy S3-only credential bridge reads the expanded encrypted file. Its S3
credential save replaces the complete payload and erases the maintenance probe
and archive-key namespaces.
If rolling back, preserve the encrypted file privately first and avoid credential
edits with that bridge. Restore a version supporting every saved namespace before
editing credentials. Rollback does not provide authenticated maintenance probes
or encrypted S3 delivery; full mode can refuse with 401.


Use exactly the scheduler/queue replacement lines in overview's
configuration.checks.jobs.suggestions. They include a private flock and accepted
>/dev/null 2>&1 tail. Replace the existing scheduler entry rather than adding a
second one. Backups never writes crontab or systemd configuration. A continuous
queue service must be replaced with bounded coordinated work before full mode;
database-only mode does not depend on job locks or local store classification.
