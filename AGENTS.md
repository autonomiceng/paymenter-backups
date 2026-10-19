# Development guide

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing execution,
maintenance, storage, configuration, scheduling, or recovery. It defines module
contracts and consistency guarantees. Read [docs/MAINTENANCE.md](docs/MAINTENANCE.md)
for release validation and [CONTRIBUTING.md](CONTRIBUTING.md) for contribution setup.

Keep Paymenter application files, native schema, themes, and root Composer
dependencies upstream. Native settings contain non-secret configuration; durable
state and encrypted credentials belong in an owner-private directory outside the
application and all web roots. Use disposable fixtures and fake credentials.
Keep deployment credentials and private operational logs outside the repository.

Paymenter must stay upgradable without extension releases. Follow [Keeping Paymenter upgradable](docs/ARCHITECTURE.md#keeping-paymenter-upgradable): no container rebinding, native route remaps, global hooks or version gates.

Set up a Python virtual environment and install `requirements-s3.lock` with
`pip install --require-hashes`. Run
`PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests -v`,
then `python -m compileall -q src tools`. Lint PHP on 8.3 and 8.4. Build the native
ZIP twice with `python tools/build_release.py --output PATH` and compare the bytes.

Recovery or consistency changes require updated authoritative documentation and
meaningful isolated tests. Release validation requires passing checks,
reproducible native ZIPs and exact-ZIP
isolated restore drills on PHP 8.3 and 8.4. Host and provider validation are
separate operational checks.

Use Conventional Commits. Before committing, verify the effective Git author,
committer, and signing configuration; preserve contributor attribution.
