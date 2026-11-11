# Scheduling and capture

This document defines when backups run, how requests are admitted and what each
capture mode produces. Module contracts, storage, durability and the upgrade rules
are in [ARCHITECTURE.md](ARCHITECTURE.md).

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
Unstarted capture requests from a disabled, edited or superseded schedule are cancelled.
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

A successful capture is journaled with its request ID and original delivery
scope before any upload. The request becomes `deliver` for that exact run; a
restart recovers this transition from the completed journal. Delivery obligations
survive schedule edits/supersession and request expiry. Failed delivery retries
with 60-second exponential backoff capped at one hour, using storage's frozen
ciphertext, provider scope and recovery-key generation. A changed destination
refuses before provider calls. Missing/changed spools never authorize regeneration.
No upload failure calls capture again. Undelivered generations remain protected
from automatic retention even after storage settings change, and their archive
and spools continue to count toward disk admission budgets.
Successful delivery or explicit cancellation clears that delivery obligation.
Cancelling stops retries; it does not delete a backup or abandon prepared
provider work. A frozen delivery intent still protects its files until delivery
finishes or the existing explicit Delete action safely removes the run.

Retryable capture-tool failures have a budget of at most three attempts within
the existing request age; admission contention does not consume that budget.
Interrupted workers also respect it. A failed or timed-out mysql inventory check
is terminal with `database-inventory-failed`, without automatic retry or deferral.
Scheduled extension changes skip to the next full slot instead.
Repeated capture failure needs inspection because another automatic copy cannot
repair a failing database/export tool; existing failure details identify the cause.

The tick has no cache mutex (`withoutOverlapping`/`onOneServer`); private flock
is authoritative. It invokes the short launcher with Python `-I`. Each tick
observes the actual native CronJob event's expression/effective timezone and
next eight days of UTC instants into an atomic private billing guard, fingerprinted
against cold-read native billing settings. Full admission requires a fresh
matching guard; stale/unknown billing state defers full mode and coordinated
database-only capture. Provide a booted
CLI guard-refresh command for terminal use before a scheduler tick is available.

The tick is the sole detached launcher. Foreground terminal execution is also
supported. A launcher closes inherited nonessential descriptors and starts an
independent session before running capture; verify that it owns no inherited
scheduler/worker lock. Never execute capture synchronously while holding a
caller job lock or through the ordinary queue. FPM never launches capture.

Protect the native daily billing minute: schedule validation and runtime
admission reject/defer holds overlapping `cronjob_time` in its actual timezone,
including capture bounds, lock waits, safety margin and deferral. Record
the protected minute and actual hold interval. Coordinated database-only capture
checks billing before taking any lock and again after waiting. Its inventory and
dump share capture_timeout; compression, hashing and upload follow lock release.
The database billing admission bound is max(lock_timeout, 60) + capture_timeout + 30 seconds,
plus billing_margin_seconds around the native minute. Full mode retains
lock_timeout + capture_timeout + 30.
The extra 30 seconds covers process termination and release overhead. Native
Laravel daily schedules do not catch up automatically after a skipped minute. UTC is recommended;
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
remain separate from this cron preprocessing. Discovery parser version 4
invalidates older cached interpretations and requires rediscovery.
Help preserves the first matching guarded scheduler from inventory order. Its
queue command uses the first coordinated crontab worker with a verified private
lock, otherwise the private `queue.lock` fallback. Later matches never replace
either choice. These display suggestions do not authorize capture.
No scheduler, failed/incomplete discovery, unsupported/uncoordinated jobs,
continuous workers, unsafe/missing locks and explicit-list mismatch refuse.
Empty v2 job_lock_paths derives the verified union; nonempty lists must match.
Locks open without creation, in stable order, and identity/remaining workers
are rechecked after acquisition. init creates only the suggested scheduler.lock
and queue.lock as 0600 owner-private regular files and validates existing
descriptors without acquiring these launcher locks. Full capture requires these
locks. When finite jobs have verified cooperative locks, database-only capture
takes those locks, waits up to max(lock_timeout, 60 seconds), and defers while
a guarded finite job still holds its lock. This spans the suggested queue worker's
50-second run; billing admission reserves that wait. It uses
no maintenance mode or drain. Locks keep a multi-step job out of the dump; a
transactional snapshot alone cannot make its separate writes atomic.
Continuous workers, including stock systemd `queue:work`, may keep running during
a database-only snapshot. Their lifetime lock cannot provide a boundary between
jobs, so database admission excludes it and the continuous process from its writer
check. Bounded workers with a verified cooperative lock still wait, even when
systemd restarts them. V1 with explicit locks retains the legacy finite launcher
syntax, including queue selection options; v2 retains its strict parser. Full admission
continues to refuse continuous workers.
Worker classification reads the command after `artisan`, skipping only `--quiet`,
`-q`, `--no-interaction` and `-n`, as discovery does. Queue names and other argument
values do not identify continuous workers.
A database snapshot may therefore contain a continuous job's intermediate commits;
it guarantees transactional InnoDB consistency, not atomic multi-step jobs.
Fresh discovery still verifies discovered finite locks and any explicit lock list;
discovery failures, unknown locks and unsafe finite locks defer and retry.
Only a complete fresh inventory with no finite coordination to acquire, or with no
configured/discovered coordination at all, permits capture without locks. With an
empty `job_lock_paths` setting and only unlocked uncoordinated or continuous jobs
in the inventory, this includes an unguarded finite `schedule:run`: it is neither
waited for nor excluded from the dump. Its intermediate commits may appear in the
transactional snapshot too. The page shows that these snapshots can overlap jobs.
Backups never stops a continuous worker.
Blocked flock launchers proven by kernel wait records for our held inodes are
excluded from the writer scan; actual children and uncoordinated writers remain
checked when coordination locks are held. Settings saves, schedule changes and
initialization do not acquire launcher locks.
Routine contention stays deferred without a failed run or staff alert. Database
requests waiting for jobs or failed lock verification retain their retry obligation
past request_max_age through later transient deferrals; other age limits and schedule
supersession still apply. Requests
retain their first job-wait timestamp across superseded hourly slots. Overview
exposes one job_wait_overdue boolean after a day without a completed capture;
the page asks staff to inspect stuck scheduler/queue jobs because Backups cannot
safely stop a job that may be changing data. Capture success clears the warning.
Backups never edits host launchers.
Legacy `systemd_locks` skips an explicit `activates: null` timer entry: it has
no target and cannot launch `schedule:run` or `queue:work`. Other entries must
name one valid service; missing or malformed targets still refuse with a
bounded `detail["activates"]` diagnostic. A null entry does not bypass lock
verification for other timers.
Legacy coordination also reads active or activating services and enabled,
enabled-runtime or static service files through the same standalone inventory
as v2 discovery. A relevant restarting queue worker refuses even without a
timer or a currently running worker process. A disabled, inactive service with
no timer target does not enter that inventory. Uninstantiated templates from
`list-unit-files` are excluded before querying service properties. Running
instances from `list-units` remain in the inventory.
The parser accepts documented /dev/null redirection tails, common PHP/Artisan
flags and hosting PHP basenames, with shell expansion/wrappers refused.
For an application at `/app`, an operator may set the process environment variable
`PAYMENTER_BACKUPS_GUARDED_SCHEDULER` to a canonical absolute script path, for
example `/opt/paymenter/scheduler.php`. It defaults to empty (Artisan only).
PHP Runtime and Python detached launchers explicitly preserve this one nonsecret
setting through their child environment allowlists. Set it in the operating
system environment of every PHP web, scheduler/cron and standalone CLI process;
a Laravel `.env` entry alone does not set it. See [installation](INSTALL.md).
Standalone crontab environment assignments, including quoted values, configure
job environments and are excluded from both discovery and legacy coordination;
scheduled commands with environment prefixes remain subject to strict parsing.
The parser then accepts exactly `/usr/local/bin/php CONFIGURED_PATH` behind the same explicit flock and
owner-private lock checks. This guarded scheduler takes no interpreter or script
arguments. Other interpreters, script paths and application roots refuse.
The script must be a root-owned regular file with no group or world write bits.
Its canonical path must remain exactly the configured path, with no file
or parent-directory symlinks. The canonical-path and `lstat` regular-file checks
follow `discovery.lock_safe`; script ownership and write restrictions differ
from that helper's app-owned, exactly 0600 lock policy. Discovery, cache reads
and admission recheck the script. Legacy cron and systemd validation use the
same parser. An unsafe script remains visible as unsupported with its guarded
command, so diagnostics cannot suggest replacing the guard with Artisan.
Normalized commands, cached diagnostics and replacement suggestions preserve
the guarded launcher and its lock, rather than replacing it with Artisan.
Unrecognized direct `/usr/local/bin/php` launchers for `/app` stay relevant
after this setting changes or clears, including scripts without a `.php` suffix.
They refuse admission instead of silently losing their cooperative lock.
Artisan targets with explicit commands retain ordinary application/command
filtering; a script named `artisan` without a command remains a possible retired
guarded launcher and refuses.
The final writer check also retains same-owner direct PHP script processes after
the selected path changes or clears, so retiring a cron entry cannot hide an
invocation that is still running under its previous lock.
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
continuous workers are full-backup setup blockers.

## Capture modes

`database-online` produces a transactional InnoDB SQL snapshot without
maintenance, under a no-concurrent-DDL condition. Its manifest clearly excludes
an atomic application/files snapshot and external provider state.

`full-quiesced` retains its historical mode identifier and archive format. New
captures keep Paymenter online, take the private backup lock and verified job
coordination locks, then capture one transactional SQL dump plus the full app.
They never run `artisan down`, drain HTTP requests, or probe HTTP 503. The
manifest guarantee is `transactional-database-and-live-app-files`: this is not an
atomic application/database checkpoint while HTTP writers remain active.

Full mode still refuses missing or unverified job locks, unsupported engines,
unaccounted durable external stores, external symlink targets and uncontrolled
scheduler/queue writers. A host that cannot provide verified locks cannot take a
full backup; it may use the explicitly qualified database-only fallback. Empty
v2 overrides derive verified locks, never an unlocked full capture. Both modes
keep the no-concurrent-DDL condition and all tool capability checks.

Before the dump and after the app tar, compare the complete extension tree's
paths, types, ownership/mode, inode, size, nanosecond modification/change times
and file hashes, including in-app symlink targets. A change discards the capture.
Scheduled full work skips that slot and tries at the next configured full time;
manual capture retries are bounded. Live runtime contents are excluded:
`storage/logs`, `storage/framework/cache`, `storage/framework/sessions`,
`storage/framework/views` and `storage/debugbar`. Paymenter's standard logging,
file cache/session stores, compiled views and debugbar write there during HTTP
requests. The manifest lists these paths. The archive supplies empty writable
directories, including `storage/framework/cache/data`, so Laravel can boot after
extraction without the omitted runtime data. Restored users must sign in again.
Application code/config, keys and extensions stay strict. Native `storage/app`
uploads, invoices and attachments remain included while HTTP requests write
them. Only the app tar may accept exit 1, and only when every stderr line is
exactly GNU tar's `file changed as we read it` or `File removed before we read it`
warning for a path inside `storage/app`. Escaped/ambiguous paths, key/code files,
symlinks, hard-linked files, mixed diagnostics, read errors and other exit codes
fail. Directory-only changes keep the copy. A changed or removed regular file
causes one extra app-tar pass under the original locks and deadline; SQL is not
dumped again. Files still changing or missing in that last pass are listed in
manifest `unstable_paths`; the last copy is kept. New uploads may be absent,
and a changing attachment or PDF may be incomplete. Invoice PDFs can be
regenerated from the restored database. This remains a live-files backup, not
an atomic database/files snapshot. `--ignore-failed-read` is not used. Do not
update code or schema during capture.
Pre-existing safe maintenance files are preserved for foreground capture;
scheduled capture skips pre-existing maintenance. Historical maintenance ownership
and recovery handling remain supported without creating any new owned markers.

Use transient private client defaults; `--defaults-file` is the first option.
Known-native capture uses established transactional/quick/hex-blob flags. Refuse
visible-nonzero routines/events/views/triggers or non-InnoDB tables; inventory
query failures refuse. Visible-zero is accepted with explicit visibility limits
and recorded current-user grants, without claiming hidden objects do not exist.
Do not add export flags requiring unavailable mysql.proc privileges. Verify trigger support and
dump completion against real supported clients. There is one SQL dump; live
database changes do not trigger a second export.

Preserve `.env`/APP_KEY, Passport keys, native storage, release/extension files,
ACLs/xattrs and symlinks. Work/output stays outside app and every docroot. New
captures exclude no maintenance files; preserve pre-existing marker files. This
is an app/database backup, not a backup
of host/Plesk/cron/Redis/object storage or remote provider actions.
