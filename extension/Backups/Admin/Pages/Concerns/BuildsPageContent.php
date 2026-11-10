<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\Component;
use Filament\Schemas\Components\EmbeddedSchema;
use Filament\Schemas\Components\EmbeddedTable;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Components\Tabs;
use Filament\Schemas\Components\Tabs\Tab;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Schema;
use Filament\Support\Enums\FontWeight;
use Filament\Support\Enums\Size;
use Illuminate\Contracts\Support\Htmlable;
use Illuminate\Support\HtmlString;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Admin\Support\Sheet;

trait BuildsPageContent
{
    /** One setup row: title, explanation, exact fixes and the existing actions. */
    private function setupItem(array $item, string $prefix): Component
    {
        $children = [];
        foreach ($item['fixes'] as $fix) {
            if (filled($fix['lead'] ?? null)) { $children[] = Text::make($fix['lead']); }
            $children[] = $this->command($fix['code']);
        }
        $actions = [];
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
        }
        return Sheet::row(TextEntry::make($prefix . '_' . $item['code'])->label($item['title'])
            ->state($item['text'])->belowContent([Group::make($children)->grow()->maxWidth('full')]), $actions);
    }

    public function content(Schema $schema): Schema
    {
        return $schema->components([
            Callout::make('Settings save needs attention')
                ->warning()
                ->description(fn () => Messages::withWriterCause(
                    Messages::error($this->overviewError), $this->overviewErrorCause
                ))
                ->visible(fn () => $this->overview() && $this->overviewError !== null
                    && $this->overviewErrorCause !== null && $this->privateRootProblem() === null),
            Callout::make('Backups can\'t start')
                ->danger()
                ->description(fn () => Messages::error($this->overviewError) . ' To see details, run these as the site user:')
                ->footer([Group::make([$this->command(fn () => $this->terminalFunction()), $this->command('paymenter_backups overview --offline')])])
                ->visible(fn () => $this->overview() && $this->overviewError !== null
                    && $this->overviewErrorCause === null && $this->privateRootProblem() === null),
            Tabs::make('Backups')
                ->contained(false)
                ->persistTabInQueryString('tab')
                ->tabs([
                    Tab::make('Backups')->id('backups')->schema([
                        Sheet::split([
                            $this->setupSection(),
                            ...$this->alertCallouts(),
                            $this->scheduleSection(),
                            $this->storageSection(),
                        ], [$this->checksSection()]),
                        EmbeddedTable::make(),
                    ]),
                    Tab::make('Settings')->id('settings')->schema([
                        EmbeddedSchema::make('settingsForm'),
                    ]),
                    Tab::make('Help')->id('help')->schema([Sheet::make($this->helpSections())]),
                ]),
        ]);
    }

    /** One line under the title: how backups are doing, then the next thing to know. */
    public function getSubheading(): string|Htmlable|null
    {
        $this->overview();
        if ($this->overviewError !== null) { return Sheet::headLine('danger', 'Backups can\'t start', 'See below.'); }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'running') {
                return Sheet::headLine('info', Messages::mode($run['mode']) . ' running', 'Started ' . $this->ago($run['created_at']) . '. The site stays online.');
            }
        }
        if (!$this->initialized()) { return Sheet::headLine('warning', 'Setup needed', 'Save your settings to finish setup.'); }
        if (!$this->schedulerFresh()) {
            $fix = ' <a class="underline" href="#setup">See the fix</a>';
            return Sheet::headLine('warning', 'Backups waiting', new HtmlString(e($this->lastTick()
                ? 'Paymenter\'s cron job stopped ' . $this->ago($this->lastTick()) . '.' : 'Paymenter\'s cron job hasn\'t run yet.') . $fix));
        }
        if ($this->nothingCanStart()) {
            return Sheet::headLine('warning', 'Setup needed', ($this->blockers('database-online')[0]['title'] ?? 'Finish setup') . '. See Checks.');
        }
        $newest = null;
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] !== 'deleted' && ($newest === null || $run['created_at'] > $newest['created_at'])) { $newest = $run; }
        }
        if ($newest !== null && in_array($newest['status'], ['failed', 'interrupted', 'attention', 'damaged'], true)) {
            return Sheet::headLine('danger', 'Last backup failed', new HtmlString(e(Messages::mode($newest['mode']) . ' from ') . $this->moment($newest['created_at']) . '. Its Details say why.'));
        }
        $good = array_filter([$this->lastGood('full-quiesced'), $this->lastGood('database-online')]);
        if (!$good) { return Sheet::headLine('gray', 'No backups yet', 'Create a backup or turn on a schedule.'); }
        $last = max(array_map(static fn ($run) => $run['created_at'], $good));
        return Sheet::headLine('success', 'Backups OK', new HtmlString('Last backup ' . $this->moment($last) . '.'));
    }

    /** Every blocker with its exact fix. Only Paymenter's cron job missing gets its own title. */
    private function setupSection(): Section
    {
        $cronOnly = function () {
            $items = array_merge(...array_values($this->setupGroups()));
            return $items && !array_filter($items, static fn ($item) => !in_array($item['code'], self::CRON_CODES, true));
        };
        return Sheet::section(fn () => $cronOnly() ? 'Fix the cron job' : 'Setup needed')->id('setup')
            ->schema(function () {
                $groups = $this->setupGroups();
                $blocks = array_map(fn ($item) => $this->setupItem($item, 'setupAll'), $groups['all']);
                foreach (self::KIND_LABELS as $mode => $label) {
                    if (!$groups[$mode]) { continue; }
                    $blocks[] = Group::make([Text::make($label . ' only')->weight(FontWeight::SemiBold)->extraAttributes(['class' => 'backups-group-title']),
                        ...array_map(fn ($item) => $this->setupItem($item, 'setup' . ucfirst(self::KINDS[$mode])), $groups[$mode])]);
                }
                return $blocks;
            })
            ->visible(fn () => $this->setupOpen());
    }

    /** Saved recurrence in the staff timezone, anchored to the next scheduled instant. */
    private function scheduleSentence(string $mode): string|HtmlString
    {
        $values = $this->formValues();
        $kind = self::KINDS[$mode];
        $frequency = $values[$kind . '_schedule'] ?? 'off';
        $zone = ' ' . ($values['schedule_timezone'] ?? 'UTC');
        $days = array_map(static fn ($day) => Messages::WEEKDAYS[$day] ?? '', (array) ($values[$kind . '_weekdays'] ?? []));
        $fallback = match ($frequency) {
            'hourly' => ((int) ($values['database_every_hours'] ?? 1) === 1 ? 'Every hour' : 'Every ' . (int) $values['database_every_hours'] . ' hours')
                . ' at :' . str_pad((string) (int) ($values['database_minute'] ?? 0), 2, '0', STR_PAD_LEFT) . $zone,
            'daily' => 'Daily at ' . ($values[$kind . '_time'] ?? '') . $zone,
            'weekly' => 'Weekly, ' . implode(', ', $days) . ' at ' . ($values[$kind . '_time'] ?? '') . $zone,
            default => 'Off',
        };
        if ($frequency === 'off') { return $fallback; }
        $at = $this->scheduleFor($mode)['next_at'] ?? null;
        if ($at === null) { return $fallback; }
        $instant = \Carbon\CarbonImmutable::createFromTimestamp((int) $at, trim($zone));
        $instants = [$instant->toIso8601String()];
        if ($frequency === 'weekly') {
            $monday = $instant->startOfWeek(\Carbon\CarbonInterface::MONDAY);
            $instants = array_map(static fn ($day) => $monday->addDays((int) $day)
                ->setTimeFromTimeString($values[$kind . '_time'])->toIso8601String(), (array) $values[$kind . '_weekdays']);
        }
        return new HtmlString(trim(view('backups::partials.schedule', [
            'fallback' => $fallback, 'frequency' => $frequency, 'instants' => $instants,
            'every' => (int) ($values['database_every_hours'] ?? 1),
        ])->render()));
    }

    private function scheduleSection(): Section
    {
        $next = function (string $mode) {
            if (($this->scheduleFor($mode)['frequency'] ?? 'off') === 'off') { return [Text::make('Create these backups by hand, or turn on a schedule.')]; }
            $at = $this->scheduleFor($mode)['next_at'] ?? null;
            return [Text::make($at ? new HtmlString('Next ' . $this->moment($at) . ($this->schedulerFresh() ? '' : ', once the cron job runs')) : 'None in the next 8 days')];
        };
        $row = fn (string $mode) => Sheet::row(Sheet::hint(TextEntry::make('schedule_' . self::KINDS[$mode])->label(self::KIND_LABELS[$mode]), Messages::TYPE_HINTS[$mode])
            ->state(fn () => $this->scheduleSentence($mode))->belowContent(fn () => $next($mode)),
            [$this->link('schedule' . ucfirst(self::KINDS[$mode]) . 'Change', 'Change', $this->settingsUrl('schedule'))]);
        return Sheet::section('Schedule')->schema([$row('full-quiesced'), $row('database-online')]);
    }

    private function storageSection(): Section
    {
        $count = fn () => count(array_filter($this->overview()['runs'], static fn ($run) => $run['local']));
        $plural = static fn (int $n, string $word) => $n . ' ' . $word . ($n === 1 ? '' : 's');
        return Sheet::section('Storage')->schema([
            Sheet::row(TextEntry::make('storage_local')->label('This server')->state(function () {
                if (!($this->storage()['keep_local'] ?? true)) { return 'Off'; }
                $space = $this->configuration()['space'] ?? null;
                if (!$space || $space['used'] === null) { return 'Nothing stored yet'; }
                return Messages::bytes($space['used']) . ' of ' . Messages::bytes($space['budget']) . ' used' . (($space['state'] ?? null) === 'full' ? '. Full.' : '');
            })->belowContent(fn () => [Text::make(($this->storage()['keep_local'] ?? true)
                ? $plural($count(), 'backup') : 'Each backup is deleted here once its S3 copy is verified.')]),
                [$this->testStorageAction()->button()->outlined()->size(Size::Small)]),
            Sheet::row(TextEntry::make('storage_s3')->label('S3')->state(fn () => $this->uploadsToS3() ? ($this->storage()['s3_bucket'] ?: 'No bucket') : 'Off')
                ->belowContent(fn () => [Text::make($this->s3Sentence())]),
                [$this->link('storageS3', 'Set up S3', $this->settingsUrl('s3'))->label(fn () => $this->uploadsToS3() ? 'Change' : 'Set up S3')]),
            Sheet::row(Sheet::hint(TextEntry::make('storage_keep')->label('Keep'),
                'The newest verified backup in each place is always kept. So are backups not yet uploaded and backups that failed a check.')->state(function () {
                $values = $this->formValues();
                return 'Newest ' . (int) ($values['retention_keep_full'] ?? 0) . ' full and ' . (int) ($values['retention_keep_database'] ?? 0) . ' database backups';
            })->belowContent(fn () => [Text::make(($this->formValues()['automatic_retention'] ?? true) ? 'Cleanup runs after each backup.' : 'Automatic cleanup is off.')]),
                [$this->applyRetentionAction()->button()->outlined()->size(Size::Small)]),
        ]);
    }

    /** What the S3 copy needs, or when it was last made. */
    private function s3Sentence(): string|HtmlString
    {
        if (!$this->uploadsToS3()) { return 'Optional. Add a bucket to keep a copy off this server.'; }
        if (!$this->savedS3Keys()) { return 'Keys missing. Uploads start once keys are saved in Settings.'; }
        $pending = (int) ($this->overview()['s3_pending']['count'] ?? 0);
        $parts = [];
        if ($this->encryption()['enabled'] ?? false) {
            $parts[] = $this->pendingKeyShort() !== null ? 'Encrypted once the recovery key is saved.' : 'Encrypted.';
        } else {
            $parts[] = 'Not encrypted.';
        }
        $run = $this->run($this->overview()['last_good']['s3'] ?? null);
        if ($run) {
            $parts[] = 'Last upload ' . $this->moment($run['created_at']) . e($run['remote_checked_at'] ? ', checked ' . $this->ago($run['remote_checked_at']) . '.' : '.');
        }
        if ($pending) { $parts[] = e($pending . ($pending === 1 ? ' backup' : ' backups') . ' not uploaded yet.'); }
        return new HtmlString(implode(' ', $parts));
    }

    private function checksSection(): Section
    {
        return Sheet::section(Messages::INSTALLATION_TITLE)->id('checks')
            ->afterHeader([Text::make(function () {
                $steps = $this->checkSteps();
                return count(array_filter($steps, static fn ($step) => $step['color'] === 'success')) . ' of ' . count($steps) . ' OK';
            })->color('gray')])
            ->schema(fn () => [Sheet::steps($this->checkSteps(), false)]);
    }

    /** The rail: each check with a state word and, unless it's fine, why and what to do. */
    private function checkSteps(): array
    {
        $fix = ' <a class="underline" href="#setup">See the fix</a>';
        $last = $this->lastTick();
        $steps = [match (true) {
            $last === null => ['label' => 'Cron job', 'color' => 'warning', 'value' => 'Not running', 'detail' => new HtmlString('It hasn\'t run yet.' . $fix)],
            $this->schedulerFresh() => ['label' => 'Cron job', 'color' => 'success', 'value' => 'OK', 'detail' => 'Last run ' . $this->ago($last)],
            default => ['label' => 'Cron job', 'color' => 'warning', 'value' => 'Not running', 'detail' => new HtmlString(e('Last run ' . $this->ago($last) . '.') . $fix)],
        }];
        if ($this->overviewError !== null) { return $steps; }
        foreach (['full-quiesced', 'database-online'] as $mode) {
            $label = $mode === 'full-quiesced'
                ? new HtmlString('<a class="underline" href="' . e($this->settingsUrl('full-backup-checks')) . '">' . e(self::KIND_LABELS[$mode]) . '</a>')
                : self::KIND_LABELS[$mode];
            $steps[] = ['label' => $label] + $this->typeCheck($mode);
        }
        $test = $this->latestRequest('destination-test');
        $steps[] = ['label' => 'Storage test'] + match ($this->storageTestState()) {
            'passed' => ['color' => 'success', 'value' => 'OK', 'detail' => new HtmlString('Passed ' . $this->moment($test['finished_at'] ?? null))],
            'stale' => ['color' => 'warning', 'value' => 'Test again', 'detail' => 'Settings changed since the last test.'],
            'failed' => ['color' => 'danger', 'value' => 'Failed', 'detail' => Messages::error($test['error'] ?? null)],
            'queued' => ['color' => 'info', 'value' => 'Queued', 'detail' => null],
            default => ['color' => 'gray', 'value' => 'Not tested', 'detail' => 'Use Test storage beside This server.'],
        };
        $checks = $this->configuration()['installation']['checks'] ?? [];
        foreach (['observer', 'tools', 'private_state', 'local_storage', 'offsite'] as $key) {
            if (!isset($checks[$key])) { continue; }
            [$word, $color, $next] = Messages::installationState($key, $checks[$key]);
            if ($key === 'local_storage' && $color === 'success') { continue; }
            if ($key === 'offsite' && $color === 'success' && ($short = $this->keyInfo()['short'])) { $next = 'Uploads encrypted with recovery key ' . $short; }
            $steps[] = ['label' => Messages::INSTALLATION_CHECKS[$key], 'color' => $color, 'value' => $word, 'detail' => $color === 'success' && $key !== 'offsite' ? null : $next];
        }
        if ($this->uploadsToS3() && !$this->savedS3Keys()) {
            $steps[] = ['label' => 'S3 access keys', 'color' => 'danger', 'value' => 'Missing', 'detail' => new HtmlString('Uploads to S3 can\'t start. <a class="underline" href="' . e($this->settingsUrl('s3')) . '">Add keys in Settings</a>')];
        }
        if ($this->pendingKeyShort() !== null) {
            $steps[] = ['label' => 'Recovery key', 'color' => 'warning', 'value' => 'Not saved', 'detail' => new HtmlString('Uploads start after you save it outside this server.' . $fix)];
        }
        return $steps;
    }

    /** A backup type's state for the rail. */
    private function typeCheck(string $mode): array
    {
        [, $color] = $this->typeStatus($mode);
        $blockers = $this->blockers($mode);
        if ($blockers) {
            return ['color' => $color, 'value' => 'Not ready', 'detail' => implode('. ', array_unique(array_map(static fn ($item) => $item['title'], $blockers))) . '.'];
        }
        if ($color === 'info') {
            $waiting = array_values(array_filter($this->readiness($mode)['items'], static fn ($item) => in_array($item['code'], self::TRANSIENT, true)));
            return ['color' => 'info', 'value' => 'Waiting', 'detail' => $waiting ? Messages::error($waiting[0]['code']) : null];
        }
        return $color === 'gray' ? ['color' => 'gray', 'value' => 'Off', 'detail' => 'No schedule.'] : ['color' => 'success', 'value' => 'OK', 'detail' => null];
    }

    /** Transient conditions, one callout each. */
    private function alertCallouts(): array
    {
        $pending = fn () => (int) ($this->overview()['s3_pending']['count'] ?? 0);
        return [
            Text::make('Database backups are running without job locks and can overlap Paymenter jobs.')
                ->visible(fn () => ($this->overview()['database_job_coordination']['state'] ?? null) === 'absent'),
            Callout::make(Messages::JOB_COORDINATION_UNVERIFIED)->warning()
                ->description(fn () => Messages::error($this->overview()['database_job_coordination']['error'] ?? null))
                ->visible(fn () => ($this->overview()['database_job_coordination']['state'] ?? null) === 'unverified'),
            Callout::make('Database backups have waited a day for Paymenter jobs')->warning()
                ->description('Check scheduler and queue jobs and their lock setup on this tab. Waiting backups retry automatically. If none is waiting, queue a database backup. Backups cannot safely stop a job that may be changing data or guess which locks protect it.')
                ->visible(fn () => (bool) ($this->overview()['job_wait_overdue'] ?? false)),
            Callout::make('Backups in S3 aren\'t encrypted')->info()
                ->description('Save settings to turn on encryption for new uploads. Backups already in S3 stay as they are.')
                ->actions([$this->link('alertEncryption', 'Go to S3 settings', $this->settingsUrl('s3'))])
                ->visible(fn () => $this->overviewError === null && $this->settingsVersion() < 2 && $this->uploadsToS3()
                    && !($this->encryption()['enabled'] ?? true)),
            Callout::make(fn () => $pending() . ($pending() === 1 ? ' backup isn\'t' : ' backups aren\'t') . ' uploaded to S3 yet')->warning()
                ->description('They\'re kept on this server. Use Upload to S3 in each row\'s menu.')
                ->visible(fn () => $pending() > 0),
            Callout::make('Backup space is full')->danger()
                ->description(fn () => 'Backups on this server reached the ' . Messages::bytes($this->configuration()['space']['budget'] ?? null) . ' limit. New backups stop until you delete old ones or raise the limit.')
                ->actions([$this->link('alertSpace', 'Change space limit', $this->settingsUrl('storage'))])
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
}
