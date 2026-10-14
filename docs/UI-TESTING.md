# UI workflow testing

This maps administrator controls to engine calls and isolated tests. Use a
disposable Paymenter application, database, private root, and fake credentials.
Check browser navigation, keyboard behavior, theme, mobile layout, and actual
downloads separately from native Livewire assertions.

## Workflow map

"Fixture" means the disposable pinned Paymenter application, MariaDB socket,
private root and fake credentials created by `tools/native_integration.py`.

| Workflow | Page control | Engine call and durable result | Isolated test |
| --- | --- | --- | --- |
| Status, readiness | Backups tab: setup checklist, Overview, Backup history | `overview`: whitelisted runs, requests, per-mode admission, next slot, scheduler check-in | `test_admin_workflows`, native `check` and `ui-history render` |
| Edit settings | Settings tab, Save settings | Filament rules, then `config check`, then changed native rows through Eloquent (audited). Credentials untouched | Native `check`: invalid values, disclosed hidden malformed JSON repair, strict guard refusal/native editor repair, billing window, row diff, audit, credential digest |
| Native fallback | Extensions editor | Same labels and engine-matching rules as stored strings; `updated()` advisory | Native `check`: zero, weekday 7 and relative path refused |
| S3 keys | Settings, S3 access keys for S3 or previously saved keys | Bridge `credentials-write`, or `{"clear":true}`; probe namespace kept | Native `check`: validation reset, canary absence, encryption, removal. Contract test `admin_contracts.php modals`: the Remove confirmation renders outside the tab panels |
| HTTP Basic authentication | Settings, collapsed advanced HTTP Basic section for full backups or saved credentials | Bridge `maintenance-probe-credentials-write`; origin taken on the server from Paymenter's https URL | Native `check`: browser-sent origin ignored, HTTP save refused, HTTP removal available, purpose preserved, unreadable store kept, removal keeps S3 |
| Create backup | Header action, mode choice, blocking reasons | `requests create --action run` with `mode`, `actor`; tick starts it | Native `check`: pending request, no capture in FPM |
| Cancel waiting task | Row Cancel, or More actions, Cancel task | `requests cancel` | Native `check` cancels spooled requests |
| Download | Row Download link | Panel-authenticated route, `artifact-stream`, recorded identity; bounded startup and idle waits | Native `ui-history download`: exact bytes, headers, audit; HTTP 302 without session. `test_runtime_stream`: silent, SIGTERM-ignoring and idle children stopped. Contract test `admin_contracts.php download_anchor`: with SPA mode on, the rendered link has no `wire:navigate` and no new tab |
| Verify | Row Verify or Verify again | Queued `verify` under the run lock, records identity or damage | Native: stale identity refused, queued verify restores download; unit damage test |
| Copy to S3 | More actions, Copy to S3 | Queued `deliver` | S3 protocol unit tests |
| Retrieve from S3 | Remote-only row action | Queued `retrieve` verifies and publishes a local archive before Download | S3 fixture retrieval and isolated native/browser checks |
| Delete selected copies | More actions, Delete, preview modal | `delete ID --location local|s3|all` preview; queued `delete` with location | Native: last backup refused, selected backup removed, survivor intact; unit refusal, resume, parent-swap and replaced-file tests |
| Storage test | Overview header action | Queued `destination-test` | Native `check` spools it |
| Retention | Overview header action, shown when the engine reports local or S3 candidates | Queued `retention` with `execute` | Native `check`: hidden with no backups; `ui-history retention`: offered with two verified backups and keep 1, queued only after confirmation |
| First save | Settings, Save settings with no field changed | `config check`, then `init`; rows unchanged | Contract test `admin_contracts.php first_use` |
| Recovery, export, restore | Help tab terminal commands | CLI only | `test_cli`, restore drill |

## Validation contracts

* Positive integer settings use `min:1` in both the page and native fallback,
  matching the engine. Floats and non-digit strings refuse.
* Weekdays are checkboxes on the page and a strict JSON list in the fallback.
  An enabled weekly schedule needs at least one day. Path lists are one
  absolute path per line on the page and a strict JSON list in the fallback.
* Cross-field engine checks run before persistence through `config check`, so a
  refused page save leaves native rows unchanged.
* Status is a table with dates, type, size, status, location and row actions.
  Queued, running, failed, interrupted, damaged and deleted states have their
  own labels. Request IDs and run IDs moved to Details.
* Verify records an identity that download relies on; a queued request is not
  treated as verification.

## Remaining limits

* Disabling automatic backups does not stop manual tasks. Disabling the
  native extension stops its tick.
* The scheduler check-in is the time of the last engine tick, not a guarantee
  that the server launcher keeps running.
* Download serves local archives. Remote-only copies require queued Retrieve
  from S3 first. Provider access and disk capacity can refuse retrieval.
* The page cannot move an initialized private root or run recovery.
* Browser behaviour (keyboard order, focus, dark mode, 390 px layout, a real
  download event from the Download link, a visible confirmation on the
  Settings tab) needs a real browser. The Livewire tests check state,
  rendering and secrets only. A direct authenticated GET proves the bytes but
  not the click.

## Primary source contracts

* [Admin page](../extension/Backups/Admin/Pages/Backups.php),
  [messages](../extension/Backups/Messages.php),
  [settings](../extension/Backups/Settings.php),
  [download route](../extension/Backups/Http/DownloadController.php),
  [runtime bridge](../extension/Backups/Runtime.php),
  [credential bridge](../extension/Backups/bridge.php).
* [Artifact resolution](../src/paymenter_backups/artifacts.py),
  [overview](../src/paymenter_backups/presentation.py),
  [requests](../src/paymenter_backups/requests.py),
  [delete and retention](../src/paymenter_backups/storage.py),
  [CLI dispatch](../src/paymenter_backups/cli.py).
* [Native checks](../tests/integration/native_checks.php),
  [fixture orchestration](../tools/native_integration.py).

## Copy and field help checks

The browser `inspect` phase checks that native command controls copy their exact
text with a click, Enter and Space over HTTPS. HTTP checks ensure the commands
remain selectable and automatic copying is not offered. It verifies the shell function keeps its
argument quoting. It also opens the job lock help by keyboard and click, closes
it with Close and Escape, and compares native rows and encrypted credential
digests before and after. Native `check` verifies the help dialog renders and
keeps settings and credentials unchanged. HTTP Basic purpose and the Paymenter
login distinction remain visible before credentials are saved, after saving, and
when the configured address uses HTTP. These assertions need a run against the exact release candidate before they
complete these checks. Public-directory guards remain strict: malformed JSON refuses
ordinary page saves with a named native-editor remedy, and EditExtension can
correct the validated JSON path list without changing encrypted credentials.
