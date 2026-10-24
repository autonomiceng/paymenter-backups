"""Private temporary roots used by disposable native fixtures."""
import os
import pathlib
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.fixture_environment import private_work_directory, require_private_workroot


class PrivateWorkDirectoryTests(unittest.TestCase):
    def parent_stat_with_owner(self, parent, owner):
        original_stat = pathlib.Path.stat

        def controlled_stat(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == parent:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=owner)
            return info

        return patch.object(pathlib.Path, "stat", controlled_stat)

    def test_rejects_world_writable_parent_without_sticky_bit(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary) / "unsafe"
            parent.mkdir(mode=0o700)
            parent.chmod(0o777)
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                with self.assertRaisesRegex(RuntimeError, "world-writable without sticky bit"):
                    private_work_directory("fixture-")

    def test_rejects_group_writable_parent_without_sticky_bit(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary) / "unsafe"
            parent.mkdir(mode=0o700)
            parent.chmod(0o770)
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                with self.assertRaisesRegex(RuntimeError, "writable without sticky bit"):
                    private_work_directory("fixture-")

    def test_rejects_foreign_owned_temporary_parent(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary).resolve()
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                with self.parent_stat_with_owner(parent, os.geteuid() + 10000):
                    with self.assertRaisesRegex(RuntimeError, "temporary parent owner"):
                        private_work_directory("fixture-")

    def test_creates_random_private_root_under_sticky_parent(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary) / "sticky"
            parent.mkdir(mode=0o700)
            parent.chmod(0o1777)
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                first = private_work_directory("fixture-")
                second = private_work_directory("fixture-")
            self.assertNotEqual(first, second)
            for work in (first, second):
                self.assertEqual(work.parent, parent)
                self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)

    def test_fixture_workroot_requires_random_owner_private_parent(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            with patch.dict(os.environ, {"TMPDIR": temporary}):
                base = private_work_directory("paymenter-backups-ui-")
                work = private_work_directory("ui-", base)
                self.assertEqual(require_private_workroot(work, "ui-", "paymenter-backups-ui-"), work)
                with self.assertRaisesRegex(RuntimeError, "outside its private temporary base"):
                    require_private_workroot(pathlib.Path(temporary) / "ui-fixed", "ui-", "paymenter-backups-ui-")

    def test_fixture_workroot_rejects_parent_that_became_world_writable(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary)
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                base = private_work_directory("paymenter-backups-ui-")
                work = private_work_directory("ui-", base)
                parent.chmod(0o777)
                with self.assertRaisesRegex(RuntimeError, "world-writable without sticky bit"):
                    require_private_workroot(work, "ui-", "paymenter-backups-ui-")

    def test_fixture_workroot_rejects_parent_that_became_group_writable(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary)
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                base = private_work_directory("paymenter-backups-ui-")
                work = private_work_directory("ui-", base)
                parent.chmod(0o770)
                with self.assertRaisesRegex(RuntimeError, "group-writable without sticky bit"):
                    require_private_workroot(work, "ui-", "paymenter-backups-ui-")

    def test_fixture_workroot_rejects_foreign_owned_temporary_parent(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary).resolve()
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                base = private_work_directory("paymenter-backups-ui-")
                work = private_work_directory("ui-", base)
                with self.parent_stat_with_owner(parent, os.geteuid() + 10000):
                    with self.assertRaisesRegex(RuntimeError, "temporary parent owner"):
                        require_private_workroot(work, "ui-", "paymenter-backups-ui-")

    def test_fixture_workroot_accepts_root_owned_sticky_temporary_parent(self):
        with tempfile.TemporaryDirectory(prefix="fixture-temp-parent-") as temporary:
            parent = pathlib.Path(temporary).resolve()
            parent.chmod(0o1777)
            with patch.dict(os.environ, {"TMPDIR": str(parent)}):
                base = private_work_directory("paymenter-backups-ui-")
                work = private_work_directory("ui-", base)
                with self.parent_stat_with_owner(parent, 0):
                    self.assertEqual(require_private_workroot(work, "ui-", "paymenter-backups-ui-"), work)


if __name__ == "__main__":
    unittest.main()
