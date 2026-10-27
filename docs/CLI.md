# Command line

Use Linux, Python 3.10+ and the application owner's UID. The shipped wrapper
loads its own engine and pinned SDK; scheduled invocation uses Python `-I`.
Run locally over ordinary SSH. Do not put credentials in shell arguments.

```sh
python3 -I /path/to/Backups/runtime/cli.py \
  --app /srv/paymenter --private-root /srv/paymenter-backups-private \
  --php /usr/bin/php --bridge /srv/paymenter/extensions/Others/Backups/bridge.php \
  status
```

Development uses `bin/paymenter-backups` with the same arguments. Global flags
precede the workflow. Defaults: app is the working directory, private root is
`../paymenter-backups-private`, bridge is `extensions/Others/Backups/bridge.php`,
PHP is `/usr/bin/php`. Select the actual hosting PHP binary explicitly. Every
command emits JSON; failures emit a safe error code and optional nonsecret
field/detail diagnostics to stderr and exit 1.
`--version` and `--help` need no application.

| Workflow | Behavior |
| --- | --- |
| `config show` | Cold read, redacted runtime configuration |
| `config validate` | Cold read and configuration validation |
| `config check` | JSON stdin `{"settings":{…}}`; validates proposed settings, writes nothing |
| `config set` | JSON stdin; native nonsecret writes through audited Eloquent |
| `init` | Explicitly provision private installation identity and policy |
| `schedule show [--kind full\|database]` | Versioned schedule configuration and next slots |
| `schedule set --kind full\|database` | JSON stdin; frequency/time/weekdays/timezone, database every_hours/minute |
| `schedule enable`, `schedule disable` | Optional --kind; v2 unqualified disable disables both; ambiguous set/enable refuse |
| `plan` | Admission assessment and consistency scope |
| `run` | Plan only |
| `run --execute` | Foreground capture using configured mode, then delivery |
| `run --execute --mode database-online` | Transactional SQL snapshot only |
| `list`, `status` | Offline durable runs, recovery state and requests |
| `overview [--offline]` | Whitelisted backups, tasks and readiness; no archive paths, manifests or results |
| `verify ID [--show]` | Offline whole archive, gzip trailer and payload verification |
| `verify ID --record` | Same check under the run lock; records the identity that enables admin download |
| `deliver ID` | Idempotent S3 delivery; repairs missing/corrupt remote markers |
| `retention plan`, `retention apply` | Preview or explicitly delete verified owned artifacts |
| `delete ID [--location local\|s3\|all] [--execute]` | Preview or remove selected copies, protecting a survivor at each location |
| `retrieve ID [--execute]` | Preview or retrieve and verify an S3-only backup for local download |
| `destination-test` | Owned local/S3 probe, readback, cleanup |
| `requests create --action ACTION` | Durable admin-style request; JSON stdin arguments, including `delete` |
| `requests list`, `requests cancel ID` | Inspect or cancel pending/deferred requests |
| `recover` | Offline recovery preview |
| `recover --execute`, `recover --auto` | Guarded offline recovery |
| `recover acknowledge --run ID` | Record operator resolution, preserve every app file |
| `export ID --output /private/new-kit` | Copy verified archive and recovery instructions |
| `encryption-key generate\|import\|acknowledge\|discard-pending\|status` | Prepare, activate or inspect the independent S3 recovery key |
| `recovery-key export --output /private/new-key.json [--fingerprint FULL]` | Export one recovery secret and its identity to a new private file |
| `credentials-rewrap` | Re-encrypt every saved credential namespace under the current APP_KEY |

`status`, `list`, `overview --offline`, `verify`, `recover` and requests work without PHP,
Laravel, database or provider startup. Recovery-kit export and recovery-key
export require a cold PHP/PDO read of the current native document roots before
validating the output location. Missing capture tools, billing settings or invalid
unrelated capture settings do not prevent that privacy check; unreadable roots
refuse export. Archive-key operations and APP_KEY rewrapping also work without
billing settings, while preserving installation and credential checks.
Configuration reads and plans use the
cold PHP/PDO bridge. Native config writes require a bootable application.
Native MySQL/MariaDB host/socket connections and `DB_URL` are honored. Native
database TLS options currently refuse with `database-tls-unsupported`; matching
dump/client TLS configuration is required before that connection mode can be
supported. The engine never silently downgrades a configured TLS connection.
Ordinary configuration and backup-status reads never create private directories,
locks or installation identity. Key operations require initialized state and take
the shared credential lock; reveal/export also write secret-free audit evidence.
Before first capture, `init`, `config set` or `run --execute` provisions
state explicitly. Native config saves also perform explicit initialization.

`config set`, native page Save and direct Artisan saves share configuration/tick
locks with a ten-second acquisition deadline (`busy` refuses before writes).
Credential-bearing worker reads wait for a complete settings/keys snapshot.
Definite failures before credential publication restore only unchanged native
rows; concurrent editor updates survive, and rejected saves preserve schedule
epochs and waiting requests. A partial response reports confirmed outcomes;
`outcome_unknown:true` means a child may have committed without returning its
receipt. The private save intent then blocks credential-bearing backup work with
`config-save-outcome-unknown`. Check the current settings and repeat `config set`
with the intended settings and every requested uncertain S3/site credential
group, or explicit `{ "clear": true }` for that group. A settings-only save or
`init` cannot resolve an uncertain credential group. Secret material remains on
stdin and never appears in the intent or public response.

Examples use a private stdin pipe; substitute real paths and use your secret
manager rather than saving credential JSON in shell history:

```sh
printf '%s\n' '{"settings":{"mode":"database-online","no_concurrent_ddl_acknowledged":true}}' |
  bin/paymenter-backups --app /srv/paymenter config set
printf '%s\n' '{"schedule_frequency":"weekly","schedule_time":"02:15","schedule_weekdays":[0,3],"schedule_timezone":"UTC"}' |
  bin/paymenter-backups --app /srv/paymenter schedule set
secret-manager-command |
  bin/paymenter-backups --app /srv/paymenter config set
printf '%s\n' '{"run_id":"RUN-ID"}' |
  bin/paymenter-backups --app /srv/paymenter requests create --action verify
```

Request arguments are `run_id` for `verify`, `deliver` and `delete`, `execute`
(`true` is required for `retention` deletion and for `delete`), `location`
(`local`, `s3` or `all`, for deletion), `mode`
(`full-quiesced` or `database-online`, `run` only) and `actor`, a numeric
administrator ID recorded for audit. Repeating an identical waiting manual
request returns the waiting request. A `delete` request is refused while
another request for that backup is active.

`delete ID` previews both copies by default. Use `--location local` or
`--location s3` to remove one copy. Execution holds the run lock, fully verifies
another survivor at each affected location, records a separate intent for each
copy and resumes after failure. It refuses conflicting tasks and uncertain or
changed S3 locations. Local deletion remains available when an older S3 copy
is outside the current settings. It never changes retention settings. Example:

```sh
bin/paymenter-backups --app /srv/paymenter delete RUN-ID
bin/paymenter-backups --app /srv/paymenter delete RUN-ID --execute
bin/paymenter-backups --app /srv/paymenter delete RUN-ID --location local --execute
```

`overview` is the administrator page's status source and a compact terminal
summary. Each run shows a status code (`ready`, `damaged`, `running`, `failed`,
`attention`, `interrupted`, `deleting`, `delete-interrupted` or `deleted`),
size, copy locations and whether a recorded verification still matches the
archive (`download: ready`) or a new check is needed (`verify`).

Credential stdin envelope: `{"credentials":{"access_key":"…","secret_key":"…",
"session_token":"…"}}`; `session_token` is optional. It never enters native
setting rows or public output. Credentials are encrypted through the native
Encrypter and bound to installation UUID, cipher and APP_KEY fingerprint. Key
rotation honors native previous keys. An unreadable store refuses changes;
resolve the APP_KEY or file binding before deliberately re-entering credentials.
Configuration and credentials are coordinated across separate atomic writes.
A definite credential refusal restores unchanged native settings and preserves
the existing store; concurrent editor updates survive. An ambiguous commit
keeps the durable save intent and pauses credential-bearing backup work until a
complete Save resubmits the uncertain groups. Inspect redacted config before
retrying. The native Extensions lifecycle form exposes no Backups settings and
preserves the external credential store.
S3 writes preserve the separate maintenance probe and archive-key namespaces;
probe and archive-key writes preserve S3. All credential paths share an
owner-private file lock.

If the saved data-directory path is relative, contains `..` or names a symlink,
correct that native pin from the terminal. Use the existing canonical directory
containing `installation.json`; do not select a new directory. First run
`status` with that explicit `--private-root` and confirm `initialized: true` and
the known installation UUID. The read does not create state. Then use the same
directory in both the flag and the settings payload:

```sh
printf '%s\n' '{"settings":{"private_root":"/srv/paymenter-backups-private"}}' |
  python3 -I /srv/paymenter/extensions/Others/Backups/runtime/cli.py \
    --app /srv/paymenter --private-root /srv/paymenter-backups-private \
    --php /usr/bin/php config set
```

Choose the application's actual CLI PHP binary. The explicit directory is
checked against its application, owner and installation binding; the proposed
pin must equal that bound directory. After the successful status precheck, using
that same canonical directory preserves UUID and encrypted credentials and moves
no private files. If status cannot identify the known UUID, stop; do not run
`config set`. That command can initialize a fresh directory, so a guessed path
does not establish the identity of the existing installation.

For outer HTTP Basic protection, privately pipe this envelope into `config set`:
`{"maintenance_probe_credentials":{"origin":"https://billing.example.com",
"username":"…","password":"…"}}`. The explicit HTTPS origin must match the
configured application URL's scheme, host and effective port. Origin paths,
userinfo, queries, fragments, control characters and ambiguous authorities refuse.
Username/password must be nonempty UTF-8 strings up to 8192 bytes; username
colons and control characters in either field refuse. Reconfigure the credentials
when changing application origin. Clear only this namespace with
`{"maintenance_probe_credentials":{"clear":true}}`; unreadable stores also
refuse clearing. `{"credentials":{"clear":true}}` removes only the S3 keys. `config show` redacts the complete namespace including username.
The probe uses system TLS trust, no inherited proxy/CA/keylog environment, no
cookies and no redirects. Basic auth only passes the outer guard. Full mode
still requires exact native 503/template bytes; 401/200/404/redirect refuse.

Defaults and prerequisites:

* Legacy (settings_version absent/1): schedule disabled; daily at 02:00 UTC when enabled. Weekly weekdays use Monday
  0 to Sunday 6. Enabling or editing starts at the next future slot. After an
  outage, one fresh backup is queued instead of a backlog. Unstarted tasks
  expire `request_max_age` after creation, default 24 hours.
  Local DST gaps skip nonexistent slots; repeated wall slots execute once.
* Mode `full-quiesced`, destination `local`, seven retained runs. Automatic
  retention follows successful run/delivery publication; set
  `automatic_retention=false` to require explicit retention apply. Failed
  remote delivery keeps its local copy, and deletion intent is durable/retryable.
  Local artifacts
  default to `PRIVATE_ROOT/backups` for new installations. Existing installations
  keep `PRIVATE_ROOT/artifacts` when their binding has no directory field.
  `local_path` may choose another canonical
  owner-private location outside the app and every hosting document root.
  Declare every additional hosting document root with `extra_docroots` (JSON
  absolute path list). Canonical app/public is always excluded. This policy is
  stored privately for offline artifact checks and refreshed by native config
  saves. Both export commands always refresh it in memory from native rows,
  including after a failed initialization; they never rely on the cached policy
  to authorize plaintext output.
* Byte budget 10 GiB, minimum free reserve 1 GiB. Per-child file-size limits
  bound capture output. Capture and retrieval reserve checks use filesystem
  free space once per second or per 64 MiB, plus completion. Full history
  accounting runs at output boundaries. Reserve and budget include retained failed payloads. A
  filesystem's available space does not establish a hosting subscription quota.
  Size these limits against the actual quota and expected app/database size.
* Capture timeout 300 seconds, drain 30 seconds, lock wait 15 seconds, billing
  margin 120 seconds, billing guard age 180 seconds. Full capture has a shared
  deadline across inventory, dump, app capture and stability dump.
* Set `no_concurrent_ddl_acknowledged=true` only after excluding concurrent DDL.
  Full capture also requires `cooperative_writers_acknowledged=true` and
  `external_state_acknowledged=true`, native file maintenance, no external
  symlink targets and supported local/database storage drivers. Acknowledgement
  does not make uncontrolled writers or external durable state safe.
* Configure existing job lock paths, owned by the app UID. Real account cron
  must directly run `flock PATH php APP/artisan schedule:run` / `queue:work`, or
  `cd APP && flock PATH php artisan …`. Exclusive `-x`, nonblocking `-n`, wait
  `-w N` and exit code `-E N` are recognized. Shared locks, force workers,
  compound shell tails, fd locks and unverified cron forms refuse full mode.
  Existing systemd timer services can also use the same explicit flock command.
  The runner reads their service command and Unix user, with a bounded discovery
  deadline. It never installs or changes jobs. Verify cron/service semantics
  and request/worker bounds on the deployment.
  For an app at `/app`, set the process environment variable
  `PAYMENTER_BACKUPS_GUARDED_SCHEDULER=/opt/paymenter/scheduler.php` for PHP, cron
  and the standalone CLI (see [installation](INSTALL.md)). It is empty by default,
  so only the standard Artisan form is accepted until configured.
  The guarded scheduler may then run
  `flock PATH /usr/local/bin/php /opt/paymenter/scheduler.php`, with no interpreter or
  script arguments. Only the configured script path and this interpreter/app root
  are accepted.
  The script must be root-owned, regular, and not group or world writable, with
  no file or parent-directory symlinks. Its safety is rechecked before admission
  and when reading cached readiness.
  Discovered guarded commands stay intact in replacement suggestions.

Refresh a terminal billing guard with the booted native command:

```sh
php /srv/paymenter/artisan backups:guard
```

The native minute tick observes the actual CronJob event expression/timezone and
next eight days of UTC billing instants. Full admission requires a fresh matching
guard and excludes the protected billing minute, waits, drain, capture, cleanup
and margin. Billing daily jobs do not catch up automatically. Online database
mode does not need the guard. UTC is recommended.

The tick is the only detached launcher. It launches one durable request with
closed inherited descriptors and a new session. Administrator pages write
requests; PHP-FPM never performs capture or delivery. Tick recovery runs while
maintenance is active and before cold configuration reads. Foreground capture
preserves strict pre-existing maintenance; scheduled captures skip it.

For legacy S3 set destination, HTTPS endpoint (or AWS default), region, bucket, prefix,
path-style selection and `plaintext_offsite_acknowledged=true`. The UUID is
always appended to the prefix. Request/response checksum policy is
`when_required`; complete streamed GET SHA256 validates archive and manifest.
The manifest is published after the archive; `lastmanifest.json` points to the
newest delivered run. Failed delivery preserves local artifacts. SDK transport
uses explicit credentials, verified TLS, bounded retry/timeouts and no inherited
proxy, CA, profile, endpoint override or TLS key logging.

Grant only the installation prefix: PutObject/GetObject/DeleteObject and
multipart create/upload/complete/abort permissions. Do not change bucket policy
through this extension. Versioning and Object Lock are optional operator
choices; DeleteObject under versioning creates delete markers rather than
purging old versions. Retention preserves a good copy at each location and never scans
or deletes other installation prefixes. Failed/unresolved runs remain outside
retention until reviewed. Tracked incomplete multipart work is visible in its
journal and aborts on delivery retry; resolve a changed destination explicitly.

Configure retention independently:

```json
{"settings":{"retention_keep":7,"s3_retention_policy":"age","s3_retention_days":90}}
```

`retention_keep` controls this server. S3 policies are `local-count` to keep the
same number, `count` with `s3_retention_keep`, `age` with `s3_retention_days`, or
`keep-all`. The default is `local-count`; the default 30-copy and 90-day values
do not activate those policies. Age uses the backup's creation time. Cleanup
runs after successful capture/delivery or `retention apply`. Failed uploads,
damaged copies and freshly retrieved copies may temporarily exceed the count.
Skipped copies and partial cleanup are visible in overview and run details.
Bucket lifecycle can remove objects independently; this extension does not
manage that policy, old object versions or Object Lock.

For an S3-only backup, `retrieve ID` previews and `retrieve ID --execute` verifies
and publishes a local copy under the disk limits. That copy is protected from
automatic cleanup for 24 hours. The page queues the same workflow and offers
Download when it finishes. `verify ID --record` checks the local copy when one
exists, or the recorded S3 copy when it does not. Old remote records without
provider binding require an explicit verified delivery retry before destructive
operations. Fully deleted historical records remain deleted.

The legacy S3-only credential bridge reads the expanded encrypted file. Its S3
credential save replaces the complete payload and erases the maintenance probe
and archive-key namespaces.
If rolling back, preserve the encrypted file privately first and avoid credential
edits with that bridge. Restore a version supporting every saved namespace before
editing credentials. Rollback does not provide authenticated maintenance probes
or encrypted S3 delivery; full mode can refuse with 401.


## Settings v2 and independent schedules

The native marker settings_version=2 adopts independent schedules and retention. Missing/1 retains
legacy behavior until an explicit save; the adapter never writes or starts work.
V2 starts with both schedules off, local storage on and S3 upload off. Full
schedule defaults to 03:00/Sunday; database to hourly interval 1/minute 0 or
daily/weekly 02:00/all weekdays. Full supports off/daily/weekly; database also
supports hourly intervals 1,2,3,4,6,8,12 at local divisible hours and minute 0-59.
Examples after explicit v2 adoption:

```sh
printf '%s\n' '{"frequency":"hourly","every_hours":2,"minute":15,"timezone":"UTC"}' |
  bin/paymenter-backups --app /srv/paymenter schedule set --kind database
printf '%s\n' '{"frequency":"weekly","time":"03:00","weekdays":[6]}' |
  bin/paymenter-backups --app /srv/paymenter schedule set --kind full
bin/paymenter-backups --app /srv/paymenter schedule show
bin/paymenter-backups --app /srv/paymenter schedule disable --kind full
bin/paymenter-backups --app /srv/paymenter schedule disable
```

Unqualified set/enable selects the sole non-off kind or refuses
schedule-kind-required; it never enables both. Explicit enable of an off kind
uses daily for full/hourly for database. V1 unqualified commands retain their
old fields; --kind must match its selected legacy mode. Enabling/editing starts
in the future, including an exact-minute save. Timezone edits reset both epochs;
other edits affect only their kind. Catch-up is one latest slot within eight
days per kind, with expiry measured from queue creation. DST gaps skip and
repeated v2 wall times run once. Both due obligations are queued with explicit
modes; eligible full wins an initial tie, deferred work is retried fairly.
A service-recovered full snapshot whose recorded database snapshot lower bound
is at or after a waiting scheduled database slot can satisfy that slot only while
an available generation passes current verification. Satisfaction is durable
before delivery; an existing coverage receipt survives later retention or
provider loss. Manual tasks remain.

V2 retention uses retention_keep_full=7 / retention_keep_database=24 and
s3_retention_policy_{full,database}=local-count, inactive counts 30/168 and
ages 90/90. Ordinary deletion requires another verified same-kind copy at each
selected location. Unknown/inconsistent modes stay. Newest S3 copies are
protected separately by kind. Keep-local/upload-S3 must have at least one true.
S3-only uploads require full remote proof before journaled local staging removal;
already-delivered/remote-only retry follows the same proof. Retrieved copies
have a 24-hour lease; only expired retrievals can use this same-run exception.
This exception does not authorize ordinary Delete. Disk budgets include staging.

Settings validation and initialization use credential-free read-settings, so
unreadable saved keys do not prevent nonsecret repair. Combined credential
submissions merge requested S3/site namespaces in one credential write; omitted
or null groups stay unchanged. Settings and credentials are separate resources.
Binary paths may be blank to detect via fixed trusted directories; explicit pins
remain authoritative. PHP follows the hosting version. Overview names the tool,
store and job behind a refusal and returns clock-time billing-window boundaries.

V2 encryption defaults on. New S3 delivery requires usable GnuPG and an active
acknowledged recovery key. Missing key setup or an unusable encryption tool
blocks delivery rather than local capture; failed uploads retain their local
source. Encrypted delivery publishes the archive ciphertext and encrypted
recovery document before its public descriptor. Retrieval and remote survivor
checks decrypt and fully verify the recorded generation. Existing acknowledged
v1 S3 uploads remain plaintext until adoption; older copies keep their recorded
format after adoption. Encryption never falls back to plaintext. Plaintext
upload requires acknowledgement, including a fresh explicit acknowledgement
when turning encryption off.

## Recovery-key setup and rotation

After `init`, use `encryption-key generate` to create a pending random key, or
pipe `{"passphrase":"…"}` from your secret manager into `encryption-key import`.
Passphrases preserve exact UTF-8, require at least 20 characters, forbid control
characters and allow at most 1024 bytes. Generated keys are 64 lowercase hex
characters. Ordinary command output contains only the fingerprint and state.

Export the pending key to a new file in an owner-private directory outside the
application and every document root, then save it outside this server:

```sh
bin/paymenter-backups --app /srv/paymenter encryption-key generate
bin/paymenter-backups --app /srv/paymenter recovery-key export --output /srv/private-export/recovery-key.json
printf '%s\n' '{"fingerprint":"FULL-FINGERPRINT-FROM-GENERATE"}' |
  bin/paymenter-backups --app /srv/paymenter encryption-key acknowledge
bin/paymenter-backups --app /srv/paymenter destination-test
```

Acknowledgement must name the exact full pending fingerprint and activates it
under the run lock. A pending first key cannot authorize uploads; during rotation,
the acknowledged active key stays in use until the replacement is activated.
Prepared deliveries keep their original fingerprint. Retired keys remain saved
for earlier copies; keep every exported key. `--fingerprint` selects an older key
for export. Import accepts an exported JSON record and preserves its fingerprint
parameters. `discard-pending` removes only the unactivated replacement.

Destination tests use the current remote format and, for encryption, the active
acknowledged key in an upload/readback/decrypt/compare/cleanup round trip. Repeat
the test after changing the destination, enabling encryption or activating a new
key. `credentials-rewrap` preserves all keys and S3/site credentials while
rewrapping under the current APP_KEY; do it before removing a previous APP_KEY.
See [RECOVERY.md](RECOVERY.md) for the separately saved key and fresh-machine
restore procedure.
