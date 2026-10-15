# Architecture and build contract

This document defines the implementation contracts and consistency guarantees.
Default behavior: a full database/files checkpoint with brief maintenance.
Online database snapshots remain selectable. Implementation and tests use
disposable fixtures with fake credentials.

## Module and interface

The Python `paymenter_backups` module owns backup lifecycle, configuration
validation, scheduling decisions, verification, storage, retention and recovery.
A native `Others/Backups` PHP extension supplies configuration, administrator
pages, a cold configuration bridge and a native every-minute tick. Linux,
Python 3.10+ and PHP 8.3+ are supported. Release build bundles a pinned S3 SDK
inside the extension; application Composer/vendor files remain unchanged.

Source layout: `src/paymenter_backups/`, `bin/paymenter-backups`,
`extension/Backups/`, `tests/`, `tools/`, `docs/`, `.github/workflows/`.
Release ZIP layout: `extensions/Others/Backups` or native uploader-compatible
`Backups/` at its documented root. Main namespace/class is
`Paymenter\Extensions\Others\Backups\Backups`.

Public CLI workflows: `config show|set|validate|check`, `schedule show|set|enable|disable`,
`plan`, `run`, `list`, `status`, `overview [--offline]`, `verify [--show] [--record]`,
idempotent `deliver`, `retention plan|apply`, `delete ID [--execute]`,
`destination-test`, `requests list|create|cancel`, `recover [--auto|--execute]`,
`recover acknowledge --run ID`, and `export` of a verified recovery kit. All
mutations require an explicit command or `--execute` as appropriate. `run` and
`delete` without `--execute` print a plan. All workflows work from a server
terminal; use ordinary SSH from another machine.
Secrets enter through JSON stdin or a private pipe, never command arguments.
Status, verify and recovery work without Laravel/database startup.

Configuration show, validation and plan use a cold PHP/PDO bridge rather than
booting Paymenter/providers: instantiate Illuminate Application and bootstrap
only LoadEnvironmentVariables/LoadConfiguration, then use PDO and the native
model's morph class. Honor cached config without registering providers; return
runtime secrets on a private pipe. Native-row writes use a booted extension
Artisan command/Eloquent for events/auditing. Credential-file writes are atomic
and fsynced through the cold bridge.

Admin configuration, manual requests, backup history, verify, download,
per-backup delete, delivery retry, retention and destination testing use the
same engine. Native authentication, CSRF and the native extension-edit
`admin.extensions.update` permission guard every page action and the download
route. The admin page writes durable requests and never launches capture,
verification, delivery or deletion from PHP-FPM. Recovery remains CLI plus the
automatic tick. [ADMIN-UI.md](ADMIN-UI.md) defines the page's journeys and
language; the contracts below bind it to the engine.

PHP `open_basedir` may exclude system executables while permitting process
launch. The runtime validates configured Python/PHP paths in a short isolated
Python child, requiring absolute regular executable paths and canonical CLI
PHP identity. This executable-only probe does not change PHP's private-root,
document-root or credential-file guards and does not relax hosting policy.

## Configuration bridge contract

Versioned JSON envelope: `schema_version=1`, `app`, `owner_uid`, `private_root`,
`database`, `settings`, `framework`, `billing`, `tools`. Database fields:
`driver`, `host`, `port`, `socket`, `name`, `user`, `password`. Framework fields
include `maintenance_driver`, `cache_driver`, `session_driver`, `queue_driver`,
`filesystem_driver`, `app_url`, `timezone`, `app_key` and `cipher` where needed.
Billing fields include `daily_time` and its timezone. Tools specify absolute
Python/PHP/dump/tar/gzip paths. Public output redacts secrets.

Settings use structured daily/weekly schedule, HH:MM, weekdays and timezone;
mode `full-quiesced` or `database-online`; enabled flag; destination `local` or
`s3`; endpoint/region/bucket/prefix/path-style; local byte budget; timeouts and
grace/deferral limits; optional shared job lock paths; retention policy; and
explicit plaintext-offsite acknowledgement. Provide documented safe defaults
and fail clearly when a consistency prerequisite is unconfigured.

Native setting rows contain nonsecrets only. Store access key, secret and session
token and optional maintenance probe Basic credentials in a private file encrypted with the native Illuminate Encrypter/APP_KEY.
The default native extension edit loads all setting values into browser state,
including undeclared rows, so secrets must never be stored there. Custom secret
fields are blank/write-only and reset after save. Native settings edits preserve
the external credential store.

Credential headers bind schema version, installation UUID, cipher and an HMAC
key fingerprint; support native previous keys. A mismatch/decryption failure
returns `credentials-unreadable` and never silently overwrites the file. The
installation UUID lives outside native rows and survives extension uninstall.
An independent restored/new host gets a new UUID; adoption requires the old
installation to be decommissioned. APP_KEY encryption protects a separately
leaked file, not compromise of the app UID. Native form edits nonsecrets; custom
page handles credentials/requests/status and clears secret input on failure.

## Administrator page contracts

`overview` is the page's only status source. It returns fixed codes, run and
request IDs, timestamps, sizes, booleans, configured nonsecret settings,
per-mode admission codes, the next schedule slot and the scheduler's last
check-in. It never returns artifact or manifest paths, manifests, grants,
process identities, request results, stderr or credentials; credentials appear
only as saved/not saved. Every engine run of `tick --execute` writes a private
`scheduler.json` check-in. `status` and `list` keep their 0.1 output.

Settings saved on the page pass plain Laravel field rules that match the engine
(positive integers stay positive, weekdays are 0 to 6 and required for an
enabled weekly schedule, path lists are absolute, the S3 prefix has no empty,
`.` or `..` parts or backslashes), then `config check`, which
runs engine normalization, validation, schedule/billing admission and the
backup-folder location rule without writing. Only keys that differ from the
stored rows, read on the server at save time, are then written through native
Eloquent, so native auditing records them. The browser cannot supply that
baseline. The page then runs `init`; if it fails, the written rows are put
back and the page reports the refusal, never "saved". The first save runs
`config check` and `init` even when every default is kept, so first-time setup
needs no edited field. `init` validates before it creates an identity. A
settings save never opens the credential file, and saving keys or the site
password never creates private storage. The page refuses to change an
initialized private root (the field is disabled, and the server rechecks the
installation file), because a new root would start a new identity without the
saved keys and history. When the engine itself cannot start, the page saves
only the private root, document roots and program paths needed to repair it.
The native extension editor applies the same rules to stored strings,
including `boolean` for checkboxes and the S3 prefix pattern.

S3 keys and the site password are write-only Livewire state that `dehydrate()`
resets on every response, including validation failures. The site password's
address is Paymenter's own https origin, taken on the server; the browser
cannot send one, and an http address offers no site password. Explicit removal
of either namespace preserves the other under the shared credential flock.

Requests carry an optional numeric `actor` and, for `run`, an optional `mode`.
A repeated identical waiting manual request returns the existing request.

## Artifact resolution, download and per-backup delete

`artifacts.py` is the only code that turns a run ID into files. It requires
the run ID grammar, an owner-private journal for this installation, recorded
names exactly `RUN.tar.gz` and `RUN.manifest.json` in one canonical,
owner-only directory outside the app and every document root, and opens that
directory component by component with `O_NOFOLLOW`, comparing its device and
inode with the checked location. Files open relative to that descriptor with
`O_NOFOLLOW` and must be regular, owned by the app UID, mode exactly 0600 and
single-linked. Manifests are bounded and must carry a hexadecimal SHA-256 and
positive size for this installation and run. Verify, export, delivery,
download, retention and per-backup delete use it.

Capture records the published archive's identity (device, inode, size,
nanosecond mtime and ctime) and SHA-256 after its full archive check. Queued
`verify` and `verify --record` hold the run lock, fully check the archive and
record the same identity, or record `verification_error` and remove the old
record. A request status is never treated as verification.

The download route is registered inside the admin panel's authenticated route
group and accepts only a run ID. It checks `admin.extensions.update`, then
starts the engine's internal `artifact-stream`. The engine opens the archive
once, refuses with `verification-required` unless the open descriptor matches
the recorded identity and manifest digest, streams from that descriptor,
hashes while streaming and withholds the final chunk unless the bytes and
descriptor identity still match. Content-Length then exposes any refusal as an
incomplete transfer. Each attempt appends actor, run ID, outcome and byte count
to a private `audit.jsonl`. PHP never opens archive paths or hashes archives.
PHP waits at most 30 seconds for the stream header and at most 60 seconds
between body reads; total transfer time is unbounded so slow clients still
finish. On refusal, idle timeout, client disconnect or early request end it
closes the pipes, gives the engine two seconds to record the interruption,
then sends SIGTERM and, after two more seconds, SIGKILL before reaping it.
Remote-only download is not implemented; the page offers local archives only.

`delete ID` previews which local files and S3 objects one backup's deletion
removes and returns the first refusal. Queued and `--execute` deletion holds
the run lock and refuses an incomplete or deleted run, a remote copy outside
the configured bucket and installation prefix, the newest copy in the
configured S3 prefix, any other active request for that run, or the absence of
another backup that passes a full verification at that moment, checked on
every attempt. It records `delete_intent` with the owned manifest before
removing anything, verifies remote objects match or are gone, removes S3
manifest then archive, then removes the local manifest then archive relative
to the verified directory descriptor. Before each unlink it re-reads the name
with `fstatat` through that descriptor and requires the full identity (device,
inode, size, mtime_ns, ctime_ns) that the same attempt checked, so a file
modified in place or recreated on a reused inode number is not mistaken for the
checked one; a replaced or modified file is kept and the attempt fails with
`delete-local-verification-failed`. The stat-to-unlink step is not atomic
against a hostile same-UID writer; the owner-private directory assumption
still applies. A failed attempt resumes from its
intent. Retention ignores runs with a manual delete intent, and per-backup
deletion never changes retention settings.

## Scheduling and admission

The native tick runs every minute with `evenInMaintenanceMode`; register lazily
in console boot after Schedule resolution. It computes due requests and missed
slots through the Python module. Request files are atomic/fsynced, identified
by installation and schedule slot, and durable across deferred/busy runs.
Initial maintenance causes scheduled capture to skip, not reopen the site.
Tick performs only guarded auto-recovery while maintenance is active.

The tick has no cache mutex (`withoutOverlapping`/`onOneServer`); private flock
is authoritative. It invokes the short launcher with Python `-I`. Each tick
observes the actual native CronJob event's expression/effective timezone and
next eight days of UTC instants into an atomic private billing guard, fingerprinted
against cold-read native billing settings. Full admission requires a fresh
matching guard; stale/unknown billing state refuses full mode. Provide a booted
CLI guard-refresh command for terminal use before a scheduler tick is available.

The tick is the sole detached launcher. Foreground terminal execution is also
supported. A launcher closes inherited nonessential descriptors and starts an
independent session before running capture; verify that it owns no inherited
scheduler/worker lock. Never execute capture synchronously while holding a
caller job lock or through the ordinary queue. FPM never launches capture.

Protect the native daily billing minute: schedule validation and runtime
admission reject/defer holds overlapping `cronjob_time` in its actual timezone,
including drain/capture bounds, lock waits, safety margin and deferral. Record
the protected minute and actual hold interval. Native Laravel daily schedules
do not catch up automatically after a skipped minute. UTC is recommended;
document and test DST gaps and repeated local slots.

## Capture modes

`database-online` produces a transactional InnoDB SQL snapshot without
maintenance, under a no-concurrent-DDL condition. Its manifest clearly excludes
an atomic application/files snapshot and external provider state.

`full-quiesced` acquires a private backup lock and configured job coordination
locks, enters native prerendered maintenance, drains existing requests/jobs and
captures SQL plus the full app while controlled writers remain excluded. Idle
observations choose a quiet time; they never substitute for admission control.
Refuse unsupported engines, unaccounted durable external state, external
symlink targets and uncontrolled writers. Document the no-DDL, bounded-request
and cooperative-writer assumptions. Never kill provider work to achieve idle.

After native down, require file driver, empty exceptions, no secret/redirect,
nonempty rendered template and status503; verify an actual early-bootstrap HTTP
503. Plain down allows settings/extension bootstrap before middleware rejects.
Probe a nonce path without redirects and require body equality with the exact
native template, not status alone. Optional HTTP Basic credentials live in an independent encrypted
namespace, bound to the explicit configured HTTPS origin (scheme/host/effective
port). Changes to app origin refuse credential use until credentials are rebound.
Anonymous HTTP remains supported. The nonce GET sends no cookies and follows no
redirect; credentials never bypass native maintenance. Transport uses explicit
system CA trust, verified hostname/TLS, no proxy and no TLS key logging, independent
of inherited environment overrides. Python independently validates stored and
current origins immediately before transport and is authoritative for credential
use; PHP uses the same strict grammar for early save feedback. Parser differences
therefore refuse at use rather than authorizing a different target. Tests cover
canonical/parity cases; IPv4-mapped IPv6 and non-ASCII hostnames refuse. A10-second
wall-clock deadline covers response headers and exact bounded body proof, including
slow-drip peers. Native templates above 64 KiB refuse before any request. An existing
caller SIGALRM timer is preserved and causes proof refusal. Only exact 503/template bytes prove success;
401, ordinary pages and redirects refuse. Credential writes serialize the entire
read/merge/encrypt/atomic-write under one owner-private flock, preserving S3 and
probe namespaces independently; explicit probe clear preserves S3. Open configured job locks without creation,
verify owner/type and actual account crontab use, then verify app scheduler/worker
processes are drained after acquisition. Full mode refuses unverified job locks
or forced/uncoordinated workers; document prerequisite setup for other hosts.
Foreground capture starting in strict pre-existing maintenance preserves it;
unsafe pre-existing maintenance refuses full mode. Scheduled capture skips it.

Use transient private client defaults; `--defaults-file` is the first option.
Known-native capture uses established transactional/quick/hex-blob flags. Refuse
visible-nonzero routines/events/views/triggers or non-InnoDB tables; inventory
query failures refuse. Visible-zero is accepted with explicit visibility limits
and recorded current-user grants, without claiming hidden objects do not exist.
Do not add export flags requiring unavailable mysql.proc privileges. Verify trigger support and
dump completion against real supported clients. A second stability dump streams
to a hasher, never another secret file.

Preserve `.env`/APP_KEY, Passport keys, native storage, release/extension files,
ACLs/xattrs and symlinks. Work/output stays outside app and every docroot. Exclude
only this run's new maintenance files and declare exclusions in the manifest;
preserve pre-existing marker files. This is an app/database backup, not a backup
of host/Plesk/cron/Redis/object storage or remote provider actions.

## Durability and recovery

Private roots bind canonical app identity, UID and immutable installation UUID;
directories are0700, files0600, process umask077. Enforce canonical destinations,
private-root byte budget, minimum reserve and bounded outputs. Filesystem free
space does not establish a subscription quota; document quota assumptions.

Atomic/fsynced journals record phase, initial state, safe error code, exact
maintenance marker `(device,inode,size,mtime_ns,ctime_ns,SHA256)`, process/child
identities and groups, boot ID and deadlines. Separate capture, service recovery
and delivery success. No credentials enter safe journals or public output.

SIGINT/SIGTERM perform bounded cleanup. Linux child processes use PDEATHSIG with
parent recheck and verified process-group closure. A held run lock is the
authoritative live-run guard; PID/starttime/boot ID support diagnosis and safe
group handling. Recovery never removes markers merely because bytes match.
Remove only owned unchanged down first, then bootstrap, and preserve original
maintenance or operator replacements. Uncertain ownership/process state stays
visible and closed.
Normal release uses exactly the same identity-checked cleanup as recovery;
document the small remaining check/unlink race. Recovery acknowledgement records
an operator resolution without touching application files and unblocks later
runs only through that explicit resolution.

Native tick auto-recovers only when run lock is available, recorded groups are
empty and exact marker identity matches. Manual independent recovery covers
cases where Laravel/DB cannot boot. A continuing host/application outage remains
an operator recovery case; no always-on watchdog guarantee is claimed.

Restore prior service state and release coordination locks after safe capture,
then compress immutable payloads into one tar.gz. Full content/roundtrip checks,
delivery and retries happen after reopening. Publish complete artifacts only
after integrity verification and fsync; retain failed/unresolved runs separately.

## Storage and retention

Local completion and remote delivery are distinct. S3 uses pinned isolated
boto3/botocore, verified TLS, endpoint/region/bucket/path-style configuration,
bounded retries/timeouts and multipart. Default request/response checksum
policies to `when_required` for compatibility. Verify whole-object SHA256 through
streamed GET; never substitute multipart ETag/composite checksums. Manifest is
published last. Preserve local data on delivery failure.

Prefix includes installation UUID. Retention previews then deletes only verified
owned run/object IDs, obeys configured local/remote policy and preserves the last
known good copy. Track incomplete multipart work. Destination-test is explicitly
requested and performs owned probe/readback/cleanup only. Bucket policy is never
modified. Document least-privilege credentials, optional versioning/Object Lock,
and plaintext archives containing private data and APP_KEY.

## Test requirements

Meaningful tests cover real filesystem archive/checksum roundtrip, real process
groups/lock inheritance/signals/parent death, billing-window refusal, initial and
rewritten markers, admission failures, secret non-disclosure and native settings
preservation, S3 SDK fixture upload/readback/multipart/retry/retention scope, and
fresh native extension/config/schedule/admin/CLI discovery in a disposable pinned
official Paymenter release.

An end-to-end disposable MariaDB restore drill must restore full app/SQL into new
paths, preserve APP_KEY, decrypt a known setting, load Passport keys, show no
pending native migrations and render native login. Restore instructions cover
queued work duplication, webhook redelivery and unsupported routines/definers.
Use disposable applications, databases and storage for tests.

The project uses MIT and includes third-party notices. CI runs tests, PHP lint,
reproducible builds and native integration tests. Dependency updates use the same checks.
