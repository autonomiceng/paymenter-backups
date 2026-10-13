# UI workflow testing

This maps administrator controls to engine calls and isolated tests. Use a
disposable Paymenter application, database, private root, and fake credentials.
Check browser navigation, keyboard behavior, theme, mobile layout, and actual
downloads separately from native Livewire assertions.

## Workflow map

| Public workflow | UI control / native or engine call | Validation and expected durable result | Test scope |
| --- | --- | --- | --- |
| Install, enable, disable, uninstall extension | Native extension upload/install/editor; native extension record `enabled` | ZIP discovery; defaults become nonsecret setting rows. Uninstall deletes native record/settings, preserves private state and remote artifacts. Extension enablement and backup scheduling are separate flags. | Disposable fixture |
| `config show` | Native Extensions editor shows declared nonsecret fields; no redacted-envelope control | Native editor loads all setting rows; secrets must remain external. CLI returns redacted bridge envelope. Page load changes no backup files. | Disposable fixture |
| `config set`, `config validate`, `init` | Native editor Save invokes `updated()`; no Validate or Initialize button | Native field rules run before save. Native rows are written before engine validation. Hook attempts `init`, then validation, and writes `configuration-advisory.json`; invalid saves are advisory, without rollback. CLI `config set` validates merged configuration and schedule before native writes. Initialization writes installation identity/policy. | Disposable fixture |
| Credential update through `config set` | Backups page Access key, Secret key, optional Session token; Save credentials → `Runtime::credentials()` → bridge `credentials-write` | Bridge requires nonempty access/secret strings, maximum 8192 bytes each and bounded JSON. Initializes identity if needed; atomically encrypts `credentials.json` outside native rows. Unreadable bound store refuses replacement. Inputs clear on success, failure and unrelated Livewire responses. | Disposable fixture |
| Probe credential update/clear through `config set` | Backups page HTTPS origin, Probe username/password; Save/Clear probe credentials → `Runtime::credentials(..., true)` → bridge `maintenance-probe-credentials-write` | Explicit HTTPS origin must match configured app origin; bounded nonempty strings, no username colon/control characters. Shared encrypted file preserves S3 namespace under private flock. Failed, successful and unrelated responses clear all probe inputs; unreadable stores refuse replacement/clear. | Disposable fixture |
| `schedule show`, `set`, `enable`, `disable` | Native editor `enabled`, frequency, time, weekdays and timezone; Save | HH:MM, timezone, enums and JSON rules; engine also checks day types/range, enabled weekly nonempty selection and full-mode billing overlap. Valid CLI changes persist native rows; native form failures can persist as advisory-invalid settings. | Disposable fixture |
| `plan`, `run` without `--execute` | No preview/admission control | CLI assesses mode, acknowledgements, initialization, billing guard/job locks and unresolved recovery. No capture or new private state. | Disposable fixture |
| `run --execute`, per-run `--mode` | Request backup → `requests create --action run`; no per-request mode selector | Writes one pending request using configured mode. Next tick starts capture; durable request points to run ID. Run journal distinguishes service recovery, local completion and delivery. Full mode changes maintenance; online mode captures SQL only. Run/delivery can invoke automatic retention. | Disposable fixture |
| `list`, `status` | Backups page initial load / Refresh status → `status` | Displays JSON installation, runs, unresolved IDs, maintenance and requests. No dedicated run table. UI requires bootable native app/DB and working FPM private-root/Python/PHP access; standalone CLI status remains offline-capable. | Disposable fixture |
| `verify ID`, `verify --show` | Run ID / Verify run → queued `verify`; no full-manifest control | UI and engine share `[A-Za-z0-9_-]{1,100}` grammar. Existing/completed run is checked by worker, not at request creation. Worker returns whole archive/payload verification in durable request result; verification does not create a new run or modify artifact. `--show` is CLI only. | Disposable fixture |
| `deliver ID` | Run ID / Retry delivery → queued `deliver` | Syntax checked before spool; completed artifact checked by worker. Local destination returns `remote_delivered=false`; S3 upload/readback publishes manifest last, journals delivery or safe failure, preserves local artifact. Successful delivery can apply automatic retention. | Disposable fixture |
| `retention plan`, `retention apply` | Apply retention with confirmation → queued `retention` with `execute=true`; no preview control | Deletes only eligible verified owned artifacts under configured policy; keeps latest known good copy, journals deletion intent. Confirmation does not show candidate IDs. | Disposable fixture |
| `destination-test` | Test destination → queued `destination-test` | Configuration checked at worker startup. Local probe/readback/cleanup or S3 installation-prefix probe/readback/delete; durable request result reports success/failure. May create local destination directory. | Disposable fixture |
| `requests create`, `list`, `cancel` | Five action buttons create; status JSON lists; no Cancel control | Request file has action, arguments, identity, pending status and attempts. Tick/worker records launching/running/complete, failed, deferred, skipped or expired. Cancellation is CLI-only and allowed for pending/deferred requests. | Disposable fixture |
| `recover`, `recover --execute`, `recover --auto`, `recover acknowledge` | No recovery control; tick performs guarded automatic recovery | CLI preview, identity-checked marker cleanup or explicit operator acknowledgement. Preserve operator replacements and prior maintenance; acknowledgement does not touch application files. | Disposable fixture |
| `export ID --output` and restore | No export/download/restore control | CLI verifies owned archive and writes a new private kit; restore requires new paths/database and controlled activation. | Disposable fixture |
| Native minute tick / billing guard refresh | No UI trigger; native provider registers every-minute event; terminal `backups:guard` observes billing event | Guard file pins actual native billing expression/timezone. Engine tick handles auto-recovery, due slots and one pending/deferred request. | Disposable fixture |

## Validation and request tests

* Submit zero for `local_byte_budget`, `capture_timeout`, `billing_guard_max_age`,
  `request_max_age`, `s3_timeout`, `s3_retries` and `retention_keep`. The engine
  requires positive values. Check native rows and `configuration-advisory.json`
  after a native form save. The engine must refuse invalid saved values.
* Test weekday and path-list semantics with `[7]`, `["0"]`, scalar JSON,
  objects, relative paths, and `[]` with an enabled weekly schedule. Weekdays
  must be integers from 0 to 6. Path lists must contain absolute paths. Compare
  PHP normalization with engine validation, including list shape.
* Test S3 acknowledgement, bucket, HTTPS endpoint, executable paths,
  private-directory boundaries and full-mode billing overlap. A native save
  writes rows before engine validation and reports invalid settings through
  `configuration-advisory.json`. Use CLI validation to check admission.
* Submit blank, oversized and valid fake credentials. The bridge requires
  nonempty access and secret keys, limits each to 8192 bytes, and bounds JSON
  input. Check error feedback, secret clearing and preservation of an unreadable
  credential store. Keep test credentials out of screenshots and network logs.
* Queue each supported action and inspect its request file before and after
  worker execution. Submission creates a pending request. The worker checks
  run existence and admission, then records completion or refusal. Check
  CLI-only plan, cancellation, recovery and export commands separately.
* Disable the automatic schedule and queue a manual request. The tick must
  still process manual work. Disable the native extension and check that its
  tick stops. Inspect pending requests before executing either scheduler path.
* With an HTTP Basic guard, configure private encrypted probe credentials for
  the configured HTTPS origin. Check permission gates, write-only fields,
  secret clearing after every response, and preservation of S3 credentials.
  The maintenance probe must refuse 401 responses, redirects, cookies and normal
  pages. Only exact native 503/template bytes permit full capture. An
  authenticated 404 checks access through the outer guard, not maintenance.
  Database-online mode does not require the maintenance probe.
* Run scheduler checks only in the disposable fixture. `artisan schedule:run`
  invokes every due native and extension event. `tick --execute` runs backup
  recovery, schedules and queued work without invoking ordinary native events.

## Browser checks

Open native login and both settings/page routes in a disposable browser context.
Submit the actual controls and compare field feedback, native non-secret rows,
and safe request/journal results. Check secret clearing, permission denial, CSRF,
keyboard navigation, client validation, and asynchronous completion. Keep
screenshots and downloaded credentials outside Git.

Prepare valid, incomplete, and corrupted owned fixture runs, enough verified
copies for retention, and pending/deferred requests for cancellation. Full mode
needs cooperative job locks, bounded writers, required acknowledgements, a fresh
billing guard, and the exact maintenance-template response. S3 checks need a
dedicated test destination and scoped credentials; local tests need no provider.

## Primary source contracts

* [Settings](../extension/Backups/Settings.php), [admin page](../extension/Backups/Admin/Pages/Backups.php),
  [Blade controls](../extension/Backups/resources/views/admin.blade.php),
  [runtime bridge](../extension/Backups/Runtime.php), [credential bridge](../extension/Backups/bridge.php),
  [update hook](../extension/Backups/Backups.php), [native provider](../extension/Backups/NativeProvider.php).
* [CLI dispatch](../src/paymenter_backups/cli.py), [configuration validation](../src/paymenter_backups/config.py),
  [request lifecycle](../src/paymenter_backups/requests.py), [maintenance admission](../src/paymenter_backups/maintenance.py),
  [capture/verify/export](../src/paymenter_backups/engine.py), [delivery/retention/probes](../src/paymenter_backups/storage.py).
* Stock Paymenter source: `app/Admin/Resources/ExtensionResource.php`,
  `app/Admin/Resources/ExtensionResource/Pages/EditExtension.php`,
  `app/Helpers/ExtensionHelper.php`, `app/Classes/FilamentInput.php`.
* [Native integration coverage](../tests/integration/native_checks.php),
  [fixture orchestration](../tools/native_integration.py), [architecture](ARCHITECTURE.md),
  [CLI](CLI.md), [installation prerequisites](INSTALL.md).
