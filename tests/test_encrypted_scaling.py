"""Real output accounting with large private history; no native application."""
import json
import os
import pathlib
import time
from unittest.mock import patch

from paymenter_backups import engine, remote_artifacts, storage
from paymenter_backups.common import BackupError
from test_engine_schedules import PrivateFixture


class EncryptedScaling(PrivateFixture):
    def history(self):
        for index in range(5000):
            run = 'history-' + str(index)
            directory = self.state.run_path(run)
            directory.mkdir(mode=0o700)
            target = directory / 'journal.json'
            target.write_text(json.dumps({'run_id': run, 'installation_uuid': self.state.uuid,
                                         'retained_deleted': True, 'created_at': index + 1}))
            target.chmod(0o600)

    def test_real_capture_child_accounts_5000_journals_only_at_boundaries(self):
        self.history()
        target = self.state.root / 'database.sql'
        script = self.script('large-dump', "import sys,time\nfor _ in range(128):\n sys.stdout.buffer.write(b'x'*65536);sys.stdout.buffer.flush();time.sleep(.005)")
        with patch.object(engine, 'storage_usage', wraps=engine.storage_usage) as accounting:
            with patch.object(engine.DiskReserve, 'check', autospec=True, side_effect=engine.DiskReserve.check) as polls:
                engine.run_output_tool(self.state, self.settings, 'dump', [script], 10, target, stdout=True)
        self.assertEqual(target.stat().st_size, 8 * 1024**2)
        self.assertGreater(polls.call_count, 5)
        self.assertEqual(accounting.call_count, 2)

    def test_plaintext_retrieval_accounts_5000_journals_only_at_boundaries(self):
        from test_admin_contracts import AdminArtifactContracts
        fixture = AdminArtifactContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        archive, _ = fixture.backup('large-plaintext', payload=os.urandom(4 * 1024**2))
        fixture.envelope['settings'].update(minimum_free_bytes=0, s3_bucket='fixture', destination='s3',
                                            s3_encryption=False, plaintext_offsite_acknowledged=True)
        storage.deliver(fixture.state, fixture.envelope, 'large-plaintext')
        self.state = fixture.state
        self.history()
        item = next(item for item in self.state.journals() if item['run_id'] == 'large-plaintext')
        directory_fd = os.open(archive.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch.object(engine, 'storage_usage', wraps=engine.storage_usage) as accounting:
                with storage.checked_remote(self.state, fixture.envelope, item, directory=archive.parent, directory_fd=directory_fd) as (temporary, manifest):
                    self.assertEqual(temporary.read_bytes(), archive.read_bytes())
                    self.assertEqual(manifest['archive']['size'], archive.stat().st_size)
            self.assertEqual(accounting.call_count, 2)
        finally:
            os.close(directory_fd)

    def test_capture_output_final_accounting_refuses_other_growth(self):
        target = self.state.root / 'database.sql'
        other = self.state.root / 'other-owned-output'
        self.settings['local_byte_budget'] = engine.storage_usage(self.state, self.settings) + 65536
        script = self.script('other-growth', 'import pathlib,sys\npathlib.Path(sys.argv[1]).write_bytes(b"x"*65536)\nsys.stdout.write("dump")')
        with self.assertRaisesRegex(BackupError, 'local-byte-budget-exceeded'):
            engine.run_output_tool(self.state, self.settings, 'dump', [script, str(other)], 10, target, stdout=True)

    def test_capture_deadline_checks_admission_and_stops_real_child(self):
        from paymenter_backups.process import group_members
        target = self.state.root / 'database.sql'
        script = self.script('slow-dump', 'import sys,time\nfor _ in range(100):\n sys.stdout.write("dump");sys.stdout.flush();time.sleep(.05)')
        deadline = time.monotonic() + .15
        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise BackupError('capture-timeout')
            return seconds
        # A larger child timeout must not override the capture's absolute bound.
        groups = []
        with self.assertRaisesRegex(BackupError, 'capture-timeout'):
            engine.run_output_tool(self.state, self.settings, 'dump', [script], 10, target,
                                   stdout=True, deadline_check=remaining, register=groups.append)
        self.assertTrue(groups)
        self.assertFalse(group_members(groups[0]))
        target.unlink()
        deadline = time.monotonic() + .01
        with patch.object(engine, 'output_limit', side_effect=lambda *args: time.sleep(.02) or 65536):
            with self.assertRaisesRegex(BackupError, 'capture-timeout'):
                engine.run_output_tool(self.state, self.settings, 'dump', [script], remaining, target, stdout=True)
        self.assertFalse(target.exists())

    def test_capture_deadline_is_checked_after_final_history_accounting(self):
        target = self.state.root / 'database.sql'
        script = self.script('finished-dump', 'print("finished dump")')
        expired = False
        def remaining():
            if expired:
                raise BackupError('capture-timeout')
            return 10
        original_space = engine.space
        def final_accounting(*args):
            nonlocal expired
            self.assertEqual(target.read_bytes(), b'finished dump\n')
            original_space(*args)
            expired = True
        with patch.object(engine, 'space', side_effect=final_accounting):
            with self.assertRaisesRegex(BackupError, 'capture-timeout'):
                engine.run_output_tool(self.state, self.settings, 'dump', [script], remaining, target,
                                       stdout=True, deadline_check=remaining)

    def test_200_mib_spool_accounts_5000_journals_only_at_boundaries(self):
        self.settings.update(local_byte_budget=512 * 1024**2)
        self.history()
        item = {'run_id': 'large-spool', 'installation_uuid': self.state.uuid, 'created_at': 6000,
                'delivery_intent': {'spools': {}}}
        self.state.run_path(item['run_id']).mkdir(mode=0o700)
        self.state.save(item)
        chunk = b'fixture ciphertext bytes' * 4096
        wanted = 200 * 1024**2

        def chunks():
            remaining = wanted
            while remaining:
                part = chunk[:min(remaining, len(chunk))]
                remaining -= len(part)
                yield part

        with patch.object(engine, 'storage_usage', wraps=engine.storage_usage) as accounting:
            receipt, digest = remote_artifacts._write_spool(self.state, self.envelope, item, 'archive', chunks())
        self.assertEqual(digest['size'], wanted)
        self.assertEqual((self.state.run_path(item['run_id']) / receipt['name']).stat().st_size, wanted)
        self.assertEqual(accounting.call_count, 2)

    def test_reserve_is_periodic_and_still_refuses_disk_pressure(self):
        from types import SimpleNamespace
        from paymenter_backups.common import BackupError
        guard = remote_artifacts.DiskReserve(self.state, {'minimum_free_bytes': 100}, self.state.root)
        with patch('shutil.disk_usage', return_value=SimpleNamespace(free=101)) as disk:
            with patch('time.monotonic', return_value=1):
                for index in range(10000):
                    guard.check(index)
            self.assertEqual(disk.call_count, 1)
            disk.return_value = SimpleNamespace(free=99)
            with self.assertRaisesRegex(BackupError, 'disk-reserve-exceeded'):
                guard.check(10000, force=True)

    def test_reserve_checks_byte_and_time_boundaries_without_history(self):
        from types import SimpleNamespace
        guard = engine.DiskReserve(self.state, {'minimum_free_bytes': 100}, self.state.root)
        with patch('shutil.disk_usage', return_value=SimpleNamespace(free=101)) as disk:
            with patch('time.monotonic', return_value=1):
                guard.check(0)
                disk.return_value = SimpleNamespace(free=99)
                guard.check(64 * 1024**2 - 1)
                with self.assertRaisesRegex(BackupError, 'disk-reserve-exceeded'):
                    guard.check(64 * 1024**2)
            with patch('time.monotonic', return_value=2):
                with self.assertRaisesRegex(BackupError, 'disk-reserve-exceeded'):
                    guard.check(1)

    def test_transfer_deadline_is_size_scaled_and_independent_of_capture(self):
        self.assertEqual(remote_artifacts.proof_timeout(200 * 1024**2), 1100)
        self.assertEqual(remote_artifacts.proof_timeout(1024**5), 86400)
        self.assertGreaterEqual(remote_artifacts.proof_timeout(1), 300)

    def test_actual_delivery_and_remote_proof_ignore_tiny_capture_timeout(self):
        import test_encryption
        fixture = test_encryption.Encryption('test_real_gpg_roundtrip_packet_profile_and_corruption')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        item = fixture.archive()
        run_id = item['run_id']
        fixture.envelope['settings']['capture_timeout'] = 0.001
        storage.deliver(fixture.state, fixture.envelope, run_id)
        item = next(item for item in fixture.state.journals() if item['run_id'] == run_id)
        with remote_artifacts.checked_remote(fixture.state, fixture.envelope, item, record=True) as (_, manifest):
            self.assertEqual(manifest['archive'], item['manifest']['archive'])

    def test_failed_spool_budget_keeps_other_files_and_removes_unpublished_output(self):
        item = {'run_id': 'failed-spool', 'installation_uuid': self.state.uuid, 'created_at': 1,
                'delivery_intent': {'spools': {}}}
        directory = self.state.run_path(item['run_id'])
        directory.mkdir(mode=0o700)
        sentinel = directory / 'unrelated.bin'
        sentinel.write_bytes(b'keep')
        sentinel.chmod(0o600)
        self.state.save(item)
        for oversized in (True, False):
            with self.subTest(oversized=oversized):
                with patch.object(engine, 'output_limit', return_value=128):
                    with patch.object(engine, 'space', side_effect=None if oversized else BackupError('local-byte-budget-exceeded')):
                        with self.assertRaisesRegex(BackupError, 'local-byte-budget-exceeded'):
                            remote_artifacts._write_spool(self.state, self.envelope, item, 'archive', iter([b'x' * (129 if oversized else 128)]))
                self.assertFalse((directory / 'delivery.archive.gpg').exists())
                self.assertEqual(sentinel.read_bytes(), b'keep')
                self.assertEqual(item['delivery_intent']['spools'], {})
