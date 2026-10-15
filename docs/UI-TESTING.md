# UI workflow testing

This maps administrator controls to engine calls and isolated tests. Use a
disposable Paymenter application, database, private root, and fake credentials.
Check browser navigation, keyboard behavior, theme, mobile layout, and actual
downloads separately from native Livewire assertions.

## Workflow map

"Fixture" means the disposable pinned Paymenter application, MariaDB socket,
private root and fake credentials created by `tools/native_integration.py` or
`tools/native_ui_fixture.py`.

| Workflow | Page control | Engine call and durable result | Isolated test |
| --- | --- | --- | --- |
| Setup readiness | Backups tab, Setup: per type, named items with fixes | `overview` `configuration.readiness[mode]`, capture `admission[mode]`, `checks` | Native `check`: Create dialog names blockers once, never counts; `admin_contracts.php field_help`: deep-link targets |
| Status | Overview, Backup history | `overview`: runs, requests, `last_good`, `schedules`, `storage` | Native `ui-history render` |
| Edit settings | Settings, one Save settings | Rules, then `config check`, then changed native rows through Eloquent (audited), then `init` with row rollback | Native `check`: invalid values, GiB conversion, weekly days, no storage place, billing overlap, row diff, audit, credential digest; contract test `validation`, `baseline` |
| First v2 save | Save settings on 0.3 rows | `Settings::fromLegacy()` view; writes `settings_version=2` and every v2 key | `tests/integration/settings_migration.php` against the shared vectors |
| Malformed rows | Save settings | Defaults written and named in a warning; malformed guard JSON refused | Native `check`; contract test `repair` |
| S3 keys | Storage, Upload to S3, key fields | `Runtime::saveCredentials()` after settings and `init`; `{"clear":true}` to remove | Native `check`: incomplete pair writes nothing, canaries cleared, encrypted at rest, settings-only save keeps keys, removal keeps the site password; contract test `credentials`, `modals` |
| Site password | Full backup requirements, Site password (HTTP Basic) | Same Save; origin taken on the server from Paymenter's https URL | Native `check`: browser origin ignored, http refused, removal over http, unreadable store kept; contract test `basic_states` |
| Recovery key | Storage and Setup: Show, Download, I saved it, Create a new recovery key, Discard | `Runtime::encryptionKey()`, `revealRecoveryKey($fingerprint)` (render only; full granted identity), `recoveryKeyUrl()` | Native `check`: Save offers the key, key only in the dialog partial, confirmation needs the checkbox and activates the key; isolated page/bridge tests refuse replaced or disappeared granted keys |
| Native lifecycle | Extensions editor | No Backups setting fields; enable/disable and uninstall stay native | Forged settings Save preserves configuration, version, credentials and private bytes |
| Public folder repair | Backups settings | Damaged list requires complete replacement and explicit confirmation | Ordinary Save and invalid replacement preserve state; confirmed nonempty and empty lists repair; concurrent changes refuse |
| Saved directory pin | Standalone CLI | Explicit existing canonical directory and identity | Symlink, dot-segment and relative saved pins repair; a proposed move refuses; UUID and credentials stay unchanged |
| Create backup | Header action, type choice | `requests create --action run` with `mode`, `actor` | Native `check`: pending request, no capture in FPM |
| Cancel waiting task | Row Cancel, or More actions, Cancel | `requests cancel` | Native `check` cancels spooled requests |
| Download | Row Download link | Panel-authenticated route, `artifact-stream` | Native `ui-history download`; contract test `download_anchor` |
| Verify | Row Verify or More actions, Verify | Queued `verify` | Native `ui-history verifyFirst` |
| Upload to S3 | More actions, Upload to S3 | Queued `deliver` | S3 protocol unit tests |
| Download from S3 | Row action for S3-only backups | Queued `retrieve` | Contract test `download_anchor`; S3 fixture retrieval |
| Delete | More actions, Delete, preview | `delete ID --location` preview; queued `delete` | Native `ui-history delete`, `delete-refused`; contract test `delete_gate` |
| Storage test | Overview, Test storage; Setup item | Queued `destination-test`; `result_verified` decides "Passed" | Native `check` spools it |
| Delete old backups now | Overview header, disabled with nothing to delete | Queued `retention` with `execute` | Native `check`: disabled with nothing to delete; `ui-history retention` |
| First save | Save and finish setup with no field changed | `config check`, then `init`; rows unchanged | Contract test `first_use` |
| Recovery, export, restore | Help, Command line and Restoring | CLI only | `test_cli`, restore drill |

The native `check` and contract-test phases exercise engine readiness items,
`config check` for version 2 keys, schedule overlap refusals, and recovery-key
state.

## Browser checklist

Run `tools/browser_ui_checks.mjs` against a disposable `ui-` fixture, then check
by eye:

`--phase all --allow-fixture-mutations` exercises Settings,
recovery-key download/confirmation, history lifecycle and damaged-survivor edge,
manual cleanup, Upload to S3 and S3-only retrieval with an actual Download event.
Every manual capture explicitly selects **Database backup**; seeded captures
also pass `--mode database-online`. This browser lane never opts into full
captures. Its workers always use `--loopback-s3`, whose fixture-only SDK transport
refuses bindings other than `https://browser-s3.invalid`, the fake browser bucket
and fixed fake keys. Objects and request traces stay under the owned fixture.
These checks establish browser/protocol behavior; they do not validate provider
TLS, real S3 or full-backup admission. The downloaded recovery key and screenshots
are private fixture output and must stay out of Git.

Settings choices use deferred browser state and `visibleJs`; the harness awaits
field visibility/state after changing them. It waits for a Livewire POST only
for Save and actions that run on the server. Waiting for a POST after a schedule,
storage or encryption choice would hang a valid client-only interaction.

* Backups tab on a fresh fixture: Setup names the cron line, copyable over
  HTTPS and selectable over HTTP; database backups read Ready or Not ready
  without listing full-only items; nothing is counted.
* Settings: both schedule fieldsets side by side from 768 px and stacked on a
  390 px phone; hourly shows the run times; the billing window shows clock
  times; turning Upload to S3 on reveals S3 fields, keys, retention per type and
  encryption; turning encryption off reveals the unencrypted confirmation.
* One Save: an incomplete key pair, missing bucket, over-byte-limit Unicode
  credential or passphrase with a Unicode control character keeps every native
  row and the credential file unchanged and clears the typed secrets.
* Save with encryption on and no key: the "Show the recovery key?" confirmation
  opens; the key shows once in its dialog, copy works over HTTPS, Download
  saves a file, "I saved it" needs the checkbox; afterwards Setup no longer
  lists the key. Replace a pending key while its confirmation is open: the
  original confirmation must refuse and never activate the replacement.
* Full backup requirements: detected jobs with their current and replacement
  lines, detected drivers with the `.env` change, the confirmation, and the
  collapsed Site password section.
* Create backup: one callout naming what's missing, one info line, disabled
  types not selectable, "See what's needed" opens Setup. Missing, unconfirmed
  or invalid archive keys and an unavailable encryption tool explain why
  uploads wait without preventing admissible local capture. Billing window
  labels match the engine's HH:MM clock values, including midnight wrap.
* History: Created column names the time zone, Show deleted appears only with
  deleted backups, Delete preview names what is removed and refuses the last
  usable backup.
* Light and dark, keyboard focus, no horizontal overflow at 390 px.

The browser fixture's `worker` control admits capture requests only when their
durable arguments explicitly select `database-online`. Full, omitted and
unrecognized modes refuse before request promotion, leaving the pending/deferred
request and attempt count unchanged and starting no engine process. Database
captures and non-capture actions remain available. `worker --allow-full` is an
explicit opt-in for an authorized disposable full-capture drill; it defaults off.
The guard tests mock execution even when checking that opt-in. The fixture
corruption helper resolves the installation-bound journal and owned artifact
location, confined to its recorded default directory (`backups` for new
bindings, `artifacts` for legacy bindings).

## Remaining limits

* Disabling a schedule does not stop manual tasks. Disabling the native
  extension stops its tick.
* The cron check-in is the time of the last engine tick, not a guarantee that
  the server's cron job keeps running.
* Secrets are reset on every response. Form choices show and hide fields in
  the browser, so they don't clear typed keys; a validation error or table
  refresh still does, and the page then asks for them again inline.
* The page cannot move an initialized data directory or run recovery.
* Browser behaviour (keyboard order, dark mode, 390 px layout, a real download
  event, clipboard) needs a real browser. Livewire tests check state,
  rendering and secrets only.

## Primary source contracts

* [Admin page](../extension/Backups/Admin/Pages/Backups.php),
  [messages](../extension/Backups/Messages.php),
  [settings](../extension/Backups/Settings.php),
  [download route](../extension/Backups/Http/DownloadController.php),
  [runtime bridge](../extension/Backups/Runtime.php).
* [Native checks](../tests/integration/native_checks.php),
  [admin contract tests](../tests/integration/admin_contracts.php),
  [settings migration vectors](../tests/integration/settings_migration.php),
  [browser checks](../tools/browser_ui_checks.mjs),
  [fixture orchestration](../tools/native_integration.py).
