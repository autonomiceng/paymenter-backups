"""A backup records when it finished; older backups say nothing rather than guess."""
import time
import unittest

import test_engine

from paymenter_backups.common import BackupError
from paymenter_backups.engine import capture
from paymenter_backups.presentation import run_entry


class RunFinishTime(unittest.TestCase):
    # Borrow the engine fixture without inheriting, and so rerunning, its tests.
    script = test_engine.Fixture.script

    def setUp(self):
        test_engine.Fixture.setUp(self)
        self.settings.update(mode="database-online", lock_timeout=0)

    def test_completed_backup_records_its_finish_time(self):
        started = time.time()
        journal = capture(self.state, self.envelope)
        self.assertGreaterEqual(journal["finished_at"], journal["created_at"])
        self.assertGreaterEqual(journal["created_at"], started - 1)
        entry = run_entry(self.state, self.state.journals()[0], set())
        self.assertEqual(entry["status"], "ready")
        self.assertEqual(entry["finished_at"], journal["finished_at"])

    def test_failed_backup_records_when_it_stopped(self):
        self.envelope["tools"]["dump"] = str(self.script("faildump", "import sys;sys.exit(1)"))
        with self.assertRaises(BackupError):
            capture(self.state, self.envelope)
        item = self.state.journals()[0]
        self.assertEqual(item["phase"], "failed")
        entry = run_entry(self.state, item, set())
        self.assertEqual(entry["status"], "failed")
        self.assertGreaterEqual(entry["finished_at"], entry["created_at"])

    def test_backup_from_an_earlier_version_has_no_finish_time(self):
        capture(self.state, self.envelope)
        item = self.state.journals()[0]
        del item["finished_at"]
        self.state.save(item)
        entry = run_entry(self.state, self.state.journals()[0], set())
        self.assertEqual(entry["status"], "ready")
        self.assertIsNone(entry["finished_at"])


if __name__ == "__main__":
    unittest.main()
