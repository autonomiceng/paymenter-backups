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
use Filament\Schemas\Components\Component;
use Filament\Schemas\Components\EmbeddedSchema;
use Filament\Schemas\Components\EmbeddedTable;
use Filament\Schemas\Components\Fieldset;
use Filament\Schemas\Components\Flex;
use Filament\Schemas\Components\Form;
use Filament\Schemas\Components\Grid;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Icon;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Components\Tabs;
use Filament\Schemas\Components\Tabs\Tab;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\UnorderedList;
use Filament\Schemas\Components\Utilities\Get;
use Filament\Schemas\Schema;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Enums\FontWeight;
use Filament\Support\Enums\Size;
use Filament\Support\Facades\FilamentView;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Concerns\InteractsWithTable;
use Filament\Tables\Contracts\HasTable;
use Filament\Tables\Table;
use Illuminate\Pagination\LengthAwarePaginator;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Route;
use Illuminate\Validation\ValidationException;
use Livewire\Attributes\Locked;
use Paymenter\Extensions\Others\Backups\Backups as Extension;
use Paymenter\Extensions\Others\Backups\ArchiveEncryption;
use Paymenter\Extensions\Others\Backups\CredentialInput;
use Paymenter\Extensions\Others\Backups\Http\DownloadController;
use Paymenter\Extensions\Others\Backups\Http\RecoveryKeyController;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\PrivateStore;
use Paymenter\Extensions\Others\Backups\Runtime;
use Paymenter\Extensions\Others\Backups\Settings;
use Paymenter\Extensions\Others\Backups\SettingsWriter;

class Backups extends Page implements HasTable
{
    use InteractsWithTable;
    use Concerns\LoadsOverview;
    use Concerns\BuildsReadiness;

    protected static string|\BackedEnum|null $navigationIcon = 'ri-save-line';
    protected static string|\BackedEnum|null $activeNavigationIcon = 'ri-save-fill';
    protected static string|\UnitEnum|null $navigationGroup = 'System';
    protected static ?string $title = 'Backups';
    /** Native page that renders action modals before the tabs, not inside the Backups tab with the history table. */
    protected string $view = 'backups::page';

    /** Space settings are entered in GiB and stored in bytes. */
    private const GIB_FIELDS = ['local_byte_budget', 'minimum_free_bytes'];
    private const LINE_LISTS = ['job_lock_paths', 'extra_docroots'];
    private const WEEKDAY_FIELDS = ['full_weekdays', 'database_weekdays'];
    /** Paths the engine needs before it can check anything else. */
    private const BOOTSTRAP = ['private_root', 'extra_docroots', 'python', 'php'];
    private const ENVIRONMENT_ERRORS = ['python-binary-invalid', 'php-binary-mismatch', 'private-root-invalid', 'private-root-unavailable', 'document-roots-invalid', 'engine-unavailable', 'installation-mismatch', 'owner-mismatch', 'private-state-permissions'];
    /** Admission results that only delay a queued backup. */
    private const TRANSIENT = ['billing-window-protected', 'billing-guard-stale', 'job-lock-busy', 'busy'];
    private const SCHEDULER_STALE_SECONDS = 300;
    /** Engine run modes, in the order the page shows them. */
    private const KINDS = ['database-online' => 'database', 'full-quiesced' => 'full'];
    private const KIND_LABELS = ['database-online' => 'Database backups', 'full-quiesced' => 'Full backups'];
    private const ACTIVE = ['pending', 'deferred', 'launching', 'running'];
    /** "Runs at 00:15, 06:15, …" for the hourly database schedule, evaluated in the browser. */
    private const HOURLY_SENTENCE_JS = <<<'JS'
        (() => {
            const every = [1, 2, 3, 4, 6, 8, 12].includes(parseInt($get('database_every_hours'))) ? parseInt($get('database_every_hours')) : 1;
            const minute = Math.min(59, Math.max(0, parseInt($get('database_minute')) || 0));
            const zone = $get('schedule_timezone') ? ' ' + $get('schedule_timezone') : '';
            const pad = (value) => String(value).padStart(2, '0');
            if (every === 1) { return 'Runs every hour at :' + pad(minute) + zone + '.'; }
            const slots = [];
            for (let hour = 0; hour < 24; hour += every) { slots.push(pad(hour) + ':' + pad(minute)); }
            return 'Runs at ' + (slots.length <= 4 ? slots.slice(0, -1).join(', ') + ' and ' + slots[slots.length - 1] : slots.slice(0, 3).join(', ') + ' and so on') + zone + '.';
        })()
        JS;
    /** Codes about Paymenter's cron job itself, shown as one item. */
    private const CRON_CODES = ['scheduler-missing', 'billing-guard-stale'];
    /** Checks that only a running cron job refreshes. */
    private const CRON_QUIET = ['job-discovery-unknown', 'job-discovery-stale', 'discovery-stale'];
    /** Setup items that inform but never stop Create backup. */
    private const ADVISORY = ['billing-window-protected', 'job-lock-busy', 'busy', 'storage-untested',
        // Uploads wait for these; backups on this server still run.
        'archive-key-required', 'archive-key-save-required', 'archive-encryption-tool-missing', 'archive-encryption-tool-unusable'];

    /** Nonsecret settings form state. */
    public ?array $settings = [];
    /** Write-only. Reset on every response. */
    public ?array $s3Keys = [];
    /** Write-only. Reset on every response. The site address comes from Paymenter, never the browser. */
    public ?array $siteAccess = [];
    /** The key choice persists; the passphrase is write-only and reset on every response. */
    public ?array $encryptionInput = ['source' => 'generate', 'passphrase' => null];
    public bool $showDeleted = false;
    /** Nonsecret fingerprint of the recovery key this administrator was shown or created. */
    #[Locked]
    public ?string $recoveryFingerprint = null;
    /** Exact key named when the acknowledgement dialog opened. */
    #[Locked]
    public ?string $acknowledgementFingerprint = null;
    /** Server-set window in which this component may render the key it was granted, after the confirmed Show step. */
    #[Locked]
    public ?int $revealGrantUntil = null;
    #[Locked]
    public ?string $revealGrantKey = null;
    /** Nonsecret: which typed secret groups a response cleared before they were saved. */
    #[Locked]
    public array $secretsCleared = [];

    private ?array $overviewCache = null;
    /** Setup items for the current overview, recomputed when the overview is read again. */
    private ?array $setupCache = null;
    private ?array $setupCacheFor = null;
    /** Current native rows in form shape; the server-side baseline for change detection. */
    private ?array $storedForm = null;
    private int $storedVersion = 2;
    private array $malformedKeys = [];
    private ?array $storedRows = null;
    #[Locked]
    public ?string $publicDirectoriesRepairPin = null;
    private ?string $overviewError = null;
    private array $deletePreviews = [];
    /** Revealed key for this request only; never a public property. */
    private ?array $revealedKey = null;
    private bool $collectingErrors = false;
    /** Secret groups this request actually saved; the others were cleared unsaved. */
    private array $secretsSaved = [];
    private const REVEAL_GRANT = 'backups.recovery_key_reveal';

    public static function canAccess(): bool
    {
        return Auth::user()?->hasPermission('admin.extensions.update') ?? false;
    }

    private function authorizeAction(): void { abort_unless(static::canAccess(), 403); }

    /** Download and recovery-key routes live in the panel's authenticated route group. */
    public static function routes(Panel $panel, ?PageConfiguration $configuration = null): void
    {
        parent::routes($panel, $configuration);
        Route::get(static::getRoutePath($panel) . '/download/{run}', DownloadController::class)
            ->middleware(static::getRouteMiddleware($panel))
            ->withoutMiddleware(static::getWithoutRouteMiddleware($panel))
            ->where('run', '[A-Za-z0-9_-]{1,100}')
            ->name(static::getRelativeRouteName($panel) . '.download');
        // Appends /recovery-key and .recovery-key; the route also requires a signed, five-minute link.
        RecoveryKeyController::register($panel, static::getRoutePath($panel), static::getRelativeRouteName($panel),
            static::getRouteMiddleware($panel), static::getWithoutRouteMiddleware($panel));
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
        $this->publicDirectoriesRepairPin = in_array('extra_docroots', $this->malformedKeys, true)
            ? hash('sha256', serialize($this->storedRows)) : null;
        $this->s3KeysForm->fill();
        $this->siteAccessForm->fill();
        $this->encryptionForm->fill(['source' => 'generate']);
    }

    /** Secrets never travel back to the browser, including after validation errors. */
    /** Runs before the view renders, so the note appears in the same response that clears the fields. */
    public function rendering(): void
    {
        $this->noteClearedSecrets();
    }

    public function dehydrate(): void
    {
        $this->noteClearedSecrets();
        $this->s3Keys = ['access_key' => null, 'secret_key' => null, 'session_token' => null];
        $this->siteAccess = ['username' => null, 'password' => null];
        $this->encryptionInput = ['source' => in_array($this->encryptionInput['source'] ?? null, ['generate', 'passphrase'], true) ? $this->encryptionInput['source'] : 'generate', 'passphrase' => null];
        $this->revealedKey = null;
    }

    /**
     * Typed secrets that this response clears without saving get an inline note,
     * so Save never silently keeps the old values the administrator meant to replace.
     */
    private function noteClearedSecrets(): void
    {
        $typed = [
            's3' => array_filter($this->s3Keys ?? [], 'filled') !== [],
            'site' => array_filter($this->siteAccess ?? [], 'filled') !== [],
            'passphrase' => filled($this->encryptionInput['passphrase'] ?? null),
        ];
        foreach ($typed as $group => $present) {
            if (isset($this->secretsSaved[$group])) { unset($this->secretsCleared[$group]); }
            elseif ($present) { $this->secretsCleared[$group] = true; }
        }
    }

    protected function onValidationError(ValidationException $exception): void
    {
        parent::onValidationError($exception);
        // Dialog fields show their own errors; only the settings form needs a notice.
        $settings = (bool) array_filter(array_keys($exception->errors()), static fn ($key) => (bool) preg_match('/^(settings|s3Keys|siteAccess|encryptionInput)\./', $key));
        if ($settings && !$this->collectingErrors) {
            Notification::make()->title('Settings not saved')->body('Fix the highlighted fields.')->danger()->send();
        }
    }

    // ---------------------------------------------------------------- Engine data

    /** One sentence about when queued work starts. Promises a minute only while the cron job checks in and nothing else is running. */
    private function startLine(): string
    {
        if (!$this->schedulerFresh()) {
            return $this->lastTick() ? 'Starts when Paymenter\'s cron job runs again (last run ' . $this->ago($this->lastTick()) . ').' : 'Starts once Paymenter\'s cron job is added and runs.';
        }
        return $this->hasActiveWork() ? 'Starts after the current task finishes.' : 'Starts within a minute.';
    }

    // ---------------------------------------------------------------- Readiness

    private static function shell(string $word): string
    {
        return preg_match('~^[A-Za-z0-9_@%+=:,./-]+$~D', $word) ? $word : escapeshellarg($word);
    }

    /** Cron parses percent before the shell, even inside quoted arguments. */
    private static function cronShell(string $word): string
    {
        return implode('\\%', array_map([self::class, 'shell'], explode('%', $word)));
    }

    private function safePrivateRoot(): ?string
    {
        try { return Runtime::privateRoot(); } catch (\Throwable $error) { return null; }
    }

    private function binary(string $tool, string $fallback): string
    {
        $checked = $this->checks()['tools'][$tool] ?? [];
        $configured = $this->formValues()[$tool] ?? '';
        return $configured ?: (is_string($checked['resolved'] ?? null) ? $checked['resolved'] : $fallback);
    }

    /** A shell function defined once, so each Help command stays one short line. */
    private function terminalFunction(): string
    {
        $words = [$this->binary('python', '/usr/bin/python3'), '-I', dirname(__DIR__, 2) . '/runtime/cli.py', '--app', base_path(), '--private-root', $this->safePrivateRoot() ?? '/path/to/private', '--php', $this->binary('php', PHP_BINDIR . '/php')];
        return 'paymenter_backups() { ' . implode(' ', array_map([self::class, 'shell'], $words)) . ' "$@"; }';
    }

    /** Cron line for Paymenter's scheduler, from the engine. The fallback mirrors its format. */
    private function schedulerLine(): string
    {
        $line = $this->checks()['jobs']['suggestions']['scheduler'] ?? null;
        if (is_string($line) && $line !== '') { return $line; }
        $root = $this->safePrivateRoot() ?? '/path/to/paymenter-backups-private';
        return '* * * * * /usr/bin/flock -n ' . self::cronShell($root . '/scheduler.lock') . ' ' . self::cronShell($this->binary('php', PHP_BINDIR . '/php')) . ' ' . self::cronShell(base_path('artisan')) . ' schedule:run >/dev/null 2>&1';
    }

    private function queueLine(): string
    {
        $line = $this->checks()['jobs']['suggestions']['queue'] ?? null;
        if (is_string($line) && $line !== '') { return $line; }
        $root = $this->safePrivateRoot() ?? '/path/to/paymenter-backups-private';
        return '* * * * * /usr/bin/flock -n ' . self::cronShell($root . '/queue.lock') . ' ' . self::cronShell($this->binary('php', PHP_BINDIR . '/php')) . ' ' . self::cronShell(base_path('artisan')) . ' queue:work --stop-when-empty --max-time=50 >/dev/null 2>&1';
    }

    /** Paymenter's daily billing time and the window full backups keep clear, as clock times. */
    private function billingWindow(): ?array
    {
        $billing = $this->checks()['billing'] ?? $this->configuration()['billing'] ?? null;
        if (!is_array($billing) || !is_string($billing['time'] ?? null) || !preg_match('/^\d{2}:\d{2}$/', $billing['time'])) { return null; }
        $zone = is_string($billing['timezone'] ?? null) && $billing['timezone'] !== '' ? $billing['timezone'] : 'UTC';
        if (is_string($billing['window_before'] ?? null) && preg_match(Settings::TIME, $billing['window_before'])
            && is_string($billing['window_after'] ?? null) && preg_match(Settings::TIME, $billing['window_after'])) {
            return ['time' => $billing['time'], 'timezone' => $zone, 'start' => $billing['window_before'], 'end' => $billing['window_after']];
        }
        $stored = $this->formValues();
        $margin = (int) ($stored['billing_margin_seconds'] ?? 120);
        $before = (int) $stored['lock_timeout'] + (int) $stored['drain_seconds'] + max(1, (int) $stored['capture_timeout']) + 30 + $margin;
        $after = 60 + $margin;
        try { $at = CarbonImmutable::createFromFormat('H:i', $billing['time'], $zone); } catch (\Throwable $error) { return null; }
        return ['time' => $billing['time'], 'timezone' => $zone, 'start' => $at->subSeconds($before)->format('H:i'), 'end' => $at->addSeconds($after)->format('H:i')];
    }

    private function billingSentence(): ?string
    {
        $window = $this->billingWindow();
        return $window ? 'Paymenter bills daily at ' . $window['time'] . ' ' . $window['timezone'] . '. Full backups won\'t start between ' . $window['start'] . ' and ' . $window['end'] . '.' : null;
    }

    // ---------------------------------------------------------------- Shared components

    /** Exact escaped text, copyable over HTTPS with click, Enter or Space. */
    private function command(string|\Closure $command): Text
    {
        $plain = static fn () => $command instanceof \Closure ? (string) $command() : $command;
        // Keep meaningful whitespace inside its own span: preserving whitespace
        // on the native Text wrapper would also show the template's indentation.
        return Text::make(static fn () => new \Illuminate\Support\HtmlString('<span style="white-space: pre-wrap">' . e($plain()) . '</span>'))->fontFamily(FontFamily::Mono)
            ->copyable(fn () => request()->isSecure())->copyableState($plain)->copyMessage('Copied')
            ->tooltip(fn () => request()->isSecure() ? 'Copy' : null)
            // The theme's input frame makes a native code block; long paths wrap inside it, copied text is unchanged.
            ->extraAttributes(['data-backups-command' => true, 'class' => 'fi-input-wrp w-full px-4 py-2 break-words', 'style' => 'overflow-wrap: anywhere']);
    }

    /** A bulleted list. Items are blocks so each bullet sits on the first line of a wrapped item. */
    private function bullets(array $lines): UnorderedList
    {
        return UnorderedList::make(array_map(static fn ($line) => Text::make($line)->extraAttributes(['style' => 'display: block']), $lines))->columns(1);
    }

    private function settingsUrl(string $anchor = 'settings'): string
    {
        return static::getUrl(['tab' => 'settings']) . ($anchor !== 'settings' ? '#' . $anchor : '');
    }

    private function link(string $name, string $label, string $url): Action
    {
        return Action::make($name)->label($label)->url($url)->button()->size(Size::Small)->color('gray');
    }

    /** One setup item: icon, title, one line, exact fixes and at most one action. */
    private function setupItem(array $item, string $prefix): Component
    {
        $tone = $item['tone'];
        $children = [Group::make([
            Text::make($item['title'])->weight(FontWeight::SemiBold)->color('gray'),
            Text::make($item['text']),
        ])->gap(false)];
        foreach ($item['fixes'] as $fix) {
            if (filled($fix['lead'] ?? null)) { $children[] = Text::make($fix['lead']); }
            $children[] = $this->command($fix['code']);
        }
        $action = $item['action'];
        if ($action) {
            $name = $prefix . preg_replace('/[^A-Za-z0-9]/', '', ucwords($item['code'], '-'));
            $actions = match ($action['kind']) {
                'settings' => [$this->link($name, $action['label'], $this->settingsUrl($action['target'] ?? 'settings'))],
                'help' => [$this->link($name, $action['label'], static::getUrl(['tab' => 'help']) . '#' . ($action['target'] ?? 'help'))],
                'test-storage' => $this->schedulerFresh() ? [$this->testStorageAction()->button()->size(Size::Small)] : [],
                'recovery-key' => $this->recoveryKeyActions(),
                default => [],
            };
            if ($actions) { $children[] = Actions::make($actions); }
        }
        return Flex::make([
            Icon::make(match ($tone) { 'danger' => 'ri-alert-line', 'info' => 'ri-information-line', default => 'ri-error-warning-line' })
                ->color(match ($tone) { 'danger' => 'danger', 'info' => 'info', default => 'warning' })->grow(false),
            Group::make($children)->dense()->grow(),
        ])->verticallyAlignStart();
    }

    // ---------------------------------------------------------------- Page content

    public function content(Schema $schema): Schema
    {
        return $schema->components([
            Callout::make('Backups can\'t start')
                ->danger()
                ->description(fn () => Messages::error($this->overviewError) . ' To see details, run these as the site user:')
                ->footer([Group::make([$this->command(fn () => $this->terminalFunction()), $this->command('paymenter_backups overview --offline')])])
                ->visible(fn () => $this->overview() && $this->overviewError !== null),
            Tabs::make('Backups')
                ->contained(false)
                ->persistTabInQueryString('tab')
                ->tabs([
                    Tab::make('Backups')->id('backups')->schema([
                        $this->setupSection(),
                        ...$this->alertCallouts(),
                        $this->overviewSection(),
                        EmbeddedTable::make(),
                    ]),
                    Tab::make('Settings')->id('settings')->schema([
                        EmbeddedSchema::make('settingsForm'),
                    ]),
                    Tab::make('Help')->id('help')->schema($this->helpSections()),
                ]),
        ]);
    }

    private function setupSection(): Section
    {
        return Section::make('Setup')->id('setup')
            ->schema(function () {
                $groups = $this->setupGroups();
                $blocks = [];
                if ($groups['all']) {
                    $blocks[] = Fieldset::make('All backups')->columns(1)
                        ->schema(array_map(fn ($item) => $this->setupItem($item, 'setupAll'), $groups['all']));
                }
                foreach (self::KIND_LABELS as $mode => $label) {
                    [$icon, $color, $line] = $this->typeStatus($mode);
                    $blocks[] = Fieldset::make($label)->columns(1)->schema([
                        Flex::make([Icon::make($icon)->color($color)->grow(false), Text::make($line)->grow()])->verticallyAlignStart(),
                        ...array_map(fn ($item) => $this->setupItem($item, 'setup' . ucfirst(self::KINDS[$mode])), $groups[$mode]),
                    ]);
                }
                return $blocks;
            })
            ->visible(fn () => $this->setupOpen());
    }

    /** Transient conditions, one callout each. */
    private function alertCallouts(): array
    {
        $pending = fn () => (int) ($this->overview()['s3_pending']['count'] ?? 0);
        return [
            Callout::make('Backups in S3 aren\'t encrypted')->info()
                ->description('Save settings to turn on encryption for new uploads. Backups already in S3 stay as they are.')
                ->actions([$this->link('alertEncryption', 'Go to Storage settings', $this->settingsUrl('storage'))->button()])
                ->visible(fn () => $this->overviewError === null && $this->settingsVersion() < 2 && $this->uploadsToS3()
                    && !($this->encryption()['enabled'] ?? true)),
            Callout::make(fn () => $pending() . ($pending() === 1 ? ' backup isn\'t' : ' backups aren\'t') . ' uploaded to S3 yet')->warning()
                ->description('They\'re kept on this server. Use Upload to S3 in each row\'s menu.')
                ->visible(fn () => $pending() > 0),
            Callout::make('Backup space is full')->danger()
                ->description(fn () => 'Backups on this server reached the ' . Messages::bytes($this->configuration()['space']['budget'] ?? null) . ' limit. New backups stop until you delete old ones or raise the limit.')
                ->actions([$this->link('alertSpace', 'Go to Storage settings', $this->settingsUrl('storage'))->button()])
                ->visible(fn () => ($this->configuration()['space']['state'] ?? null) === 'full'),
            Callout::make('The site is in maintenance mode')->info()
                ->description('Queued tasks wait until it\'s back online.')
                ->visible(fn () => (bool) ($this->overview()['maintenance'] ?? false)),
            Callout::make(fn () => 'Cleanup skipped ' . $this->skippedCount() . ($this->skippedCount() === 1 ? ' backup' : ' backups'))->warning()
                ->description('They weren\'t deleted. Their rows say why.')
                ->visible(fn () => $this->skippedCount() > 0),
        ];
    }

    private function skippedCount(): int { return (int) ($this->configuration()['retention']['last_apply']['skipped_count'] ?? 0); }

    /** Schedule summary for a mode from the overview, or the legacy single schedule. */
    private function scheduleFor(string $mode): array
    {
        $schedules = $this->configuration()['schedules'] ?? null;
        if (is_array($schedules) && is_array($schedules[self::KINDS[$mode]] ?? null)) { return $schedules[self::KINDS[$mode]]; }
        $legacy = $this->configuration()['schedule'] ?? [];
        $selected = ($this->configuration()['mode'] ?? 'full-quiesced') === $mode && ($legacy['enabled'] ?? false);
        return ['frequency' => $selected ? ($legacy['frequency'] ?? 'daily') : 'off', 'next_at' => $selected ? ($legacy['next_at'] ?? null) : null];
    }

    /** Newest verified backup of a mode: the engine's pointer, or the newest verified row. */
    private function lastGood(string $mode): ?array
    {
        $pointer = $this->overview()['last_good'][self::KINDS[$mode]] ?? null;
        if ($pointer !== null) { return $this->run($pointer); }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['mode'] === $mode && $run['status'] === 'ready' && ($run['download'] === 'ready' || in_array($run['remote'], ['delivered', 'present'], true))) { return $run; }
        }
        return null;
    }

    private function storageTestState(): string
    {
        $test = $this->latestRequest('destination-test');
        if (!$test) { return 'none'; }
        if ($test['status'] === 'complete') {
            // The engine says whether the test still proves the current bucket, format and recovery key.
            if (array_key_exists('result_verified', $test)) { return $test['result_verified'] === true ? 'passed' : 'stale'; }
            $current = $this->configuration()['destination']['id'] ?? null;
            return $current !== null && ($test['result_destination_id'] ?? null) === $current ? 'passed' : 'stale';
        }
        return in_array($test['status'], self::ACTIVE, true) ? 'queued' : $test['status'];
    }

    private function overviewSection(): Section
    {
        $next = function (string $mode) {
            $schedule = $this->scheduleFor($mode);
            if (($schedule['frequency'] ?? 'off') === 'off') { return 'Off'; }
            return ($schedule['next_at'] ?? null) ? $this->time($schedule['next_at'], true) : 'None in the next 8 days';
        };
        return Section::make('Overview')
            ->headerActions([$this->testStorageAction(), $this->applyRetentionAction()])
            ->schema([
                Grid::make(['default' => 1, 'md' => 2])->schema([
                    TextEntry::make('overview_last_full')->label('Last full backup')
                        ->state(fn () => ($run = $this->lastGood('full-quiesced')) ? $this->when($run['created_at']) : 'None yet'),
                    TextEntry::make('overview_last_database')->label('Last database backup')
                        ->state(fn () => ($run = $this->lastGood('database-online')) ? $this->when($run['created_at']) : 'None yet'),
                    TextEntry::make('overview_next_full')->label('Next full backup')->state(fn () => $next('full-quiesced')),
                    TextEntry::make('overview_next_database')->label('Next database backup')->state(fn () => $next('database-online')),
                    TextEntry::make('overview_last_s3')->label('Last upload to S3')->state(function () {
                        $run = $this->run($this->overview()['last_good']['s3'] ?? null);
                        if (!$run) { return 'None yet'; }
                        return $this->time($run['created_at'], true) . ($run['remote_checked_at'] ? ', checked ' . $this->ago($run['remote_checked_at']) : ', not checked yet');
                    })->visible(fn () => $this->uploadsToS3()),
                    TextEntry::make('overview_storage')->label('Stored on')->state(function () {
                        $storage = $this->storage();
                        if (!($this->configuration()['valid'] ?? false)) { return 'Unknown until settings are valid'; }
                        $places = [];
                        if ($storage['keep_local'] ?? true) { $places[] = 'This server'; }
                        if ($storage['upload_s3'] ?? false) { $places[] = 'S3 (' . ($storage['s3_bucket'] ?: 'no bucket') . ')' . ($this->savedS3Keys() ? '' : ', keys missing'); }
                        return implode(', ', $places) ?: 'This server';
                    }),
                    TextEntry::make('overview_encryption')->label('S3 encryption')->state(function () {
                        if (!($this->encryption()['enabled'] ?? false)) { return 'Off'; }
                        $key = $this->keyInfo();
                        return match ($this->keyState()) {
                            'none' => 'On, no recovery key yet',
                            'pending' => 'On, recovery key ' . ($this->pendingKeyShort() ?? '') . ' not confirmed',
                            default => 'On, recovery key ' . $key['short'] . ($this->pendingKeyShort() !== null ? '; new key ' . $this->pendingKeyShort() . ' not confirmed' : ''),
                        };
                    })->visible(fn () => $this->uploadsToS3()),
                    TextEntry::make('overview_space')->label('Space used')->state(function () {
                        $space = $this->configuration()['space'] ?? null;
                        if (!$space || $space['used'] === null) { return 'Nothing stored yet'; }
                        $count = count(array_filter($this->overview()['runs'], static fn ($run) => $run['local']));
                        $used = Messages::bytes($space['used']) . ' of ' . Messages::bytes($space['budget']);
                        if (($space['state'] ?? null) === 'full') { return $used . '. Full.'; }
                        return $used . ($count === 0 ? ' (no backups stored here)' : ', ' . $count . ' ' . ($count === 1 ? 'backup' : 'backups'));
                    }),
                    TextEntry::make('overview_scheduler')->label('Cron job')->state(function () {
                        $last = $this->lastTick();
                        if (!$last) { return 'Never ran'; }
                        return $this->schedulerFresh() ? 'Ran ' . $this->ago($last) : 'Not running (last run ' . $this->ago($last) . ')';
                    })->color(fn () => $this->schedulerFresh() ? null : 'warning'),
                    TextEntry::make('overview_test')->label('Storage test')->state(function () {
                        $test = $this->latestRequest('destination-test');
                        return match ($this->storageTestState()) {
                            'none' => 'Not tested',
                            'passed' => 'Passed ' . $this->ago($test['finished_at']),
                            'stale' => 'Settings changed since the last test',
                            'failed' => 'Failed ' . $this->ago($test['finished_at']) . '. ' . Messages::error($test['error']),
                            'cancelled' => 'Last test cancelled',
                            'expired' => 'Last test expired before it ran',
                            default => 'Queued',
                        };
                    })->color(fn () => $this->storageTestState() === 'failed' ? 'danger' : null),
                ]),
            ]);
    }

    private function helpSections(): array
    {
        $bullets = fn (array $lines) => $this->bullets($lines);
        return [
            Section::make('What\'s in a backup')->id('backup-contents')->collapsible()->collapsed()->schema([
                $bullets([
                    'Full backup: the database plus every file in Paymenter\'s folder, including .env, APP_KEY, uploads and extensions. While the database and files are copied, the site shows its maintenance page (HTTP 503), up to the backup time limit. Compression and verification run after the site is back online.',
                    'Database backup: the database only. The site stays online. To restore, you also need Paymenter\'s files, from a full backup or your own copy.',
                    'Backups contain customer data and secrets (.env, APP_KEY). Keep downloads private. Saved S3 keys, site passwords and recovery keys are never included.',
                ]),
            ]),
            Section::make('How backups run')->id('how-backups-run')->collapsible()->schema([
                Text::make('Paymenter\'s cron job runs schedule:run every minute. Each minute, Backups starts what is queued: scheduled backups, Create backup, uploads, verification and deletion. If the cron job isn\'t running, nothing happens. Backups never edits your crontab or systemd units.'),
                Text::make(fn () => 'Add this line to the crontab of ' . ($this->checks()['jobs']['crontab_user'] ?? 'the user that owns Paymenter') . ' with crontab -e. If a schedule:run line is already there, replace it:'),
                $this->command(fn () => $this->schedulerLine()),
                Text::make('flock lets a full backup pause this job while it copies. Database backups work without it.'),
                Text::make('Copying needs HTTPS. Over HTTP, select the text to copy it.')->visible(fn () => !request()->isSecure()),
            ]),
            Section::make('Full backups: what has to be true')->id('full-backups')->collapsible()->schema([
                Text::make('During a full backup, Backups waits for running Paymenter cron jobs to finish and holds their lock files so new ones wait. It then puts the site into maintenance mode, waits for open page loads, checks that App URL shows the maintenance page, copies the database and files, and takes the site out of maintenance mode.'),
                Text::make('Maintenance mode stops new page loads, but a cron job that already started keeps writing. If it writes while files are copied, the database and files in the backup won\'t match. Holding the same lock file as the job prevents that.'),
                $bullets([
                    'Every Paymenter cron job uses a lock file. For schedule:run, use the line above.',
                    'No always-on queue worker. A worker started by systemd or Supervisor never stops, so it can\'t be paused. Run queue jobs from cron with their own lock, then stop the service:',
                ]),
                $this->command(fn () => $this->queueLine()),
                $bullets([
                    'No Redis or other outside storage for cache, sessions, queue or files. Data held there isn\'t in the backup. The Backups tab names the .env keys to change.',
                    'File-based maintenance mode: APP_MAINTENANCE_DRIVER=file, Paymenter\'s default.',
                    'Nothing else writes to Paymenter\'s database or files, such as another app, a sync script or manual SQL.',
                    'If your web server asks for a username and password before Paymenter loads, enter it under Settings, Site password.',
                ]),
            ]),
            Section::make('Encrypted backups in S3')->id('encryption')->collapsible()->collapsed()->schema([
                Text::make('Backups uploaded to S3 are encrypted on this server with GnuPG (AES-256) before upload. Copies on this server and downloads from this page aren\'t encrypted.'),
                Text::make('The recovery key decrypts them. Keep it in a password manager or another safe place outside this server. If this server is lost, APP_KEY and your S3 keys can\'t replace it.'),
                Text::make('A new recovery key applies to new uploads only. Earlier backups keep needing the key they were encrypted with, so keep every key you\'ve saved.'),
            ]),
            Section::make('Schedules and missed backups')->id('schedules')->collapsible()->collapsed()->schema([
                $bullets([
                    'A new or changed schedule starts at its next time. It doesn\'t run immediately. Changing one schedule doesn\'t affect the other.',
                    'If the cron job was down and a backup was missed in the last 8 days, one backup of that type runs once it\'s back. Missed backups don\'t pile up.',
                    'When a full and a database backup are due together, the full backup runs first. If it succeeds, it also counts as the database backup.',
                    'Full backups never start close to Paymenter\'s daily billing run.',
                    'With a time zone other than UTC, a time skipped by a daylight-saving change doesn\'t run that day, and a repeated time runs once.',
                ]),
            ]),
            Section::make('Cleanup')->id('cleanup')->collapsible()->collapsed()->schema([
                $bullets([
                    'After each backup, Backups deletes the oldest backups beyond the number to keep, per type, on this server and in S3.',
                    'The newest verified backup in each place is always kept. So are backups not yet uploaded to S3, backups that failed verification, backups in an old S3 folder, and copies downloaded from S3 in the last 24 hours.',
                ]),
            ]),
            Section::make('Restoring')->id('restoring')->collapsible()->collapsed()->schema([
                Text::make('Restore into a new folder and a new database, never over the running site.'),
                Group::make([
                    Text::make('1. Get the backup file: Download it here, or on the server run paymenter_backups export BACKUP-ID --output /new/folder. If this server is lost, download the backup\'s files from your bucket and decrypt them with your recovery key, as the full guide shows.'),
                    Text::make('2. Check the file against its manifest: the SHA-256 and size must match.'),
                    Text::make('3. Extract it into a private folder. It holds database.sql and, for a full backup, app.tar. Extract app.tar into a new application folder.'),
                    Text::make('4. Create an empty database and import database.sql.'),
                    Text::make('5. Keep APP_KEY and the Passport keys. Change only the database connection and APP_URL in .env, and Paymenter\'s app_url setting. Before the restored site\'s cron job runs, turn off the copied Backups schedules and give it a new data directory.'),
                    Text::make('6. Test the restored site, then switch the web server over.'),
                ]),
                Text::make('Full guide on this server:'),
                $this->command(dirname(__DIR__, 2) . '/docs/RECOVERY.md'),
                Text::make(fn () => 'Installation ID: ' . ($this->overview()['installation_uuid'] ?? 'not created yet') . '.'),
            ]),
            Section::make('Command line')->id('command-line')->collapsible()->collapsed()
                ->description('Run these as the user that owns Paymenter. They work even when Paymenter won\'t start.')
                ->schema([
                    Text::make('Define this once per shell:'),
                    $this->command(fn () => $this->terminalFunction()),
                    Text::make('See backups and queued tasks:'),
                    $this->command('paymenter_backups overview --offline'),
                    Text::make('After an interrupted full backup, see what needs fixing (changes nothing):'),
                    $this->command('paymenter_backups recover'),
                    Text::make('Take the site out of the backup\'s maintenance mode, after reading the output above:'),
                    $this->command('paymenter_backups recover --execute'),
                    Text::make('If you fixed the site by hand, let new backups start:'),
                    $this->command('paymenter_backups recover acknowledge --run BACKUP-ID'),
                    Text::make('Copy a verified backup to a new folder. The backup ID is in each backup\'s Details:'),
                    $this->command('paymenter_backups export BACKUP-ID --output /new/folder'),
                ]),
        ];
    }

    // ---------------------------------------------------------------- Settings form

    public function settingsForm(Schema $schema): Schema
    {
        // Private properties are rebuilt on each Livewire request. Read the
        // current native rows before deciding which repair fields validate.
        $this->formValues();
        $positive = static fn (string $key) => in_array($key, Settings::POSITIVE, true) ? 1 : 0;
        $integer = fn (string $key, string $label, ?string $suffix = null, string|\Closure|null $help = null) => TextInput::make($key)->label($label)
            ->integer()->required()->minValue($positive($key))->suffix($suffix)->helperText($help);
        $gib = fn (string $key, string $label, string $help, bool $positive) => TextInput::make($key)->label($label)->suffix('GiB')->helperText($help)
            ->numeric()->required()->inputMode('decimal')->step(0.01)
            ->rules(['numeric', $positive ? 'gt:0' : 'min:0', 'max:1000000', 'decimal:0,2'])
            ->validationMessages(['gt' => 'Enter more than 0.', 'decimal' => 'Use at most two decimals.']);
        $path = static fn (string $key, string $label, string|\Closure|null $help = null, string|\Closure|null $placeholder = null) => TextInput::make($key)->label($label)
            ->helperText($help)->placeholder($placeholder)->rules(['nullable', 'string', 'regex:/^\/[^\x00]*$/'])
            ->validationMessages(['regex' => 'Enter a full path starting with /.']);
        // Plain Laravel rules: Filament evaluates closures in rules() as its own utilities.
        $lineList = static fn (string $key, string $label, string $help) => Textarea::make($key)->label($label)->helperText($help)->rows(3)
            ->rules(['nullable', 'string', 'regex:' . Settings::PATH_LINES])
            ->validationMessages(['regex' => 'Put one full path on each line, starting with /.']);
        $time = static fn (string $key) => TextInput::make($key)->label('At')->type('time')->required()->rules(['regex:' . Settings::TIME])
            ->validationMessages(['regex' => 'Enter a time as HH:MM.']);
        $days = static fn (string $key, string $schedule) => CheckboxList::make($key)->label('On these days')->options(array_map('strval', Messages::WEEKDAYS))->columns(['default' => 2, 'lg' => 4])
            ->required(fn (Get $get) => $get($schedule) === 'weekly')
            ->rules(['array'])->nestedRecursiveRules(['in:0,1,2,3,4,5,6'])
            ->validationMessages(['required' => 'Choose at least one day.'])
            ->visibleJs("\$get('{$schedule}') === 'weekly'");
        $keep = fn (string $key) => $integer($key, 'Keep on this server', 'backups', 'The newest verified backup is always kept.');
        $s3Policy = fn (string $type, string $label) => [
            Select::make('s3_retention_policy_' . $type)->label($label)->options(Settings::SELECTS['s3_retention_policy_' . $type])
                ->required()->selectablePlaceholder(false),
            $integer('s3_retention_keep_' . $type, 'Backups to keep in S3', 'backups')
                ->visibleJs("\$get('s3_retention_policy_{$type}') === 'count'"),
            $integer('s3_retention_days_' . $type, 'Days to keep in S3', 'days', 'The newest backup in S3 is always kept.')
                ->visibleJs("\$get('s3_retention_policy_{$type}') === 'age'"),
        ];
        $tool = fn (string $key, string $label) => $path($key, $label, null, fn () => $this->detectedTool($key));
        $zones = array_combine(\DateTimeZone::listIdentifiers(), \DateTimeZone::listIdentifiers());

        return $schema->components([
            Form::make([
                Section::make('Public web folders need correction')->visible(fn () => in_array('extra_docroots', $this->malformedKeys, true))
                    ->description('The saved list of public web folders is invalid. Enter the complete list and confirm it before saving. Save the folder list by itself first.')
                    ->schema([
                        $lineList('extra_docroots', 'Other public web folders', 'One full path per line. Leave empty only if this account has no other public web folders.'),
                        Checkbox::make('confirm_public_directories')->label('I have listed all public web folders for this account.')
                            ->accepted()->validationMessages(['accepted' => 'Confirm the complete list before saving.']),
                    ]),
                Section::make('First-time setup')->id('first-time-setup')
                    ->description('Choose where Backups keeps its private data. You can\'t change this after the first save.')
                    ->visible(fn () => !$this->initialized())
                    ->schema([
                        $path('private_root', 'Data directory', 'Holds backup history, the installation ID and encrypted keys. Must be outside Paymenter and any public web folder.', dirname(base_path()) . '/paymenter-backups-private')
                            ->visible(fn () => !$this->initialized()),
                        $lineList('extra_docroots', 'Other public web folders', 'Document roots of other sites on this account, one per line. Backups are never stored inside them.')
                            ->visible(fn () => !$this->initialized() && !in_array('extra_docroots', $this->malformedKeys, true)),
                    ]),

                Section::make('Schedule')->id('schedule')->description('Backups run from Paymenter\'s cron job at these times.')->schema([
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        Fieldset::make('Full backups')->columns(1)->schema([
                            Text::make('Database and all files. The site shows its maintenance page while the copy runs.'),
                            Select::make('full_schedule')->label('Frequency')->options(Settings::SELECTS['full_schedule'])->required()->selectablePlaceholder(false),
                            $days('full_weekdays', 'full_schedule'),
                            $time('full_time')->helperText(fn () => $this->billingSentence())->visibleJs("\$get('full_schedule') !== 'off'"),
                            $keep('retention_keep_full'),
                            Flex::make([
                                Text::make('Full backups aren\'t ready yet.')->color('warning'),
                                Actions::make([$this->link('scheduleFullSetup', 'See what\'s needed', static::getUrl(['tab' => 'backups']) . '#setup')]),
                            ])->verticallyAlignCenter()->visible(fn () => $this->initialized() && $this->overviewError === null && $this->blockers('full-quiesced') !== []),
                        ]),
                        Fieldset::make('Database backups')->columns(1)->schema([
                            Text::make('Database only. The site stays online. To restore, you also need a copy of Paymenter\'s files.'),
                            Select::make('database_schedule')->label('Frequency')->options(Settings::SELECTS['database_schedule'])->required()->selectablePlaceholder(false),
                            Grid::make(2)->schema([
                                Select::make('database_every_hours')->label('Interval')->options(Settings::SELECTS['database_every_hours'])->required()->selectablePlaceholder(false)
                                    ->rules(['in:' . implode(',', Settings::EVERY_HOURS)]),
                                TextInput::make('database_minute')->label('At minute')->integer()->required()->minValue(0)->maxValue(59),
                            ])->visibleJs("\$get('database_schedule') === 'hourly'"),
                            // Computed in the browser, so changing these fields needs no round trip.
                            Text::make(self::HOURLY_SENTENCE_JS)->js()->visibleJs("\$get('database_schedule') === 'hourly'"),
                            $days('database_weekdays', 'database_schedule'),
                            $time('database_time')->visibleJs("['daily', 'weekly'].includes(\$get('database_schedule'))"),
                            $keep('retention_keep_database'),
                        ]),
                    ]),
                    Select::make('schedule_timezone')->label('Time zone')->options($zones)->searchable()->required()->rule('timezone')
                        ->helperText('Use UTC to avoid daylight-saving gaps.'),
                ]),

                Section::make('Storage')->id('storage')->schema([
                    $path('local_path', 'Backup directory', 'In a private folder outside Paymenter. Leave empty to use the default. Changing it affects new backups only.',
                        fn () => $this->storage()['default_local_path'] ?? (($root = $this->safePrivateRoot()) ? $root . '/backups' : null)),
                    Text::make(fn () => $this->legacyDirectorySentence())->visible(fn () => $this->legacyDirectorySentence() !== null),
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        $gib('local_byte_budget', 'Space limit for backups', 'New backups stop when backups on this server reach this size.', true),
                        $gib('minimum_free_bytes', 'Minimum free disk space', 'Backups stop if the disk would drop below this.', false),
                    ]),
                    Checkbox::make('keep_local')->label('Keep backups on this server')
                        ->helperText('When off, each backup is written here first and deleted after its upload to S3 is verified.')
                        ->accepted(fn (Get $get) => !$get('upload_s3'))
                        ->validationMessages(['accepted' => 'Keep backups on this server, upload them to S3, or both.']),
                    Checkbox::make('upload_s3')->label('Upload to S3')->helperText('AWS S3 or any S3-compatible storage.'),
                    Group::make([
                        Fieldset::make('S3 bucket')->id('s3')->columns(['default' => 1, 'md' => 2])->schema([
                            TextInput::make('s3_endpoint')->label('Endpoint')->placeholder('https://s3.example.com')
                                ->helperText('Leave empty for AWS. For other providers, enter their S3 API URL, for example https://s3.example.com.')
                                ->rules(['nullable', 'regex:/^https:\/\/[^\s\/?#@]+(?:\/[^\s?#]*)?$/D'])->validationMessages(['regex' => 'Enter the provider\'s https:// API URL, without a username or query.']),
                            TextInput::make('s3_region')->label('Region')->placeholder('us-east-1'),
                            TextInput::make('s3_bucket')->label('Bucket')->helperText('Bucket name, for example my-backups. It must already exist.')
                                ->required(fn (Get $get) => (bool) $get('upload_s3'))->validationMessages(['required' => 'Enter the bucket name.']),
                            TextInput::make('s3_prefix')->label('Folder in bucket')->required()
                                ->helperText(fn () => 'Backups go to FOLDER/' . ($this->overview()['installation_uuid'] ?? 'INSTALLATION-ID') . '/. Changing it doesn\'t move existing backups.')
                                ->rules(['string', 'regex:' . Settings::S3_PREFIX])
                                ->validationMessages(['regex' => 'The folder can\'t contain "//", ".", ".." or "\\".']),
                            Toggle::make('s3_path_style')->label('Path-style addressing')->helperText('Turn on for MinIO and some other S3-compatible providers.')->inline(false),
                        ]),
                        Fieldset::make('S3 access keys')->columns(1)->schema([
                            EmbeddedSchema::make('s3KeysForm'),
                        ]),
                        Fieldset::make('Keep in S3')->columns(['default' => 1, 'md' => 2])->schema([
                            Grid::make(1)->schema($s3Policy('full', 'Full backups')),
                            Grid::make(1)->schema($s3Policy('database', 'Database backups')),
                        ]),
                        Fieldset::make('Encryption')->id('encryption')->columns(1)->schema([
                            Toggle::make('s3_encryption')->label('Encrypt backups in S3')
                                ->helperText(fn () => $this->encryptionHelp()),
                            EmbeddedSchema::make('encryptionForm')
                                ->visible(fn () => $this->keyState() === 'none')->visibleJs("\$get('s3_encryption')"),
                            Group::make([
                                Text::make(fn () => $this->recoveryKeySentence()),
                                Actions::make(fn () => [...$this->recoveryKeyActions(), $this->rotateRecoveryKeyAction(), $this->discardRecoveryKeyAction()]),
                            ])->dense()->visible(fn () => $this->keyState() !== 'none')->visibleJs("\$get('s3_encryption')"),
                            Checkbox::make('plaintext_offsite_acknowledged')->label('I understand backups in S3 won\'t be encrypted')
                                ->helperText('They contain customer data, .env and APP_KEY. Give these keys access to this bucket only, and turn on server-side encryption in the bucket if you need it.')
                                ->accepted(fn (Get $get) => (bool) $get('upload_s3') && !$get('s3_encryption'))
                                ->validationMessages(['accepted' => 'Confirm this, or turn encryption on.'])
                                ->visibleJs("! \$get('s3_encryption')"),
                        ]),
                    ])->visibleJs("\$get('upload_s3')"),
                    Toggle::make('automatic_retention')->label('Delete old backups automatically')
                        ->helperText('After each backup, delete backups beyond the numbers to keep.'),
                ]),

                Section::make('Full backup requirements')->id('full-backup-requirements')
                    ->description('A full backup pauses Paymenter\'s cron jobs and shows the maintenance page, so the database and files are copied in one consistent state.')
                    ->collapsible()->collapsed(fn () => ($this->formValues()['full_schedule'] ?? 'off') === 'off' && $this->blockers('full-quiesced') === [])
                    ->schema(fn () => [
                        ...$this->detectedChecks(),
                        Checkbox::make('full_host_assumptions_acknowledged')->label('No other program writes to Paymenter\'s database or files.')
                            ->helperText('Paymenter\'s cron jobs are paused during a full backup. Other programs aren\'t, and their writes would make the backup inconsistent.'),
                        $this->sitePasswordSection(),
                    ]),

                Section::make('Before backups run')->id('before-backups-run')->schema([
                    Checkbox::make('no_concurrent_ddl_acknowledged')->label('I won\'t update Paymenter, install extensions or run migrations while a backup runs.')
                        ->helperText('A schema change during a backup can make the database copy unusable. This applies to both backup types.'),
                ]),

                Section::make('Advanced')->id('advanced')->description('Binary paths, time limits and folders. Most sites don\'t need to change these.')
                    ->collapsible()->collapsed()->schema([
                        Fieldset::make('Binary paths')->columns(['default' => 1, 'md' => 2])->schema([
                            Text::make('An empty path uses the binary Backups detects.')->columnSpanFull(),
                            $tool('python', 'Python 3'), $tool('php', 'PHP'), $tool('dump', 'mysqldump or mariadb-dump'),
                            $tool('mysql', 'mysql or mariadb client'), $tool('tar', 'tar'), $tool('gzip', 'gzip'), $tool('gpg', 'GnuPG (gpg)'),
                        ]),
                        Fieldset::make('Time limits')->columns(['default' => 1, 'md' => 2])->schema([
                            $integer('capture_timeout', 'Backup time limit', 'seconds', 'Longest the database export and file copy may take. For full backups, the longest the site stays in maintenance mode.'),
                            $integer('drain_seconds', 'Wait for open requests', 'seconds', 'After maintenance mode starts, wait this long for page loads already in progress to finish.'),
                            $integer('lock_timeout', 'Wait for running cron jobs', 'seconds', 'If a Paymenter cron job is running, wait this long before trying again next minute.'),
                            $integer('billing_margin_seconds', 'Billing safety margin', 'seconds', 'Extra time kept clear before and after Paymenter\'s daily billing run.'),
                            $integer('request_max_age', 'Queued task expiry', 'seconds', 'Tasks that haven\'t started after this long are dropped.'),
                            $integer('s3_timeout', 'S3 timeout', 'seconds', 'Per request.'),
                            $integer('s3_retries', 'S3 retries', 'attempts', 'Total attempts per request, including the first.'),
                        ]),
                        Fieldset::make('Lock files')->columns(1)->schema([
                            $lineList('job_lock_paths', 'Lock files (override)', 'Leave empty to use the lock files your cron jobs use. One full path per line.'),
                        ]),
                        Fieldset::make('Folders')->columns(['default' => 1, 'md' => 2])->visible(fn () => $this->initialized())->schema([
                            $path('private_root', 'Data directory', 'Holds backup history, the installation ID and encrypted keys. Can\'t be changed after setup.')
                                ->disabled()->visible(fn () => $this->initialized()),
                            $lineList('extra_docroots', 'Other public web folders', 'Document roots of other sites on this account, one per line. Backups are never stored inside them.')
                                ->visible(fn () => $this->initialized() && !in_array('extra_docroots', $this->malformedKeys, true)),
                        ]),
                    ]),
            ])->livewireSubmitHandler('saveSettings')->footer([
                Actions::make([Action::make('saveSettings')->label(fn () => $this->initialized() ? 'Save settings' : 'Save and finish setup')->submit('saveSettings')->keyBindings(['mod+s'])]),
            ]),
        ])->statePath('settings');
    }

    private function detectedTool(string $tool): ?string
    {
        $checked = $this->checks()['tools'][$tool] ?? null;
        if (!is_array($checked)) { return null; }
        return is_string($checked['resolved'] ?? null) && $checked['resolved'] !== '' ? $checked['resolved'] : 'Not found';
    }

    private function legacyDirectorySentence(): ?string
    {
        $legacy = $this->storage()['legacy_default_path'] ?? null;
        if (!is_string($legacy) || $legacy === '') { return null; }
        $stored = (string) ($this->formValues()['local_path'] ?? '');
        if ($stored === $legacy) { return 'This is the old default folder. Clear the field to use the new default for new backups.'; }
        return $stored === '' ? 'Earlier backups stay in ' . $legacy . '.' : null;
    }

    private function encryptionHelp(): string
    {
        if ($this->settingsVersion() < 2 && $this->uploadsToS3() && !($this->encryption()['enabled'] ?? true)) {
            return 'Backups in S3 aren\'t encrypted yet. After you save, uploads wait until you save the recovery key.';
        }
        return 'Backups are encrypted with a recovery key before upload. Copies on this server and downloads aren\'t encrypted.';
    }

    private function recoveryKeySentence(): string
    {
        $key = $this->keyInfo();
        $pending = $this->pendingKeyShort();
        if ($this->keyState() === 'active' && $pending === null) {
            return 'Recovery key ' . $key['short'] . ($key['created_at'] ?? null ? ', created ' . $this->time($key['created_at']) : '') . '.';
        }
        if ($this->keyState() === 'active') {
            return 'New recovery key ' . $pending . ' isn\'t confirmed yet. Uploads use key ' . $key['short'] . ' until you save the new key and confirm.';
        }
        return 'Recovery key ' . ($pending ?? '') . ' isn\'t confirmed yet. Uploads to S3 start after you save it outside this server and confirm.';
    }

    /** Detected jobs, stores and maintenance driver, each with its fix. */
    private function detectedChecks(): array
    {
        $jobs = $this->checks()['jobs'] ?? null;
        $rows = [];
        $row = function (string $tone, string $title, array $lines = []) {
            $icon = match ($tone) { 'success' => 'ri-checkbox-circle-line', 'gray' => 'ri-question-line', default => 'ri-error-warning-line' };
            return Flex::make([Icon::make($icon)->color($tone)->grow(false), Group::make([Text::make($title), ...$lines])->grow()])->verticallyAlignStart();
        };
        if (!is_array($jobs)) {
            $rows[] = $row('gray', 'Cron jobs haven\'t been checked yet. The check runs from Paymenter\'s cron job.');
        } else {
            $user = $jobs['crontab_user'] ?? 'the site user';
            $state = $jobs['state'] ?? 'unknown';
            if ($state === 'failed') {
                $rows[] = $row('warning', 'Couldn\'t read the crontab or systemd units for ' . $user . '.');
            } elseif ($state === 'stale') {
                $rows[] = $row('gray', 'Cron jobs were last checked ' . $this->ago($jobs['checked_at'] ?? null) . '. The check refreshes while Paymenter\'s cron job runs.');
            } elseif ($state !== 'ok') {
                $rows[] = $row('gray', 'Cron jobs haven\'t been checked yet. The check runs from Paymenter\'s cron job.');
            }
            $hasScheduler = false;
            foreach ($jobs['items'] ?? [] as $job) {
                if (!is_array($job)) { continue; }
                $kind = ($job['kind'] ?? null) === 'queue-worker' ? 'Queue worker' : 'Cron job';
                $hasScheduler = $hasScheduler || ($job['kind'] ?? null) === 'scheduler';
                $source = ($job['source'] ?? null) === 'systemd' ? ' (systemd ' . ($job['label'] ?? 'unit') . ')' : '';
                $command = filled($job['command'] ?? null) ? [Text::make((string) $job['command'])->fontFamily(FontFamily::Mono)->extraAttributes(['style' => 'overflow-wrap: anywhere'])] : [];
                $fix = $kind === 'Queue worker' ? $this->queueLine() : $this->schedulerLine();
                $systemd = ($job['source'] ?? null) === 'systemd' && filled($job['label'] ?? null);
                $rows[] = match ($job['coordination'] ?? null) {
                    'coordinated' => $row('success', $kind . $source . ' uses lock file ' . ($job['lock'] ?? '') . '.', $command),
                    'continuous' => $row('warning', $kind . $source . ' runs all the time, so full backups can\'t pause it.', [...$command,
                        ...($systemd ? [Text::make('Stop and disable it:'), $this->command('systemctl disable --now ' . $job['label'])] : []),
                        Text::make('Run queue jobs from cron instead:'), $this->command($this->queueLine())]),
                    'unsupported' => $row('warning', $kind . $source . ': Backups can\'t read this line.', [...$command, Text::make('Replace it with:'), $this->command($fix)]),
                    default => $row('warning', $kind . $source . ' has no lock file.', [...$command, Text::make('Replace it with:'), $this->command($fix)]),
                };
            }
            if (!$hasScheduler && in_array($jobs['state'] ?? null, ['ok', 'stale'], true)) {
                $rows[] = $row('warning', 'No cron job runs Paymenter\'s scheduler. Add this line to the crontab of ' . $user . ':', [$this->command($this->schedulerLine())]);
            }
        }
        foreach ($this->checks()['stores'] ?? [] as $store) {
            if (!is_array($store)) { continue; }
            $name = Messages::STORES[$store['kind'] ?? ''] ?? 'Storage';
            $driver = (string) ($store['driver'] ?? 'unknown');
            $rows[] = match ($store['supported'] ?? null) {
                true => $row('success', $name . ': ' . $driver),
                false => $row('warning', $name . ': ' . $driver . '. Full backups need local storage. Change it in .env:',
                    filled($store['env'] ?? null) && filled($store['suggest'] ?? null) ? [$this->command($store['env'] . '=' . $store['suggest'])] : []),
                default => $row('gray', $name . ': ' . $driver . '. Backups can\'t tell whether this keeps data outside Paymenter.'),
            };
        }
        return $rows;
    }

    private function sitePasswordSection(): Section
    {
        $site = fn () => $this->configuration()['site_password'] ?? [];
        $saved = fn () => (bool) ($this->configuration()['saved_keys']['site'] ?? false);
        $attention = fn () => $saved() && in_array($site()['state'] ?? null, ['http', 'unavailable', 'origin-changed'], true);
        return Section::make('Site password (HTTP Basic)')->id('site-password')->compact()
            ->collapsible()->collapsed(fn () => !$saved() && !in_array('maintenance-probe-failed', $this->configuration()['admission']['full-quiesced'] ?? [], true))
            ->afterHeader([
                Text::make('Saved')->badge()->color('success')->visible(fn () => $saved() && !$attention()),
                Text::make('Needs attention')->badge()->color('warning')->visible($attention),
            ])
            ->description(fn () => 'Only if your web server asks for a username and password before Paymenter loads. A full backup opens '
                . ($site()['probe_base'] ?? 'App URL') . ' to check that visitors see the maintenance page, and needs this password to get past the prompt.')
            ->schema([
                Text::make(fn () => match ($site()['state'] ?? 'unavailable') {
                    'saved' => 'Saved for ' . ($site()['origin'] ?? 'App URL') . '. Only sent there.',
                    'http' => $saved() ? 'Your App URL now uses http://, so the saved password can\'t be sent. Remove it, or change App URL back to https://.'
                        : 'Your App URL uses http://. A site password can only be sent over https://.',
                    'unavailable' => 'Your App URL (Settings, General) isn\'t a valid web address.',
                    'origin-changed' => 'The saved password is for ' . ($site()['saved_origin'] ?? 'another address') . ', but App URL is now ' . ($site()['origin'] ?? 'different') . '. Enter it again or remove it.',
                    default => 'Not set.',
                }),
                EmbeddedSchema::make('siteAccessForm')->visible(fn () => $this->httpsOrigin() !== null),
                Actions::make([$this->removeSitePasswordAction()]),
            ]);
    }

    /** Form values from native rows. A malformed row falls back to its default. */
    private function formValues(): array
    {
        if ($this->storedForm !== null) { return $this->storedForm; }
        $rows = []; $this->storedRows = [];
        foreach ($this->record()->settings()->get() as $row) {
            $rows[$row->key] = $row->value;
            $this->storedRows[$row->key] = $row->getAttributes();
        }
        ksort($this->storedRows);
        $this->malformedKeys = [];
        $this->storedVersion = Settings::parse('settings_version', $rows['settings_version'] ?? null) ?? 1;
        $invalid = function (string $key) use ($rows): bool {
            if (!array_key_exists($key, $rows)) { return false; }
            $value = $rows[$key];
            if ($value === null && is_string(Settings::DEFAULTS[$key]) && !isset(Settings::SELECTS[$key]) && !in_array($key, Settings::TIME_KEYS, true)) { return false; }
            if (Settings::parse($key, $value) === null) { return true; }
            return \Illuminate\Support\Facades\Validator::make([$key => $value], [$key => Settings::rules($key)])->fails();
        };
        // A malformed public-directory guard is never defaulted.
        try { Settings::normalize(array_intersect_key($rows, ['extra_docroots' => true])); }
        catch (\Throwable $error) { $this->malformedKeys[] = 'extra_docroots'; }
        if ($this->storedVersion === 2) {
            $values = [];
            foreach (Settings::v2Keys() as $key) {
                if ($invalid($key)) { $this->malformedKeys[] = $key; }
                $values[$key] = in_array($key, $this->malformedKeys, true) ? Settings::DEFAULTS[$key] : (Settings::parse($key, $rows[$key] ?? null) ?? Settings::DEFAULTS[$key]);
            }
        } else {
            foreach (array_keys(Settings::DEFAULTS) as $key) {
                if ($key !== 'extra_docroots' && $invalid($key)) { $this->malformedKeys[] = $key; }
            }
            $values = Settings::fromLegacy(array_diff_key($rows, array_flip($this->malformedKeys)));
            // The page proposes encryption. Uploads keep their 0.3 behaviour until this form is saved.
            $values['s3_encryption'] = true;
            $values['plaintext_offsite_acknowledged'] = false;
        }
        $form = [];
        foreach ($values as $key => $value) { $form[$key] = $this->formValue($key, $value); }
        return $this->storedForm = $form;
    }

    /** Form shape of an engine value. */
    private function formValue(string $key, mixed $value): mixed
    {
        if (in_array($key, self::WEEKDAY_FIELDS, true)) { return array_map('strval', is_array($value) ? array_values(array_filter($value, 'is_int')) : []); }
        if (in_array($key, self::LINE_LISTS, true)) { return implode("\n", is_array($value) ? array_filter($value, 'is_string') : []); }
        if (in_array($key, self::GIB_FIELDS, true)) {
            $gib = round((int) $value / Settings::GIB, 2);
            return $gib == floor($gib) ? (int) $gib : $gib;
        }
        return $value;
    }

    /** Engine value for a form value. */
    private function engineValue(string $key, mixed $value): mixed
    {
        $default = Settings::DEFAULTS[$key];
        if (in_array($key, Settings::WEEKDAY_KEYS, true)) { $days = array_values(array_unique(array_map('intval', (array) $value))); sort($days); return $days; }
        if (in_array($key, self::LINE_LISTS, true)) { return array_values(array_filter(array_map('trim', preg_split('/\R/', (string) $value)), static fn ($line) => $line !== '')); }
        if (in_array($key, self::GIB_FIELDS, true)) { return (int) round((float) $value * Settings::GIB); }
        if (is_int($default)) { return (int) $value; }
        if (is_bool($default)) { return (bool) $value; }
        return trim((string) ($value ?? ''));
    }

    private function changed(string $key, mixed $value): bool
    {
        // Compare with native rows read on this request, never with anything the browser can send.
        $before = $this->formValues()[$key] ?? null;
        if (in_array($key, $this->malformedKeys, true)) { return true; }
        if (in_array($key, self::GIB_FIELDS, true)) { return round((float) $value, 2) !== round((float) $before, 2); }
        return json_encode($this->engineValue($key, $value)) !== json_encode($this->engineValue($key, $before));
    }

    /**
     * Validate settings and every secret group together, so one response shows
     * every problem. Returns [settings, s3 keys|null, site password|null, key setup|null].
     */
    private function validatedInput(): array
    {
        $errors = [];
        $states = [];
        $this->collectingErrors = true;
        try {
            foreach (['settingsForm', 's3KeysForm', 'siteAccessForm', 'encryptionForm'] as $form) {
                try { $states[$form] = $this->{$form}->getState(); }
                catch (ValidationException $exception) { $errors += $exception->errors(); }
            }
            $siteEntered = filled($this->siteAccess['username'] ?? null) || filled($this->siteAccess['password'] ?? null);
            if ($siteEntered && $this->httpsOrigin() === null) {
                $errors['siteAccess.username'] = [Messages::error('maintenance-probe-https-required')];
            }
        } finally {
            $this->collectingErrors = false;
        }
        if ($errors) {
            Notification::make()->title('Settings not saved')->body('Fix the highlighted fields.')->danger()->send();
            $this->dispatch('form-validation-error', livewireId: $this->getId());
            throw ValidationException::withMessages($errors);
        }
        $state = $states['settingsForm'];
        $uploads = (bool) ($this->settings['upload_s3'] ?? false);
        $s3 = $states['s3KeysForm'] ?? [];
        $s3Keys = $uploads && (filled($s3['access_key'] ?? null) || filled($s3['secret_key'] ?? null))
            ? array_filter(['access_key' => (string) $s3['access_key'], 'secret_key' => (string) $s3['secret_key'], 'session_token' => filled($s3['session_token'] ?? null) ? (string) $s3['session_token'] : null], static fn ($value) => $value !== null)
            : null;
        $site = $states['siteAccessForm'] ?? [];
        // The address is Paymenter's own; a browser cannot point the check elsewhere.
        $siteAccess = filled($site['username'] ?? null) ? ['origin' => $this->httpsOrigin(), 'username' => (string) $site['username'], 'password' => (string) $site['password']] : null;
        $keySetup = null;
        if ($this->keySetupApplies()) {
            $source = ($this->encryptionInput['source'] ?? 'generate') === 'passphrase' ? 'passphrase' : 'generate';
            $keySetup = $source === 'passphrase' ? ['source' => 'passphrase', 'passphrase' => (string) ($states['encryptionForm']['passphrase'] ?? '')] : ['source' => 'generate'];
        }
        // Laravel's character limits do not enforce the bridge's UTF-8 byte
        // limits. Validate every group before config check or native writes.
        if ($s3Keys !== null) {
            try { CredentialInput::merge([], ['s3' => $s3Keys], ''); }
            catch (\RuntimeException $error) {
                foreach (array_keys($s3Keys) as $field) { $errors['s3Keys.' . $field] = [Messages::error($error->getMessage())]; }
            }
        }
        if ($siteAccess !== null) {
            try { CredentialInput::merge([], ['site' => $siteAccess], $this->httpsOrigin() ?? ''); }
            catch (\RuntimeException $error) {
                foreach (['username', 'password'] as $field) { $errors['siteAccess.' . $field] = [Messages::error($error->getMessage())]; }
            }
        }
        if ($s3Keys !== null || $siteAccess !== null) {
            try {
                if (strlen(json_encode(['s3' => $s3Keys, 'site' => $siteAccess], JSON_THROW_ON_ERROR)) > 65536) {
                    throw new \RuntimeException('credentials-invalid');
                }
            } catch (\JsonException|\RuntimeException $error) {
                foreach ($s3Keys !== null ? array_keys($s3Keys) : [] as $field) { $errors['s3Keys.' . $field] = [Messages::error('credentials-invalid')]; }
                foreach ($siteAccess !== null ? ['username', 'password'] : [] as $field) { $errors['siteAccess.' . $field] = [Messages::error('credentials-invalid')]; }
            }
        }
        if (($keySetup['source'] ?? null) === 'passphrase') {
            try { ArchiveEncryption::secret($keySetup['passphrase']); }
            catch (\RuntimeException $error) { $errors['encryptionInput.passphrase'] = [Messages::error($error->getMessage())]; }
        }
        if ($errors) {
            Notification::make()->title('Settings not saved')->body('Fix the highlighted fields.')->danger()->send();
            $this->dispatch('form-validation-error', livewireId: $this->getId());
            throw ValidationException::withMessages($errors);
        }
        return [$state, $s3Keys, $siteAccess, $keySetup];
    }

    /** A recovery key is created on save when encrypted uploads are on and no key exists. */
    private function keySetupApplies(): bool
    {
        return (bool) ($this->settings['upload_s3'] ?? false) && (bool) ($this->settings['s3_encryption'] ?? false) && $this->keyState() === 'none';
    }

    private function s3KeysRequired(): bool
    {
        if (!($this->settings['upload_s3'] ?? false)) { return false; }
        if (filled($this->s3Keys['access_key'] ?? null) || filled($this->s3Keys['secret_key'] ?? null) || filled($this->s3Keys['session_token'] ?? null)) { return true; }
        // Turning S3 on needs keys. An S3 setup whose keys were removed still saves other
        // changes; Setup then asks for the keys.
        return !$this->savedS3Keys() && !($this->formValues()['upload_s3'] ?? false);
    }

    private function siteRequired(): bool
    {
        return filled($this->siteAccess['username'] ?? null) || filled($this->siteAccess['password'] ?? null);
    }

    public function saveSettings(): void
    {
        $this->authorizeAction();
        [$state, $s3Keys, $siteAccess, $keySetup] = $this->validatedInput();
        $this->storedForm = null;
        $stored = $this->formValues();
        $repairBaseline = $this->storedRows;
        if (($this->publicDirectoriesRepairPin !== null
                && !hash_equals($this->publicDirectoriesRepairPin, hash('sha256', serialize($repairBaseline))))
            || (($state['confirm_public_directories'] ?? false) === true && $this->publicDirectoriesRepairPin === null)) {
            $this->refuseSettings('config-changed');
            return;
        }
        if (in_array('extra_docroots', $this->malformedKeys, true)
            && (!array_key_exists('extra_docroots', $state) || ($state['confirm_public_directories'] ?? false) !== true)) {
            // Never substitute an empty default for a malformed public-directory guard.
            $this->refuseSettings('document-roots-invalid');
            return;
        }
        $v2 = Settings::v2Keys();
        $privacyRepair = in_array('extra_docroots', $this->malformedKeys, true);
        // What the administrator changed, against native rows read now.
        $userChanges = [];
        foreach ($state as $key => $value) {
            if (!in_array($key, $v2, true) || !$this->changed($key, $value)) { continue; }
            $engineValue = $this->engineValue($key, $value);
            // Repair the privacy guard first. Other damaged rows' visible
            // defaults are implicit repairs, not a second user edit; defer them.
            if ($privacyRepair && !in_array($key, self::BOOTSTRAP, true) && in_array($key, $this->malformedKeys, true)
                && json_encode($engineValue) === json_encode($this->engineValue($key, $stored[$key]))) { continue; }
            $userChanges[$key] = $engineValue;
        }
        $encrypted = (bool) ($userChanges['s3_encryption'] ?? $this->engineValue('s3_encryption', $stored['s3_encryption']));
        if (!$privacyRepair && $encrypted && $this->engineValue('plaintext_offsite_acknowledged', $stored['plaintext_offsite_acknowledged'])) {
            // Turning encryption off later needs a fresh confirmation.
            $userChanges['plaintext_offsite_acknowledged'] = false;
        }
        $changes = $userChanges;
        foreach ($this->malformedKeys as $key) {
            // Hidden optional fields still need their invalid native rows repaired.
            if (in_array($key, $v2, true) && !array_key_exists($key, $changes)) { $changes[$key] = $this->engineValue($key, $stored[$key]); }
        }
        foreach ($this->malformedKeys as $key) {
            // Malformed 0.3 rows are reset too; the CLI still reads mode as its default type.
            if (in_array($key, Settings::LEGACY, true)) { $changes[$key] = Settings::DEFAULTS[$key]; }
        }
        if ($this->storedVersion !== 2) {
            // The first save in this version writes every key explicitly.
            foreach ($v2 as $key) {
                if (!array_key_exists($key, $changes)) { $changes[$key] = $this->engineValue($key, $stored[$key]); }
            }
            $changes['settings_version'] = 2;
        }
        $repaired = array_values(array_filter($this->malformedKeys, static fn ($key) => $key !== 'extra_docroots'));
        $initialized = $this->initialized();
        $secrets = $s3Keys !== null || $siteAccess !== null;
        // The first save creates private storage, even when every default is kept.
        if (!$changes && $initialized && !$secrets && $keySetup === null) {
            $kept = array_intersect_key(['s3' => 'Saved S3 keys weren\'t changed.', 'site' => 'The saved site password wasn\'t changed.', 'passphrase' => 'No passphrase was saved.'], array_filter($this->secretsCleared));
            foreach (array_keys($kept) as $group) { $this->secretsSaved[$group] = true; }
            Notification::make()->title('No changes')->body($kept ? implode(' ', $kept) : null)->info()->send();
            return;
        }
        if (array_key_exists('private_root', $changes) && $initialized && $this->engineValue('private_root', $stored['private_root']) !== $changes['private_root']) {
            // Moving would silently start a new identity without the saved keys or history.
            $this->refuseSettings('private-root-move-refused');
            return;
        }
        if (array_key_exists('private_root', $userChanges) || array_key_exists('extra_docroots', $userChanges)) {
            try {
                $docroots = $changes['extra_docroots'] ?? $this->engineValue('extra_docroots', $stored['extra_docroots']);
                PrivateStore::root(realpath(base_path()), ($changes['private_root'] ?? $this->engineValue('private_root', $stored['private_root'])) ?: null, false, $docroots);
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
            if (!$userChanges || !in_array($code, self::ENVIRONMENT_ERRORS, true) || array_diff(array_keys($userChanges), self::BOOTSTRAP)) {
                if (in_array('extra_docroots', $this->malformedKeys, true)) {
                    Notification::make()->title('Settings not saved')->danger()->body('Save the folder list by itself first. Then save your other changes.')->send();
                    return;
                }
                $this->refuseSettings($code, in_array($code, self::ENVIRONMENT_ERRORS, true));
                return;
            }
            $checked = false;
            $changes = $userChanges;
        }
        $record = $this->record();
        try {
            if ($checked) {
                $saved = Runtime::saveConfiguration($changes, $secrets ? ['s3' => $s3Keys, 'site' => $siteAccess] : null);
            } else {
                SettingsWriter::repairBinaries($record, $changes, $repairBaseline);
                $saved = ['updated' => true];
            }
        } catch (\Throwable $error) {
            $this->storedForm = null;
            $this->overviewCache = null;
            $code = $error instanceof \RuntimeException ? $error->getMessage() : 'config-save-outcome-unknown';
            if ($code === 'config-save-outcome-unknown') {
                Notification::make()->title('Save outcome unknown')->warning()
                    ->body('Refresh and check the saved settings before trying again. Keys you entered were cleared.')->send();
            } else { $this->refuseSettings($code); }
            return;
        } finally {
            unset($s3Keys, $siteAccess);
        }
        $this->storedForm = null;
        $this->overviewCache = null;
        $this->overviewError = null;
        $this->settingsForm->fill($this->formValues());
        $this->publicDirectoriesRepairPin = in_array('extra_docroots', $this->malformedKeys, true)
            ? hash('sha256', serialize($this->storedRows)) : null;
        if (!$checked) {
            (new Extension)->configurationUpdated($record);
            $this->overview();
            $body = $this->overviewError ? 'Backups still can\'t start: ' . Messages::error($this->overviewError) : 'Backups can start again. Save your other changes now.';
            if ($secrets) { $body .= ' Keys you entered weren\'t saved; enter them again.'; }
            Notification::make()->title('Setup settings saved')->warning()->body($body)->send();
            return;
        }
        if (($saved['credentials_updated'] ?? false) === true) { $this->secretsSaved['s3'] = true; }
        if (($saved['maintenance_probe_credentials_updated'] ?? false) === true) { $this->secretsSaved['site'] = true; }
        if (($saved['updated'] ?? false) !== true) {
            Notification::make()->title('Save incomplete')->warning()
                ->body(($saved['outcome_unknown'] ?? false)
                    ? 'The saved settings or keys could not be confirmed. Backup work is paused. Refresh, check settings and enter the replacement keys again to complete Save.'
                    : (($saved['concurrent_settings_preserved'] ?? false)
                        ? 'Concurrent settings changes were preserved. Keys you entered weren\'t saved. Refresh and check settings before trying again.'
                        : 'Some settings could not be restored. Backup work is paused. Keys you entered weren\'t saved. Refresh, check settings and enter the replacement keys again to complete Save.'))->send();
            return;
        }
        $problems = [];
        if ($saved['partial'] ?? false) {
            $problems[] = 'Settings and entered keys were saved, but final setup did not finish: ' . Messages::error($saved['error'] ?? 'config-save-failed') . ' Refresh and check setup.';
        }
        $created = false;
        if ($keySetup !== null) {
            try {
                $result = $keySetup['source'] === 'passphrase'
                    ? Runtime::encryptionKey('import', ['passphrase' => $keySetup['passphrase']])
                    : Runtime::encryptionKey('generate');
                $this->recoveryFingerprint = is_string($result['fingerprint'] ?? null) ? $result['fingerprint'] : null;
                $created = true;
                if ($keySetup['source'] === 'passphrase') { $this->secretsSaved['passphrase'] = true; }
            } catch (\Throwable $error) {
                $problems[] = 'The recovery key wasn\'t created: ' . Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-invalid');
            } finally {
                unset($keySetup);
            }
        }
        $this->overviewCache = null;
        $title = $initialized ? 'Settings saved' : 'Setup saved';
        $body = [];
        if (!$initialized) { $body[] = 'Backups can start once the items on the Backups tab are done.'; }
        if ($repaired) {
            $body[] = 'Some saved values were invalid and were reset: ' . implode(', ', array_map(static fn ($key) => Settings::LABELS[$key][0], $repaired)) . '. Check them.';
        }
        if ($created) { $body[] = 'Save the recovery key outside this server now. Uploads to S3 start after you confirm it.'; }
        // Keys typed earlier were cleared before this Save; say plainly that the saved ones stay.
        foreach (['s3' => 'Saved S3 keys weren\'t changed.', 'site' => 'The saved site password wasn\'t changed.'] as $group => $sentence) {
            if (($this->secretsCleared[$group] ?? false) && !isset($this->secretsSaved[$group])) {
                $body[] = $sentence;
                $this->secretsSaved[$group] = true;
            }
        }
        array_push($body, ...$problems);
        Notification::make()->title($title)->body($body ? implode(' ', $body) : null)
            ->status($problems || $repaired ? 'warning' : 'success')->send();
        if ($created) {
            // The one moment the key matters most: offer it immediately, behind the same confirmation.
            // Mounted from the browser after this response, because a mount here would render
            // only the dialog and leave the saved form and setup stale.
            $this->js('$wire.mountAction(\'showRecoveryKey\')');
        }
    }

    private function refuseSettings(string $code, bool $environment = false): void
    {
        $message = $code === 'schedule-overlaps-billing' && ($window = $this->billingWindow())
            ? 'This time is too close to Paymenter\'s daily billing at ' . $window['time'] . ' ' . $window['timezone'] . '. Pick a time outside ' . $window['start'] . ' to ' . $window['end'] . '.'
            : Messages::error($code);
        if ($field = Messages::field($code)) {
            $this->addError('settings.' . $field, $message);
            // Opens a collapsed section and scrolls to the field, as for form validation.
            $this->dispatch('form-validation-error', livewireId: $this->getId());
        }
        Notification::make()->title('Settings not saved')->danger()
            ->body($environment ? $message . ' Fix Binary paths under Advanced, save, then save your other changes.' : $message)->send();
    }

    // ---------------------------------------------------------------- Secrets

    public function s3KeysForm(Schema $schema): Schema
    {
        $saved = fn () => $this->savedS3Keys();
        $secret = fn (string $key, string $label) => TextInput::make($key)->label($label)->password()->revealable(false)->maxLength(8192)->autocomplete('new-password')
            ->placeholder(fn () => $saved() ? 'Saved. Leave empty to keep.' : null)
            ->required(fn () => $this->s3KeysRequired())
            ->validationMessages(['required' => $key === 'access_key' ? 'Enter your access key ID.' : 'Enter your secret access key.']);
        return $schema->components([
            Grid::make(['default' => 1, 'md' => 2])->schema([
                $secret('access_key', 'Access key ID'),
                $secret('secret_key', 'Secret access key'),
                TextInput::make('session_token')->label('Session token (optional)')->password()->revealable(false)->maxLength(8192)->autocomplete('new-password')
                    ->helperText('Only for temporary credentials. Saving new keys clears a saved token unless you enter it again.'),
                Group::make([
                    Text::make('Keys saved.')->visible($saved),
                    Actions::make([$this->removeS3KeysAction()]),
                ]),
                Text::make('Enter the keys again. They were cleared for security when the form updated.')->color('warning')->columnSpanFull()
                    ->visible(fn () => $this->secretsCleared['s3'] ?? false),
            ]),
        ])->statePath('s3Keys');
    }

    public function siteAccessForm(Schema $schema): Schema
    {
        $saved = fn () => (bool) ($this->configuration()['saved_keys']['site'] ?? false);
        return $schema->components([
            Grid::make(['default' => 1, 'md' => 2])->schema([
                TextInput::make('username')->label('Username')->helperText('Not your Paymenter login.')->maxLength(8192)->autocomplete('off')
                    ->placeholder(fn () => $saved() ? 'Saved. Leave empty to keep.' : null)
                    ->required(fn () => $this->siteRequired())->markAsRequired(false)
                    ->rule('not_regex:/[:\x00-\x1f\x7f]/')->validationMessages(['not_regex' => 'The username can\'t contain ":" or control characters.', 'required' => 'Enter the username too.']),
                TextInput::make('password')->label('Password')->password()->revealable(false)->maxLength(8192)->autocomplete('new-password')
                    ->placeholder(fn () => $saved() ? 'Saved. Leave empty to keep.' : null)
                    ->required(fn () => $this->siteRequired())->markAsRequired(false)
                    ->validationMessages(['required' => 'Enter the password too.']),
                Text::make('Enter the username and password again. They were cleared for security when the form updated.')->color('warning')->columnSpanFull()
                    ->visible(fn () => $this->secretsCleared['site'] ?? false),
            ]),
        ])->statePath('siteAccess');
    }

    public function encryptionForm(Schema $schema): Schema
    {
        return $schema->components([
            // Shown and hidden in the browser: a server round trip would clear a typed passphrase.
            Radio::make('source')->label('Recovery key')->required()
                ->options(['generate' => 'Generate a recovery key (recommended)', 'passphrase' => 'Use my own passphrase'])
                ->descriptions([
                    'generate' => 'A random 64-character key, created when you save.',
                    'passphrase' => 'At least 20 characters. A few random words are easy to keep and hard to guess.',
                ]),
            TextInput::make('passphrase')->label('Passphrase')->password()->revealable(false)->autocomplete('new-password')
                ->helperText('Keep a copy outside this server. Without it, backups in S3 can\'t be restored if this server is lost.')
                ->required(fn (Get $get) => $get('source') === 'passphrase' && $this->keySetupApplies())
                ->rules(['nullable', 'string', 'min:20', 'max:1024', 'not_regex:/[\x00-\x1f\x7f]/'])
                ->validationMessages(['min' => 'Use at least 20 characters.', 'not_regex' => 'Don\'t use line breaks or control characters.', 'required' => 'Enter a passphrase, or generate a key instead.'])
                ->visibleJs(<<<'JS'
                    $get('source') === 'passphrase'
                    JS),
            Text::make('Enter the passphrase again. It was cleared for security when the form updated.')->color('warning')
                ->visible(fn () => $this->secretsCleared['passphrase'] ?? false),
        ])->statePath('encryptionInput');
    }

    public function removeS3KeysAction(): Action
    {
        return Action::make('removeS3Keys')->label('Remove saved keys')->color('danger')->link()
            ->visible(fn () => $this->savedS3Keys())
            ->requiresConfirmation()->modalHeading('Remove saved S3 keys?')
            ->modalDescription('Uploads to S3 stop until you add keys again. Backups already in S3 are kept. The keys stay valid at your provider; revoke them there if needed.')
            ->modalSubmitActionLabel('Remove keys')
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

    public function removeSitePasswordAction(): Action
    {
        return Action::make('removeSitePassword')->label('Remove saved password')->color('danger')->link()
            ->visible(fn () => $this->configuration()['saved_keys']['site'] ?? false)
            ->requiresConfirmation()->modalHeading('Remove the saved site password?')
            ->modalDescription('Full backups will check the site without it. Your web server\'s password isn\'t changed.')
            ->modalSubmitActionLabel('Remove password')
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

    // ---------------------------------------------------------------- Recovery key

    /** Show, download and confirm, for a key that still needs its confirmation; show and download otherwise. */
    private function recoveryKeyActions(): array
    {
        return [$this->showRecoveryKeyAction(), $this->downloadRecoveryKeyAction(), $this->acknowledgeRecoveryKeyAction()];
    }

    public function showRecoveryKeyAction(): Action
    {
        return Action::make('showRecoveryKey')->label('Show recovery key')->icon('ri-eye-line')->color('gray')->button()->size(Size::Small)
            ->visible(fn () => $this->keyState() !== 'none')
            ->requiresConfirmation()->modalIcon('ri-key-2-line')->modalHeading('Show the recovery key?')
            ->modalDescription('Make sure no one else can see your screen.')
            ->modalSubmitActionLabel('Show key')
            ->action(function () {
                $this->authorizeAction();
                $this->grantReveal();
                $this->replaceMountedAction('recoveryKey');
            });
    }

    /** The key a reveal would show: the one awaiting confirmation, else the active key. */
    private function revealIdentity(): ?string
    {
        try { return Runtime::recoveryKeyFingerprint(); }
        catch (\Throwable) { return null; }
    }

    /** One-time, short-lived permission set only by a confirmed Show (or a confirmed rotation). */
    private function grantReveal(): void
    {
        $identity = $this->revealIdentity();
        if ($identity === null) { throw new \RuntimeException('archive-key-unavailable'); }
        session()->put(self::REVEAL_GRANT, ['user' => Auth::id(), 'key' => $identity, 'until' => time() + 30]);
    }

    /**
     * Whether this component may render the key now. A grant is consumed once and
     * opens a two-minute window for this component and this key only; a direct
     * mount or ?action= link has no grant and shows nothing.
     */
    private function revealAllowed(): bool
    {
        $identity = $this->revealIdentity();
        if ($identity === null) { return false; }
        if ($this->revealGrantUntil !== null && $this->revealGrantUntil >= time() && is_string($this->revealGrantKey) && hash_equals($this->revealGrantKey, $identity)) {
            return true;
        }
        $grant = session()->pull(self::REVEAL_GRANT);
        if (!is_array($grant) || ($grant['user'] ?? null) !== Auth::id() || (int) ($grant['until'] ?? 0) < time()
            || !is_string($grant['key'] ?? null) || !hash_equals($grant['key'], $identity)) {
            return false;
        }
        $this->revealGrantUntil = time() + 120;
        $this->revealGrantKey = $identity;
        return true;
    }

    /** Opened only from Show recovery key. The key lives in this modal's rendered content, never in Livewire state. */
    public function recoveryKeyAction(): Action
    {
        return Action::make('recoveryKey')->modalHeading('Recovery key')->modalWidth('2xl')
            ->mountUsing(function (Action $action) {
                $this->authorizeAction();
                if (!$this->revealAllowed()) {
                    Notification::make()->title('Recovery key not shown')->warning()->body('Confirm Show recovery key first.')->send();
                    $action->halt();
                }
            })
            ->schema(fn () => $this->recoveryKeySchema())
            ->modalSubmitAction(false)->modalCancelActionLabel('Close')
            ->extraModalFooterActions(fn () => array_filter([
                $this->downloadRecoveryKeyAction(),
                $this->pendingKeyShort() !== null ? Action::make('recoveryKeySaved')->label('I saved it')->color('primary')
                    ->action(fn () => $this->replaceMountedAction('acknowledgeRecoveryKey')) : null,
            ]));
    }

    private function revealKey(): ?array
    {
        if ($this->revealedKey !== null) { return $this->revealedKey; }
        $this->authorizeAction();
        try {
            if (!$this->revealAllowed() || $this->revealGrantKey === null) { throw new \RuntimeException('archive-key-mismatch'); }
            $key = Runtime::revealRecoveryKey($this->revealGrantKey);
            if (!is_string($key['fingerprint'] ?? null) || !hash_equals($this->revealGrantKey, $key['fingerprint'])) {
                throw new \RuntimeException('archive-key-mismatch');
            }
        } catch (\Throwable $error) {
            return $this->revealedKey = ['error' => $error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'];
        }
        if (is_string($key['fingerprint'] ?? null)) { $this->recoveryFingerprint = $key['fingerprint']; }
        return $this->revealedKey = ['secret' => (string) ($key['secret'] ?? ''), 'fingerprint' => (string) ($key['fingerprint'] ?? '')];
    }

    private function recoveryKeySchema(): array
    {
        if (!$this->revealAllowed()) {
            return [Text::make('Confirm Show recovery key first.')];
        }
        $key = $this->revealKey();
        if (isset($key['error'])) {
            return [Callout::make('The recovery key can\'t be shown')->danger()->description(Messages::error($key['error']))];
        }
        return [
            Text::make('Save this recovery key in your password manager or another safe place outside this server. You need it to restore backups from S3 if this server is lost. APP_KEY and your S3 keys can\'t replace it.'),
            $this->command($key['secret'])->size('lg'),
            Text::make('Fingerprint (not secret; it identifies the key on your backups):'),
            Text::make($key['fingerprint'])->fontFamily(FontFamily::Mono)->extraAttributes(['style' => 'overflow-wrap: anywhere']),
        ];
    }

    public function downloadRecoveryKeyAction(): Action
    {
        return Action::make('downloadRecoveryKey')->label('Download recovery key')->icon('ri-download-2-line')->color('gray')->button()->size(Size::Small)
            ->visible(fn () => $this->keyState() !== 'none')
            ->action(function () {
                $this->authorizeAction();
                try {
                    // A fresh signed address each time; it expires after a few minutes.
                    $url = Runtime::recoveryKeyUrl();
                    parse_str((string) parse_url($url, PHP_URL_QUERY), $query);
                    if (is_string($query['fingerprint'] ?? null) && preg_match('/^[a-f0-9]{64}$/D', $query['fingerprint'])) {
                        $this->recoveryFingerprint = $query['fingerprint'];
                    }
                    FilamentView::spaUrlExceptions([$url]);
                    $this->redirect($url, navigate: false);
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not downloaded')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                }
            });
    }

    public function acknowledgeRecoveryKeyAction(): Action
    {
        return Action::make('acknowledgeRecoveryKey')->label('I saved it')->icon('ri-check-line')->color('primary')->button()->size(Size::Small)
            ->visible(fn () => $this->pendingKeyShort() !== null)
            ->modalHeading('Confirm the recovery key is saved')
            ->modalDescription(fn () => 'Uploads to S3 ' . ($this->keyState() === 'active' ? 'switch to recovery key ' : 'start with recovery key ') . $this->pendingKeyShort() . ' after this.')
            ->mountUsing(function (Action $action, Schema $schema) {
                $this->authorizeAction();
                $this->acknowledgementFingerprint = null;
                try {
                    $this->acknowledgementFingerprint = $this->pendingFingerprint();
                    $schema->fill(['saved' => false]);
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not confirmed')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                    $action->halt();
                }
            })
            ->schema([
                Checkbox::make('saved')->label('I saved this recovery key outside this server.')->accepted()
                    ->validationMessages(['accepted' => 'Save the key first, then check this.']),
            ])
            ->modalSubmitActionLabel('Confirm')
            ->action(function (Action $action) {
                $this->authorizeAction();
                try {
                    $fingerprint = $this->acknowledgementIdentity();
                    Runtime::encryptionKey('acknowledge', ['fingerprint' => $fingerprint]);
                    $this->overviewCache = null;
                    Notification::make()->title('Recovery key confirmed')->body('Uploads to S3 can start.')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not confirmed')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                    $action->halt();
                }
            });
    }

    /**
     * Current full pending identity, without revealing any key material.
     * A previously shown or downloaded identity must still name this key.
     */
    private function pendingFingerprint(): string
    {
        $fingerprint = $this->keyInfo()['pending_fingerprint'] ?? null;
        if (!is_string($fingerprint) || !preg_match('/^[a-f0-9]{64}$/D', $fingerprint)) { throw new \RuntimeException('archive-key-unavailable'); }
        $short = $this->pendingKeyShort();
        if ($short === null || !str_starts_with($fingerprint, $short)
            || ($this->recoveryFingerprint !== null && !hash_equals($fingerprint, $this->recoveryFingerprint))) {
            throw new \RuntimeException('archive-key-mismatch');
        }
        return $fingerprint;
    }

    private function acknowledgementIdentity(): string
    {
        $fingerprint = $this->acknowledgementFingerprint;
        if (!is_string($fingerprint) || !preg_match('/^[a-f0-9]{64}$/D', $fingerprint)) { throw new \RuntimeException('archive-key-unavailable'); }
        if (!hash_equals($fingerprint, $this->pendingFingerprint())) { throw new \RuntimeException('archive-key-mismatch'); }
        return $fingerprint;
    }

    public function rotateRecoveryKeyAction(): Action
    {
        return Action::make('rotateRecoveryKey')->label('Create a new recovery key')->color('gray')->link()
            ->visible(fn () => $this->keyState() === 'active' && $this->pendingKeyShort() === null)
            ->requiresConfirmation()->modalHeading('Create a new recovery key?')
            ->modalDescription('New backups in S3 will use the new key. Earlier backups still need their earlier keys, so keep every recovery key you\'ve saved. Uploads keep using the current key until you save the new one and confirm.')
            ->modalSubmitActionLabel('Create key')
            ->action(function () {
                $this->authorizeAction();
                try {
                    $result = Runtime::encryptionKey('generate');
                    $this->recoveryFingerprint = is_string($result['fingerprint'] ?? null) ? $result['fingerprint'] : null;
                    $this->overviewCache = null;
                    $this->grantReveal();
                    $this->replaceMountedAction('recoveryKey');
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not created')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-invalid'))->send();
                }
            });
    }

    public function discardRecoveryKeyAction(): Action
    {
        return Action::make('discardRecoveryKey')->label('Discard this key')->color('danger')->link()
            ->visible(fn () => $this->pendingKeyShort() !== null && !($this->keyState() === 'active' && $this->keyInfo()['acknowledged'] && blank($this->keyInfo()['pending_short'])))
            ->requiresConfirmation()->modalHeading('Discard the unconfirmed recovery key?')
            ->modalDescription(fn () => $this->keyState() === 'active'
                ? 'Uploads keep using recovery key ' . $this->keyInfo()['short'] . '.'
                : 'Uploads to S3 stay paused until a new key is created and confirmed. Save settings to create one.')
            ->modalSubmitActionLabel('Discard key')
            ->action(function () {
                $this->authorizeAction();
                try {
                    Runtime::encryptionKey('discard-pending');
                    $this->recoveryFingerprint = null;
                    $this->overviewCache = null;
                    Notification::make()->title('Recovery key discarded')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not discarded')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                }
            });
    }

    // ---------------------------------------------------------------- Actions

    /** Queue one durable request. The scheduler tick does the work. */
    private function queue(string $action, array $arguments, string $title, ?string $body = null): bool
    {
        $this->authorizeAction();
        $arguments['actor'] = (int) Auth::id();
        $start = $this->startLine();
        try {
            Runtime::command(['requests', 'create', '--action', $action], $arguments);
        } catch (\Throwable $error) {
            Notification::make()->title(str_replace(' queued', ' not queued', $title))->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable'))->send();
            return false;
        }
        $this->overviewCache = null;
        Notification::make()->title($title)->success()->body($body ?? $start)->send();
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

    /** A type that can start now, database first because it never takes the site offline. */
    private function preferredMode(): string
    {
        foreach (['full-quiesced', 'database-online'] as $mode) {
            if (($this->scheduleFor($mode)['frequency'] ?? 'off') !== 'off' && $this->blockers($mode) === []) { return $mode; }
        }
        foreach (['database-online', 'full-quiesced'] as $mode) {
            if ($this->blockers($mode) === []) { return $mode; }
        }
        return 'database-online';
    }

    /** The same dialog serves the page header and the empty history. */
    private function backupAction(string $name): Action
    {
        $blockedTitle = fn () => match (true) {
            $this->nothingCanStart() => 'Backups aren\'t ready',
            $this->blockers('full-quiesced') !== [] => 'Full backups aren\'t ready',
            default => 'Database backups aren\'t ready',
        };
        $blockedText = function () {
            $modes = $this->nothingCanStart() ? ['database-online', 'full-quiesced'] : [$this->blockers('full-quiesced') !== [] ? 'full-quiesced' : 'database-online'];
            $titles = [];
            foreach ($modes as $mode) { foreach ($this->blockers($mode) as $item) { $titles[$item['title']] = true; } }
            return implode('. ', array_keys($titles)) . '.';
        };
        $base = [
            'full-quiesced' => 'Database and all files. The site shows its maintenance page while the copy runs.',
            'database-online' => 'Database only. The site stays online.',
        ];
        return Action::make($name)->label('Create backup')->icon('ri-add-line')
            ->modalHeading('Create backup')
            ->modalDescription('Runs in the background. You can leave this page.')
            ->fillForm(fn () => ['mode' => $this->preferredMode()])
            ->schema([
                Radio::make('mode')->label('Type')->required()->live()
                    ->options(Messages::MODES)
                    ->descriptions(fn () => array_map(fn ($mode) => $base[$mode] . ($this->blockers($mode) !== [] ? ' Not ready yet.' : ''), array_combine(array_keys($base), array_keys($base))))
                    // A type that cannot start is shown but not selectable; the callout says why.
                    ->disableOptionWhen(fn (string $value) => !$this->nothingCanStart() && $this->blockers($value) !== []),
                Callout::make($blockedTitle)->warning()
                    ->description($blockedText)
                    ->actions([$this->link($name . 'Setup', 'See what\'s needed', static::getUrl(['tab' => 'backups']) . '#setup')])
                    ->visible(fn () => $this->blockers('full-quiesced') !== [] || $this->blockers('database-online') !== []),
                Text::make(fn (Get $get) => $this->createInfoLine($get('mode')))->color('gray')
                    ->visible(fn (Get $get) => !$this->nothingCanStart() && $this->createInfoLine($get('mode')) !== null),
            ])
            ->modalSubmitAction(fn (Action $action) => $this->nothingCanStart() ? false : $action)
            ->modalSubmitActionLabel('Create backup')
            ->modalCancelActionLabel(fn () => $this->nothingCanStart() ? 'Close' : 'Cancel')
            ->action(function (array $data, Action $action) {
                $mode = in_array($data['mode'] ?? null, array_keys(self::KINDS), true) ? $data['mode'] : null;
                if ($mode === null || ($items = $this->blockers($mode)) !== []) {
                    $reason = isset($items) && $items ? implode('. ', array_map(static fn ($item) => $item['title'], $items)) . '.' : 'Choose a backup type that\'s ready.';
                    Notification::make()->title('Backup not queued')->danger()->body($reason)->send();
                    $action->halt();
                }
                if (!$this->queue('run', ['mode' => $mode], 'Backup queued')) { $action->halt(); }
            });
    }

    /** At most one line about when a new backup starts. */
    private function createInfoLine(?string $mode): ?string
    {
        $mode = in_array($mode, array_keys(self::KINDS), true) ? $mode : 'database-online';
        if ($request = $this->activeRequest('run')) { return 'A ' . strtolower(Messages::mode($request['mode'] ?: $mode)) . ' is already queued.'; }
        if (!$this->schedulerFresh()) {
            return $this->lastTick() ? 'It waits for Paymenter\'s cron job, which last ran ' . $this->ago($this->lastTick()) . '.' : 'It waits for Paymenter\'s cron job, which hasn\'t run yet.';
        }
        $transient = array_filter($this->readiness($mode)['items'], static fn ($item) => in_array($item['code'], self::TRANSIENT, true));
        return $transient ? 'May start late: it waits for Paymenter\'s daily billing run or a running cron job to finish.' : null;
    }

    public function testStorageAction(): Action
    {
        return Action::make('testStorage')->label('Test storage')->color('gray')
            ->disabled(fn () => $this->activeRequest('destination-test') !== null)
            ->tooltip(fn () => $this->activeRequest('destination-test') ? 'A storage test is already queued.' : 'Writes, reads and deletes a small test file in ' . ($this->uploadsToS3() ? 'the S3 bucket' . (filled($this->storage()['s3_bucket'] ?? null) ? ' ' . $this->storage()['s3_bucket'] : '') : 'the backup directory') . '.')
            ->action(fn () => $this->queue('destination-test', [], 'Storage test queued'));
    }

    /** Backups that cleanup would delete now, per place, from the engine. */
    private function retentionPending(): array
    {
        $pending = $this->configuration()['retention']['pending'] ?? null;
        $local = 0;
        $s3 = 0;
        if (is_array($pending)) {
            foreach (['full', 'database'] as $type) {
                if (is_array($pending[$type] ?? null)) { $local += (int) ($pending[$type]['local'] ?? 0); $s3 += (int) ($pending[$type]['s3'] ?? 0); }
            }
            if (!isset($pending['full']) && !isset($pending['database'])) { $local = (int) ($pending['local'] ?? 0); $s3 = (int) ($pending['s3'] ?? 0); }
        }
        return ['local' => $local, 's3' => $s3];
    }

    public function applyRetentionAction(): Action
    {
        $count = static fn (int $n) => $n . ($n === 1 ? ' backup' : ' backups');
        return Action::make('applyRetention')->label('Delete old backups now')->color('gray')
            ->disabled(fn () => array_sum($this->retentionPending()) === 0)
            ->tooltip(fn () => array_sum($this->retentionPending()) === 0 ? 'Nothing to delete' : null)
            ->requiresConfirmation()->modalHeading('Delete old backups now?')
            ->modalDescription(function () use ($count) {
                $pending = $this->retentionPending();
                $places = array_filter([$pending['local'] ? $count($pending['local']) . ' on this server' : null, $pending['s3'] ? $count($pending['s3']) . ' in S3' : null]);
                return 'Deletes ' . implode(' and ', $places) . ' beyond your numbers to keep. The newest verified backup in each place is always kept.';
            })
            ->modalSubmitActionLabel('Delete old backups')
            ->action(function () {
                if (array_sum($this->retentionPending()) === 0) { return; }
                $this->queue('retention', ['execute' => true], 'Cleanup queued');
            });
    }

    // ---------------------------------------------------------------- History table

    /** Whether a request came from a schedule, by request ID. */
    private function scheduledRequests(): array
    {
        $scheduled = [];
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] === 'run' && $request['request_id']) { $scheduled[$request['request_id']] = (bool) $request['scheduled']; }
        }
        return $scheduled;
    }

    /** History rows: queued or refused backup requests, then backups. */
    private function historyRows(bool $showDeleted): array
    {
        $rows = [];
        $started = [];
        $scheduled = $this->scheduledRequests();
        $uploads = $this->uploadsToS3();
        foreach ($this->overview()['runs'] as $run) {
            if ($run['request_id']) { $started[$run['request_id']] = true; }
        }
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] !== 'run' || isset($started[$request['request_id']]) || $request['result_run_id']) { continue; }
            if (in_array($request['status'], ['complete', 'cancelled', 'satisfied'], true)) { continue; }
            $active = in_array($request['status'], self::ACTIVE, true);
            [$label, $color] = $active || in_array($request['status'], ['expired', 'skipped'], true) ? Messages::taskStatus($request['status']) : ['Didn\'t start', 'danger'];
            $detail = match ($request['status']) {
                'pending' => $this->schedulerFresh() ? 'Starts within a minute.' : 'Waiting for Paymenter\'s cron job.',
                'deferred' => 'Retrying soon. ' . Messages::error($request['error']),
                'launching' => 'Starting.', 'running' => 'Running.',
                'expired' => 'Expired: Paymenter\'s cron job didn\'t run in time.',
                default => Messages::error($request['error']),
            };
            $rows[] = ['__key' => 'request-' . $request['request_id'], 'kind' => 'request', 'request_id' => $request['request_id'],
                'created_at' => $request['created_at'], 'type' => Messages::type($request['mode'] ?: ($this->configuration()['mode'] ?? null)), 'origin' => $request['scheduled'] ? 'Scheduled' : 'Manual',
                'size' => null, 'size_label' => '—', 'status_label' => $label, 'status_color' => $color, 'detail' => $detail, 'location' => '—',
                'cancel_request' => in_array($request['status'], ['pending', 'deferred'], true) ? $request['request_id'] : null,
                'run_id' => null, 'run' => null];
        }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'deleted' && !$showDeleted) { continue; }
            [$label, $color] = Messages::runStatus($run);
            $detail = Messages::runDetail($run + ['maintenance' => $this->overview()['maintenance'] ?? false]);
            $cancel = null;
            $tasks = $this->tasksFor($run['run_id']);
            $active = array_values(array_filter($tasks, static fn ($task) => in_array($task['status'], self::ACTIVE, true)));
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
                if ($latest && $latest['status'] === 'failed') { $detail = 'The last deletion didn\'t finish. ' . Messages::error($latest['error']); }
            }
            $origin = $run['request_id'] && isset($scheduled[$run['request_id']]) ? ($scheduled[$run['request_id']] ? 'Scheduled' : 'Manual') : null;
            $rows[] = ['__key' => $run['run_id'], 'kind' => 'run', 'request_id' => null, 'created_at' => $run['created_at'],
                'type' => Messages::type($run['mode']), 'origin' => $origin, 'size' => $run['size'], 'size_label' => $run['size'] === null ? 'Unknown' : Messages::bytes($run['size']), 'status_label' => $label, 'status_color' => $color,
                'detail' => $detail, 'location' => Messages::location($run, $uploads), 'cancel_request' => $cancel,
                'run_id' => $run['run_id'], 'run' => $run];
        }
        usort($rows, static fn ($a, $b) => ($b['created_at'] ?? 0) <=> ($a['created_at'] ?? 0));
        return $rows;
    }

    private function hasDeletedRuns(): bool
    {
        return array_filter($this->overview()['runs'], static fn ($run) => $run['status'] === 'deleted') !== [];
    }

    public function table(Table $table): Table
    {
        $isRun = static fn (array $record) => $record['kind'] === 'run';
        $inS3 = static fn (array $record) => in_array($record['run']['remote'] ?? null, ['delivered', 'present'], true);
        return $table
            ->heading('Backup history')
            ->records(function (int|string $page, int|string $recordsPerPage) {
                $rows = $this->historyRows($this->showDeleted);
                $perPage = max(1, (int) $recordsPerPage);
                $page = max(1, (int) $page);
                return new LengthAwarePaginator(array_slice($rows, ($page - 1) * $perPage, $perPage), count($rows), $perPage, $page);
            })
            ->paginated([10, 25, 50])
            // No polling while a dialog is open: it would re-render the dialog, including a shown recovery key.
            ->poll(fn () => $this->hasActiveWork() && !$this->mountedActions ? '5s' : null)
            // Below sm each row stacks, so long status details and both row actions get the full width.
            ->stackedOnMobile()
            ->headerActions([
                Action::make('toggleDeleted')->label(fn () => $this->showDeleted ? 'Hide deleted' : 'Show deleted')->link()->color('gray')
                    ->visible(fn () => $this->hasDeletedRuns())
                    ->action(function () { $this->showDeleted = !$this->showDeleted; $this->resetPage(); }),
            ])
            ->columns([
                TextColumn::make('created_at')->label(fn () => 'Created (' . $this->zone() . ')')
                    ->formatStateUsing(fn ($state) => $this->time($state))
                    ->description(fn (array $record) => $this->ago($record['created_at'])),
                // Below md these move to Details so the table stays readable.
                TextColumn::make('type')->label('Type')->description(fn (array $record) => $record['origin'])->visibleFrom('md'),
                TextColumn::make('size_label')->label('Size')->visibleFrom('md'),
                TextColumn::make('status_label')->label('Status')->badge()->color(fn (array $record) => $record['status_color'])
                    ->description(fn (array $record) => $record['detail'])->wrap(),
                TextColumn::make('location')->label('Stored on')->visibleFrom('lg'),
            ])
            ->recordActions([
                Action::make('download')->label('Download')->icon('ri-download-2-line')->button()->color('gray')
                    ->url(fn (array $record) => static::downloadUrl($record['run_id']))
                    ->visible(fn (array $record) => $isRun($record) && $record['run']['download'] === 'ready'),
                Action::make('prepareDownload')->label('Download from S3')->icon('ri-download-cloud-2-line')->button()->color('gray')
                    ->visible(fn (array $record) => $isRun($record) && $record['run']['download'] === 'retrieve'
                        && !$this->activeRequest('retrieve', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('retrieve', ['run_id' => $record['run_id']], 'Download from S3 queued',
                        'Downloading from S3 to this server. ' . $this->startLine() . ' Download appears here when it\'s ready and stays available for 24 hours.')),
                Action::make('verifyFirst')->label('Verify')->icon('ri-shield-check-line')->button()->color('gray')
                    ->tooltip('Checks the backup against its checksums.')
                    ->visible(fn (array $record) => $isRun($record) && in_array($record['run']['status'], ['ready', 'damaged'], true)
                        && !in_array($record['run']['download'], ['ready', 'retrieve'], true) && !$this->activeRequest('verify', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                Action::make('cancelQueued')->label('Cancel')->icon('ri-close-line')->button()->color('gray')
                    ->visible(fn (array $record) => $record['kind'] === 'request' && $record['cancel_request'] !== null)
                    ->requiresConfirmation()->modalHeading('Cancel this backup?')->modalDescription('It hasn\'t started. Scheduled backups still run.')
                    ->modalSubmitActionLabel('Cancel backup')->modalCancelActionLabel('Keep')
                    ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                ActionGroup::make([
                    Action::make('details')->label('Details')->icon('ri-information-line')
                        ->modalHeading('Backup details')->modalSubmitAction(false)->modalCancelActionLabel('Close')
                        ->schema(fn (array $record) => $this->detailsSchema($record)),
                    Action::make('verify')->label('Verify')->icon('ri-shield-check-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['run']['status'] === 'ready'
                            && ($record['run']['download'] === 'ready' || (!$record['run']['local'] && $inS3($record)))
                            && !$this->activeRequest('verify', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                    Action::make('copy')->label('Upload to S3')->icon('ri-upload-cloud-2-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['run']['status'] === 'ready' && !$inS3($record)
                            && $this->uploadsToS3() && !$this->activeRequest('deliver', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('deliver', ['run_id' => $record['run_id']], 'Upload to S3 queued')),
                    Action::make('cancelTask')->label(fn (array $record) => 'Cancel ' . strtolower($this->pendingTaskLabel($record)))->icon('ri-close-circle-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['cancel_request'] !== null)
                        ->requiresConfirmation()->modalHeading(fn (array $record) => 'Cancel ' . strtolower($this->pendingTaskLabel($record)) . '?')->modalDescription('It hasn\'t started.')
                        ->modalSubmitActionLabel('Cancel task')->modalCancelActionLabel('Keep')
                        ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                    Action::make('delete')->label('Delete')->icon('ri-delete-bin-line')->color('danger')
                        ->visible(fn (array $record) => $isRun($record) && in_array($record['run']['status'], ['ready', 'damaged', 'delete-interrupted'], true)
                            && !$this->activeRequest('delete', $record['run_id']))
                        ->requiresConfirmation()
                        ->modalHeading(fn (array $record) => 'Delete the ' . strtolower(Messages::mode($record['run']['mode'])) . ' from ' . $this->time($record['created_at']) . '?')
                        ->modalDescription('This can\'t be undone.')
                        ->fillForm(fn (array $record) => ['location' => $this->defaultDeletionLocation($record)])
                        ->schema(fn (array $record) => [
                            // A choice appears only when the backup is in more than one place.
                            Radio::make('location')->label('Delete from')->required()->live()
                                ->options($places = array_filter([
                                    'all' => $record['run']['local'] && $inS3($record) ? 'This server and S3' : null,
                                    'local' => $record['run']['local'] ? 'This server' : null,
                                    's3' => !$record['run']['local'] || $inS3($record) ? 'S3' : null,
                                ]))
                                ->visible(count($places) > 1)
                                ->rule('in:all,local,s3'),
                            Callout::make(fn () => $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal'] ? 'Can\'t delete' : 'What will be deleted')->warning()
                                ->footer(fn () => [$this->bullets($this->deleteLines($record['run_id'], $this->currentDeletionLocation($record)))]),
                        ])
                        ->modalSubmitAction(fn (Action $action, array $record) => $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal'] ? false : $action)
                        ->modalSubmitActionLabel('Delete')
                        ->action(fn (array $record, array $data) => $this->queue('delete', ['run_id' => $record['run_id'], 'location' => $data['location'] ?? $this->defaultDeletionLocation($record), 'execute' => true], 'Deletion queued')),
                ])->label('More actions')->icon('ri-more-2-fill')->color('gray')->tooltip('More actions'),
            ])
            ->emptyStateIcon('ri-save-line')
            ->emptyStateHeading('No backups yet')
            ->emptyStateDescription(fn () => $this->setupOpen() && $this->nothingCanStart() ? 'Finish setup above to create your first backup.' : 'Create a backup or turn on a schedule.')
            ->emptyStateActions([$this->backupAction('createFirstBackup')->visible(fn () => !$this->nothingCanStart())]);
    }

    private function pendingTaskLabel(array $record): string
    {
        foreach ($this->tasksFor($record['run_id'] ?? '') as $task) {
            if ($task['request_id'] === $record['cancel_request']) { return Messages::TASKS[$task['action']] ?? 'Task'; }
        }
        return 'Task';
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

    /** What a deletion removes and what stays, one fact per line. */
    private function deleteLines(string $runId, string $location = 'all'): array
    {
        $preview = $this->deletePreview($runId, $location);
        if ($preview['refusal']) { return [Messages::error($preview['refusal'])]; }
        $lines = [];
        if ($preview['local_copy']) { $lines[] = 'The backup file on this server.'; }
        if ($preview['remote_copy']) { $lines[] = 'The backup in S3 bucket ' . $preview['remote_copy']['bucket'] . '. If bucket versioning is on, old versions stay.'; }
        $lines[] = 'Another verified backup must remain in each place; otherwise nothing is deleted.';
        return $lines;
    }

    private function detailsSchema(array $record): array
    {
        if ($record['kind'] !== 'run') {
            return [
                TextEntry::make('d_task')->label('Task ID')->state($record['request_id'])->fontFamily(FontFamily::Mono)->copyable(request()->isSecure()),
                TextEntry::make('d_status')->label('Status')->state($record['status_label'] . '. ' . $record['detail']),
            ];
        }
        $run = $record['run'];
        $code = $run['error'] ?? $run['verification_error'] ?? $run['delivery_error'] ?? $run['retention_error'] ?? null;
        $format = Messages::remoteFormat($run['remote_format'] ?? null, $run['key_short'] ?? null);
        return [
            TextEntry::make('d_id')->label('Backup ID')->state($run['run_id'])->fontFamily(FontFamily::Mono)
                ->copyable(request()->isSecure())->copyMessage('Copied'),
            Grid::make(2)->schema([
                TextEntry::make('d_created')->label('Created')->state($this->time($run['created_at'], true)),
                TextEntry::make('d_type')->label('Type')->state(Messages::mode($run['mode']) . ($record['origin'] ? ', ' . strtolower($record['origin']) : '')),
                TextEntry::make('d_size')->label('Size')->state(Messages::bytes($run['size'])),
                TextEntry::make('d_location')->label('Stored on')->state($record['location']),
                TextEntry::make('d_remote_format')->label('S3 copy')->state($format . (($run['ciphertext_size'] ?? null) !== null ? ', ' . Messages::bytes($run['ciphertext_size']) : ''))->visible($format !== null),
                TextEntry::make('d_verified')->label('Last verified')->state($run['verified_at'] ? $this->time($run['verified_at'], true) . ($run['verification_source'] === 'capture' ? ' (at creation)' : '') : 'Not recorded'),
                TextEntry::make('d_status')->label('Status')->state($record['status_label']),
                TextEntry::make('d_retrieved')->label('Downloaded copy kept until')->state(fn () => $this->time($run['local_download_until'] ?? null, true))
                    ->visible((bool) ($run['local_download_until'] ?? null)),
            ]),
            TextEntry::make('d_detail')->label('Details')->state($record['detail'])->visible((bool) $record['detail']),
            TextEntry::make('d_code')->label('Error code')->state($code)->fontFamily(FontFamily::Mono)->visible((bool) $code),
        ];
    }
}
