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
