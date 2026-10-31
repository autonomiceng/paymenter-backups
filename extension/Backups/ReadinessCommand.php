<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Console\Command;

/** Small JSON interface for installers; it never installs host jobs or enables schedules. */
final class ReadinessCommand extends Command
{
    protected $signature = 'backups:readiness {--prepare} {--kind=}';
    protected $description = 'Check installation readiness, optionally preparing private and local storage';

    public function handle(): int
    {
        try {
            $kind = $this->option('kind');
            if ($kind !== null && !in_array($kind, ['full', 'database'], true)) {
                throw new \RuntimeException('schedule-kind-required');
            }
            $result = Runtime::command($this->option('prepare') ? ['readiness', '--prepare'] : ['readiness'], null, 60);
            foreach ($result['checks'] as &$check) {
                $check['next_step'] = $check['code'] === null ? null : Messages::error($check['code']);
            }
            unset($check);
            foreach ($result['schedules'] as &$schedule) {
                $schedule['next_steps'] = array_map(static fn ($code) => Messages::error($code), $schedule['blockers']);
            }
            unset($schedule);
            $result['ready'] = $kind === null
                ? $result['schedules']['full']['ready'] && $result['schedules']['database']['ready']
                : $result['schedules'][$kind]['ready'];
            $this->line(json_encode($result, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR));
            return $result['ready'] ? self::SUCCESS : self::FAILURE;
        } catch (\Throwable $error) {
            $code = $error instanceof \RuntimeException && Messages::known($error->getMessage())
                ? $error->getMessage() : 'engine-unavailable';
            $this->line(json_encode(['schema_version' => 1, 'ready' => false, 'error' => $code,
                'next_step' => Messages::error($code)], JSON_THROW_ON_ERROR));
            return self::FAILURE;
        }
    }
}
