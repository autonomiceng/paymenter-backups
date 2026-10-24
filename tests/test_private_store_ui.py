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

    def private_root_problem(self, app, private, setup=""):
        source = ROOT / "extension/Backups/PrivateStore.php"
        messages = ROOT / "extension/Backups/Messages.php"
        result = self.php(
            setup
            + 'require $argv[1]; require $argv[4]; $problem = '
            + 'Paymenter\\Extensions\\Others\\Backups\\PrivateStore::problem($argv[2], $argv[3]); '
            + 'echo json_encode(["problem" => $problem, "message" => '
            + 'Paymenter\\Extensions\\Others\\Backups\\Messages::privateRootProblem($problem)], JSON_THROW_ON_ERROR);',
            source, app, private, messages,
        )
        return json.loads(result)

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
            '["code" => "private-root-invalid", "params" => ["path" => "/private/" . str_repeat("x", 500) . "\\n", "reason" => "owner", "identified" => true]]);',
            source,
        )
        self.assertIn("isn't owned by the web user", message)
        self.assertNotIn("\n", message)
        self.assertLess(len(message), 700)

    def test_generic_private_store_errors_do_not_recommend_changing_shared_folders(self):
        source = ROOT / "extension/Backups/Messages.php"
        output = self.php(
            'require $argv[1]; $codes = ["private-root-invalid", "private-root-unavailable", "owner-mismatch", '
            '"private-state-permissions", "private-file-invalid"]; $messages = []; '
            'foreach ($codes as $code) { $messages[$code] = '
            'Paymenter\\Extensions\\Others\\Backups\\Messages::error($code); } '
            'echo json_encode($messages, JSON_THROW_ON_ERROR);',
            source,
        )
        messages = json.loads(output)
        for message in messages.values():
            self.assertNotIn("chmod", message.lower())
            self.assertNotIn("change its owner", message.lower())
            self.assertNotIn("set its mode", message.lower())
            self.assertNotIn("permissions to 700", message.lower())
        for code in ("private-root-invalid", "private-root-unavailable", "owner-mismatch"):
            self.assertIn("new, dedicated, empty folder", messages[code])

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
                self.assertEqual(result["problem"]["params"], {
                    "path": str(path), "parent": str(parent), "parent_is_folder": True,
                    "path_exists": False, "identified": False,
                })
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
            (private / "installation.json").write_text('{"schema_version":1}')
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
            self.assertEqual(result["problem"]["params"], {"path": str(private), "reason": "mode", "identified": True})
            self.assertNotIn("message", result["problem"])
            self.assertEqual(result["message"],
              f"Backups data directory {private} must have mode 0700. Set its mode to 0700.")
            self.assertNotIn("choose another folder", result["message"])
            code = self.php(
                'require $argv[1]; try { Paymenter\\Extensions\\Others\\Backups\\PrivateStore::root($argv[2], $argv[3]); } '
                'catch (RuntimeException $error) { echo $error->getMessage(); }',
                source, app, private,
            )
            self.assertEqual(code, "private-root-invalid")

    def test_unidentified_foreign_directory_prompts_for_a_new_dedicated_folder(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-foreign-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            private = base / "shared"
            private.mkdir(mode=0o700)
            setup = (
                'eval("namespace Paymenter\\\\Extensions\\\\Others\\\\Backups; '
                'function posix_geteuid() { return 999999; }"); '
            )
            result = self.private_root_problem(app, private, setup)
            self.assertEqual(result["problem"]["code"], "private-root-invalid")
            self.assertEqual(result["problem"]["params"]["reason"], "owner")
            self.assertIn("Choose a new, dedicated, empty folder", result["message"])
            self.assertNotIn("Change its owner", result["message"])
            self.assertNotIn("Set its mode", result["message"])

    def test_unidentified_0755_directory_prompts_for_a_new_dedicated_folder(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-shared-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            private = base / "shared"
            private.mkdir(mode=0o755)
            private.chmod(0o755)
            result = self.private_root_problem(app, private)
            self.assertEqual(result["problem"]["code"], "private-root-invalid")
            self.assertEqual(result["problem"]["params"]["reason"], "mode")
            self.assertIn("Choose a new, dedicated, empty folder", result["message"])
            self.assertNotIn("Set its mode", result["message"])

    def test_identified_directory_with_wrong_mode_keeps_repair_instructions(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-identified-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            private = base / "private"
            private.mkdir(mode=0o750)
            (private / "installation.json").write_text('{"schema_version":1}')
            private.chmod(0o750)
            result = self.private_root_problem(app, private)
            self.assertEqual(result["problem"]["params"]["reason"], "mode")
            self.assertIn("Set its mode to 0700", result["message"])
            self.assertNotIn("Choose a new, dedicated, empty folder", result["message"])

    def test_unreadable_installation_marker_does_not_identify_directory_for_repair(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-marker-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            private = base / "private"
            private.mkdir(mode=0o750)
            marker = private / "installation.json"
            marker.write_text('{"schema_version":1}')
            setup = (
                'eval("namespace Paymenter\\\\Extensions\\\\Others\\\\Backups; '
                'function is_readable(\\$path) { return false; }"); '
            )
            result = self.private_root_problem(app, private, setup)
            self.assertEqual(result["problem"]["params"]["reason"], "mode")
            self.assertIn("Choose a new, dedicated, empty folder", result["message"])
            self.assertNotIn("Set its mode", result["message"])

    def test_unreadable_unidentified_directory_prompts_for_a_new_folder(self):
        with tempfile.TemporaryDirectory(prefix="backups-private-store-unreadable-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            private = base / "shared"
            private.mkdir(mode=0o700)
            private.chmod(0o000)
            try:
                result = self.private_root_problem(app, private)
            finally:
                private.chmod(0o700)
            self.assertIn("Choose a new, dedicated, empty folder", result["message"])
            self.assertNotIn("Create it for the web user", result["message"])

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
