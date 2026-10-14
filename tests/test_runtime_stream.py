"""The web download never waits on the engine without a bound."""
import json
import pathlib
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("php"), "PHP is needed for the native stream bounds")
class RuntimeStreamBounds(unittest.TestCase):
    def test_startup_idle_refusal_and_cleanup_are_bounded(self):
        result = subprocess.run([shutil.which("php"), str(ROOT / "tests/integration/runtime_stream_checks.php"), sys.executable],
                                check=True, capture_output=True, text=True, timeout=60)
        checks = json.loads(result.stdout)
        self.assertEqual(checks["startup"], "artifact-stream-timeout")
        self.assertEqual(checks["sigterm_ignored"], "artifact-stream-timeout")
        self.assertEqual(checks["refusal"], "verification-required")
        self.assertEqual(checks["idle"], "artifact-stream-timeout")
        self.assertFalse(checks["idle_child_running"])
        # Startup or idle limit plus at most three short grace periods.
        for key in ("startup_seconds", "sigterm_ignored_seconds", "idle_seconds"):
            self.assertLess(checks[key], 9, key)
        self.assertEqual((checks["body"], checks["header_size"]), ("abcdef", 6))


if __name__ == "__main__":
    unittest.main()
