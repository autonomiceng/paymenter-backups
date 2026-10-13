# Paymenter Backups

An MIT-licensed Paymenter extension and CLI for scheduled, compressed backups to
private local storage or an S3-compatible destination.

Development targets the official Paymenter v1.5.9 release, PHP 8.3+, Python 3.10+
and Linux. The default mode captures the database and application during a brief
native maintenance window. Online database snapshots are a separate mode with a
narrower recovery scope. Compression and remote delivery happen after service
restoration.

The extension preserves Paymenter core files, native table definitions and root
Composer dependencies. Both the administrator interface and terminal workflows
use the same backup engine. See [the architecture](docs/ARCHITECTURE.md) for
consistency, credential, scheduling and recovery contracts.

Copyright © 2026 Autonomic Engineering. Licensed under [MIT](LICENSE).
