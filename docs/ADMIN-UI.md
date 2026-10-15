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
   • Paymenter's scheduler has not run yet. Check the existing Paymenter server scheduler.
   • Full backups need 3 items under Settings, Host checks.
                                         [Open settings] [Paymenter scheduler setup]

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
checks, Retention and space, Advanced (collapsed: time limits, directories and
programs), then the "Save settings" button. Host checks shows only what the
chosen backup type needs: the schema-change confirmation for both types, plus
job lock files and two more confirmations for full backups. The Backup directory
default appears as selectable code below a blank input; the Overview only says "This
server". Below the form, S3 access keys appear when S3 is chosen and HTTP Basic
authentication appears as a collapsed advanced section for full backups. Saved credentials keep their section available even after the mode or destination changes. Each credential section saves separately
from settings. HTTP Basic authentication always explains the browser password
prompt and its maintenance check purpose, including before setup, after saving,
and when HTTPS is unavailable. It distinguishes these credentials from the
Paymenter account login. The saved state names the replacement action and says
that the saved values are hidden. The server supplies the maintenance check base from App URL in Paymenter settings. Remove stays available when saved credentials no longer match that address, including after a change to HTTP.
Removal deletes the extension's saved credentials without changing server or
account passwords. Before setup, the page asks the administrator to save settings.

Below 640 px the history table uses Filament's stacked-on-mobile layout: each
row lists Created and Status with its full detail, then Download and More
actions together on a separate line. Type, Size and Stored on stay in Details below
768 px (Stored on below 1024 px). Long commands on the Help tab wrap inside
the page.

Help explains what a backup contains and why the existing Paymenter server
launcher must invoke its scheduler. It links the official installation guide.
A collapsed job coordination section shows a `flock` wrapper for the existing
scheduler command. Paymenter server setup owns systemd timers and cron jobs. Backups does not
install or change them; it registers tasks with Paymenter and reports check-ins.
Help states that enabling or changing a schedule starts at its next future time.
After an outage, missed times in the last eight days produce one fresh backup,
possibly outside the planned time. Full backups show the maintenance page. A
newer scheduled time replaces a waiting catch-up; maintenance and admission
checks still apply, with no backlog. Terminal help defines one shell function, then gives short commands for offline
status, recovery, explicit recovery acknowledgement and export. Detailed host
setup belongs to Paymenter's installation and the server operator.

Apply retention uses the engine's local and S3 candidate counts. Local retention
keeps at least one verified local backup. S3 retention selects a local-count,
independent count, age or keep-all policy and always keeps its newest S3
copy. Older backups can therefore exist only in S3. Their contextual action is
Retrieve from S3, which queues retrieval and verification; Download appears when
the local archive is ready. Details show a retrieved copy's retention protection.
Delete uses one dialog with a storage-location choice and a live read-only
preview. The deleted filter appears only when a deleted backup exists.

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
and what to do next. They never apologise. Known errors use plain explanations. Details may show a
safe support error code; private paths, stderr and JSON never appear. Engine codes, statuses and task names come from
`extension/Backups/Messages.php`. Forms and actions use the same terms.

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
   The first save initializes private storage even with no field changed. Malformed
   non-location rows show trusted defaults and are corrected by the next save.
   Invalid schedule and job-lock JSON remains raw on the cold read until the
   configuration check merges that validated repair. Ordinary engine reads
   refuse it. A successful repair names the corrected fields in a warning. Malformed public-directory guard JSON is never defaulted: the page
   refuses and directs the administrator to the native Extensions editor. A
   change to an initialized extension data directory is refused. Field errors open a
   collapsed section and scroll to the field, and a notification says to check
   the highlighted fields. Saving settings never opens or rewrites the
   encrypted credential file.
4. **Secrets are write-only.** S3 keys and the HTTP Basic credentials live in public
   Livewire arrays that `dehydrate()` resets on every response, including
   validation failures and unrelated actions. The page shows only "Saved" or
   "Not saved". The HTTP Basic authentication address is Paymenter's own https origin,
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
   arrives complete. A missing or stale record requires verification. Remote-only archives use a
   queued retrieval before Download. The recorded download readiness enables
   Download independently of an interrupted deletion at the other location.
7. **Per-copy delete is a request.** The preview uses the selected local, S3 or
   all-copy location. The engine requires a verified survivor at each selected
   location and refuses the newest S3 copy, conflicting tasks and out-of-scope
   remote copies. Execution records per-location intent before removal and
   resumes that same location on retry. Local unlinks use the verified directory
   descriptor and the complete file identity. A failed S3 deletion leaves a
   verified local download available. Deleting a copy never changes retention.

## Accessibility and states

Native Filament controls provide labels, focus rings, keyboard tab order and
dark mode. The page adds:

* Essential field help stays visible and concise. Selected unfamiliar settings
  also have a named native help button. It opens a short dialog by click, tap or
  keyboard, with Close and no submit action. Tooltips supplement those buttons.
* Filesystem labels use Directory. The S3 Object prefix explains that its value
  and this installation's ID precede object names.
* Help commands are escaped native Filament Text components. They wrap without
  changing copied text. HTTPS supports click, Enter and Space. HTTP offers
  selectable text and explains that automatic copying needs HTTPS. A single instruction
  explains copying; the visible command text is its accessible name, with a supplemental tooltip.
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
