"""Scheduler commands shown by the native page preserve the detected launcher."""
import json
import pathlib
import shlex
import shutil
import subprocess
import unittest

from paymenter_backups import discovery
from guarded_scheduler_fixture import guarded_host


ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = shutil.which('php')
PROGRAM = r'''
namespace Paymenter\Extensions\Others\Backups {
    class Runtime {
        public static function privateRoot(): string { return '/private/default'; }
    }
}
namespace {
    function base_path($path = '') {
        return $GLOBALS['input']['app'] . ($path === '' ? '' : '/' . $path);
    }
    require $argv[1] . '/extension/Backups/Admin/Pages/Concerns/BuildsCommands.php';
    class SchedulerPage {
        use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsCommands {
            schedulerLine as public schedulerSuggestion;
        }
        private function checks(): array { return $GLOBALS['input']['checks']; }
        private function formValues(): array { return ['php' => '/fixture/php']; }
    }
    $GLOBALS['input'] = json_decode(file_get_contents('php://stdin'), true, 64, JSON_THROW_ON_ERROR);
    echo json_encode((new SchedulerPage)->schedulerSuggestion(), JSON_THROW_ON_ERROR);
}
'''


@unittest.skipUnless(PHP, 'PHP CLI required for scheduler commands')
class GuardedSchedulerUI(unittest.TestCase):
    COMMAND = "/usr/bin/flock -n '/private/existing jobs.lock' /usr/local/bin/php /opt/beco/scheduler.php"

    def setUp(self):
        self.enterContext(guarded_host())

    def scheduler_line(self, app='/app', items=None, suggestion=None):
        jobs = {'items': items or []}
        if suggestion is not None:
            jobs['suggestions'] = {'scheduler': suggestion}
        result = subprocess.run(
            [PHP, '-r', PROGRAM, str(ROOT)],
            input=json.dumps({'app': app, 'checks': {'jobs': jobs}}),
            capture_output=True, text=True, check=True, timeout=5,
        )
        return json.loads(result.stdout)

    def guarded_item(self, **changes):
        return dict({'kind': 'scheduler', 'coordination': 'coordinated',
                     'command': self.COMMAND, 'lock': '/private/existing jobs.lock'}, **changes)

    def test_engine_scheduler_suggestion_passes_through_unchanged(self):
        suggestion = '* * * * * /usr/bin/flock -n /private/engine.lock /usr/local/bin/php /opt/beco/scheduler.php >/dev/null 2>&1'
        self.assertEqual(self.scheduler_line(items=[self.guarded_item()], suggestion=suggestion), suggestion)

    def test_guarded_fallback_preserves_exact_normalized_command_and_lock(self):
        self.assertEqual(
            self.scheduler_line(items=[self.guarded_item()]),
            "* * * * * /usr/bin/flock -n '/private/existing jobs.lock' /usr/local/bin/php /opt/beco/scheduler.php >/dev/null 2>&1",
        )

    def test_guarded_fallback_escapes_percent_and_backslash_percent_locks(self):
        for lock in ('/private/jobs%archive.lock', r'/private/jobs\%archive.lock'):
            with self.subTest(lock=lock):
                command = shlex.join(['/usr/bin/flock', '-n', lock,
                                      '/usr/local/bin/php', '/opt/beco/scheduler.php'])
                line = self.scheduler_line(items=[self.guarded_item(command=command, lock=lock)])
                parsed = discovery.parse(line, pathlib.Path('/app'))
                self.assertEqual(parsed['lock'], lock)
                self.assertEqual(parsed['command'], command)

    def test_different_app_fallback_stays_artisan(self):
        self.assertEqual(
            self.scheduler_line(app='/other/app', items=[self.guarded_item()]),
            '* * * * * /usr/bin/flock -n /private/default/scheduler.lock /fixture/php /other/app/artisan schedule:run >/dev/null 2>&1',
        )

    def test_other_inventory_items_keep_ordinary_artisan_fallback(self):
        for items in ([], [self.guarded_item(kind='queue-worker')],
                      [self.guarded_item(coordination='unsupported')],
                      [self.guarded_item(lock=None)], [self.guarded_item(lock='')],
                      [self.guarded_item(lock=42)],
                      [self.guarded_item(command=self.COMMAND + ' --quiet')]):
            with self.subTest(items=items):
                self.assertEqual(
                    self.scheduler_line(items=items),
                    '* * * * * /usr/bin/flock -n /private/default/scheduler.lock /fixture/php /app/artisan schedule:run >/dev/null 2>&1',
                )
