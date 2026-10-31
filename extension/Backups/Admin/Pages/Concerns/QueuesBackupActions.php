<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Actions\Action;
use Filament\Forms\Components\Radio;
use Filament\Notifications\Notification;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\Utilities\Get;
use Illuminate\Support\Facades\Auth;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Runtime;

trait QueuesBackupActions
{
    /** One sentence about when queued work starts. Promises a minute only while the cron job checks in and nothing else is running. */
    private function startLine(): string
    {
        if (!$this->schedulerFresh()) {
            return $this->lastTick() ? 'Starts when Paymenter\'s cron job runs again (last run ' . $this->ago($this->lastTick()) . ').' : 'Starts once Paymenter\'s cron job is added and runs.';
        }
        return $this->hasActiveWork() ? 'Starts after the current task finishes.' : 'Starts within a minute.';
    }

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
        $notification = Notification::make()->title($title)->success();
        $notice = $body ?? $start;
        if ($notice !== '') { $notification->body($notice); }
        $notification->send();
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
            'full-quiesced' => 'Database and application files. Runtime logs, cache and sessions are omitted. The site stays online.',
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
                if (!$this->queue('run', ['mode' => $mode], 'Backup queued', $this->schedulerFresh() ? null : '')) { $action->halt(); }
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
}
