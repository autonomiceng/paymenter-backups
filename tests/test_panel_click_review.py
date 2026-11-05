"""Demo click regressions against the installed Filament schemas and engine API."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from paymenter_backups import discovery
from paymenter_backups.common import State

ROOT = Path(__file__).resolve().parents[1]
PHP = r'''
require $argv[1].'/vendor/autoload.php';
$app = new Illuminate\Foundation\Application($argv[1]);
$app->instance('config', new Illuminate\Config\Repository(['app' => ['locale' => 'en']]));
$app->instance('translator', new Illuminate\Translation\Translator(new Illuminate\Translation\ArrayLoader, 'en'));
$app->instance('request', Illuminate\Http\Request::create('https://fixture.invalid/admin/backups'));
spl_autoload_register(function ($class) use ($argv) {
    $prefix = 'Paymenter\\Extensions\\Others\\Backups\\';
    if (str_starts_with($class, $prefix)) {
        require $argv[2].'/extension/Backups/'.str_replace('\\', '/', substr($class, strlen($prefix))).'.php';
    }
});
$class = Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::class;
$page = new class extends Paymenter\Extensions\Others\Backups\Admin\Pages\Backups {
    public static function getUrl(array $parameters = [], bool $isAbsolute = true, ?string $panel = null, ?Illuminate\Database\Eloquent\Model $tenant = null, bool $shouldGuessMissingParameters = false, ?string $configuration = null): string { return '/admin/backups'; }
    public static function downloadUrl(string $runId): string { return '/admin/backups/download/'.$runId; }
};
$input = json_decode(file_get_contents('php://stdin'), true);
$overview = ['initialized' => true, 'scheduler' => ['last_tick_at' => time()], 'runs' => [],
    'requests' => [['action' => 'destination-test', 'status' => 'complete', 'result_verified' => true]],
    'configuration' => ['available' => true, 'valid' => true, 'checks' => $input['checks'] ?? [],
        'readiness' => ['full-quiesced' => ['state' => 'waiting', 'items' => [['code' => 'job-lock-busy']]],
                        'database-online' => ['state' => 'ready', 'items' => []]],
        'admission' => ['full-quiesced' => ['job-lock-busy'], 'database-online' => []]]];
(new ReflectionProperty($class, 'overviewCache'))->setValue($page, $overview);
(new ReflectionProperty($class, 'storedForm'))->setValue($page, Paymenter\Extensions\Others\Backups\Settings::DEFAULTS);
$call = fn ($name, ...$args) => (new ReflectionMethod($class, $name))->invoke($page, ...$args);
function flatten($components) {
    $result = [];
    foreach ($components as $component) {
        $result[] = $component;
        if (method_exists($component, 'getChildComponents')) $result = [...$result, ...flatten($component->getChildComponents())];
    }
    return $result;
}
if ($input['case'] === 'health') {
    echo json_encode(['setup' => $call('setupOpen'), 'full_blockers' => $call('blockers', 'full-quiesced'), 'full' => $call('typeCheck', 'full-quiesced')]);
} elseif ($input['case'] === 'queue') {
    echo json_encode($call('queueLine'));
} elseif ($input['case'] === 'details') {
    $run = ['run_id' => 'fixture-ready', 'status' => 'ready', 'mode' => 'database-online', 'created_at' => null, 'finished_at' => null, 'verified_at' => null,
            'remote' => 'none', 'local' => true, 'size' => 128, 'download' => 'ready'];
    $record = ['kind' => 'run', 'run_id' => 'fixture-ready', 'run' => $run, 'origin' => 'Staff'];
    $action = $page->table(Filament\Tables\Table::make($page))->getFlatRecordActions()['details']->record($record);
    $actions = $action->getExtraModalFooterActions();
    echo json_encode(['buttons' => array_values(array_map(fn ($a) => $a->getLabel(), $actions)),
        'url' => isset($actions['detailsDownload']) ? $actions['detailsDownload']->getUrl() : null,
        'attributes' => $action->getExtraAttributes()]);
} elseif ($input['case'] === 'settings') {
    $rows = Filament\Schemas\Schema::make($page)->components($call('detectedChecks'))->getComponents();
    $command = null;
    foreach (flatten($rows) as $component) {
        if ($component instanceof Filament\Schemas\Components\Text && str_contains((string) $component->getContent(), '/usr/bin/flock')) $command = $component->getExtraAttributes();
    }
    $schema = $page->settingsForm(Filament\Schemas\Schema::make($page));
    $columns = [];
    foreach (flatten($schema->getComponents()) as $component) {
        if ($component instanceof Filament\Schemas\Components\Group) {
            $children = $component->getChildComponents();
            if (isset($children[0]) && $children[0] instanceof Filament\Schemas\Components\Text && in_array((string) $children[0]->getContent(), ['Full backups', 'Database backups'])) $columns[] = $component->getExtraAttributes();
        }
    }
    echo json_encode(['command' => $command, 'columns' => $columns]);
}
'''


class ClickReview(unittest.TestCase):
    def page(self, case, **input):
        result = subprocess.run([os.environ['PHP_BINARY'], '-r', PHP, os.environ['PAYMENTER_TEST_APP'], str(ROOT)], input=json.dumps({'case': case, **input}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_cron_lock_wait_does_not_open_setup_but_remains_waiting(self):
        result = self.page('health')
        self.assertFalse(result['setup'], 'An ordinary cron tick must not open Setup needed')
        self.assertEqual(result['full_blockers'], [])
        self.assertEqual(result['full']['value'], 'Waiting')

    def test_help_queue_command_uses_the_detected_capture_lock(self):
        with tempfile.TemporaryDirectory(prefix='queue-help-') as scratch:
            base = Path(scratch)
            (base/'app').mkdir()
            state = State(base/'app', base/'private')
            lock = base/'actual-queue.lock'
            lock.touch(mode=0o600)
            line = f'* * * * * /usr/bin/flock -n {lock} /usr/bin/php {state.app}/artisan queue:work --stop-when-empty --max-time=50'
            job = discovery.record(line, state.app, 'crontab', 'line 1')
            inventory = {'state': 'ok', 'items': [job]}
            command = discovery.suggestions(state, {'tools': {'php': '/usr/bin/php'}}, inventory)['queue']
            shown = self.page('queue', checks={'jobs': dict(inventory, suggestions={'queue': command})})
            self.assertEqual(discovery.parse(shown, state.app)['lock'], str(lock), 'Help must use the lock that capture coordinates')

    def test_details_offers_verify_download_and_closes_the_row_menu(self):
        result = self.page('details')
        self.assertIn('Verify', result['buttons'])
        self.assertIn('Download', result['buttons'])
        self.assertIn('close', result['attributes'].get('x-on:click', ''))

    def test_settings_commands_can_shrink_and_schedule_columns_start_at_top(self):
        command = '/usr/bin/flock -n /fixture/paymenter-job-locks/scheduler.lock /usr/bin/php /app/artisan schedule:run'
        result = self.page('settings', checks={'jobs': {'state': 'ok', 'items': [{'kind': 'scheduler', 'coordination': 'coordinated', 'command': command, 'lock': '/fixture/paymenter-job-locks/scheduler.lock'}]}})
        self.assertIn('min-width: 0', result['command'].get('style', ''))
        self.assertEqual(len(result['columns']), 2)
        self.assertTrue(all('align-content: start' in col.get('style', '') for col in result['columns']))
