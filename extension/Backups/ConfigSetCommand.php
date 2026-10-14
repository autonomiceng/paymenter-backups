<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Console\Command;

final class ConfigSetCommand extends Command
{
    protected $signature = 'backups:config-set';
    protected $description = 'Set non-secret backup configuration through native audited Eloquent rows (JSON stdin)';

    public function handle(): int
    {
        try {
            $bytes = stream_get_contents(STDIN, 65537);
            if (strlen($bytes) > 65536) { throw new \RuntimeException('config-invalid'); }
            $input = json_decode($bytes, true, 16, JSON_THROW_ON_ERROR);
            $fields = array_column((new Backups)->getConfig(), null, 'name');
            if (!is_array($input) || array_diff(array_keys($input), array_keys($fields))) { throw new \RuntimeException('config-invalid'); }
            foreach ($input as $key => $value) {
                if (is_array($value) || is_object($value) || is_null($value)) { throw new \RuntimeException('config-invalid'); }
                if (!empty($fields[$key]['validation'])) {
                    \Illuminate\Support\Facades\Validator::make([$key => $value], [$key => $fields[$key]['validation']])->validate();
                }
            }
            $record = \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->firstOrFail();
            SettingsWriter::write($record, $input);
            Runtime::command(['init']);
            (new Backups)->updated($record);
            $this->line(json_encode(['ok' => true]));
            return self::SUCCESS;
        } catch (\Throwable $error) {
            $this->error('config-invalid');
            return self::FAILURE;
        }
    }
}
