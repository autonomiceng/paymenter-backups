<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Console\Command;

require_once __DIR__ . '/DatabaseConnectionContext.php';

final class ConfigSetCommand extends Command
{
    protected $signature = 'backups:config-set {--defer-init : Internal audited writer for an inherited coordinator} {--restore : Internal compare-and-restore receipt} {--finalize : Internal credential publication under native row locks} {--config-lock-fd=} {--tick-lock-fd=} {--coordination-root=} {--actor=}';
    protected $description = 'Set non-secret backup configuration through native audited Eloquent rows (JSON stdin)';

    public function handle(): int
    {
        $handles = [];
        try {
            $bytes = stream_get_contents(STDIN, 65537);
            if (strlen($bytes) > 65536) { throw new \RuntimeException('config-invalid'); }
            $input = json_decode($bytes, true, 16, JSON_THROW_ON_ERROR);
            if (!is_array($input)) { throw new \RuntimeException('config-invalid'); }
            if (array_key_exists('_database_connection', $input)) {
                if (!$this->option('defer-init')) { throw new \RuntimeException('config-invalid'); }
                $databaseContext = $input['_database_connection'];
                unset($input['_database_connection']);
                DatabaseConnectionContext::apply($databaseContext);
            }
            if (!$this->option('restore') && !$this->option('finalize')) {
                $fields = array_column(Settings::fields(), null, 'name');
                if (array_diff(array_keys($input), array_keys($fields))) { throw new \RuntimeException('config-invalid'); }
                foreach ($input as $key => $value) {
                    if (is_array($value) || is_object($value) || is_null($value)) { throw new \RuntimeException('config-invalid'); }
                    if (!empty($fields[$key]['validation'])) {
                        \Illuminate\Support\Facades\Validator::make([$key => $value], [$key => $fields[$key]['validation']])->validate();
                    }
                }
            }
            $record = \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->firstOrFail();
            if (!$this->option('defer-init')) {
                if ($this->option('restore') || $this->option('finalize')) { throw new \RuntimeException('config-invalid'); }
                $result = Runtime::saveConfiguration($input);
                if (($result['updated'] ?? false) !== true || ($result['partial'] ?? false)) { throw new \RuntimeException('config-save-incomplete'); }
                $this->line(json_encode(['ok' => true]));
                return self::SUCCESS;
            }
            // Only an owning coordinator may bypass lock acquisition. The fd
            // inherits the same open-file-description flock from Python.
            $root = PrivateStore::root(realpath(base_path()), $this->option('coordination-root'));
            foreach (['config' => 'config.lock', 'tick' => 'tick.lock'] as $option => $name) {
                $fd = $this->option($option . '-lock-fd');
                if (!is_string($fd) || !ctype_digit($fd)) { throw new \RuntimeException('config-coordination-required'); }
                $handle = @fopen('php://fd/' . $fd, 'r+b');
                $info = $handle ? fstat($handle) : false;
                $path = $root . '/' . $name;
                $expected = !is_link($path) ? @stat($path) : false;
                if (!$info || !$expected || $info['dev'] !== $expected['dev'] || $info['ino'] !== $expected['ino']
                    || ($info['mode'] & 0170000) !== 0100000 || ($info['mode'] & 0777) !== 0600 || $info['nlink'] !== 1 || $info['uid'] !== posix_geteuid()
                    || !flock($handle, LOCK_EX | LOCK_NB)) { throw new \RuntimeException('config-coordination-required'); }
                $handles[] = $handle;
            }
            $actor = $this->option('actor');
            if ($actor !== null) {
                if (!ctype_digit((string) $actor) || !\Illuminate\Support\Facades\Auth::loginUsingId((int) $actor)
                    || !\Illuminate\Support\Facades\Auth::user()->hasPermission('admin.extensions.update')) { throw new \RuntimeException('config-invalid'); }
            }
            // Paymenter's audit.console defaults false. This native writer must
            // keep page saves audited even though persistence is in a CLI child.
            config(['audit.console' => true]);
            if ($this->option('finalize')) {
                if (!is_array($input['snapshot'] ?? null) || !is_array($input['credentials'] ?? null)
                    || array_diff(array_keys($input), ['snapshot', 'credentials'])) { throw new \RuntimeException('config-invalid'); }
                SettingsWriter::finalize($record, $input['snapshot'], static fn () => Runtime::publishCredentialsWhileLocked($input['credentials'], $root));
                $result = [];
            } elseif ($this->option('restore')) { $result = ['skipped' => SettingsWriter::restore($record, $input)]; }
            else {
                $result = \Illuminate\Support\Facades\DB::transaction(function () use ($record, $input) {
                    return ['receipt' => (object) SettingsWriter::write($record, $input), 'snapshot' => (object) SettingsWriter::snapshot($record)];
                });
            }
            $this->line(json_encode(['ok' => true, ...$result], JSON_THROW_ON_ERROR));
            return self::SUCCESS;
        } catch (\Throwable $error) {
            $code = in_array($error->getMessage(), ['busy', 'config-coordination-required', 'config-save-incomplete', 'config-changed',
                'config-save-outcome-unknown', 'credentials-unreadable', 'credentials-invalid', 'maintenance-probe-credentials-invalid',
                'maintenance-probe-origin-invalid', 'maintenance-probe-origin-mismatch', 'installation-missing', 'installation-mismatch',
                'private-file-invalid', 'private-root-invalid', 'billing-settings-unreadable', 'extension-uninstalled',
                'database-connection-invalid', 'database-driver-unsupported'], true) ? $error->getMessage() : 'config-invalid';
            if ($this->option('defer-init')) {
                // Artisan's styled error() writes to its console output stream.
                // Internal callers consume a bounded, secret-free stderr code.
                fwrite(STDERR, json_encode(['error' => $code], JSON_THROW_ON_ERROR) . "\n");
            } else { $this->error($code); }
            return self::FAILURE;
        } finally {
            // Closing inherited copies must not unlock the parent's descriptors.
            foreach ($handles as $handle) { fclose($handle); }
        }
    }
}
