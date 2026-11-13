"""The panel pieces under extension/Backups stay exactly what tools/panel_sync.py writes.

tools/panel_sync.lock.json records the script's output. Set PAYMENTER_DOMAINS_CHECKOUT to a Domains git checkout to
also rebuild that output from Domains and compare it.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/panel_sync.py'
LOCK = json.loads((ROOT / 'tools/panel_sync.lock.json').read_text())
OWNED = {'extension/Backups/Panel/Presentation/State.php'}
COPIED_DIRS = ('extension/Backups/Panel', 'extension/Backups/resources/views/panel')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PanelSync(unittest.TestCase):
    def test_every_copied_file_matches_the_script_output(self):
        for name, digest in LOCK['files'].items():
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).is_file(), f'{name} is missing; run tools/panel_sync.py')
                self.assertEqual(sha(ROOT / name), digest, f'{name} was edited; change it in Domains and run tools/panel_sync.py')

    def test_lock_belongs_to_this_script_and_its_revision(self):
        self.assertEqual(sha(SCRIPT), LOCK['script'], 'tools/panel_sync.py changed; run it again')
        self.assertIn(f"REV = '{LOCK['rev']}'", SCRIPT.read_text())

    def test_nothing_else_lives_beside_the_copy(self):
        found = {str(path.relative_to(ROOT)) for folder in COPIED_DIRS for path in (ROOT / folder).rglob('*') if path.is_file()}
        self.assertEqual(found - set(LOCK['files']) - OWNED, set(), 'Add pieces in Domains, or own them outside the copy')
        self.assertEqual(OWNED - found, set())

    @unittest.skipUnless(os.environ.get('PAYMENTER_DOMAINS_CHECKOUT'), 'PAYMENTER_DOMAINS_CHECKOUT not set')
    def test_script_rebuilds_the_same_copy_from_domains(self):
        result = subprocess.run([sys.executable, str(SCRIPT), os.environ['PAYMENTER_DOMAINS_CHECKOUT'], '--check'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
