<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Infolists\Components\TextEntry;
use Filament\Actions\Action;
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
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Admin\Support\Sheet;
use Paymenter\Extensions\Others\Backups\Panel\{Head, Row, Trace, Value};
use Paymenter\Extensions\Others\Backups\Panel\Page\Section as PanelSection;
use Paymenter\Extensions\Others\Backups\Panel\Presentation\{Fact, Plain, Reader, StaffTime, State, Tone};
use Paymenter\Extensions\Others\Backups\Panel\Support\Sheet as PanelSheet;

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
        $testing = $action && $action['kind'] === 'test-storage' && $this->activeRequest('destination-test') !== null;
        if ($testing && $this->schedulerFresh()) {
            $children[] = Text::make('A storage test is already queued. Wait for it to finish before testing again.')
                ->extraAttributes(['id' => $prefix . '-storage-test-reason']);
        }
        if ($action) {
            $name = $prefix . preg_replace('/[^A-Za-z0-9]/', '', ucwords($item['code'], '-'));
            $actions = match ($action['kind']) {
                'settings' => [$this->link($name, $action['label'], $this->settingsUrl($action['target'] ?? 'settings'))],
                'help' => [$this->link($name, $action['label'], static::getUrl(['tab' => 'help']) . '#' . ($action['target'] ?? 'help'))],
                'test-storage' => !$this->schedulerFresh() ? [] : ($testing
                    ? [Action::make($name)->label('Test storage')->button()->size(Size::Small)->color('gray')
                        ->alpineClickHandler('$event.preventDefault()')->livewireClickHandlerEnabled(false)
                        ->extraAttributes(['aria-disabled' => 'true', 'aria-describedby' => $prefix . '-storage-test-reason', 'class' => 'domains-panel-blocked'])]
                    : [$this->testStorageAction()->button()->size(Size::Small)]),
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
                        PanelSheet::split([
                            $this->setupSection(),
                            // Each section is its own sheet column, so the sheet draws the line between them.
                            Group::make(fn () => [$this->scheduleSection()->toSchema()]),
                            Group::make(fn () => [$this->storageSection()->toSchema()]),
                        ], [Group::make(fn () => [$this->checksSection()->toSchema()->id('checks')])], 'sheet'),
                        EmbeddedTable::make(),
                    ]),
                    Tab::make('Settings')->id('settings')->schema([
                        EmbeddedSchema::make('settingsForm'),
                    ]),
                    Tab::make('Help')->id('help')->schema([Sheet::make($this->helpSections())]),
                ]),
        ]);
    }

    public function getHeading(): string|Htmlable
    {
        return $this->head()->heading();
    }

    /** One line under the title: the state of backups on this server, then one fact or link. A running backup is a job,
     * so it gets the first row of Schedule, not the head. */
    public function getSubheading(): string|Htmlable|null
    {
        return $this->head()->line();
    }

    private function head(): Head
    {
        $head = Head::make('Backups', Reader::Staff);
        $this->overview();
        if ($this->overviewError !== null) { return $head->state(State::Failed, 'backups'); }
        if (!$this->initialized()) { return $head->state(State::NotSet, 'backups')->link('Open Settings', $this->settingsUrl()); }
        $good = array_filter([$this->lastGood('full-quiesced'), $this->lastGood('database-online')]);
        $last = $good ? Fact::date((int) max(array_map(static fn ($run) => $run['created_at'], $good)), Reader::Staff) : null;
        // A failure outranks every amber state below, and its fix is the head's link.
        if (($failure = $this->failure()) !== null) {
            $head->state(State::Failed, 'backups');
            $at = array_key_exists('at', $failure) ? $failure['at'] : $last;
            if ($at !== null) { $head->fact('Last backup', $at); }
            return isset($failure['link']) ? $head->link(...$failure['link']) : $head;
        }
        if (!$this->schedulerFresh()) {
            $head->state(State::NotRunning, 'backups');
            if ($this->lastTick() !== null) { $head->fact('Cron job last ran', Fact::date((int) $this->lastTick(), Reader::Staff)); }
            return $head->link('See the fix', '#setup');
        }
        if ($this->nothingCanStart()) { return $head->state(State::NeedsReview, 'backups')->link('See the fix', '#setup'); }
        if (($worst = $this->worstState()) !== null) {
            $head->state($worst, 'backups');
            return $last === null ? $head : $head->fact('Last backup', $last);
        }
        if ($last === null) { return $head->link('Turn on a schedule', $this->settingsUrl('schedule')); }
        return $head->state(State::Ok, 'backups')->fact('Last backup', $last);
    }

    /** The first failure staff must fix, with the link to its fix: a full disk, then the newest backup failing, then a
     * failed row or check. Null when nothing failed. */
    private function failure(): ?array
    {
        if ($this->spaceValue()?->state?->tone(Reader::Staff) === Tone::Failed) {
            return ['link' => ['Change space limit', $this->settingsUrl('storage')]];
        }
        $newest = null;
        foreach ($this->overview()['runs'] as $run) {
            if (!in_array($run['status'], ['deleted', 'running'], true) && ($newest === null || $run['created_at'] > $newest['created_at'])) { $newest = $run; }
        }
        if ($newest !== null && in_array($newest['status'], ['failed', 'interrupted', 'attention', 'damaged'], true)) {
            return ['at' => $newest['created_at'] === null ? null : Fact::date((int) $newest['created_at'], Reader::Staff)];
        }
        if ($this->worstState(Tone::Failed) !== null) { return ['link' => ['See the fix', '#checks']]; }
        return null;
    }

    /** The worst state a Schedule or Storage row, the Storage notice or a check shows, so the head never reads OK over
     * a problem. A job (Running, Queued) keeps its own row and never sets the head. */
    private function worstState(Tone ...$tones): ?State
    {
        $storage = $this->storageValues();
        $states = [$this->spaceValue()?->state, $this->scheduleValue('full-quiesced')->state, $this->scheduleValue('database-online')->state,
            $this->lockValue()?->state, $storage['server']->state, $storage['s3']->state, $storage['keep']->state,
            ...array_column($this->checkSteps(), 'state')];
        foreach ($tones ?: [Tone::Failed, Tone::Attention] as $tone) {
            foreach ($states as $state) { if ($state !== null && $state->tone(Reader::Staff) === $tone) { return $state; } }
        }
        return null;
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

    /** How a saved schedule repeats. With a next run it reads in the staff time zone, like every other time on the page,
     * taking the clock time and the day shift from that run. Without one it reads in the schedule's own zone. */
    private function scheduleRecurrence(string $mode, ?int $at = null): string
    {
        $values = $this->formValues();
        $kind = self::KINDS[$mode];
        $ownZone = (string) ($values['schedule_timezone'] ?? 'UTC');
        $zone = ' ' . $ownZone;
        $time = (string) ($values[$kind . '_time'] ?? '');
        $minute = (int) ($values['database_minute'] ?? 0);
        $shift = 0;
        if ($at !== null && in_array($ownZone, \DateTimeZone::listIdentifiers(), true)) {
            $local = StaffTime::local($at);
            $own = (new \DateTimeImmutable('@' . $at))->setTimezone(new \DateTimeZone($ownZone));
            // -1, 0 or 1: the run's calendar day in the staff zone against its day in the schedule's zone.
            $shift = (int) round((strtotime($local->format('Y-m-d') . ' UTC') - strtotime($own->format('Y-m-d') . ' UTC')) / 86400);
            $time = $local->format('g:i A');
            $minute = (int) $local->format('i');
            $zone = ' ' . $local->format('T');
        }
        $days = array_map(static fn ($day) => is_numeric($day) ? (Messages::WEEKDAYS[((int) $day + $shift + 7) % 7] ?? '') : '',
            (array) ($values[$kind . '_weekdays'] ?? []));
        return match ($values[$kind . '_schedule'] ?? 'off') {
            'hourly' => ((int) ($values['database_every_hours'] ?? 1) === 1 ? 'Every hour' : 'Every ' . (int) $values['database_every_hours'] . ' hours')
                . ' at :' . str_pad((string) $minute, 2, '0', STR_PAD_LEFT) . $zone,
            'daily' => 'Daily at ' . $time . $zone,
            'weekly' => 'Weekly, ' . implode(', ', $days) . ' at ' . $time . $zone,
            default => 'Off',
        };
    }

    /** A running backup first, as a job row; else the maintenance hold on queued tasks. Facts staff must act on live
     * on their own rows (Database backups, Lock files), so this one notice never hides them. */
    private function scheduleNotice(): ?Row
    {
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'running') {
                $value = Value::state(State::Running, Reader::Staff, Messages::mode($run['mode']) . ' running. The site stays online.', 'run-' . $run['run_id']);
                return Row::notice('running', $run['created_at'] === null ? $value : $value->when((int) $run['created_at']));
            }
        }
        return ($this->overview()['maintenance'] ?? false)
            ? Row::notice('maintenance', Value::state(State::Queued, Reader::Staff, 'The site is in maintenance mode. Queued tasks wait until it\'s back online.')) : null;
    }

    private function lockFilesRow(): ?Row
    {
        $value = $this->lockValue();
        return $value === null ? null : Row::make('Lock files', $value);
    }

    /** Job locks that need staff: unverified or absent. Null when they are fine. */
    private function lockValue(): ?Value
    {
        $coordination = $this->overview()['database_job_coordination'] ?? [];
        $reason = match ($coordination['state'] ?? null) {
            'unverified' => Messages::JOB_COORDINATION_UNVERIFIED . '. ' . Messages::error($coordination['error'] ?? null),
            'absent' => 'Database backups are running without job locks and can overlap Paymenter jobs.',
            default => null,
        };
        return $reason === null ? null : Value::state(State::NeedsReview, Reader::Staff, $reason, 'job-locks');
    }

    private function scheduleValue(string $mode): Value
    {
        $at = $this->scheduleFor($mode)['next_at'] ?? null;
        $recurrence = $this->scheduleRecurrence($mode, $at === null ? null : (int) $at);
        $value = match (true) {
            // Capture stalled for a day: the row says so, with the recovery steps, until a backup runs.
            $mode === 'database-online' && (bool) ($this->overview()['job_wait_overdue'] ?? false) => Value::state(State::NeedsReview, Reader::Staff,
                'Database backups have waited a day for Paymenter jobs. Check scheduler and queue jobs and their lock setup on this tab. Waiting backups retry automatically. If none is waiting, queue a database backup. Backups cannot safely stop a job that may be changing data or guess which locks protect it.', 'job-wait'),
            ($this->scheduleFor($mode)['frequency'] ?? 'off') === 'off' => Value::plain(Plain::Off, 'Create these backups by hand, or turn on a schedule.'),
            $at === null => Value::plain(Plain::None, $recurrence . '. None in the next 8 days.'),
            default => Value::fact(Fact::date((int) $at, Reader::Staff), $recurrence . ($this->schedulerFresh() ? '.' : ', once the cron job runs.')),
        };
        if ($value->state !== null && $at !== null) { $value = $value->detail(Fact::date((int) $at, Reader::Staff)); }
        return $value;
    }

    private function scheduleSection(): PanelSection
    {
        $row = function (string $mode): Row {
            $value = $this->scheduleValue($mode);
            return Row::make(self::KIND_LABELS[$mode], $value)->help(Messages::TYPE_HINTS[$mode])
                ->actions($this->link('schedule' . ucfirst(self::KINDS[$mode]) . 'Change', 'Change', $this->settingsUrl('schedule')));
        };
        return PanelSection::make('Schedule', array_values(array_filter([$this->scheduleNotice(), $row('full-quiesced'), $row('database-online'), $this->lockFilesRow()])));
    }

    /** A full disk stops every backup, so it is the Storage notice. Uploads waiting, plain S3 copies and skipped
     * cleanup each show on their own row, so this notice never hides them. */
    private function storageNotice(): ?Row
    {
        $value = $this->spaceValue();
        return $value === null ? null : Row::notice('space', $value, $this->link('alertSpace', 'Change space limit', $this->settingsUrl('storage')));
    }

    private function spaceValue(): ?Value
    {
        if (($this->configuration()['space']['state'] ?? null) !== 'full') { return null; }
        return Value::state(State::Failed, Reader::Staff, 'Backups on this server reached the ' . Messages::bytes($this->configuration()['space']['budget'] ?? null)
            . ' limit. New backups stop until you delete old ones or raise the limit.');
    }

    /** Legacy settings that upload to S3 without encryption. */
    private function plainS3(): bool
    {
        return $this->overviewError === null && $this->settingsVersion() < 2 && $this->uploadsToS3() && !($this->encryption()['enabled'] ?? true);
    }

    /** @return array{server: Value, s3: Value, keep: Value} */
    private function storageValues(): array
    {
        $count = count(array_filter($this->overview()['runs'], static fn ($run) => $run['local']));
        $pending = (int) ($this->overview()['s3_pending']['count'] ?? 0);
        $local = (bool) ($this->storage()['keep_local'] ?? true);
        $space = $this->configuration()['space'] ?? null;
        $used = $space && $space['used'] !== null ? Fact::names([Messages::bytes($space['used']) . ' of ' . Messages::bytes($space['budget']) . ' used']) : null;
        $server = match (true) {
            // Waiting uploads are kept here whatever the setting, until their S3 copy is made.
            $pending > 0 => Value::state(State::NeedsReview, Reader::Staff, $pending . ($pending === 1 ? ' backup isn\'t' : ' backups aren\'t')
                . ' uploaded to S3 yet. They\'re kept on this server. Use Upload to S3 in each row\'s menu.', 's3-pending'),
            !$local => Value::plain(Plain::Off, 'Each backup is deleted here once its S3 copy is verified.'),
            $used === null => Value::plain(Plain::None, 'No backups are stored here yet.'),
            default => Value::fact($used, $count . ' ' . ($count === 1 ? 'backup' : 'backups') . ' here.'),
        };
        if ($server->state !== null && $used !== null) { $server = $server->detail($used); }
        $bucket = $this->storage()['s3_bucket'] ?? null;
        $s3 = match (true) {
            !$this->uploadsToS3() => Value::plain(Plain::Off, 'Optional. Add a bucket to keep a copy off this server.'),
            !filled($bucket) => Value::state(State::NotSet, Reader::Staff, 'Add a bucket in Settings. ' . $this->s3Sentence()),
            $this->plainS3() => Value::state(State::NeedsReview, Reader::Staff, 'Backups in S3 aren\'t encrypted. Save settings to turn on encryption for new uploads. Backups already in S3 stay as they are.', 's3-encryption')
                ->detail(Fact::names([$bucket])),
            default => Value::fact(Fact::names([$bucket]), $this->s3Sentence()),
        };
        $values = $this->formValues();
        $keep = Fact::names([$this->settingsVersion() < 2
            ? 'Newest ' . (int) ($values['retention_keep_full'] ?? 0) . ' backups'
            : 'Newest ' . (int) ($values['retention_keep_full'] ?? 0) . ' full and ' . (int) ($values['retention_keep_database'] ?? 0) . ' database backups']);
        $skipped = $this->skippedCount();
        $keepValue = $skipped > 0
            ? Value::state(State::NeedsReview, Reader::Staff, 'Cleanup skipped ' . $skipped . ($skipped === 1 ? ' backup' : ' backups') . '. They weren\'t deleted. Their rows say why.', 'cleanup-skipped')->detail($keep)
            : Value::fact($keep, ($values['automatic_retention'] ?? true) ? 'Cleanup runs after each backup.' : 'Automatic cleanup is off.');
        return ['server' => $server, 's3' => $s3, 'keep' => $keepValue];
    }

    private function storageSection(): PanelSection
    {
        ['server' => $server, 's3' => $s3, 'keep' => $keepValue] = $this->storageValues();
        $keepRow = Row::make('Keep', $keepValue)->actions($this->applyRetentionAction()->button()->outlined()->size(Size::Small));
        if (array_sum($this->retentionPending()) === 0) { $keepRow->blocked('Nothing to delete yet.'); }
        $serverRow = Row::make('This server', $server)->actions($this->testStorageAction()->button()->outlined()->size(Size::Small));
        if ($this->activeRequest('destination-test') !== null) { $serverRow->blocked('A storage test is already queued. Wait for it to finish before testing again.'); }
        $s3Link = $this->plainS3() ? $this->link('alertEncryption', 'Go to S3 settings', $this->settingsUrl('s3'))
            : $this->link('storageS3', $this->uploadsToS3() ? 'Change' : 'Set up S3', $this->settingsUrl('s3'));
        return PanelSection::make('Storage', array_values(array_filter([
            $this->storageNotice(),
            $serverRow,
            Row::make('S3', $s3)->actions($s3Link),
            $this->keepHint() === '' ? $keepRow : $keepRow->help($this->keepHint()),
        ])));
    }

    /** What cleanup never deletes, named only for the places that keep copies. */
    private function keepHint(): string
    {
        $local = (bool) ($this->storage()['keep_local'] ?? true);
        $s3 = $this->uploadsToS3();
        if (!$local && !$s3) { return ''; }
        $places = implode(' and ', array_filter([$local ? 'on this server' : null, $s3 ? 'in S3' : null]));
        // With S3 on, every upload is required, and cleanup keeps a backup until its S3 copy is made.
        return 'The newest verified backup' . ($this->settingsVersion() < 2 ? '' : ' of each type') . ' ' . $places . ' is always kept. So are '
            . ($s3 ? 'backups waiting for their S3 upload and ' : '') . 'backups that failed a check.';
    }

    /** What the S3 copy needs, or when it was last made. */
    private function s3Sentence(): string
    {
        if (!$this->savedS3Keys()) { return 'Keys missing. Uploads start once keys are saved in Settings.'; }
        $pending = (int) ($this->overview()['s3_pending']['count'] ?? 0);
        $parts = [($this->encryption()['enabled'] ?? false) ? ($this->pendingKeyShort() !== null ? 'Encrypted once the recovery key is saved.' : 'Encrypted.') : 'Not encrypted.'];
        $run = $this->run($this->overview()['last_good']['s3'] ?? null);
        if ($run && $run['created_at'] !== null) {
            $parts[] = 'Last upload ' . $this->ago($run['created_at']) . ($run['remote_checked_at'] ? ', checked ' . $this->ago($run['remote_checked_at']) . '.' : '.');
        }
        if ($pending) { $parts[] = $pending . ($pending === 1 ? ' backup' : ' backups') . ' not uploaded yet.'; }
        return implode(' ', $parts);
    }

    private function checksSection(): PanelSection
    {
        $trace = Trace::make(Reader::Staff);
        $ok = 0;
        $steps = $this->checkSteps();
        foreach ($steps as $step) {
            $ok += $step['state'] === State::Ok ? 1 : 0;
            $trace->step($step['label'], $step['state'], $step['at'] ?? null, null, $step['reason'] ?? null, 'check-' . md5($step['label']));
        }
        return PanelSection::make(Messages::INSTALLATION_TITLE, [$trace], $ok . ' of ' . count($steps) . ' checks OK.', 'checks')
            ->link($this->link('checksFull', 'Full backup diagnostics', $this->settingsUrl('full-backup-checks')));
    }

    /** The rail: each check with its state and, unless it's fine, why and what to do. A check with nothing to do (no
     * schedule, an optional copy) has no state of its own, so it draws no mark. */
    private function checkSteps(): array
    {
        $last = $this->lastTick();
        $steps = [match (true) {
            $last === null => ['label' => 'Cron job', 'state' => State::NotRunning, 'reason' => 'It hasn\'t run yet. Setup shows the fix.'],
            $this->schedulerFresh() => ['label' => 'Cron job', 'state' => State::Ok, 'at' => (int) $last],
            default => ['label' => 'Cron job', 'state' => State::NotRunning, 'at' => (int) $last, 'reason' => 'It stopped running. Setup shows the fix.'],
        }];
        if ($this->overviewError !== null) { return $steps; }
        foreach (['full-quiesced', 'database-online'] as $mode) {
            $steps[] = ['label' => self::KIND_LABELS[$mode]] + $this->typeCheck($mode);
        }
        $test = $this->latestRequest('destination-test');
        $steps[] = ['label' => 'Storage test'] + match ($this->storageTestState()) {
            'passed' => ['state' => State::Ok, 'at' => isset($test['finished_at']) ? (int) $test['finished_at'] : null],
            'stale' => ['state' => State::OutOfDate, 'reason' => 'Settings changed since the last test.'],
            'failed' => ['state' => State::Failed, 'reason' => Messages::error($test['error'] ?? null)],
            'queued' => ['state' => State::Queued],
            default => ['state' => State::NotReadYet, 'reason' => 'Use Test storage beside This server.'],
        };
        $checks = $this->configuration()['installation']['checks'] ?? [];
        foreach (['observer', 'tools', 'private_state', 'local_storage', 'offsite'] as $key) {
            if (!isset($checks[$key]) || ($key === 'offsite' && !$this->uploadsToS3())) { continue; }
            [, , $next] = Messages::installationState($key, $checks[$key]);
            $state = match ($checks[$key]['state']) { 'ready' => State::Ok, 'pending' => $key !== 'offsite' ? null : (($checks[$key]['code'] ?? null) === 'storage-untested' ? State::NotVerified : State::NotSet), default => State::Failed };
            if ($key === 'local_storage' && $state === State::Ok) { continue; }
            if ($key === 'offsite' && $state === State::Ok && ($short = $this->keyInfo()['short'])) { $next = 'Uploads encrypted with recovery key ' . $short . '.'; }
            $steps[] = ['label' => Messages::INSTALLATION_CHECKS[$key], 'state' => $state, 'reason' => $state === State::Ok && $key !== 'offsite' ? null : $next];
        }
        if ($this->uploadsToS3() && !$this->savedS3Keys()) {
            $steps[] = ['label' => 'S3 access keys', 'state' => State::NotSet, 'reason' => 'Uploads to S3 can\'t start. Add keys in Settings.'];
        }
        if ($this->pendingKeyShort() !== null) {
            $steps[] = ['label' => 'Recovery key', 'state' => State::NeedsReview, 'reason' => 'Uploads start after you save it outside this server. Setup shows the fix.'];
        }
        return $steps;
    }

    /** A backup type's state for the rail. Off, with no schedule, has no state of its own. */
    private function typeCheck(string $mode): array
    {
        [, $color] = $this->typeStatus($mode);
        $blockers = $this->blockers($mode);
        if ($blockers) {
            return ['state' => $color === 'danger' ? State::NeedsRecovery : State::NeedsReview,
                'reason' => implode('. ', array_unique(array_map(static fn ($item) => rtrim($item['title'], '.'), $blockers))) . '.'];
        }
        if ($color === 'info') {
            $waiting = array_values(array_filter($this->readiness($mode)['items'], static fn ($item) => in_array($item['code'], self::TRANSIENT, true)));
            return ['state' => State::Queued, 'reason' => $waiting ? Messages::error($waiting[0]['code']) : null];
        }
        return $color === 'gray' ? ['state' => null, 'reason' => 'No schedule.'] : ['state' => State::Ok, 'reason' => null];
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
