# Administrator interface

The Backups page is where a Paymenter administrator sets up private backups,
checks that they are working, and downloads or deletes individual backups.
It is a native Filament 5 page in Paymenter 1.5.9. It uses Paymenter's compiled
admin theme and Filament components only. It loads no fonts, CDN assets,
JavaScript frameworks or Tailwind utilities beyond what the theme ships.

## People and jobs

| Person | First question | Page answer |
| --- | --- | --- |
| First-time administrator | What do I need to change on this server? | Setup, per backup type: each blocker named with its exact fix (cron line, `.env` keys, service to stop) and one action. Never a count. |
| Returning administrator | Are my backups fine, and when is the next one? | Last full and last database backup, next of each, last upload to S3, cron job check-in, then history. |
| Administrator under pressure | Can I get a copy now? | A row-level Download button on every verified backup; Download from S3 for copies only in S3. |
| Administrator cleaning up | Can I remove this one safely? | A Delete preview that names what it removes and refuses when it would remove the last verified backup. |
| Administrator losing the server | Can I still restore from S3? | A recovery key flow that shows, downloads and confirms the key before encrypted uploads start. |

## Layout

One page, three tabs. "Create backup" is the only header action.

```
Backups                                                     [Create backup]
[ Backups ]  [ Settings ]  [ Help ]

  Section: Setup (only while something is open)
   All backups
    ⚠ Save your recovery key          [Show recovery key] [Download] [I saved it]
    ⓘ Test your storage                                         [Test storage]
   Database backups
    ✓ Ready. Next: 14 Oct 2026, 06:00 UTC.
   Full backups
    ⚠ Not ready. Scheduled backups can't run yet.
    ⚠ Add a lock file to this cron job
      Now: * * * * * php /var/www/paymenter/artisan schedule:run >> /dev/null 2>&1
      Replace it with: * * * * * /usr/bin/flock -n …/scheduler.lock … (copyable)
    ⚠ Paymenter stores data in Redis
      CACHE_STORE=file SESSION_DRIVER=file (copyable)
    ⚠ Confirm nothing else writes to Paymenter               [Confirm in Settings]

  Callouts, one per condition: not uploaded to S3, space full, maintenance
  mode, cleanup skipped, legacy unencrypted uploads

  Section: Overview (description list)               [Delete old backups now]
   Last full backup       13 Oct 2026, 02:00 UTC (4 hours ago)
   Last database backup   14 Oct 2026, 06:00 UTC (12 minutes ago)
   Next full backup       18 Oct 2026, 03:00 UTC
   Next database backup   14 Oct 2026, 07:00 UTC
   Last upload to S3      13 Oct 2026, 02:00 UTC, checked 2 hours ago
   Stored on              This server, S3 (my-bucket)
   S3 encryption          On, recovery key 3f9a…
   Space used             1.2 GiB of 10 GiB, 3 backups
   Cron job               Ran 1 minute ago
   Storage test           Passed 2 hours ago                    [Test storage]

  Table: Backup history                                         [Show deleted]
   | Created (UTC)  | Type             | Size    | Status   | Stored on        |
   | 13 Oct, 02:00  | Full / Scheduled | 412 MiB | Verified | This server, S3  | Download ⋯
```

Settings tab, one form, one "Save settings" button at the bottom (mod+s):

```
  First-time setup (only before the first save)
   Data directory [/srv/paymenter-backups-private]   Other public web folders
  Schedule
   ┌ Full backups ──────────────┐ ┌ Database backups ─────────────┐
   │ Frequency  Off/Daily/Weekly│ │ Frequency Off/Hourly/Daily/W. │
   │ On these days, At (billing │ │ Every N hours, At minute      │
   │ window as clock times)     │ │ (or At, On these days)        │
   │ Keep on this server  [7]   │ │ Keep on this server  [24]     │
   └────────────────────────────┘ └───────────────────────────────┘
   Time zone
  Storage
   Backup directory (placeholder = default)   legacy-folder hint
   Space limit for backups (GiB)   Minimum free disk space (GiB)
   [x] Keep backups on this server
   [ ] Upload to S3
       Endpoint, Region, Bucket, Folder in bucket, Path-style addressing
       Access key ID, Secret access key, Session token ("Saved. Leave empty to keep.")
       Keep full / database backups in S3: same number / last N / N days / all
       [x] Encrypt backups in S3
           Recovery key: (•) Generate a recovery key  ( ) Use my own passphrase
           or, once a key exists: status line, Show / Download / I saved it,
           Create a new recovery key, Discard this key
       [ ] I understand backups in S3 won't be encrypted (only with encryption off)
   Delete old backups automatically
  Full backup requirements
   Detected cron jobs and queue workers, each with status and fix line
   Detected cache, session, queue, file storage and maintenance drivers
   [ ] No other program writes to Paymenter's database or files.
   Site password (HTTP Basic), collapsed unless saved or needed
  Before backups run
   [ ] I won't update Paymenter, install extensions or run migrations while a backup runs.
  Advanced (collapsed)
   Binary paths (placeholder = detected), Time limits, Lock files (override), Folders
                                                             [Save settings]
```

Help is a set of collapsible sections with ids for deep links:
`backup-contents`, `how-backups-run`, `full-backups`, `encryption`,
`schedules`, `cleanup`, `restoring`, `command-line`. Settings sections use
`first-time-setup`, `schedule`, `storage`, `s3`, `full-backup-requirements`,
`site-password`, `before-backups-run` and `advanced`. Setup items link to
`?tab=settings#SECTION` or `?tab=help#SECTION`.

Below 640 px the history table uses Filament's stacked-on-mobile layout. Type,
Size and Stored on move to Details below 768 px (Stored on below 1024 px). The
two schedule fieldsets stack below 768 px. Commands scroll inside their block.

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
shown inline. Use Filament semantic status colors:

| Meaning | Filament color | Used for |
| --- | --- | --- |
| Verified, done, ready | success | Verified backups, completed tasks, ready types, detected items that pass |
| Waiting, in progress, advisory | info | Queued and running tasks, waiting types, advisory setup items |
| Needs a person | warning | Setup blockers, retries, verification needed |
| Broken | danger | Failed backups and tasks, damaged archives, interrupted full backups |
| Historical, off, unknown | gray | Deleted backups, cancelled tasks, types without a schedule |

* Show overview facts in one description list.
* Use status icons for independent setup conditions. Name each blocker and its
  fix without a blocker count.
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
to S3, Download from S3, Delete old backups now, Verify, Create backup.

Each sentence does one job. Never repeat an explanation in a description,
helper and status line. Don't describe the interface ("click X to Y"). Errors
say what happened and what to do; they never apologise. Name what the person
sees ("the site shows its maintenance page"), not internals.

Start times: "Starts within a minute" appears only while the cron job checked
in within five minutes and no other task is active. Otherwise the page says
when the cron job last ran and that work starts when it next runs. A full
backup's maintenance page lasts until the copy finishes, up to the backup time
limit.

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
   says the saved keys weren't changed.
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
   from 0.3: see [ARCHITECTURE.md](ARCHITECTURE.md). Download needs a recorded
   verification; S3-only copies use Download from S3 first; Delete previews the
   selected location and requires a verified survivor in each place.

## Accessibility and states

Native Filament controls provide labels, focus rings, keyboard order and dark
mode. The page adds:

* Commands are escaped Filament `Text`. Over HTTPS they copy their exact text
  by click, Enter or Space. Over HTTP Help says that copying needs HTTPS and the
  text stays selectable.
* Every destructive action uses a Filament confirmation with a danger button
  that repeats the action name.
* Loading: Filament button spinners. The table polls every 5 seconds only while
  a task is queued or running, and never while a dialog is open.
* Download links and the recovery key download are Filament SPA exceptions, so
  the browser saves the attachment instead of navigating to it.
* Action confirmations render once, before the tabs, from the extension's
  `backups::page` view. Filament 5 returns only the `action-modals` partial when
  an action mounts; without this view a confirmation opened from Settings would
  stay hidden inside the Backups tab.
* "Delete old backups now" stays in the Overview header, disabled with the
  tooltip "Nothing to delete", so the header does not jump.
* Empty history: "No backups yet" with Create backup once a type is ready.
* Engine unavailable: a danger callout "Backups can't start" with the reason and
  the offline overview command.
