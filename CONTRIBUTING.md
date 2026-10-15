# Contributing

Read [the architecture](docs/ARCHITECTURE.md) before changing execution,
configuration, maintenance, scheduling, storage, or recovery. Keep Paymenter
application files, native schema, themes, and root Composer dependencies upstream.
Use disposable fixtures with fake credentials. Keep private data and operational
logs outside the repository.

## Set up and test

Use Linux, Python 3.10+, and PHP 8.3+. Install the pinned SDK in a virtual
environment so the S3 protocol tests run without skips:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-s3.lock
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tools
```

Lint PHP files in `extension/` and `tests/integration/` on PHP 8.3 and 8.4.
Add meaningful regression coverage for consistency, recovery, or secret-handling
changes and update the corresponding authoritative documentation.

## Build and validate

```sh
python tools/build_release.py --output dist/first.zip
python tools/build_release.py --output dist/second.zip
cmp dist/first.zip dist/second.zip
python tools/native_integration.py --release dist/first.zip --php /path/to/php --composer /path/to/composer
```

Run the native integration command with PHP 8.3 and 8.4. It creates a disposable
Paymenter application and MariaDB instance, installs the exact ZIP, and performs
an encrypted full backup and isolated restore. It needs MariaDB server/client,
Composer, ACL tools, and the PHP extensions listed in CI. See
[maintenance](docs/MAINTENANCE.md) for prerequisites, browser checks, and cleanup.
`--skip-restore` is for development and does not complete release validation.

Before release, compare reproducible ZIPs,
and finish the exact-ZIP native restore drill. Record the artifact hash and
actual check results. Test real providers and the target host separately;
isolated fixtures establish their own narrower checks.

## Submit changes

Use Conventional Commits: `<type>(scope): <description>`. Preserve your Git
identity, signing settings, and contributor attribution. Describe the behavior
changed, its validation, and remaining limits in the pull request. Contributions
are licensed under the project's [MIT license](LICENSE).
