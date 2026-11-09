# Administrator interface

The Backups page is where a Paymenter administrator sets up private backups,
checks that they are working, and downloads or deletes individual backups.
It is a native Filament 5 page in Paymenter 1.5.9. It uses Paymenter's compiled
admin theme and Filament components. An optional shared admin skin styles the
existing sheet and row classes; without it, the same page stays plain Filament.
The extension loads no fonts, CDN assets or JavaScript frameworks.

## Downloads hold secrets

Download is for staff with the `admin.extensions.update` permission. It saves a
plain, unencrypted `.tar.gz`. A full backup includes `.env`, with APP_KEY and
database credentials, plus Passport keys and customer data. S3 encryption applies
only to S3 copies when **Encrypt backups in S3** is on; it does not encrypt staff
downloads, including copies retrieved from S3.

Save downloads in a private folder outside web roots. Encrypt them before keeping
or sharing them, store that key or passphrase separately, and remove temporary
plain copies after use. Never attach them to tickets or commit them to a repository.

## People and jobs

| Person | First question | Page answer |
| --- | --- | --- |
| First-time administrator | What do I need to change on this server? | Setup, per backup type: each blocker named with its exact fix (cron line, `.env` keys, service to stop) and one action. Never a count. |
| Returning administrator | Are my backups fine, and when is the next one? | Last full and last database backup, next of each, last upload to S3, cron job check-in, then history. |
| Administrator under pressure | Can I get a copy now? | A row-level Download button on every verified backup; Get from S3 for copies only in S3. |
| Administrator cleaning up | Can I remove this one safely? | A Delete preview that names what it removes and refuses when it would remove the last verified backup. |
| Administrator losing the server | Can I still restore from S3? | A recovery key flow that shows, downloads and confirms the key before encrypted uploads start. |

## Layout

One page, three native tabs: Backups, Settings and Help. "Create backup" is
the only header action. Detail content caps at Filament's SixExtraLarge width.

Backups shows Setup needed (only while something blocks), current alerts,
Schedule and Storage in one sheet, with a Checks rail beside it. Facts use a
label, value and optional action row. Setup keeps every blocker, exact command
and existing recovery-key action. Run cleanup sits on the Keep row; Test storage
on the This server row. The history table has its own surface below the sheet.
Details is a dialog in the same sheet style: What went wrong (failed only),
Backup with Started, Finished and Took ("Not recorded" for backups made before
finish times were recorded), Location, Check, Restore (the export command in a
one-line box) and Keep, which says what cleanup does on this server and in S3,
each under its own policy. Create backup picks the type with a select.

Settings is one form in one sheet, with one Save button (mod+s): Schedule,
This server, S3, Cleanup, Full backups need and Advanced, with Full backup
checks beside it. Schedule groups sit side by side on wide screens and stack
below 768 px.
Fields, conditional visibility, validation and secret-handling flows stay native.
Help joins its collapsible sections into a sheet with padded prose and commands.

History searches the entire eligible array before paging, including backup and
task IDs, type, origin, status, failure detail, location and displayed size.
Words match case-insensitively and all words must match. Show deleted still
controls eligibility. The default is 25 rows, with 25, 50 and 100 choices and one
pager. Columns can hide Type, Size, Status and Stored on; Created stays visible.
A search with no matches names the next step instead of offering first-backup setup.

Absolute dates in facts, setup, history, Details and the delete confirmation use
the viewer's time zone, with the zone written. UTC stays in the title and is the
fallback when JavaScript is unavailable. Relative-time and S3 check notes remain.

Help is a set of collapsible sections with ids for deep links:
`backup-contents`, `how-backups-run`, `full-backups`, `encryption`,
`schedules`, `cleanup`, `restoring`, `command-line`. Settings sections use
`first-time-setup`, `schedule`, `storage`, `s3`, `full-backup-requirements`,
`before-backups-run` and `advanced`. Setup items link to
`?tab=settings#SECTION` or `?tab=help#SECTION`.

Below 640 px the history table uses Filament's stacked-on-mobile layout. Type,
Size and Stored on move to Details below 768 px (Stored on below 1024 px). The
two schedule groups stack below 768 px. Commands scroll inside their block.

### Readiness

Setup renders `configuration.readiness[mode].items` from the engine. The page
never reconstructs policy: an item's code and safe detail become a title, one
line of explanation, exact fixes and at most one action through
`Messages::readinessItem()`. Items present for both modes appear once under
"All backups". Items only blocking full backups appear under "Full backups", so
the administrator always sees that database backups work. Repeated fix lines
show once per group. The page adds only its own preconditions: "Save your
settings" before the first save, an invalid configuration, the recovery key
confirmation, and an advisory "Test your storage".

Create backup uses the engine's per-mode `configuration.admission` to
distinguish capture blockers from delivery setup. Per-mode readiness remains
the source of named setup items; its state can need setup for uploads while
local capture is admissible. Older envelopes without admission fall back to
readiness and the documented delivery advisories. Waiting codes (billing window,
a busy cron job lock, another task) and S3 key and encryption setup never block
creating a backup on this server; uploads wait for them instead. Billing
`checks.billing.window_before` and `window_after` are the authoritative HH:MM
clock times displayed by the page, including windows crossing midnight.

### Recovery key

When "Encrypt backups in S3" is on and no key exists, Save creates one: a
generated key by default, or the administrator's passphrase (at least 20
characters, at most 1024 UTF-8 bytes, with no Unicode control characters). Save
then opens "Show the recovery key?". Confirming renders the key once in its dialog with Download recovery key and I saved it. The block
stays in Setup and in Storage until the key is confirmed with the checkbox "I
saved this recovery key outside this server". Uploads start only after that.
Create a new recovery key rotates: new uploads switch after the new key is
confirmed; earlier copies keep needing their own keys.

## Visual decisions

Use Paymenter's native theme, typography, and components. Keep the hierarchy
quiet and make Setup prominent, with each backup type's blockers and fixes
shown inline. Plain Filament keeps semantic status colors. The optional skin
uses an explicit history status-to-tone map for its hex marks; unknown status
words keep the native appearance. Colors alone never set a mark:

| Meaning | Filament color | Used for |
| --- | --- | --- |
| Verified, done, ready | success | Verified backups, completed tasks, ready types, detected items that pass |
| Waiting, in progress, advisory | info | Queued and running tasks, waiting types, advisory setup items |
| Needs a person | warning | Setup blockers, retries, verification needed |
| Broken | danger | Failed backups and tasks, damaged archives, interrupted full backups |
| Historical, off, unknown | gray | Deleted backups, cancelled tasks, types without a schedule |

* Show overview facts as shared label/value/action rows.
* Name each setup blocker and its exact fix without a blocker count.
* Render lists with `UnorderedList` and commands with `Text` in the theme's
  monospace font. Commands are copyable over HTTPS. At 375 px each command line
  stays whole (`white-space: pre`) and scrolls sideways (`overflow-x: auto`), so
  words such as `php` never split.
* Put field help in the field's helper text.
* Use sentence case and plain labels without decorative headings or metadata.

## Language rules

Write for a hosting administrator who knows backups but not this engine. Use
the industry words: cron job, binary, data directory, retention, maintenance
mode, access key ID, secret access key, endpoint, path-style addressing, folder
in bucket, recovery key, site password (HTTP Basic). Fixed action names: Upload
to S3, Get from S3, Run cleanup, Verify, Create backup.

Each sentence does one job. Never repeat an explanation in a description,
helper and status line. Don't describe the interface ("click X to Y"). Errors
say what happened and what to do; they never apologise. Name what the person
sees ("the site stays online"), not internals.

Start times: "Starts within a minute" appears only while the cron job checked
in within five minutes and no other task is active. Otherwise the page says
when the cron job last ran and that work starts when it next runs. A full
backup keeps the site online and pauses cron jobs only for the database and file copy.

Details may show a safe error code labelled "Error code"; private paths,
stderr and JSON never appear. Engine codes, statuses, setup items and task names
come from `extension/Backups/Messages.php`.

## Safety seam

These rules bind the page to the engine. The engine stays authoritative.

1. **No heavy work in PHP-FPM.** The page writes durable requests. The
   scheduler tick launches capture, verification, upload, download from S3,
   cleanup and deletion. Page loads read a whitelisted `overview`.
2. **Whitelisted presentation.** `overview` returns dates, modes, sizes, status
   codes, copy locations, safe error codes, readiness items with safe scalar
   detail, detected jobs as reconstructed display commands, store drivers and
   key fingerprints. It never returns archive or manifest paths, raw crontab
   lines, process identities, request results, stderr or secrets. Table data is
   recomputed per request and never stored in a public Livewire property.
3. **One Save, validated before persistence.** Save validates the settings form
   and every secret group in one pass (plain Laravel rules, no closures in
   `rules()`), so one response shows every problem. Pure shared credential
   and archive-secret validation also runs before writes, enforcing the
   bridge's UTF-8 byte limits and control-character rules without changing
   secret bytes. Secret groups are validated
   only when entered; S3 keys must be a complete pair, and are required only
   when uploads are on and no keys are saved. The page then compares submitted
   values with native rows read on the server, sends the changes to
   `config check`, then calls `Runtime::saveConfiguration()` with settings and
   entered secret groups. The backend takes bounded configuration/tick locks,
   rereads the native baseline, publishes changed rows through audited Eloquent,
   initializes private state, publishes credentials while native settings rows
   are locked, and synchronizes schedules. A definite failure before credential
   commit restores only unchanged rows, preserving concurrent edits and absent
   rows. Rejected saves preserve existing schedule epochs and waiting requests.
   Partial outcomes name what completed. If a child loses its commit response,
   a durable private intent pauses credential-bearing backup work; refresh shows
   setup required and the administrator resubmits the intended settings and each
   uncertain credential group to complete Save. Recovery-key creation follows
   as a separate `Runtime::encryptionKey()` operation; its failure warns that
   settings were saved and the key was not created. Secret inputs are cleared
   on every response.
4. **Settings versions.** Rows with `settings_version` absent or 1 keep their
   0.3 meaning. The page shows them through `Settings::fromLegacy()`, the PHP
   half of the shared v1-to-v2 adapter, and proposes encryption on. The first
   Save writes `settings_version=2` and every version 2 key explicitly, and
   clears the old plaintext acknowledgement. Legacy uploads stay plaintext until
   that save.
5. **Guards that never default.** Malformed non-guard rows show trusted defaults,
   are rewritten by the next save and are named in a warning. Malformed
   public-directory guard JSON stays refusing until the complete list is entered
   and explicitly confirmed in Backups settings. An empty list requires the same
   confirmation. Concurrent native-row changes refuse that repair. A change to
   an initialized data directory is refused.
   An unusable stored data-directory pin is corrected through the documented
   standalone CLI bound to the existing canonical directory and identity.
   When the engine itself cannot start, Save writes only changed binary paths,
   the data directory and public web folders ("Setup settings saved").
6. **Secrets are write-only.** `$s3Keys`, `$siteAccess` and the passphrase in
   `$encryptionInput` are reset by `dehydrate()` on every response, including
   validation failures and unrelated actions. They render inside the one form
   through embedded schemas, without nested `<form>` elements. The site
   password's address is Paymenter's own https origin, taken on the server; an
   http address offers no site password field. Saving secrets never creates
   private storage. Show and hide inside the form run in the browser
   (`visibleJs`), so choosing options never makes a round trip that clears
   typed secrets. If a response does clear typed secrets unsaved (for example
   a validation error), an inline note asks for them again, and a later Save
   says the saved keys weren't changed. The browser cannot send the site
   password's address. Explicit removal of either the S3 keys or the site
   password preserves the other under the shared credential flock.
7. **The recovery key never enters Livewire state.** `Runtime::revealRecoveryKey($fingerprint)`
   is called only while rendering the confirmed "Recovery key" dialog and is
   cached for that request only. The key is in that dialog's HTML and nowhere
   else: not in public properties, action data, notifications or the overview.
   Table polling pauses while any dialog is open. The dialog renders only with
   a one-time grant that the confirmed "Show the recovery key?" step (or a
   confirmed rotation) puts in the session, bound to the administrator and the
   key's full fingerprint, valid for 30 seconds and then for two minutes on that
   component; a direct mount or `?action=recoveryKey` link shows nothing.
   The displayed value and Copy preserve exact UTF-8 bytes, including leading,
   repeated and trailing spaces in an imported passphrase. HTML is escaped;
   only the value preserves whitespace, so native template indentation is not
   displayed or copied. Click, Enter and Space use the same native Copy handler.
   Only the nonsecret fingerprint is kept (locked) to bind the confirmation to the key that was
   shown. The bridge checks the granted fingerprint under its credential lock;
   replacement or disappearance refuses disclosure. Runtime and the renderer
   also check the returned fingerprint. Opening the acknowledgement dialog
   freezes that exact fingerprint;
   replacement of the pending key requires a new confirmation and never
   redirects an existing confirmation to the replacement key. Download uses `Runtime::recoveryKeyUrl()`, a fresh five-minute signed
   link opened by a server action, outside SPA navigation.
8. **Safe artifact resolution, verified downloads, per-copy delete.** Unchanged
   from 0.3: see [ARCHITECTURE.md](ARCHITECTURE.md#artifact-resolution-download-and-per-backup-delete).
   Download needs a recorded
   verification; S3-only copies use Get from S3 first; Delete previews the
   selected location and requires a verified survivor in each place.

## Administrator page contracts

`overview` is the page's only status source. It returns fixed codes, run and
request IDs, timestamps, sizes, booleans, configured nonsecret settings,
per-mode admission codes, the next schedule slot and the scheduler's last
check-in. It never returns artifact or manifest paths, manifests, grants,
process identities, request results, stderr or credentials; credentials appear
only as saved/not saved. Every engine run of `tick --execute` writes a private
`scheduler.json` check-in. `status` and `list` keep their 0.1 output.

`observe` reads native settings without credentials or capture-tool probes and
returns scheduler and copy-freshness incident codes. It never ticks, launches
work, takes writer locks or changes journals. Full and database checks are
separate, each with local and offsite checks for the selected storage policy.
A full copy counts for database only through the runtime's durable satisfied
request/coverage proof. Local copies require current archive/manifest identity;
offsite freshness uses successful delivery receipts for the current destination
and excludes copies already observed missing, damaged or unknown. This is not
a new remote integrity scan or restore proof.

Freshness expires after the next scheduled slot following the last usable
snapshot (or the saved schedule epoch for a first copy), plus a recovery window.
The window is at least 15 minutes, otherwise three capture attempts with their
capture/compression and lock timeouts plus retry delays. Offsite adds the
configured transport timeout/retry window and five minutes. Schedule Off skips
freshness; a stopped scheduler matters only while a schedule is enabled. Its
every-minute check-in gets a five-minute grace. Terminal failed/expired backup
requests alert without waiting for freshness; deferred retries stay quiet until
freshness expires. Direct failed CLI captures have no request retry and alert
from their terminal journal. No new settings are introduced.

Host cron invokes `backups:observe` independently of `schedule:run` and queue
workers. It sends synchronous mail to Paymenter's system email address and
respects the system mail disable switch. A private `incidents.json` ledger and
observer-only lock serialize checks. One incident key per type/location stays
open through subsequent failed slots/retries and closes after a healthy check;
the next failure gets a new key. Mail transport failure retains an unsent
incident for the next host check. A failed engine read preserves existing
incidents and alerts on a later host check after a four-minute hold-off. This
allows for command runtime so the next five-minute check can send mail. The
observer does not restart unknown host processes or override recovery guards:
the request runtime remains the recovery owner. See [INSTALL.md](INSTALL.md)
for the independent host job and its monitoring limits.

Settings saved on the page pass plain Laravel field rules that match the engine
(positive integers stay positive, weekdays are 0 to 6 and required for an
enabled weekly schedule, path lists are absolute, the S3 prefix has no empty,
`.` or `..` parts or backslashes), then `config check`, which
uses engine normalization and validation plus the backup-directory location
rule without writing. `config validate` checks saved values against the same
schema. Setup repairs and schedule Off remain saveable. Enabling or editing an
enabled schedule requires installation readiness before any native publication:
tools, private/local storage, discovered and recently running scheduler,
recent independent observer check, per-type static capture prerequisites, and
current destination/key test when uploads are selected. Safe local preparation
precedes a refusal. Transient billing/lock waits remain runtime admission checks;
this gate neither captures nor takes writer locks. The page passes changes and entered credential groups to
`Runtime::saveConfiguration`, backed by CLI `config set`. The engine acquires
`config.lock` then `tick.lock` with one ten-second acquisition deadline, or
refuses `busy` before publication. It rereads and validates the authoritative
native baseline under those locks. Only changed native rows are written through
Eloquent; the internal Artisan writer inherits the owning lock descriptors and
restores the authenticated page actor, with console auditing explicitly enabled.
The resolved database connection travels to scrubbed children on private stdin.
The same private handoff carries the parent's resolved framework store defaults,
drivers, connection names and storage paths. The cold bridge applies this
nonsecret whitelist after loading configuration, so readiness uses the running
application's stores even when uncached config depends on environment overrides.
Provider URLs, credentials and connection definitions are excluded. Detached
workers and coordinated native writers preserve the snapshot on stdin. Native
writers validate it without changing their operational store configuration. Direct
CLI calls without a parent snapshot keep using cold configuration.
The Python runtime passes only Laravel's fixed `DB_*` connection fields to
Artisan during bootstrap, when extension discovery needs the database before
the command can read stdin. Other inherited environment values stay scrubbed;
database credentials are not written to files or command arguments. Historical maintenance captures used the same allowlist for Artisan. The billing
guard runs inside Paymenter's ordinary scheduler process, not a scrubbed child.
The browser cannot supply the baseline or actor. The writer returns persisted
row receipts and a full native snapshot. Initialization, local storage preparation
and privacy-policy publication remain beneath the same locks, including a first save with no edits.

Before publishing entered secrets, a second native transaction locks every
extension settings row, verifies the full snapshot, then invokes the cold
`credentials-merge` bridge while the row locks remain held. A concurrent native
editor therefore either precedes the snapshot comparison (`config-changed`) or
waits until credential publication has finished. Schedule synchronization follows
successful credential publication; rejected initialization/credential saves do
not reset an unchanged schedule epoch or supersede its waiting requests.
Worker, direct-command and storage reloads acquire `config.lock` around the whole
native-settings/credential snapshot. Tick uses config-before-tick lock order.
Standalone initialization also rereads settings and writes policy/schedule under
these locks; direct Artisan saves use the same coordinator.

A definite failure before credential commit compares and restores each unchanged
published row under a database row lock. Concurrent updates remain, and formerly
absent rows are deleted through audited Eloquent. Rollback failure or concurrent
changes are explicit partial outcomes. Before publication, the coordinator
snapshots the bytes and existence of `schedule.json`, `policy.json` and
`configuration-advisory.json`. A definite rollback restores these exact snapshots
and fsyncs their publication while both coordination locks remain held, including
failures after schedule synchronization or intent removal. If concurrent native
rows survive rollback, synchronization starts from the restored schedule snapshot
and changes only affected epochs; privacy policy reflects the surviving rows.
Waiting obligations from unchanged schedules retain their original epochs.
A private, secret-free `config-save.json`
intent exists before publication. If a child loses its receipt or times out
across a possible commit, the engine keeps this intent and returns
`outcome_unknown`; it never restores destination rows across an ambiguous
credential commit. Credential-bearing loads refuse
`config-save-outcome-unknown`, including already-locked tick reloads, and overview
shows setup required. A subsequent complete Save must resubmit each credential
group requested by that intent (or explicitly clear it), then removes/fsyncs the
intent. Settings-only Save and `init` cannot clear an uncertain secret outcome.
A rejected retry preserves the earlier intent even when the retry's own native
rows roll back successfully. Only confirmed publication of every requested
credential group, including explicit clearing, resolves that earlier uncertainty.
Intent-removal fsync failure republishes the guard before reporting uncertainty;
workers remain paused if rollback or repeated cleanup also fails.
A successful credential commit followed by a setup/advisory failure keeps the
coherent settings/keys and reports an explicit partial success.

The short native writer/finalizer runs beneath an owned Linux supervisor group.
The supervisor retains the inherited coordination descriptors, kills the PHP
writer and its credential bridge descendants on timeout, and uses a
parent-death signal to kill the whole group if the engine disappears. A retry
cannot acquire released coordinator locks while an orphan credential child can
still commit. Public output remains fixed codes and confirmed outcome flags.

Settings-only `config check`, `init`, CLI settings updates and schedule updates
use bridge `read-settings`, which never opens credentials.json or constructs an
encrypter. The pure `credentials-check` validates submitted groups before any
native publication. `credentials-merge` accepts `{s3: object|null, site: object|null}`;
null/omitted groups preserve saved values, `{clear:true}` removes only that
namespace, and missing session_token clears the previous token when replacing
S3 keys. One credential flock covers decrypt/merge/encrypt/fsynced publication
and preserves archive keys and other namespaces. Recovery-key creation remains
a separately reported operation after the coordinated configuration save.
The page refuses to change an initialized private root, because a new root
would start a new identity without the saved keys and history. When the engine
cannot start, only private root, document roots and Python/PHP pins can be
repaired, without secret publication; an initialized root still uses the bounded
configuration/tick locks. The Backups page is the sole web settings editor;
the native Extensions page retains enable/disable and uninstall, exposes no
Backups setting fields (only a read-only link to the Backups page, which is never
stored), and its Save hook performs no configuration or private state work. CLI native writes validate against the internal `Settings::fields`
schema independently of `getConfig()`.

The private root is owned by the site user with mode 0700. Before the first
installation identity is written, only Backups' `identity.lock` and
`billing-guard.json` may already exist; each must be a regular owner-private
file. Any other entry, including saved credentials or backup sets without an
installation identity, remains an orphaned-state refusal.
Administrator repair guidance uses the persisted nonempty private-root pin even
when the installation marker cannot be inspected. Older installations left the
default pin blank both before and after Save, so an inaccessible default cannot
be distinguished from unused storage. Treat that ambiguity as requiring repair
before replacement. A readable, searchable default without an installation marker
retains first-use guidance. Overview, Save and save errors share this decision.
When a saved directory cannot be identified as Backups data, the guidance asks
the administrator to confirm its contents before restoring ownership and access.
An identified saved directory keeps direct repair instructions.
Malformed public-folder lists remain refusing until an administrator explicitly
enters and confirms the complete list on Backups settings, including an
intentional empty list. Bootstrap repair validates those folders before writing,
compares the exact native row snapshot again under native row locks and, for an
initialized root, configuration/tick locks. Fresh repair uses a native snapshot
transaction before initialization. It preserves an initialized directory and identity, and publishes no
secrets or settings-version adoption. Ordinary Save never repairs this guard
from defaults. Until repair succeeds, exports still refuse unknown boundaries.
Implicit default repairs for unrelated damaged settings are deferred until the
next ordinary Save, so they cannot block a folder-list-only bootstrap repair.
A saved private-root pin that is unusable because it is relative, contains
dot segments or names a symlink is corrected through the standalone CLI,
explicitly bound to the existing canonical private directory. State validates
its application/owner/identity; the operator first verifies the known UUID with
offline status. Config-set refuses a proposed root different from that bound
directory. With that same canonical directory, correction preserves UUID and
credentials and moves no private state. A guessed directory is not an identity
proof; config-set can initialize fresh state when explicitly selected.

Requests carry an optional numeric `actor` and, for `run`, an optional `mode`.
A repeated identical waiting manual request returns the existing request.

## Accessibility and states

Native Filament controls provide labels, focus rings, keyboard order and dark
mode. The page adds:

* Commands are escaped Filament `Text`. Over HTTPS they copy their exact text
  by click, Enter or Space. Over HTTP Help says that copying needs HTTPS and the
  text stays selectable.
* Every destructive action asks first, with a danger button that repeats the
  action name (Delete backup, Run cleanup).
* Loading: Filament button spinners. The table polls every 5 seconds only while
  a task is queued or running, and never while a dialog is open.
* Download links and the recovery key download are Filament SPA exceptions, so
  the browser saves the attachment instead of navigating to it.
* Action confirmations render once, before the tabs, from the extension's
  `backups::page` view. Filament 5 returns only the `action-modals` partial when
  an action mounts; without this view a confirmation opened from Settings would
  stay hidden inside the Backups tab.
* Run cleanup stays on the Keep row, disabled with the tooltip "Nothing to
  delete", so the row does not jump.
* Empty history: "No backups yet" with Create backup once a type is ready.
* Engine unavailable: a danger callout "Backups can't start" with the reason and
  the offline overview command.
