# Contributing

Keep this project portable and Paymenter upgradeable. Read
[the architecture contract](docs/ARCHITECTURE.md) before changing capture,
maintenance, scheduling, storage or recovery. Use disposable applications and
databases with fake credentials. Keep customer data, credentials and private
operational logs outside the repository.

Use Python 3.10+ on Linux and PHP 8.3+. Install development dependencies into an
isolated virtual environment with `pip install --require-hashes -r
requirements-s3.lock`; never change a Paymenter application's dependencies.
Run the unit suite with `PYTHONPATH=src python -m unittest discover -s tests -v`.
The S3 tests must run with the pinned SDK available, without skips.

Before submitting changes, lint PHP, build twice and compare the release ZIPs.
Run the disposable native integration and restore drill described in
[maintenance](docs/MAINTENANCE.md) for runtime changes. Add meaningful regression
coverage for consistency, recovery and secret-handling changes. Release checks must pass before publication.

Use Conventional Commits: `<type>(scope): <description>`. Preserve your normal
Git identity and signing configuration. Describe the behavior changed, its
validation and any remaining deployment limits in a pull request. Contributions
are licensed under the project's [MIT license](LICENSE).
