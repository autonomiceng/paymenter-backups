"""Exercise the production PHP bridge with its private database handoff."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_database_context import MARIADB_CONTEXT


ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get("PAYMENTER_BACKUPS_TEST_PHP") or shutil.which("php")
BRIDGE = ROOT / "extension/Backups/bridge.php"
SAFE_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}


@unittest.skipUnless(PHP, "PHP CLI required for bridge integration")
class RealBridgeDatabaseContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="backups-real-bridge-")
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.app = self.base / "app"
        (self.app / "vendor").mkdir(parents=True)
        self.private = self.base / "paymenter-backups-private"
        self.private.mkdir(mode=0o700)
        # Keep the Laravel bootstrap small while executing bridge.php itself.
        # These framework boundary classes let the production bridge reach its
        # PDO-driver check without needing a full Paymenter checkout.
        (self.app / "vendor/autoload.php").write_text(r'''<?php
namespace Illuminate\Foundation {
    final class Application {
        public function __construct(string $base) {}
        public function bootstrapWith(array $bootstrappers): void {}
        public function make(string $name): object { return new \FixtureConfig(); }
    }
}
namespace Illuminate\Foundation\Bootstrap {
    final class LoadConfiguration {}
    final class LoadEnvironmentVariables {}
}
namespace {
    final class FixtureConfig {
        public function get(string $key, mixed $default = null): mixed { return $default; }
    }
}
''')
        drivers = subprocess.run(
            [PHP, "-r", "echo json_encode(PDO::getAvailableDrivers());"],
            capture_output=True, check=True, timeout=10, env=SAFE_ENVIRONMENT,
        )
        self.drivers = json.loads(drivers.stdout)
        self.expected_bridge_error = (
            "database-pdo-driver-unavailable"
            if "mysql" not in self.drivers else "database-connection-failed"
        )

    def bridge_call(self, verb):
        request = {"database_connection": MARIADB_CONTEXT, "payload": None}
        return subprocess.run(
            [PHP, str(BRIDGE), verb, "--app", str(self.app), "--private-root", str(self.private),
             "--database-connection-stdin"],
            input=json.dumps(request).encode(), capture_output=True, timeout=15,
            env=SAFE_ENVIRONMENT,
        )

    def test_real_bridge_accepts_mariadb_context_for_read_and_read_settings(self):
        for verb in ("read", "read-settings"):
            with self.subTest(verb=verb):
                result = self.bridge_call(verb)
                self.assertEqual(result.returncode, 1, result.stdout.decode(errors="replace"))
                self.assertEqual(json.loads(result.stderr), {"error": self.expected_bridge_error})

    def test_cli_config_check_reaches_real_bridge_with_mariadb_context(self):
        request = {"settings": {}, "_database_connection": MARIADB_CONTEXT}
        environment = dict(SAFE_ENVIRONMENT, PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run(
            [sys.executable, "-m", "paymenter_backups.cli", "--app", str(self.app),
             "--private-root", str(self.private), "--bridge", str(BRIDGE), "--php", PHP,
             "--database-connection-stdin", "config", "check"],
            input=json.dumps(request).encode(), capture_output=True, timeout=30,
            env=environment, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 1, result.stdout.decode(errors="replace"))
        self.assertEqual(json.loads(result.stderr), {"error": self.expected_bridge_error})


if __name__ == "__main__":
    unittest.main()
