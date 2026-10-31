"""Engine schedules, storage policies and tool discovery with isolated fixtures.

No native application, full capture, host jobs, network or provider account.
"""
import copy
import datetime
import fcntl
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import shutil
import select
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from paymenter_backups import artifacts, cli, config, discovery, engine, presentation, requests, storage, tools
from paymenter_backups.common import BackupError, State, atomic_json, read_json
from paymenter_backups.maintenance import coordination
from paymenter_backups.readiness import stores, state_for
from test_retention_lifecycle import ObjectStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')


def instant(value):
    return datetime.datetime.fromisoformat(value).timestamp()


def settings(**changes):
    value = dict(config.DEFAULTS, settings_version=2, mode='database-online',
                 minimum_free_bytes=0, no_concurrent_ddl_acknowledged=True,
                 full_host_assumptions_acknowledged=True, **changes)
    config.normalize(value)
    return value


class PrivateFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='backups-engine-test-')
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.app = self.base / 'app'
        (self.app / 'public').mkdir(parents=True)
        self.state = State(self.app, self.base / 'private')
        self.settings = settings()
        self.envelope = {'settings': self.settings, 'app': str(self.app),
                         'tools': {name: self.script('php', "print('cli 8.3.35')") if name == 'php' else sys.executable for name in ('python', 'php', 'dump', 'mysql', 'tar', 'gzip')},
                         'framework': {'app_url': 'https://fixture.invalid', 'maintenance_driver': 'file',
                                       'stores': {kind: {'driver': driver, 'supported': True} for kind, driver in
                                                  (('maintenance', 'file'), ('cache', 'file'), ('session', 'file'), ('queue', 'database'), ('filesystem', 'local'))}},
                         'billing': {'daily_time': '00:00', 'timezone': 'UTC'}}
        self.envelope['tools']['systemctl'] = self.script('systemctl', "print('[]')")
        self.envelope['tools']['crontab'] = self.script('crontab', "print('# no fixture jobs')")
        self.store = ObjectStore()
        patched = patch.object(storage, 'client', return_value=self.store)
        patched.start()
        self.addCleanup(patched.stop)

    def script(self, name, body):
        target = self.base / name
        target.write_text('#!' + sys.executable + '\n' + body + '\n')
        target.chmod(0o700)
        return str(target)

    def backup(self, run_id, mode='database-online', created_at=1, deliver=False):
        directory = self.state.root / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        payloads = {'database.sql': b'CREATE TABLE fixture(id INT);\n'}
        if mode == 'full-quiesced':
            inner = io.BytesIO()
            with tarfile.open(fileobj=inner, mode='w') as archive:
                entry = tarfile.TarInfo('.env')
                data = b'APP_KEY=synthetic-private-value\n'
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            payloads['app.tar'] = inner.getvalue()
        manifest = {'schema_version': 1, 'installation_uuid': self.state.uuid, 'run_id': run_id,
                    'mode': mode, 'created_at': created_at,
                    'payloads': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)} for name, data in payloads.items()}}
        archive = directory / (run_id + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as output:
            for name, data in list(payloads.items()) + [('manifest.json', json.dumps(manifest).encode())]:
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
        archive.chmod(0o600)
        manifest['archive'] = {'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'size': archive.stat().st_size}
        manifest_path = directory / (run_id + '.manifest.json')
        atomic_json(manifest_path, manifest)
        self.state.run_path(run_id).mkdir(mode=0o700)
        journal = {'run_id': run_id, 'installation_uuid': self.state.uuid, 'created_at': created_at,
                   'phase': 'complete', 'mode': mode, 'capture_complete': True, 'service_recovered': True,
                   'local_complete': True, 'remote_delivered': False, 'manifest': manifest,
                   'archive_path': str(archive), 'manifest_path': str(manifest_path)}
        self.state.save(journal)
        with self.state.lock():
            engine.verify(self.state, run_id, record=True)
        if deliver:
            storage.deliver(self.state, self.envelope, run_id)
        return archive, manifest_path

    def item(self, run_id):
        return artifacts.journal(self.state, run_id)

    def remote(self, **changes):
        self.settings.update(dict(upload_s3=True, destination='s3', s3_bucket='fixture',
                             s3_endpoint='https://fixture.invalid', s3_encryption=False,
                             plaintext_offsite_acknowledged=True) | changes)

    def inventory(self):
        line = discovery.suggestions(self.state, self.envelope)['scheduler']
        # The fixture executable is Python; display commands use real PHP names.
        line = line.replace(sys.executable, '/usr/bin/php')
        return {'state': 'ok', 'error': None, 'checked_at': time.time(),
                'items': [discovery.record(line, self.app, 'crontab', 'line 1')]}

    def tick(self, at):
        with patch.object(requests.time, 'time', return_value=at), patch.object(discovery, 'refresh_cache'), patch.object(requests, 'detach', return_value=123456), patch.object(requests, 'process_identity', return_value=None):
            return requests.tick(self.state, self.envelope, ['fixture'])


class ConfigurationTests(PrivateFixture):
    def tool_reports(self, *args, **kwargs):
        return {name: {'configured': '', 'resolved': sys.executable, 'ok': True, 'code': None} for name in tools.CANDIDATES}

    def validate(self, value):
        with patch.object(tools, 'checks', side_effect=self.tool_reports):
            return config.validate({'settings': value})

    def test_shared_python_legacy_vectors_and_absent_marker(self):
        vectors = json.loads((ROOT / 'tests/fixtures/settings-migration.json').read_text())['vectors']
        for vector in vectors:
            with self.subTest(vector=vector['name']):
                self.assertEqual(config.from_legacy(vector['legacy']), vector['expected'])
                value = dict(vector['legacy'])
                config.normalize(value)
                self.assertEqual(value['settings_version'], 1)
                for key, expected in vector['expected'].items():
                    self.assertEqual(value[key], expected)

    def test_v2_defaults_and_all_valid_storage_combinations(self):
        self.assertEqual((self.settings['full_schedule'], self.settings['database_schedule']), ('off', 'off'))
        self.assertEqual((self.settings['full_time'], self.settings['full_weekdays']), ('03:00', [6]))
        for keep, upload in ((True, False), (True, True), (False, True)):
            value = settings(keep_local=keep, upload_s3=upload, s3_bucket='fixture')
            self.validate(value)
            self.assertEqual(value['destination'], 's3' if upload else 'local')
        with self.assertRaisesRegex(BackupError, 'storage-required'):
            self.validate(settings(keep_local=False, upload_s3=False))

    def test_plaintext_ack_is_conditional_and_fresh_when_disabling_encryption(self):
        self.validate(settings(upload_s3=True, s3_bucket='fixture'))
        with self.assertRaisesRegex(BackupError, 'plaintext-offsite'):
            self.validate(settings(upload_s3=True, s3_bucket='fixture', s3_encryption=False))
        self.validate(settings(upload_s3=True, s3_bucket='fixture', s3_encryption=False, plaintext_offsite_acknowledged=True))
        with self.assertRaisesRegex(BackupError, 'plaintext-offsite'):
            cli.require_fresh_plaintext_ack(dict(self.settings, plaintext_offsite_acknowledged=True), {'s3_encryption': False})
        cli.require_fresh_plaintext_ack(self.settings, {'s3_encryption': False, 'plaintext_offsite_acknowledged': True})


    def test_nonsecret_repair_can_replace_invalid_legacy_json_before_parsing(self):
        previous = {'settings_version': 1, 'job_lock_paths': 'invalid-json', 'schedule_weekdays': 'invalid-json'}
        cli.require_fresh_plaintext_ack(previous, {'job_lock_paths': [], 'schedule_weekdays': [0]})
        cli.require_fresh_plaintext_ack(previous, {'settings_version': 2, 's3_encryption': False, 'plaintext_offsite_acknowledged': True})
        value = settings()
        value.update(schedule_weekdays='stale invalid legacy row', retention_keep='stale invalid count')
        config.normalize(value)
        self.validate(value)

    def test_legacy_s3_stays_plaintext_and_unacknowledged_legacy_still_refuses(self):
        legacy = dict(config.DEFAULTS, destination='s3', s3_bucket='fixture', plaintext_offsite_acknowledged=True)
        config.normalize(legacy)
        self.assertFalse(legacy['s3_encryption'])
        self.assertTrue(legacy['keep_local'])
        self.validate(legacy)
        legacy['plaintext_offsite_acknowledged'] = False
        config.normalize(legacy)
        with self.assertRaisesRegex(BackupError, 'plaintext-offsite'):
            self.validate(legacy)

    def test_weekdays_times_hours_counts_and_versions_refuse_invalid_rows(self):
        invalid = ({'settings_version': 3}, {'full_schedule': 'hourly'}, {'full_schedule': 'weekly', 'full_weekdays': []},
                   {'database_schedule': 'weekly', 'database_weekdays': [7]}, {'database_time': '2:00'},
                   {'database_schedule': 'hourly', 'database_every_hours': 5}, {'database_schedule': 'hourly', 'database_minute': 60},
                   {'retention_keep_full': 0}, {'s3_retention_policy_database': 'other'}, {'gpg': 'relative/gpg'})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(BackupError):
                value = dict(self.settings, **changes)
                config.normalize(value)
                self.validate(value)

    def test_direct_database_config_init_and_schedule_do_not_acquire_launcher_locks(self):
        self.envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(self.state.root))
        for lock_name in ('scheduler.lock', 'queue.lock'):
            for command, payload in ((['run', '--execute', '--mode', 'database-online'], None),
                                     (['config', 'set'], {'settings': {'retention_keep_database': 31}}),
                                     (['init'], None),
                                     (['schedule', 'disable', '--kind', 'database'], None)):
                with self.subTest(lock=lock_name, command=command):
                    args = cli.parser().parse_args(['--app', str(self.app), '--private-root', str(self.state.root),
                                                    '--php', '/usr/bin/php', *command])
                    def publish(arguments, input_bytes, descriptors):
                        self.settings.update(json.loads(input_bytes))
                        config.normalize(self.settings)
                        output = json.dumps({'ok': True, 'receipt': {}, 'snapshot': copy.deepcopy(self.settings)}).encode()
                        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b'')
                    with (self.state.root / lock_name).open('r+b') as held:
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        with patch.object(cli, 'bridge', side_effect=lambda *a, **k: copy.deepcopy(self.envelope)), \
                             patch.object(config, 'bridge', side_effect=lambda *a, **k: copy.deepcopy(self.envelope)), \
                             patch.object(cli, 'stdin_json', return_value=payload), \
                             patch.object(tools, 'checks', side_effect=self.tool_reports), \
                             patch.object(cli, 'run_native_writer', side_effect=publish), \
                             patch.object(cli, 'capture', return_value={'run_id': 'stubbed-database', 'archive_path': '/fixture'}) as capture:
                            result = cli.dispatch(args)
                        if command[0] == 'run':
                            self.assertEqual(result['run_id'], 'stubbed-database')
                            self.assertEqual(capture.call_args.args[1]['settings']['mode'], 'database-online')
                        else:
                            capture.assert_not_called()
                            self.assertTrue(result['initialized'] if command[0] == 'init' else result['updated'])
                            self.assertFalse(result.get('partial', False))
                        with (self.state.root / lock_name).open('r+b') as contender:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_credential_free_check_init_and_path_clear_use_authoritative_reads(self):
        self.envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(self.state.root))
        calls = []
        def bridge(args, verb='read', payload=None):
            calls.append(verb)
            if verb == 'read':
                raise BackupError('credentials-unreadable')
            return copy.deepcopy(self.envelope)
        args = cli.parser().parse_args(['--app', str(self.app), '--private-root', str(self.state.root), '--php', '/usr/bin/php', 'init'])
        with patch.object(cli, 'bridge', side_effect=bridge), patch.object(config, 'bridge', side_effect=bridge), patch.object(tools, 'checks', side_effect=self.tool_reports):
            self.assertTrue(cli.dispatch(args)['initialized'])
            self.assertTrue(cli.check_config(args, self.state, {'settings': {'dump': ''}})['valid'])
        self.assertTrue(calls)
        self.assertEqual(set(calls), {'read-settings'})
        value = copy.deepcopy(self.envelope)
        value['tools']['dump'] = '/broken/old-pin'
        cli.override_tools(value, {'dump': '', 'php': ''}, '/usr/bin/php')
        self.assertEqual(value['tools']['dump'], '')
        self.assertEqual(value['tools']['php'], '/usr/bin/php')


    def test_legacy_overview_keeps_total_pending_and_adds_per_kind_counts(self):
        self.settings['settings_version'] = 1
        self.settings.update(retention_keep=1, cooperative_writers_acknowledged=False)
        config.normalize(self.settings)
        self.backup('old', created_at=1)
        self.backup('new', created_at=2)
        value = presentation.overview(self.state, self.envelope)['configuration']['retention']['pending']
        self.assertEqual((value['local'], value['s3']), (1, 0))
        self.assertEqual(value['database'], {'local': 1, 's3': 0})
        self.assertEqual(value['full'], {'local': 0, 's3': 0})

    def test_v2_directory_preserves_legacy_binding_and_counted_paths(self):
        binding = read_json(self.state.binding)
        binding.pop('default_local_directory')
        atomic_json(self.state.binding, binding)
        legacy = State(self.app, self.state.root)
        before = legacy.binding.read_bytes()
        self.assertEqual(engine.destination(legacy, self.envelope), self.state.root / 'backups')
        self.assertEqual(legacy.binding.read_bytes(), before)
        self.assertEqual(legacy.default_destination(), self.state.root / 'artifacts')


class SchedulerTests(PrivateFixture):
    def enable(self):
        self.settings.update(full_schedule='daily', full_time='03:00', database_schedule='daily', database_time='03:00')
        config.normalize(self.settings)
        requests.sync_schedule(self.state, self.settings, now=instant('2026-10-01T00:00:00+00:00'))

    def test_same_minute_distinct_ids_modes_epochs_and_deduplication(self):
        self.enable()
        at = instant('2026-10-02T03:00:00+00:00')
        launched = self.tick(at)
        items = requests.list_requests(self.state)
        self.assertEqual(len(items), 2)
        self.assertEqual({item['schedule_kind'] for item in items}, {'full', 'database'})
        self.assertEqual({item['arguments']['mode'] for item in items}, {'full-quiesced', 'database-online'})
        self.assertEqual(next(item for item in items if item['request_id'] == launched['launched'][0])['schedule_kind'], 'full')
        self.tick(at + 1)
        self.assertEqual(len(requests.list_requests(self.state)), 2)

    def test_edit_disable_and_timezone_change_have_independent_epochs(self):
        self.enable()
        before = read_json(self.state.root / 'schedule.json')
        self.settings['database_time'] = '04:00'
        requests.sync_schedule(self.state, self.settings, now=instant('2026-10-02T00:00:00+00:00'))
        after = read_json(self.state.root / 'schedule.json')
        self.assertEqual(after['kinds']['full'], before['kinds']['full'])
        self.assertNotEqual(after['kinds']['database'], before['kinds']['database'])
        self.settings['retention_keep_full'] = 3
        requests.sync_schedule(self.state, self.settings, now=instant('2026-10-02T01:00:00+00:00'))
        self.assertEqual(read_json(self.state.root / 'schedule.json'), after)
        self.settings['schedule_timezone'] = 'Europe/Berlin'
        requests.sync_schedule(self.state, self.settings, now=instant('2026-10-02T02:00:00+00:00'))
        changed = read_json(self.state.root / 'schedule.json')
        self.assertTrue(all(changed['kinds'][kind] != after['kinds'][kind] for kind in ('full', 'database')))

    def test_edit_one_cancels_only_its_waiting_request(self):
        self.enable()
        at = instant('2026-10-02T03:00:00+00:00')
        self.tick(at)
        for item in requests.list_requests(self.state):
            item.update(status='deferred', retry_at=at + 1000)
            atomic_json(requests.path(self.state, item['request_id']), item)
        self.settings['full_schedule'] = 'off'
        requests.sync_schedule(self.state, self.settings, now=at + 1)
        self.tick(at + 2)
        items = {item['schedule_kind']: item for item in requests.list_requests(self.state)}
        self.assertEqual(items['full']['status'], 'cancelled')
        self.assertEqual(items['database']['status'], 'deferred')

    def test_captured_delivery_survives_worker_exit_schedule_disable_and_age_limit(self):
        self.remote()
        self.enable()
        at = instant('2026-10-02T03:00:00+00:00')
        request_id = self.tick(at)['launched'][0]
        self.backup('captured', 'full-quiesced', created_at=at)
        journal = self.item('captured')
        journal.update(request_id=request_id, delivery_required=True,
                       delivery_scope=storage.remote_scope(self.state, self.envelope))
        self.state.save(journal)
        item = read_json(requests.path(self.state, request_id))
        item.update(status='running', worker=None)
        atomic_json(requests.path(self.state, request_id), item)
        self.settings.update(full_schedule='off', database_schedule='off', request_max_age=1)
        later = at + 86400
        requests.sync_schedule(self.state, self.settings, now=later)
        self.assertEqual(self.tick(later)['launched'], [request_id])
        resumed = read_json(requests.path(self.state, request_id))
        self.assertEqual((resumed['action'], resumed['arguments']), ('deliver', {'run_id': 'captured'}))
        self.assertEqual(resumed['delivery_scope'], journal['delivery_scope'])
        # Storage edits cannot let retention discard an upload still owed.
        self.backup('newer', 'full-quiesced', created_at=later)
        self.settings.update(destination='local', upload_s3=False, retention_keep_full=1)
        self.assertNotIn('captured', storage.retention(self.state, self.envelope)['local_delete'])
        resumed.update(status='deferred')
        atomic_json(requests.path(self.state, request_id), resumed)
        requests.cancel(self.state, request_id)
        self.assertFalse(self.item('captured')['delivery_required'])
        self.assertIn('captured', storage.retention(self.state, self.envelope)['local_delete'])

    def test_every_supported_hour_interval_is_wall_clock_anchored(self):
        at = instant('2026-10-02T23:59:00+00:00')
        for hours in (1, 2, 3, 4, 6, 8, 12):
            with self.subTest(hours=hours):
                value = settings(database_schedule='hourly', database_every_hours=hours, database_minute=17)
                slots = list(requests.due_slots(value, at, since=instant('2026-10-02T00:00:00+00:00'), window_seconds=86400, kind='database'))
                self.assertEqual(len(slots), 24 // hours)
                self.assertTrue(all(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).hour % hours == 0 and datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).minute == 17 for _, ts in slots))

    def test_hourly_dst_gap_fold_and_next_slot_share_rules(self):
        value = settings(database_schedule='hourly', database_minute=30, schedule_timezone='Europe/Berlin')
        gap = list(requests.due_slots(value, instant('2026-03-29T04:00:00+00:00'), since=instant('2026-03-28T23:00:00+00:00'), window_seconds=86400, kind='database'))
        self.assertFalse(any('T02:30' in slot for slot, _ in gap))
        fold = list(requests.due_slots(value, instant('2026-10-25T03:00:00+00:00'), since=instant('2026-10-24T22:00:00+00:00'), window_seconds=86400, kind='database'))
        self.assertEqual(sum('T02:30' in slot for slot, _ in fold), 1)
        self.assertEqual(requests.next_slot(value, instant('2026-10-25T00:40:00+00:00'), kind='database'), instant('2026-10-25T02:30:00+00:00'))

    def test_exact_minute_enable_waits_and_outage_has_one_fresh_request_per_kind(self):
        self.settings.update(full_schedule='daily', full_time='03:00', database_schedule='hourly')
        config.normalize(self.settings)
        at = instant('2026-10-02T03:00:00+00:00')
        requests.sync_schedule(self.state, self.settings, now=at)
        self.tick(at)
        self.assertEqual(requests.list_requests(self.state), [])
        self.tick(at + 5 * 86400)
        items = requests.list_requests(self.state)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item['created_at'] == at + 5 * 86400 for item in items))

    def test_deferred_full_and_database_alternate_without_starvation(self):
        self.enable()
        at = instant('2026-10-02T03:00:00+00:00')
        first = self.tick(at)['launched'][0]
        item = read_json(requests.path(self.state, first))
        item.update(status='deferred', retry_at=at + 60)
        atomic_json(requests.path(self.state, first), item)
        second = self.tick(at + 1)['launched'][0]
        self.assertNotEqual(first, second)
        item = read_json(requests.path(self.state, second))
        item.update(status='deferred', retry_at=at + 60)
        atomic_json(requests.path(self.state, second), item)
        self.assertEqual(self.tick(at + 61)['launched'], [first])


    def test_new_database_requests_do_not_starve_older_deferred_full(self):
        self.enable()
        at = instant('2026-10-02T03:00:00+00:00')
        full = self.tick(at)['launched'][0]
        item = read_json(requests.path(self.state, full))
        item.update(status='deferred', retry_at=at + 60)
        atomic_json(requests.path(self.state, full), item)
        database = self.tick(at + 1)['launched'][0]
        item = read_json(requests.path(self.state, database))
        item['status'] = 'complete'
        atomic_json(requests.path(self.state, database), item)
        with patch.object(requests.time, 'time', return_value=at + 60):
            requests.create(self.state, arguments={'mode': 'database-online'})
        self.assertEqual(self.tick(at + 61)['launched'], [full])

    def scheduled_database(self, slot_at):
        return requests.create(self.state, arguments={'mode': 'database-online'}, slot='database:slot-' + str(slot_at),
                               scheduled_metadata={'schedule_kind': 'database', 'slot_at': slot_at, 'schedule_fingerprint': 'test', 'schedule_since': 0})

    def test_available_full_coverage_survives_delivery_error_and_restart(self):
        request = self.scheduled_database(100)
        manual = requests.create(self.state, arguments={'mode': 'database-online'})
        self.backup('full', 'full-quiesced')
        item = self.item('full')
        item.update(database_snapshot_lower_bound=101, database_coverage={'snapshot_lower_bound': 101, 'verified_at': 102}, delivery_error='s3-delivery-failed', verification=None)
        self.state.save(item)
        requests.reconcile_coverage(State(self.app, self.state.root, initialize=False))
        saved = read_json(requests.path(self.state, request['request_id']))
        self.assertEqual((saved['status'], saved['covered_by']), ('satisfied', 'full'))
        self.assertEqual(read_json(requests.path(self.state, manual['request_id']))['status'], 'pending')

    def coverage(self, run_id, lower_bound):
        item = self.item(run_id)
        item.update(database_snapshot_lower_bound=lower_bound,
                    database_coverage={'snapshot_lower_bound': lower_bound, 'verified_at': lower_bound + 1})
        self.state.save(item)

    def test_outage_deleted_covering_full_does_not_absorb_resumed_database_slot(self):
        slot = instant('2026-10-02T03:00:00+00:00')
        self.settings.update(database_schedule='daily', database_time='03:00')
        config.normalize(self.settings)
        requests.sync_schedule(self.state, self.settings, now=slot - 3600)
        self.backup('older-full', 'full-quiesced', slot - 86400)
        self.coverage('older-full', slot - 86400)
        self.backup('outage-full', 'full-quiesced', slot + 60)
        self.coverage('outage-full', slot + 60)
        # No tick has created the 03:00 obligation during the outage. Manual
        # deletion at 03:05 is legal because an older full generation survives.
        with patch.object(storage.time, 'time', return_value=slot + 300):
            storage.delete_run(self.state, self.envelope, 'outage-full', location='local')
        self.assertTrue(self.item('outage-full')['retained_deleted'])
        launched = self.tick(slot + 600)['launched']
        resumed = requests.list_requests(self.state)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]['slot_at'], slot)
        self.assertEqual(resumed[0]['status'], 'launching')
        self.assertEqual(launched, [resumed[0]['request_id']])
        self.assertNotIn('covered_by', resumed[0])

    def test_satisfied_receipt_survives_covering_generation_deletion(self):
        request = self.scheduled_database(100)
        self.backup('older-full', 'full-quiesced')
        self.backup('covering-full', 'full-quiesced', 2)
        self.coverage('covering-full', 101)
        requests.reconcile_coverage(self.state, self.envelope)
        receipt = read_json(requests.path(self.state, request['request_id']))
        self.assertEqual(receipt['status'], 'satisfied')
        storage.delete_run(self.state, self.envelope, 'covering-full', location='local')
        requests.reconcile_coverage(State(self.app, self.state.root, initialize=False), self.envelope)
        self.assertEqual(read_json(requests.path(self.state, request['request_id'])), receipt)

    def test_coverage_requires_current_bytes_and_defers_during_deletion_lock(self):
        request = self.scheduled_database(100)
        archive, _ = self.backup('full', 'full-quiesced')
        self.coverage('full', 101)
        with self.state.lock():
            requests.reconcile_coverage(self.state, self.envelope)
            self.assertEqual(read_json(requests.path(self.state, request['request_id']))['status'], 'pending')
        original = archive.read_bytes()
        archive.write_bytes(b'x' * len(original))
        requests.reconcile_coverage(self.state, self.envelope)
        self.assertEqual(read_json(requests.path(self.state, request['request_id']))['status'], 'pending')
        archive.write_bytes(original)
        with self.state.lock():
            requests.reconcile_coverage(self.state, self.envelope, locked=True)
        self.assertEqual(read_json(requests.path(self.state, request['request_id']))['status'], 'satisfied')

    def test_remote_only_coverage_requires_current_complete_remote_proof(self):
        self.remote(keep_local=False)
        self.backup('remote-full', 'full-quiesced', deliver=True)
        self.coverage('remote-full', 101)
        self.assertFalse(self.item('remote-full')['local_complete'])
        request = self.scheduled_database(100)
        requests.reconcile_coverage(self.state)
        self.assertEqual(read_json(requests.path(self.state, request['request_id']))['status'], 'pending')
        remote = self.item('remote-full')['remote']
        key = (remote['bucket'], remote['archive_key'])
        saved = self.store.objects.pop(key)
        requests.reconcile_coverage(self.state, self.envelope)
        self.assertEqual(read_json(requests.path(self.state, request['request_id']))['status'], 'pending')
        self.store.objects[key] = saved
        requests.reconcile_coverage(self.state, self.envelope)
        self.assertEqual(read_json(requests.path(self.state, request['request_id']))['status'], 'satisfied')

    def test_tick_uses_current_local_verification_and_launches_without_expensive_proof(self):
        slot = instant('2026-10-02T03:00:00+00:00')
        self.settings.update(database_schedule='daily', database_time='03:00')
        config.normalize(self.settings)
        requests.sync_schedule(self.state, self.settings, now=slot - 3600)
        self.backup('current-full', 'full-quiesced')
        self.coverage('current-full', slot + 60)
        with patch.object(engine, 'verify', side_effect=AssertionError('tick cannot hash archives')), patch.object(storage, 'checked_remote', side_effect=AssertionError('tick cannot contact storage')):
            self.assertEqual(self.tick(slot + 600)['launched'], [])
        receipt = requests.list_requests(self.state)[0]
        self.assertEqual((receipt['status'], receipt['covered_by']), ('satisfied', 'current-full'))
        # On the next day's outage, historical remote delivery and stale local
        # verification must not make the bounded tick wait for expensive proof.
        self.coverage('current-full', slot + 86400 + 60)
        item = self.item('current-full')
        item.update(verification=None, remote_delivered=True)
        self.state.save(item)
        with patch.object(engine, 'verify', side_effect=AssertionError('tick cannot hash archives')), patch.object(storage, 'checked_remote', side_effect=AssertionError('tick cannot contact storage')):
            launched = self.tick(slot + 86400 + 600)['launched']
        fresh = next(item for item in requests.list_requests(self.state) if item['slot_at'] == slot + 86400)
        self.assertEqual(launched, [fresh['request_id']])
        self.assertEqual(fresh['status'], 'launching')
        fresh['status'] = 'complete'
        atomic_json(requests.path(self.state, fresh['request_id']), fresh)
        self.coverage('current-full', slot + 2 * 86400 + 60)
        item = self.item('current-full')
        item['local_complete'] = False
        self.state.save(item)
        with patch.object(engine, 'verify', side_effect=AssertionError('tick cannot hash archives')), patch.object(storage, 'checked_remote', side_effect=AssertionError('tick cannot contact storage')):
            launched = self.tick(slot + 2 * 86400 + 600)['launched']
        remote_only = next(item for item in requests.list_requests(self.state) if item['slot_at'] == slot + 2 * 86400)
        self.assertEqual(launched, [remote_only['request_id']])
        self.assertEqual(remote_only['status'], 'launching')


    def test_worker_delivery_failure_cannot_undo_successful_full_coverage(self):
        self.remote()
        self.settings['automatic_retention'] = False
        waiting = self.scheduled_database(100)
        self.backup('covered-full', 'full-quiesced')
        journal = self.item('covered-full')
        journal.update(database_snapshot_lower_bound=101, database_coverage={'snapshot_lower_bound': 101, 'verified_at': 102})
        self.state.save(journal)
        full = requests.create(self.state, arguments={'mode': 'full-quiesced'})
        full['status'] = 'launching'
        atomic_json(requests.path(self.state, full['request_id']), full)
        self.store.fail_put = True
        with patch.object(engine, 'capture', return_value=journal):
            result = requests.worker(self.state, self.envelope, full['request_id'])
        self.assertEqual(result['status'], 'deferred')
        covered = read_json(requests.path(self.state, waiting['request_id']))
        self.assertEqual((covered['status'], covered['covered_by']), ('satisfied', 'covered-full'))
        self.assertTrue(self.item('covered-full')['local_complete'])

    def test_failed_unrecovered_or_pre_slot_full_does_not_cover(self):
        request = self.scheduled_database(100)
        self.backup('full', 'full-quiesced')
        for changes in ({'database_snapshot_lower_bound': 99, 'database_coverage': {'snapshot_lower_bound': 99}},
                        {'database_snapshot_lower_bound': 101, 'capture_complete': False, 'database_coverage': {'snapshot_lower_bound': 101}},
                        {'database_snapshot_lower_bound': 101, 'capture_complete': True, 'service_recovered': False, 'database_coverage': {'snapshot_lower_bound': 101}}):
            item = self.item('full')
            item.update(changes)
            self.state.save(item)
            requests.reconcile_coverage(self.state)
            self.assertEqual(read_json(requests.path(self.state, request['request_id']))['status'], 'pending')

    def test_legacy_mode_less_requests_freeze_before_default_changes(self):
        item = requests.create(self.state)
        requests.freeze_legacy_requests(self.state, 'database-online')
        self.assertEqual(read_json(requests.path(self.state, item['request_id']))['arguments']['mode'], 'database-online')

    def test_v2_cli_kind_selection_and_unqualified_disable(self):
        self.envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(self.state.root))
        def load(*args, **kwargs):
            return copy.deepcopy(self.envelope)
        def command(verb, kind=None):
            argv = ['--app', str(self.app), '--private-root', str(self.state.root), 'schedule', verb]
            if kind:
                argv += ['--kind', kind]
            return cli.parser().parse_args(argv)
        with patch.object(cli, 'bridge', side_effect=load), patch.object(cli, 'load', side_effect=load), patch.object(cli, 'set_config', return_value={'updated': True}) as save:
            with self.assertRaisesRegex(BackupError, 'schedule-kind-required'):
                cli.dispatch(command('enable'))
            cli.dispatch(command('enable', 'database'))
            self.assertEqual(save.call_args.args[2], {'settings': {'database_schedule': 'hourly'}})
            cli.dispatch(command('disable'))
            self.assertEqual(save.call_args.args[2], {'settings': {'full_schedule': 'off', 'database_schedule': 'off'}})
            self.enable()
            with self.assertRaisesRegex(BackupError, 'schedule-kind-required'):
                cli.dispatch(command('enable'))
            shown = cli.dispatch(command('show', 'full'))
            self.assertEqual(set(shown['schedules']), {'full'})

    def test_outage_request_expiry_uses_creation_and_not_old_slot(self):
        self.enable()
        at = instant('2026-10-06T02:00:00+00:00')
        self.settings['request_max_age'] = 100
        self.tick(at)
        for item in requests.list_requests(self.state):
            self.assertEqual(item['created_at'], at)
            item.update(status='deferred', retry_at=at + 1000)
            atomic_json(requests.path(self.state, item['request_id']), item)
        self.tick(at + 101)
        self.assertTrue(all(item['status'] == 'expired' for item in requests.list_requests(self.state)))


class StorageTests(PrivateFixture):
    def test_mixed_history_keeps_verified_same_type_sets_and_unknown_mode(self):
        self.settings.update(retention_keep_full=1, retention_keep_database=2)
        for name, kind, ts in (('f1', 'full-quiesced', 1), ('d1', 'database-online', 2), ('f2', 'full-quiesced', 3), ('d2', 'database-online', 4), ('d3', 'database-online', 5), ('unknown', 'database-online', 6)):
            self.backup(name, kind, ts)
        item = self.item('unknown')
        item['mode'] = 'unknown'
        self.state.save(item)
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(set(result['removed']['local']), {'f1', 'd1'})
        self.assertTrue(self.item('unknown')['local_complete'])
        self.assertTrue(any(item['code'] == 'retention-mode-unknown' for item in result['skipped']))

    def test_manual_delete_requires_same_type_survivor_at_selected_location(self):
        self.backup('full', 'full-quiesced')
        self.backup('database', 'database-online', 2)
        self.assertEqual(storage.delete_plan(self.state, self.envelope, 'full', location='local')['refusal'], 'delete-last-backup')
        with self.assertRaisesRegex(BackupError, 'delete-last-backup'):
            storage.delete_run(self.state, self.envelope, 'full', location='local')

    def test_inconsistent_mode_cannot_authorize_deletion(self):
        self.backup('full', 'full-quiesced')
        self.backup('fake-full', 'database-online', 2)
        item = self.item('fake-full')
        item['mode'] = 'full-quiesced'
        self.state.save(item)
        self.assertEqual(storage.delete_plan(self.state, self.envelope, 'full', location='local')['refusal'], 'delete-last-backup')
        self.assertEqual(storage.delete_plan(self.state, self.envelope, 'fake-full')['refusal'], 'delete-mode-unknown')

    def test_s3_only_removes_last_local_staging_after_content_proof_and_retry(self):
        self.remote(keep_local=False)
        archive, manifest = self.backup('only')
        result = storage.deliver(self.state, self.envelope, 'only')
        self.assertFalse(result['local_complete'])
        self.assertFalse(archive.exists())
        self.assertFalse(manifest.exists())
        self.assertFalse(self.item('only').get('retained_deleted'))
        trace_at = len(self.store.trace)
        result = storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(result['already_delivered'])
        self.assertTrue(any(op == 'get' for op, _, _ in self.store.trace[trace_at:]))
        entry = presentation.overview(self.state, self.envelope)['runs'][0]
        self.assertEqual((entry['local'], entry['download']), (False, 'retrieve'))

    def test_upload_manifest_and_readback_failures_preserve_staging(self):
        self.remote(keep_local=False)
        archive, _ = self.backup('only')
        self.store.fail_put = True
        with self.assertRaises(BackupError):
            storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        self.store.fail_put = False
        with patch.object(storage, 'whole_hash', return_value=('0' * 64, 0)), self.assertRaisesRegex(BackupError, 's3-readback-mismatch'):
            storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        with patch.object(storage, 'staging_removal', side_effect=BackupError('controlled-stop')):
            with self.assertRaisesRegex(BackupError, 'controlled-stop'):
                storage.deliver(self.state, self.envelope, 'only')
        key = ('fixture', storage.prefix(self.state, self.envelope) + 'only.manifest.json')
        self.store.objects[key] = b'wrong remote manifest'
        with self.assertRaises(BackupError):
            with self.state.lock():
                storage.staging_removal(self.state, self.envelope, self.item('only'))
        self.assertTrue(archive.exists())

    def delivered_with_local(self, run_id='only'):
        self.remote()
        files = self.backup(run_id, deliver=True)
        self.settings['keep_local'] = False
        return files

    def test_crash_between_unlinks_resumes_with_new_remote_proof(self):
        archive, manifest = self.delivered_with_local()
        real = artifacts.remove_owned
        def interrupted(location, names, seen):
            real(location, [names[0]], seen)
            raise OSError('synthetic crash between unlinks')
        with patch.object(artifacts, 'remove_owned', side_effect=interrupted), self.assertRaisesRegex(BackupError, 'staging-removal-local-delete-failed'):
            storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        self.assertFalse(manifest.exists())
        self.assertTrue(self.item('only')['staging_removal_intent'])
        result = storage.deliver(self.state, self.envelope, 'only')
        self.assertFalse(result['local_complete'])
        self.assertFalse(archive.exists())
        self.assertNotIn('staging_removal_intent', self.item('only'))


    def test_crashes_before_intent_and_before_final_receipt_retry_safely(self):
        self.remote()
        for run_id, before_intent in (('before-intent', True), ('before-receipt', False)):
            archive, manifest = self.backup(run_id, deliver=True)
            self.settings['keep_local'] = False
            real = self.state.save
            def crash(item):
                if item['run_id'] == run_id and ((before_intent and item.get('staging_removal_intent')) or (not before_intent and item.get('local_complete') is False)):
                    raise OSError('controlled persistence interruption')
                return real(item)
            with patch.object(self.state, 'save', side_effect=crash), self.assertRaises(OSError):
                storage.deliver(self.state, self.envelope, run_id)
            self.assertEqual(archive.exists(), before_intent)
            self.assertEqual(manifest.exists(), before_intent)
            storage.deliver(self.state, self.envelope, run_id)
            self.assertFalse(self.item(run_id)['local_complete'])
            self.assertTrue(self.item(run_id)['remote_delivered'])
            self.settings['keep_local'] = True

    def test_damaged_newest_copy_does_not_consume_another_types_survivor(self):
        self.settings.update(retention_keep_full=1, retention_keep_database=1)
        self.backup('old-full', 'full-quiesced', 1)
        newest, _ = self.backup('bad-full', 'full-quiesced', 2)
        self.backup('database', 'database-online', 3)
        newest.write_bytes(b'damaged')
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(result['removed']['local'], [])
        self.assertTrue(self.item('old-full')['local_complete'])
        self.assertEqual(set(result['keep']), {'old-full', 'database'})
        self.assertEqual([item['run_id'] for item in result['damaged']], ['bad-full'])
        self.assertTrue(newest.exists())

    def test_replaced_files_refuse_unlink_and_durable_intent_survives(self):
        archive, _ = self.delivered_with_local()
        real = artifacts.remove_owned
        def substitute(location, names, seen):
            replacement = archive.with_suffix('.replacement')
            replacement.write_bytes(archive.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, archive)
            return real(location, names, seen)
        with patch.object(artifacts, 'remove_owned', side_effect=substitute), self.assertRaisesRegex(BackupError, 'staging-removal-local-verification-failed'):
            storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        self.assertTrue(self.item('only')['staging_removal_intent'])

    def test_live_lease_survives_retry_then_expired_retrieval_is_cleaned(self):
        archive, _ = self.delivered_with_local()
        item = self.item('only')
        item['local_download_until'] = time.time() + 1000
        self.state.save(item)
        storage.deliver(self.state, self.envelope, 'only')
        storage.retention(self.state, self.envelope, execute=True)
        self.assertTrue(archive.exists())
        item = self.item('only')
        item['local_download_until'] = time.time() - 1
        self.state.save(item)
        storage.retention(self.state, self.envelope, execute=True)
        self.assertFalse(archive.exists())
        self.assertTrue(self.item('only')['remote_delivered'])

    def test_changed_provider_or_remote_disappearance_keeps_local_staging(self):
        archive, _ = self.delivered_with_local()
        self.settings['s3_endpoint'] = 'https://changed.invalid'
        with self.assertRaises(BackupError):
            storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        self.settings['s3_endpoint'] = 'https://fixture.invalid'
        self.store.objects.clear()
        with self.state.lock(), self.assertRaises(BackupError):
            storage.staging_removal(self.state, self.envelope, self.item('only'))
        self.assertTrue(archive.exists())

    def test_per_type_newest_remote_protection_and_local_count_without_local_kept(self):
        self.remote(keep_local=False, retention_keep_full=1, retention_keep_database=1)
        for name, mode, at in (('f1', 'full-quiesced', 1), ('d1', 'database-online', 2), ('f2', 'full-quiesced', 3), ('d2', 'database-online', 4)):
            self.backup(name, mode, at, deliver=True)
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(set(result['removed']['s3']), {'f1', 'd1'})
        self.assertTrue(self.item('f2')['remote_delivered'])
        self.assertTrue(self.item('d2')['remote_delivered'])
        self.assertEqual(storage.delete_plan(self.state, self.envelope, 'f2', location='s3')['refusal'], 'delete-last-s3-backup')

    def test_encrypted_policy_has_no_plaintext_fallback_without_a_key(self):
        self.remote(s3_encryption=True)
        archive, _ = self.backup('only')
        with self.assertRaisesRegex(BackupError, 'archive-key-required'):
            storage.deliver(self.state, self.envelope, 'only')
        self.assertEqual(self.store.trace, [])
        self.assertTrue(archive.exists())


class DiscoveryTests(PrivateFixture):
    def test_cron_escaping_is_shared_by_discovery_and_legacy_lock_validation(self):
        from paymenter_backups.maintenance import _legacy_cron_lock
        app = self.base / 'app%site'
        lock = str(self.base / 'private%archive' / 'scheduler.lock')
        php = str(self.base / 'hosting%php' / 'php')
        command = f"/usr/bin/flock -n '{lock}' '{php}' '{app}/artisan' schedule:run"
        line = '* * * * * ' + command.replace('%', r'\%')
        self.assertEqual(_legacy_cron_lock(line, app, [lock]), lock)
        self.assertTrue(discovery.relevant(line, app))
        self.assertFalse(discovery.relevant(line, self.app))
        self.assertEqual(discovery.parse(line, app)['lock'], lock)
        self.assertEqual(discovery.parse(command, app, cron=False)['lock'], lock)
        # Shell quotes do not protect cron's stdin delimiter, and an even number
        # of backslashes does not escape it either.
        for invalid in ('* * * * * ' + command, line.replace(r'\%', r'\\%')):
            with self.subTest(line=invalid):
                with self.assertRaisesRegex(BackupError, 'job-command-unsupported'):
                    discovery.parse(invalid, app)
                with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
                    _legacy_cron_lock(invalid, app, [lock])
                self.assertTrue(discovery.relevant(invalid, self.app))
                self.assertEqual(discovery.record(invalid, app, 'crontab', 'line 1')['command'], '')

    def test_suggested_cron_jobs_preserve_percent_and_backslash_paths(self):
        # Cron processes escapes before the shell, without honoring shell quotes.
        def cron_command(line):
            def translate(match):
                value = match.group()
                self.assertNotEqual(value, '%', 'cron would split the command into stdin')
                return '%' if value == r'\%' else value
            return re.sub(r'\\.|%', translate, line.split(' ', 5)[5])

        for index, names in enumerate((('app%site', 'private', 'hosting'),
                                       ('app', 'private%archive', 'hosting'),
                                       ('app', 'private', 'hosting%php'),
                                       (r"app\%site'", r'private\\%archive', r'hosting\%php'),
                                       ('%app%', '%private%', '%hosting%'))):
            with self.subTest(names=names):
                base = self.base / str(index)
                app, private, hosting = (base / name for name in names)
                for directory in (app, private, hosting):
                    directory.mkdir(parents=True)
                php = hosting / 'php'
                receipt = hosting / 'arguments.json'
                php.write_text('#!' + sys.executable + '\nimport json, pathlib, sys\n'
                               "pathlib.Path(__file__).with_name('arguments.json').write_text(json.dumps(sys.argv[1:]))\n")
                php.chmod(0o700)
                state = SimpleNamespace(app=app, root=private)
                for kind, line in discovery.suggestions(state, {'tools': {'php': str(php)}}).items():
                    shell = cron_command(line)
                    result = subprocess.run(['/bin/sh', '-c', shell], check=False, timeout=5)
                    self.assertEqual(result.returncode, 0)
                    expected = [str(app / 'artisan'), 'schedule:run'] if kind == 'scheduler' else [str(app / 'artisan'), 'queue:work', '--stop-when-empty', '--max-time=50']
                    self.assertEqual(json.loads(receipt.read_text()), expected)
                    item = discovery.parse(line, app)
                    self.assertEqual(item['lock'], str(private / ('scheduler.lock' if kind == 'scheduler' else 'queue.lock')))
                    self.assertEqual(item['coordination'], 'coordinated')
                    self.assertEqual(discovery.parse(item['command'], app, cron=False), item)

    def test_every_emitted_suggestion_parses_and_init_locks_are_private(self):
        for key, line in discovery.suggestions(self.state, self.envelope).items():
            item = discovery.parse(line, self.app)
            self.assertEqual(item['coordination'], 'coordinated')
            self.assertEqual(item['kind'], 'scheduler' if key == 'scheduler' else 'queue-worker')
            self.assertTrue(discovery.lock_safe(item['lock']))

    def test_redirect_php_and_artisan_flag_variants(self):
        for basename in ('php', 'php8.3', 'php83', 'php-cli', 'ea-php83', 'lsphp83'):
            for redirect in ('>/dev/null', '> /dev/null', '>>/dev/null', '>> /dev/null', '>/dev/null 2>&1'):
                with self.subTest(basename=basename, redirect=redirect):
                    line = f'* * * * * cd {self.app} && /usr/bin/flock -w 5 -E 2 --timeout=3 {self.state.root}/scheduler.lock /opt/hosting/{basename} -q -d memory_limit=128M -f artisan --quiet --no-interaction schedule:run -n {redirect}'
                    parsed = discovery.parse(line, self.app)
                    self.assertEqual(parsed['coordination'], 'coordinated')
                    self.assertNotIn('memory_limit', parsed['command'])

    def test_expanded_locks_shell_wrappers_redirects_force_and_secret_canaries_refuse(self):
        valid = discovery.suggestions(self.state, self.envelope)['scheduler']
        invalid = (valid.replace('scheduler.lock', '$LOCK'), valid.replace(self.envelope['tools']['php'], '/opt/$SECRET/php'),
                   valid.replace(self.envelope['tools']['php'], '/opt/`secret`/php'), valid.replace('/dev/null', '/tmp/output'),
                   valid.replace('schedule:run', 'queue:work --force --password=secret-canary'),
                   valid + ' &', valid.replace('/usr/bin/flock', '/bin/sh -c /usr/bin/flock'))
        for line in invalid:
            with self.subTest(line=line), self.assertRaises(BackupError):
                discovery.parse(line, self.app)
            row = discovery.record(line, self.app, 'crontab', 'line 1')
            self.assertNotIn('secret-canary', json.dumps(row))
            self.assertEqual(row['command'], '')


    def test_legacy_queue_options_remain_readable_until_v2_adoption(self):
        from paymenter_backups.maintenance import _legacy_cron_lock
        lock = str(self.state.root / 'queue.lock')
        line = f'* * * * * /usr/bin/flock {lock} /usr/bin/php {self.app}/artisan queue:work --stop-when-empty --queue=high'
        self.assertEqual(_legacy_cron_lock(line, self.app, [lock]), lock)
        with self.assertRaises(BackupError):
            discovery.parse(line, self.app)

    def test_diagnostic_output_is_bounded_and_never_cached_raw(self):
        self.envelope['tools']['crontab'] = self.script('large-crontab', "print('secret-canary' * 30000)")
        value = discovery.discover(self.state, self.envelope)
        self.assertEqual(value['error'], 'job-discovery-incomplete')
        self.assertNotIn('secret-canary', json.dumps(value))

    def test_multiple_jobs_derive_union_and_explicit_mismatch_refuses(self):
        lines = discovery.suggestions(self.state, self.envelope)
        rows = [discovery.record(lines['scheduler'], self.app, 'crontab', 'line 1'),
                discovery.record(lines['scheduler'], self.app, 'crontab', 'line 2'),
                discovery.record(lines['queue'], self.app, 'crontab', 'line 3')]
        inventory = {'state': 'ok', 'error': None, 'items': rows}
        errors, locks = discovery.admission(self.envelope, inventory)
        self.assertEqual(errors, [])
        self.assertEqual(set(locks), {str(self.state.root / 'scheduler.lock'), str(self.state.root / 'queue.lock')})
        self.settings['job_lock_paths'] = [locks[0]]
        self.assertIn('job-lock-override-mismatch', [item['code'] for item in discovery.admission(self.envelope, inventory)[0]])

    def test_missing_scheduler_incomplete_unknown_stale_and_unsafe_lock_refuse(self):
        for state in ('unknown', 'stale', 'failed'):
            errors, _ = discovery.admission(self.envelope, {'state': state, 'error': None, 'items': []})
            self.assertIn('scheduler-missing', [item['code'] for item in errors])
            self.assertTrue(any(item['code'].startswith('job-discovery-') for item in errors))
        inventory = self.inventory()
        (self.state.root / 'scheduler.lock').chmod(0o644)
        self.assertIn('job-lock-unsafe', [item['code'] for item in discovery.admission(self.envelope, inventory)[0]])

    def test_cache_refresh_is_bounded_private_and_overview_never_rediscovers(self):
        with patch.object(discovery, 'discover', return_value=dict(self.inventory(), app=str(self.app), schema_version=1, parser_version=discovery.PARSER_VERSION)) as discover:
            discovery.refresh_cache(self.state, self.envelope)
            discovery.refresh_cache(self.state, self.envelope)
            discover.assert_called_once()
        self.assertEqual((self.state.root / 'discovery.json').stat().st_mode & 0o777, 0o600)
        with patch.object(discovery, 'discover', side_effect=AssertionError('overview must only read cache')):
            result = presentation.overview(self.state, self.envelope)
        self.assertEqual(result['configuration']['checks']['jobs']['state'], 'ok')
        value = read_json(self.state.root / 'discovery.json')
        self.assertEqual(discovery.cached(self.state, value['checked_at'] + 901)['state'], 'stale')

    def test_cache_from_before_cron_escape_validation_requires_rediscovery(self):
        old = dict(self.inventory(), app=str(self.app), schema_version=1, parser_version=1)
        atomic_json(self.state.root / 'discovery.json', old)
        self.assertEqual(discovery.cached(self.state)['state'], 'unknown')


    def test_other_absolute_application_jobs_are_not_claimed(self):
        line = '* * * * * /usr/bin/flock /other/private/job.lock /usr/bin/php /other/application/artisan schedule:run'
        self.assertFalse(discovery.relevant(line, self.app))
        self.assertTrue(discovery.relevant('* * * * * php artisan schedule:run', self.app))
        self.assertTrue(discovery.relevant('* * * * * sh -c "cd /other && php artisan schedule:run"', self.app))
        self.assertTrue(discovery.relevant('* * * * * php /other/artisan schedule:run; php ./artisan schedule:run', self.app))

    def test_discovery_failure_differs_from_no_crontab(self):
        self.envelope['tools']['crontab'] = '/missing/crontab'
        self.assertEqual(discovery.discover(self.state, self.envelope)['error'], 'job-discovery-failed')
        self.envelope['tools']['crontab'] = self.script('crontab', 'print("# no relevant jobs")')
        with patch.object(discovery, 'systemd', return_value=[]):
            value = discovery.discover(self.state, self.envelope)
        self.assertEqual((value['state'], value['items']), ('ok', []))
        self.assertIn('scheduler-missing', [item['code'] for item in discovery.admission(self.envelope, value)[0]])

    def test_actual_flock_exclusion_and_replacement_refuse(self):
        inventory = self.inventory()
        lock = self.state.root / 'scheduler.lock'
        self.settings['lock_timeout'] = 0
        with lock.open('r+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with patch.object(discovery, 'discover', return_value=inventory), self.assertRaisesRegex(BackupError, 'job-lock-busy'):
                with coordination(self.state, self.envelope):
                    self.fail('must not acquire a held launcher lock')
        real = fcntl.flock
        changed = False
        def replacing(descriptor, operation):
            nonlocal changed
            if not changed:
                replacement = lock.with_suffix('.new')
                replacement.touch(mode=0o600)
                os.replace(replacement, lock)
                changed = True
            return real(descriptor, operation)
        with patch.object(discovery, 'discover', return_value=inventory), patch('paymenter_backups.maintenance.fcntl.flock', side_effect=replacing), self.assertRaisesRegex(BackupError, 'job-lock-unsafe'):
            with coordination(self.state, self.envelope):
                pass

    def test_capture_admission_rediscovers_and_never_uses_stale_cache(self):
        self.settings['mode'] = 'full-quiesced'
        with patch.object(discovery, 'discover', return_value={'state': 'failed', 'error': 'job-discovery-incomplete', 'items': []}) as fresh, patch.object(discovery, 'cached', return_value=self.inventory()) as cache, patch('paymenter_backups.engine.billing_admission'), patch('paymenter_backups.engine.schedule_admission'):
            with self.assertRaisesRegex(BackupError, 'job-discovery-incomplete'):
                engine.capture(self.state, self.envelope)
            fresh.assert_called_once()
            cache.assert_not_called()
        self.assertEqual(self.state.journals(), [])

    def test_standalone_systemd_continuous_service_and_plain_fallback(self):
        self.envelope['tools']['systemctl'] = '/usr/bin/true'
        unit = 'paymenter-worker.service'
        command = f'/usr/bin/php {self.app}/artisan queue:work'
        seen = []
        def read(argv, timeout):
            seen.append(argv)
            if '--output=json' in argv:
                raise BackupError('child-failed')
            if 'list-timers' in argv:
                return b''
            if 'list-units' in argv:
                return (unit + ' loaded active running worker\n').encode()
            if 'list-unit-files' in argv:
                return (unit + ' enabled enabled\n').encode()
            return (f'Id={unit}\nUser={os.getuid()}\nWorkingDirectory={self.app}\nDynamicUser=no\nLoadState=loaded\nRestart=always\nExecStart={{ path=/usr/bin/php ; argv[]={command} ; ignore_errors=no ; }}\n').encode()
        with patch.object(discovery, 'run', side_effect=read):
            rows = discovery.systemd(self.app, self.envelope['tools'])
        self.assertEqual(rows[0]['coordination'], 'continuous')
        self.assertEqual(rows[0]['label'], unit)
        self.assertTrue(any('list-unit-files' in argv for argv in seen))
        self.assertTrue(all(not any(word in argv for word in ('start', 'stop', 'disable', 'enable')) for argv in seen))

    def test_restarting_finite_systemd_worker_stays_continuous_after_cache_round_trip(self):
        self.envelope['tools']['systemctl'] = sys.executable
        unit = 'paymenter-worker.service'
        command = f'/usr/bin/flock -n {self.state.root}/queue.lock /usr/bin/php {self.app}/artisan queue:work --stop-when-empty'

        def read(argv, timeout):
            if '--user' in argv or 'list-timers' in argv:
                return b'[]'
            if 'list-units' in argv:
                return json.dumps([{'unit': unit, 'active': 'active'}]).encode()
            if 'list-unit-files' in argv:
                return json.dumps([{'unit_file': unit, 'state': 'enabled'}]).encode()
            return (f'Id={unit}\nUser={os.getuid()}\nWorkingDirectory={self.app}\nDynamicUser=no\nLoadState=loaded\nRestart=always\n'
                    f'ExecStart={{ path=/usr/bin/flock ; argv[]={command} ; ignore_errors=no ; }}\n').encode()

        with patch.object(discovery, 'run', side_effect=read):
            rows = discovery.systemd(self.app, self.envelope['tools'])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['coordination'], rows[0]['reason']),
                         ('continuous', 'continuous-worker-present'))
        inventory = dict(self.inventory(), app=str(self.app), schema_version=1,
                         parser_version=discovery.PARSER_VERSION)
        inventory['items'].extend(rows)
        fresh_errors, fresh_locks = discovery.admission(self.envelope, inventory)
        self.assertIn('continuous-worker-present', [item['code'] for item in fresh_errors])
        atomic_json(self.state.root / 'discovery.json', inventory)
        current = discovery.cached(self.state)
        self.assertEqual(current['items'][-1]['reason'], 'continuous-worker-present')
        self.assertEqual(current['items'], inventory['items'])
        self.assertEqual(discovery.admission(self.envelope, current), (fresh_errors, fresh_locks))

    def test_legacy_coordination_and_discovery_admission_agree_on_restarting_finite_worker(self):
        unit = 'paymenter-worker.service'
        lock = self.state.root / 'queue.lock'
        lock.touch(mode=0o600, exist_ok=True)
        command = f'/usr/bin/flock -n {lock} /usr/bin/php {self.app}/artisan queue:work --stop-when-empty'
        self.envelope['tools']['systemctl'] = self.script('restart-aware-systemctl', "print('[]')")
        show = {
            'Id': unit,
            'ExecStart': f'{{ path=/usr/bin/flock ; argv[]={command} ; ignore_errors=no ; }}',
            'User': str(os.getuid()),
            'WorkingDirectory': str(self.app),
            'DynamicUser': 'no',
            'LoadState': 'loaded',
            'Restart': 'always',
        }

        def systemctl(arguments, timeout):
            if '--user' in arguments:
                return b'[]'
            if 'list-timers' in arguments:
                return json.dumps([{'activates': unit}]).encode()
            if 'list-units' in arguments:
                return json.dumps([{'unit': unit, 'active': 'active'}]).encode()
            if 'list-unit-files' in arguments:
                return json.dumps([{'unit_file': unit, 'state': 'enabled'}]).encode()
            if 'show' in arguments:
                requested = next(value.partition('=')[2].split(',') for value in arguments
                                 if value.startswith('--property='))
                return ('\n'.join(f'{name}={show[name]}' for name in requested) + '\n').encode()
            raise AssertionError(f'unexpected systemctl command: {arguments}')

        def maintenance_command(arguments, timeout):
            if arguments[0] == self.envelope['tools']['crontab']:
                return b'# no fixture jobs\n'
            return systemctl(arguments, timeout)

        with patch.object(discovery, 'run', side_effect=systemctl):
            rows = discovery.systemd(self.app, self.envelope['tools'])
        inventory = dict(self.inventory(), app=str(self.app), schema_version=1,
                         parser_version=discovery.PARSER_VERSION)
        inventory['items'].extend(rows)
        errors, _ = discovery.admission(self.envelope, inventory)
        self.assertIn('continuous-worker-present', [item['code'] for item in errors])

        legacy = dict(self.envelope, settings=dict(self.settings, settings_version=1,
                                                    job_lock_paths=[str(lock)]))
        with patch('paymenter_backups.maintenance.run', side_effect=maintenance_command), \
             patch('paymenter_backups.maintenance.app_workers', return_value=[]), \
             self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
            with coordination(self.state, legacy):
                self.fail('a restarting finite worker must not be admitted')


class ReadinessTests(PrivateFixture):
    def full_plan(self, inventory=None):
        self.settings['mode'] = 'full-quiesced'
        with patch('paymenter_backups.engine.billing_admission'), patch('paymenter_backups.engine.schedule_admission'), patch('paymenter_backups.process.continuous_workers', return_value=False):
            return engine.plan(self.state, self.envelope, inventory or self.inventory())

    def test_automatic_store_classification_and_both_confirmations(self):
        self.assertEqual(self.full_plan()['admission_errors'], [])
        for supported in (False, None):
            self.envelope['framework']['stores']['cache'] = {'driver': 'redis' if supported is False else 'custom', 'supported': supported}
            result = self.full_plan()
            detail = next(item['detail'] for item in result['readiness_items'] if item['code'] == 'external-durable-state-unsupported')
            self.assertEqual(detail['stores'][0]['kind'], 'cache')
            self.assertEqual(detail['stores'][0]['env'], 'CACHE_STORE')
        self.settings['no_concurrent_ddl_acknowledged'] = False
        for mode in ('database-online', 'full-quiesced'):
            self.settings['mode'] = mode
            self.assertIn('no-concurrent-ddl-acknowledgement-required', engine.plan(self.state, self.envelope, self.inventory())['admission_errors'])
        self.settings.update(no_concurrent_ddl_acknowledged=True, full_host_assumptions_acknowledged=False)
        self.assertIn('full-host-assumptions-acknowledgement-required', self.full_plan()['admission_errors'])
        self.settings['mode'] = 'database-online'
        self.assertNotIn('full-host-assumptions-acknowledgement-required', engine.plan(self.state, self.envelope)['admission_errors'])


    @unittest.skipUnless(PHP, 'PHP CLI required for effective store classifier')
    def test_php_effective_store_connections_and_inside_app_paths(self):
        (self.app / 'storage').mkdir()
        (self.app / 'outside-link').symlink_to(self.base)
        result = subprocess.run([PHP, str(ROOT / 'tests/integration/framework_store_checks.php'), str(self.app)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checks'], 10)

    @unittest.skipUnless(PHP, 'PHP CLI required for live scheduler detection')
    def test_real_continuous_scheduler_is_found_without_exposing_process_details(self):
        # A small owner process with a realistic PHP argv tests /proc matching.
        command = [PHP, '-r', 'echo "ready\\n"; flush(); sleep(20);', str(self.app / 'artisan'), 'schedule:work']
        child = subprocess.Popen(command, cwd=self.app, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            from paymenter_backups.process import app_workers, continuous_workers
            ready, _, _ = select.select([child.stdout], [], [], 3)
            self.assertTrue(ready, 'synthetic child must announce readiness')
            self.assertEqual(child.stdout.readline(), b'ready\n')
            deadline = time.monotonic() + 2
            while not continuous_workers(self.app) and time.monotonic() < deadline:
                self.assertIsNone(child.poll())
                time.sleep(0.01)
            self.assertIn(child.pid, app_workers(self.app))
            self.assertTrue(continuous_workers(self.app))
        finally:
            child.terminate()
            child.wait(timeout=3)
            child.stdout.close()

    def test_unknown_resolved_store_is_never_assumed_safe_in_v2(self):
        self.envelope['framework'].pop('stores')
        self.envelope['framework'].update(cache_driver='file', session_driver='file', queue_driver='database', filesystem_driver='local')
        result = self.full_plan()
        self.assertIn('external-durable-state-unsupported', result['admission_errors'])
        self.assertTrue(all(item['supported'] is None for item in stores(self.envelope)[1:]))

    def test_v1_preserves_old_store_allowlists_until_explicit_adoption(self):
        self.settings['settings_version'] = 1
        self.settings.update(cooperative_writers_acknowledged=True, external_state_acknowledged=True,
                             job_lock_paths=[str(self.state.root / 'scheduler.lock')])
        self.envelope['framework'].update(cache_driver='file', session_driver='file', queue_driver='database', filesystem_driver='local')
        self.envelope['framework']['stores']['cache']['supported'] = False
        self.assertNotIn('external-durable-state-unsupported', self.full_plan()['admission_errors'])

    def test_lock_contention_is_waiting_and_continuous_worker_is_setup(self):
        with (self.state.root / 'scheduler.lock').open('r+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.full_plan()
            self.assertEqual(result['admission_errors'], ['job-lock-busy'])
            self.assertEqual(state_for(result['readiness_items']), 'waiting')
        self.settings['mode'] = 'full-quiesced'
        with patch('paymenter_backups.engine.billing_admission'), patch('paymenter_backups.engine.schedule_admission'), patch('paymenter_backups.process.continuous_workers', return_value=True):
            result = engine.plan(self.state, self.envelope, self.inventory())
        self.assertIn('continuous-worker-present', result['admission_errors'])
        self.assertEqual(state_for(result['readiness_items']), 'needs-setup')

    def test_overview_exact_v2_shapes_types_last_good_and_secret_whitelist(self):
        self.backup('full', 'full-quiesced')
        self.backup('database', 'database-online', 2)
        self.envelope['credentials'] = {'access_key': 'secret-canary', 'secret_key': 'secret-canary'}
        result = presentation.overview(self.state, self.envelope)
        conf = result['configuration']
        self.assertEqual(conf['settings_version'], 2)
        self.assertEqual(set(conf['schedules']), {'timezone', 'full', 'database'})
        self.assertEqual(set(conf['schedules']['full']), {'frequency', 'time', 'weekdays', 'next_at'})
        self.assertEqual(set(conf['schedules']['database']), {'frequency', 'every_hours', 'minute', 'time', 'weekdays', 'next_at'})
        self.assertEqual(set(conf['storage']), {'keep_local', 'upload_s3', 'local_path', 'default_local_path', 'legacy_default_path', 's3_bucket', 's3_region', 's3_endpoint', 's3_prefix', 's3_path_style'})
        self.assertEqual(set(conf['retention']), {'automatic', 'full', 'database', 'pending', 'last_apply'})
        self.assertEqual(set(conf['retention']['pending']), {'full', 'database'})
        self.assertEqual(set(conf['checks']), {'jobs', 'stores', 'tools', 'billing'})
        self.assertEqual(set(conf['checks']['jobs']), {'checked_at', 'state', 'error', 'items', 'suggestions', 'crontab_user'})
        self.assertEqual(set(conf['checks']['billing']), {'time', 'timezone', 'window_before', 'window_after'})
        self.assertEqual((result['last_good']['full'], result['last_good']['database']), ('full', 'database'))
        raw = json.dumps(result)
        self.assertNotIn('secret-canary', raw)
        self.assertNotIn('APP_KEY', raw)
        self.assertNotIn('archive_path', raw)
        self.assertNotIn('manifest_path', raw)
        for readiness in conf['readiness'].values():
            self.assertTrue(all(set(item) == {'code', 'field', 'detail'} for item in readiness['items']))
        for report in conf['checks']['tools'].values():
            self.assertEqual(set(report), {'configured', 'resolved', 'ok', 'code'})


class TimezoneDataTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        self.settings.update(settings_version=1, enabled=True, schedule_frequency='daily',
                             schedule_time='02:00', schedule_timezone='UTC')
        config.normalize(self.settings)
        self.envelope['settings'] = self.settings
        self.envelope['tools'] = {name: sys.executable for name in tools.CANDIDATES}

    def valid_toolchain(self):
        bindir = self.base / 'valid-tools'
        bindir.mkdir()
        (bindir / 'python3').symlink_to(sys.executable)
        programs = {
            'php': "printf 'cli 8.3.35\\n'",
            'mariadb-dump': "printf 'mariadb-dump 11.8.9\\n'",
            'mariadb': "printf 'mariadb 11.8.9\\n'",
            'tar': "printf 'tar (GNU tar) 1.35\\n'",
            'gzip': "printf 'gzip 1.12\\nCopyright (C) 2025 Free Software Foundation, Inc.\\n'",
        }
        for name, output in programs.items():
            binary = bindir / name
            binary.write_text('#!/bin/sh\n' + output + '\n')
            binary.chmod(0o700)
        self.envelope['tools'] = {name: str(bindir / binary) for name, binary in {
            'python': 'python3', 'php': 'php', 'dump': 'mariadb-dump', 'mysql': 'mariadb',
            'tar': 'tar', 'gzip': 'gzip', 'gpg': 'missing-gpg',
        }.items()}

    def without_timezone_data(self):
        import zoneinfo
        empty = self.base / 'empty-zoneinfo'
        empty.mkdir()
        previous = zoneinfo.TZPATH
        zoneinfo.reset_tzpath([str(empty)])
        zoneinfo.ZoneInfo.clear_cache()
        find_tzfile = patch.object(zoneinfo._tzpath, 'find_tzfile', return_value=None)
        load_tzdata = patch.object(zoneinfo._common, 'load_tzdata', side_effect=zoneinfo.ZoneInfoNotFoundError)
        find_tzfile.start()
        load_tzdata.start()

        def restore():
            load_tzdata.stop()
            find_tzfile.stop()
            zoneinfo.reset_tzpath(previous)
            zoneinfo.ZoneInfo.clear_cache()

        self.addCleanup(restore)

    def test_utc_configuration_and_next_slot_work_without_timezone_data(self):
        self.without_timezone_data()
        self.valid_toolchain()
        config.validate(self.envelope)
        self.assertIsInstance(requests.next_slot(self.settings, now=instant('2026-10-23T00:00:00+00:00')), int)
        self.assertTrue(list(requests.due_slots(self.settings, now=instant('2026-10-23T02:00:10+00:00'))))

    def test_other_zones_report_missing_timezone_data_and_field(self):
        self.without_timezone_data()
        self.settings['schedule_timezone'] = 'Europe/Berlin'
        with self.assertRaises(BackupError) as refused:
            config.validate(self.envelope)
        self.assertEqual((refused.exception.code, refused.exception.field),
                         ('timezone-data-unavailable', 'schedule_timezone'))

    def test_invalid_single_schedule_value_names_its_field(self):
        self.settings['schedule_time'] = 'not-a-time'
        with self.assertRaises(BackupError) as refused:
            config.validate(self.envelope)
        self.assertEqual((refused.exception.code, refused.exception.field), ('config-invalid', 'schedule_time'))

    def test_bad_timezone_key_errors_are_field_specific_and_overview_survives(self):
        self.settings['schedule_timezone'] = 'Europe/Berlin'
        for failure in (ValueError('invalid zone key'), OSError('zone file unreadable')):
            with self.subTest(error=type(failure).__name__), patch.object(config, 'ZoneInfo', side_effect=failure):
                with patch.object(cli, 'bridge', return_value=copy.deepcopy(self.envelope)):
                    args = SimpleNamespace(app=str(self.app), private_root=str(self.state.root), bridge='fixture',
                                           command='overview', offline=False)
                    result = cli.dispatch(args)
                self.assertFalse(result['configuration']['valid'])
                self.assertEqual(result['configuration']['error_field'], 'schedule_timezone')

    def test_overview_uses_detected_tools_when_a_setting_stops_validation(self):
        bindir = self.base / 'tools'
        bindir.mkdir()
        (bindir / 'python3').symlink_to(sys.executable)
        programs = {
            'php': "printf 'cli 8.3.35\\n'",
            'mariadb-dump': "printf 'mariadb-dump 11.8.9\\n'",
            'mariadb': "printf 'mariadb 11.8.9\\n'",
            'tar': "printf 'tar (GNU tar) 1.35\\n'",
            'gzip': "printf 'gzip 1.12\\nCopyright (C) 2025 Free Software Foundation, Inc.\\n'",
        }
        for name, output in programs.items():
            binary = bindir / name
            binary.write_text('#!/bin/sh\n' + output + '\n')
            binary.chmod(0o700)
        self.envelope['tools'] = {name: '' for name in tools.CANDIDATES}
        self.settings['schedule_time'] = 'not-a-time'
        with patch.object(tools, 'TRUSTED_PATH', str(bindir)):
            try:
                config.validate(self.envelope)
            except BackupError as error:
                self.envelope['validation_error'] = {'field': error.field, 'detail': error.detail}
                result = presentation.overview(self.state, self.envelope, error.code)
            else:
                self.fail('the malformed saved setting must stop validation before tool discovery')
        configuration = result['configuration']
        reports = configuration['checks']['tools']
        for name in ('dump', 'mysql', 'tar', 'gzip'):
            with self.subTest(tool=name):
                self.assertTrue(reports[name]['ok'], reports[name])
                self.assertTrue(reports[name]['resolved'], reports[name])
                self.assertNotIn('tool-unavailable', configuration['admission']['database-online'])
        self.assertTrue(any(item['code'] == 'config-invalid' and item['field'] == 'schedule_time'
                            for item in configuration['readiness']['database-online']['items']))

    def test_busybox_gzip_is_reported_as_unsupported_in_overview(self):
        bindir = self.base / 'busybox-tools'
        bindir.mkdir()
        (bindir / 'python3').symlink_to(sys.executable)
        programs = {
            'php': "printf 'cli 8.3.35\\n'",
            'mariadb-dump': "printf 'mariadb-dump 11.8.9\\n'",
            'mariadb': "printf 'mariadb 11.8.9\\n'",
            'tar': "printf 'tar (GNU tar) 1.35\\n'",
            'gzip': "if [ \"$1\" = '--version' ]; then printf 'gzip: unrecognized option: version\\n' >&2; exit 1; fi; printf 'BusyBox v1.35.0\\n' >&2; exit 1",
        }
        for name, output in programs.items():
            binary = bindir / name
            binary.write_text('#!/bin/sh\n' + output + '\n')
            binary.chmod(0o700)
        self.envelope['tools'] = {name: '' for name in tools.CANDIDATES}
        self.settings['schedule_time'] = 'not-a-time'
        with patch.object(tools, 'TRUSTED_PATH', str(bindir)):
            try:
                config.validate(self.envelope)
            except BackupError as error:
                self.assertEqual((error.code, error.field), ('config-invalid', 'schedule_time'))
                self.envelope['validation_error'] = {'field': error.field, 'detail': error.detail}
                result = presentation.overview(self.state, self.envelope, error.code)
            else:
                self.fail('the invalid saved setting must reproduce the early validation stop')
        configuration = result['configuration']
        self.assertEqual(configuration['checks']['tools']['gzip']['code'], 'gzip-busybox')
        self.assertIn('gzip-busybox', configuration['admission']['database-online'])
        self.assertTrue(any(item['code'] == 'gzip-busybox' and item['field'] == 'gzip'
                            for item in configuration['readiness']['database-online']['items']))


class ToolTests(PrivateFixture):
    def test_gzip_rejects_non_gnu_non_busybox_binary(self):
        binary = pathlib.Path(self.script('lookalike-gzip', "print('gzip 1.14')"))
        result = tools.inspect('gzip', str(binary))
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'tool-capability-unsupported')

    def test_trusted_detection_ignores_path_cwd_and_preserves_explicit_pin(self):
        hostile = self.script('python3', 'raise SystemExit("secret-canary")')
        with patch.dict(os.environ, {'PATH': str(self.base)}):
            result = tools.inspect('python')
        self.assertTrue(result['ok'])
        self.assertNotEqual(result['resolved'], hostile)
        explicit = tools.inspect('python', sys.executable)
        self.assertEqual(explicit['resolved'], str(pathlib.Path(sys.executable).resolve()))
        self.assertFalse(tools.inspect('python', '/missing/pinned-python')['ok'])
        self.assertEqual(tools.inspect('python', 'relative/python')['code'], 'tool-path-invalid')

    def test_supported_versions_php_cli_gnu_tar_and_client_alternatives(self):
        for name, output in (('python', '3.9'), ('php', 'fpm-fcgi 8.3.0'), ('tar', 'bsdtar 3.6'), ('dump', 'unsupported'), ('mysql', 'unsupported')):
            with self.subTest(name=name):
                binary = self.script(name, 'print(' + repr(output) + ')')
                self.assertEqual(tools.inspect(name, binary)['code'], 'tool-capability-unsupported')
        calls = []
        def which(name, path=None):
            calls.append((name, path))
            return '/usr/bin/true' if name in ('mysqldump', 'mysql') else None
        with patch.object(tools.shutil, 'which', side_effect=which):
            self.assertTrue(tools.inspect('dump', capabilities=False)['ok'])
            self.assertTrue(tools.inspect('mysql', capabilities=False)['ok'])
        self.assertEqual([name for name, _ in calls], ['mariadb-dump', 'mysqldump', 'mariadb', 'mysql'])
        self.assertTrue(all(path == tools.TRUSTED_PATH for _, path in calls))
        self.assertTrue(tools.inspect('php', fallback=self.script('hosting-php', "print('cli 8.3.35')"))['ok'])


    def test_tool_child_that_closes_pipes_and_hangs_is_bounded_and_structured(self):
        binary = self.script('closed-pipes', 'import os,time\nos.close(1)\nos.close(2)\ntime.sleep(20)')
        result = tools.inspect('tar', binary)
        self.assertEqual(result['code'], 'tool-unavailable')
        self.assertFalse(result['ok'])


    def test_failed_capture_tool_names_program_without_child_stderr(self):
        binary = self.script('failed-dump', 'import sys\nsys.stderr.write("secret-canary")\nsys.exit(2)')
        with self.assertRaises(BackupError) as refused:
            engine.run_tool('dump', [binary], 2)
        self.assertEqual((refused.exception.code, refused.exception.field), ('child-failed', 'dump'))
        self.assertEqual(refused.exception.detail, {'tool': 'dump'})
        self.assertNotIn('secret-canary', str(refused.exception))

    @unittest.skipUnless(PHP, 'PHP CLI required for combined credential validation')
    def test_php_combined_validation_preservation_and_no_partial_mutation(self):
        result = subprocess.run([PHP, str(ROOT / 'tests/integration/credential_input_checks.php')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checks'], 8)
