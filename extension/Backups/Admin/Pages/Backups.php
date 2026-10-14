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
    private array $malformedKeys = [];
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
        $secret = (bool) array_filter(array_keys($exception->errors()), static fn ($key) => str_starts_with($key, 's3Keys.') || str_starts_with($key, 'siteAccess.'));
        Notification::make()->title($secret ? 'Credentials not saved' : 'Settings not saved')->body('Check the highlighted fields, including collapsed sections.')->danger()->send();
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
            [$label, $color] = $active || in_array($request['status'], ['expired', 'skipped'], true) ? Messages::taskStatus($request['status']) : ['Not started', 'danger'];
            if ($request['status'] === 'pending') { $label = 'Queued'; }
            $detail = match ($request['status']) {
                'pending' => 'Starts when Paymenter\'s scheduler next runs.',
                'deferred' => 'Waiting to retry. ' . Messages::error($request['error']),
                'launching' => 'Starting.', 'running' => 'Running.',
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
            $detail = Messages::runDetail($run + ['maintenance' => $this->overview()['maintenance'] ?? false]);
            $cancel = null;
            $tasks = $this->tasksFor($run['run_id']);
            $active = array_values(array_filter($tasks, static fn ($task) => in_array($task['status'], ['pending', 'deferred', 'launching', 'running'], true)));
            if ($active) {
                $task = $active[0];
                $detail = Messages::taskProgress($task['action'], $task['status']) . ($detail ? ' ' . $detail : '');
                if (in_array($task['status'], ['pending', 'deferred'], true)) { $cancel = $task['request_id']; }
            } elseif ($run['status'] === 'ready') {
                // Only the newest finished delete matters; a later success hides an older failure.
                $latest = null;
                foreach ($tasks as $task) {
                    if ($task['action'] === 'delete' && ($latest === null || $task['created_at'] > $latest['created_at'])) { $latest = $task; }
                }
                if ($latest && $latest['status'] === 'failed') { $detail = 'The last deletion did not finish. ' . Messages::error($latest['error']); }
            }
            $rows[] = ['__key' => $run['run_id'], 'kind' => 'run', 'request_id' => null, 'created_at' => $run['created_at'],
                'type' => Messages::mode($run['mode']), 'size' => $run['size'], 'size_label' => $run['size'] === null ? 'Unknown' : Messages::bytes($run['size']), 'status_label' => $label, 'status_color' => $color,
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
        return $last ? 'Paymenter\'s scheduler last ran ' . $this->ago($last) . '. Check Paymenter\'s server scheduler. See Help.'
            : 'Paymenter\'s scheduler has not run yet. See Help for server setup.';
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
            $sentences[] = ($mode === 'full-quiesced' ? 'Full backups' : 'Backups') . ' need ' . $count . ' ' . ($count === 1 ? 'item' : 'items') . ' under Settings, Host checks.';
        }
        return $sentences;
    }

    private static function shell(string $word): string
    {
        return preg_match('~^[A-Za-z0-9_@%+=:,./-]+$~D', $word) ? $word : escapeshellarg($word);
    }

    /** The scheduler line, run through flock so full backups can pause it. */
    private function jobWrapper(): string
    {
        $settings = $this->formValues();
        $php = $settings['php'] !== '' && $settings['php'] !== null ? $settings['php'] : PHP_BINDIR . '/php';
        $locks = $this->engineValue('job_lock_paths', $settings['job_lock_paths']);
        $lock = $locks[0] ?? (($root = $this->safePrivateRoot()) ? $root . '/scheduler.lock' : '/path/to/paymenter-scheduler.lock');
        return '/usr/bin/flock -n ' . self::shell($lock) . ' ' . self::shell($php) . ' ' . self::shell(base_path('artisan')) . ' schedule:run';
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
            Callout::make('The backup program cannot run from the web server')
                ->danger()
                ->description(fn () => Messages::error($this->overviewError) . ' The terminal commands on the Help tab may still work. Use the overview command there to diagnose this.')
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
                            ->description(fn () => 'Used to upload, verify, retrieve and delete copies in the configured S3 bucket. Keys save separately from settings.'
                                . (($this->settings['destination'] ?? null) === 'local' ? ' Not used while Backup storage is This server.' : ' Limit keys to this installation\'s object prefix.'))
                            ->schema([
                                Text::make(fn () => match (true) {
                                    !$this->initialized() => 'Save settings before saving S3 keys.',
                                    $this->configuration()['saved_keys']['s3'] ?? false => 'S3 keys are saved and encrypted. Enter both new keys to replace them. A saved session token is cleared unless you enter a new one.',
                                    default => 'No S3 keys saved. S3 backups need keys before they can start.',
                                }),
                                EmbeddedSchema::make('s3KeysForm')->visible(fn () => $this->initialized() && ($this->settings['destination'] ?? null) === 's3'),
                                Actions::make([$this->removeS3KeysAction()]),
                            ])
                            ->visible(fn () => ($this->settings['destination'] ?? null) === 's3' || ($this->configuration()['saved_keys']['s3'] ?? false)),
                        Section::make('HTTP Basic authentication (advanced)')->collapsible()->collapsed()
                            ->description(fn () => 'Optional. Only sites protected by a web server password prompt need these credentials for the maintenance check. '
                                . $this->siteCredentialSummary())
                            ->schema([
                                Text::make('These credentials are separate from your Paymenter account login.'),
                                Text::make(fn () => $this->sitePasswordDescription()),
                                Text::make(fn () => 'During a full backup, this server requests a test page under ' . ($this->configuration()['site_password']['probe_base'] ?? 'App URL in Paymenter settings') . ' to confirm visitors see the maintenance page.'),
                                EmbeddedSchema::make('siteAccessForm')->visible(fn () => $this->initialized() && $this->httpsOrigin() !== null),
                                Actions::make([$this->removeSitePasswordAction()]),
                            ])
                            ->visible(fn () => ($this->settings['mode'] ?? null) === 'full-quiesced' || ($this->configuration()['saved_keys']['site'] ?? false)),
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
                Action::make('openSettings')->label('Open settings')->color('primary')->link()->url(fn () => static::getUrl(['tab' => 'settings']))
                    ->visible(fn () => count($this->setupItems()) > ($this->schedulerFresh() ? 0 : 1)),
                Action::make('schedulerHelp')->label('Paymenter scheduler setup')->color('primary')->link()->url('https://paymenter.org/docs/installation/install')->openUrlInNewTab()
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
        if (($this->overview()['s3_pending']['count'] ?? 0) > 0) {
            $items[] = $this->overview()['s3_pending']['count'] . (($this->overview()['s3_pending']['count'] ?? 0) === 1 ? ' backup is' : ' backups are') . ' not copied to S3 yet. They stay local. Use Copy to S3 in their row menus.';
        }
        if (($configuration['space']['state'] ?? null) === 'full') { $items[] = 'The space limit is reached. Delete older copies or review Retention and space before new backups.'; }
        if ($this->overview()['maintenance'] ?? false) { $items[] = 'The site currently shows its maintenance page. Queued tasks wait until it reopens.'; }
        return array_values(array_unique($items));
    }

    private function overviewSection(): Section
    {
        return Section::make('Overview')
            ->headerActions([$this->testStorageAction(), $this->applyRetentionAction()])
            ->schema([
                Grid::make(['default' => 1, 'md' => 2])->schema([
                    TextEntry::make('overview_last')->label('Last verified local backup')->state(function () {
                        foreach ($this->overview()['runs'] as $run) {
                            if ($run['status'] === 'ready' && $run['download'] === 'ready') {
                                return $this->time($run['created_at'], true) . ', ' . $this->ago($run['created_at']);
                            }
                        }
                        return 'None yet';
                    }),
                    TextEntry::make('overview_last_s3')->label('Last S3 copy recorded')->state(function () {
                        $id = $this->overview()['last_good']['s3'] ?? null;
                        foreach ($this->overview()['runs'] as $run) {
                            if ($run['run_id'] !== $id) { continue; }
                            return $this->time($run['created_at'], true) . ($run['remote_checked_at'] ? '. Last checked ' . $this->ago($run['remote_checked_at']) . '.' : '. Verify this copy before relying on it.');
                        }
                        return 'None recorded';
                    })->visible(fn () => ($this->configuration()['destination']['type'] ?? null) === 's3'),
                    TextEntry::make('overview_next')->label('Next automatic backup')->state(function () {
                        $schedule = $this->configuration()['schedule'] ?? null;
                        if (!$schedule || !$schedule['enabled']) { return 'Off. Turn on automatic backups in Settings.'; }
                        return $schedule['next_at'] ? $this->time($schedule['next_at'], true) . ', ' . strtolower(Messages::mode($this->configuration()['mode'])) : 'None in the next 8 days. Check the scheduled days in Settings.';
                    }),
                    // The exact directory is in Settings; the overview names the kind of place.
                    TextEntry::make('overview_storage')->label('Stored on')->state(function () {
                        $destination = $this->configuration()['destination'] ?? null;
                        if (!$destination) { return 'Unknown until settings are valid'; }
                        if ($destination['type'] === 's3' && !($this->configuration()['saved_keys']['s3'] ?? false)) { return 'This server. S3 keys need to be saved for bucket ' . $destination['s3_bucket'] . '.'; }
                        return $destination['type'] === 's3' ? 'This server and S3 bucket ' . $destination['s3_bucket'] : 'This server';
                    }),
                    TextEntry::make('overview_space')->label('Space used')->state(function () {
                        $space = $this->configuration()['space'] ?? null;
                        if (!$space || $space['used'] === null) { return 'Nothing stored yet'; }
                        $count = count(array_filter($this->overview()['runs'], static fn ($run) => $run['local']));
                        if (($space['state'] ?? null) === 'full') { return Messages::bytes($space['used']) . ' of ' . Messages::bytes($space['budget']) . '. Space limit reached; new backups stop.'; }
                        if ($count === 0) { return Messages::bytes($space['used']) . ' used by working files. No local backups. The limit is ' . Messages::bytes($space['budget']) . '.'; }
                        return Messages::bytes($space['used']) . ' of ' . Messages::bytes($space['budget']) . ', ' . $count . ' ' . ($count === 1 ? 'backup' : 'backups');
                    }),
                    TextEntry::make('overview_scheduler')->label('Scheduler')->state(function () {
                        $last = $this->overview()['scheduler']['last_tick_at'] ?? null;
                        return $last ? 'Last ran ' . $this->ago($last) . ($this->schedulerFresh() ? '' : '. Tasks are waiting for it.') : 'Not run yet';
                    })->color(fn () => $this->schedulerFresh() ? null : 'warning'),
                    TextEntry::make('overview_retention_warning')->label('Retention needs review')->color('warning')
                        ->state(function () {
                            $count = (int) ($this->configuration()['retention']['last_apply']['skipped_count'] ?? 0);
                            return $count . ($count === 1 ? ' copy was kept' : ' copies were kept') . ' because retention could not process it. Check row details before trying again.';
                        })
                        ->visible(fn () => ($this->configuration()['retention']['last_apply']['skipped_count'] ?? 0) > 0),
                    TextEntry::make('overview_test')->label('Storage test')->state(function () {
                        $test = $this->latestRequest('destination-test');
                        if (!$test) { return 'Not run yet. Use Test storage before relying on this destination.'; }
                        return match ($test['status']) {
                            'complete' => ($test['result_destination_id'] ?? null) === ($this->configuration()['destination']['id'] ?? null) && ($test['result_destination_id'] ?? null) !== null
                                ? 'Passed ' . $this->ago($test['finished_at']) . ' for the current destination.'
                                : 'Passed ' . $this->ago($test['finished_at']) . ' for earlier or unrecorded storage settings. Test again.',
                            'failed' => 'Failed ' . $this->ago($test['finished_at']) . '. ' . Messages::error($test['error']),
                            'cancelled' => 'The last test was cancelled.',
                            'expired' => 'The scheduler did not start the last test in time.',
                            default => 'Queued. ' . Messages::taskStatus($test['status'])[0] . '.',
                        };
                    })->color(fn () => ($this->latestRequest('destination-test')['status'] ?? null) === 'failed' ? 'danger' : null),
                ]),
            ]);
    }

    private function helpSections(): array
    {
        // Native copy handling keeps the raw command separate from its escaped, wrapping code block.
        $code = static function (string|\Closure $command): Text {
            $plain = static fn () => $command instanceof \Closure ? $command() : $command;
            return Text::make(static fn () => new HtmlString('<code>' . e($plain()) . '</code>'))
                ->fontFamily(FontFamily::Mono)->copyable(request()->isSecure())->copyableState($plain)
                ->tooltip(request()->isSecure() ? 'Copy exact command' : null)->copyMessage('Command copied')
                ->extraAttributes(['data-backups-command' => true, 'style' => 'display: block; white-space: pre-wrap; overflow-wrap: anywhere; text-align: start; padding: .75rem; border: 1px solid color-mix(in srgb, currentColor 18%, transparent); border-radius: .5rem; background: color-mix(in srgb, currentColor 4%, transparent);']);
        };
        return [
            Section::make('What a backup contains')->schema([
                Text::make('A full backup holds the database and every file in Paymenter\'s directory, including .env, APP_KEY, uploads and extensions. The site shows its maintenance page while data is copied, subject to the limits under Advanced. Compression and verification run after the site reopens.'),
                Text::make('A database-only backup keeps the site online and holds only the database. Restoring it needs a separate copy of Paymenter\'s files. Both types require the schema-change confirmation in Host checks.'),
                Text::make('Archives contain customer data and secrets. Keep downloads private. Saved S3 keys and HTTP Basic credentials are never included.'),
            ]),
            Section::make('Server scheduler')->schema([
                Text::make('Backup actions queue tasks. Paymenter\'s scheduler runs them one at a time in the background. Tasks wait while the site is in maintenance mode or this extension is disabled, and unstarted tasks can expire.'),
                Text::make('Paymenter\'s server setup owns systemd timers and cron jobs. Backups does not install or change them. It registers tasks with Paymenter\'s scheduler and reports whether it is running.'),
                Text::make('The server must invoke schedule:run every minute. If check-ins stop, ask the server administrator to check Paymenter\'s systemd timer or cron job.'),
                Actions::make([Action::make('paymenterSchedulerGuide')->label('Paymenter installation guide')->color('gray')->link()->url('https://paymenter.org/docs/installation/install')->openUrlInNewTab()]),
                Section::make('Job coordination for full backups')->collapsible()->collapsed()->schema([
                    Text::make('Full backups require existing Paymenter scheduler and queue-worker jobs to use flock with a listed lock file. Keep the existing systemd timer or cron job; wrap its command rather than adding a second launcher.'),
                    Text::make('Example wrapper for the existing scheduler command. Add the lock path under Settings, Host checks:'),
                    $code(fn () => $this->jobWrapper()),
                    Text::make('Queue workers must also use a listed lock file. A continuous worker, including a systemd queue service, prevents full backups even when it uses flock. Database-only backups still work. This extension never installs or starts host jobs.'),
                ]),
                Text::make('Enabling or changing the schedule starts at its next future time. After an outage, missed times in the last eight days produce one fresh backup, possibly outside the planned time. Full backups then show the maintenance page. There is no backlog. A newer scheduled time replaces a catch-up that is still waiting. Maintenance and the required backup checks still apply.'),
                Text::make('Automatic backups are separate from enabling the extension. Turn them on in Settings, under Schedule.'),
            ]),
            Section::make('Terminal commands')->description('Run these as the Unix user that owns Paymenter. Offline status and recovery do not need Paymenter to start.')->schema([
                Text::make(fn () => request()->isSecure() ? 'Select a command or press Enter to copy its exact text. Define this function once per terminal session:' : 'Copy needs HTTPS. Select the command text to copy manually. Define this function once per terminal session:'),
                $code(fn () => $this->terminalFunction()),
                Text::make('Show backups and tasks:'),
                $code('paymenter_backups overview --offline'),
                Text::make('After an interrupted backup, check what needs fixing. This only reports and changes nothing:'),
                $code('paymenter_backups recover'),
                Text::make('Remove the unchanged maintenance page created by the backup. Run this only after reading the recovery check:'),
                $code('paymenter_backups recover --execute'),
                Text::make('If recovery cannot reopen the site safely, resolve the site by hand. Then acknowledge that backup so new backups can start:'),
                $code('paymenter_backups recover acknowledge --run BACKUP-ID'),
                Text::make('Copy a verified backup and its restore notes into a new private directory that does not exist yet. The backup ID is in each backup\'s Details:'),
                $code('paymenter_backups export BACKUP-ID --output /path/to/new-directory'),
            ]),
            Section::make('Restoring')->schema([
                Text::make('Restore into a new directory and a new database, never over the running site. The steps are in this guide:'),
                $code(dirname(__DIR__, 2) . '/docs/RECOVERY.md'),
                Text::make(fn () => 'Installation ID: ' . ($this->overview()['installation_uuid'] ?? 'not created yet') . '. It is added to this site\'s S3 object prefix.'),
            ]),
        ];
    }

    private function safePrivateRoot(): ?string
    {
        try { return Runtime::privateRoot(); } catch (\Throwable $error) { return null; }
    }

    private function defaultBackupDirectory(): ?string
    {
        $root = $this->safePrivateRoot();
        if ($root === null) { return null; }
        $directory = 'backups';
        if (@is_file($root . '/installation.json')) {
            try {
                $identity = PrivateStore::read($root . '/installation.json');
                $directory = $identity['default_local_directory'] ?? 'artifacts';
            } catch (\Throwable $error) { return null; }
        }
        return in_array($directory, ['backups', 'artifacts'], true) ? $root . '/' . $directory : null;
    }

    // ---------------------------------------------------------------- Settings form

    /** Native, keyboard- and touch-accessible detail for an unfamiliar field. */
    private function fieldHelp(string $name, string $heading, string $detail): Action
    {
        return Action::make($name)->label('Help for ' . $heading)->icon('ri-question-line')->iconButton()->color('gray')
            ->tooltip('Help for ' . $heading)->modalHeading($heading)->modalDescription($detail)
            ->modalSubmitAction(false)->modalCancelActionLabel('Close');
    }

    private function billingWindowHelp(Get $get): ?string
    {
        $billing = $this->configuration()['billing'] ?? null;
        if (!$billing || !$billing['time']) { return null; }
        $margin = max(0, (int) $get('billing_margin_seconds'));
        $before = max(0, (int) $get('lock_timeout')) + max(0, (int) $get('drain_seconds')) + max(1, (int) $get('capture_timeout')) + 30 + $margin;
        $after = 60 + $margin;
        return 'Billing runs at ' . $billing['time'] . ' ' . $billing['timezone'] . '. Full backups avoid ' . $before . ' seconds before and ' . $after . ' seconds after.';
    }

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
                Section::make('Schedule')->description('Automatic backups use Paymenter\'s scheduler. After a scheduler outage, one fresh backup can run outside the planned time. Full backups show the maintenance page.')->schema([
                    Toggle::make('enabled')->label('Create backups automatically')->live(),
                    Grid::make(['default' => 1, 'md' => 3])->schema([
                        Radio::make('schedule_frequency')->label('Frequency')->options(Settings::SELECTS['schedule_frequency'])->required()->inline()->live(),
                        TextInput::make('schedule_time')->label('Time')->type('time')->required()->regex('/^([01][0-9]|2[0-3]):[0-5][0-9]$/')
                            ->helperText(fn (Get $get) => $this->billingWindowHelp($get)),
                        Select::make('schedule_timezone')->label('Time zone')->options($zones)->searchable()->required()->rule('timezone')
                            ->helperText('UTC avoids daylight saving changes. Skipped local times do not run; repeated times run once.'),
                    ])->visible(fn (Get $get) => (bool) $get('enabled')),
                    CheckboxList::make('schedule_weekdays')->label('Days')->options(array_map('strval', Messages::WEEKDAYS))->columns(['default' => 2, 'md' => 4])
                        ->required(fn (Get $get) => (bool) $get('enabled'))
                        ->rules(['array'])->nestedRecursiveRules(['in:0,1,2,3,4,5,6'])
                        ->validationMessages(['required' => 'Choose at least one day.'])
                        ->visible(fn (Get $get) => (bool) $get('enabled') && $get('schedule_frequency') === 'weekly'),
                ]),
                Section::make('Storage')->schema([
                    Radio::make('destination')->label('Backup storage')->required()->live()
                        ->options(Settings::SELECTS['destination'])
                        ->descriptions([
                            'local' => 'A private backup directory on this server.',
                            's3' => 'A local backup, plus a copy in an S3-compatible bucket after each backup.',
                        ]),
                    $path('local_path', 'Backup directory', 'Optional. Leave empty for the default below. A new path affects new backups only.')
                        ->hintAction($this->fieldHelp('backupDirectoryHelp', 'Backup directory', 'Archives are written here. The directory must be private, owned by the Unix site user, and outside Paymenter and every public web directory. Changing it affects new backups only. Existing archives keep their recorded paths.')),
                    Text::make(fn () => new HtmlString('Default directory: <code>' . e($this->defaultBackupDirectory() ?? 'unavailable until the extension data directory is valid') . '</code>')),

                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        TextInput::make('s3_bucket')->label('Bucket')->helperText('Existing bucket name only, without s3:// or a web address.')->required(fn (Get $get) => $get('destination') === 's3'),
                        TextInput::make('s3_region')->label('Region')->placeholder('us-east-1')->helperText('Use the region required by your S3 provider.'),
                        TextInput::make('s3_endpoint')->label('Endpoint')->placeholder('https://s3.example.com')
                            ->helperText('Leave empty for AWS. Other providers need an https:// address.')
                            ->hintAction($this->fieldHelp('s3EndpointHelp', 'S3 endpoint', 'AWS uses its standard regional endpoint when this field is empty. For another S3-compatible provider, enter its HTTPS API address. This is not a bucket website or an administrator login address.'))
                            ->rules(['nullable', 'regex:/^https:\/\/[^\s\/?#@]+(?:\/[^\s?#]*)?$/D'])->validationMessages(['regex' => 'Enter an https:// address without a user name or query.']),
                        TextInput::make('s3_prefix')->label('Object prefix')->helperText('Added to object names with this installation\'s ID. Earlier copies stay under their original prefix.')->required()
                            ->rules(['string', 'regex:' . Settings::S3_PREFIX])
                            ->validationMessages(['regex' => 'Use prefix parts separated by /, without "." or "..".']),
                        Toggle::make('s3_path_style')->label('Use path-style URLs')->helperText('Turn on if your provider needs the bucket name in the path.')
                            ->hintAction($this->fieldHelp('s3PathStyleHelp', 'Path-style URLs', 'With path-style URLs, the bucket name is part of the address path. Some S3-compatible providers require this. Use your provider\'s recommended setting.')),
                    ])->visible(fn (Get $get) => $get('destination') === 's3'),
                    Checkbox::make('plaintext_offsite_acknowledged')->label('I understand archives in S3 are not encrypted')
                        ->helperText('They contain customer data, the .env file and APP_KEY. Limit bucket access to this site\'s keys.')
                        ->accepted(fn (Get $get) => $get('destination') === 's3')
                        ->validationMessages(['accepted' => 'Confirm this before using S3.'])
                        ->visible(fn (Get $get) => $get('destination') === 's3'),
                ]),
                Section::make('Host checks')
                    ->description(fn (Get $get) => $full($get)
                        ? 'Full backups pause Paymenter\'s scheduled jobs and show the maintenance page while data is copied. Confirm only what is true for this server.'
                        : 'Confirm only what is true for this server.')
                    ->schema([
                        $lineList('job_lock_paths', 'Job lock files', 'One path per line. Each Paymenter scheduled job must run through flock with one of these files. Help explains job coordination.')
                            ->hintAction($this->fieldHelp('cronLockHelp', 'Job lock files', 'Use the lock paths from this Unix user\'s systemd services or crontab. Every Paymenter scheduler and queue worker must use flock with a listed lock. Full backups hold those locks until the copy finishes. The Help tab shows a wrapper for the existing launcher.'))
                            ->visible($full),
                        Checkbox::make('cooperative_writers_acknowledged')->label('Only Paymenter and its scheduled jobs write to its files and database.')->visible($full),
                        Checkbox::make('external_state_acknowledged')->label('Paymenter keeps no data in Redis, object storage or other outside services.')->visible($full),
                        Checkbox::make('no_concurrent_ddl_acknowledged')->label('Database migrations and other schema changes never run during a backup.'),
                    ]),
                Section::make('Retention and space')->schema([
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        $integer('retention_keep', 'Local backups to keep', 'backups', 'Keep this many verified backups on this server. At least one is always kept.'),
                        Toggle::make('automatic_retention')->label('Delete older backups automatically')->helperText('Runs after backups and S3 copies. When off, use Apply retention to delete older copies.')->inline(false),
                        Select::make('s3_retention_policy')->label('S3 retention')->options(Settings::SELECTS['s3_retention_policy'])->required()->live()
                            ->helperText('Choose when to delete S3 copies. The newest S3 copy is always kept.')
                            ->visible(fn (Get $get) => $get('destination') === 's3'),
                        $integer('s3_retention_keep', 'S3 copies to keep', 'copies', 'Keep the newest S3 copies, independently of the local count.')
                            ->visible(fn (Get $get) => $get('destination') === 's3' && $get('s3_retention_policy') === 'count'),
                        $integer('s3_retention_days', 'Keep S3 copies for', 'days', 'Delete older S3 copies by backup creation date. The newest S3 copy stays.')
                            ->visible(fn (Get $get) => $get('destination') === 's3' && $get('s3_retention_policy') === 'age'),
                        $integer('local_byte_budget', 'Space limit', 'MiB', 'Total limit for the extension data and backup directories. New backups stop at the limit.'),
                        $integer('minimum_free_bytes', 'Keep this much disk free', 'MiB', 'Backups stop before free space drops below this.'),
                    ]),
                ]),
                Section::make('Advanced')->description('Time limits, directories and programs. Most sites keep the defaults.')
                    ->collapsible()->collapsed()->schema([
                        Fieldset::make('Time limits')->columns(['default' => 1, 'md' => 2])->schema([
                            $integer('capture_timeout', 'Capture time limit', 'seconds', 'Longest time to export the database and copy files.'),
                            $integer('drain_seconds', 'Wait before capture', 'seconds', 'Time for open requests to finish after maintenance begins, before copying starts.'),
                            $integer('lock_timeout', 'Job lock wait', 'seconds', 'How long to wait for running jobs.'),
                            $integer('billing_margin_seconds', 'Billing safety margin', 'seconds', 'Gap kept around Paymenter\'s daily billing run.'),
                            $integer('billing_guard_max_age', 'Scheduler report age', 'seconds', 'Full backups need a scheduler billing-time report newer than this.'),
                            $integer('request_max_age', 'Task expiry', 'seconds', 'Queued tasks expire if they have not started within this time after being queued. 86400 is one day.'),
                            $integer('s3_timeout', 'S3 timeout', 'seconds', 'Maximum wait for each S3 connection or response.'),
                            $integer('s3_retries', 'S3 attempts per request', 'attempts', 'Includes the first attempt. Failed copies keep the local backup.'),
                        ]),
                        Fieldset::make('Directories and programs')->columns(['default' => 1, 'md' => 2])->schema([
                            $path('private_root', 'Extension data directory', 'Holds the installation ID, encrypted keys and backup history. Set it before the first save. It cannot be changed after setup.', dirname(base_path()) . '/paymenter-backups-private')
                                ->disabled(fn () => $this->initialized()),
                            $lineList('extra_docroots', 'Other public web directories', 'One path per line, for every site this account serves. Backups never go inside them.'),
                            $path('php', 'PHP program', 'Command-line PHP. Leave empty to detect it.', PHP_BINDIR . '/php'),
                            $path('python', 'Python program', 'Absolute path to Python 3.10 or newer.'),
                            $path('dump', 'Database export program', 'Absolute path to mariadb-dump or mysqldump.'),
                            $path('mysql', 'Database client program', 'Absolute path to mariadb or mysql, used to inspect the database.'),
                            $path('tar', 'tar program', 'Absolute path to GNU tar, used to copy and archive files.'),
                            $path('gzip', 'gzip program', 'Absolute path to gzip, used to compress archives.'),
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
        $this->malformedKeys = [];
        foreach (Settings::DEFAULTS as $key => $default) {
            try {
                if (array_key_exists($key, $rows) && \Illuminate\Support\Facades\Validator::make([$key => $rows[$key]], [$key => Settings::rules($key)])->fails()) {
                    throw new \RuntimeException('config-invalid');
                }
                $value = Settings::normalize(array_key_exists($key, $rows) ? [$key => $rows[$key]] : [])[$key];
            } catch (\Throwable $error) { $this->malformedKeys[] = $key; $value = $default; }
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
        return in_array($key, $this->malformedKeys, true) || json_encode($this->engineValue($key, $value)) !== json_encode($this->engineValue($key, $before));
    }

    public function saveSettings(): void
    {
        $this->authorizeAction();
        $state = $this->settingsForm->getState();
        $this->storedForm = null;
        $stored = $this->formValues();
        if (in_array('extra_docroots', $this->malformedKeys, true)) {
            // Never substitute an empty default for a malformed public-directory guard.
            $this->refuseSettings('document-roots-invalid');
            return;
        }
        $changes = [];
        foreach ($state as $key => $value) {
            if (array_key_exists($key, Settings::DEFAULTS) && $this->changed($key, $value)) { $changes[$key] = $this->engineValue($key, $value); }
        }
        foreach ($this->malformedKeys as $key) {
            // Hidden optional fields still need their invalid native rows repaired.
            if (!array_key_exists($key, $changes)) { $changes[$key] = $this->engineValue($key, $stored[$key]); }
        }
        $repairedKeys = array_values(array_intersect(array_keys($changes), $this->malformedKeys));
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
            if ($repairedKeys) {
                $labels = array_map(static fn ($key) => Settings::LABELS[$key][0], $repairedKeys);
                Notification::make()->title('Settings saved with corrections')->warning()
                    ->body('Invalid saved values were corrected. Review ' . implode(', ', $labels) . '.')->send();
                return;
            }
            Notification::make()->title($initialized ? 'Settings saved' : 'Settings saved. The extension data directory is ready.')->success()->send();
            return;
        }
        $this->overview();
        Notification::make()->title('Paths saved')->warning()
            ->body($this->overviewError ? 'The backup program still cannot start. ' . Messages::error($this->overviewError) : 'The backup program starts again. Review the other settings.')->send();
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
            ->body($environment ? 'The backup program could not check them. ' . $message . ' Save the paths under Advanced on their own first.' : $message)->send();
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
                        ->helperText('Optional. Only for temporary keys; leave empty otherwise.'),
                ]),
            ])->livewireSubmitHandler('saveS3Keys')->footer([
                Actions::make([
                    Action::make('saveS3Keys')->label(fn () => ($this->configuration()['saved_keys']['s3'] ?? false) ? 'Replace S3 keys' : 'Save S3 keys')->submit('saveS3Keys'),
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
            ->modalDescription('Removes the S3 keys saved here. While S3 is selected, Create backup is unavailable. Automatic backups can still capture locally, then report failed S3 tasks. S3 upload, verification, retrieval, retention and deletion stop until keys are saved. Existing copies stay; provider keys are not revoked. HTTP Basic credentials are kept. Choose This server to continue with local storage.')
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

    private function siteCredentialSummary(): string
    {
        $site = $this->configuration()['site_password'] ?? [];
        $saved = $this->configuration()['saved_keys']['site'] ?? false;
        return match ($site['state'] ?? 'unavailable') {
            'http' => $saved ? 'Saved credentials cannot be used with HTTP. Full backups cannot start. Expand to remove them.' : '',
            'unavailable' => $saved ? 'App URL is invalid. Full backups cannot start. Expand to remove credentials or correct App URL.' : '',
            'origin-changed' => 'Saved credentials belong to another App URL. Expand to replace or remove them.',
            'saved' => 'Credentials saved.',
            default => '',
        };
    }

    private function sitePasswordDescription(): string
    {
        if (!$this->initialized()) { return 'Save settings before saving HTTP Basic credentials.'; }
        $site = $this->configuration()['site_password'] ?? [];
        $saved = $this->configuration()['saved_keys']['site'] ?? false;
        return match ($site['state'] ?? 'unavailable') {
            'http' => $saved ? 'App URL in Paymenter settings uses http://. Remove the saved credentials, or restore an HTTPS App URL, before a full backup.'
                : 'App URL in Paymenter settings uses http://. Full backups check it anonymously. HTTP Basic credentials require HTTPS.',
            'unavailable' => 'App URL in Paymenter settings is not valid for this check. Correct it there. Saved credentials can still be removed here.',
            'origin-changed' => 'HTTP Basic credentials are saved for a different App URL. Save both values for the current HTTPS address or remove them before a full backup.',
            'saved' => 'HTTP Basic credentials are saved and encrypted. Saved values are hidden. Enter both values and click Replace credentials to replace them.',
            default => 'No credentials saved. Full backups check App URL without HTTP Basic authentication.',
        };
    }

    public function siteAccessForm(Schema $schema): Schema
    {
        return $schema->components([
            Form::make([
                Text::make(fn () => 'Credentials are sent only to this HTTPS origin: ' . ($this->httpsOrigin() ?? 'unavailable') . '.'),
                Grid::make(['default' => 1, 'md' => 2])->schema([
                    // Required to save, but the section itself is optional, so no asterisk.
                    TextInput::make('username')->label('HTTP Basic username')->helperText('The username requested by the browser password prompt.')->hintAction($this->fieldHelp('httpBasicHelp', 'HTTP Basic authentication', 'A web server can require a browser username and password before Paymenter loads. Enter those credentials here only if this site uses that prompt. They are sent to the Paymenter HTTPS address shown here to check its maintenance page. Saving or removing them does not change any account or server password.'))->required()->markAsRequired(false)->maxLength(8192)->autocomplete('off')
                        ->rule('not_regex:/[:\x00-\x1f\x7f]/')->validationMessages(['not_regex' => 'HTTP Basic usernames cannot contain a colon or control characters.']),
                    TextInput::make('password')->label('HTTP Basic password')->helperText('The password requested by that prompt.')->password()->revealable(false)->required()->markAsRequired(false)->maxLength(8192)->autocomplete('new-password'),
                ]),
            ])->livewireSubmitHandler('saveSitePassword')->footer([
                Actions::make([
                    Action::make('saveSitePassword')->label(fn () => ($this->configuration()['saved_keys']['site'] ?? false) ? 'Replace credentials' : 'Save credentials')->submit('saveSitePassword'),
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
            Notification::make()->title('HTTP Basic credentials saved')->success()->send();
        } catch (\Throwable $error) {
            $code = $error instanceof \RuntimeException ? $error->getMessage() : 'credentials-save-failed';
            Notification::make()->title('HTTP Basic credentials not saved')->danger()->body(Messages::error($code))->send();
        } finally {
            unset($data);
        }
    }

    public function removeSitePasswordAction(): Action
    {
        return Action::make('removeSitePassword')->label('Remove credentials')->color('danger')->link()
            ->visible(fn () => $this->configuration()['saved_keys']['site'] ?? false)
            ->requiresConfirmation()->modalHeading('Remove saved HTTP Basic credentials?')
            ->modalDescription('Removes the credentials saved by this extension. Full backups will check the site without HTTP Basic authentication. The server password and S3 keys stay unchanged.')
            ->modalSubmitActionLabel('Remove credentials')
            ->action(function () {
                $this->authorizeAction();
                try {
                    Runtime::credentials(['clear' => true], true);
                    $this->overviewCache = null;
                    Notification::make()->title('HTTP Basic credentials removed')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('HTTP Basic credentials not removed')->danger()->body(Messages::error($error->getMessage()))->send();
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
            Notification::make()->title(str_replace(' queued', ' not queued', $title))->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable'))->send();
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
            ->modalDescription('It starts when Paymenter\'s scheduler next runs and continues in the background. You can leave this page.')
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
                        Action::make($name . 'OpenSettings')->label('Open settings')->color('primary')->link()->url(fn () => static::getUrl(['tab' => 'settings'])),
                        Action::make($name . 'SchedulerHelp')->label('Paymenter scheduler setup')->color('primary')->link()->url('https://paymenter.org/docs/installation/install')->openUrlInNewTab()
                            ->visible(fn () => !$this->schedulerFresh()),
                    ])
                    ->visible(fn () => $this->blockers('full-quiesced') !== [] || $this->blockers('database-online') !== []),
                Callout::make('It may wait before starting')->info()
                    ->description('It waits for Paymenter\'s daily billing run or running jobs to finish first.')
                    ->visible(fn (Get $get) => array_intersect($this->configuration()['admission'][$get('mode') ?? 'full-quiesced'] ?? [], self::TRANSIENT) !== []),
                Callout::make('Waiting for Paymenter\'s scheduler')->info()
                    ->description('The backup starts when Paymenter\'s scheduler next runs.')
                    ->visible(fn () => !$this->schedulerFresh() && !$this->nothingCanStart()),
                Callout::make('A backup is already queued')->info()
                    ->description('The same backup type is already queued. Choosing it again adds no second task. Its row shows when it starts.')
                    ->visible(fn () => $this->activeRequest('run') !== null),
            ])
            ->modalSubmitAction(fn (Action $action) => $this->nothingCanStart() ? false : $action)
            ->modalSubmitActionLabel('Create backup')
            ->modalCancelActionLabel(fn () => $this->nothingCanStart() ? 'Close' : 'Cancel')
            ->action(function (array $data, Action $action) {
                $mode = in_array($data['mode'] ?? null, ['full-quiesced', 'database-online'], true) ? $data['mode'] : null;
                if ($mode === null || ($issues = $this->issueSentences($mode)) !== []) {
                    Notification::make()->title('Backup not queued')->danger()->body(implode(' ', $issues ?? ['Choose a backup type that can start.']))->send();
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
            ->tooltip(fn () => $this->activeRequest('destination-test') ? 'A storage test is already queued.' : (($this->configuration()['destination']['type'] ?? null) === 's3' ? 'Writes, reads back and deletes a small test file in the S3 bucket.' : 'Writes, reads back and deletes a small test file in the backup directory.'))
            ->action(fn () => $this->queue('destination-test', [], 'Storage test queued'));
    }

    private function retentionHasCandidates(): bool
    {
        $policy = $this->configuration()['retention'] ?? [];
        $pending = $policy['pending'] ?? null;
        if (is_array($pending) && $pending['local'] !== null && $pending['s3'] !== null) { return $pending['local'] + $pending['s3'] > 0; }
        $ready = array_filter($this->overview()['runs'], static fn ($run) => $run['status'] === 'ready');
        $local = array_filter($ready, static fn ($run) => $run['local']);
        if (count($local) > ($policy['local_keep'] ?? $policy['keep'] ?? PHP_INT_MAX)) { return true; }
        if (($this->configuration()['destination']['type'] ?? null) !== 's3') { return false; }
        $remote = array_filter($ready, static fn ($run) => in_array($run['remote'], ['delivered', 'present'], true));
        if (count($remote) <= 1) { return false; }
        return match ($policy['s3_policy'] ?? 'local-count') {
            'keep-all' => false,
            'count' => count($remote) > ($policy['s3_keep'] ?? PHP_INT_MAX),
            'age' => (bool) array_filter($remote, static fn ($run) => $run['created_at'] < time() - ($policy['s3_days'] ?? PHP_INT_MAX) * 86400),
            default => count($remote) > ($policy['local_keep'] ?? $policy['keep'] ?? PHP_INT_MAX),
        };
    }

    private function retentionDescription(): string
    {
        $policy = $this->configuration()['retention'] ?? [];
        $description = 'Keeps the newest ' . ($policy['local_keep'] ?? $policy['keep'] ?? 7) . ' verified local backups and deletes older local copies.';
        if (($this->configuration()['destination']['type'] ?? null) === 's3') {
            $description .= ' ' . match ($policy['s3_policy'] ?? 'local-count') {
                'keep-all' => 'Keeps every S3 copy.',
                'count' => 'Keeps the newest ' . ($policy['s3_keep'] ?? 30) . ' verified S3 copies.',
                'age' => 'Deletes S3 copies created more than ' . ($policy['s3_days'] ?? 90) . ' days ago.',
                default => 'Uses the local backup count for S3 copies.',
            };
        }
        return $description . ' The last verified copy at each location is kept. Copies that fail verification, local copies awaiting S3 delivery, copies in earlier S3 locations and recently retrieved copies are kept.';
    }

    public function applyRetentionAction(): Action
    {
        return Action::make('applyRetention')->label('Apply retention')->color('gray')
            ->visible(fn () => $this->retentionHasCandidates())
            ->requiresConfirmation()->modalHeading('Apply retention now?')
            ->modalDescription(fn () => $this->retentionDescription())
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
                    ->visible(fn (array $record) => $record['kind'] === 'run' && $record['run']['download'] === 'ready'),
                Action::make('prepareDownload')->label('Retrieve from S3')->icon('ri-download-2-line')->button()->color('gray')
                    ->tooltip('Queues retrieval and verification from S3. Download appears when ready. The retrieved copy is kept for at least 24 hours.')
                    ->visible(fn (array $record) => $record['kind'] === 'run' && $record['run']['download'] === 'retrieve'
                        && !$this->activeRequest('retrieve', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('retrieve', ['run_id' => $record['run_id']], 'S3 retrieval queued')),
                Action::make('verifyFirst')->label('Verify')->icon('ri-shield-check-line')->button()->color('gray')
                    ->tooltip('Checks the whole backup. A copy stored only in S3 must also be retrieved before downloading.')
                    ->visible(fn (array $record) => $record['kind'] === 'run' && in_array($record['run']['status'], ['ready', 'damaged'], true)
                        && !in_array($record['run']['download'], ['ready', 'retrieve'], true) && !$this->activeRequest('verify', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                Action::make('cancelQueued')->label('Cancel')->button()->color('gray')
                    ->visible(fn (array $record) => $record['kind'] === 'request' && $record['cancel_request'] !== null)
                    ->requiresConfirmation()->modalHeading('Cancel this queued backup?')->modalDescription('It has not started. The site stays unchanged and later scheduled backups still run.')
                    ->modalSubmitActionLabel('Cancel backup')->modalCancelActionLabel('Keep it queued')
                    ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                ActionGroup::make([
                    Action::make('details')->label('Details')->icon('ri-information-line')
                        ->modalHeading('Backup details')->modalSubmitAction(false)->modalCancelActionLabel('Close')
                        ->schema(fn (array $record) => $this->detailsSchema($record)),
                    Action::make('verify')->label('Verify again')->icon('ri-shield-check-line')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && $record['run']['status'] === 'ready'
                            && ($record['run']['download'] === 'ready' || (!$record['run']['local'] && in_array($record['run']['remote'], ['delivered', 'present'], true)))
                            && !$this->activeRequest('verify', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                    Action::make('copy')->label('Copy to S3')->icon('ri-upload-cloud-2-line')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && $record['run']['status'] === 'ready' && !in_array($record['run']['remote'], ['delivered', 'present'], true)
                            && ($this->configuration()['destination']['type'] ?? null) === 's3' && !$this->activeRequest('deliver', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('deliver', ['run_id' => $record['run_id']], 'Copy to S3 queued')),
                    Action::make('cancelTask')->label('Cancel task')->icon('ri-close-circle-line')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && $record['cancel_request'] !== null)
                        ->requiresConfirmation()->modalHeading('Cancel this task?')->modalDescription('The task has not started yet.')
                        ->modalSubmitActionLabel('Cancel task')->modalCancelActionLabel('Keep it queued')
                        ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                    Action::make('delete')->label('Delete')->icon('ri-delete-bin-line')->color('danger')
                        ->visible(fn (array $record) => $record['kind'] === 'run' && in_array($record['run']['status'], ['ready', 'damaged', 'delete-interrupted'], true)
                            && !$this->activeRequest('delete', $record['run_id']))
                        ->requiresConfirmation()
                        ->modalHeading(fn (array $record) => 'Delete the ' . ($record['run']['mode'] === 'database-online' ? 'database-only' : 'full') . ' backup from ' . $this->time($record['created_at']) . '?')
                        ->modalDescription('Choose the copies to remove. The preview explains what will be deleted.')
                        ->fillForm(fn (array $record) => ['location' => $this->defaultDeletionLocation($record)])
                        ->schema(fn (array $record) => [
                            Radio::make('location')->label('Delete from')->required()->live()
                                ->options(fn () => array_filter(['all' => $record['run']['local'] && in_array($record['run']['remote'], ['delivered', 'present'], true) ? 'All stored copies' : null, 'local' => $record['run']['local'] ? 'This server only' : null, 's3' => !$record['run']['local'] || in_array($record['run']['remote'], ['delivered', 'present'], true) ? 'S3 only' : null]))
                                ->rule('in:all,local,s3'),
                            Callout::make('Deletion preview')->warning()
                                ->description(fn () => $this->deleteDescription($record['run_id'], $this->currentDeletionLocation($record))),
                        ])
                        ->modalSubmitAction(fn (Action $action, array $record) => $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal'] ? false : $action)
                        ->modalSubmitActionLabel('Delete selected copies')
                        ->action(fn (array $record, array $data) => $this->queue('delete', ['run_id' => $record['run_id'], 'location' => $data['location'], 'execute' => true], 'Deletion queued')),
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
            Notification::make()->title('Task cancelled')->success()->send();
        } catch (\Throwable $error) {
            Notification::make()->title('Task not cancelled')->danger()->body(Messages::error($error->getMessage()))->send();
        }
    }

    private function defaultDeletionLocation(array $record): string
    {
        return $record['run']['local'] && in_array($record['run']['remote'], ['delivered', 'present'], true)
            ? 'all' : ($record['run']['local'] ? 'local' : 's3');
    }

    private function currentDeletionLocation(array $record): string
    {
        // getData() is populated only after submission. The original mounted
        // action owns the live radio state; the injected footer action does not.
        $location = $this->getMountedAction()?->getRawData()['location'] ?? $this->defaultDeletionLocation($record);
        return in_array($location, ['local', 's3', 'all'], true) ? $location : 'invalid';
    }

    private function deletePreview(string $runId, string $location = 'all'): array
    {
        $key = $runId . ':' . $location;
        if (isset($this->deletePreviews[$key])) { return $this->deletePreviews[$key]; }
        try {
            $preview = Runtime::command(['delete', $runId, '--location', $location], null, 20);
        } catch (\Throwable $error) {
            $preview = ['refusal' => $error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable', 'local_copy' => false, 'remote_copy' => null, 'survivors' => []];
        }
        return $this->deletePreviews[$key] = $preview;
    }

    private function deleteDescription(string $runId, string $location = 'all'): HtmlString
    {
        $preview = $this->deletePreview($runId, $location);
        if ($preview['refusal']) {
            return new HtmlString(e('This backup cannot be deleted. ' . Messages::error($preview['refusal'])));
        }
        $items = [];
        if ($preview['local_copy']) { $items[] = 'Deletes the archive and its backup record from this server\'s backup directory.'; }
        if ($preview['remote_copy']) { $items[] = 'Deletes this backup from S3 bucket ' . $preview['remote_copy']['bucket'] . '. If the bucket keeps old versions, those remain.'; }
        $items[] = 'Another verified backup must remain at each selected storage location. The scheduler checks before deleting and keeps copies when that check fails.';
        $items[] = ($this->overview()['scheduler']['last_tick_at'] ?? null) === null
            ? 'Paymenter\'s scheduler has not run yet. Nothing is deleted until it does.'
            : 'Deletion runs when Paymenter\'s scheduler next runs.';
        return $this->issueList($items);
    }

    private function detailsSchema(array $record): array
    {
        if ($record['kind'] !== 'run') {
            return [
                Text::make('Task ID'),
                Text::make($record['request_id'])->fontFamily(FontFamily::Mono)->copyable(request()->isSecure()),
                TextEntry::make('d_status')->label('Status')->state($record['status_label'] . '. ' . $record['detail']),
            ];
        }
        $run = $record['run'];
        return [
            Text::make('Backup ID'),
            Text::make(new HtmlString('<code>' . e($run['run_id']) . '</code>'))->fontFamily(FontFamily::Mono)
                ->copyable(request()->isSecure())->copyableState($run['run_id']),
            Text::make('Use this ID in the terminal commands on Help.'),
            Grid::make(2)->schema([
                TextEntry::make('d_created')->label('Created')->state($this->time($run['created_at'], true)),
                TextEntry::make('d_type')->label('Type')->state(Messages::mode($run['mode'])),
                TextEntry::make('d_size')->label('Size')->state(Messages::bytes($run['size'])),
                TextEntry::make('d_location')->label('Stored on')->state($record['location']),
                TextEntry::make('d_verified')->label('Last verified')->state($run['verified_at'] ? $this->time($run['verified_at'], true) . ($run['verification_source'] === 'capture' ? ', when created' : '') : 'Not recorded'),
                TextEntry::make('d_status')->label('Status')->state($record['status_label']),
                TextEntry::make('d_retrieved')->label('Retrieved copy protected until')->state(fn () => $this->time($run['local_download_until'] ?? null, true))
                    ->helperText('Local retention keeps this retrieved copy until at least this time.')->visible((bool) ($run['local_download_until'] ?? null)),
            ]),
            TextEntry::make('d_detail')->label('Details')->state($record['detail'])->visible((bool) $record['detail']),
            TextEntry::make('d_code')->label('Error code for support')->state($run['error'] ?? $run['verification_error'] ?? $run['delivery_error'] ?? $run['retention_error'] ?? null)->fontFamily(FontFamily::Mono)
                ->visible((bool) ($run['error'] ?? $run['verification_error'] ?? $run['delivery_error'] ?? $run['retention_error'] ?? null)),
        ];
    }
}
