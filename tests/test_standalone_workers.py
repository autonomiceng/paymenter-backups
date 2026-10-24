"""Admission inspects standalone services even between worker processes."""
import json
import os
import pathlib
import sys
import tempfile
import unittest

from paymenter_backups.common import BackupError, State
from paymenter_backups.maintenance import coordination


class StandaloneWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='backups-standalone-test-')
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.app = self.base / 'app'
        self.app.mkdir()
        self.state = State(self.app, self.base / 'private')
        self.lock = self.state.root / 'scheduler.lock'
        self.lock.touch(mode=0o600)
        self.unit = 'paymenter-worker.service'
        self.fixture = self.base / 'service.json'
        self.calls = self.base / 'calls.jsonl'
        self.service = dict(timer=False, active='active', state='enabled', restart='always', plain=False)
        cron = f'* * * * * /usr/bin/flock -n {self.lock} /usr/bin/php {self.app}/artisan schedule:run'
        self.envelope = {
            'settings': {'settings_version': 1, 'job_lock_paths': [str(self.lock)], 'lock_timeout': 0},
            'tools': {
                'crontab': self.script('crontab', 'print(' + repr(cron) + ')'),
                'systemctl': self.script('systemctl', f'''
import json, pathlib, sys
args = sys.argv[1:]
with pathlib.Path({str(self.calls)!r}).open('a') as output:
    output.write(json.dumps(args) + '\\n')
value = json.loads(pathlib.Path({str(self.fixture)!r}).read_text())
unit = {self.unit!r}
if '--user' in args:
    print('[]')
elif value['plain'] and '--output=json' in args:
    sys.exit(1)
elif value.get('broken') and ('list-units' in args or 'list-unit-files' in args):
    print(json.dumps(value['broken']))
elif 'list-timers' in args:
    print(('- - - - worker.timer ' + unit if value['timer'] else '') if value['plain'] else json.dumps([{{'activates': unit}}] if value['timer'] else []))
elif 'list-units' in args:
    print(unit + ' loaded ' + value['active'] + ' running worker' if value['plain'] else json.dumps([{{'unit': unit, 'active': value['active']}}]))
elif 'list-unit-files' in args:
    print(unit + ' ' + value['state'] + ' enabled' if value['plain'] else json.dumps([{{'unit_file': unit, 'state': value['state']}}]))
elif 'show' in args:
    assert args[args.index('--') + 1:] == [unit]
    print('Id=' + unit)
    print('User={os.getuid()}\\nWorkingDirectory={self.app}\\nDynamicUser=no\\nLoadState=loaded')
    print('Restart=' + value['restart'])
    print('ExecStart={{ path=/usr/bin/flock ; argv[]=/usr/bin/flock -n {self.lock} /usr/bin/php {self.app}/artisan queue:work --stop-when-empty ; ignore_errors=no ; }}')
else:
    raise AssertionError(args)
'''),
            },
        }

    def script(self, name, body):
        target = self.base / name
        target.write_text('#!' + sys.executable + '\n' + body + '\n')
        target.chmod(0o700)
        return str(target)

    def configure(self, **changes):
        self.service.update(changes)
        self.fixture.write_text(json.dumps(self.service))

    def test_legacy_refuses_restarting_worker_without_timer_between_runs(self):
        self.configure()
        with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
            with coordination(self.state, self.envelope):
                pass

    def test_timer_and_standalone_service_states_in_both_settings_versions(self):
        cases = (
            ({'timer': True, 'active': 'inactive', 'state': 'disabled'}, True),
            ({'active': 'active', 'state': 'disabled'}, True),
            ({'active': 'activating', 'state': 'disabled'}, True),
            ({'active': 'inactive', 'state': 'enabled'}, True),
            ({'active': 'inactive', 'state': 'enabled-runtime'}, True),
            ({'active': 'inactive', 'state': 'static'}, True),
            ({'active': 'inactive', 'state': 'disabled'}, False),
            ({'restart': 'no'}, False),
            ({'plain': True, 'active': 'inactive'}, True),
        )
        for version in (1, 2):
            self.envelope['settings']['settings_version'] = version
            for changes, refuses in cases:
                with self.subTest(version=version, changes=changes):
                    self.configure(timer=False, active='active', state='enabled', restart='always', plain=False)
                    self.configure(**changes)
                    if refuses:
                        code = 'job-locks-unverified' if version == 1 else 'continuous-worker-present'
                        with self.assertRaisesRegex(BackupError, code):
                            with coordination(self.state, self.envelope):
                                pass
                    else:
                        with coordination(self.state, self.envelope):
                            pass
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertTrue(any('show' in call for call in calls))
        self.assertTrue(all(any(verb in call for verb in ('list-timers', 'list-units', 'list-unit-files', 'show')) for call in calls))

    def test_restart_policy_changes_are_rechecked_on_each_admission(self):
        for policy in ('always', 'no', 'on-failure', 'no'):
            with self.subTest(policy=policy):
                self.configure(restart=policy)
                if policy == 'no':
                    with coordination(self.state, self.envelope):
                        pass
                else:
                    with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
                        with coordination(self.state, self.envelope):
                            pass

    def test_malformed_standalone_inventory_refuses_in_both_settings_versions(self):
        for version in (1, 2):
            self.envelope['settings']['settings_version'] = version
            for broken in ([None], [{'active': 'active', 'unit': []}],
                           [{'state': 'enabled', 'unit_file': '--option.service'}],
                           [{'unit': self.unit, 'active': 'active'}] * 1001):
                with self.subTest(version=version, size=len(broken)):
                    self.configure(broken=broken)
                    code = 'job-locks-unverified' if version == 1 else 'job-discovery-incomplete'
                    with self.assertRaisesRegex(BackupError, code):
                        with coordination(self.state, self.envelope):
                            pass
