"""Private database context handoff used by the native Save path."""
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from paymenter_backups import cli
from paymenter_backups.common import BackupError
from paymenter_backups.config import validate_database_context


MARIADB_CONTEXT = {
    "schema_version": 1,
    "default": "mariadb",
    "connection": {
        "driver": "mariadb",
        "host": "127.0.0.1",
        "port": "3307",
        "unix_socket": "",
        "database": "paymenter_fixture",
        "username": "fixture",
        "password": "fixture-password",
        "prefix": "",
        "options": {},
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
        "strict": True,
        "engine": None,
        "prefix_indexes": True,
    },
}


class DatabaseContextTests(unittest.TestCase):
    def test_native_writer_receives_context_before_artisan_boot(self):
        args = SimpleNamespace(php="/usr/bin/php", database_connection=MARIADB_CONTEXT)
        state = SimpleNamespace(app=pathlib.Path("/fixture/app"), root=pathlib.Path("/fixture/private"))
        completed = SimpleNamespace(returncode=0, stdout=b'{"ok":true}', stderr=b"")
        with patch.object(cli, "run_native_writer", return_value=completed) as writer:
            cli.native_settings(args, state, {"dump": "/usr/bin/mariadb-dump"}, (11, 12))

        self.assertEqual(writer.call_args.kwargs["database_context"], MARIADB_CONTEXT)
        self.assertNotIn("fixture-password", " ".join(writer.call_args.args[0]))
        self.assertEqual(json.loads(writer.call_args.args[1])["_database_connection"], MARIADB_CONTEXT)

    def test_config_check_consumes_private_context_before_bridge_read(self):
        with tempfile.TemporaryDirectory(prefix="backups-db-context-") as temporary:
            root = pathlib.Path(temporary)
            app = root / "app"
            app.mkdir()
            private = root / "private"
            private.mkdir(mode=0o700)
            args = cli.parser().parse_args([
                "--app", str(app), "--private-root", str(private),
                "--database-connection-stdin", "config", "check",
            ])
            request = {"settings": {}, "_database_connection": MARIADB_CONTEXT}
            state = SimpleNamespace()
            result = {"valid": True}

            def checked(received_args, received_state, changes):
                self.assertIs(received_args, args)
                self.assertIs(received_state, state)
                self.assertEqual(received_args.database_connection, MARIADB_CONTEXT)
                self.assertEqual(changes, {"settings": {}})
                return result

            with patch.object(cli, "clean_environment"), \
                    patch.object(cli, "stdin_json", return_value=request) as read_input, \
                    patch.object(cli, "State", return_value=state), \
                    patch.object(cli, "check_config", side_effect=checked):
                self.assertIs(cli.dispatch(args), result)

            read_input.assert_called_once_with()

    def test_schedule_set_consumes_context_wrapped_stdin_once(self):
        with tempfile.TemporaryDirectory(prefix="backups-db-context-schedule-") as temporary:
            root = pathlib.Path(temporary)
            app = root / "app"
            app.mkdir()
            private = root / "private"
            private.mkdir(mode=0o700)
            args = cli.parser().parse_args([
                "--app", str(app), "--private-root", str(private), "--database-connection-stdin",
                "schedule", "set", "--kind", "full",
            ])
            request = {
                "_database_connection": MARIADB_CONTEXT,
                "frequency": "daily", "time": "02:00", "weekdays": [0], "timezone": "UTC",
            }
            settings = dict(cli.DEFAULTS, settings_version=2)
            envelope = {"settings": settings}
            state = SimpleNamespace()
            result = {"updated": True}
            with patch.object(cli, "clean_environment"), \
                    patch.object(cli, "stdin_json", return_value=request) as read_input, \
                    patch.object(cli, "State", return_value=state), \
                    patch.object(cli, "bridge", return_value=envelope), \
                    patch.object(cli, "load", return_value=envelope), \
                    patch.object(cli, "set_config", return_value=result) as write_config:
                self.assertIs(cli.dispatch(args), result)

            read_input.assert_called_once_with()
            self.assertEqual(args.database_connection, MARIADB_CONTEXT)
            write_config.assert_called_once_with(args, state, {"settings": {
                "full_schedule": "daily", "full_time": "02:00", "full_weekdays": [0],
                "schedule_timezone": "UTC",
            }})

    def test_bridge_wraps_context_and_payload_on_private_stdin(self):
        from paymenter_backups import config as config_module
        from paymenter_backups.cli import parser

        args = parser().parse_args([
            "--app", "/fixture/app", "--private-root", "/fixture/private",
            "--bridge", "/fixture/bridge.php", "--php", "/usr/bin/php", "config", "check",
        ])
        args.database_connection = MARIADB_CONTEXT
        expected_payload = {"access_key": "fixture-key"}
        completed = SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")
        with patch("subprocess.run", return_value=completed) as run:
            self.assertEqual(config_module.bridge(args, "credentials-check", expected_payload), {})

        command = run.call_args.args[0]
        self.assertIn("--database-connection-stdin", command)
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(sent, {
            "database_connection": MARIADB_CONTEXT,
            "payload": expected_payload,
        })
        self.assertNotIn("fixture-password", json.dumps(command))

    def test_bridge_preserves_safe_php_failure_codes(self):
        from paymenter_backups import config as config_module
        from paymenter_backups.cli import parser

        args = parser().parse_args([
            "--app", "/fixture/app", "--private-root", "/fixture/private",
            "--bridge", "/fixture/bridge.php", "--php", "/usr/bin/php", "config", "check",
        ])
        args.database_connection = MARIADB_CONTEXT
        for code in ("bridge-unavailable", "database-pdo-driver-unavailable", "database-connection-failed"):
            completed = SimpleNamespace(returncode=1, stdout=b"", stderr=json.dumps({"error": code}).encode())
            with self.subTest(code=code), patch("subprocess.run", return_value=completed):
                with self.assertRaises(BackupError) as raised:
                    config_module.bridge(args, "read-settings")
                self.assertEqual(raised.exception.code, code)

        completed = SimpleNamespace(returncode=0, stdout=json.dumps({"error": "database-driver-unsupported"}).encode(), stderr=b"")
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(BackupError) as raised:
                config_module.bridge(args, "read-settings")
        self.assertEqual(raised.exception.code, "database-driver-unsupported")

        completed = SimpleNamespace(returncode=1, stdout=json.dumps({"error": "database-driver-unsupported"}).encode(), stderr=b"")
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(BackupError) as raised:
                config_module.bridge(args, "read-settings")
        self.assertEqual(raised.exception.code, "database-driver-unsupported")

        completed = SimpleNamespace(returncode=0, stdout=b"{}", stderr=json.dumps({"error": "database-driver-unsupported"}).encode())
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(BackupError) as raised:
                config_module.bridge(args, "read-settings")
        self.assertEqual(raised.exception.code, "database-driver-unsupported")

        completed = SimpleNamespace(returncode=0, stdout=b"{}", stderr=json.dumps({"error": "credential-value-hidden"}).encode())
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(BackupError) as raised:
                config_module.bridge(args, "read-settings")
        self.assertEqual(raised.exception.code, "bridge-failed")

    def test_database_context_rejects_extra_fields_and_oversized_secrets(self):
        invalid = json.loads(json.dumps(MARIADB_CONTEXT))
        invalid["connection"]["unexpected"] = "value"
        with self.assertRaisesRegex(BackupError, "database-connection-invalid"):
            validate_database_context(invalid)
        invalid = json.loads(json.dumps(MARIADB_CONTEXT))
        invalid["connection"]["password"] = "x" * 8193
        with self.assertRaisesRegex(BackupError, "database-connection-invalid"):
            validate_database_context(invalid)

    def test_database_context_rejects_unsupported_driver_before_bridge_use(self):
        invalid = json.loads(json.dumps(MARIADB_CONTEXT))
        invalid["connection"]["driver"] = "sqlite"
        with self.assertRaisesRegex(BackupError, "database-driver-unsupported"):
            validate_database_context(invalid)


if __name__ == "__main__":
    unittest.main()
