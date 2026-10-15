"""Combined engine/encryption page contracts with small disposable fixtures."""
import copy
import json

from paymenter_backups import config, encryption, engine, presentation
from test_encryption import key_record
from test_engine_schedules import PrivateFixture


class IntegratedOverview(PrivateFixture):
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
