# Architecture and build contract

This document defines the implementation contracts and consistency guarantees.
Settings v2 starts with both automatic schedules off and local storage on.
Full checkpoints and online database snapshots have independent schedules. Implementation and tests use
disposable fixtures with fake credentials.

Scheduling, admission and capture modes are in [SCHEDULING.md](SCHEDULING.md).
Administrator page contracts are in [ADMIN-UI.md](ADMIN-UI.md#administrator-page-contracts).

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
automatic tick. [ADMIN-UI.md](ADMIN-UI.md) defines the page's journeys,
language and the contracts that bind it to the engine.

Retention previews reuse a recorded full local verification only while its
file identity and manifest checksum still match. A changed or unchecked archive
gets a fresh full check. Applying retention always verifies archive bytes under
the run lock before deletion; a render's cached result cannot authorize it.
Application symlink readiness uses directory-entry metadata for ordinary files
and resolves symlinks without descending into linked directories.

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
its same absolute capture deadline after admission accounting, while polling,
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
Historical maintenance captures used the same identity-checked cleanup at normal release;
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

### Local history retention

Before publishing history receipts or moving active payloads, maintenance syncs
every history directory's parent, including directories left by an interrupted
attempt. Leaf publication and source removal then sync their own directories.

The tick and explicit `history-maintain` command move completed requests and
fully deleted, recovered runs to `history/v1/requests` and `history/v1/runs`
after 13 calendar months, and delete their cold payloads after three calendar
years from their proven terminal time. All dates use UTC. Missing terminal
dates, unknown outcomes, unfinished intents, remaining run work and unresolved
recovery stay active and appear in `history-maintenance.json`. Failed requests
remain uncertain records. This policy never deletes backup artifacts.
The newest completed, verified storage-test request stays active as readiness
evidence, regardless of age. A newer passing test releases the older proof to
ordinary retention; a failed retest does not displace it.

Maintenance takes the run lock before requests.lock, both without waiting.
It skips a busy attempt. It writes and fsyncs permanent installation-bound
request/slot and run receipts in `history/v1/request-keys` and `run-keys` before
atomically renaming an exact payload on the same filesystem. Each receipt pins
the cold name, terminal time and checksum. Both rename directories are fsynced.
A crash before renaming leaves the active record authoritative; a crash after
renaming leaves the cold payload and its replay receipt. Request creation
checks the permanent receipt before publishing a new request. Receipts never
expire, including when their payload is deleted. Cold expiration verifies the
receipt and checksum; damaged entries remain and are reported separately.
Recent fallback reads verify the receipt checksum against the exact bytes
decoded as JSON. An existing active record remains authoritative.
Terminal records within their first 13 months move to `recent-requests` and
`recent-runs` under the same locks, receipt and rename contract. These records
are still active history, separate from open work. Recent filenames contain
creation and terminal UTC microseconds plus the record ID; page selection
uses those names before decoding payloads. At 13 months, a checksum-verified
recent payload moves to its receipt's cold name. Existing cold receipts remain
compatible. Unclassifiable rows stay in the work set.
An interrupted source move resumes to the receipt's recorded recent or cold
destination, even across an age boundary, before ordinary cooling applies.

Tick, recovery, admission and storage enumerate the work set. Overview reads
that set plus the latest 30 terminal requests; all unresolved runs remain
visible regardless of page. Its `recent_runs` contains IDs and creation times
from filenames, without opening terminal journals. The administrator table
merges these names with live rows, selects a page, then requests only those
terminal payloads. Explicit full-field searches read recent deleted records
to preserve the existing search behavior. The read-only
`history-page runs|requests [--offset N] [--limit 1..100] [--cold] [--ids IDS]`
command returns a whitelisted page and per-entry errors. `list` and
`requests list` also accept `--offset` and `--limit` for terminal history,
alongside all work records; `status` includes work only. The initial upgrade
classifies existing files once under the same locks; subsequent ticks do not
read recent terminal payloads until they reach the cold-storage boundary.
Concurrent readers skip a vanished work record only when a permanent receipt
proves its terminal transition; missing unresolved records still fail closed.
Unresolved runs are shown as needing recovery even if marked deleted. Cold
entries never appear on normal pages. Payload permissions, installation identity and receipt
checksums are checked on each history read. A damaged entry does not suppress
other entries or an unresolved run.

Normal active readers never enumerate cold payloads. Manifests and unresolved
recovery journals retain their authority; no SQL or provider access is involved.

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
run outside the job-lock and billing hold with a separate deadline: five
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

Known dependency on internals: historical recovery reads and removes Laravel's maintenance-mode files. Keep it covered
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
window brackets the daily billing minute using lock wait, capture and cleanup
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
