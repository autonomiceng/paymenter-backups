# Administrator interface

The Backups page is where a Paymenter administrator sets up private backups,
checks that they are working, and downloads or deletes individual backups.
It is a native Filament 5 page in Paymenter 1.5.9. It uses Paymenter's compiled
admin theme and Filament components only. It loads no fonts, CDN assets,
JavaScript frameworks or Tailwind utilities beyond what the theme ships.

## People and jobs

| Person | First question | Page answer |
| --- | --- | --- |
| First-time administrator | What do I need to do before backups work? | A setup checklist of at most a few lines (save settings, scheduler, host checks), and the settings form. |
| Returning administrator | Are my backups fine, and when is the next one? | Last good backup, next scheduled backup, storage, scheduler check-in, then history. |
| Administrator under pressure | Can I get a copy now? | A row-level Download button on every verified backup. |
| Administrator cleaning up | Can I remove this one safely? | A Delete preview that names every copy it removes and refuses when it would remove the last verified backup. |

## Layout

One page, three tabs. "Create backup" is the only header action.

```
Backups                                                     [Create backup]
[ Backups ]  [ Settings ]  [ Help ]

  Callout (only while something is open)
  Finish setup
   • Paymenter's scheduler has not run yet. Add its cron job from the Help tab.
   • Full backups need 3 more host checks. Confirm them in Settings, under Host checks.
                                         [Open settings] [Show cron setup]

  Section: Overview (description list, not stat cards)   [Test storage] [Apply retention]
   Last verified backup 13 Oct 2026, 02:00 UTC, 4 hours ago
   Next automatic backup Off. Turn on automatic backups in Settings.
   Stored on            This server (or: This server and S3 bucket NAME)
   Space used           1.2 GiB of 10 GiB, 3 backups
   Scheduler            Last ran 1 minute ago

  Section: Tasks (only while tasks are queued, running or recently failed)
   | Task                  | Requested     | Status                  |        |
   | Create full backup    | 2 minutes ago | Waiting for scheduler   | Cancel |

  Table: Backup history                                            [Refresh]
   | Created ▼        | Type          | Size    | Status   | Stored on      |            |
   | 13 Oct, 02:00    | Full          | 412 MiB | Verified | This server    | Download ⋯ |
   | 12 Oct, 02:00    | Database only | 38 MiB  | Failed   | Not stored     |          ⋯ |
```

Settings tab, in reading order: What to back up, Schedule, Storage, Host
checks, Retention and space, Advanced (collapsed: time limits, folders and
programs), then the "Save settings" button. Host checks shows only what the
chosen backup type needs: the schema-change confirmation for both types, plus
cron lock files and two more confirmations for full backups. The Backup folder
placeholder shows the exact default folder; the Overview only says "This
server". Below the form, S3 access keys appear once S3 is chosen and Site
password appears for full backups. Each starts by saying it is saved
separately and encrypted, and its buttons name it ("Save S3 keys", "Save site
password"). The site password uses Paymenter's own https address, shown as
text; an http address offers no site password and says why. Before the first
save, both sections say to save settings first.

Below 640 px the history table uses Filament's stacked-on-mobile layout: each
row lists Created and Status with its full detail, then Download and More
actions together on a separate line. Type, Size and Stored on stay in Details below
768 px (Stored on below 1024 px). Long commands on the Help tab wrap inside
the page.

Help tab: what a backup contains, how tasks and the scheduler work with a
cron line that runs `schedule:run` through `flock` and a lock file (keep an
existing flock line), a shell function defined once so each terminal command
is one short, shell-quoted line, recovery split into a read-only check and the
execute step, export, the restore guide's path and the installation ID.

Apply retention appears only when verified local backups exceed the number to
keep. The deleted-backups filter appears only when a deleted backup exists.
Create backup disables a backup type that cannot start and shows why, with
Open settings; when neither type can start the dialog only offers Close. The
empty history offers Create backup once setup is complete, and otherwise says
to finish setup first.

## Visual decisions

Use Paymenter's native theme, typography, and components. Keep the hierarchy
quiet and make Setup prominent, with each backup type's blockers and fixes
shown inline. Use Filament semantic status colors:

| Meaning | Filament color | Used for |
| --- | --- | --- |
| Verified, done | success | Verified backups, completed tasks |
| Waiting, in progress | info | Queued and running tasks, backups being created |
| Needs a person | warning | Setup items, retries, verification needed |
| Broken | danger | Failed backups and tasks, damaged archives |
| Historical | gray | Deleted backups, cancelled tasks |

* Show overview facts in one description list.
* Use status icons for independent setup conditions.
* Lead backup history with dates and keep run IDs in Details.
* Use sentence case, native components, and semantic status colors.

## Language rules

Write for a hosting administrator who knows backups but not this engine.
Never promise a start time or a pause length the host has not shown: queued
work starts when Paymenter's scheduler next runs, and a full backup's
maintenance page lasts until the copy finishes, which depends on the data and
the time limits under Advanced.
Name what the person sees ("site shows a maintenance page"), not internals
("native prerendered maintenance"). One action keeps one name: the button
"Create backup" produces the notice "Backup queued". Errors say what happened
and what to do next. They never apologise and never show raw engine codes,
paths from journals, stderr or JSON. All UI strings come from
`extension/Backups/Messages.php`, so the same code always reads the same way.

## Safety seam

These rules bind the page to the engine. The engine stays authoritative.

1. **No heavy work in PHP-FPM.** The page writes durable requests. The
   scheduler tick launches capture, verification, copy to S3, retention and
   deletion. Page loads read a whitelisted `overview` from the engine.
2. **Whitelisted presentation.** `overview` returns dates, mode, size, status
   codes, copy locations and safe error codes. It never returns archive or
   manifest paths, manifests, database grants, process identities, request
   results or stderr. The table data is recomputed per request and never
   stored in a public Livewire property.
3. **Settings validation before persistence.** The form mirrors the engine's
   field rules with plain Laravel rules (no closures, which Filament would
   evaluate as its own utilities). On save the page compares the submitted
   values with native rows read on the server, sends the changes to
   `config check`, which runs the engine's normalization, validation and
   schedule/billing admission without writing, then writes changed native rows
   through Eloquent, which keeps native auditing, and runs `init`. If `init`
   fails, the rows are put back and the page says the settings were not saved.
   The first save initializes private storage even with no field changed. A
   change to an initialized private folder is refused. Field errors open a
   collapsed section and scroll to the field, and a notification says to check
   the highlighted fields. Saving settings never opens or rewrites the
   encrypted credential file.
4. **Secrets are write-only.** S3 keys and the site password live in public
   Livewire arrays that `dehydrate()` resets on every response, including
   validation failures and unrelated actions. The page shows only "Saved" or
   "Not saved". The site password's address is Paymenter's own https origin,
   taken on the server. Saving either secret needs initialized private
   storage; it never creates it.
5. **Safe artifact resolution.** `artifacts.py` is the one place that turns a
   run ID into files. It requires the strict run ID grammar, an owner-private
   journal for this installation, recorded names exactly `RUN.tar.gz` and
   `RUN.manifest.json` in one canonical, owner-only directory outside the app
   and every declared document root, and opens each file with `O_NOFOLLOW`
   relative to that directory. Files must be regular, owned by the app user,
   mode 0600 and have one link. Verify, download, retention and per-backup
   delete use it.
6. **Download needs a recorded verification.** Capture and queued
   verification record the archive identity (device, inode, size, mtime and
   ctime in nanoseconds) and SHA-256 after a full check. The download route
   accepts only a run ID, checks native auth and `admin.extensions.update`,
   and starts the engine's `artifact-stream`. The engine opens the archive
   once, compares the open descriptor to the recorded identity and the manifest
   digest, and streams from that descriptor. It hashes while streaming and
   withholds the final chunk until the hash matches, so a changed file never
   arrives complete. A missing or stale record answers "Verify this backup
   first" and offers the Verify action. Status alone never enables Download.
7. **Per-backup delete is a request.** The preview names the local files and
   the S3 objects it removes. The engine refuses when the backup is the latest
   S3 copy, when no other backup passes full verification at execution time,
   when another task targets it, or when its S3 copy is outside the configured
   bucket and prefix. Execution holds the run lock, records `delete_intent`
   before removing anything, removes the S3 manifest before the archive and the
   local manifest before the archive, and resumes from the intent on retry.
   Each local unlink goes through the verified directory descriptor and first
   checks that the name still has the device and inode checked in the same
   attempt.
   Global retention settings are never changed to delete one backup.

## Accessibility and states

Native Filament controls provide labels, focus rings, keyboard tab order and
dark mode. The page adds:

* Field help as Filament helper text, under 20 words, and an error message per
  field that names the fix.
* Every destructive action uses a Filament confirmation modal with a danger
  button that repeats the action name.
* Loading: Filament button spinners on every action. The table polls every
  5 seconds only while a task is queued or running.
* Download: PHP waits at most 30 seconds for the engine to start the stream and
  60 seconds between chunks, then stops the engine and ends the transfer short.
* Download is an ordinary link that the browser saves in the same tab. Each
  download address is a Filament SPA exception, so Livewire navigation does
  not intercept the attachment. Other admin links keep SPA navigation.
* Action confirmations render once, before the tabs, from the extension's
  `backups::page` view. Filament renders the action modal container only once
  per page. Without this view the history table renders it inside the Backups
  tab, and a confirmation opened from Settings stays hidden.
* Empty history: "No backups yet" with the Create backup action.
* Engine unavailable: a danger callout that explains the web process cannot
  reach private storage and gives the terminal command to diagnose it.
