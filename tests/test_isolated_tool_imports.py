"""Tool scripts that run under isolated Python can load their explicit helpers."""
import pathlib
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class IsolatedToolImportTests(unittest.TestCase):
    def test_encrypted_restore_loads_fixture_helper_under_isolated_python(self):
        script = ROOT / "tools/native_encrypted_restore.py"
        with tempfile.TemporaryDirectory(prefix="isolated-tool-import-") as temporary:
            environment = dict(os.environ, TMPDIR=temporary)
            result = subprocess.run(
                [sys.executable, "-I", str(script), "--work", str(ROOT / "not-a-fixture"),
                 "--php", "/usr/bin/php", "--run", "fixture-run"],
                capture_output=True, text=True, timeout=10, env=environment,
            )
        self.assertNotIn("No module named 'fixture_environment'", result.stderr)
        self.assertIn("fixture workroot is outside its private temporary base", result.stderr)
