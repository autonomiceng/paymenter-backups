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

`status`, `list`, `overview --offline`, `verify`, `recover` and requests work without PHP,
Laravel, database or provider startup. Recovery-kit export and recovery-key
export require a cold PHP/PDO read of the current native document roots before
validating the output location. Missing capture tools or invalid unrelated
capture settings do not prevent that privacy check; unreadable roots refuse
export. Configuration reads and plans use the
cold PHP/PDO bridge. Native config writes require a bootable application.
Native MySQL/MariaDB host/socket connections and `DB_URL` are honored. Native
database TLS options currently refuse with `database-tls-unsupported`; matching
dump/client TLS configuration is required before that connection mode can be
supported. The engine never silently downgrades a configured TLS connection.
Read-only commands never create private directories, locks or installation
identity. Before first capture, `init`, `config set` or `run --execute` provisions
state explicitly. Native config saves also perform explicit initialization.

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
Configuration and credentials are two atomic writes, not a cross-resource
transaction. A credential failure can leave a valid native settings update;
inspect redacted config before retrying. The native edit form preserves secrets.
S3 writes preserve the separate maintenance probe namespace and probe writes
preserve S3. Both credential paths share an owner-private file lock.

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
* Byte budget 10 GiB, minimum free reserve 1 GiB. Polling plus per-child file-size
  limits bound output. Reserve and budget include retained failed payloads. A
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

The legacy S3-only credential bridge reads the expanded encrypted file, but
its credential save replaces the complete payload and erases the probe namespace.
Before rollback, keep a private copy of the encrypted file and avoid credential
edits with that bridge. Install a version supporting every saved namespace before
entering probe credentials again. Without authenticated maintenance probes,
full mode can refuse with 401.


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
A verified/service-recovered full snapshot at or after a waiting scheduled
database slot durably satisfies that slot before delivery; manual tasks remain.

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

V2 encryption defaults on. Check encryption settings and remote formats.
Encrypted delivery must refuse when encryption support is unavailable. Plaintext upload requires acknowledgement, including a fresh
explicit acknowledgement when turning encryption off. Existing acknowledged v1
S3 uploads remain plaintext until adoption. See ARCHITECTURE.md for the exact
overview fields and encryption requirements.
