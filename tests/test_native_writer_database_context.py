import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups.engine import run_tool
from paymenter_backups.native_writer import run


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


class NativeWriterDatabaseContextTests(unittest.TestCase):
    def test_artisan_child_gets_only_validated_database_environment(self):
        # This child models Artisan's environment lookup before the command can
        # read its JSON stdin. Its parent has DB settings and unrelated secrets,
        # but native_writer must send only the resolved MariaDB allowlist.
        script = (
            "import json, os, sys; "
            "keys = ('DB_CONNECTION','DB_URL','DB_HOST','DB_PORT','DB_DATABASE',"
            "'DB_USERNAME','DB_PASSWORD','DB_SOCKET','DB_PREFIX','DB_CHARSET',"
            "'DB_COLLATION','DB_STRICT','DB_ENGINE','DB_PREFIX_INDEXES'); "
            "print(json.dumps({'environment': {key: os.environ.get(key) for key in keys}, "
            "'unexpected': [key for key in os.environ if key.startswith('DB_') and key not in keys] "
            "or [key for key in ('AWS_SECRET_ACCESS_KEY','MYSQL_PWD','PARENT_CANARY') if key in os.environ], "
            "'stdin': sys.stdin.buffer.read().decode()}))"
        )
        parent_environment = {
            "DB_CONNECTION": "sqlite",
            "DB_DATABASE": "must-not-leak",
            "DB_PASSWORD": "parent-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "MYSQL_PWD": "mysql-secret",
            "PARENT_CANARY": "parent-canary",
        }
        payload = b'{"settings":{"dump":"/usr/bin/mariadb-dump"}}'
        with tempfile.TemporaryDirectory(prefix="backups-writer-db-context-") as temporary:
            script_path = pathlib.Path(temporary) / "artisan-child.py"
            script_path.write_text(script)
            command = [sys.executable, str(script_path)]
            with patch.dict(os.environ, parent_environment, clear=True):
                result = run(command, payload, (), database_context=MARIADB_CONTEXT)

        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        observed = json.loads(result.stdout)
        self.assertEqual(observed["environment"], {
            "DB_CONNECTION": "mariadb",
            "DB_URL": "null",
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "3307",
            "DB_DATABASE": "paymenter_fixture",
            "DB_USERNAME": "fixture",
            "DB_PASSWORD": "fixture-password",
            "DB_SOCKET": "",
            "DB_PREFIX": "",
            "DB_CHARSET": "utf8mb4",
            "DB_COLLATION": "utf8mb4_unicode_ci",
            "DB_STRICT": "true",
            "DB_ENGINE": "null",
            "DB_PREFIX_INDEXES": "true",
        })
        self.assertEqual(observed["unexpected"], [])
        self.assertEqual(observed["stdin"], payload.decode())
        self.assertNotIn("fixture-password", " ".join(command))

    def test_maintenance_artisan_child_gets_same_database_allowlist(self):
        script = (
            "import json, os; print(json.dumps({'driver': os.environ.get('DB_CONNECTION'), "
            "'database': os.environ.get('DB_DATABASE'), 'password': os.environ.get('DB_PASSWORD'), "
            "'parent_secret': os.environ.get('AWS_SECRET_ACCESS_KEY')}))"
        )
        with tempfile.TemporaryDirectory(prefix="backups-artisan-db-context-") as temporary:
            script_path = pathlib.Path(temporary) / "artisan-down.py"
            script_path.write_text(script)
            result = run_tool("php", [sys.executable, str(script_path)], 10, database_context=MARIADB_CONTEXT)

        self.assertEqual(json.loads(result), {
            "driver": "mariadb",
            "database": "paymenter_fixture",
            "password": "fixture-password",
            "parent_secret": None,
        })


if __name__ == "__main__":
    unittest.main()
