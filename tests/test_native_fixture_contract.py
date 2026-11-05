import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
native_integration = importlib.import_module('native_integration')


class NativeFixtureContractTests(unittest.TestCase):
    def test_maria_socket_contract_accepts_only_a_string_path(self):
        self.assertEqual(native_integration.require_maria_socket_path('/tmp/paymenter.sock'), '/tmp/paymenter.sock')
        for invalid in (('/tmp/paymenter.sock', 3307), pathlib.Path('/tmp/paymenter.sock')):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                native_integration.require_maria_socket_path(invalid)


class NativePanelEnvironmentTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='native-panel-environment-')
        self.addCleanup(scratch.cleanup)
        self.work = pathlib.Path(scratch.name)
        self.bin = self.work / 'managed-bin'
        self.bin.mkdir()
        self.node = self.bin / 'node'
        self.node.write_text('#!/bin/sh\nexit 0\n')
        self.node.chmod(0o700)

    def environment(self, **changes):
        parent = {'PATH': str(self.bin), 'PAYMENTER_BACKUPS_PLAYWRIGHT': '/fixture/playwright',
                  'PLAYWRIGHT_BROWSERS_PATH': '/fixture/browsers', 'XDG_CACHE_HOME': '/fixture/cache',
                  'HOME': '/fixture/home', 'DB_PASSWORD': 'synthetic-parent-only',
                  'APP_ENV': 'production'}
        parent.update(changes)
        with patch.dict(os.environ, parent, clear=True), \
             patch.object(native_integration.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            native_integration.panel_regressions(self.work / 'app', '/fixture/php', self.work)
        self.assertEqual(run.call_count, 1)
        return run.call_args.kwargs['env']

    def test_discovers_version_managed_node_before_sanitizing_parent_path(self):
        environment = self.environment()
        self.assertEqual(environment.get('NODE_BINARY'), str(self.node.resolve()))
        self.assertNotIn(str(self.bin), environment['PATH'])
        self.assertEqual(environment['PAYMENTER_BACKUPS_PLAYWRIGHT'], '/fixture/playwright')

    def test_browser_cache_overrides_survive_without_database_configuration(self):
        environment = self.environment(NODE_BINARY=str(self.node))
        for key, value in [('PLAYWRIGHT_BROWSERS_PATH', '/fixture/browsers'),
                           ('XDG_CACHE_HOME', '/fixture/cache'), ('HOME', '/fixture/home')]:
            self.assertEqual(environment.get(key), value)
        self.assertNotIn('DB_PASSWORD', environment)
        self.assertNotIn('APP_ENV', environment)

    def test_explicit_node_name_is_resolved_to_an_absolute_executable(self):
        environment = self.environment(NODE_BINARY='node')
        self.assertEqual(environment.get('NODE_BINARY'), str(self.node.resolve()))

    def test_missing_explicit_node_refuses_instead_of_selecting_another_binary(self):
        with self.assertRaisesRegex(RuntimeError, 'Node required'):
            self.environment(NODE_BINARY=str(self.work / 'missing-node'))


if __name__ == '__main__':
    unittest.main()
