"""Demo click regressions against the installed Filament schemas and engine API."""
from html.parser import HTMLParser
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

from paymenter_backups import discovery
from paymenter_backups.common import State

ROOT = Path(__file__).resolve().parents[1]
PHP = r'''
require $argv[1].'/vendor/autoload.php';
function view($name, $data = []) {
    return new class($name, $data) {
        public function __construct(private string $name, private array $data) {}
        public function render() {
            $compiler = new Illuminate\View\Compilers\BladeCompiler(new Illuminate\Filesystem\Filesystem, sys_get_temp_dir());
            $compiled = $compiler->compileString(file_get_contents($GLOBALS['argv'][2].'/extension/Backups/resources/views/'.str_replace('.', '/', substr($this->name, 9)).'.blade.php'));
            extract($this->data); ob_start(); eval('?>'.$compiled); return ob_get_clean();
        }
    };
}
$app = new Illuminate\Foundation\Application($argv[1]);
Illuminate\Support\Facades\Facade::setFacadeApplication($app);
$app->bind(Filament\Support\Contracts\LoadingIndicator::class, Filament\Support\View\DefaultLoadingIndicator::class);
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
    public static function getUrl(array $parameters = [], bool $isAbsolute = true, ?string $panel = null, ?Illuminate\Database\Eloquent\Model $tenant = null, bool $shouldGuessMissingParameters = false, ?string $configuration = null): string { return '/admin/backups'.($parameters ? '?'.http_build_query($parameters) : ''); }
    public static function downloadUrl(string $runId): string { return '/admin/backups/download/'.$runId; }
};
$input = json_decode(file_get_contents('php://stdin'), true);
$overview = ['initialized' => true, 'scheduler' => ['last_tick_at' => time()], 'runs' => $input['runs'] ?? [],
    'requests' => $input['requests'] ?? [['action' => 'destination-test', 'status' => 'complete', 'result_verified' => true, 'run_id' => null]],
    'configuration' => ['available' => true, 'valid' => true, 'settings_version' => $input['settings_version'] ?? 2, 'checks' => $input['checks'] ?? [], 'storage' => $input['storage'] ?? [],
        'readiness' => ['full-quiesced' => ['state' => 'waiting', 'items' => [['code' => 'job-lock-busy']]],
                        'database-online' => ['state' => 'ready', 'items' => []]],
        'admission' => ['full-quiesced' => ['job-lock-busy'], 'database-online' => []]]];
$overview = array_replace_recursive($overview, $input['overview'] ?? []);
(new ReflectionProperty($class, 'overviewCache'))->setValue($page, $overview);
$values = ($input['settings_version'] ?? 2) < 2
    ? Paymenter\Extensions\Others\Backups\Settings::fromLegacy($input['settings'] ?? [])
    : array_replace(Paymenter\Extensions\Others\Backups\Settings::DEFAULTS, $input['settings'] ?? []);
(new ReflectionProperty($class, 'storedForm'))->setValue($page, $values);
$call = fn ($name, ...$args) => (new ReflectionMethod($class, $name))->invoke($page, ...$args);
function flatten($components) {
    $result = [];
    foreach ($components as $component) {
        $result[] = $component;
        if (method_exists($component, 'getChildComponents')) $result = [...$result, ...flatten($component->getChildComponents())];
    }
    return $result;
}
// Panel rows and traces keep their parts private; read them as the Blade view does.
$part = fn ($object, $name) => (new ReflectionProperty($object, $name))->getValue($object);
$panelRows = function (array $sections) use ($page, $part) {
    $rows = [];
    foreach (flatten(Filament\Schemas\Schema::make($page)->components(array_map(fn ($section) => $section->toSchema(), $sections))->getComponents()) as $component) {
        if ($component instanceof Paymenter\Extensions\Others\Backups\Panel\Row) $rows[$part($component, 'rowLabel')] = $component;
    }
    return $rows;
};
if ($input['case'] === 'health') {
    echo json_encode(['setup' => $call('setupOpen'), 'full_blockers' => $call('blockers', 'full-quiesced'), 'full' => $call('typeCheck', 'full-quiesced')]);
} elseif ($input['case'] === 'queue') {
    echo json_encode($call('queueLine'));
} elseif ($input['case'] === 'details') {
    $run = ['run_id' => 'fixture-ready', 'status' => 'ready', 'mode' => 'database-online', 'created_at' => null, 'finished_at' => null, 'verified_at' => null,
            'remote' => 'none', 'local' => true, 'size' => 128, 'download' => 'ready'];
    $record = ['__key' => 'fixture-ready', 'kind' => 'run', 'run_id' => 'fixture-ready', 'run' => $run, 'origin' => 'Staff'];
    foreach ($page->table(Filament\Tables\Table::make($page))->getRecordActions() as $group) {
        if ($group instanceof Filament\Actions\ActionGroup) {
            foreach ($group->getActions() as $item) {
                if ($item->getName() === 'details') $action = $item->record($record);
            }
        }
    }
    $actions = $action->getExtraModalFooterActions();
    echo json_encode(['buttons' => array_values(array_map(fn ($a) => $a->getLabel(), $actions)),
        'url' => isset($actions['detailsDownload']) ? $actions['detailsDownload']->getUrl() : null,
        // Inline decoration avoids requiring an icon-pack provider in this isolated fixture.
        'html' => $action->icon(new Illuminate\Support\HtmlString('<svg aria-hidden="true"></svg>'))->toHtml()]);
} elseif ($input['case'] === 'detail-rows') {
    $run = $input['run'] + ['run_id' => 'fixture-run', 'mode' => 'database-online', 'created_at' => null, 'finished_at' => null, 'verified_at' => null,
            'remote' => 'none', 'remote_checked_at' => null, 'local' => true, 'size' => 128];
    $record = ['__key' => $run['run_id'], 'kind' => 'run', 'run_id' => $run['run_id'], 'run' => $run, 'origin' => 'Staff'];
    $rows = [];
    foreach (flatten(Filament\Schemas\Schema::make($page)->components($call('detailsSchema', $record))->getComponents()) as $component) {
        // Each row is its entry, then the actions beside it.
        if ($component instanceof Filament\Schemas\Components\Section) $entry = 'section ' . $component->getHeading();
        if ($component instanceof Filament\Infolists\Components\TextEntry) $entry = $component->getName();
        if ($component instanceof Filament\Actions\Action && $component->isVisible()) {
            $rows[$entry][] = ['name' => $component->getName(), 'label' => $component->getLabel(), 'url' => $component->getUrl()];
        }
    }
    $verify = [];
    foreach ($page->table(Filament\Tables\Table::make($page))->getRecordActions() as $action) {
        // The table binds the record to the group before evaluating any child.
        $action->record($record);
        $actions = $action instanceof Filament\Actions\ActionGroup ? $action->getActions() : [$action];
        foreach ($actions as $item) {
            if (in_array($item->getName(), ['verifyFirst', 'verify'], true)) $verify[$item->getName()] = $item->isVisible();
        }
    }
    echo json_encode(['rows' => $rows, 'verify' => $verify]);
} elseif ($input['case'] === 'rows') {
    // Each section's rows: the notice key, each row's state and the actions it offers.
    $out = [];
    foreach (['schedule' => 'scheduleSection', 'storage' => 'storageSection'] as $name => $method) {
        foreach ($call($method)->rows as $row) {
            $value = $part($row, 'rowValue');
            $out[$name][] = ['key' => $row->getKey(false), 'label' => $part($row, 'rowLabel'), 'state' => $value->state?->value, 'kind' => $value->kind,
                'actions' => array_map(fn ($action) => ['name' => $action->getName(), 'url' => $action->getUrl()], $part($row, 'controls'))];
        }
    }
    echo json_encode($out);
} elseif ($input['case'] === 'keep') {
    echo json_encode($part($panelRows([$call('storageSection')])['Keep'], 'rowValue')->word);
} elseif ($input['case'] === 'hints') {
    $record = ['__key' => 'fixture-run', 'kind' => 'run', 'run_id' => 'fixture-run', 'origin' => 'Staff', 'run' => ['run_id' => 'fixture-run',
        'status' => 'ready', 'mode' => 'full-quiesced', 'created_at' => null, 'finished_at' => null, 'verified_at' => null, 'remote' => 'none', 'local' => true, 'size' => 1, 'download' => 'ready']];
    $hints = [];
    $tip = new ReflectionMethod(Paymenter\Extensions\Others\Backups\Panel\Row::class, 'tip');
    foreach ($panelRows([$call('scheduleSection'), $call('storageSection')]) as $label => $row) {
        if ($tip->invoke($row) !== null) $hints[$label] = ['label' => $tip->invoke($row)];
    }
    foreach (flatten(Filament\Schemas\Schema::make($page)->components($call('detailsSchema', $record))->getComponents()) as $component) {
        if (!$component instanceof Filament\Infolists\Components\TextEntry) continue;
        foreach ($component->getHintActions() as $hint) {
            $hints[$component->getName()] = ['label' => $hint->getLabel(), 'tooltip' => $hint->getTooltip(), 'icon_button' => $hint->isIconButton(),
                'click' => $hint->getAlpineClickHandler(), 'url' => $hint->getUrl()];
        }
    }
    echo json_encode($hints);
} elseif ($input['case'] === 'history') {
    $table = $page->table(Filament\Tables\Table::make($page));
    $order = fn (?string $direction) => array_map(fn ($row) => $row['__key'], $table->evaluate($table->getDataSource(), ['page' => 1, 'recordsPerPage' => 25,
        'search' => null, 'sortColumn' => $direction ? 'created_at' : null, 'sortDirection' => $direction])->items());
    echo json_encode(['sortable' => $table->getColumn('created_at')->isSortable(), 'default' => $order(null), 'asc' => $order('asc'), 'desc' => $order('desc')]);
} elseif ($input['case'] === 'checks') {
    $settings = $page->settingsForm(Filament\Schemas\Schema::make($page));
    $sections = [];
    foreach (flatten($settings->getComponents()) as $component) {
        if ($component instanceof Filament\Schemas\Components\Section && $component->getId() === 'full-backup-checks') $sections[] = ['title' => $component->getHeading(), 'description' => $component->getDescription()];
    }
    $serialize = function ($components) {
        return array_map(function ($component) {
            $html = [];
            foreach (flatten([$component]) as $child) {
                if ($child instanceof Filament\Schemas\Components\Html) $html[] = (string) $child->getContent();
                if ($child instanceof Filament\Schemas\Components\Text) $html[] = '<div class="command" '.new Illuminate\View\ComponentAttributeBag($child->getExtraAttributes()).'>'.e((string) $child->getContent()).'</div>';
            }
            return ['attributes' => $component->getExtraAttributes(), 'children' => array_map(fn ($child) => $child->getExtraAttributes(), $component->getChildComponents()), 'html' => $html];
        }, $components);
    };
    $checks = $call('checksSection');
    $steps = array_map(fn ($step) => ['label' => $step['label'], 'state' => $step['state']?->value], $part($checks->rows[0], 'steps'));
    echo json_encode(['sections' => $sections, 'backups' => ['steps' => $steps, 'link' => $checks->link?->getUrl()],
        'diagnostics' => $serialize(Filament\Schemas\Schema::make($page)->components($call('detectedChecks'))->getComponents())]);
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


@unittest.skipUnless(os.environ.get('PHP_BINARY') and os.environ.get('PAYMENTER_TEST_APP'),
                     'PHP_BINARY and PAYMENTER_TEST_APP required')
class ClickReview(unittest.TestCase):
    def page(self, case, **input):
        result = subprocess.run([os.environ['PHP_BINARY'], '-r', PHP, os.environ['PAYMENTER_TEST_APP'], str(ROOT)], input=json.dumps({'case': case, **input}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_cron_lock_wait_does_not_open_setup_but_remains_waiting(self):
        result = self.page('health')
        self.assertFalse(result['setup'], 'An ordinary cron tick must not open Setup needed')
        self.assertEqual(result['full_blockers'], [])
        self.assertEqual(result['full']['state'], 'queued')

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

    def test_details_offers_download_and_closes_the_row_menu(self):
        result = self.page('details')
        # Verify sits in the Check row, as in the mock; the footer keeps Download only.
        self.assertEqual(result['buttons'], ['Download'])
        class Item(HTMLParser):
            attributes = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'button' and 'fi-ac-grouped-action' in attrs.get('class', '').split():
                    self.attributes = attrs

        item = Item()
        item.feed(result['html'])
        self.assertIsNotNone(item.attributes, result['html'])
        self.assertEqual(item.attributes.get('x-on:click.capture'), 'close()')
        self.assertIn("mountAction('details'", item.attributes.get('wire:click', ''))

    def detail_rows(self, **run):
        return self.page('detail-rows', run=run)['rows']

    def test_details_offer_verify_in_check_and_set_up_s3_beside_s3_off(self):
        rows = self.detail_rows(status='ready', download='ready')
        self.assertEqual([item['label'] for item in rows['d_verified']], ['Verify'])
        self.assertEqual(rows['d_s3'], [{'name': 'detailsSetUpS3', 'label': 'Set up S3', 'url': '/admin/backups?tab=settings#s3'}])

    def test_keep_summary_counts_follow_the_stored_settings_schema(self):
        settings = dict(retention_keep=7, retention_keep_full=3, retention_keep_database=11)
        for version, counts in ((1, ['7']), (2, ['3', '11'])):
            with self.subTest(version=version):
                summary = self.page('keep', settings_version=version, settings=settings)
                self.assertEqual(re.findall(r'\d+', summary), counts)

    def test_details_offer_verify_wherever_the_row_does(self):
        # A damaged copy shows Verify on its row; its Details must offer it too.
        for run in ({'status': 'damaged', 'download': 'none'}, {'status': 'ready', 'download': 'none'},
                    {'status': 'ready', 'download': 'retrieve', 'local': False, 'remote': 'delivered'},
                    {'status': 'damaged', 'download': 'retrieve', 'local': False, 'remote': 'delivered'}):
            with self.subTest(run=run):
                rows = self.detail_rows(**run)
                self.assertEqual([item['name'] for item in (rows or {}).get('d_verified', [])], ['detailsVerify'])
                # The fixture has S3 off; only copies without an S3 copy offer Set up S3.
                expected_s3_actions = [] if run.get('remote') == 'delivered' else [
                    {'name': 'detailsSetUpS3', 'label': 'Set up S3', 'url': '/admin/backups?tab=settings#s3'}]
                self.assertEqual(rows.get('d_s3', []), expected_s3_actions)

    def test_verify_menu_complements_the_row_and_suppresses_active_requests(self):
        for status in ('ready', 'damaged', 'failed'):
            for download in ('none', 'ready', 'retrieve'):
                run = {'run_id': 'fixture-run', 'status': status, 'download': download,
                       'local': download != 'retrieve', 'remote': 'delivered' if download == 'retrieve' else 'none'}
                for request_status in (None, 'pending', 'deferred', 'launching', 'running', 'failed'):
                    with self.subTest(run=run, request_status=request_status):
                        requests = [{'action': 'verify', 'status': request_status, 'run_id': run['run_id']}] if request_status else []
                        result = self.page('detail-rows', run=run, requests=requests)
                        eligible = status in ('ready', 'damaged') and request_status not in ('pending', 'deferred', 'launching', 'running')
                        self.assertEqual(result['verify'], {'verifyFirst': eligible and download == 'none',
                                                           'verify': eligible and download != 'none'})
                        self.assertEqual(bool((result['rows'] or {}).get('d_verified')), eligible)

    def test_question_hints_explain_types_and_keep_without_a_request(self):
        hints = self.page('hints', settings_version=2)
        self.assertEqual(set(hints), {'Full backups', 'Database backups', 'Keep', 'd_type'})
        # Each type's row and a backup of that type explain it the same way.
        self.assertEqual(hints['Full backups']['label'], hints['d_type']['label'])
        self.assertNotEqual(hints['Full backups']['label'], hints['Database backups']['label'])
        for name in ('d_type',):
            hint = hints[name]
            with self.subTest(hint=name):
                # The label is the button's accessible name; the tooltip shows the same words.
                self.assertEqual(hint['tooltip'], hint['label'])
                self.assertTrue(hint['icon_button'])
                # A tap focuses the button, which opens the tooltip, and sends nothing to the server.
                self.assertEqual(hint['click'], '$el.focus()')
                self.assertIsNone(hint['url'])

    def test_keep_hint_shows_only_when_some_place_keeps_copies(self):
        for storage, shown in (({'keep_local': True, 'upload_s3': False}, True), ({'keep_local': True, 'upload_s3': True}, True),
                               ({'keep_local': False, 'upload_s3': True}, True), ({'keep_local': False, 'upload_s3': False}, False)):
            for version in (1, 2):
                with self.subTest(settings_version=version, storage=storage):
                    self.assertEqual('Keep' in self.page('hints', settings_version=version, storage=storage), shown)

    def rows(self, **input):
        return {name: {row['label'] or row['key']: row for row in rows} for name, rows in self.page('rows', **input).items()}

    def test_a_running_backup_or_lock_problem_never_hides_a_stalled_database_capture(self):
        running = [{'run_id': 'fixture-running', 'request_id': None, 'status': 'running', 'mode': 'full-quiesced', 'created_at': 1_700_000_000,
                    'finished_at': None, 'verified_at': None, 'remote': 'none', 'local': True, 'size': None, 'download': 'none'}]
        for coordination in ('unverified', 'absent'):
            with self.subTest(coordination=coordination):
                schedule = self.rows(runs=running, overview={'job_wait_overdue': True, 'maintenance': True,
                                     'database_job_coordination': {'state': coordination, 'error': None}})['schedule']
                self.assertEqual(schedule['notice-running']['state'], 'running')
                self.assertEqual(schedule['Database backups']['state'], 'needs_review')
                self.assertEqual(schedule['Lock files']['state'], 'needs_review')
                self.assertEqual([action['name'] for action in schedule['Database backups']['actions']], ['scheduleDatabaseChange'])
        quiet = self.rows(overview={'database_job_coordination': {'state': 'coordinated'}})['schedule']
        self.assertNotIn('Lock files', quiet)
        self.assertEqual(quiet['Database backups']['kind'], 'plain')
        self.assertEqual([row['key'] for row in self.page('rows', overview={'maintenance': True})['schedule']][0], 'notice-maintenance')

    def test_a_full_disk_never_hides_waiting_uploads_plain_s3_copies_or_skipped_cleanup(self):
        storage = self.rows(settings_version=1, storage={'keep_local': True, 'upload_s3': True, 's3_bucket': 'fixture-bucket'}, overview={
            's3_pending': {'count': 2}, 'configuration': {'space': {'state': 'full', 'used': 10, 'budget': 10}, 'encryption': {'enabled': False},
                                                         'retention': {'last_apply': {'skipped_count': 3}}}})['storage']
        self.assertEqual(storage['notice-space']['state'], 'failed')
        self.assertEqual(storage['notice-space']['actions'], [{'name': 'alertSpace', 'url': '/admin/backups?tab=settings#storage'}])
        self.assertEqual(storage['This server']['state'], 'needs_review')
        self.assertEqual(storage['S3']['state'], 'needs_review')
        self.assertEqual(storage['S3']['actions'], [{'name': 'alertEncryption', 'url': '/admin/backups?tab=settings#s3'}])
        self.assertEqual(storage['Keep']['state'], 'needs_review')
        calm = self.rows(storage={'keep_local': True, 'upload_s3': False})['storage']
        self.assertFalse(any(key.startswith('notice-') for key in calm))
        self.assertEqual({label: row['state'] for label, row in calm.items()}, {'This server': None, 'S3': None, 'Keep': None})

    def test_started_sorts_both_ways_and_defaults_to_newest_first(self):
        runs = [{'run_id': run_id, 'request_id': None, 'status': 'ready', 'mode': 'database-online', 'created_at': created,
                 'finished_at': None, 'verified_at': None, 'remote': 'none', 'local': True, 'size': 1, 'download': 'ready'}
                for run_id, created in (('older', 1_700_000_000), ('newest', 1_700_000_200), ('middle', 1_700_000_100))]
        result = self.page('history', runs=runs)
        self.assertTrue(result['sortable'])
        self.assertEqual(result['default'], ['newest', 'middle', 'older'])
        self.assertEqual(result['desc'], ['newest', 'middle', 'older'])
        self.assertEqual(result['asc'], ['older', 'middle', 'newest'])

    def test_settings_commands_can_shrink_and_schedule_columns_start_at_top(self):
        command = '/usr/bin/flock -n /fixture/paymenter-job-locks/scheduler.lock /usr/bin/php /app/artisan schedule:run'
        result = self.page('settings', checks={'jobs': {'state': 'ok', 'items': [{'kind': 'scheduler', 'coordination': 'coordinated', 'command': command, 'lock': '/fixture/paymenter-job-locks/scheduler.lock'}]}})
        self.assertIn('min-width: 0', result['command'].get('style', ''))
        self.assertEqual(len(result['columns']), 2)
        self.assertTrue(all('align-content: start' in col.get('style', '') for col in result['columns']))

    def test_diagnostics_share_readiness_rows_and_link_from_full_backups(self):
        command = '/usr/bin/flock -n /fixture/scheduler.lock /usr/bin/php /app/artisan schedule:run'
        result = self.page('checks', checks={'jobs': {'state': 'ok', 'items': [
            {'kind': 'scheduler', 'coordination': 'coordinated', 'command': command, 'lock': '/fixture/scheduler.lock'}]},
            'stores': [{'kind': 'cache', 'driver': 'redis', 'supported': False, 'env': 'CACHE_STORE', 'suggest': 'file'}]})
        self.assertEqual(len(result['sections']), 1)
        self.assertIn({'label': 'Full backups', 'state': 'queued'}, result['backups']['steps'])
        self.assertEqual(result['backups']['link'], '/admin/backups?tab=settings#full-backup-checks')
        self.assertEqual(len(result['diagnostics']), 1)
        diagnostics = ''.join(result['diagnostics'][0]['html'])
        self.assertIn('OK', diagnostics)
        self.assertIn('Needs setup', diagnostics)
        self.assertIn(command, diagnostics)
        self.assertIn('CACHE_STORE=file', diagnostics)
        self.assertIn('tabindex="0"', diagnostics)

    def test_diagnostics_keep_unknown_failed_and_stale_inventory_visible(self):
        for jobs, expected in ((None, 'Not checked'), ({'state': 'failed'}, 'Failed'),
                               ({'state': 'stale', 'items': []}, 'Stale')):
            with self.subTest(jobs=jobs):
                result = self.page('checks', checks={'jobs': jobs})
                html = ''.join(result['diagnostics'][0]['html'])
                self.assertIn(expected, html)
