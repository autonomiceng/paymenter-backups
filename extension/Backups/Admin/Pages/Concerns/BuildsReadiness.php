<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Paymenter\Extensions\Others\Backups\Messages;

trait BuildsReadiness
{
    /** Engine readiness for a run mode. Falls back to admission codes from engines without readiness. */
    private function readiness(string $mode): array
    {
        $configuration = $this->configuration();
        $readiness = $configuration['readiness'][$mode] ?? $configuration['readiness'][self::KINDS[$mode]] ?? null;
        if (is_array($readiness) && isset($readiness['items']) && is_array($readiness['items'])) {
            return ['state' => (string) ($readiness['state'] ?? 'needs-setup'), 'items' => array_values(array_filter($readiness['items'], static fn ($item) => is_array($item) && is_string($item['code'] ?? null)))];
        }
        $codes = $configuration['admission'][$mode] ?? [];
        $items = array_map(static fn ($code) => ['code' => (string) $code, 'field' => null, 'detail' => []], $codes);
        $blocking = array_filter($codes, static fn ($code) => !in_array($code, self::TRANSIENT, true));
        if (($configuration['valid'] ?? false) && $this->uploadsToS3() && !$this->savedS3Keys()) {
            $items[] = ['code' => 's3-credentials-required', 'field' => null, 'detail' => []];
            $blocking[] = 's3-credentials-required';
        }
        $state = $blocking ? (in_array('recovery-required', $codes, true) ? 'needs-recovery' : 'needs-setup') : ($codes ? 'waiting' : 'ready');
        return ['state' => $state, 'items' => $items];
    }

    /** Display data for setup items. Never contains secrets. */
    private function readinessContext(): array
    {
        $jobs = [];
        foreach ($this->checks()['jobs']['items'] ?? [] as $job) {
            if (is_array($job) && is_string($job['id'] ?? null)) { $jobs[$job['id']] = $job + ['command' => null, 'label' => null, 'kind' => null, 'source' => null]; }
        }
        return [
            'suggestions' => ['scheduler' => $this->schedulerLine(), 'queue' => $this->queueLine()],
            'crontab_user' => $this->checks()['jobs']['crontab_user'] ?? null,
            'jobs' => $jobs,
            'tools' => $this->checks()['tools'] ?? [],
            'unsupported_stores' => array_values(array_filter($this->checks()['stores'] ?? [], static fn ($store) => is_array($store) && ($store['supported'] ?? null) === false && ($store['kind'] ?? null) !== 'maintenance')),
            'env_path' => base_path('.env'),
            'last_tick' => $this->lastTick() ? $this->ago($this->lastTick()) : null,
            'cron_fresh' => $this->schedulerFresh(),
        ];
    }

    /**
     * Setup items grouped by what they block: 'all' for both types, then each
     * mode. Each blocker is named once, with its fix. Nothing is counted.
     */
    private function setupGroups(): array
    {
        $overview = $this->overview();
        if ($this->setupCache !== null && $this->setupCacheFor === $overview) { return $this->setupCache; }
        $this->setupCacheFor = $overview;
        return $this->setupCache = $this->buildSetupGroups();
    }

    private function buildSetupGroups(): array
    {
        $groups = ['all' => [], 'database-online' => [], 'full-quiesced' => []];
        $configuration = $this->configuration();
        if ($this->overviewError !== null) { return $groups; }
        $context = $this->readinessContext();
        $render = static function (array $item) use ($context): array {
            $rendered = Messages::readinessItem($item['code'], is_array($item['detail'] ?? null) ? $item['detail'] : [], $context + ['field' => $item['field'] ?? null]);
            return $rendered + ['code' => $item['code'], 'id' => $item['code'] . ':' . md5(json_encode($item['detail'] ?? [])), 'page' => $item['page'] ?? false, 'advisory' => $item['advisory'] ?? false];
        };
        if (!$this->initialized()) {
            $groups['all'][] = $render(['code' => 'installation-not-initialized', 'page' => true]);
        } elseif (($configuration['available'] ?? false) && !($configuration['valid'] ?? false)) {
            $groups['all'][] = $render(['code' => $configuration['error'] ?: 'config-invalid',
                'field' => $configuration['error_field'] ?? Messages::field((string) $configuration['error']), 'page' => true]);
        }
        $byMode = [];
        // Nothing runs without Paymenter's cron job: its items become one item, first, and
        // checks that only the cron job can refresh stay quiet until it runs.
        $cronFresh = $this->schedulerFresh();
        $cronCodes = [];
        $engineStorageTest = false;
        foreach (array_keys(self::KINDS) as $mode) {
            foreach ($this->readiness($mode)['items'] as $item) {
                if (($item['code'] ?? null) === 'installation-not-initialized' && !$this->initialized()) { continue; }
                if (in_array($item['code'], self::CRON_CODES, true)) { $cronCodes[$item['code']] = true; continue; }
                if (!$cronFresh && in_array($item['code'], self::CRON_QUIET, true)) { continue; }
                $engineStorageTest = $engineStorageTest || $item['code'] === 'storage-untested';
                $rendered = $render($item);
                $byMode[$mode][$rendered['id']] = $rendered;
            }
        }
        $shared = array_intersect_key($byMode['database-online'] ?? [], $byMode['full-quiesced'] ?? []);
        foreach ($shared as $id => $rendered) { $groups['all'][$id] = $rendered; }
        foreach ($byMode as $mode => $items) {
            foreach ($items as $id => $rendered) { if (!isset($shared[$id])) { $groups[$mode][$id] = $rendered; } }
        }
        // Saving the recovery key affects uploads of both types.
        if ($this->pendingKeyShort() !== null && !$this->hasItem($groups, 'archive-key-save-required')) {
            $groups['all'][] = $render(['code' => 'archive-key-save-required', 'advisory' => true]);
        }
        foreach (['database-online', 'full-quiesced'] as $mode) {
            foreach ($groups[$mode] as $id => $rendered) {
                if ($rendered['code'] === 'archive-key-save-required') { $groups['all'][$id] = $rendered; unset($groups[$mode][$id]); }
            }
        }
        if (!$engineStorageTest && $this->initialized() && ($configuration['valid'] ?? false) && $this->storageTestState() !== 'passed') {
            $groups['all'][] = $render(['code' => 'storage-untested', 'advisory' => true]);
        }
        if ($this->initialized() && $this->overviewError === null && (!$cronFresh || $cronCodes)) {
            array_unshift($groups['all'], $render(['code' => isset($cronCodes['scheduler-missing']) || !$cronCodes ? 'scheduler-missing' : 'billing-guard-stale', 'advisory' => !isset($cronCodes['scheduler-missing'])]));
        }
        // Blockers first, then waiting and advisory items.
        foreach ($groups as $group => $items) {
            uasort($items, static fn ($a, $b) => ($a['tone'] === 'info') <=> ($b['tone'] === 'info'));
            $groups[$group] = $items;
        }
        // A fix line (such as the cron line) is shown once per group, under its first item.
        foreach ($groups as $group => $items) {
            $seen = [];
            $titles = [];
            foreach ($items as $id => $item) {
                if (isset($titles[$item['title']])) { unset($groups[$group][$id]); continue; }
                $titles[$item['title']] = true;
                $groups[$group][$id]['fixes'] = array_values(array_filter($item['fixes'], static function ($fix) use (&$seen) {
                    if (isset($seen[$fix['code']])) { return false; }
                    return $seen[$fix['code']] = true;
                }));
            }
        }
        return array_map('array_values', $groups);
    }

    private function hasItem(array $groups, string $code): bool
    {
        foreach ($groups as $items) { foreach ($items as $item) { if ($item['code'] === $code) { return true; } } }
        return false;
    }

    /**
     * Capture admission is authoritative. Delivery setup remains visible while
     * local capture can start; the page adds its own initialization preconditions.
     */
    private function blockers(string $mode): array
    {
        $configuration = $this->configuration();
        if ($this->overviewError !== null || !($configuration['available'] ?? false)) {
            return [Messages::readinessItem($configuration['error'] ?? 'engine-unavailable', [])];
        }
        $groups = $this->setupGroups();
        $items = [...$groups['all'], ...$groups[$mode]];
        $page = array_values(array_filter($items, static fn ($item) => $item['page'] ?? false));
        if ($page) { return $page; }
        if (array_key_exists($mode, $configuration['admission'] ?? [])) {
            $admission = $configuration['admission'][$mode];
            if (!is_array($admission) || !array_is_list($admission) || array_filter($admission, static fn ($code) => !is_string($code))) {
                return [Messages::readinessItem('config-invalid', []) + ['code' => 'config-invalid']];
            }
            $admission = array_values(array_filter($admission, static fn ($code) => !in_array($code, self::TRANSIENT, true)));
            if (!$admission) { return []; }
            $blocking = array_values(array_filter($items, static fn ($item) => in_array($item['code'], $admission, true)));
            return $blocking ?: array_map(static fn ($code) => Messages::readinessItem($code, []) + ['code' => $code], $admission);
        }
        if (!in_array($this->readiness($mode)['state'], ['needs-setup', 'needs-recovery'], true)) { return []; }
        $blocking = array_values(array_filter($items, static fn ($item) => !($item['advisory'] ?? false) && !in_array($item['code'], self::ADVISORY, true)));
        return $blocking ?: [Messages::readinessItem('config-invalid', []) + ['code' => 'config-invalid']];
    }

    private function nothingCanStart(): bool
    {
        return $this->blockers('full-quiesced') !== [] && $this->blockers('database-online') !== [];
    }

    private function setupOpen(): bool
    {
        if ($this->overviewError !== null) { return false; }
        foreach ($this->setupGroups() as $items) { if ($items) { return true; } }
        return false;
    }

    /** Status line for a backup type: icon and a short fact. */
    private function typeStatus(string $mode): array
    {
        $readiness = $this->readiness($mode);
        $frequency = $this->scheduleFor($mode)['frequency'] ?? 'off';
        $blocked = $this->blockers($mode) !== [];
        $state = $blocked ? ($readiness['state'] === 'needs-recovery' ? 'needs-recovery' : 'needs-setup') : ($readiness['state'] === 'waiting' ? 'waiting' : 'ready');
        if ($blocked && !$this->initialized()) { $state = 'needs-setup'; }
        $next = $this->scheduleFor($mode)['next_at'] ?? null;
        return match ($state) {
            'needs-recovery' => ['ri-alert-line', 'danger', 'Blocked until the interrupted backup is resolved.'],
            'needs-setup' => ['ri-error-warning-line', 'warning', $frequency === 'off' ? 'Not ready. No schedule.' : 'Not ready. Scheduled backups can\'t run yet.'],
            'waiting' => ['ri-time-line', 'info', 'Ready. Waiting before the next start.'],
            default => $frequency === 'off'
                ? ['ri-checkbox-blank-circle-line', 'gray', 'Ready. No schedule; create backups manually.']
                : ['ri-checkbox-circle-line', 'success', 'Ready.' . ($next ? ' Next: ' . $this->time($next, true) . '.' : '')],
        };
    }
}
