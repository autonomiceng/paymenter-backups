"""A first config Save accepts only safe Backups runtime files."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups import cli
from paymenter_backups.common import BackupError, State, atomic_json


class PrivateStoreInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="backups-first-save-")
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.app = self.base / "app"
        (self.app / "public").mkdir(parents=True)
        self.root = self.base / "private"
        self.root.mkdir(mode=0o700)

    def save(self):
        args = cli.parser().parse_args([
            "--app", str(self.app), "--private-root", str(self.root), "config", "set",
        ])
        with patch.object(cli, "clean_environment"), \
                patch.object(cli, "bridge", return_value={"settings": {"extra_docroots": []}}), \
                patch.object(cli, "stdin_json", return_value={"settings": {}}), \
                patch.object(cli, "set_config", return_value={"updated": True}) as write:
            result = cli.dispatch(args)
        return result, write

    def own_file(self, name, content=b""):
        descriptor = os.open(self.root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)

    def test_first_save_accepts_identity_lock_and_billing_guard(self):
        self.own_file("identity.lock")
        atomic_json(self.root / "billing-guard.json", {
            "schema_version": 1, "generated_at": 1, "fingerprint": "fixture",
            "expression": "* * * * *", "timezone": "UTC", "instants": [2],
        })

        result, write = self.save()

        self.assertEqual(result, {"updated": True})
        write.assert_called_once()
        self.assertTrue((self.root / "installation.json").is_file())
        self.assertEqual(json.loads((self.root / "billing-guard.json").read_text())["instants"], [2])

    def test_first_save_refuses_a_billing_guard_with_public_permissions(self):
        self.own_file("identity.lock")
        guard = self.root / "billing-guard.json"
        atomic_json(guard, {"schema_version": 1, "instants": [2]})
        guard.chmod(0o644)

        with self.assertRaisesRegex(BackupError, "private-state-permissions"):
            self.save()

        self.assertFalse((self.root / "installation.json").exists())

    def test_first_save_refuses_a_linked_billing_guard(self):
        self.own_file("identity.lock")
        target = self.base / "external-guard.json"
        atomic_json(target, {"schema_version": 1, "instants": [2]})
        (self.root / "billing-guard.json").symlink_to(target)

        with self.assertRaisesRegex(BackupError, "private-state-permissions"):
            self.save()

        self.assertFalse((self.root / "installation.json").exists())

    def test_private_root_must_have_exact_owner_private_mode(self):
        self.root.chmod(0o750)

        with self.assertRaisesRegex(BackupError, "owner-mismatch"):
            State(self.app, self.root, initialize=False)

    def test_first_save_still_refuses_foreign_files_and_old_keys(self):
        for name, content in (("foreign.txt", b"unrelated"), ("credentials.json", b"old-key-file")):
            with self.subTest(name=name):
                child = self.base / name.replace(".", "-")
                child.mkdir(mode=0o700)
                self.root = child
                self.own_file("identity.lock")
                self.own_file(name, content)
                args = cli.parser().parse_args([
                    "--app", str(self.app), "--private-root", str(self.root), "config", "set",
                ])
                with patch.object(cli, "clean_environment"), \
                        patch.object(cli, "bridge", return_value={"settings": {"extra_docroots": []}}), \
                        patch.object(cli, "stdin_json", return_value={"settings": {}}), \
                        patch.object(cli, "set_config", return_value={"updated": True}) as write:
                    with self.assertRaisesRegex(BackupError, "installation-state-orphaned"):
                        cli.dispatch(args)
                write.assert_not_called()
