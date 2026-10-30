# Maintenance and release validation

Source and release ZIP are MIT projects. Build reproducibly from the committed
source and hash-pinned Python SDK lock. Dependency changes must pass Python 3.10,
PHP 8.3/8.4 lint, native stock discovery, secret non-disclosure and restore tests.
Python 3.10 reaches upstream end-of-life in October 2026; a pinned bundle remains
usable, but accepting a SDK update requires testing its declared Python floor.
Never update production dependencies with pip or alter root Composer files.

## Test and build

Use Linux, Python 3.10+, PHP CLI 8.3 or 8.4, Composer, MariaDB server/client,
ACL tools, and PHP `intl`, `pdo_mysql`, `mbstring`, `zip`, and `dom`. Install the
SDK into a development virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-s3.lock
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tools
python tools/build_release.py --output dist/first.zip
python tools/build_release.py --output dist/second.zip
cmp dist/first.zip dist/second.zip
```

Lint every PHP file in `extension/` and `tests/integration/` on PHP 8.3 and 8.4.
The S3 protocol tests need the pinned SDK; skipped dependency tests leave
validation incomplete. `PAYMENTER_BACKUPS_TEST_PHP` selects the PHP executable
for unit fixtures. Build with `--wheelhouse /path/to/wheels` to verify cached
hash-locked wheels entirely offline.

## Native restore and browser checks

Run the exact ZIP with each supported PHP version:

```sh
python tools/native_integration.py --release dist/first.zip --php /path/to/php --composer /path/to/composer
```

The fixture fetches and verifies the official Paymenter v1.5.9 archive, creates
its own MariaDB datadir/socket, uses fake credentials, and restores into a second
application/schema. It checks extension discovery, native settings, authentication,
secret handling, encrypted transfer, recovery verification, preserved `APP_KEY`,
Passport keys, migrations, and login. It closes its isolated processes afterwards.
Allow enough disk for both applications, backup payloads, and private state.
`--skip-restore` is a development shortcut and does not complete release checks.
The fixture refreshes the native billing guard immediately before configuration
and schedule writes and full capture. To check independence from earlier slow steps,
repeat the full command with `--slow-step-delay 181`: it pauses before the schedule
drill, reports the earlier guard's age, then refreshes before each write. The
production guard's default 180-second maximum age is unchanged.

For browser testing, start a disposable fixture:

```sh
python tools/native_ui_fixture.py serve --release dist/first.zip --php /path/to/php
```

Use its `ui-fixture.json` work directory and URL with `tools/browser_ui_checks.mjs`
(`--workroot`, `--base-url`, `--package-path /path/to/playwright`, `--phase all`,
and `--allow-fixture-mutations`). Install Playwright and Chromium separately.
Follow [UI-TESTING.md](UI-TESTING.md) for desktop/mobile, light/dark, keyboard,
actual downloads, and queued actions. The browser fixture uses database-only
captures and loopback S3. Stop it with
`python tools/native_ui_fixture.py stop --work /path/to/ui-fixture`; verify
`ui-closed.json` and owner-process exit before removing its files.

Before release, compare the two ZIPs and
record the final artifact hash and actual test results. Changes to the candidate
require the affected checks again. Host configuration, live provider/TLS behavior,
and production recovery need separate validation. Keep fixture logs, screenshots,
keys, and operational records outside Git. Clean only confirmed-dead owned
fixtures.

## Security and host checks

Native EditExtension loads every setting into browser state, so no secret or
encrypted rows belong to Backups. Only non-secret fields are declared. The custom
page resets write-only secrets on every response, including failed saves and
unrelated actions. Gate every page/action and the download route with
`admin.extensions.update`, with native auth/CSRF. Updates must preserve these
negative integration checks, the settings validation-before-write checks and
the download, verification and per-backup delete checks in the drill. Keep all
administrator copy in `extension/Backups/Messages.php` and follow
[ADMIN-UI.md](ADMIN-UI.md).

Cold configuration bootstraps only environment/configuration, no providers, and
reads rows through PDO using the native morph class. Its framework URL/timezone
honour native non-secret overrides. SettingsProvider caches may differ from DB
rows; billing safety relies on the fresh observed native scheduler event and
matching cold settings fingerprint, rather than synthesizing its timezone.
Configuration writes require a bootable app and native audited Eloquent. Offline
service recovery never requires configuration writes or Laravel startup.

Check scheduler and worker cooperative locks and
flags, cron service KillMode, bounded request/job duration, effective PHP binary,
FPM UID/open_basedir, cached configuration, maintenance template correctness,
quota and MariaDB client/object visibility. UTC avoids backup schedule DST gaps
and repeats. Native billing instants are observed for the next eight days, including
DST; a fresh guard is mandatory whenever capture takes job locks, including
database-only capture. Database-only capture waits for verified jobs without
maintenance or a drain, and defers around billing. Its lock wait is at least
60 seconds so it can outlast the normal 50-second queue run. Only a complete fresh
check proving no coordination is set up permits an unlocked snapshot, shown on
the page as able to overlap jobs. That notice clears when a fresh attempt verifies
lock setup, even if it must wait for jobs or billing. Failed discovery or verification
retries safely. Inventory and dumping share
one capture timeout; compression and upload run after releasing the locks.
A scheduled native billing minute has no catch-up guarantee after a lock hold.
Busy jobs are routine: queued database backups keep retrying past their normal
request age, including later billing or other transient deferrals. One warning appears
after a day without progress; check scheduler and queue jobs and their lock setup.
Backups cannot safely stop a job that may be changing data.

Keep credentials and private operational logs outside this public
repository. Preserve failed/unresolved runs and explicitly acknowledge operator
resolution instead of deleting journals to unblock capture. Normal reopening and
manual recovery use the same marker-identity checks. There remains a small
identity-check/unlink race; operator replacement during that gap is unsupported.

The MariaDB 10.6 disposable visibility fixture creates a root-defined routine,
event, trigger and view. An own-schema account with SELECT, INSERT, UPDATE, DELETE,
CREATE, ALTER, INDEX and DROP sees the view and hides the other three. Inventory
refuses visible nonzero and query errors; visible-zero records current-user grants
and explicitly leaves hidden objects unknown. MariaDB versions with different
visibility need the same privilege and inventory tests.

## Settings adapter contract

Version 2 has independent schedules and retention by backup type, full-to-database
coverage, S3-only staging removal, strict launcher discovery, and trusted tool
detection. Legacy behavior remains until `settings_version=2`. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the invariants and overview whitelist.

`tests/fixtures/settings-migration.json` contains the shared migration vectors.
Python tests compare the complete adapter projection. The PHP parity checker in
`tests/integration/settings_migration.php` checks every expected value and type,
including list shape, absent markers, enabled and disabled schedules, plaintext
S3, both retention kinds, acknowledgement combinations, and preserved pins.
A missing adapter method must fail this check.

PHP `Settings.php` and the Python configuration adapter share these keys,
defaults, and validation rules. Fresh records write version 2; existing records
use version 1 until the first explicit page Save writes every v2 field.
`Settings::fromLegacy()` proposes values without enabling another schedule or
changing pinned paths.

| Keys | Defaults | Rules |
| --- | --- | --- |
| settings_version | fresh 2; missing interpreted 1 | integer 1 or 2 |
| full_schedule / full_time / full_weekdays | off / 03:00 / [6] | off,daily,weekly; HH:MM; JSON weekdays 0-6, nonempty for weekly |
| database_schedule / database_time / database_weekdays | off / 02:00 / [0,1,2,3,4,5,6] | off,hourly,daily,weekly; HH:MM; JSON weekdays, nonempty for weekly |
| database_every_hours / database_minute | 1 / 0 | hourly: N in 1,2,3,4,6,8,12; minute 0-59 |
| schedule_timezone | UTC | shared valid timezone |
| keep_local / upload_s3 | true / false | booleans; at least one true |
| retention_keep_full / retention_keep_database | 7 / 24 | positive integers |
| s3_retention_policy_full / s3_retention_policy_database | local-count / local-count | local-count,count,age,keep-all |
| s3_retention_keep_full / s3_retention_keep_database | 30 / 168 | positive integers |
| s3_retention_days_full / s3_retention_days_database | 90 / 90 | positive integers |
| full_host_assumptions_acknowledged | false | boolean; full only |
| no_concurrent_ddl_acknowledged | existing false default | boolean; both kinds |
| s3_encryption / gpg | true / empty | boolean / optional absolute executable path |
| job_lock_paths | [] | absolute JSON list; empty v2 means derive |
| python,php,dump,mysql,tar,gzip | empty | optional absolute paths; empty means detect |
| plaintext_offsite_acknowledged | false | required for plaintext S3; freshly submitted when disabling encryption |

Other keys retain their portable defaults and validation rules. A blank v2
`local_path` uses `PRIVATE_ROOT/backups`; recorded older locations remain valid.
Keep legacy enabled/mode/schedule/destination/retention/acknowledgement keys in
storage for v1. Configuration repair merges replacement values before validating
malformed saved rows; public-directory validation remains strict.

Page Save uses `Runtime::saveConfiguration` to coordinate settings and credential
changes under the configuration/tick locks. It validates and initializes state
before merging submitted credentials while native settings rows are locked.
Blank secret groups keep saved keys and site password. Test PHP adapter parity,
native Save rollback, and partial outcomes. `CredentialInput` validates combined groups
before merging and preserves the archive-encryption namespace under the shared
credential flock and atomic writer.

Delivery uses recorded remote formats and complete encrypted restore proofs for
retention survivors and S3-only staging removal. Schedule coverage becomes durable
before delivery. Encryption never falls back to plaintext.

## Engine error codes

| Code | Meaning |
| --- | --- |
| schedule-kind-required | Select a schedule kind when set/enable is ambiguous or differs from v1's selected kind. |
| storage-required | Keep local or upload to S3 must be enabled. |
| full-host-assumptions-acknowledgement-required | Full mode needs the merged host assumptions confirmation. |
| tool-capability-unsupported | The named executable lacks the supported runtime/client identity. |
| job-discovery-failed | Account/system discovery could not be read reliably. |
| job-discovery-incomplete | Discovery metadata/output exceeded bounds or could not be fully validated. |
| job-discovery-stale | Cached diagnostics are older than 15 minutes. |
| job-discovery-unknown | No validated discovery cache is available. |
| scheduler-missing | No Paymenter scheduler launcher was discovered. |
| job-command-unsupported | A relevant launcher cannot be parsed safely. |
| job-uncoordinated | A relevant job has no supported flock. |
| continuous-worker-present | A scheduler/queue worker runs continuously. |
| job-lock-unsafe | A discovered lock is missing, replaced or not an owner-private single-linked regular file. |
| job-lock-override-mismatch | The explicit lock list differs from the discovered union. |
| job-workers-running | Application writers remain after the coordination locks were acquired. |
| retention-mode-unknown | Unknown/inconsistent recorded mode cannot authorize cleanup. |
| delete-mode-unknown | Unknown/inconsistent recorded mode cannot authorize ordinary Delete. |
| staging-removal-conflict | Another per-copy/manual removal intent is unfinished. |
| staging-removal-remote-proof-required | Exact current provider scope/completion evidence is absent. |
| staging-removal-local-verification-failed | Local files differ from the manifest or from their checked identities. |
| staging-removal-ownership-mismatch | Local paths/files fail ownership/private-directory checks. |
| staging-removal-local-delete-failed | Verified local staging could not be unlinked; its intent remains resumable. |

Existing tool-unavailable/tool-path-invalid, unsupported-db-objects and admission
codes remain. Tool validation supplies safe field/detail diagnostics; unsupported
DB objects name their fixed categories. Request status `satisfied` carries
`covered_by`; it records successful schedule coverage.
