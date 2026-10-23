"""PHP private-data path and administrator diagnostics."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get("PAYMENTER_BACKUPS_TEST_PHP") or shutil.which("php")


@unittest.skipUnless(PHP, "PHP required")
class PrivateStoreUiTests(unittest.TestCase):
    def php(self, program, *arguments):
        result = subprocess.run(
            [PHP, "-r", program, *map(str, arguments)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_paymenter_at_filesystem_root_gets_a_single_slash_default(self):
        source = ROOT / "extension/Backups/PrivateStore.php"
        path = self.php(
            'require $argv[1]; echo Paymenter\\Extensions\\Others\\Backups\\PrivateStore::root("/app");',
            source,
        )
        self.assertEqual(path, "/paymenter-backups-private")

    def test_unwritable_parent_returns_an_actionable_data_directory_line(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            parent = base / "read-only"
            parent.mkdir(mode=0o700)
            parent.chmod(0o500)
            path = parent / "paymenter-backups-private"
            source = ROOT / "extension/Backups/PrivateStore.php"
            try:
                result = json.loads(self.php(
                    'require $argv[1]; echo json_encode('
                    'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::problem($argv[2], $argv[3]), '
                    'JSON_THROW_ON_ERROR);',
                    source, app, path,
                ))
            finally:
                parent.chmod(0o700)
            self.assertEqual(result["code"], "private-root-unavailable")
            self.assertEqual(result["message"],
                f"Data directory {path} can't be created: the web user can't write to {parent}. "
                "Create it for the web user with mode 0700, or choose another folder outside Paymenter.")

    def test_existing_private_directory_wrong_mode_has_a_specific_fix(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-mode-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            private = base / "private"
            private.mkdir(mode=0o700)
            private.chmod(0o750)
            source = ROOT / "extension/Backups/PrivateStore.php"
            result = json.loads(self.php(
                'require $argv[1]; echo json_encode('
                'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::problem($argv[2], $argv[3]), '
                'JSON_THROW_ON_ERROR);',
                source, app, private,
            ))
            self.assertEqual(result["code"], "private-root-invalid")
            self.assertEqual(result["message"],
                f"Data directory {private} must have mode 0700. Set its mode to 0700, or choose another folder outside Paymenter.")
            code = self.php(
                'require $argv[1]; try { Paymenter\\Extensions\\Others\\Backups\\PrivateStore::root($argv[2], $argv[3]); } '
                'catch (RuntimeException $error) { echo $error->getMessage(); }',
                source, app, private,
            )
            self.assertEqual(code, "private-root-invalid")


if __name__ == "__main__":
    unittest.main()
