"""Deployment selection survives real scrubbed PHP/Python child boundaries."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from paymenter_backups import process

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = shutil.which('php')
SETTING = 'PAYMENTER_BACKUPS_GUARDED_SCHEDULER'
SCRIPT = '/opt/paymenter/custom % runner.php'
CHILD = '''import json, os
print(json.dumps({'ok': True, 'size': 1, 'filename': 'paymenter-backup-test.tar.gz',
                  'script': os.environ.get('PAYMENTER_BACKUPS_GUARDED_SCHEDULER'),
                  'override': os.environ.get('MYSQL_PWD')}), flush=True)
'''
PROGRAM = r'''
function base_path() { return getcwd(); }
require $argv[1] . '/extension/Backups/Runtime.php';
use Paymenter\Extensions\Others\Backups\Runtime;
[$process, $stdout, $stderr, $header] = Runtime::openStream([$argv[2], '-c', $argv[3]], 5);
Runtime::stopStream($process, [$stdout, $stderr]);
echo json_encode(['path' => Runtime::guardedSchedulerPath(), 'child' => $header], JSON_THROW_ON_ERROR);
'''


class GuardedSchedulerEnvironment(unittest.TestCase):
    def test_python_child_keeps_only_configured_scheduler_selection(self):
        with patch.dict(os.environ, {SETTING: SCRIPT, 'MYSQL_PWD': 'fixture-only'}):
            result = json.loads(process.run([sys.executable, '-c', CHILD], 5))
        self.assertEqual(result['script'], SCRIPT)
        self.assertIsNone(result['override'])

    def test_detached_worker_keeps_scheduler_selection(self):
        with tempfile.TemporaryDirectory(prefix='backups-guarded-env-') as temporary:
            target = pathlib.Path(temporary) / 'result.json'
            child = "import json, os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(os.environ.get('PAYMENTER_BACKUPS_GUARDED_SCHEDULER')))"
            with patch.dict(os.environ, {SETTING: SCRIPT}):
                pid = process.detach([sys.executable, '-c', child, str(target)])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(item.pid == pid for item in process._DETACHED_CHILDREN):
                process.reap_detached()
                time.sleep(.01)
            self.assertEqual(json.loads(target.read_text()), SCRIPT)

    @unittest.skipUnless(PHP, 'PHP CLI required for child environment checks')
    def test_php_child_preserves_only_valid_deployment_selection(self):
        for value, expected in ((SCRIPT, SCRIPT), ('', ''), ('/opt/../runner.php', ''),
                                ('/opt//runner.php', ''), ('/opt/runner.php/', ''), ('/opt/runner.php\n', '')):
            with self.subTest(value=value), patch.dict(os.environ, {SETTING: value, 'MYSQL_PWD': 'fixture-only'}):
                result = subprocess.run([PHP, '-r', PROGRAM, str(ROOT), sys.executable, CHILD],
                                        text=True, capture_output=True, check=True, timeout=10)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['path'], expected)
                self.assertEqual(payload['child']['script'], expected or None)
                self.assertIsNone(payload['child']['override'])
