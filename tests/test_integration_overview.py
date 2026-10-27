"""Combined engine/encryption page contracts with small disposable fixtures."""
import copy
import json
import os
from unittest.mock import MagicMock, patch

from paymenter_backups import config, encryption, engine, presentation, storage
from paymenter_backups.common import BackupError
from test_encryption import key_record
from test_engine_schedules import PrivateFixture


class IntegratedOverview(PrivateFixture):
    def test_full_readiness_does_not_stat_every_application_file(self):
        for index in range(200):
            (self.app / ('file-' + str(index))).write_text('fixture')
        with patch('os.lstat', wraps=os.lstat) as stats:
            engine.external_symlinks(self.app)
        self.assertLess(stats.call_count, 10)

    def test_symlink_scan_preserves_unreadable_directory_handling(self):
        for failure in ('iteration', 'classification'):
            with self.subTest(failure=failure):
                entries = MagicMock()
                entries.__enter__.return_value = entries
                if failure == 'iteration':
                    entries.__iter__.side_effect = OSError('unreadable directory')
                else:
                    entry = MagicMock()
                    entry.is_symlink.return_value = False
                    entry.is_dir.side_effect = OSError('unreadable entry')
                    entries.__iter__.return_value = iter([entry])
                with patch('os.scandir', return_value=entries):
                    engine.external_symlinks(self.app)

    def test_overview_uses_recorded_archive_checks_without_reading_payloads(self):
        self.settings['retention_keep_database'] = 3
        for index in range(6):
            self.backup('database-' + str(index), 'database-online', index + 1)
        self.remote(s3_encryption=True)
        # Unchanged verified archives need only metadata checks during rendering.
        # The worker that actually applies retention still checks their bytes.
        with patch.object(engine, 'check_archive', side_effect=AssertionError('render read an archive payload')), \
                patch.object(engine.artifacts.Artifact, 'digest', side_effect=AssertionError('render hashed an archive')), \
                patch('paymenter_backups.storage.client', side_effect=AssertionError('render contacted storage')):
            value = presentation.overview(self.state, self.envelope)
        self.assertEqual(len(value['runs']), 6)
        # Undelivered local sources remain protected by the S3 policy.
        self.assertEqual(value['configuration']['retention']['pending']['database']['local'], 0)
        self.assertTrue(all(run['download'] == 'ready' for run in value['runs']))

    def test_symlink_scan_preserves_internal_external_and_broken_link_checks(self):
        (self.app / 'inside').mkdir()
        link = self.app / 'link'
        link.symlink_to(self.app / 'inside')
        engine.external_symlinks(self.app)
        link.unlink()
        for target in (self.base, self.app / 'missing', link):
            with self.subTest(target=target):
                link.symlink_to(target)
                with self.assertRaisesRegex(BackupError, 'external-symlink-target'):
                    engine.external_symlinks(self.app)
                link.unlink()

    def test_retention_preview_rechecks_a_changed_archive(self):
        archive, _ = self.backup('changed')
        self.backup('newer', created_at=2)
        with archive.open('r+b') as output:
            output.seek(-1, 2)
            output.write(b'!')
        preview = storage.retention(self.state, self.envelope)
        self.assertIn({'run_id': 'changed', 'code': 'archive-checksum-mismatch'}, preview['damaged'])
        self.assertTrue(archive.exists())

    def test_retention_execution_rechecks_verified_payloads_before_deleting(self):
        self.settings['retention_keep_database'] = 1
        older, _ = self.backup('older', created_at=1)
        newer, _ = self.backup('newer', created_at=2)
        with patch.object(engine, 'check_archive', side_effect=BackupError('archive-payload-mismatch')) as checked:
            result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(checked.call_count, 2)
        self.assertEqual({item['run_id'] for item in result['damaged']}, {'older', 'newer'})
        self.assertTrue(older.exists() and newer.exists())

    def encryption_namespace(self, pending=False):
        record = key_record()
        fingerprint = record['fingerprint']['value']
        if pending:
            record['state'] = 'pending'
            record.pop('acknowledgement')
        self.envelope['archive_encryption_credentials'] = {
            'version': 1, 'active_fingerprint': None if pending else fingerprint,
            'pending_fingerprint': fingerprint if pending else None,
            'keys': {fingerprint: record}}
        return fingerprint, record

    def test_delivery_setup_is_visible_without_blocking_local_capture(self):
        self.remote(s3_encryption=True)
        for expected, pending, tool_missing in (
                ('archive-key-required', False, False),
                ('archive-key-save-required', True, False),
                ('archive-encryption-tool-missing', False, True)):
            with self.subTest(code=expected):
                self.envelope.pop('archive_encryption_credentials', None)
                if pending:
                    self.encryption_namespace(pending=True)
                self.settings['gpg'] = '/missing/gpg' if tool_missing else '/usr/bin/gpg'
                proposed = engine.plan(self.state, self.envelope)
                self.assertEqual(proposed['admission_errors'], [])
                self.assertIn(expected, [item['code'] for item in proposed['readiness_items']])
                overview = presentation.overview(self.state, self.envelope)
                conf = overview['configuration']
                self.assertEqual(conf['encryption']['readiness_code'], expected)
                self.assertEqual(conf['admission']['database-online'], [])
                self.assertIn(expected, [item['code'] for item in conf['readiness']['database-online']['items']])

    def test_overview_preserves_pending_identity_typed_history_and_secret_boundary(self):
        self.backup('full', 'full-quiesced', 1)
        self.backup('database', 'database-online', 2)
        self.remote(s3_encryption=True)
        fingerprint, record = self.encryption_namespace(pending=True)
        value = presentation.overview(self.state, self.envelope)
        conf = value['configuration']
        self.assertEqual(conf['encryption']['key']['pending_fingerprint'], fingerprint)
        self.assertEqual(conf['encryption']['key']['pending_short'], fingerprint[:16])
        self.assertEqual(conf['encryption']['key']['state'], 'pending')
        self.assertEqual((value['last_good']['full'], value['last_good']['database']), ('full', 'database'))
        self.assertEqual(set(conf['retention']['pending']), {'full', 'database'})
        self.assertEqual(set(conf['schedules']), {'timezone', 'full', 'database'})
        self.assertIn('ciphertext_size', value['runs'][0])
        self.assertIn('staging_removal_pending', value['runs'][0])
        self.assertNotIn(record['secret'], json.dumps(value))
        self.assertEqual(config.redact(value), value)

    def test_destination_evidence_and_schedule_coverage_survive_combined_projection(self):
        self.remote(s3_encryption=True)
        fingerprint, _ = self.encryption_namespace()
        binding = presentation.destination_test_binding(self.state, self.envelope)
        item = {'request_id': 'test', 'action': 'destination-test', 'status': 'complete',
                'schedule_kind': 'database', 'covered_by': 'covering-full',
                'result': {'verified': True, 'destination': 's3',
                           'destination_id': binding['destination_id'],
                           'format': encryption.FORMAT, 'key_fingerprint': fingerprint}}
        value = presentation.request_entry(item, self.state, self.envelope, binding)
        self.assertTrue(value['result_verified'])
        self.assertEqual(value['schedule_kind'], 'database')
        self.assertEqual(value['covered_by'], 'covering-full')
        stale = copy.deepcopy(item)
        stale['result']['key_fingerprint'] = '0' * 64
        self.assertFalse(presentation.request_entry(stale, self.state, self.envelope, binding)['result_verified'])
