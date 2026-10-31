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
python3 tools/build_release.py --output dist/Backups-0.4.3.zip
(cd dist && sha256sum -c Backups-0.4.3.zip.sha256)
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
field rules, the S3 endpoint and backup directory location. A billing window
can delay a queued backup that pauses jobs without blocking the save.

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
requires the merged host assumptions confirmation, supported
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
job invoking `schedule:run` each minute. This standard Artisan launcher needs no
guarded-script setting.

After private-state setup, add a separate host cron job as the site user:

```cron
*/5 * * * * /path/to/php /path/to/app/artisan backups:observe >> /path/to/private/observer.log 2>&1
```

Replace the paths with the installed app, its CLI PHP and existing private
directory. PHP needs POSIX support, as do the existing private-state checks.
Keep this job outside `schedule:run`, queue workers, and their coordination
locks. It can then notice either stopped service and send mail synchronously.
It never starts backup work itself. The runtime first retries/reconciles safe
backup work; staff mail starts when a failure is terminal or a copy misses its
schedule and recovery window. It cannot safely restart a host job whose service
manager and ownership it does not control. Restore the job named in the mail
and confirm a new backup finishes.

Set Paymenter's system email address to the staff mailbox and enable system
mail. Run `backups:observe` once as the site user and check its exit status.
The private incident ledger suppresses repeated mail across command restarts;
a transport failure stays pending and retries on the next host check. Do not
delete that ledger to acknowledge an incident; a healthy check clears it.
Use the host's existing log rotation for `observer.log`. This job and its paths,
mail transport and system email address are installation responsibilities.

The observer survives a stopped Paymenter scheduler or worker, not a stopped
host, host cron service, database, PHP bootstrap, or inaccessible private state.
Monitor this host job's nonzero exit/output and host availability through the
host's existing monitoring. Do not discard its stderr. Offsite freshness tracks
completed deliveries and recorded remote observations; use verified recovery
checks for remote integrity. SMTP acceptance and the local sent receipt cannot
commit together: a crash between those two can repeat a message with the same
incident ID. Normal repeated checks send one message per incident.

For an app at `/app` using a guarded scheduler instead, set the process environment
variable `PAYMENTER_BACKUPS_GUARDED_SCHEDULER` to its exact absolute script path,
for example `/opt/paymenter/scheduler.php`. The default is empty: no guarded
script is accepted until you set it. Set it for PHP web services, scheduler/cron
processes and standalone Backups CLI invocations, including detached workers.
For a container, put it in the container's environment (for example a Dockerfile
`ENV PAYMENTER_BACKUPS_GUARDED_SCHEDULER=/opt/paymenter/scheduler.php`); for systemd,
set `Environment=PAYMENTER_BACKUPS_GUARDED_SCHEDULER=/opt/paymenter/scheduler.php`
in the service. Restart affected services after changing it. A Laravel `.env`
entry alone is insufficient: this setting reads the operating system environment.
A standalone invocation can start with
`export PAYMENTER_BACKUPS_GUARDED_SCHEDULER=/opt/paymenter/scheduler.php`.
The corresponding launcher is
`flock PATH /usr/local/bin/php /opt/paymenter/scheduler.php`.
Use a canonical absolute path without dot segments, duplicate or trailing slashes;
path characters may be letters, numbers, spaces or `_@%+=:,./-`.
For `/app`, unrecognized direct `/usr/local/bin/php` launchers remain visible as
unsupported even after this setting changes or clears. Retire obsolete cron
entries/services when switching scripts; a second Artisan scheduler does not
make an unknown PHP launcher safe.
The script must be a root-owned regular file, not group or world writable, and
its path must contain no file or parent-directory symlinks. Backups rechecks it
at discovery, admission and cache reads.
The guarded form accepts no interpreter or script arguments and retains its
exact command in diagnostics and replacement suggestions. Keep a working
existing launcher and use exactly one scheduler launcher. Backups never installs, enables or edits
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

Full backups no longer contact the web server or enter maintenance. Previously
saved probe credentials remain readable/clearable through the CLI for compatibility;
they do not gate capture. Full backups require verified job locks. If the host
cannot provide them, full capture refuses; database-only snapshots may run under
the explicitly documented no-coordination fallback.

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
database-only mode still works without job-lock setup or local store classification.
A complete fresh launcher check must prove no coordination is set up; the page
shows that these database snapshots can overlap jobs. Failed checks retry rather
than bypassing locks.
When locks are verified, database-only mode waits for scheduled/queue jobs and
avoids the daily billing window. It releases the locks after the dump and uses
no maintenance mode or drain.

## Installation readiness and installer handoff

**System > Backups > Installation** reports Tools, Scheduler, Independent
observer, Private state, Local storage and Offsite storage separately. Both
schedules stay Off on a new install. Offsite is Pending until configured and a
storage test passes for the current destination and recovery key. Pending
unselected offsite storage does not block local-only schedules.

The small interface for an external installer is:

```sh
/path/to/php /path/to/app/artisan backups:readiness --prepare --kind=database
```

Use `--kind=full` for the full schedule. Omit `--prepare` for a read-only check;
omit `--kind` to require both types. This command emits JSON on stdout, exits
0 when the selected type is ready and 1 otherwise. `schema_version: 1` contains
`ready`, `checks` (each has `state`, `code`, `next_step`) and `schedules`
(`full`/`database`, each with `ready`, `blockers`, `next_steps`). Bootstrap/engine
failure instead returns `ready:false`, `error`, `next_step`. Do not parse prose.
Readiness does not install anything or choose/enable a schedule.

`--prepare` safely creates the private installation identity and suggested lock
files, creates owner-private local storage, and writes, fsyncs, reads and removes
only its own small storage probe. Saving Settings also prepares local storage.
Existing unsafe storage permissions are never changed automatically. The local
check includes the configured byte budget and free-space reserve; it does not
prove future archive capacity, hosting quota, capture or restoration.

The scheduler check requires readable cron/systemd discovery and an actual tick
within five minutes. Full readiness retains the existing cooperative-job and
host-assumption requirements. The independent observer must have checked backup
state within 15 minutes with this installation's incident ledger; Missing means
it has never run, while stale or blind checks need attention. A recent manual
invocation proves it ran, not that a recurring host job is installed: verify the
separate five-minute cron line above and monitor it through the host. No host
service is edited because Backups does not know its ownership or service manager.
The observer's intentional at-least-once mail behavior is unchanged.

Commission in this order: prepare with schedules Off; repair any tools/storage
items; install/verify Paymenter's scheduler and the separate observer host job;
run `backups:observe` once; configure/test S3 and save its recovery key if using
it; check readiness for the chosen type; then enable that schedule in Settings.
An installer can use the existing audited `artisan backups:config-set` with
JSON stdin (for example `{"database_schedule":"hourly"}`); it goes through the
same readiness gate as the staff Save and Python `schedule enable/set` commands.
A refusal leaves the previous schedule unchanged and gives a plain next step.
Off and ordinary setup repairs remain available. S3 keys must be saved and the
current destination tested before a separate schedule-enabling save.

This is commissioning, not the full-backup/download or restore proof. No landing
installer or host deployment is included in this extension interface.
