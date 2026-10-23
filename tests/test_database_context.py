"""Private database context handoff used by the native Save path."""
import json
import os
import pathlib
import sys
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

    def test_native_writer_missing_command_keeps_a_safe_cause(self):
        args = SimpleNamespace(php="/usr/bin/php", database_connection=MARIADB_CONTEXT)
        state = SimpleNamespace(app=pathlib.Path("/fixture/app"), root=pathlib.Path("/fixture/private"))
        completed = SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b'There are no commands defined in the "backups" namespace.\n',
        )
        with patch.object(cli, "run_native_writer", return_value=completed):
            with self.assertRaises(BackupError) as raised:
                cli.native_settings(args, state, {"dump": "/usr/bin/mariadb-dump"}, (11, 12))

        self.assertEqual(raised.exception.code, "command-not-registered")
        self.assertEqual(raised.exception.detail, {"writer_cause": "command-not-registered"})

    def test_native_writer_uses_structured_php_reason_and_field(self):
        args = SimpleNamespace(php="/usr/bin/php", database_connection=MARIADB_CONTEXT)
        state = SimpleNamespace(app=pathlib.Path("/fixture/app"), root=pathlib.Path("/fixture/private"))
        completed = SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=(b'{"error":"config-invalid","field":"retention_keep_full",'
                    b'"detail":{"writer_cause":"validation:retention_keep_full:integer"}}'),
        )
        with patch.object(cli, "run_native_writer", return_value=completed):
            with self.assertRaises(BackupError) as raised:
                cli.native_settings(args, state, {"retention_keep_full": "seven"}, (11, 12), actor=9)

        self.assertEqual(raised.exception.code, "config-write-outcome-unknown")
        self.assertEqual(raised.exception.field, "retention_keep_full")
        self.assertEqual(raised.exception.detail,
                         {"writer_cause": "validation:retention_keep_full:integer"})

    def test_native_writer_cause_redacts_connection_secrets(self):
        cause = cli.native_writer_cause(
            b"PDO error for fixture: password=fixture-password username=fixture-user",
            database_context=MARIADB_CONTEXT,
        )
        self.assertNotIn("fixture-password", cause)
        self.assertNotIn("fixture-user", cause)
        self.assertIn("[redacted]", cause)

    def test_config_save_dispatch_runs_writer_with_context_from_private_stdin(self):
        with tempfile.TemporaryDirectory(prefix="backups-save-writer-context-") as temporary:
            root = pathlib.Path(temporary)
            app = root / "app"
            app.mkdir()
            private = root / "private"
            private.mkdir(mode=0o700)
            artisan = app / "artisan"
            artisan.write_text(
                "import json, os, sys\n"
                "data = json.load(sys.stdin)\n"
                "expected = {'DB_CONNECTION': 'mariadb', 'DB_DATABASE': 'paymenter_fixture', 'DB_PASSWORD': 'fixture-password'}\n"
                "actual = {key: os.environ.get(key) for key in expected}\n"
                "extras = [key for key in os.environ if key.startswith('DB_') and key not in "
                "('DB_CONNECTION','DB_URL','DB_HOST','DB_PORT','DB_DATABASE','DB_USERNAME','DB_PASSWORD','DB_SOCKET',"
                "'DB_PREFIX','DB_CHARSET','DB_COLLATION','DB_STRICT','DB_ENGINE','DB_PREFIX_INDEXES')]\n"
                "if actual != expected or extras or os.environ.get('AWS_SECRET_ACCESS_KEY') or os.environ.get('MYSQL_PWD') "
                "or data['_database_connection']['connection']['driver'] != 'mariadb': sys.exit(31)\n"
                "print(json.dumps({'ok': True, 'receipt': {}, 'snapshot': {}}))\n"
            )
            args = cli.parser().parse_args([
                "--app", str(app), "--private-root", str(private), "--database-connection-stdin", "config", "set",
            ])
            args.php = sys.executable
            state = SimpleNamespace(app=app, root=private)
            request = {"settings": {}, "_database_connection": MARIADB_CONTEXT}
            envelope = {"settings": dict(cli.DEFAULTS)}
            config_fd = os.open(root / "config.lock", os.O_CREAT | os.O_RDWR, 0o600)
            tick_fd = os.open(root / "tick.lock", os.O_CREAT | os.O_RDWR, 0o600)

            def save(received_args, received_state, changes):
                self.assertIs(received_args, args)
                self.assertIs(received_state, state)
                self.assertEqual(changes, {"settings": {}})
                return cli.native_settings(received_args, received_state, {"dump": "/usr/bin/mariadb-dump"},
                                           (config_fd, tick_fd))

            parent_environment = {
                "DB_CONNECTION": "sqlite", "DB_DATABASE": "wrong-parent-database",
                "DB_PASSWORD": "parent-password", "AWS_SECRET_ACCESS_KEY": "parent-aws-secret",
            }
            try:
                with patch.dict(os.environ, parent_environment, clear=True), \
                        patch.object(cli, "clean_environment"), \
                        patch.object(cli, "stdin_json", return_value=request), \
                        patch.object(cli, "State", return_value=state), \
                        patch.object(cli, "bridge", return_value=envelope), \
                        patch.object(cli, "set_config", side_effect=save):
                    result = cli.dispatch(args)
            finally:
                os.close(config_fd)
                os.close(tick_fd)

        self.assertEqual(result["ok"], True)

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
