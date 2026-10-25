"""Resolved parent store settings must survive a scrubbed cold bridge."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from paymenter_backups import config, readiness
from test_archive_keys import PHP_RUNTIME
from test_database_context import MARIADB_CONTEXT

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')
CANARY = 'fixture-store-secret'


@unittest.skipUnless(PHP, 'PHP CLI required for framework context')
class FrameworkContextFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='backups-framework-context-')
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.app = self.base / 'app'
        (self.app / 'storage').mkdir(parents=True)
        (self.app / 'public').mkdir()
        self.private = self.base / 'private'
        self.private.mkdir(mode=0o700)
        self.values = {
            'app.maintenance.driver': 'file',
            'cache.default': 'active-cache',
            'cache.stores.active-cache': {'driver': 'redis', 'connection': 'cache-redis',
                'password': CANARY, 'url': 'redis://fixture:' + CANARY + '@example.test'},
            'session.driver': 'database', 'session.connection': 'mysql',
            'session.files': str(self.app / 'storage'),
            'queue.default': 'active-queue',
            'queue.connections.active-queue': {'driver': 'database', 'connection': 'mysql',
                'secret': CANARY},
            'filesystems.default': 'active-disk',
            'filesystems.disks.active-disk': {'driver': 'local', 'root': str(self.app / 'storage'),
                'key': CANARY},
            'database.redis': {'password': CANARY}, 'app.key': CANARY,
        }

    def runtime_calls(self):
        source = PHP_RUNTIME[:PHP_RUNTIME.index("    $fp = str_repeat('a',64);")]
        source = source.replace('public static function where(...$args) { return new self; }',
            'public static function __callStatic($name, $args) { return new self; }')
        source = source.replace('public function where($key, $value) { $this->key = $value; return $this; }',
            "public function __call($name, $args) { if ($name === 'where') { $this->key = $args[1]; } return $this; }")
        source = source.replace('namespace Illuminate\\Support\\Facades {',
            'namespace Paymenter\\Extensions\\Others\\Backups { function posix_geteuid() { return getmyuid(); } }\n'
            'namespace Illuminate\\Support\\Facades {')
        source = source.replace('return match ($name) {',
            '$values = json_decode(' + json.dumps(json.dumps(self.values)) + ', true);\n'
            '            if (array_key_exists($name, $values)) { return $values[$name]; }\n'
            '            return match ($name) {')
        source = source.replace('self::$calls[] = [$command, $input];',
                                'self::$calls[] = [$command, $input, $environment];')
        source += r'''
    \Symfony\Component\Process\Process::$response = ['updated' => true];
    \Paymenter\Extensions\Others\Backups\Runtime::command(['config', 'show']);
    \Paymenter\Extensions\Others\Backups\Runtime::saveConfiguration([]);
    \Paymenter\Extensions\Others\Backups\Runtime::publishCredentialsWhileLocked([], $GLOBALS['private']);
    echo json_encode(\Symfony\Component\Process\Process::$calls);
}
'''
        # The final private writer expects an ok receipt.
        source = source.replace("    \\Paymenter\\Extensions\\Others\\Backups\\Runtime::publishCredentialsWhileLocked",
            "    \\Symfony\\Component\\Process\\Process::$response = ['ok' => true];\n"
            "    \\Paymenter\\Extensions\\Others\\Backups\\Runtime::publishCredentialsWhileLocked")
        runner = self.base / 'runtime.php'
        runner.write_text(source)
        environment = dict(os.environ, SESSION_DRIVER='redis', CACHE_STORE='file',
                           REDIS_PASSWORD=CANARY, AWS_SECRET_ACCESS_KEY=CANARY)
        result = subprocess.run([PHP, str(runner), str(ROOT), str(self.app), str(self.private)],
                                env=environment, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
        return [call for call in json.loads(result.stdout) if '--database-connection-stdin' in call[0]]

class FrameworkContextTests(FrameworkContextFixture):
    def test_parent_context_uses_only_nonsecret_fields_and_keeps_environment_scrubbed(self):
        calls = self.runtime_calls()
        self.assertEqual(len(calls), 3)
        for argv, raw_input, environment in calls:
            with self.subTest(argv=argv[-2:]):
                request = json.loads(raw_input)
                context = request.get('_framework_context', request.get('framework_context'))
                self.assertIsInstance(context, dict)
                self.assertEqual(context['session']['driver'], 'database')
                self.assertEqual(context['session']['connection'], 'mysql')
                self.assertEqual(context['cache']['driver'], 'redis')
                self.assertEqual(context['cache']['connection'], 'cache-redis')
                self.assertEqual(context['queue']['default'], 'active-queue')
                self.assertEqual(context['queue']['connection'], 'mysql')
                self.assertNotIn(CANARY, json.dumps(context))
                forwarded = {key: value for key, value in environment.items() if value is not False}
                self.assertEqual(forwarded, {'PATH': '/usr/local/bin:/usr/bin:/bin',
                    'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC'})
                self.assertNotIn(CANARY, json.dumps(argv))

# Database and Laravel bootstrap doubles sit at external boundaries. The bridge,
# store classification and Python readiness code below are production code.
COLD_AUTOLOAD = r'''<?php
namespace Illuminate\Foundation {
    class Application {
        public function __construct(string $base) {}
        public function bootstrapWith(array $steps): void {}
        public function make(string $name): object { return new \ColdConfig; }
    }
}
namespace App\Models { class Extension { public function getMorphClass() { return 'extension'; } } }
namespace {
    function posix_geteuid() { return getmyuid(); }
    class ColdConfig {
        private array $values = [
            'database.default' => 'cold-db', 'cache.default' => 'file',
            'cache.stores.file' => ['driver' => 'file', 'path' => '/outside'],
            'session.driver' => 'redis', 'queue.default' => 'sync',
            'queue.connections.sync' => ['driver' => 'sync'],
        ];
        public function get($key, $default = null) { return array_key_exists($key, $this->values) ? $this->values[$key] : $default; }
        public function set($key, $value) { $this->values[$key] = $value; }
    }
    class PDO {
        const ATTR_ERRMODE=3, ERRMODE_EXCEPTION=2, FETCH_ASSOC=2, FETCH_KEY_PAIR=12;
        const MYSQL_ATTR_SSL_CA=1009, MYSQL_ATTR_SSL_CAPATH=1010, MYSQL_ATTR_SSL_CERT=1007,
            MYSQL_ATTR_SSL_KEY=1008, MYSQL_ATTR_SSL_CIPHER=1011, MYSQL_ATTR_SSL_VERIFY_SERVER_CERT=1014;
        public function __construct(...$arguments) {}
        public static function getAvailableDrivers() { return ['mysql']; }
        public function query($query) { return new class {
            public function fetch($mode) { return ['id' => 1, 'enabled' => 1]; }
            public function fetchAll($mode) { return ['cronjob_time' => '02:00']; }
        }; }
        public function prepare($query) { return new class {
            public function execute($values) {}
            public function fetchAll($mode) { return []; }
        }; }
    }
}
'''


@unittest.skipUnless(PHP, 'PHP CLI required for cold bridge')
class ColdBridgeFrameworkContextTests(FrameworkContextFixture):
    def cold_args(self, context):
        import shlex
        # -n keeps PDO loadable as an external database double on both host/CI.
        binary = subprocess.run([PHP, '-r', 'echo PHP_BINARY;'], check=True,
                                capture_output=True, timeout=10).stdout.decode()
        launcher = self.base / 'php-cold'
        launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(binary) + ' -n "$@"\n')
        launcher.chmod(0o700)
        (self.app / 'vendor').mkdir(exist_ok=True)
        (self.app / 'vendor/autoload.php').write_text(COLD_AUTOLOAD)
        return SimpleNamespace(php=str(launcher), bridge=str(ROOT / 'extension/Backups/bridge.php'),
            app=str(self.app), private_root=str(self.private),
            database_connection=dict(MARIADB_CONTEXT, default='mysql'), framework_context=context)

    def test_cold_bridge_readiness_uses_parent_context_and_same_database_name(self):
        context = json.loads(self.runtime_calls()[0][1])['_framework_context']
        args = self.cold_args(context)
        for unused in range(2):
            envelope = config.bridge(args, 'read-settings')
            detected = {item['kind']: item for item in readiness.stores(envelope)}
            self.assertEqual(detected['session']['driver'], 'database')
            self.assertTrue(detected['session']['supported'])
            self.assertTrue(detected['queue']['supported'])
            self.assertEqual(detected['cache']['driver'], 'redis')
            self.assertFalse(detected['cache']['supported'])
            self.assertNotIn(CANARY, json.dumps(envelope))
        # Connections outside the backed-up DB must still be refused.
        context['session']['connection'] = 'other-db'
        context['cache'].update(driver='database', connection='mysql', lock_connection='other-db')
        context['queue']['connection'] = 'other-db'
        envelope = config.bridge(args, 'read-settings')
        for kind in ('session', 'cache', 'queue'):
            self.assertFalse(envelope['framework']['stores'][kind]['supported'])

    def test_bridge_rejects_extra_or_invalid_context_fields_without_echoing_secrets(self):
        from paymenter_backups.common import BackupError
        context = json.loads(self.runtime_calls()[0][1])['_framework_context']
        for kind, field, value in [('cache', 'password', CANARY), ('session', 'driver', []),
                                   ('queue', 'connection', 'x' * 257), ('cache', 'path', 'bad\0path')]:
            invalid = json.loads(json.dumps(context))
            invalid[kind][field] = value
            with self.subTest(kind=kind, field=field):
                args = self.cold_args(invalid)
                with self.assertRaises(BackupError) as raised:
                    config.bridge(args, 'read-settings')
                self.assertEqual(raised.exception.code, 'framework-store-context-invalid')
                self.assertNotIn(CANARY, str(raised.exception))
                with patch.object(config, 'validate_framework_context', side_effect=lambda value: value):
                    with self.assertRaises(BackupError) as php_error:
                        config.bridge(args, 'read-settings')
                self.assertEqual(php_error.exception.code, 'framework-store-context-invalid')


class FrameworkHandoffTests(FrameworkContextFixture):
    def test_cli_strips_context_before_validation_and_forwards_it_to_native_writer(self):
        from paymenter_backups import cli
        context = json.loads(self.runtime_calls()[0][1])['_framework_context']
        args = cli.parser().parse_args(['--app', str(self.app), '--private-root', str(self.private),
            '--database-connection-stdin', 'config', 'check'])
        request = {'settings': {}, '_database_connection': MARIADB_CONTEXT, '_framework_context': context}
        state = SimpleNamespace(app=self.app, root=self.private)
        with patch.object(cli, 'clean_environment'), patch.object(cli, 'stdin_json', return_value=request), \
                patch.object(cli, 'State', return_value=state), patch.object(cli, 'check_config', return_value={}) as check:
            cli.dispatch(args)
        self.assertEqual(check.call_args.args[2], {'settings': {}})
        self.assertEqual(args.framework_context, context)
        complete = SimpleNamespace(returncode=0, stdout=b'{"ok":true}', stderr=b'')
        with patch.object(cli, 'run_native_writer', return_value=complete) as writer:
            cli.native_settings(args, state, {}, (11, 12))
        self.assertEqual(json.loads(writer.call_args.args[1])['_framework_context'], context)
        self.assertNotIn(CANARY, json.dumps(writer.call_args.kwargs))

    def test_detached_worker_receives_parent_context_on_private_stdin(self):
        from paymenter_backups import discovery, requests
        from paymenter_backups.common import State
        context = json.loads(self.runtime_calls()[0][1])['_framework_context']
        state = State(self.app, self.private)
        envelope = {'settings': dict(config.DEFAULTS, enabled=False), 'tools': {}}
        request = requests.create(state, 'verify', {'run_id': 'fixture-run'})
        with patch.object(discovery, 'refresh_cache'), \
                patch.object(requests, 'detach', return_value=123456) as detached, \
                patch.object(requests, 'process_identity', return_value=None):
            result = requests.tick(state, envelope, ['fixture'], database_context=MARIADB_CONTEXT,
                                   framework_context=context)
        self.assertEqual(result['launched'], [request['request_id']])
        sent = json.loads(detached.call_args.args[1])
        self.assertEqual(sent['_framework_context'], context)
        self.assertNotIn(CANARY, json.dumps(sent))


if __name__ == '__main__':
    unittest.main()
