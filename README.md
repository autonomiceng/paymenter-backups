# Paymenter Backups

An MIT Paymenter extension and standalone CLI for compressed application/database
checkpoints in private local storage or an S3-compatible destination. The default
full-quiesced mode briefly uses native maintenance; compression and delivery run
after service restoration. Database-online has narrower recovery scope.

Supports Linux, Python 3.10+ and PHP 8.3+; native integration tests use the pinned
official Paymenter v1.5.9 archive. Application source, authentication, native table
definitions, themes and root Composer dependencies remain upstream. Administrator
heavy actions write durable requests; the native every-minute tick launches work.
Terminal workflows use the same engine and independent offline recovery.

The **System > Backups** admin page covers setup, settings, backup history,
verified downloads, per-backup deletion with a last-good-backup guard, and
queued task status. It uses Paymenter's own admin components and theme. See
[the administrator interface](docs/ADMIN-UI.md).

Start with [installation](docs/INSTALL.md), [CLI workflows](docs/CLI.md),
[recovery](docs/RECOVERY.md), [maintenance](docs/MAINTENANCE.md) and the
[testable architecture](docs/ARCHITECTURE.md). [Third-party notices](NOTICES.md)
explain the isolated hash-pinned SDK and shipped licenses. Release ZIP has native
uploader layout `Backups/Backups.php`; only S3 service models are bundled to fit
the native10 MiB upload limit.

```sh
python3 -m pip install --require-hashes -r requirements-s3.lock
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 tools/build_release.py
python3 tools/native_integration.py --release dist/Backups-0.3.0.zip
```

The drill uses a fresh verified stock release and its own disposable MariaDB
socket/datadir, fake credentials and restored application/schema. It checks native
uploader/config/schedule/admin/CLI discovery, credential non-disclosure, admin
settings validation, download, verification and deletion, native source
preservation, APP_KEY decryption, Passport keys, migrations and login.
Host configuration and provider behavior require separate validation.

Copyright © 2026 Autonomic Engineering. Licensed under [MIT](LICENSE).
