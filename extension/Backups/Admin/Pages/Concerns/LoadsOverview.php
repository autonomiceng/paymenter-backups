<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Carbon\CarbonImmutable;
use Paymenter\Extensions\Others\Backups\Backups as Extension;
use Paymenter\Extensions\Others\Backups\PrivateStore;
use Paymenter\Extensions\Others\Backups\Runtime;

trait LoadsOverview
{
    private function overview(): array
    {
        if ($this->overviewCache !== null) { return $this->overviewCache; }
        $problem = $this->privateRootProblem();
        if ($problem !== null) {
            $this->overviewError = $problem['code'];
            return $this->overviewCache = ['initialized' => false, 'runs' => [], 'requests' => [], 'scheduler' => ['last_tick_at' => null],
                'configuration' => ['available' => false, 'valid' => false, 'error' => $this->overviewError], 'maintenance' => false, 'installation_uuid' => null];
        }
        try {
            $overview = Runtime::command(['overview'], null, 20);
            $configuration = $overview['configuration'] ?? [];
            $cause = Runtime::safeWriterCause($configuration['writer_cause'] ?? null);
            if ($cause !== null) {
                $this->overviewError = is_string($configuration['error'] ?? null)
                    ? $configuration['error'] : 'config-save-outcome-unknown';
                $this->overviewErrorCause = $cause;
            }
            return $this->overviewCache = $overview;
        } catch (\Throwable $error) {
            $this->overviewError = $error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable';
            $this->overviewErrorCause = $error instanceof \Paymenter\Extensions\Others\Backups\EngineCommandException
                ? Runtime::safeWriterCause($error->cause) : null;
            return $this->overviewCache = ['initialized' => false, 'runs' => [], 'requests' => [], 'scheduler' => ['last_tick_at' => null],
                'configuration' => ['available' => false, 'valid' => false, 'error' => $this->overviewError], 'maintenance' => false, 'installation_uuid' => null];
        }
    }

    /** Return a data directory issue the admin can fix before the engine starts. */
    private function privateRootProblem(): ?array
    {
        try {
            $record = $this->record();
            $configured = $record->settings()->where('key', 'private_root')->value('value');
            $rawDocroots = $record->settings()->where('key', 'extra_docroots')->value('value') ?? '[]';
            $docroots = json_decode($rawDocroots, true, 16, JSON_THROW_ON_ERROR);
            if (!is_array($docroots) || !array_is_list($docroots) || !str_starts_with(ltrim($rawDocroots), '[')) { return null; }
            $application = realpath(base_path());
            if ($application === false) { return null; }
            $configuredRoot = is_string($configured) && $configured !== '' ? $configured : null;
            $activeRoot = $configuredRoot ?? PrivateStore::defaultPath($application);
            $savedRoot = $configuredRoot !== null || PrivateStore::hasInstallationMarker($activeRoot);
            return PrivateStore::problem($application, $configuredRoot, $docroots, $savedRoot);
        } catch (\Throwable $error) {
            return null;
        }
    }

    private function configuration(): array { return $this->overview()['configuration'] ?? []; }

    private function checks(): array { return $this->configuration()['checks'] ?? []; }

    /** True when private state exists, even if the engine cannot report it right now. */
    private function initialized(): bool
    {
        if ($this->overview()['initialized'] ?? false) { return true; }
        try { return @is_file(Runtime::privateRoot() . '/installation.json'); }
        catch (\Throwable $error) {
            // Keep existing or inaccessible data out of first-time setup while
            // owner, mode, and public-directory checks wait for repair.
            try {
                $configured = $this->record()->settings()->where('key', 'private_root')->value('value');
                $application = realpath(base_path());
                if ($application === false) { return false; }
                $path = is_string($configured) && $configured !== '' ? $configured : PrivateStore::defaultPath($application);
                return PrivateStore::installedOrRequiresRepair($path);
            } catch (\Throwable $ignored) { return false; }
        }
    }

    private function record(): \App\Models\Extension
    {
        return \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->firstOrFail();
    }

    private function zone(): string { return config('app.timezone') ?: 'UTC'; }

    private function time(?float $timestamp, bool $withZone = false): string
    {
        if ($timestamp === null) { return 'Unknown'; }
        $date = CarbonImmutable::createFromTimestamp((int) $timestamp)->setTimezone($this->zone());
        return $date->format('j M Y, H:i') . ($withZone ? ' ' . $this->zone() : '');
    }

    private function ago(?float $timestamp): string
    {
        return $timestamp === null ? '' : CarbonImmutable::createFromTimestamp((int) $timestamp)->diffForHumans();
    }

    /** "13 Oct 2026, 02:00 UTC (4 hours ago)" */
    private function when(?float $timestamp): string
    {
        return $timestamp === null ? 'Unknown' : $this->time($timestamp, true) . ' (' . $this->ago($timestamp) . ')';
    }

    private function storage(): array
    {
        $configuration = $this->configuration();
        $destination = $configuration['destination'] ?? [];
        return ($configuration['storage'] ?? []) + [
            'keep_local' => true, 'upload_s3' => ($destination['type'] ?? null) === 's3',
            'local_path' => $destination['local_path'] ?? null, 'default_local_path' => null, 'legacy_default_path' => null,
            's3_bucket' => $destination['s3_bucket'] ?? null,
        ];
    }

    private function uploadsToS3(): bool { return (bool) ($this->storage()['upload_s3'] ?? false); }

    private function savedS3Keys(): bool { return (bool) ($this->configuration()['saved_keys']['s3'] ?? false); }

    private function encryption(): array { return $this->configuration()['encryption'] ?? []; }

    private function keyInfo(): array { return ($this->encryption()['key'] ?? []) + ['state' => 'none', 'short' => null, 'acknowledged' => false, 'pending_short' => null, 'source' => null]; }

    private function keyState(): string { return in_array($this->keyInfo()['state'], ['none', 'pending', 'active'], true) ? $this->keyInfo()['state'] : 'none'; }

    /** Short fingerprint of a key that still needs its saved-outside-this-server confirmation. */
    private function pendingKeyShort(): ?string
    {
        $key = $this->keyInfo();
        if (filled($key['pending_short'])) { return $key['pending_short']; }
        return $key['state'] === 'pending' || ($key['state'] === 'active' && !$key['acknowledged']) ? ($key['short'] ?: null) : null;
    }

    private function settingsVersion(): int
    {
        $this->formValues();
        return (int) ($this->configuration()['settings_version'] ?? $this->storedVersion);
    }

    private function lastTick(): ?float { return $this->overview()['scheduler']['last_tick_at'] ?? null; }

    private function schedulerFresh(): bool
    {
        $last = $this->lastTick();
        return $last !== null && time() - $last < self::SCHEDULER_STALE_SECONDS;
    }

    /** Active requests for a backup, from the engine's whitelist. */
    private function tasksFor(string $runId): array
    {
        return array_values(array_filter($this->overview()['requests'], static fn ($request) => $request['run_id'] === $runId));
    }

    private function hasActiveWork(): bool
    {
        foreach ($this->overview()['requests'] as $request) {
            if (in_array($request['status'], self::ACTIVE, true)) { return true; }
        }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'running') { return true; }
        }
        return false;
    }

    private function activeRequest(string $action, ?string $runId = null): ?array
    {
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] === $action && $request['run_id'] === $runId && in_array($request['status'], self::ACTIVE, true)) { return $request; }
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

    private function run(?string $runId): ?array
    {
        if ($runId === null) { return null; }
        foreach ($this->overview()['runs'] as $run) { if ($run['run_id'] === $runId) { return $run; } }
        return null;
    }
}
