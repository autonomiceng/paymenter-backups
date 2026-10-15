<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages;

use Carbon\CarbonImmutable;
use Filament\Actions\Action;
use Filament\Actions\ActionGroup;
use Filament\Forms\Components\Checkbox;
use Filament\Forms\Components\CheckboxList;
use Filament\Forms\Components\Radio;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\TextInput;
use Filament\Forms\Components\Textarea;
use Filament\Forms\Components\Toggle;
use Filament\Infolists\Components\TextEntry;
use Filament\Notifications\Notification;
use Filament\Pages\Page;
use Filament\Pages\PageConfiguration;
use Filament\Panel;
use Filament\Schemas\Components\Actions;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\EmbeddedSchema;
use Filament\Schemas\Components\EmbeddedTable;
use Filament\Schemas\Components\Fieldset;
use Filament\Schemas\Components\Form;
use Filament\Schemas\Components\Grid;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Components\Tabs;
use Filament\Schemas\Components\Tabs\Tab;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\Utilities\Get;
use Filament\Schemas\Schema;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Facades\FilamentView;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Concerns\InteractsWithTable;
use Filament\Tables\Contracts\HasTable;
use Filament\Tables\Filters\Filter;
use Filament\Tables\Table;
use Illuminate\Pagination\LengthAwarePaginator;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Route;
use Illuminate\Support\HtmlString;
use Illuminate\Validation\ValidationException;
use Paymenter\Extensions\Others\Backups\Backups as Extension;
use Paymenter\Extensions\Others\Backups\Http\DownloadController;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\PrivateStore;
use Paymenter\Extensions\Others\Backups\Runtime;
use Paymenter\Extensions\Others\Backups\Settings;
use Paymenter\Extensions\Others\Backups\SettingsWriter;

class Backups extends Page implements HasTable
{
    use InteractsWithTable;

    protected static string|\BackedEnum|null $navigationIcon = 'ri-save-line';
    protected static string|\BackedEnum|null $activeNavigationIcon = 'ri-save-fill';
    protected static string|\UnitEnum|null $navigationGroup = 'System';
    protected static ?string $title = 'Backups';
    /** Native page that renders action modals before the tabs, not inside the Backups tab with the history table. */
    protected string $view = 'backups::page';

    private const MIB = 1048576;
    private const MIB_FIELDS = ['local_byte_budget', 'minimum_free_bytes'];
    private const LINE_LISTS = ['job_lock_paths', 'extra_docroots'];
    /** Paths the engine needs before it can check anything else. */
    private const BOOTSTRAP = ['private_root', 'extra_docroots', 'python', 'php'];
    private const ENVIRONMENT_ERRORS = ['python-binary-invalid', 'php-binary-mismatch', 'private-root-invalid', 'private-root-unavailable', 'document-roots-invalid', 'engine-unavailable', 'installation-mismatch', 'owner-mismatch', 'private-state-permissions'];
    /** Admission results that only delay a queued backup. */
    private const TRANSIENT = ['billing-window-protected', 'job-lock-busy', 'busy'];
    private const SCHEDULER_STALE_SECONDS = 300;
    /** Admission codes an administrator settles in Settings, under Host checks. */
    private const HOST_CHECKS = ['job-locks-unverified', 'cooperative-writers-acknowledged', 'external-state-acknowledged', 'no-concurrent-ddl-acknowledgement-required'];

    /** Nonsecret settings form state. */
    public ?array $settings = [];
    /** Write-only. Reset on every response. */
    public ?array $s3Keys = [];
    /** Write-only. Reset on every response. The site address comes from Paymenter, never the browser. */
    public ?array $siteAccess = [];

    private ?array $overviewCache = null;
    /** Current native rows in form shape; the server-side baseline for change detection. */
    private ?array $storedForm = null;
    private ?string $overviewError = null;
    private array $deletePreviews = [];

    public static function canAccess(): bool
    {
        return Auth::user()?->hasPermission('admin.extensions.update') ?? false;
    }

    private function authorizeAction(): void { abort_unless(static::canAccess(), 403); }

    /** The download route lives in the panel's authenticated route group. */
    public static function routes(Panel $panel, ?PageConfiguration $configuration = null): void
    {
        parent::routes($panel, $configuration);
        Route::get(static::getRoutePath($panel) . '/download/{run}', DownloadController::class)
            ->middleware(static::getRouteMiddleware($panel))
            ->withoutMiddleware(static::getWithoutRouteMiddleware($panel))
            ->where('run', '[A-Za-z0-9_-]{1,100}')
            ->name(static::getRelativeRouteName($panel) . '.download');
    }

    /** Excluded from SPA navigation, which would load the attachment as a page instead of saving it. Action evaluates the URL before rendering the anchor. */
    public static function downloadUrl(string $runId): string
    {
        $url = route(static::getRouteName() . '.download', ['run' => $runId]);
        FilamentView::spaUrlExceptions([$url]);
        return $url;
    }

    public function mount(): void
    {
        $this->authorizeAction();
        $this->settingsForm->fill($this->formValues());
        $this->s3KeysForm->fill();
        $this->siteAccessForm->fill();
    }

    /** Secrets never travel back to the browser, including after validation errors. */
    public function dehydrate(): void
    {
        $this->s3Keys = ['access_key' => null, 'secret_key' => null, 'session_token' => null];
        $this->siteAccess = ['username' => null, 'password' => null];
    }

    /** Validation errors can sit in a collapsed section; say so where the person is looking. */
    protected function onValidationError(ValidationException $exception): void
    {
        parent::onValidationError($exception);
        Notification::make()->title('Check the highlighted fields')->danger()->send();
    }

    // ---------------------------------------------------------------- Engine data

    private function overview(): array
    {
        if ($this->overviewCache !== null) { return $this->overviewCache; }
        try {
            return $this->overviewCache = Runtime::command(['overview'], null, 20);
        } catch (\Throwable $error) {
            $this->overviewError = $error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable';
            return $this->overviewCache = ['initialized' => false, 'runs' => [], 'requests' => [], 'scheduler' => ['last_tick_at' => null],
                'configuration' => ['available' => false, 'valid' => false, 'error' => $this->overviewError], 'maintenance' => false, 'installation_uuid' => null];
        }
    }

    private function configuration(): array { return $this->overview()['configuration'] ?? []; }

    /** True when private state exists, even if the engine cannot report it right now. */
    private function initialized(): bool
    {
        if ($this->overview()['initialized'] ?? false) { return true; }
        try { return @is_file(Runtime::privateRoot() . '/installation.json'); } catch (\Throwable $error) { return false; }
    }

    private function record(): \App\Models\Extension
    {
        return \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->firstOrFail();
    }

    private function time(?float $timestamp, bool $withZone = false): string
    {
        if ($timestamp === null) { return 'Unknown'; }
        $zone = config('app.timezone') ?: 'UTC';
        $date = CarbonImmutable::createFromTimestamp((int) $timestamp)->setTimezone($zone);
        return $date->format('j M Y, H:i') . ($withZone ? ' ' . $zone : '');
    }

    private function ago(?float $timestamp): string
    {
        return $timestamp === null ? '' : CarbonImmutable::createFromTimestamp((int) $timestamp)->diffForHumans();
    }

    /** Active request for a backup, keyed by action, from the engine's whitelist. */
    private function tasksFor(string $runId): array
    {
        $tasks = [];
        foreach ($this->overview()['requests'] as $request) {
            if ($request['run_id'] === $runId) { $tasks[] = $request; }
        }
        return $tasks;
    }

    /** History rows: queued or refused backup requests, then backups. */
    private function historyRows(bool $showDeleted): array
    {
        $configuration = $this->configuration();
        $configuredMode = $configuration['mode'] ?? null;
        $rows = [];
        $started = [];
        foreach ($this->overview()['runs'] as $run) {
            if ($run['request_id']) { $started[$run['request_id']] = true; }
        }
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] !== 'run' || isset($started[$request['request_id']]) || $request['result_run_id']) { continue; }
            if (in_array($request['status'], ['complete', 'cancelled'], true)) { continue; }
            $active = in_array($request['status'], ['pending', 'deferred', 'launching', 'running'], true);
            [$label, $color] = $active ? Messages::taskStatus($request['status']) : ['Not started', 'danger'];
            if ($request['status'] === 'pending') { $label = 'Queued'; }
            $detail = match ($request['status']) {
                'pending' => 'Waiting for Paymenter\'s scheduler.',
                'deferred' => 'Will retry. ' . Messages::error($request['error']),
                'launching', 'running' => 'Starting now.',
                'expired' => 'The scheduler did not start it in time.',
                default => Messages::error($request['error']),
            };
            $rows[] = ['__key' => 'request-' . $request['request_id'], 'kind' => 'request', 'request_id' => $request['request_id'],
                'created_at' => $request['created_at'], 'type' => Messages::mode($request['mode'] ?: $configuredMode) . ($request['scheduled'] ? ', scheduled' : ''),
                'size' => null, 'size_label' => 'Not yet', 'status_label' => $label, 'status_color' => $color, 'detail' => $detail, 'location' => 'Not stored',
                'cancel_request' => in_array($request['status'], ['pending', 'deferred'], true) ? $request['request_id'] : null,
                'run_id' => null, 'run' => null];
        }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'deleted' && !$showDeleted) { continue; }
            [$label, $color] = Messages::runStatus($run);
            $detail = Messages::runDetail($run);
            $cancel = null;
            $tasks = $this->tasksFor($run['run_id']);
            $active = array_values(array_filter($tasks, static fn ($task) => in_array($task['status'], ['pending', 'deferred', 'launching', 'running'], true)));
            if ($active) {
                $task = $active[0];
                $state = match ($task['status']) { 'pending' => 'waiting for the scheduler', 'deferred' => 'will retry', default => 'running' };
                $detail = (Messages::TASKS[$task['action']] ?? 'Task') . ' ' . $state . '.' . ($detail ? ' ' . $detail : '');
                if (in_array($task['status'], ['pending', 'deferred'], true)) { $cancel = $task['request_id']; }
            } elseif ($run['status'] === 'ready') {
                // Only the newest finished delete matters; a later success hides an older failure.
                $latest = null;
                foreach ($tasks as $task) {
                    if ($task['action'] === 'delete' && ($latest === null || $task['created_at'] > $latest['created_at'])) { $latest = $task; }
                }
                if ($latest && $latest['status'] === 'failed') { $detail = 'Not deleted. ' . Messages::error($latest['error']); }
            }
            $rows[] = ['__key' => $run['run_id'], 'kind' => 'run', 'request_id' => null, 'created_at' => $run['created_at'],
                'type' => Messages::mode($run['mode']), 'size' => $run['size'], 'size_label' => $run['size'] === null ? 'None' : Messages::bytes($run['size']), 'status_label' => $label, 'status_color' => $color,
                'detail' => $detail, 'location' => Messages::location($run, $configuration), 'cancel_request' => $cancel,
                'run_id' => $run['run_id'], 'run' => $run];
        }
        usort($rows, static fn ($a, $b) => ($b['created_at'] ?? 0) <=> ($a['created_at'] ?? 0));
        return $rows;
    }

    private function hasActiveWork(): bool
    {
        foreach ($this->overview()['requests'] as $request) {
            if (in_array($request['status'], ['pending', 'deferred', 'launching', 'running'], true)) { return true; }
        }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'running') { return true; }
        }
        return false;
    }

    private function activeRequest(string $action, ?string $runId = null): ?array
    {
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] === $action && $request['run_id'] === $runId && in_array($request['status'], ['pending', 'deferred', 'launching', 'running'], true)) { return $request; }
        }
        return null;
    }

    private function latestRequest(string $action): ?array
    {
        $latest = null;
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] === $action && ($latest === null || $request['created_at'] > $latest['created_at'])) { $latest = $request; }
        }
        return $latest;
    }

    /** Blocking admission problems for a mode, as [code => message]. */
    private function blockers(string $mode): array
    {
        $configuration = $this->configuration();
        if (!($configuration['available'] ?? false)) { return [$configuration['error'] ?? 'engine-unavailable' => Messages::error($configuration['error'] ?? 'engine-unavailable')]; }
        if (!($configuration['valid'] ?? false)) { return [$configuration['error'] => Messages::error($configuration['error'])]; }
        $issues = [];
        foreach ($configuration['admission'][$mode] ?? [] as $code) {
            if (!in_array($code, self::TRANSIENT, true)) { $issues[$code] = Messages::error($code); }
        }
        if (($configuration['destination']['type'] ?? null) === 's3' && !($configuration['saved_keys']['s3'] ?? false)) {
            $issues['s3-credentials-required'] = Messages::error('s3-credentials-required');
        }
        return $issues;
    }

    private function schedulerFresh(): bool
    {
        $last = $this->overview()['scheduler']['last_tick_at'] ?? null;
        return $last !== null && time() - $last < self::SCHEDULER_STALE_SECONDS;
    }

    /** One sentence about the scheduler, shared by the checklist and the Create backup dialog. */
    private function schedulerSentence(): string
    {
        $last = $this->overview()['scheduler']['last_tick_at'] ?? null;
        return $last ? 'Paymenter\'s scheduler last ran ' . $this->ago($last) . '. Check its cron job on the Help tab.'
            : 'Paymenter\'s scheduler has not run yet. Add its cron job from the Help tab.';
    }

    /**
     * Short sentences for what stops a backup type, with host checks counted
     * once as one action and the scheduler named once.
     */
    private function issueSentences(string $mode, bool $withHostChecks = true): array
    {
        $blockers = $this->blockers($mode);
        $host = $withHostChecks ? array_values(array_intersect(array_keys($blockers), self::HOST_CHECKS)) : [];
        $sentences = [];
        foreach ($blockers as $code => $message) {
            if (in_array($code, self::HOST_CHECKS, true)) { continue; }
            $sentences[] = $code === 'billing-guard-stale' ? $this->schedulerSentence() : $message;
        }
        if ($host === ['no-concurrent-ddl-acknowledgement-required']) {
            $sentences[] = 'Confirm in Settings, under Host checks, that database schema changes never run during a backup.';
        } elseif ($host) {
            $count = count($host);
            $sentences[] = ($mode === 'full-quiesced' ? 'Full backups' : 'Backups') . ' need ' . $count . ' host ' . ($count === 1 ? 'check' : 'checks') . '. Confirm them in Settings, under Host checks.';
        }
        return $sentences;
    }

    private static function shell(string $word): string
    {
        return preg_match('~^[A-Za-z0-9_@%+=:,./-]+$~D', $word) ? $word : escapeshellarg($word);
    }

    /** The scheduler line, run through flock so full backups can pause it. */
    private function cronLine(): string
    {
        $settings = $this->formValues();
        $php = $settings['php'] !== '' && $settings['php'] !== null ? $settings['php'] : PHP_BINDIR . '/php';
        $locks = $this->engineValue('job_lock_paths', $settings['job_lock_paths']);
        $lock = $locks[0] ?? (($root = $this->safePrivateRoot()) ? $root . '/cron.lock' : '/path/to/paymenter-cron.lock');
        return '* * * * * /usr/bin/flock -n ' . self::shell($lock) . ' ' . self::shell($php) . ' ' . self::shell(base_path('artisan')) . ' schedule:run';
    }

    /** A shell function defined once, so each Help command stays one short line. */
    private function terminalFunction(): string
    {
        $settings = $this->formValues();
        $python = $settings['python'] ?: '/usr/bin/python3';
        $php = $settings['php'] !== '' && $settings['php'] !== null ? $settings['php'] : PHP_BINDIR . '/php';
        $words = [$python, '-I', dirname(__DIR__, 2) . '/runtime/cli.py', '--app', base_path(), '--private-root', $this->safePrivateRoot() ?? '/path/to/private', '--php', $php];
        return 'paymenter_backups() { ' . implode(' ', array_map([self::class, 'shell'], $words)) . ' "$@"; }';
    }

    // ---------------------------------------------------------------- Page content

    public function content(Schema $schema): Schema
    {
        return $schema->components([
            Callout::make('The backup engine cannot run from the web server')
                ->danger()
                ->description(fn () => Messages::error($this->overviewError) . ' The terminal commands on the Help tab still work.')
                ->visible(fn () => $this->overview() && $this->overviewError !== null),
            Tabs::make('Backups')
                ->contained(false)
                ->persistTabInQueryString('tab')
                ->tabs([
                    Tab::make('Backups')->id('backups')->schema([
                        $this->setupCallout(),
                        $this->overviewSection(),
                        EmbeddedTable::make(),
                    ]),
                    Tab::make('Settings')->id('settings')->schema([
                        EmbeddedSchema::make('settingsForm'),
                        Section::make('S3 access keys')
                            ->description(fn () => match (true) {
                                !$this->initialized() => 'Save settings first. Keys are stored in the private folder that saving creates.',
                                $this->configuration()['saved_keys']['s3'] ?? false => 'Saved and encrypted. Enter new keys to replace them. Saved keys are never shown.',
                                default => 'Saved separately and encrypted. Save settings does not include them. Use keys limited to this bucket and prefix.',
                            })
                            ->schema([EmbeddedSchema::make('s3KeysForm')->visible(fn () => $this->initialized())])
                            ->visible(fn () => ($this->settings['destination'] ?? null) === 's3'),
                        Section::make('Site password')
                            ->description(fn () => $this->sitePasswordDescription())
                            ->schema([EmbeddedSchema::make('siteAccessForm')->visible(fn () => $this->initialized() && $this->httpsOrigin() !== null)])
                            ->visible(fn () => ($this->settings['mode'] ?? null) === 'full-quiesced'),
                    ]),
                    Tab::make('Help')->id('help')->schema($this->helpSections()),
                ]),
        ]);
    }

    private function setupCallout(): Callout
    {
        return Callout::make(fn () => $this->initialized() && $this->overview()['runs'] ? 'Backups need attention' : 'Finish setup')
            ->warning()
            ->description(fn () => $this->issueList($this->setupItems()))
            ->actions([
                Action::make('openSettings')->label('Open settings')->color('gray')->url(fn () => static::getUrl(['tab' => 'settings']))
                    ->visible(fn () => count($this->setupItems()) > ($this->schedulerFresh() ? 0 : 1)),
                Action::make('cronHelp')->label('Show cron setup')->color('gray')->url(fn () => static::getUrl(['tab' => 'help']))
                    ->visible(fn () => !$this->schedulerFresh()),
            ])
            ->visible(fn () => $this->overviewError === null && $this->setupItems() !== []);
    }

    /** Open setup items: at most one line each for storage, scheduler and host checks, then real faults. */
    private function setupItems(): array
    {
        $configuration = $this->configuration();
        $items = [];
        if (!$this->initialized()) {
            $items[] = Messages::error('installation-not-initialized');
        }
        if (($configuration['available'] ?? false) && !($configuration['valid'] ?? false)) {
            $items[] = Messages::error($configuration['error']);
        }
        if (!$this->schedulerFresh()) {
            $items[] = $this->schedulerSentence();
        }
        if ($configuration['valid'] ?? false) {
            $mode = $configuration['mode'];
            $issues = $this->issueSentences($mode);
            array_push($items, ...$issues);
            if ($issues && $mode === 'full-quiesced' && $this->schedulerFresh() && $this->blockers('database-online') === []) {
                $items[] = 'Database-only backups can run now.';
            }
        }
        return array_values(array_unique($items));
    }

    private function overviewSection(): Section
    {
        return Section::make('Overview')
            ->headerActions([$this->testStorageAction(), $this->applyRetentionAction()])
            ->schema([
                Grid::make(['default' => 1, 'md' => 2])->schema([
                    TextEntry::make('overview_last')->label('Last verified backup')->state(function () {
                        foreach ($this->overview()['runs'] as $run) {
                            if ($run['status'] === 'ready' && $run['download'] === 'ready') {
                                return $this->time($run['created_at'], true) . ', ' . $this->ago($run['created_at']);
                            }
                        }
                        return 'None yet';
                    }),
                    TextEntry::make('overview_next')->label('Next automatic backup')->state(function () {
                        $schedule = $this->configuration()['schedule'] ?? null;
                        if (!$schedule || !$schedule['enabled']) { return 'Off. Turn on automatic backups in Settings.'; }
                        return $schedule['next_at'] ? $this->time($schedule['next_at'], true) . ', ' . strtolower(Messages::mode($this->configuration()['mode'])) : 'Not scheduled in the next 8 days.';
                    }),
                    // The exact folder is in Settings; the overview names the kind of place.
                    TextEntry::make('overview_storage')->label('Stored on')->state(function () {
                        $destination = $this->configuration()['destination'] ?? null;
                        if (!$destination) { return 'Unknown'; }
                        return $destination['type'] === 's3' ? 'This server and S3 bucket ' . $destination['s3_bucket'] : 'This server';
                    }),
                    TextEntry::make('overview_space')->label('Space used')->state(function () {
                        $space = $this->configuration()['space'] ?? null;
                        if (!$space || $space['used'] === null) { return 'Nothing stored yet'; }
                        $count = count(array_filter($this->overview()['runs'], static fn ($run) => $run['local']));
                        if ($count === 0) { return 'No backups stored. The limit is ' . Messages::bytes($space['budget']) . '.'; }
                        return Messages::bytes($space['used']) . ' of ' . Messages::bytes($space['budget']) . ', ' . $count . ' ' . ($count === 1 ? 'backup' : 'backups');
                    }),
                    TextEntry::make('overview_scheduler')->label('Scheduler')->state(function () {
                        $last = $this->overview()['scheduler']['last_tick_at'] ?? null;
                        return $last ? 'Last ran ' . $this->ago($last) : 'Not run yet';
                    })->color(fn () => $this->schedulerFresh() ? null : 'warning'),
                    TextEntry::make('overview_test')->label('Storage test')->state(function () {
                        $test = $this->latestRequest('destination-test');
                        if (!$test) { return 'Not run yet'; }
                        return match ($test['status']) {
                            'complete' => 'Passed ' . $this->ago($test['finished_at']),
                            'failed' => 'Failed ' . $this->ago($test['finished_at']) . '. ' . Messages::error($test['error']),
                            'cancelled', 'expired' => 'Not run. Last request was ' . $test['status'] . '.',
                            default => 'Queued. ' . Messages::taskStatus($test['status'])[0] . '.',
                        };
                    })->color(fn () => ($this->latestRequest('destination-test')['status'] ?? null) === 'failed' ? 'danger' : null),
                ]),
            ]);
    }

    private function helpSections(): array
    {
        // Long paths have no spaces; let them wrap anywhere so phones show the whole command.
        $code = static fn (string|\Closure $command) => Text::make($command)->fontFamily(FontFamily::Mono)->copyable()
            ->extraAttributes(['style' => 'overflow-wrap: anywhere']);
        return [
            Section::make('What a backup contains')->schema([
                Text::make('A full backup holds the database and every file in Paymenter\'s folder, including .env, APP_KEY, uploads and extensions. The site shows its maintenance page until the copy finishes. How long that takes depends on how much data there is.'),
                Text::make('A database-only backup keeps the site online and holds only the database. Restoring it also needs a separate copy of Paymenter\'s files.'),
                Text::make('Archives contain customer data and secrets. Keep downloads private. Saved S3 keys and the site password are never included.'),
            ]),
            Section::make('How tasks run')->schema([
                Text::make('Buttons on this page queue tasks. Paymenter\'s scheduler runs them one at a time in the background, so the web server never runs a backup itself.'),
                Text::make('The scheduler needs a cron job for the site user. If your crontab already runs schedule:run through flock, keep that line. Otherwise use this one, and add its lock file under Host checks in Settings:'),
                $code(fn () => $this->cronLine()),
                Text::make('Run any queue:work cron job through the same lock file. Full backups hold the lock, so cron jobs do not run while data is copied.'),
                Text::make('Automatic backups are separate from enabling the extension. Turn them on in Settings, under Schedule.'),
            ]),
            Section::make('Terminal commands')->description('Run these as the site user. They work even when Paymenter itself cannot start.')->schema([
                Text::make('Set up the command once in each terminal session:'),
                $code(fn () => $this->terminalFunction()),
                Text::make('Show backups and tasks:'),
                $code('paymenter_backups overview --offline'),
                Text::make('After an interrupted backup, check what needs fixing. This only reports and changes nothing:'),
                $code('paymenter_backups recover'),
                Text::make('Then fix it and reopen the site. Run this only after reading the check:'),
                $code('paymenter_backups recover --execute'),
                Text::make('Copy a verified backup and its restore notes into a new private folder. The backup ID is in each backup\'s Details:'),
                $code('paymenter_backups export BACKUP-ID --output /path/to/new-folder'),
            ]),
            Section::make('Restoring')->schema([
                Text::make('Restore into a new folder and a new database, never over the running site. The steps are in this guide:'),
                $code(dirname(__DIR__, 2) . '/docs/RECOVERY.md'),
                Text::make(fn () => 'Installation ID: ' . ($this->overview()['installation_uuid'] ?? 'not created yet') . '. It names this site\'s folder in S3.'),
            ]),
        ];
    }

    private function safePrivateRoot(): ?string
    {
        try { return Runtime::privateRoot(); } catch (\Throwable $error) { return null; }
    }

    // ---------------------------------------------------------------- Settings form

    public function settingsForm(Schema $schema): Schema
    {
        $positive = static fn (string $key) => in_array($key, Settings::POSITIVE, true) ? 1 : 0;
        $integer = fn (string $key, string $label, string $suffix, ?string $help = null) => TextInput::make($key)->label($label)
            ->integer()->required()->minValue($positive($key))->suffix($suffix)->helperText($help);
        $path = static fn (string $key, string $label, ?string $help = null, ?string $placeholder = null) => TextInput::make($key)->label($label)
            ->helperText($help)->placeholder($placeholder)->rules(['nullable', 'string', 'regex:/^\/[^\x00]*$/'])
            ->validationMessages(['regex' => 'Enter an absolute path that starts with /.']);
        // Plain Laravel rules: Filament evaluates closures in rules() as its own utilities.
        $lineList = static fn (string $key, string $label, string $help) => Textarea::make($key)->label($label)->helperText($help)->rows(3)
            ->rules(['nullable', 'string', 'regex:' . Settings::PATH_LINES])
            ->validationMessages(['regex' => 'Put one absolute path on each line, starting with /.']);
        $full = static fn (Get $get) => $get('mode') === 'full-quiesced';
        $zones = array_combine(\DateTimeZone::listIdentifiers(), \DateTimeZone::listIdentifiers());

        return $schema->components([
            Form::make([
                Section::make('What to back up')->schema([
                    Radio::make('mode')->label('Backup type')->required()->live()
                        ->options(Messages::MODES)
                        ->descriptions([
                            'full-quiesced' => 'Database and all of Paymenter\'s files. The site shows its maintenance page while data is copied.',
                            'database-online' => 'The site stays online. Restoring also needs a copy of Paymenter\'s files.',
                        ]),
                ]),
                Section::make('Schedule')->description('Automatic backups use Paymenter\'s scheduler. Enabling the extension alone does not schedule backups.')->schema([
                    Toggle::make('enabled')->label('Create backups automatically')->live(),
                    Grid::make(['default' => 1, 'md' => 3])->schema([
                        Radio::make('schedule_frequency')->label('Frequency')->options(Settings::SELECTS['schedule_frequency'])->required()->inline()->live(),
                        TextInput::make('schedule_time')->label('Time')->type('time')->required()->regex('/^([01][0-9]|2[0-3]):[0-5][0-9]$/')
                            ->helperText(fn () => ($billing = $this->configuration()['billing'] ?? null) && $billing['time'] ? 'Paymenter\'s daily billing runs at ' . $billing['time'] . ' ' . $billing['timezone'] . '. Full backups stay clear of it.' : null),
                        Select::make('schedule_timezone')->label('Time zone')->options($zones)->searchable()->required()->rule('timezone')
                            ->helperText('UTC avoids daylight saving gaps.'),
                    ])->visible(fn (Get $get) => (bool) $get('enabled')),
                    CheckboxList::make('schedule_weekdays')->label('Days')->options(array_map('strval', Messages::WEEKDAYS))->columns(['default' => 2, 'md' => 4])
                        ->required(fn (Get $get) => (bool) $get('enabled'))
                        ->rules(['array'])->nestedRecursiveRules(['in:0,1,2,3,4,5,6'])
                        ->validationMessages(['required' => 'Choose at least one day.'])
                        ->visible(fn (Get $get) => (bool) $get('enabled') && $get('schedule_frequency') === 'weekly'),
                ]),
                Section::make('Storage')->schema([
                    Radio::make('destination')->label('Keep backups on')->required()->live()
                        ->options(Settings::SELECTS['destination'])
                        ->descriptions([
                            'local' => 'A private folder on this server.',
                            's3' => 'The private folder on this server, plus a copy in an S3-compatible bucket after each backup.',
                        ]),
                    $path('local_path', 'Backup folder', 'Leave empty to use the folder shown. It must be outside Paymenter and every public web folder.',
                        ($root = $this->safePrivateRoot()) ? $root . '/artifacts' : null),
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        TextInput::make('s3_bucket')->label('Bucket')->required(fn (Get $get) => $get('destination') === 's3'),
                        TextInput::make('s3_region')->label('Region')->placeholder('us-east-1'),
                        TextInput::make('s3_endpoint')->label('Endpoint')->placeholder('https://s3.example.com')
                            ->helperText('Leave empty for AWS. Other providers need an https:// address.')
                            ->rules(['nullable', 'regex:/^https:\/\/[^\s\/?#@]+(?:\/[^\s?#]*)?$/D'])->validationMessages(['regex' => 'Enter an https:// address without a user name or query.']),
                        TextInput::make('s3_prefix')->label('Folder prefix')->helperText('This installation\'s ID is added after it.')->required()
                            ->rules(['string', 'regex:' . Settings::S3_PREFIX])
                            ->validationMessages(['regex' => 'Use folder names separated by /, without "." or "..".']),
                        Toggle::make('s3_path_style')->label('Use path-style URLs')->helperText('Turn on if your provider needs the bucket name in the path.'),
                    ])->visible(fn (Get $get) => $get('destination') === 's3'),
                    Checkbox::make('plaintext_offsite_acknowledged')->label('I understand archives in S3 are not encrypted')
                        ->helperText('They contain customer data, the .env file and APP_KEY. Limit bucket access to this site\'s keys.')
                        ->accepted(fn (Get $get) => $get('destination') === 's3')
                        ->validationMessages(['accepted' => 'Confirm this before using S3.'])
                        ->visible(fn (Get $get) => $get('destination') === 's3'),
                ]),
                Section::make('Host checks')
                    ->description(fn (Get $get) => $full($get)
                        ? 'Full backups pause Paymenter\'s cron jobs and show the maintenance page while data is copied. Confirm only what is true for this server.'
                        : 'Confirm only what is true for this server.')
                    ->schema([
                        $lineList('job_lock_paths', 'Cron lock files', 'One path per line. Each Paymenter cron job must run through flock with one of these files. Help shows the cron line.')
                            ->visible($full),
                        Checkbox::make('cooperative_writers_acknowledged')->label('Only Paymenter and its cron jobs write to its files and database.')->visible($full),
                        Checkbox::make('external_state_acknowledged')->label('Paymenter keeps no data in Redis, object storage or other outside services.')->visible($full),
                        Checkbox::make('no_concurrent_ddl_acknowledged')->label('Database migrations and other schema changes never run during a backup.'),
                    ]),
                Section::make('Retention and space')->schema([
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        $integer('retention_keep', 'Backups to keep', 'backups', 'How many verified backups to keep when deleting older backups.'),
                        Toggle::make('automatic_retention')->label('Delete older backups after each new backup')->inline(false),
                        $integer('local_byte_budget', 'Space limit', 'MiB', 'Largest total size of backups on this server, including failed attempts.'),
                        $integer('minimum_free_bytes', 'Keep this much disk free', 'MiB', 'Backups stop before free space drops below this.'),
                    ]),
                ]),
                Section::make('Advanced')->description('Time limits, folders and programs. Most sites keep the defaults.')
                    ->collapsible()->collapsed()->schema([
                        Fieldset::make('Time limits')->columns(['default' => 1, 'md' => 2])->schema([
                            $integer('capture_timeout', 'Capture time limit', 'seconds', 'Longest time to export the database and copy files.'),
                            $integer('drain_seconds', 'Wait before capture', 'seconds', 'Time for open requests to finish after the site pauses.'),
                            $integer('lock_timeout', 'Cron lock wait', 'seconds', 'How long to wait for running cron jobs.'),
                            $integer('billing_margin_seconds', 'Billing safety margin', 'seconds', 'Gap kept around Paymenter\'s daily billing run.'),
                            $integer('billing_guard_max_age', 'Scheduler report age', 'seconds', 'How recent the scheduler\'s billing-time report must be.'),
                            $integer('request_max_age', 'Task expiry', 'seconds', 'Queued tasks older than this are dropped. 86400 is one day.'),
                            $integer('s3_timeout', 'S3 timeout', 'seconds'),
                            $integer('s3_retries', 'S3 attempts per request', 'attempts'),
                        ]),
                        Fieldset::make('Folders and programs')->columns(['default' => 1, 'md' => 2])->schema([
                            $path('private_root', 'Private folder', 'Holds the installation ID, encrypted keys and backup history. Set it before the first save. Afterwards it can only be moved from the terminal.', dirname(base_path()) . '/paymenter-backups-private')
                                ->disabled(fn () => $this->initialized()),
                            $lineList('extra_docroots', 'Other public web folders', 'One path per line, for every site this account serves. Backups never go inside them.'),
                            $path('php', 'PHP program', 'Command-line PHP. Leave empty to detect it.', PHP_BINDIR . '/php'),
                            $path('python', 'Python program', 'Python 3.10 or newer.'),
                            $path('dump', 'Database export program'),
                            $path('mysql', 'Database client program'),
                            $path('tar', 'tar program'),
                            $path('gzip', 'gzip program'),
                        ]),
                    ]),
            ])->livewireSubmitHandler('saveSettings')->footer([
                Actions::make([Action::make('saveSettings')->label('Save settings')->submit('saveSettings')->keyBindings(['mod+s'])]),
            ]),
        ])->statePath('settings');
    }

    /** Form values from native rows. A malformed row falls back to its default. */
    private function formValues(): array
    {
        if ($this->storedForm !== null) { return $this->storedForm; }
        $rows = $this->record()->settings()->pluck('value', 'key')->all();
        $form = [];
        foreach (Settings::DEFAULTS as $key => $default) {
            try { $value = Settings::normalize(array_key_exists($key, $rows) ? [$key => $rows[$key]] : [])[$key]; }
            catch (\Throwable $error) { $value = $default; }
            if ($key === 'schedule_weekdays') { $value = array_map('strval', is_array($value) ? array_filter($value, 'is_int') : []); }
            elseif (in_array($key, self::LINE_LISTS, true)) { $value = implode("\n", is_array($value) ? array_filter($value, 'is_string') : []); }
            elseif (in_array($key, self::MIB_FIELDS, true)) { $value = is_int($value) ? intdiv($value, self::MIB) : $value; }
            $form[$key] = $value;
        }
        return $this->storedForm = $form;
    }

    /** Engine value for a form value. */
    private function engineValue(string $key, mixed $value): mixed
    {
        $default = Settings::DEFAULTS[$key];
        if ($key === 'schedule_weekdays') { $days = array_values(array_unique(array_map('intval', (array) $value))); sort($days); return $days; }
        if (in_array($key, self::LINE_LISTS, true)) { return array_values(array_filter(array_map('trim', preg_split('/\R/', (string) $value)), static fn ($line) => $line !== '')); }
        if (in_array($key, self::MIB_FIELDS, true)) { return (int) $value * self::MIB; }
        if (is_int($default)) { return (int) $value; }
        if (is_bool($default)) { return (bool) $value; }
        return trim((string) ($value ?? ''));
    }

    private function changed(string $key, mixed $value): bool
    {
        // Compare with native rows read on this request, never with anything the browser can send.
        $before = $this->formValues()[$key] ?? null;
        return json_encode($this->engineValue($key, $value)) !== json_encode($this->engineValue($key, $before));
    }

    public function saveSettings(): void
    {
        $this->authorizeAction();
        $state = $this->settingsForm->getState();
        $this->storedForm = null;
        $changes = [];
        foreach ($state as $key => $value) {
            if (array_key_exists($key, Settings::DEFAULTS) && $this->changed($key, $value)) { $changes[$key] = $this->engineValue($key, $value); }
        }
        $initialized = $this->initialized();
        // The first save creates private storage, even when every default is kept.
        if (!$changes && $initialized) {
            Notification::make()->title('No changes to save')->info()->send();
            return;
        }
        if (array_key_exists('private_root', $changes) && $initialized) {
            // Moving would silently start a new identity without the saved keys or history.
            $this->refuseSettings('private-root-move-refused');
            return;
        }
        if (array_key_exists('private_root', $changes) || array_key_exists('extra_docroots', $changes)) {
            try {
                $docroots = $changes['extra_docroots'] ?? $this->engineValue('extra_docroots', $this->formValues()['extra_docroots']);
                PrivateStore::root(realpath(base_path()), ($changes['private_root'] ?? $this->engineValue('private_root', $this->formValues()['private_root'])) ?: null, false, $docroots);
            } catch (\RuntimeException $error) {
                $this->refuseSettings($error->getMessage());
                return;
            }
        }
        $checked = true;
        try {
            Runtime::command(['config', 'check'], ['settings' => $changes ?: new \stdClass], 30);
        } catch (\RuntimeException $error) {
            $code = $error->getMessage();
            // A broken engine path can only be repaired by saving the paths themselves.
            if (!$changes || !in_array($code, self::ENVIRONMENT_ERRORS, true) || array_diff(array_keys($changes), self::BOOTSTRAP)) {
                $this->refuseSettings($code, in_array($code, self::ENVIRONMENT_ERRORS, true));
                return;
            }
            $checked = false;
        }
        $record = $this->record();
        $previous = $record->settings()->whereIn('key', array_keys($changes))->pluck('value', 'key')->all();
        if ($changes) {
            SettingsWriter::write($record, array_map(static fn ($value) => is_array($value) ? json_encode($value, JSON_UNESCAPED_SLASHES) : $value, $changes));
        }
        $this->storedForm = null;
        $this->overviewCache = null;
        $this->overviewError = null;
        if ($checked) {
            try {
                Runtime::command(['init'], null, 30);
            } catch (\Throwable $error) {
                // Nothing is reported as saved unless private storage accepted it.
                $restore = [];
                foreach (array_keys($changes) as $key) { $restore[$key] = $previous[$key] ?? SettingsWriter::stored($key, Settings::DEFAULTS[$key]); }
                if ($restore) { SettingsWriter::write($record, $restore); }
                $this->storedForm = null;
                $this->overviewCache = null;
                $this->refuseSettings($error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable');
                return;
            }
        }
        (new Extension)->updated($record);
        $this->overviewCache = null;
        if ($checked) {
            Notification::make()->title($initialized ? 'Settings saved' : 'Settings saved. Private storage is ready.')->success()->send();
            return;
        }
        $this->overview();
        Notification::make()->title('Paths saved')->warning()
            ->body($this->overviewError ? 'The backup engine still cannot start. ' . Messages::error($this->overviewError) : 'The backup engine starts again. Review the other settings.')->send();
    }

    private function refuseSettings(string $code, bool $environment = false): void
    {
        $message = Messages::error($code);
        if ($field = Messages::field($code)) {
            $this->addError('settings.' . $field, $message);
            // Opens a collapsed section and scrolls to the field, as for form validation.
            $this->dispatch('form-validation-error', livewireId: $this->getId());
        }
        Notification::make()->title('Settings not saved')->danger()
            ->body($environment ? 'The backup engine could not check them. ' . $message . ' Save the paths under Advanced on their own first.' : $message)->send();
    }

    // ---------------------------------------------------------------- Secrets

    public function s3KeysForm(Schema $schema): Schema
    {
        return $schema->components([
            Form::make([
                Grid::make(['default' => 1, 'md' => 2])->schema([
                    TextInput::make('access_key')->label('Access key ID')->password()->revealable(false)->required()->maxLength(8192)->autocomplete('new-password'),
                    TextInput::make('secret_key')->label('Secret access key')->password()->revealable(false)->required()->maxLength(8192)->autocomplete('new-password'),
                    TextInput::make('session_token')->label('Session token')->password()->revealable(false)->maxLength(8192)->autocomplete('new-password')
                        ->helperText('Only for temporary keys.'),
                ]),
            ])->livewireSubmitHandler('saveS3Keys')->footer([
                Actions::make([
                    Action::make('saveS3Keys')->label(fn () => ($this->configuration()['saved_keys']['s3'] ?? false) ? 'Replace S3 keys' : 'Save S3 keys')->submit('saveS3Keys'),
                    $this->removeS3KeysAction(),
                ]),
            ]),
        ])->statePath('s3Keys');
    }

    public function saveS3Keys(): void
    {
        $this->authorizeAction();
        $data = $this->s3KeysForm->getState();
        $keys = ['access_key' => (string) $data['access_key'], 'secret_key' => (string) $data['secret_key']];
        if (filled($data['session_token'] ?? null)) { $keys['session_token'] = (string) $data['session_token']; }
        try {
            Runtime::credentials($keys);
            $this->overviewCache = null;
            Notification::make()->title('S3 keys saved')->success()->send();
        } catch (\Throwable $error) {
            Notification::make()->title('S3 keys not saved')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'credentials-save-failed'))->send();
        } finally {
            unset($keys, $data);
        }
    }

    public function removeS3KeysAction(): Action
    {
        return Action::make('removeS3Keys')->label('Remove S3 keys')->color('danger')->link()
            ->visible(fn () => $this->configuration()['saved_keys']['s3'] ?? false)
            ->requiresConfirmation()->modalHeading('Remove S3 keys?')
            ->modalDescription('Backups stay on this server, but copying to S3 stops until you save new keys. The site password is kept.')
            ->modalSubmitActionLabel('Remove S3 keys')
            ->action(function () {
                $this->authorizeAction();
                try {
                    Runtime::credentials(['clear' => true]);
                    $this->overviewCache = null;
                    Notification::make()->title('S3 keys removed')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('S3 keys not removed')->danger()->body(Messages::error($error->getMessage()))->send();
                }
            });
    }

    /** Paymenter's own https origin, or null when its address is not https. */
    private function httpsOrigin(): ?string
    {
        $origin = $this->configuration()['app_origin'] ?? null;
        return is_string($origin) && str_starts_with($origin, 'https://') ? $origin : null;
    }

    private function sitePasswordDescription(): string
    {
        $configuration = $this->configuration();
        if (!$this->initialized()) { return 'Optional. Save settings first. The password is stored in the private folder that saving creates.'; }
        if ($this->httpsOrigin() === null) {
            return 'Optional. Paymenter\'s address does not use https://, so a site password cannot be sent safely. Switch Paymenter to https:// to use one.';
        }
        if ($configuration['saved_keys']['site'] ?? false) { return 'Saved and encrypted. Enter new details to replace them.'; }
        return 'Optional. Only needed if a browser password prompt protects the whole site. Saved separately and encrypted.';
    }

    public function siteAccessForm(Schema $schema): Schema
    {
        return $schema->components([
            Form::make([
                Text::make(fn () => 'Full backups load ' . ($this->httpsOrigin() ?? 'the site') . ' to confirm visitors see the maintenance page.'),
                Grid::make(['default' => 1, 'md' => 2])->schema([
                    // Required to save, but the section itself is optional, so no asterisk.
                    TextInput::make('username')->label('User name')->password()->revealable(false)->required()->markAsRequired(false)->maxLength(8192)->autocomplete('off')
                        ->rule('not_regex:/[:\x00-\x1f\x7f]/')->validationMessages(['not_regex' => 'User names cannot contain a colon.']),
                    TextInput::make('password')->label('Password')->password()->revealable(false)->required()->markAsRequired(false)->maxLength(8192)->autocomplete('new-password'),
                ]),
            ])->livewireSubmitHandler('saveSitePassword')->footer([
                Actions::make([
                    Action::make('saveSitePassword')->label(fn () => ($this->configuration()['saved_keys']['site'] ?? false) ? 'Replace site password' : 'Save site password')->submit('saveSitePassword'),
                    $this->removeSitePasswordAction(),
                ]),
            ]),
        ])->statePath('siteAccess');
    }

    public function saveSitePassword(): void
    {
        $this->authorizeAction();
        $data = $this->siteAccessForm->getState();
        try {
            // The address is Paymenter's own; a browser cannot point the check elsewhere.
            $origin = $this->httpsOrigin();
            if ($origin === null) { throw new \RuntimeException('maintenance-probe-https-required'); }
            Runtime::credentials(['origin' => $origin, 'username' => (string) $data['username'], 'password' => (string) $data['password']], true);
            $this->overviewCache = null;
            Notification::make()->title('Site password saved')->success()->send();
        } catch (\Throwable $error) {
            $code = $error instanceof \RuntimeException ? $error->getMessage() : 'credentials-save-failed';
            Notification::make()->title('Site password not saved')->danger()->body(Messages::error($code))->send();
        } finally {
            unset($data);
        }
    }

    public function removeSitePasswordAction(): Action
    {
        return Action::make('removeSitePassword')->label('Remove site password')->color('danger')->link()
            ->visible(fn () => $this->configuration()['saved_keys']['site'] ?? false)
            ->requiresConfirmation()->modalHeading('Remove the site password?')
            ->modalDescription('Full backups will check the site without a password. S3 keys are kept.')
            ->modalSubmitActionLabel('Remove site password')
            ->action(function () {
                $this->authorizeAction();
                try {
                    Runtime::credentials(['clear' => true], true);
                    $this->overviewCache = null;
                    Notification::make()->title('Site password removed')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('Site password not removed')->danger()->body(Messages::error($error->getMessage()))->send();
                }
            });
    }

    // ---------------------------------------------------------------- Actions

    /** Queue one durable request. The scheduler tick does the work. */
    private function queue(string $action, array $arguments, string $title): bool
    {
        $this->authorizeAction();
        $arguments['actor'] = (int) Auth::id();
        try {
            Runtime::command(['requests', 'create', '--action', $action], $arguments);
        } catch (\Throwable $error) {
            Notification::make()->title('Not queued')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable'))->send();
            return false;
        }
        $this->overviewCache = null;
        // No timing promise: the scheduler decides when work starts.
        $body = 'It starts when Paymenter\'s scheduler next runs.';
        Notification::make()->title($title)->success()->body($body)->send();
        return true;
    }

    protected function getHeaderActions(): array
    {
        return [$this->createBackupAction()];
    }

    public function createBackupAction(): Action
    {
        return $this->backupAction('createBackup');
    }

    /** A type that can start now: the configured one first. */
    private function preferredMode(): string
    {
        $configured = $this->configuration()['mode'] ?? 'full-quiesced';
        foreach ([$configured, 'full-quiesced', 'database-online'] as $mode) {
            if ($this->blockers($mode) === []) { return $mode; }
        }
        return $configured;
    }

    private function nothingCanStart(): bool
    {
        return $this->blockers('full-quiesced') !== [] && $this->blockers('database-online') !== [];
    }

    /** The same dialog serves the page header and the empty history. */
    private function backupAction(string $name): Action
    {
        $blockedTitle = fn () => match (true) {
            $this->nothingCanStart() => 'Backups cannot start yet',
            $this->blockers('full-quiesced') !== [] => 'Full backups cannot start yet',
            default => 'Database-only backups cannot start yet',
        };
        $blockedItems = function () {
            if (!$this->nothingCanStart()) {
                // The scheduler notice below already covers a stale scheduler.
                $items = $this->issueSentences($this->blockers('full-quiesced') !== [] ? 'full-quiesced' : 'database-online');
                $rest = array_values(array_diff($items, [$this->schedulerSentence()]));
                return $this->issueList($rest ?: $items);
            }
            // Full backups need every host check database-only backups need, so count them once.
            $items = [...$this->issueSentences('full-quiesced'), ...$this->issueSentences('database-online', false)];
            return $this->issueList(array_values(array_unique($items)));
        };
        return Action::make($name)->label('Create backup')->icon('ri-add-line')
            ->modalHeading('Create a backup')
            ->modalDescription('The backup runs in the background. You can leave this page.')
            ->fillForm(fn () => ['mode' => $this->preferredMode()])
            ->schema([
                Radio::make('mode')->label('Backup type')->required()->live()
                    ->options(Messages::MODES)
                    ->descriptions([
                        'full-quiesced' => 'Database and all files. The site shows its maintenance page until the copy finishes.',
                        'database-online' => 'The site stays online. Restoring also needs a copy of Paymenter\'s files.',
                    ])
                    // A type that cannot start is shown but not selectable; the callout says why.
                    ->disableOptionWhen(fn (string $value) => !$this->nothingCanStart() && $this->blockers($value) !== []),
                Callout::make($blockedTitle)->warning()
                    ->description($blockedItems)
                    ->actions([
                        Action::make($name . 'OpenSettings')->label('Open settings')->color('gray')->url(fn () => static::getUrl(['tab' => 'settings'])),
                        Action::make($name . 'CronHelp')->label('Show cron setup')->color('gray')->url(fn () => static::getUrl(['tab' => 'help']))
                            ->visible(fn () => !$this->schedulerFresh()),
                    ])
                    ->visible(fn () => $this->blockers('full-quiesced') !== [] || $this->blockers('database-online') !== []),
                Callout::make('It may start a little later')->info()
                    ->description('It waits for Paymenter\'s daily billing run or running cron jobs to finish first.')
                    ->visible(fn (Get $get) => array_intersect($this->configuration()['admission'][$get('mode') ?? 'full-quiesced'] ?? [], self::TRANSIENT) !== []),
                Callout::make('Waiting for Paymenter\'s scheduler')->info()
                    ->description('The backup starts when Paymenter\'s scheduler next runs.')
                    ->visible(fn () => !$this->schedulerFresh() && !$this->nothingCanStart()),
                Callout::make('A backup is already queued')->info()
                    ->description('Its row in Backup history shows when it starts.')
                    ->visible(fn () => $this->activeRequest('run') !== null),
            ])
            ->modalSubmitAction(fn (Action $action) => $this->nothingCanStart() ? false : $action)
            ->modalSubmitActionLabel('Create backup')
            ->modalCancelActionLabel(fn () => $this->nothingCanStart() ? 'Close' : 'Cancel')
            ->action(function (array $data, Action $action) {
                $mode = in_array($data['mode'] ?? null, ['full-quiesced', 'database-online'], true) ? $data['mode'] : null;
                if ($mode === null || ($issues = $this->issueSentences($mode)) !== []) {
                    Notification::make()->title('Backup not queued')->danger()->body(implode(' ', $issues ?? []))->send();
                    $action->halt();
                }
                if (!$this->queue('run', ['mode' => $mode], 'Backup queued')) { $action->halt(); }
            });
    }

    private function issueList(array $issues): HtmlString
    {
        $items = array_map(static fn ($item) => '<li>' . e($item) . '</li>', array_values($issues));
        return new HtmlString('<ul style="list-style: disc; padding-inline-start: 1.25rem; display: grid; gap: .25rem;">' . implode('', $items) . '</ul>');
    }

    public function testStorageAction(): Action
    {
        return Action::make('testStorage')->label('Test storage')->color('gray')
            ->disabled(fn () => $this->activeRequest('destination-test') !== null)
            ->tooltip(fn () => $this->activeRequest('destination-test') ? 'A storage test is already queued.' : 'Writes, reads and removes a small test file.')
            ->action(fn () => $this->queue('destination-test', [], 'Storage test queued'));
    }

    public function applyRetentionAction(): Action
    {
        return Action::make('applyRetention')->label('Apply retention')->color('gray')
            // Only offered when retention has something to delete.
            ->visible(fn () => count(array_filter($this->overview()['runs'], static fn ($run) => $run['local'] && $run['status'] === 'ready'))
                > ($this->configuration()['retention']['keep'] ?? PHP_INT_MAX))
            ->requiresConfirmation()->modalHeading('Apply retention now?')
            ->modalDescription(fn () => 'Keeps the newest ' . ($this->configuration()['retention']['keep'] ?? 7) . ' verified backups and deletes older ones from this server'
                . ((($this->configuration()['destination']['type'] ?? null) === 's3') ? ' and from S3. The newest S3 copy is always kept.' : '.')
                . ' Failed and unverified backups are never deleted by retention.')
            ->modalSubmitActionLabel('Apply retention')
            ->action(fn () => $this->queue('retention', ['execute' => true], 'Retention queued'));
    }

    // ---------------------------------------------------------------- History table

    public function table(Table $table): Table
    {
        return $table
            ->heading('Backup history')
            ->description(fn () => 'Times are in ' . (config('app.timezone') ?: 'UTC') . '.')
            // Filters are null while the deleted-backups filter is hidden.
            ->records(function (?array $filters, int|string $page, int|string $recordsPerPage) {
                $rows = $this->historyRows((bool) ($filters['show_deleted']['isActive'] ?? false));
                $perPage = max(1, (int) $recordsPerPage);
                $page = max(1, (int) $page);
                return new LengthAwarePaginator(array_slice($rows, ($page - 1) * $perPage, $perPage), count($rows), $perPage, $page);
            })
            ->paginated([10, 25, 50])
            ->poll(fn () => $this->hasActiveWork() ? '5s' : null)
            // Below sm each row stacks, so long status details and both row actions get the full width.
            ->stackedOnMobile()
            ->columns([
                TextColumn::make('created_at')->label('Created')
                    ->formatStateUsing(fn ($state) => $this->time($state))
                    ->description(fn (array $record) => $this->ago($record['created_at'])),
                // Below md these move to Details so the table stays readable.
                TextColumn::make('type')->label('Type')->visibleFrom('md'),
                TextColumn::make('size_label')->label('Size')->visibleFrom('md'),
                TextColumn::make('status_label')->label('Status')->badge()->color(fn (array $record) => $record['status_color'])
                    ->description(fn (array $record) => $record['detail'])->wrap(),
                TextColumn::make('location')->label('Stored on')->visibleFrom('lg'),
            ])
            ->filters([
                Filter::make('show_deleted')->label('Show deleted backups')->toggle()
                    ->visible(fn () => array_filter($this->overview()['runs'], static fn ($run) => $run['status'] === 'deleted') !== []),
            ])
            ->recordActions([
                Action::make('download')->label('Download')->icon('ri-download-2-line')->button()->color('gray')
                    ->url(fn (array $record) => static::downloadUrl($record['run_id']))
                    ->visible(fn (array $record) => $record['kind'] === 'run' && $record['run']['status'] === 'ready' && $record['run']['download'] === 'ready'),
                Action::make('verifyFirst')->label('Verify')->icon('ri-shield-check-line')->button()->color('gray')
                    ->tooltip('Checks the whole archive. Download becomes available when it passes.')
                    ->visible(fn (array $record) => $record['kind'] === 'run' && in_array($record['run']['status'], ['ready', 'damaged'], true)
                        && $record['run']['download'] !== 'ready' && !$this->activeRequest('verify', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                Action::make('cancelQueued')->label('Cancel')->button()->color('gray')
                    ->visible(fn (array $record) => $record['kind'] === 'request' && $record['cancel_request'] !== null)
                    ->requiresConfirmation()->modalHeading('Cancel this backup?')->modalDescription('It has not started yet. Nothing is changed on the site.')
                    ->modalSubmitActionLabel('Cancel backup')->modalCancelActionLabel('Keep it')
                    ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                ActionGroup::make([
                    Action::make('details')->label('Details')->icon('ri-information-line')
                        ->modalHeading('Backup details')->modalSubmitAction(false)->modalCancelActionLabel('Close')
                        ->schema(fn (array $record) => $this->detailsSchema($record)),
                    Action::make('verify')->label('Verify again')->icon('ri-shield-check-line')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && $record['run']['status'] === 'ready' && $record['run']['download'] === 'ready'
                            && !$this->activeRequest('verify', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                    Action::make('copy')->label('Copy to S3')->icon('ri-upload-cloud-2-line')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && $record['run']['status'] === 'ready' && $record['run']['remote'] !== 'delivered'
                            && ($this->configuration()['destination']['type'] ?? null) === 's3' && !$this->activeRequest('deliver', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('deliver', ['run_id' => $record['run_id']], 'Copy to S3 queued')),
                    Action::make('cancelTask')->label('Cancel task')->icon('ri-close-circle-line')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && $record['cancel_request'] !== null)
                        ->requiresConfirmation()->modalHeading('Cancel this task?')->modalDescription('The task has not started yet.')
                        ->modalSubmitActionLabel('Cancel task')->modalCancelActionLabel('Keep task')
                        ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                    Action::make('delete')->label('Delete')->icon('ri-delete-bin-line')->color('danger')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && in_array($record['run']['status'], ['ready', 'damaged', 'delete-interrupted'], true)
                            && !$this->activeRequest('delete', $record['run_id']))
                        ->requiresConfirmation()
                        ->modalHeading(fn (array $record) => 'Delete the ' . ($record['run']['mode'] === 'database-online' ? 'database-only' : 'full') . ' backup from ' . $this->time($record['created_at']) . '?')
                        ->modalDescription(fn (array $record) => $this->deleteDescription($record['run_id']))
                        ->modalSubmitAction(fn (Action $action, array $record) => $this->deletePreview($record['run_id'])['refusal'] ? false : $action)
                        ->modalSubmitActionLabel('Delete backup')
                        ->action(fn (array $record) => $this->queue('delete', ['run_id' => $record['run_id'], 'execute' => true], 'Deletion queued')),
                ])->label('More actions')->icon('ri-more-2-fill')->color('gray')->tooltip('More actions'),
            ])
            ->emptyStateIcon('ri-save-line')
            ->emptyStateHeading('No backups yet')
            ->emptyStateDescription(fn () => $this->setupItems() !== [] ? 'Finish setup above, then create the first backup.' : 'Backups you create appear here.')
            ->emptyStateActions([$this->backupAction('createFirstBackup')->visible(fn () => $this->setupItems() === [])]);
    }

    private function callCancel(string $requestId): void
    {
        $this->authorizeAction();
        try {
            Runtime::command(['requests', 'cancel', $requestId]);
            $this->overviewCache = null;
            Notification::make()->title('Cancelled')->success()->send();
        } catch (\Throwable $error) {
            Notification::make()->title('Not cancelled')->danger()->body(Messages::error($error->getMessage()))->send();
        }
    }

    private function deletePreview(string $runId): array
    {
        if (isset($this->deletePreviews[$runId])) { return $this->deletePreviews[$runId]; }
        try {
            $preview = Runtime::command(['delete', $runId], null, 20);
        } catch (\Throwable $error) {
            $preview = ['refusal' => $error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable', 'local_copy' => false, 'remote_copy' => null, 'survivors' => []];
        }
        return $this->deletePreviews[$runId] = $preview;
    }

    private function deleteDescription(string $runId): HtmlString
    {
        $preview = $this->deletePreview($runId);
        if ($preview['refusal']) {
            return new HtmlString(e('This backup cannot be deleted. ' . Messages::error($preview['refusal'])));
        }
        $items = [];
        if ($preview['local_copy']) { $items[] = 'Deletes this backup from this server.'; }
        if ($preview['remote_copy']) { $items[] = 'Also deletes its copy in S3 bucket ' . $preview['remote_copy']['bucket'] . '. If the bucket keeps old versions, those remain.'; }
        $items[] = 'Another usable backup must remain. The scheduler checks one fully before deleting, and keeps this backup if none passes.';
        return $this->issueList($items);
    }

    private function detailsSchema(array $record): array
    {
        if ($record['kind'] !== 'run') {
            return [
                TextEntry::make('d_request')->label('Task ID')->state($record['request_id'])->fontFamily(FontFamily::Mono)->copyable(),
                TextEntry::make('d_status')->label('Status')->state($record['status_label'] . '. ' . $record['detail']),
            ];
        }
        $run = $record['run'];
        return [
            TextEntry::make('d_run')->label('Backup ID')->state($run['run_id'])->fontFamily(FontFamily::Mono)->copyable()->helperText('Use this ID with the terminal commands on the Help tab.'),
            Grid::make(2)->schema([
                TextEntry::make('d_created')->label('Created')->state($this->time($run['created_at'], true)),
                TextEntry::make('d_type')->label('Type')->state(Messages::mode($run['mode'])),
                TextEntry::make('d_size')->label('Size')->state(Messages::bytes($run['size'])),
                TextEntry::make('d_location')->label('Stored on')->state($record['location']),
                TextEntry::make('d_verified')->label('Last full check')->state($run['verified_at'] ? $this->time($run['verified_at'], true) . ($run['verification_source'] === 'capture' ? ', when created' : '') : 'Never'),
                TextEntry::make('d_status')->label('Status')->state($record['status_label']),
            ]),
            TextEntry::make('d_detail')->label('Details')->state($record['detail'])->visible((bool) $record['detail']),
            TextEntry::make('d_code')->label('Error code')->state($run['error'] ?? $run['verification_error'] ?? $run['delivery_error'])->fontFamily(FontFamily::Mono)
                ->visible((bool) ($run['error'] ?? $run['verification_error'] ?? $run['delivery_error'])),
        ];
    }
}
