# Paymenter Backups

Paymenter Backups adds scheduled and manual backups to Paymenter. Save compressed
application and database backups in private local storage, upload encrypted copies
to an S3-compatible destination, and verify or download them from **System >
Backups** or the command line.

Full backups briefly put Paymenter into native maintenance mode while capturing
the application and database. Compression and upload happen after service
reopens. Online database backups avoid maintenance and have a narrower recovery
scope. Read [recovery](docs/RECOVERY.md) before relying on either kind.

## Requirements

- Linux, Python 3.10+, and the application's PHP CLI 8.3+.
- Paymenter 1.5.9, the version used by the native integration tests.
- GNU tar, GNU gzip, and a supported MariaDB/MySQL dump client.
- Only non-UTC schedules need time zone data (`tzdata`). Minimal images such as
  Alpine may need the `tzdata` package installed.
- For encrypted S3 copies: GnuPG with `gpg`, `gpg-agent`, and `gpgconf`.
- An owner-private directory outside Paymenter and every public web directory.
- For full backups: cooperative scheduler/worker locks and the host checks
  described in [installation](docs/INSTALL.md).

The native ZIP bundles its own pinned S3 SDK. Paymenter application files,
database definitions, themes, and root Composer dependencies stay upstream.

## Install and configure

Build the native extension ZIP on a development machine:

```sh
python3 tools/build_release.py --output dist/Backups-0.4.3.zip
(cd dist && sha256sum -c Backups-0.4.3.zip.sha256)
```

Upload the ZIP through Paymenter's native extension interface, install and enable
Backups, then open **System > Backups > Settings**. Select private storage, list
all other public web directories, and save. Both schedules start off. The setup
checklist names missing tools, confirmations, and host coordination requirements.
See [installation](docs/INSTALL.md) for program paths, cron, storage budgets,
credentials, and supported host configuration.

For S3, configure the destination and keys, save the recovery key somewhere
outside this server, confirm it, and run **Test storage**. Keep every recovery key
needed by older copies. Local backups contain private application data and
`APP_KEY`; protect the directory and every downloaded archive.

## Use

Use **Create backup** for a manual backup or enable separate database and full
schedules in Settings. Heavy actions are queued for the native every-minute tick.
Backup history shows progress and provides verification, downloads, S3 delivery,
and guarded deletion. See [the administrator interface](docs/ADMIN-UI.md).

The standalone CLI uses the same engine:

```sh
bin/paymenter-backups --app /srv/paymenter status
bin/paymenter-backups --app /srv/paymenter plan
bin/paymenter-backups --app /srv/paymenter run --mode database-online --execute
```

See [CLI workflows](docs/CLI.md) for configuration, schedules, credentials,
retention, and offline recovery. Restore to an isolated application and database
first, following [the recovery guide](docs/RECOVERY.md).

## Troubleshoot

- **A backup is blocked:** open the setup checklist for the named requirement.
  Full backups need additional host and billing-window checks.
- **Tasks stay queued:** check the cron check-in and Paymenter's scheduler.
- **S3 upload fails:** run Test storage and check the destination, credentials,
  GnuPG, and acknowledged recovery key. Failed delivery keeps its local source.
- **Private storage is unavailable:** open Backups settings for the path-specific
  fix. For an unrecognized folder, choose a new, dedicated, empty folder instead
  of changing a shared folder's owner or mode.
- **A run needs recovery:** preserve its journal and follow the CLI recovery
  procedure before attempting another capture.

Developer setup, tests, reproducible builds, and native restore validation are
in [maintenance](docs/MAINTENANCE.md). The [architecture](docs/ARCHITECTURE.md)
defines the consistency and recovery contracts.

## License and attribution

Copyright (c) 2026 Autonomic Engineering. Released under the [MIT license](LICENSE).
Paymenter is a separate project. [Third-party notices](NOTICES.md) describe the
bundled SDK and its original licenses.
