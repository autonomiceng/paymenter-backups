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
command emits JSON; failures emit only a safe error code to stderr and exit 1.
`--version` and `--help` need no application.

| Workflow | Behavior |
| --- | --- |
| `config show` | Cold read, redacted runtime configuration |
| `config validate` | Cold read and configuration validation |
| `config set` | JSON stdin; native nonsecret writes through audited Eloquent |
| `init` | Explicitly provision private installation identity and policy |
| `schedule show` | Current daily/weekly schedule |
| `schedule set` | JSON stdin of schedule fields |
| `schedule enable`, `schedule disable` | Explicit native schedule changes |
| `plan` | Admission assessment and consistency scope |
| `run` | Plan only |
| `run --execute` | Foreground capture using configured mode, then delivery |
| `run --execute --mode database-online` | Transactional SQL snapshot only |
| `list`, `status` | Offline durable runs, recovery state and requests |
| `verify ID [--show]` | Offline whole archive, gzip trailer and payload verification |
| `deliver ID` | Idempotent S3 delivery; repairs missing/corrupt remote markers |
| `retention plan`, `retention apply` | Preview or explicitly delete verified owned artifacts |
| `destination-test` | Owned local/S3 probe, readback, cleanup |
| `requests create --action ACTION` | Durable admin-style request; JSON stdin arguments |
| `requests list`, `requests cancel ID` | Inspect or cancel pending/deferred requests |
| `recover` | Offline recovery preview |
| `recover --execute`, `recover --auto` | Guarded offline recovery |
| `recover acknowledge --run ID` | Record operator resolution, preserve every app file |
| `export ID --output /private/new-kit` | Copy verified archive and recovery instructions |

`status`, `list`, `verify`, `recover`, requests and export work without PHP,
Laravel, database or provider startup. Configuration reads and plans use the
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
refuse clearing. `config show` redacts the complete namespace including username.
The probe uses system TLS trust, no inherited proxy/CA/keylog environment, no
cookies and no redirects. Basic auth only passes the outer guard. Full mode
still requires exact native 503/template bytes; 401/200/404/redirect refuse.

Defaults and prerequisites:

* Schedule disabled; daily at 02:00 UTC when enabled. Weekly weekdays use Monday
  0 to Sunday 6. Missed requests defer for `request_max_age`, default 24 hours.
  Local DST gaps skip nonexistent slots; repeated wall slots execute once.
* Mode `full-quiesced`, destination `local`, seven retained runs. Automatic
  retention follows successful run/delivery publication; set
  `automatic_retention=false` to require explicit retention apply. Failed
  remote delivery keeps its local copy, and deletion intent is durable/retryable.
  Local artifacts
  default to `PRIVATE_ROOT/artifacts`; `local_path` may choose another canonical
  owner-private location outside the app and every hosting document root.
  Declare every additional hosting document root with `extra_docroots` (JSON
  absolute path list). Canonical app/public is always excluded. This policy is
  stored privately for offline exports and refreshed by native config saves.
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
  Verify cron/service semantics and request/worker bounds on the deployment.

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

For S3 set destination, HTTPS endpoint (or AWS default), region, bucket, prefix,
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
purging old versions. Retention preserves the latest good copy and never scans
or deletes other installation prefixes. Failed/unresolved runs remain outside
retention until reviewed. Tracked incomplete multipart work is visible in its
journal and aborts on delivery retry; resolve a changed destination explicitly.

The legacy S3-only credential bridge reads the expanded encrypted file, but
its credential save replaces the complete payload and erases the probe namespace.
Before rollback, keep a private copy of the encrypted file and avoid credential
edits with that bridge. Install a version supporting every saved namespace before
entering probe credentials again. Without authenticated maintenance probes,
full mode can refuse with 401.
