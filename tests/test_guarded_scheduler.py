"""The guarded scheduler remains exact through discovery and display."""
import contextlib
import json
import os
import pathlib
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups import discovery, process
from paymenter_backups.maintenance import _legacy_cron_lock, systemd_locks
from paymenter_backups.common import BackupError, State, atomic_json
from guarded_scheduler_fixture import guarded_host


class GuardedSchedulerTests(unittest.TestCase):
    def setUp(self):
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        self.host = contexts.enter_context(guarded_host())
        self.script = self.host.script
        temporary = tempfile.TemporaryDirectory(prefix='backups-guarded-scheduler-')
        self.addCleanup(temporary.cleanup)
        base = pathlib.Path(temporary.name)
        (base / 'app').mkdir()
        (base / 'private').mkdir(mode=0o700)
        self.state = State(base / 'app', base / 'private', initialize=False)
        self.state.app = pathlib.Path('/app')
        self.lock = self.state.root / 'scheduler.lock'
        self.lock.touch(mode=0o600)
        self.line = f'* * * * * /usr/bin/flock -n {self.lock} /usr/local/bin/php /opt/paymenter/scheduler.php'
        self.envelope = {'settings': {'job_lock_paths': []}, 'tools': {'php': '/usr/local/bin/php'}}

    def test_unset_guard_refuses_script_but_accepts_standard_artisan(self):
        with patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': ''}):
            with self.assertRaisesRegex(BackupError, 'job-command-unsupported'):
                discovery.parse(self.line, self.state.app)
            self.assertEqual(discovery.record(self.line, self.state.app, 'crontab', 'line 1')['coordination'], 'unsupported')
            ordinary = self.line.replace('/opt/paymenter/scheduler.php', '/app/artisan schedule:run')
            self.assertEqual(discovery.parse(ordinary, self.state.app)['coordination'], 'coordinated')
            self.assertIn('/app/artisan schedule:run', discovery.suggestions(self.state, self.envelope)['scheduler'])

    def test_configured_custom_name_and_cron_special_characters_round_trip(self):
        script = '/opt/paymenter/custom % runner.php'
        with guarded_host(script):
            line = self.line.replace('/opt/paymenter/scheduler.php', discovery.cron_quote(script))
            self.assertTrue(discovery.relevant(line, self.state.app))
            row = discovery.record(line, self.state.app, 'crontab', 'line 1')
            self.assertEqual(row['coordination'], 'coordinated')
            self.assertEqual(shlex.split(row['command'])[-1], script)
            self.assertEqual(_legacy_cron_lock(line, self.state.app, [str(self.lock)]), str(self.lock))
            self.assertEqual(discovery.admission(self.envelope, {'state': 'ok', 'items': [row]}), ([], [str(self.lock)]))
            self.assertEqual(discovery.suggestions(self.state, self.envelope, {'items': [row]})['scheduler'], line + ' >/dev/null 2>&1')
            with self.assertRaisesRegex(BackupError, 'job-command-unsupported'):
                discovery.parse(line + ' --quiet', self.state.app)

    def test_changed_or_disabled_setting_refuses_obsolete_custom_cron_launchers(self):
        script = '/opt/paymenter/custom % runner.php'
        ordinary = f'* * * * * /usr/bin/flock -n {self.lock} /usr/local/bin/php /app/artisan schedule:run'
        with guarded_host(script):
            line = self.line.replace('/opt/paymenter/scheduler.php', discovery.cron_quote(script))
            for value in ('', '/opt/paymenter/renamed.php'):
                with self.subTest(value=value), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': value}):
                    with patch.object(discovery, 'probe', return_value=(0, (line + '\n' + ordinary + '\n').encode(), b'')), patch.object(discovery, 'systemd', return_value=[]):
                        inventory = discovery.discover(self.state, self.envelope)
                    self.assertEqual(len(inventory['items']), 2)
                    errors, _ = discovery.admission(self.envelope, inventory)
                    self.assertIn('job-command-unsupported', [item['code'] for item in errors])
                    with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
                        _legacy_cron_lock(line, self.state.app, [str(self.lock)])

    def test_changed_or_disabled_setting_refuses_obsolete_custom_systemd_launchers(self):
        script = '/opt/paymenter/old-runner'
        command = f'/usr/bin/flock -n {self.lock} /usr/local/bin/php {script}'
        ordinary = f'/usr/bin/flock -n {self.lock} /usr/local/bin/php /app/artisan schedule:run'
        def read(arguments, timeout):
            if '--user' in arguments:
                return b'[]'
            if 'list-timers' in arguments:
                return b'[{"activates":"old.service"},{"activates":"ordinary.service"}]'
            if 'show' in arguments:
                return '\n\n'.join(
                    f'Id={name}.service\nLoadState=loaded\nUser={os.getuid()}\nWorkingDirectory=/\nDynamicUser=no\nRestart=no\nExecStart={{ argv[]={line} ; ignore_errors=no ; }}\n'
                    for name, line in (('old', command), ('ordinary', ordinary))).encode()
            return b'[]'
        tools = {'systemctl': sys.executable}
        for value in ('', '/opt/paymenter/renamed.php'):
            with self.subTest(value=value), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': value}), \
                    patch.object(discovery, 'run', side_effect=read), patch('paymenter_backups.maintenance.run', side_effect=read):
                rows = discovery.systemd(self.state.app, tools)
                self.assertEqual(len(rows), 2)
                errors, _ = discovery.admission(self.envelope, {'state': 'ok', 'items': rows})
                self.assertIn('job-command-unsupported', [item['code'] for item in errors])
                with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
                    systemd_locks(self.state.app, [str(self.lock)], tools)

    def test_generic_script_basenames_do_not_claim_other_app_artisan_jobs(self):
        line = '* * * * * /usr/bin/flock -n /private/other.lock /usr/local/bin/php -q /other/artisan schedule:run'
        for script in ('/opt/paymenter/artisan', '/opt/paymenter/php'):
            with self.subTest(script=script), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': script}):
                self.assertFalse(discovery.relevant(line, self.state.app))

    def test_other_app_artisan_php_flags_do_not_become_unknown_scripts(self):
        own = f'/usr/bin/flock -n {self.lock} /usr/local/bin/php /app/artisan schedule:run'
        tools = {'systemctl': sys.executable}
        for flags in ('-q', '-f', '-d memory_limit=512M', '-q -d memory_limit=512M -f'):
            other = f'/usr/bin/flock -n /private/other.lock /usr/local/bin/php {flags} /other/artisan schedule:run'
            def read(arguments, timeout):
                if '--user' in arguments:
                    return b'[]'
                if 'list-timers' in arguments:
                    return b'[{"activates":"own.service"},{"activates":"other.service"}]'
                if 'show' in arguments:
                    return '\n\n'.join(
                        f'Id={name}.service\nLoadState=loaded\nUser={os.getuid()}\nWorkingDirectory=/\nDynamicUser=no\nRestart=no\nExecStart={{ argv[]={line} ; ignore_errors=no ; }}\n'
                        for name, line in (('own', own), ('other', other))).encode()
                return b'[]'
            with self.subTest(flags=flags), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': ''}):
                cron = ('* * * * * ' + own + '\n* * * * * ' + other + '\n').encode()
                with patch.object(discovery, 'probe', return_value=(0, cron, b'')), patch.object(discovery, 'systemd', return_value=[]):
                    inventory = discovery.discover(self.state, self.envelope)
                self.assertEqual(len(inventory['items']), 1)
                self.assertEqual(discovery.admission(self.envelope, inventory), ([], [str(self.lock)]))
                with patch.object(discovery, 'run', side_effect=read), patch('paymenter_backups.maintenance.run', side_effect=read):
                    rows = discovery.systemd(self.state.app, tools)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['label'], 'own.service')
                    self.assertEqual(systemd_locks(self.state.app, [str(self.lock)], tools), {str(self.lock)})

    def test_unrelated_artisan_commands_do_not_block_cron_or_systemd(self):
        own = f'/usr/bin/flock -n {self.lock} /usr/local/bin/php /app/artisan schedule:run'
        tools = {'systemctl': sys.executable}
        for target in ('/app/artisan', '/other/artisan'):
            for flags in ('', '-q -d memory_limit=512M -f'):
                other = f'/usr/local/bin/php {flags} {target} cache:prune-stale-tags'
                def read(arguments, timeout):
                    if '--user' in arguments:
                        return b'[]'
                    if 'list-timers' in arguments:
                        return b'[{"activates":"own.service"},{"activates":"other.service"}]'
                    if 'show' in arguments:
                        return '\n\n'.join(
                            f'Id={name}.service\nLoadState=loaded\nUser={os.getuid()}\nWorkingDirectory=/\nDynamicUser=no\nRestart=no\nExecStart={{ argv[]={line} ; ignore_errors=no ; }}\n'
                            for name, line in (('own', own), ('other', other))).encode()
                    return b'[]'
                with self.subTest(target=target, flags=flags), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': ''}):
                    cron = ('* * * * * ' + own + '\n* * * * * ' + other + '\n').encode()
                    with patch.object(discovery, 'probe', return_value=(0, cron, b'')), patch.object(discovery, 'systemd', return_value=[]):
                        inventory = discovery.discover(self.state, self.envelope)
                    self.assertEqual(len(inventory['items']), 1)
                    self.assertEqual(discovery.admission(self.envelope, inventory), ([], [str(self.lock)]))
                    with patch.object(discovery, 'run', side_effect=read), patch('paymenter_backups.maintenance.run', side_effect=read):
                        rows = discovery.systemd(self.state.app, tools)
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(systemd_locks(self.state.app, [str(self.lock)], tools), {str(self.lock)})

    def test_obsolete_guard_named_artisan_without_command_still_refuses(self):
        for ending in ('', ' >/dev/null 2>&1'):
            line = self.line.replace('/opt/paymenter/scheduler.php', '/opt/paymenter/artisan') + ending
            with self.subTest(ending=ending), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': ''}):
                self.assertTrue(discovery.relevant(line, self.state.app))
                row = discovery.record(line, self.state.app, 'crontab', 'old launcher')
                self.assertEqual(row['coordination'], 'unsupported')
                with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
                    _legacy_cron_lock(line, self.state.app, [str(self.lock)])

    def test_changed_setting_cannot_reuse_previous_guarded_inventory(self):
        inventory = {
            'schema_version': 1, 'parser_version': discovery.PARSER_VERSION,
            'app': '/app', 'checked_at': 100, 'state': 'ok', 'error': None,
            'items': [discovery.record(self.line, self.state.app, 'crontab', 'line 1')]}
        atomic_json(self.state.root / 'discovery.json', inventory)
        for value in ('', '/opt/other/scheduler.php'):
            with self.subTest(value=value), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': value}):
                self.assertEqual(discovery.cached(self.state, now=101)['state'], 'unknown')
                errors, _ = discovery.admission(self.envelope, inventory)
                self.assertIn('job-command-unsupported', [item['code'] for item in errors])
                self.assertIn('/app/artisan schedule:run', discovery.suggestions(self.state, self.envelope, inventory)['scheduler'])

    def test_invalid_deployment_paths_do_not_enable_guarded_launchers(self):
        from paymenter_backups.scheduler import guarded_scheduler_path
        for value in ('relative.php', '/', '/opt//runner.php', '/opt/./runner.php',
                      '/opt/../runner.php', '/opt/runner.php/', '/opt/runner.php\n', '/opt/$runner.php'):
            with self.subTest(value=value), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': value}):
                self.assertEqual(guarded_scheduler_path(), '')
                with self.assertRaisesRegex(BackupError, 'job-command-unsupported'):
                    discovery.parse(self.line, self.state.app)

    def test_exact_guarded_launcher_is_discovered_coordinated_and_round_trips(self):
        with patch.object(discovery, 'probe', return_value=(0, (self.line + '\n').encode(), b'')), patch.object(discovery, 'systemd', return_value=[]):
            inventory = discovery.discover(self.state, self.envelope, now=100)
        self.assertEqual(len(inventory['items']), 1)
        row = inventory['items'][0]
        self.assertEqual((row['kind'], row['coordination']), ('scheduler', 'coordinated'))
        expected = f'/usr/bin/flock -n {self.lock} /usr/local/bin/php /opt/paymenter/scheduler.php'
        self.assertEqual(row['command'], expected)
        self.assertEqual(discovery.admission(self.envelope, inventory), ([], [str(self.lock)]))
        self.assertEqual(discovery.parse(row['command'], self.state.app, cron=False)['command'], expected)
        atomic_json(self.state.root / 'discovery.json', inventory)
        self.assertEqual(discovery.cached(self.state, now=101)['items'], inventory['items'])

    def test_guarded_script_symlink_refuses_discovery_and_legacy_validation(self):
        other = self.script.with_name('other.php')
        self.script.rename(other)
        self.script.symlink_to(other)
        self.assert_guard_refused()

    def assert_guard_refused(self):
        self.assertEqual(discovery.parse(self.line, self.state.app)['coordination'], 'unsupported')
        row = discovery.record(self.line, self.state.app, 'crontab', 'line 1')
        self.assertEqual(row['coordination'], 'unsupported')
        errors, _ = discovery.admission(self.envelope, {'state': 'ok', 'items': [row]})
        self.assertIn('job-command-unsupported', [item['code'] for item in errors])
        with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
            _legacy_cron_lock(self.line, self.state.app, [str(self.lock)])

    def test_guarded_script_parent_symlink_refuses(self):
        parent = self.script.parent
        other = parent.with_name('alternate')
        parent.rename(other)
        parent.symlink_to(other, target_is_directory=True)
        self.assert_guard_refused()

    def test_guarded_script_group_and_world_writable_modes_refuse(self):
        for mode in (0o664, 0o646, 0o666):
            with self.subTest(mode=oct(mode)):
                self.script.chmod(mode)
                self.assert_guard_refused()

    def test_guarded_script_nonroot_owner_refuses(self):
        self.host.owner_uid = 1000
        self.assert_guard_refused()

    def test_guarded_script_directory_and_missing_file_refuse(self):
        self.script.unlink()
        self.assert_guard_refused()
        self.script.mkdir()
        self.assert_guard_refused()

    def test_admission_rechecks_script_after_discovery(self):
        row = discovery.record(self.line, self.state.app, 'crontab', 'line 1')
        self.assertEqual(row['coordination'], 'coordinated')
        self.script.chmod(0o666)
        errors, _ = discovery.admission(self.envelope, {'state': 'ok', 'items': [row]})
        self.assertIn('job-command-unsupported', [item['code'] for item in errors])

    def test_cached_readiness_rechecks_replaced_script_and_preserves_guard(self):
        row = discovery.record(self.line, self.state.app, 'crontab', 'line 1')
        inventory = {'schema_version': 1, 'parser_version': discovery.PARSER_VERSION,
                     'app': '/app', 'checked_at': 100, 'state': 'ok', 'error': None, 'items': [row]}
        atomic_json(self.state.root / 'discovery.json', inventory)
        other = self.script.with_name('replacement.php')
        self.script.rename(other)
        self.script.symlink_to(other)
        current = discovery.cached(self.state, now=101)
        self.assertEqual(len(current['items']), 1)
        self.assertEqual(current['items'][0]['coordination'], 'unsupported')
        self.assertEqual(current['items'][0]['command'], row['command'])
        errors, _ = discovery.admission(self.envelope, current)
        self.assertIn('job-command-unsupported', [item['code'] for item in errors])
        self.assertEqual(discovery.suggestions(self.state, self.envelope, current)['scheduler'],
                         self.line + ' >/dev/null 2>&1')

    def test_guarded_launcher_variants_refuse(self):
        invalid = [self.line + ' --quiet', self.line + ' schedule:run',
                   self.line.replace('/opt/paymenter/scheduler.php', '/opt/other/scheduler.php'),
                   self.line.replace('/usr/local/bin/php', '/usr/bin/php'),
                   self.line.replace('/usr/local/bin/php', 'php'),
                   self.line.replace('/usr/local/bin/php', '/usr/local/bin/php -q'),
                   self.line.replace(f'/usr/bin/flock -n {self.lock} ', ''),
                   self.line.replace(str(self.lock), '/private/../scheduler.lock')]
        for line in invalid:
            with self.subTest(line=line):
                self.assertTrue(discovery.relevant(line, self.state.app))
                with self.assertRaisesRegex(BackupError, 'job-command-unsupported'):
                    discovery.parse(line, self.state.app)
        with self.assertRaisesRegex(BackupError, 'job-command-unsupported'):
            discovery.parse(self.line, pathlib.Path('/other'))
        self.assertFalse(discovery.relevant(self.line, pathlib.Path('/other')))

    def test_guarded_launcher_keeps_private_lock_checks(self):
        row = discovery.record(self.line, self.state.app, 'crontab', 'line 1')
        self.lock.chmod(0o644)
        errors, _ = discovery.admission(self.envelope, {'state': 'ok', 'items': [row]})
        self.assertIn('job-lock-unsafe', [item['code'] for item in errors])

    def test_version_two_cache_is_discarded_and_refreshed(self):
        old = {'schema_version': 1, 'parser_version': 2, 'app': '/app',
               'checked_at': 100, 'state': 'ok', 'error': None, 'items': []}
        atomic_json(self.state.root / 'discovery.json', old)
        self.assertEqual(discovery.cached(self.state, now=101)['state'], 'unknown')
        with patch.object(discovery, 'probe', return_value=(0, (self.line + '\n').encode(), b'')), patch.object(discovery, 'systemd', return_value=[]):
            current = discovery.refresh_cache(self.state, self.envelope, now=101)
        self.assertEqual(current['parser_version'], 4)
        self.assertEqual(len(current['items']), 1)

    def test_suggestions_preserve_the_discovered_guard_and_lock(self):
        row = discovery.record(self.line, self.state.app, 'crontab', 'line 1')
        inventory = {'state': 'ok', 'items': [row]}
        suggested = discovery.suggestions(self.state, self.envelope, inventory)['scheduler']
        self.assertEqual(suggested, self.line + ' >/dev/null 2>&1')
        self.assertNotIn('artisan', suggested)
        self.assertEqual(discovery.parse(suggested, self.state.app)['command'], row['command'])

    def test_guarded_suggestions_keep_cron_percent_escaping(self):
        lock = str(self.state.root / r'job\% archive.lock')
        command = shlex.join(['/usr/bin/flock', '-n', lock, '/usr/local/bin/php', '/opt/paymenter/scheduler.php'])
        inventory = {'items': [{'command': command}]}
        suggested = discovery.suggestions(self.state, self.envelope, inventory)['scheduler']
        self.assertEqual(discovery.parse(suggested, self.state.app)['lock'], lock)
        self.assertTrue(suggested.endswith(' /usr/local/bin/php /opt/paymenter/scheduler.php >/dev/null 2>&1'))

    def test_legacy_validation_preserves_the_same_guarded_contract(self):
        self.assertEqual(_legacy_cron_lock(self.line, self.state.app, [str(self.lock)]), str(self.lock))
        for line, app in ((self.line + ' --quiet', self.state.app),
                          (self.line.replace('/usr/local/bin/php', '/usr/bin/php'), self.state.app),
                          (self.line.replace(f'/usr/bin/flock -n {self.lock} ', ''), self.state.app),
                          (self.line, pathlib.Path('/other'))):
            with self.subTest(line=line, app=app), self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
                _legacy_cron_lock(line, app, [str(self.lock)])

    def test_systemd_guarded_launcher_keeps_owner_checks_in_both_parsers(self):
        command = self.line.split(' ', 5)[5]
        owner = os.getuid()

        def read(arguments, timeout):
            if '--user' in arguments:
                return b'[]'
            if 'list-timers' in arguments:
                return json.dumps([{'activates': 'guarded.service'}]).encode()
            if 'show' in arguments:
                return (f'Id=guarded.service\nLoadState=loaded\nUser={owner}\n'
                        f'WorkingDirectory=/\nDynamicUser=no\nRestart=no\n'
                        f'ExecStart={{ argv[]={command} ; ignore_errors=no ; }}\n').encode()
            return b'[]'

        tools = {'systemctl': sys.executable}
        with patch.object(discovery, 'run', side_effect=read), patch('paymenter_backups.maintenance.run', side_effect=read):
            rows = discovery.systemd(self.state.app, tools)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['coordination'], 'coordinated')
            self.assertTrue(rows[0]['command'].endswith(' /usr/local/bin/php /opt/paymenter/scheduler.php'))
            self.assertEqual(systemd_locks(self.state.app, [str(self.lock)], tools), {str(self.lock)})
            self.script.chmod(0o666)
            self.assertEqual(discovery.systemd(self.state.app, tools)[0]['coordination'], 'unsupported')
            with self.assertRaisesRegex(BackupError, 'job-locks-unverified'):
                systemd_locks(self.state.app, [str(self.lock)], tools)
            self.script.chmod(0o644)
            owner += 1
            self.assertEqual(discovery.systemd(self.state.app, tools), [])
            self.assertEqual(systemd_locks(self.state.app, [str(self.lock)], tools), set())

    def test_retired_live_scheduler_blocks_replacement_lock_coordination(self):
        from paymenter_backups.maintenance import coordination
        real_path = type(self.state.root)
        proc = self.state.root / 'proc'
        child = proc / str(os.getpid() + 100000)
        child.mkdir(parents=True)
        (child / 'cwd').symlink_to('/')
        cmdline = child / 'cmdline'
        replacement = self.line.replace('/opt/paymenter/scheduler.php', '/app/artisan schedule:run')
        envelope = {'settings': {'settings_version': 2, 'job_lock_paths': [], 'lock_timeout': 1},
                    'tools': {'php': '/usr/local/bin/php'}}
        for retired in ('/opt/paymenter/old-runner', '/opt/paymenter/artisan'):
            cmdline.write_bytes(b'/usr/local/bin/php\0' + os.fsencode(retired) + b'\0')
            for setting in ('', '/opt/paymenter/new-runner'):
                with self.subTest(retired=retired, setting=setting), patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': setting}), \
                        patch.object(process.pathlib, 'Path', side_effect=lambda value: proc if str(value) == '/proc' else real_path(value)), \
                        patch.object(discovery, 'probe', return_value=(0, (replacement + '\n').encode(), b'')), \
                        patch.object(discovery, 'systemd', return_value=[]):
                    inventory = discovery.discover(self.state, envelope)
                    self.assertEqual(discovery.admission(envelope, inventory), ([], [str(self.lock)]))
                    with self.assertRaisesRegex(BackupError, 'job-workers-running'):
                        with coordination(self.state, envelope):
                            self.fail('Capture must not start beside a retired scheduler')
        cmdline.write_bytes(b'/usr/local/bin/php\0-q\0/other/artisan\0cache:prune-stale-tags\0')
        with patch.object(process.pathlib, 'Path', side_effect=lambda value: proc if str(value) == '/proc' else real_path(value)):
            self.assertEqual(process.app_workers(self.state.app), [])

    def test_running_guarded_scheduler_is_checked_after_lock_acquisition(self):
        real_path = type(self.state.root)
        proc = self.state.root / 'proc'
        child = proc / str(os.getpid() + 100000)
        child.mkdir(parents=True)
        (child / 'cwd').symlink_to('/')
        cmdline = child / 'cmdline'
        cmdline.write_bytes(b'/usr/local/bin/php\0/opt/paymenter/scheduler.php\0')
        with patch.object(process.pathlib, 'Path', side_effect=lambda value: proc if str(value) == '/proc' else real_path(value)):
            self.assertIn(int(child.name), process.app_workers(self.state.app))
            self.assertEqual(process.app_workers(real_path('/other')), [])
            with patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': ''}):
                self.assertIn(int(child.name), process.app_workers(self.state.app))
            cmdline.write_bytes(b'/usr/local/bin/php\0/opt/paymenter/scheduler.php\0--quiet\0')
            self.assertIn(int(child.name), process.app_workers(self.state.app))
            cmdline.write_bytes(b'/usr/bin/php\0/opt/paymenter/scheduler.php\0')
            self.assertEqual(process.app_workers(self.state.app), [])
