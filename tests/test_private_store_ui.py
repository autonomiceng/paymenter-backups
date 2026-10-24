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
        program = ('if (!function_exists("posix_geteuid")) { function posix_geteuid() { return getmyuid(); } } '
                   + program)
        result = subprocess.run(
            [PHP, "-r", program, *map(str, arguments)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_paymenter_at_filesystem_root_gets_a_single_slash_default(self):
        source = ROOT / "extension/Backups/PrivateStore.php"
        path = self.php(
            'require $argv[1]; echo Paymenter\\Extensions\\Others\\Backups\\PrivateStore::root("/app");',
            source,
        )
        self.assertEqual(path, "/paymenter-backups-private")

    def test_owner_problem_copy_is_formatted_with_a_bounded_sanitized_path(self):
        source = ROOT / "extension/Backups/Messages.php"
        message = self.php(
            'require $argv[1]; echo '
            'Paymenter\\Extensions\\Others\\Backups\\Messages::privateRootProblem('
            '["code" => "private-root-invalid", "params" => ["path" => "/private/" . str_repeat("x", 500) . "\\n", "reason" => "owner"]]);',
            source,
        )
        self.assertIn("isn't owned by the web user", message)
        self.assertNotIn("\n", message)
        self.assertLess(len(message), 700)

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
                    'require $argv[1]; require $argv[4]; $problem = '
                    'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::problem($argv[2], $argv[3]); '
                    '$formatted = Paymenter\\Extensions\\Others\\Backups\\Messages::privateRootProblem('
                    '["code" => "private-root-unavailable", "params" => ["path" => $argv[3], '
                    '"parent" => dirname($argv[3]), "parent_is_folder" => true]]); '
                    'echo json_encode(["problem" => $problem, "parent_writable" => is_writable(dirname($argv[3])), '
                    '"formatted" => $formatted], '
                    'JSON_THROW_ON_ERROR);',
                    source, app, path, ROOT / "extension/Backups/Messages.php",
                ))
            finally:
                parent.chmod(0o700)
            expected = f"Data directory {path} can't be created: the web user can't write to {parent}. " \
                "Create it for the web user with mode 0700, or choose another folder outside Paymenter."
            self.assertEqual(result["formatted"], expected)
            if result["parent_writable"]:
                self.assertIsNone(result["problem"], "PHP can write to the parent, so the path is not unavailable")
            else:
                self.assertEqual(result["problem"]["code"], "private-root-unavailable")
                self.assertEqual(result["problem"]["params"], {"path": str(path), "parent": str(parent), "parent_is_folder": True})
                self.assertNotIn("message", result["problem"])
                self.assertEqual(result["formatted"],
                f"Data directory {path} can't be created: the web user can't write to {parent}. "
                "Create it for the web user with mode 0700, or choose another folder outside Paymenter.")

    def test_read_only_parent_check_handles_root_write_semantics(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-root-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            parent = base / "read-only"
            parent.mkdir(mode=0o700)
            parent.chmod(0o500)
            path = parent / "paymenter-backups-private"
            source = ROOT / "extension/Backups/PrivateStore.php"
            result = json.loads(self.php(
                'eval("namespace Paymenter\\\\Extensions\\\\Others\\\\Backups; '
                'function is_writable(\\$path) { return true; }"); '
                'require $argv[1]; $problem = '
                'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::problem($argv[2], $argv[3]); '
                'echo json_encode(["problem" => $problem], JSON_THROW_ON_ERROR);',
                source, app, path,
            ))
            parent.chmod(0o700)
            self.assertIsNone(result["problem"])

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
                'require $argv[1]; require $argv[4]; $problem = '
                'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::problem($argv[2], $argv[3]); '
                'echo json_encode(["problem" => $problem, "message" => '
                'Paymenter\\Extensions\\Others\\Backups\\Messages::privateRootProblem($problem)], '
                'JSON_THROW_ON_ERROR);',
                source, app, private, ROOT / "extension/Backups/Messages.php",
            ))
            self.assertEqual(result["problem"]["code"], "private-root-invalid")
            self.assertEqual(result["problem"]["params"], {"path": str(private), "reason": "mode"})
            self.assertNotIn("message", result["problem"])
            self.assertEqual(result["message"],
              f"Data directory {private} must have mode 0700. Set its mode to 0700. "
              "Backups keeps this folder selected until it can check for an installation.")
            self.assertNotIn("choose another folder", result["message"])
            code = self.php(
                'require $argv[1]; try { Paymenter\\Extensions\\Others\\Backups\\PrivateStore::root($argv[2], $argv[3]); } '
                'catch (RuntimeException $error) { echo $error->getMessage(); }',
                source, app, private,
            )
            self.assertEqual(code, "private-root-invalid")

    def test_existing_unusable_data_directory_stays_in_repair_mode(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-repair-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            app.mkdir()
            private = base / "private"
            private.mkdir(mode=0o700)
            (private / "installation.json").write_text('{"installation_uuid":"fixture"}')
            private.chmod(0o000)
            source = ROOT / "extension/Backups/PrivateStore.php"
            try:
                result = json.loads(self.php(
                    'require $argv[1]; $protected = '
                    'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::installedOrRequiresRepair($argv[3]); '
                    '$rootCode = "none"; try { Paymenter\\Extensions\\Others\\Backups\\PrivateStore::root($argv[2], $argv[3]); } '
                    'catch (RuntimeException $error) { $rootCode = $error->getMessage(); } '
                    'echo json_encode(["protected" => $protected, "root_code" => $rootCode], JSON_THROW_ON_ERROR);',
                    source, app, private,
                ))
            finally:
                private.chmod(0o700)
            self.assertEqual(result, {"protected": True, "root_code": "private-root-invalid"})

    def test_existing_unreadable_root_stays_in_repair_state(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-repair-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            private = base / "private"
            private.mkdir(mode=0o700)
            (private / "installation.json").write_text('{"installation_uuid":"fixture"}')
            empty = base / "empty"
            empty.mkdir(mode=0o700)
            private.chmod(0o000)
            try:
                source = ROOT / "extension/Backups/PrivateStore.php"
                result = json.loads(self.php(
                    'require $argv[1]; echo json_encode(['
                    'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::installedOrRequiresRepair($argv[2]), '
                    'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::installedOrRequiresRepair($argv[3])'
                    '], JSON_THROW_ON_ERROR);',
                    source, private, empty,
                ))
            finally:
                private.chmod(0o700)
            self.assertEqual(result, [True, False])


if __name__ == "__main__":
    unittest.main()
