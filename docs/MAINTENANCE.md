# Maintenance and release validation

Source and release ZIP are MIT projects. Build reproducibly from the committed
source and hash-pinned Python SDK lock. Dependency changes must pass Python 3.10,
PHP 8.3/8.4 lint, native stock discovery, secret non-disclosure and restore tests.
Python 3.10 reaches upstream end-of-life in October 2026; a pinned bundle remains
usable, but accepting a SDK update requires testing its declared Python floor.
Never update production dependencies with pip or alter root Composer files.

Run the unit tests, build twice and compare SHA256, then run
`python3 tools/native_integration.py --release dist/Backups-0.1.2.zip`.
The fixture fetches and verifies the official Paymenter v1.5.9 archive digest,
creates its own MariaDB datadir/socket, uses fake credentials, restores into a
second app/schema and removes its isolated processes. Host schemas, live apps
and providers are outside this test. Test host configuration and live providers separately from CI and isolated restore tests.

Native EditExtension loads every setting into browser state, so no secret or
encrypted rows belong to Backups. Only non-secret fields are declared. The custom
page resets write-only secrets on every response, including failed saves and
unrelated actions. Gate every page/action with `admin.extensions.update`, with
native auth/CSRF. Updates must preserve these negative integration checks.

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
DST; a fresh guard is mandatory for full mode. A scheduled native billing minute
has no catch-up guarantee after a maintenance hold.

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
