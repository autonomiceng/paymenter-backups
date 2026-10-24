# Architecture and build contract

This document defines the implementation contracts and consistency guarantees.
Settings v2 starts with both automatic schedules off and local storage on.
Full checkpoints and online database snapshots have independent schedules. Implementation and tests use
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
idempotent `deliver`, `retrieve ID [--execute]`, `retention plan|apply`,
`delete ID [--location local|s3|all] [--execute]`,
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

Both CLI recovery-key export and ordinary recovery-kit export read current
native document roots through the cold bridge's `document-roots` verb before
checking an output destination. That read validates application, owner, private
root and installation identity, and refuses malformed or unavailable roots;
cached `policy.json` cannot authorize an export. It returns no credentials and
does not decrypt the credential store or read billing settings. Export privacy
checks require no capture tools or capture readiness, so a failed `init` cannot
leave a newly declared public directory eligible for plaintext export.

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

The persisted `settings_version` is read before defaults: absent/1 preserves
0.3 scheduling, storage and total-count retention; 2 adopts independent policies.
The Python `from_legacy` and PHP `Settings::fromLegacy` adapter contract is
`tests/fixtures/settings-migration.json`. Adapter output proposes v2 values but
never changes the marker or writes rows. A legacy schedule maps only to its
selected mode; disabled schedules remain off with their time/weekdays retained.
Local storage maps to local only; legacy S3 maps to local plus S3. Counts/policies
copy to both kinds, full assumptions map by AND, and acknowledged legacy S3
stays plaintext until explicit adoption. Existing program/lock pins are kept.

V2 defaults: full off, 03:00, Sunday [6]; database off, hourly interval 1 at
minute 0, daily/weekly time 02:00, weekdays [0,1,2,3,4,5,6]; timezone UTC.
Full allows off/daily/weekly; database additionally allows hourly, with local
hours divisible by 1,2,3,4,6,8 or 12 at minute 0-59. Weekly requires days 0-6.
`mode` remains the terminal default. `keep_local=true`, `upload_s3=false`; at
least one must be true. `destination` is derived as s3 iff uploads are enabled.
Local counts are full 7/database 24; S3 policies default to local-count, with
inactive independent counts 30/168 and ages 90/90 days. Positive counts are
required even for S3-only storage. Empty v2 program paths select trusted
candidates; nonempty paths pin. Python >=3.10, PHP CLI >=8.3, GNU tar and supported
MariaDB/MySQL client identities are checked with bounded probes. Configured and
resolved paths remain distinct. PHP detection follows the running CLI or the
FPM version's PHP_BINDIR/php, with no fallback to another system PHP.

`s3_encryption=true`, `gpg=''` (detect). Plaintext acknowledgement is required
only for uploads without encryption; explicitly turning encryption off requires
an acknowledgement in that submission. `deliver_payload` and `checked_remote`
dispatch by recorded remote format to the encrypted delivery and full
decrypt-and-verify implementation. Missing
encryption setup never permits a plaintext fallback.

Native setting rows contain nonsecrets only. Store access key, secret and session
token and optional maintenance probe Basic credentials in a private file encrypted with the native Illuminate Encrypter/APP_KEY.
The default native extension edit loads all setting values into browser state,
including undeclared rows, so secrets must never be stored there. Custom secret
fields are blank/write-only and reset after save. The page runs the shared
pure credential and archive-secret validators before persistence, including
UTF-8 byte limits and Unicode control-character refusal. Native settings edits
preserve the external credential store.

Credential headers bind schema version, installation UUID, cipher and an HMAC
key fingerprint; support native previous keys. A mismatch/decryption failure
returns `credentials-unreadable` and never silently overwrites the file. The
installation UUID lives outside native rows and survives extension uninstall.
An independent restored/new host gets a new UUID; adoption requires the old
installation to be decommissioned. APP_KEY encryption protects a separately
leaked file, not compromise of the app UID. The Backups page edits nonsecret
settings and handles credentials, requests and status; it clears secret input
on failure. The native extension form retains lifecycle controls only.

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
uses engine normalization and validation plus the backup-directory location
rule without writing. `config validate` checks saved values against the same
schema. Billing guards, billing windows and job locks are checked when queued
work runs; they never block saving settings. The page passes changes and entered credential groups to
`Runtime::saveConfiguration`, backed by CLI `config set`. The engine acquires
`config.lock` then `tick.lock` with one ten-second acquisition deadline, or
refuses `busy` before publication. It rereads and validates the authoritative
native baseline under those locks. Only changed native rows are written through
Eloquent; the internal Artisan writer inherits the owning lock descriptors and
restores the authenticated page actor, with console auditing explicitly enabled.
The resolved database connection travels to scrubbed children on private stdin.
The Python runtime passes only Laravel's fixed `DB_*` connection fields to
Artisan during bootstrap, when extension discovery needs the database before
the command can read stdin. Other inherited environment values stay scrubbed;
database credentials are not written to files or command arguments. Full
capture passes the same allowlist to Artisan's maintenance command. The billing
guard runs inside Paymenter's ordinary scheduler process, not a scrubbed child.
The browser cannot supply the baseline or actor. The writer returns persisted
row receipts and a full native snapshot. Initialization and privacy-policy
publication remain beneath the same locks, including a first save with no edits.

Before publishing entered secrets, a second native transaction locks every
extension settings row, verifies the full snapshot, then invokes the cold
`credentials-merge` bridge while the row locks remain held. A concurrent native
editor therefore either precedes the snapshot comparison (`config-changed`) or
waits until credential publication has finished. Schedule synchronization follows
successful credential publication; rejected initialization/credential saves do
not reset an unchanged schedule epoch or supersede its waiting requests.
Worker, direct-command and storage reloads acquire `config.lock` around the whole
native-settings/credential snapshot. Tick uses config-before-tick lock order.
Standalone initialization also rereads settings and writes policy/schedule under
these locks; direct Artisan saves use the same coordinator.

A definite failure before credential commit compares and restores each unchanged
published row under a database row lock. Concurrent updates remain, and formerly
absent rows are deleted through audited Eloquent. Rollback failure or concurrent
changes are explicit partial outcomes. Before publication, the coordinator
snapshots the bytes and existence of `schedule.json`, `policy.json` and
`configuration-advisory.json`. A definite rollback restores these exact snapshots
and fsyncs their publication while both coordination locks remain held, including
failures after schedule synchronization or intent removal. If concurrent native
rows survive rollback, synchronization starts from the restored schedule snapshot
and changes only affected epochs; privacy policy reflects the surviving rows.
Waiting obligations from unchanged schedules retain their original epochs.
A private, secret-free `config-save.json`
intent exists before publication. If a child loses its receipt or times out
across a possible commit, the engine keeps this intent and returns
`outcome_unknown`; it never restores destination rows across an ambiguous
credential commit. Credential-bearing loads refuse
`config-save-outcome-unknown`, including already-locked tick reloads, and overview
shows setup required. A subsequent complete Save must resubmit each credential
group requested by that intent (or explicitly clear it), then removes/fsyncs the
intent. Settings-only Save and `init` cannot clear an uncertain secret outcome.
A rejected retry preserves the earlier intent even when the retry's own native
rows roll back successfully. Only confirmed publication of every requested
credential group, including explicit clearing, resolves that earlier uncertainty.
Intent-removal fsync failure republishes the guard before reporting uncertainty;
workers remain paused if rollback or repeated cleanup also fails.
A successful credential commit followed by a setup/advisory failure keeps the
coherent settings/keys and reports an explicit partial success.

The short native writer/finalizer runs beneath an owned Linux supervisor group.
The supervisor retains the inherited coordination descriptors, kills the PHP
writer and its credential bridge descendants on timeout, and uses a
parent-death signal to kill the whole group if the engine disappears. A retry
cannot acquire released coordinator locks while an orphan credential child can
still commit. Public output remains fixed codes and confirmed outcome flags.

Settings-only `config check`, `init`, CLI settings updates and schedule updates
use bridge `read-settings`, which never opens credentials.json or constructs an
encrypter. The pure `credentials-check` validates submitted groups before any
native publication. `credentials-merge` accepts `{s3: object|null, site: object|null}`;
null/omitted groups preserve saved values, `{clear:true}` removes only that
namespace, and missing session_token clears the previous token when replacing
S3 keys. One credential flock covers decrypt/merge/encrypt/fsynced publication
and preserves archive keys and other namespaces. Recovery-key creation remains
a separately reported operation after the coordinated configuration save.
The page refuses to change an initialized private root, because a new root
would start a new identity without the saved keys and history. When the engine
cannot start, only private root, document roots and Python/PHP pins can be
repaired, without secret publication; an initialized root still uses the bounded
configuration/tick locks. The Backups page is the sole web settings editor;
the native Extensions page retains enable/disable and uninstall, exposes no
Backups setting fields, and its Save hook performs no configuration or private
state work. CLI native writes validate against the internal `Settings::fields`
schema independently of `getConfig()`.

The private root is owned by the site user with mode 0700. Before the first
installation identity is written, only Backups' `identity.lock` and
`billing-guard.json` may already exist; each must be a regular owner-private
file. Any other entry, including saved credentials or backup sets without an
installation identity, remains an orphaned-state refusal.
Malformed public-folder lists remain refusing until an administrator explicitly
enters and confirms the complete list on Backups settings, including an
intentional empty list. Bootstrap repair validates those folders before writing,
compares the exact native row snapshot again under native row locks and, for an
initialized root, configuration/tick locks. Fresh repair uses a native snapshot
transaction before initialization. It preserves an initialized directory and identity, and publishes no
secrets or settings-version adoption. Ordinary Save never repairs this guard
from defaults. Until repair succeeds, exports still refuse unknown boundaries.
Implicit default repairs for unrelated damaged settings are deferred until the
next ordinary Save, so they cannot block a folder-list-only bootstrap repair.
A saved private-root pin that is unusable because it is relative, contains
dot segments or names a symlink is corrected through the standalone CLI,
explicitly bound to the existing canonical private directory. State validates
its application/owner/identity; the operator first verifies the known UUID with
offline status. Config-set refuses a proposed root different from that bound
directory. With that same canonical directory, correction preserves UUID and
credentials and moves no private state. A guessed directory is not an identity
proof; config-set can initialize fresh state when explicitly selected.

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
S3-only backups offer a queued Download from S3 action. The worker verifies the
remote archive and manifest, then publishes a local copy through a held,
no-follow directory descriptor without overwriting existing names. Retrieval
uses the local byte budget and disk reserve. A successful retrieval protects
that local copy from automatic retention for 24 hours, and does not immediately
run retention. The native Download link becomes available afterward. PHP never
waits for an S3 transfer or exposes storage credentials or provider URLs.

`delete ID` previews the selected local, S3 or both copies. The default location
is `all` for CLI compatibility. Execution holds the run lock and requires
another fully verified survivor at every location being removed. It refuses
unbound or changed remote locations and conflicting tasks. Local-only deletion
can proceed while an earlier S3 destination remains outside the current scope.
Each location has its own durable deletion intent and manifest snapshot.
The worker removes the manifest before its archive at each location. An
interrupted S3-only deletion leaves a usable local Download available.
Before each local unlink it re-reads the name
with `fstatat` through that descriptor and requires the full identity (device,
inode, size, mtime_ns, ctime_ns) that the same attempt checked, so a file
modified in place or recreated on a reused inode number is not mistaken for the
checked one; a replaced or modified file is kept and the attempt fails with
`delete-local-verification-failed`. The stat-to-unlink step is not atomic
against a hostile same-UID writer; the owner-private directory assumption
still applies. A failed attempt resumes from its
intent. Whole-run deletion is recorded only when no known copy remains.
Retention ignores runs with a manual delete intent, and per-backup
deletion never changes retention settings.

## Scheduling and admission

The native tick runs every minute with `evenInMaintenanceMode`; register lazily
in console boot after Schedule resolution. Paymenter's server setup owns the
systemd timer or cron job that invokes its scheduler. Backups never installs,
enables, disables or edits those host jobs. Help links to Paymenter's
installation guide and explains this ownership.

V2 schedule.json holds schema_version 2 and `kinds.full` / `kinds.database`,
each with fingerprint/since. Legacy state keeps schema_version 1 unchanged.
Saving a changed schedule records a private fingerprint and start time per kind;
timezone changes affect both, unrelated edits affect neither.
Enabling or editing the schedule starts with the next future slot. After an
outage, the tick chooses only the latest due slot and queues one fresh snapshot,
without creating a backlog for earlier slots. A weekly lookup is bounded to
eight days; queued task expiry starts at creation, not at the old slot's time.
Unstarted requests from a disabled, edited or superseded schedule are cancelled.
Request files are atomic/fsynced, identified by installation, kind, schedule
epoch and wall slot, with explicit mode/schedule_kind. Each kind catches up its
latest slot within eight days; supersession never crosses kinds. Hourly slots
skip DST gaps and the second occurrence of repeated wall times. Least recently
attempted eligible work is selected fairly with a 60-second retry delay; a new
same-tick full obligation wins the initial tie. Legacy mode-less waiting requests
are frozen before terminal default edits.

Immediately before the SQL dump, capture persists database_snapshot_lower_bound.
After archive verification and service recovery it persists database_coverage
before delivery. Reconciliation marks only waiting scheduled database requests
with an earlier/equal slot `satisfied`, naming covered_by. Before creating a new
satisfaction receipt, it holds the run lock through current local archive
verification or full remote decrypt-and-verify proof and the locked request
write. The short tick accepts only an owned local descriptor whose identity and
manifest match its recorded verification; it never hashes an archive or contacts
S3 for coverage. If that proof is unavailable, the database request stays
eligible to launch. Capture and worker reconciliation can perform complete local
or remote verification. Deleted generations and unfinished manual deletions
cannot cover new requests. Failed/unrecovered or pre-slot full captures never
cover. Manual requests are never absorbed. Existing satisfaction receipts survive later deletion,
upload failure, staging removal and restart. A remote-only generation can cover
a new request only when its surviving copy passes current verification.
Scheduled metadata is part of the first locked request write, so cancellation
cannot be overwritten by a later metadata update. CLI configuration writes
hold the scheduler lock through schedule synchronization and refuse before
writing if the lock stays busy for ten seconds. Settings initialization also
waits at most ten seconds for this short scheduler lock. The launcher and initialization re-read settings
after acquiring that lock so an earlier read cannot restore an older schedule.
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

V2 full admission uses the same sanitized discovery/parser module as overview.
The tick refreshes owner-private discovery.json no more than once every five
minutes; overview only reads it and labels data older than 15 minutes stale.
Capture always rediscovers and validates; cached diagnostics never authorize it.
Cache command revalidation can add a refusal but preserves service-derived
coordination and its reason, including a finite worker kept continuous by systemd
restart policy.
Inventory covers the account crontab and system/user systemd timer targets and
active/enabled standalone services, with bounded reads and plain-table fallback.
Cron suggestions escape every `%` as `\%`. Discovery decodes that cron escape
before shell parsing, preserves literal backslashes and refuses an unescaped
`%` because cron would turn the remainder into stdin. Shell and systemd parsing
remain separate from this cron preprocessing. Discovery parser version 3
invalidates older cached interpretations and requires rediscovery.
No scheduler, failed/incomplete discovery, unsupported/uncoordinated jobs,
continuous workers, unsafe/missing locks and explicit-list mismatch refuse.
Empty v2 job_lock_paths derives the verified union; nonempty lists must match.
Locks open without creation, in stable order, and identity/remaining workers
are rechecked after acquisition. init creates only the suggested scheduler.lock
and queue.lock as 0600 owner-private regular files and validates existing
descriptors without acquiring these launcher locks. Only full-mode admission
and capture coordination acquire launcher locks; held launchers do not block
database-only capture, settings saves, schedule changes or initialization.
Backups never edits host launchers.
Legacy `systemd_locks` skips an explicit `activates: null` timer entry: it has
no target and cannot launch `schedule:run` or `queue:work`. Other entries must
name one valid service; missing or malformed targets still refuse with a
bounded `detail["activates"]` diagnostic. A null entry does not bypass lock
verification for other timers.
The parser accepts documented /dev/null redirection tails, common PHP/Artisan
flags and hosting PHP basenames, with shell expansion/wrappers refused.
For an application at `/app`, it also accepts exactly
`/usr/local/bin/php /opt/beco/scheduler.php` behind the same explicit flock and
owner-private lock checks. This guarded scheduler takes no interpreter or script
arguments. Other interpreters, script paths and application roots refuse.
The script must be a root-owned regular file with no group or world write bits.
Its canonical path must remain exactly `/opt/beco/scheduler.php`, with no file
or parent-directory symlinks. The canonical-path and `lstat` regular-file checks
follow `discovery.lock_safe`; script ownership and write restrictions differ
from that helper's app-owned, exactly 0600 lock policy. Discovery, cache reads
and admission recheck the script. Legacy cron and systemd validation use the
same parser. An unsafe script remains visible as unsupported with its guarded
command, so diagnostics cannot suggest replacing the guard with Artisan.
Normalized commands, cached diagnostics and replacement suggestions preserve
the guarded launcher and its lock, rather than replacing it with Artisan.
Commands are reconstructed from recognized nonsecret tokens; ini values, raw
cron/service/process text and process identities never enter discovery records.

V2 full plan also checks effective cached framework stores: database-backed
stores must use the captured connection; file stores and a local filesystem
disk must resolve within the app. Unsupported and unknown stores refuse full.
The full assumptions confirmation still covers custom extensions, direct Redis,
other connections, panel/Supervisor launchers and undeclared writers. Both kinds
retain the no-concurrent-DDL confirmation; absence of visible migrations proves
nothing. Busy/billing/job-lock states are waiting, recovery is needs-recovery,
other refusals are needs-setup. Bounded running jobs retain coordination's wait;
continuous workers are setup blockers.

## Capture modes

`database-online` produces a transactional InnoDB SQL snapshot without
maintenance, under a no-concurrent-DDL condition. Its manifest clearly excludes
an atomic application/files snapshot and external provider state.

`full-quiesced` acquires a private backup lock and verified job coordination
locks, enters native prerendered maintenance, drains existing requests/jobs and
captures SQL plus the full app while controlled writers remain excluded. Idle
observations choose a quiet time; they never substitute for admission control.
Refuse unsupported engines, unaccounted durable external state, external
symlink targets and uncontrolled writers. Document the no-DDL, bounded-request
and cooperative-writer assumptions. Never kill provider work to achieve idle.

After native down, require file driver, empty exceptions, no secret/redirect,
nonempty rendered template and status 503; verify an actual early-bootstrap HTTP
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
canonical/parity cases; IPv4-mapped IPv6 and non-ASCII hostnames refuse. A 10-second
wall-clock deadline covers response headers and exact bounded body proof, including
slow-drip peers. Native templates above 64 KiB refuse before any request. An existing
caller SIGALRM timer is preserved and causes proof refusal. Only exact 503/template bytes prove success;
401, ordinary pages and redirects refuse. Credential writes serialize the entire
read/merge/encrypt/atomic-write under one owner-private flock, preserving S3 and
probe namespaces independently; explicit probe clear preserves S3. Open configured job locks without creation,
verify owner/type and actual account crontab or systemd timer service use, then verify app scheduler/worker
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
Capture children calculate a remaining output ceiling at admission and enforce
it with the inherited file-size limit. Each poll checks the current output's
size and, during full capture, the absolute deadline. Filesystem reserve checks
run at most once per second or per 64 MiB; polling never rescans private history.
Every successful child forces a reserve check and full private-history
accounting before advancing or publishing. Full capture checks
its same absolute maintenance deadline after admission accounting, while polling,
and after completion accounting. Accounting grants no new capture deadline;
expiry refuses advancement or completion under the original bound. Synchronous
filesystem or accounting work can delay the start of cleanup after expiry.

Atomic/fsynced journals record phase, initial state, safe error code, exact
maintenance marker `(device,inode,size,mtime_ns,ctime_ns,SHA256)`, process/child
identities and groups, boot ID and deadlines. Separate capture, service recovery
and delivery success. No credentials enter safe journals or public output.

SIGINT/SIGTERM perform bounded cleanup. Linux child processes use PDEATHSIG with
parent recheck and verified process-group closure. A held run lock is the
authoritative live-run guard; PID/starttime/boot ID support diagnosis and safe
group handling. Recovery never removes markers merely because bytes match.
Streaming dump hashes use the same fallback group registration and successful
group-closure check as other capture children. A bounded cleanup wait expiring
does not replace the original child-timeout error.
If initial start-time inspection fails, immediate cleanup can still kill the
new session's group while its owned, unreaped `Popen` child pins the PID/PGID.
It signals before reaping; this ownership proof never applies to persisted
groups during independent recovery.
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
after integrity verification and fsync. Retain failed/unresolved journals for
recovery evidence, but remove owned temporary payloads and unpublished partial
archives after failure. Published archives and recovery markers remain subject
to their separate ownership checks.

## Storage and retention

Local completion and remote delivery are distinct. S3 uses pinned isolated
boto3/botocore, verified TLS, endpoint/region/bucket/path-style configuration,
bounded retries/timeouts and multipart. Default request/response checksum
policies to `when_required` for compatibility. Verify whole-object SHA256 through
streamed GET; never substitute multipart ETag/composite checksums. The commit
descriptor is published last. Preserve local data on delivery failure.

New installation bindings choose PRIVATE_ROOT/backups. V1 bindings without the
directory field keep PRIVATE_ROOT/artifacts. For v2, empty local_path selects
PRIVATE_ROOT/backups for future captures even with an old binding. Explicit
local_path overrides this. Recorded old paths, UUID and binding bytes are kept.
Space accounting includes earlier journaled archive directories after a path
change, as well as the private data directory.

Prefix includes installation UUID. Remote ownership records endpoint, region,
bucket, prefix and addressing style before upload. A changed destination never
silently changes the recorded ownership. Legacy unbound copies cannot authorize
destruction; an explicit verified delivery retry can bind the current location.
A surviving legacy archive can restore a missing manifest from its owned intent
only after full verification and safe descriptor-relative publication.
Multipart work carries the same provider binding before upload. Legacy unbound
multipart records refuse before provider calls and retain their journal. The
operator can resolve them through the known provider or a bucket rule that
aborts incomplete multipart uploads; changing settings cannot establish that
missing ownership evidence.

V1 `retention_keep` remains total newest-N. V2 partitions by the recorded mode
in agreement with its owned manifest. Unknown/inconsistent mode never permits
deletion or survivor substitution. Local keep sets are newest verified N of
that kind; ordinary local/S3 deletion requires another fully verified survivor
of the same kind at each affected location. The newest S3 copy of each kind is
protected. S3 local-count uses the configured count for that kind even when
keep_local=false. lastmanifest.json retains its latest-overall meaning.
Executing retention clears an interrupted retention deletion intent on the
newest remote run of each kind and keeps that remote copy. Manual deletion
intents and independent local deletion intents retain their own lifecycle.
Pointer publication reads the current remote pointer and compares the named
run's creation time with the candidate under the run lock. It never replaces a
newer usable run with an older one, even if local delivery flags are stale. A
recorded missing, damaged, deleting or pruned target permits repair to a present
copy; `remote_delivered=false` alone does not. Missing or
invalid ownership evidence refuses publication. Every pointer write is read
back and checked by whole-object SHA256 and size before reporting success.

For S3-only storage, delivery's held run-lock attempt proves provider scope,
whole-object SHA256/size, matching manifest and archive contents, then journals
staging_removal_intent before manifest-first descriptor-relative local unlinks.
Historic delivered flags and HEAD/ETag never permit cleanup. Retries reprove the
remote copy and resume partial unlinks, including after staging is already gone.
The journal remains remote-only, never whole-run deleted. Failed/uncertain upload
keeps staging. Retrieved copies keep their 24-hour lease; expired retrievals may
use the same-run remote proof for cleanup. This exception never applies to
manual Delete. Budgets still include staging, retrievals and older directories.
Provider lifecycle rules can independently remove remote copies.

For each kind, independent S3 policies are
`local-count`, `count`, `age` and `keep-all`. The compatibility default
`local-count` preserves the previous configured count. `s3_retention_keep=30`
and `s3_retention_days=90` are inactive until their policy is selected. Age is
measured from backup creation in UTC seconds. Cleanup runs after successful
capture/delivery or an explicit Delete old backups now request on the page; it
does not configure bucket lifecycle rules or promise deletion at an exact
expiration instant.

Retention deletes only verified owned copies and protects a fully verified
survivor independently at each location. Failed uploads preserve their local
sources. Damaged, missing and out-of-scope copies are reported and skipped
without blocking unrelated cleanup. Local and remote intents resume separately.
Both retention previews and execution verify local archives per run. A failed
verification or filesystem read records the run ID and safe error code in
`damaged`, excludes that run from the kept set and all automatic deletions, and
continues with the other runs. Damaged runs require an operator decision.
Already journaled partial local unlinks resume through their owned deletion
intent rather than being treated as newly damaged archives.
Kept S3 copies use cheap presence checks; full archive proofs are reserved for
deletion targets, required survivors and local-removal delivery checks, cached
within the held-lock attempt. Lifecycle disappearance repairs the latest-copy
pointer using an available present copy; a confirmed-missing, damaged or removed run
does not block that repair. This pointer is untrusted discovery
metadata; it never substitutes for full survivor, retrieval or deletion proof.
Retention reports skipped copies
to the overview. If admission fails only because the local budget is full,
automatic local retention runs once before capture re-admits, without entering
maintenance first. Track incomplete multipart work. Destination-test is explicitly
requested and performs owned probe/readback/cleanup only. Bucket policy is never
modified. Document least-privilege credentials, optional versioning/Object Lock,
and plaintext local archives containing private data and APP_KEY.

### Encrypted remote artifacts

Capture and storage mutation entry points reload the current configuration and credentials
after acquiring the run lock. Automatic retention also checks the current
automatic-retention setting under that lock, after capture/delivery, so a Save
that changes storage or retention while capture runs governs subsequent cleanup.
Callers retaining a read envelope supply its loader when concurrent Saves are
possible. Capture preserves the requested backup type while refreshing its
settings before admission and budget-triggered cleanup. A configuration Save after that reload applies to the next operation;
an admitted operation uses its consistent snapshot.

`encryption.py` owns the pinned GnuPG profile and private subprocess transport;
`remote_artifacts.py` owns versioned object names, public descriptors, encrypted
recovery documents and frozen delivery preparation. Storage calls these at
explicit remote lifecycle seams. Local tar.gz manifests and Download retain
their existing contract.

Settings use `s3_encryption` (default true for v2) and `gpg` (empty detects
`/usr/bin/gpg`, then PATH; a nonempty value pins the executable). Missing tools,
keys or acknowledgement affect delivery readiness, not local configuration
initialization or legacy plaintext retrieval. Legacy v1 interpretation belongs
to the settings adapter: accepted plaintext S3 uploads continue until the
administrator saves v2. Plaintext upload requires explicit current plaintext
acknowledgement. Encryption never falls back to plaintext.

The fixed remote format is `openpgp-symmetric-aes256-seipd-v1`: binary symmetric
AES256, SEIPD v1/MDC, salted iterated S2K mode 3/SHA256/count 65011712, no extra
compression, source filename, recipients or signatures. `--rfc4880` precedes
algorithm options. Secrets travel over private pipes, never arguments,
environment or temporary passphrase files. GnuPG uses an owner-private home,
bounded concurrent input/output/status/stderr draining and deadlines.
Agent readiness uses the socket path reported by the trusted adjacent gpgconf,
including runtime-directory socket placement. Cleanup removes only this unique
homedir's socket directory after its owned agent stops. Encryption accepts the
optional zero AEAD status field used by newer GnuPG, while refusing nonzero AEAD
and any profile other than AES256 with MDC.
Ciphertext spools and plaintext or encrypted retrieval outputs calculate remaining byte budget once
before opening the output, enforce that ceiling while writing, and account the
complete private history again before publication. In-stream reserve checks
inspect filesystem free space at most once per second or per 64 MiB, plus
completion; they never reread journals per chunk. Encrypted transforms/proofs
run outside the maintenance and billing hold with a separate deadline: five
minutes plus one second per 256 KiB of the recorded input, capped at 24 hours.
The capture deadline remains unchanged. Per-request S3 network timeouts still
apply, and output publication requires the unchanged complete checksum/archive/MDC proof.
Only owned helpers may be supervised or stopped; personal GnuPG state and account
wide helper shutdown are prohibited. Successful decryption requires exit zero,
expected `DECRYPTION_INFO`, `DECRYPTION_OKAY`, completed decryption, and no MDC
or profile failure. Early plaintext output is untrusted until completion.

An encrypted generation has `RUN.tar.gz.gpg`, `RUN.recovery.json.gpg` and
`RUN.manifest.json`. The encrypted recovery document contains the full original
manifest and binds the archive ciphertext digest/size, exact object name,
installation/run, provider scope and fingerprint. The public descriptor contains
only format/identity, key fingerprint parameters and ciphertext names/digests/
sizes. It excludes grants, payload hashes, app paths and plaintext archive
hashes. Both documents have fixed bounded schemas; provider-supplied names
cannot override the journal's exact inventory. `lastmanifest.json` is discovery
metadata referring to the descriptor, never authority for provider scope or
freshness.

Delivery first fully checks the local archive, freezes private ciphertext
spools and their identities/digests, checks a decryption round trip, and
persists immutable format, key and provider binding before uploading. Every
spool byte counts toward the local budget and minimum-free-space reserve.
Retries reuse frozen bytes; missing or altered required spools refuse rather
than regenerating a committed generation. After exact successful delivery,
identity-checked spools are removed and immutable transport metadata remains;
an idempotent retry accepts missing spools only if all required remote bytes
already match. A process crash during unfinished preparation retains a refusing
intent for operator resolution; caught preparation failures remove only owned
unpublished spools. Upload/readback order is archive,
encrypted recovery document, then descriptor. Deletion reverses publication:
descriptor first, recovery document next, archive last, with journaled resumable
absence handling. An interrupted uncommitted generation can be removed only
after checking its full local/frozen recovery proof and every extant owned
ciphertext object; absent unpublished objects do not block that cleanup.
Failed or uncertain uploads retain the local source.

Delivery and destination testing take the run lock before invoking a trusted
`envelope_loader` callback to refresh settings and credentials. CLI and queued
workers supply that callback; callers providing an already read envelope must
also supply a loader when concurrent configuration/key changes are possible.
Plaintext authorization and delivery retain the same continuous lock. A
prepared intent always keeps its recorded fingerprint across rotations.

Retrieval decrypts into private unpublished output and checks ciphertext and
plaintext SHA256/size, successful GnuPG/MDC completion and full archive contents
before descriptor-relative local publication. The existing no-overwrite rule
and 24-hour retention lease apply. Survivor and staging-removal proofs perform
the same full restore check under the held run lock. Ciphertext hashes or HEAD
presence alone cannot authorize removing a local copy. Each copy selects its
recorded fingerprint, including retired keys; a missing key preserves affected
copies. Recognized pre-encryption records resolve as `plaintext-v1` regardless
of current settings; malformed or unknown explicit formats refuse.

Destination tests use the selected active acknowledged key for an encrypted
synthetic upload/readback/decrypt/compare/cleanup round trip. Test evidence binds
provider scope, format and fingerprint; key rotation invalidates older evidence
without changing provider ownership. Overview destination-test requests expose
only the authoritative boolean `result_verified` beside `result_destination_id`.
It is true only for a completed successful test matching the current destination
binding and remote format, plus the active acknowledged fingerprint for encrypted
S3. Legacy plaintext tests become stale when encryption is enabled; activation
of a rotated key makes earlier encrypted tests stale. A pending rotation preserves
a test using the still-active key. Other request actions do not gain this field.

### Archive key credentials and PHP interface

The APP_KEY-encrypted credential payload adds `archive_encryption` version 1,
with `active_fingerprint`, `pending_fingerprint` and fingerprint-indexed `keys`.
Records hold secret, source (`generated` or `passphrase`), fingerprint parameters,
creation time, state and an optional acknowledgement bound to that fingerprint.
The private bridge envelope exposes it only as
`archive_encryption_credentials`; ordinary output redacts the entire namespace.
All credential mutations preserve S3 and maintenance-probe namespaces under
the existing shared flock. Unreadable credentials are never replaced. Generate
creates 32 random bytes as 64 lowercase hex characters; supplied passphrases
preserve exact UTF-8, require 20 characters, forbid controls and cap at 1024
bytes.

Fingerprints use `pbkdf2-hmac-sha256-v1`, PBKDF2-HMAC-SHA256 with 600000
iterations, 32-byte output, and salt
`paymenter-backups-key-fingerprint-v1\0` followed by 32 random bytes. Exported
identity imports preserve that salt. A short fingerprint is display metadata;
full fingerprints authorize acknowledgement. Rotation stages pending, reveals/
exports, acknowledges, then activates under run-lock-before-credential-lock.
Retired keys stay available indefinitely. APP_KEY rewrapping preserves every
namespace and archive secret. The page freezes the full pending fingerprint
when an acknowledgement dialog opens and submits that identity; an intervening
key replacement refuses instead of acknowledging the new key.

The page consumes these static Runtime methods:

- `encryptionKey('generate'): array` and
  `encryptionKey('import', ['passphrase' => $secret]): array` return
  `{fingerprint, short, state: "pending"}`.
- `encryptionKey('acknowledge', ['fingerprint' => $fingerprint]): array`
  includes activation; `encryptionKey('discard-pending'): array` discards only
  the pending key.
- `recoveryKeyFingerprint(): ?string` reads the currently selected full
  fingerprint through the credential-locked status bridge, without key material.
- `revealRecoveryKey(string $fingerprint): array` returns `{secret, fingerprint}`
  solely for a confirmation-bound sensitive response; never persist it in
  Livewire state. The grant stores the full fingerprint, and the credential-locked
  bridge refuses replacement, disappearance or changed selection before audit
  and disclosure. Runtime and the page also require the returned identity to
  match the grant.
- `recoveryKeyUrl(): string` supplies a five-minute signed admin attachment
  URL. The response checks `admin.extensions.update`, records secret-free audit
  evidence before releasing material, and uses no-store/no-referrer headers.

The UI owns route registration in `Admin/Pages/Backups.php::routes()`. It must
call `RecoveryKeyController::register(Panel $panel, string $path, string $name,
array $middleware, array $without)` from that authenticated panel route group;
the encryption module supplies the helper and controller. The recovery-key
attachment includes secret, full fingerprint/parameters, installation UUID and
restore instructions. Archive-key operations validate installation identity and
credentials independently of billing settings and capture readiness, so missing
`cronjob_time` cannot block CLI key export or the signed attachment. Neither
normal export nor overview includes the secret.

Overview exposes this whitelist:
`configuration.encryption = {enabled, tool: {ok, path, code}, key: {state,
short, source, acknowledged, created_at, pending_short, pending_fingerprint}, readiness_code}`.
Key state is `none`, `pending` or `active`; tool path may be null.
`runs[].remote_format` is `plaintext-v1`,
`openpgp-symmetric-aes256-seipd-v1` or null; `runs[].key_short` is a display
fingerprint or null. Full secret records never enter overview, journals,
requests, notifications, normal CLI JSON or stderr.

The ZIP includes standalone stdlib `runtime/verify_recovery.py` for fresh-host
transport and complete plaintext verification without Laravel, DB or the S3
SDK. [RECOVERY.md](RECOVERY.md) defines the separate successful GnuPG/MDC step
and isolated restore procedure. Run restore tests with the exact release ZIP.
Validate host configuration and providers separately.

## Keeping Paymenter upgradable

Paymenter must take new upstream releases without an extension release. Ship an extension release only when an upgrade
actually breaks something.

- **Public surfaces only.** Use the extension base classes and their hooks, service providers, our own routes and tables,
  Eloquent model events and observers, Filament registration, Paymenter events, jobs, config, and the documented theme
  mechanism.
- **Never intercept Paymenter globally.** Do not rebind or subclass Paymenter services in the container, remap native
  routes, add global Livewire, middleware or request hooks (including `TrimStrings::skipWhen` or `TrimStrings::except`),
  replace database resolvers or query builders, monkeypatch, or edit core or vendor files.
- **Never gate on the Paymenter version.** No supported-version list at runtime, and no refusal, pause or banner just
  because the version changed. Disable behavior only on evidence that it is broken.
- **Guard invariants at the write.** A scoped observer refuses only the specific invalid change, logs it, and tells
  staff. Native renewals and saves continue for everything else.
- **Check required hooks, not versions.** If a feature depends on a Paymenter class, event or listener, verify it at
  boot. If one is missing, pause only that feature and show staff exactly what is missing.
- **Pin remaining internals in tests.** Where a guarantee still depends on Paymenter internals (template overrides, a
  core base class, native write paths), extend the sha256 upgrade tripwire. It runs only in tests and CI, names the
  changed files and the code that depends on them, and is never regenerated automatically. Update a hash only in the
  same reviewed change that re-checks or fixes the dependent code.
- **Qualify releases in CI.** New Paymenter releases run the full suites, the tripwire and the override-template diff.
  A green run needs no extension release.
- **Exceptions are architecture decisions.** A change that cannot follow these rules states why in its pull request and
  adds a tripwire before merge.

Known dependency on internals: maintenance handling reads and writes Laravel's maintenance-mode file. Keep it covered
by the upgrade tripwire and the release-qualification run.

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

## Overview fields for settings version 2

The existing overview envelope stays schema_version 1. These are the exact new
fields for an available v2 configuration (types are shown below); unavailable
configuration retains `{available:false, valid:false, error:code}`. Operational
program/lock/directory paths are narrowly allowed configuration diagnostics;
archive/manifest paths, raw discovery output, secrets and processes remain absent.

```text
configuration.settings_version: 1|2
configuration.schedules:
  timezone: string
  full: {frequency, time, weekdays:[integer], next_at:number|null}
  database: {frequency, every_hours:integer, minute:integer, time,
             weekdays:[integer], next_at:number|null}
configuration.storage:
  {keep_local:boolean, upload_s3:boolean, local_path:string,
   default_local_path:string, legacy_default_path:string|null,
   s3_bucket, s3_region, s3_endpoint, s3_prefix, s3_path_style:boolean}
configuration.retention:
  {automatic:boolean,
   full:{local_keep:integer, s3_policy, s3_keep:integer, s3_days:integer},
   database:{local_keep:integer, s3_policy, s3_keep:integer, s3_days:integer},
   pending:{full:{local:integer|null,s3:integer|null},
            database:{local:integer|null,s3:integer|null}},
   last_apply:{at,skipped_count,local_removed,s3_removed}|null}
configuration.readiness:
  full-quiesced|database-online:
    {state:ready|waiting|needs-setup|needs-recovery,
     items:[{code:string,field:string|null,detail:object}]}
configuration.checks:
  jobs:{checked_at:number|null,state:ok|stale|failed|unknown,error:string|null,
        items:[{id,source:crontab|systemd,label,kind:scheduler|queue-worker,
                coordination:coordinated|uncoordinated|unsupported|continuous,
                command:string,lock:string|null,reason:string|null}],
        suggestions:{scheduler:string,queue:string},crontab_user:string}
  stores:[{kind:maintenance|cache|session|queue|filesystem,driver:string,
           env:string,supported:boolean|null,suggest:string|null}]
  tools: python|php|dump|mysql|tar|gzip|gpg:
    {configured:string,resolved:string|null,ok:boolean,code:string|null}
  billing:{time:string|null,timezone:string|null,
           window_before:HH:MM|null,window_after:HH:MM|null}
last_good: {local,s3,full,database,s3_full,s3_database}: run ID|null
requests[]: existing fields plus schedule_kind:full|database|null,
            covered_by:run ID|null; covered request status is satisfied
runs[]: existing fields plus remote_format:plaintext-v1|
        openpgp-symmetric-aes256-seipd-v1|null, key_short:string|null,
        ciphertext_size:number|null, remote_verification_level:string|null,
        staging_removal_pending:boolean, staging_removal_error:string|null
```

`configuration.destination` and `admission` remain. V1 retains singleton
schedule/retention fields and total pending counts alongside the per-kind
pending counts, and additionally shows the
adapted proposed schedules/storage and per-kind retention settings. The clock
window brackets the daily billing minute using lock/drain/capture/cleanup and
safety bounds, wrapping midnight; actual admission uses observed UTC instants.
The encryption summary uses the real delivery tool/key readiness and retains
the nonsecret pending fingerprint for acknowledgement. Encryption delivery
items (`archive-key-required`, `archive-key-save-required` and tool codes)
appear in per-mode readiness, but do not enter capture `admission_errors` or
the compatibility `configuration.admission`. Local capture remains admissible
and delivery failure preserves its local source. Overview reuses a single
current-key validation across historical destination tests and both modes.
Encrypted staging removal, expired retrieval cleanup and same-type remote
survivors require the full decrypt-and-verify proof under the held run lock;
ciphertext presence or digest alone cannot authorize local removal.
