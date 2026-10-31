"""Incident decisions and separate PHP command processes; no real mail or app."""
import copy
import json
import os
import subprocess
import sys
import unittest

from paymenter_backups import config, observation, requests
from paymenter_backups.common import atomic_json, read_json
from test_engine_schedules import PHP, ROOT, PrivateFixture, instant


@unittest.skipUnless(PHP, 'PHP CLI required for the cold settings subprocess')
class ObservationTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        self.start = instant('2026-10-23T00:00:00+00:00')
        self.settings.update(database_schedule='hourly', full_schedule='daily', full_time='02:00')
        config.normalize(self.settings)
        requests.sync_schedule(self.state, self.settings, now=self.start)

    def check(self, at):
        atomic_json(self.state.root / 'scheduler.json', {'last_tick_at': at})
        return self.results(at)

    def results(self, at):
        return {item['key']: item for item in observation.check(self.state, self.envelope, now=at)['checks']}

    def test_separate_types_and_locations_and_no_mail_during_recovery(self):
        self.remote()
        self.backup('full', 'full-quiesced', self.start + 2 * 3600)
        at = self.start + 4 * 3600
        checks = self.check(at)
        self.assertIsNone(checks['full:local']['reason'])
        self.assertEqual(checks['database:local']['reason'], 'backup-overdue')
        self.assertEqual(checks['full:offsite']['reason'], 'backup-overdue')
        self.assertEqual(checks['database:offsite']['reason'], 'backup-overdue')
        # A deferred failure belongs to the runtime until the freshness deadline.
        item = requests.create(self.state, arguments={'mode': 'full-quiesced'})
        item.update(status='deferred', error='child-failed', created_at=at)
        atomic_json(requests.path(self.state, item['request_id']), item)
        self.assertIsNone(self.check(at)['full:local']['reason'])
        item['status'] = 'failed'
        atomic_json(requests.path(self.state, item['request_id']), item)
        self.assertEqual(self.check(at)['full:local']['reason'], 'backup-failed')
        self.backup('recovered', 'full-quiesced', at + 60)
        self.assertIsNone(self.check(at + 120)['full:local']['reason'])

    def test_dead_scheduler_and_worker_are_visible_without_running_tick(self):
        item = requests.create(self.state, arguments={'mode': 'database-online'})
        item.update(status='running', created_at=self.start, worker={'pid': 99999999})
        atomic_json(requests.path(self.state, item['request_id']), item)
        atomic_json(self.state.root / 'scheduler.json', {'last_tick_at': self.start})
        at = self.start + 4 * 3600
        self.assertEqual(self.results(at)['scheduler']['reason'], 'scheduler-stopped')
        # Healthy scheduler check-ins cannot conceal a worker that made no copy.
        self.assertIsNone(self.check(at)['scheduler']['reason'])
        self.assertEqual(self.results(at)['database:local']['reason'], 'backup-overdue')
        self.assertEqual(read_json(requests.path(self.state, item['request_id']))['status'], 'running')

    def test_off_schedules_do_not_raise_freshness_or_scheduler_incidents(self):
        self.settings.update(database_schedule='off', full_schedule='off')
        config.normalize(self.settings)
        checks = self.results(self.start + 30 * 86400)
        self.assertNotIn('scheduler', checks)
        self.assertTrue(all(item['reason'] is None for item in checks.values()))

    def test_missing_local_archive_does_not_count_as_fresh(self):
        archive, _ = self.backup('database', created_at=self.start + 3 * 3600)
        at = self.start + 3 * 3600 + 60
        self.assertIsNone(self.check(at)['database:local']['reason'])
        archive.unlink()
        self.assertEqual(self.check(at)['database:local']['reason'], 'backup-overdue')

    def test_offsite_delivery_does_not_hide_stale_local_or_changed_destination(self):
        self.remote()
        archive, _ = self.backup('database', created_at=self.start + 3 * 3600, deliver=True)
        at = self.start + 3 * 3600 + 60
        self.assertIsNone(self.check(at)['database:offsite']['reason'])
        archive.unlink()
        checks = self.check(at)
        self.assertEqual(checks['database:local']['reason'], 'backup-overdue')
        self.assertIsNone(checks['database:offsite']['reason'])
        self.settings['s3_bucket'] = 'replacement'
        self.assertEqual(self.check(at)['database:offsite']['reason'], 'backup-overdue')

    def test_weekly_first_deadline_and_grace_follow_saved_schedule(self):
        self.settings.update(full_schedule='weekly', full_weekdays=[6], full_time='03:00')
        requests.sync_schedule(self.state, self.settings, now=self.start)
        before = self.check(instant('2026-10-25T03:01:00+00:00'))['full:local']
        self.assertEqual(before['due_at'], instant('2026-10-25T03:00:00+00:00'))
        self.assertIsNone(before['reason'])
        self.assertEqual(self.check(before['deadline_at'] + 1)['full:local']['reason'], 'backup-overdue')
        self.settings['full_time'] = '05:00'
        requests.sync_schedule(self.state, self.settings, now=before['deadline_at'] + 1)
        self.assertIsNone(self.check(before['deadline_at'] + 2)['full:local']['reason'])

    def test_full_counts_for_database_only_with_runtime_coverage_proof(self):
        at = self.start + 3 * 3600
        self.backup('full', 'full-quiesced', at)
        self.assertEqual(self.check(at + 60)['database:local']['reason'], 'backup-overdue')
        epoch = read_json(self.state.root / 'schedule.json')['kinds']['database']
        requests.create(self.state, arguments={'mode': 'database-online'}, slot='database-slot',
            scheduled_metadata={'schedule_kind': 'database', 'slot_at': at,
                                'schedule_fingerprint': epoch['fingerprint'], 'schedule_since': epoch['since']})
        journal = self.item('full')
        journal.update(database_snapshot_lower_bound=at, database_coverage={'snapshot_lower_bound': at})
        self.state.save(journal)
        requests.reconcile_coverage(self.state, self.envelope, cheap_only=True)
        self.assertIsNone(self.check(at + 60)['database:local']['reason'])

    def test_observe_cli_uses_cold_settings_and_never_reads_saved_credentials(self):
        envelope = copy.deepcopy(self.envelope)
        envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(self.state.root))
        payload = self.base / 'settings.json'
        payload.write_text(json.dumps(envelope))
        bridge = self.base / 'bridge.php'
        bridge.write_text('<?php if ($argv[1] !== "read-settings") { exit(2); } echo file_get_contents(' + json.dumps(str(payload)) + ');')
        result = subprocess.run([sys.executable, '-I', str(ROOT / 'bin/paymenter-backups'),
            '--app', str(self.app), '--private-root', str(self.state.root), '--php', PHP,
            '--bridge', str(bridge), 'observe'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['installation_uuid'], self.state.uuid)
        self.assertNotIn(str(self.app), result.stdout)


@unittest.skipUnless(PHP, 'PHP CLI required for the incident command subprocess')
class IncidentMailTests(PrivateFixture):
    def invoke(self, checks, fail=False, disabled=False):
        state = self.base / 'observation.json'
        atomic_json(state, {'installation_uuid': self.state.uuid, 'checks': checks})
        result = subprocess.run([PHP, '-d', 'extension=posix', str(ROOT / 'tests/integration/incident_mail.php'), str(self.state.root),
            str(state), str(self.base / 'outbox.json'), 'fail' if fail else 'ok', 'disabled' if disabled else 'enabled'],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_durable_dedupe_pending_transport_and_rearm_after_recovery(self):
        checks = [{'key': 'database:local', 'reason': 'backup-failed'}]
        self.assertEqual(self.invoke(checks, fail=True)['status'], 1)
        ledger = read_json(self.state.root / 'incidents.json')
        incident_id = ledger['active']['database:local']['id']
        self.assertIsNone(ledger['active']['database:local']['sent_at'])
        self.assertEqual(self.invoke(checks)['sent'], 1)
        self.assertEqual(self.invoke([{'key': 'database:local', 'reason': 'backup-overdue'}])['sent'], 1)
        self.assertEqual(read_json(self.state.root / 'incidents.json')['active']['database:local']['id'], incident_id)
        self.invoke([{'key': 'database:local', 'reason': None}])
        self.assertEqual(self.invoke(checks)['sent'], 2)
        self.assertNotEqual(read_json(self.state.root / 'incidents.json')['active']['database:local']['id'], incident_id)

    def test_mail_is_synchronous_to_system_staff_address_and_not_lost_when_disabled(self):
        checks = [{'key': 'scheduler', 'reason': 'scheduler-stopped'}]
        self.assertEqual(self.invoke(checks, disabled=True)['status'], 1)
        result = self.invoke(checks)
        self.assertEqual(result['status'], 0)
        self.assertEqual(result['sent'], 1)
        mail = read_json(self.base / 'outbox.json')[0]
        self.assertEqual(mail['to'], 'staff@example.invalid')
        self.assertIn('cron job', mail['body'])
        self.assertNotIn('fake-canary', mail['body'])

    def test_unreadable_engine_retries_before_mail_and_preserves_open_incidents(self):
        self.invoke([{'key': 'full:local', 'reason': 'backup-failed'}])
        self.assertEqual(self.invoke(None)['sent'], 1)
        ledger = read_json(self.state.root / 'incidents.json')
        self.assertIn('full:local', ledger['active'])
        ledger['active']['observer']['opened_at'] -= 301
        atomic_json(self.state.root / 'incidents.json', ledger)
        self.assertEqual(self.invoke(None)['sent'], 2)
        self.assertEqual(self.invoke(None)['sent'], 2)
        self.invoke([])
        self.assertEqual(read_json(self.state.root / 'incidents.json')['active'], [])
