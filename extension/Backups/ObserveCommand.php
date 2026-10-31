<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\Mail;

/** Host cron invokes this directly, outside scheduler locks and queue workers. */
final class ObserveCommand extends Command
{
    protected $signature = 'backups:observe';
    protected $description = 'Check backup incidents and send staff mail without using the queue';

    public function handle(): int
    {
        $lock = null;
        try {
            $root = Runtime::privateRoot();
            $identity = PrivateStore::read($root . '/installation.json');
            $lockPath = $root . '/observer.lock';
            if (is_link($lockPath)) { throw new \RuntimeException('private-file-invalid'); }
            if (file_exists($lockPath)) {
                $info = lstat($lockPath);
                if (($info['mode'] & 0170000) !== 0100000 || ($info['mode'] & 077) !== 0
                    || $info['uid'] !== posix_geteuid() || $info['nlink'] !== 1) {
                    throw new \RuntimeException('private-file-invalid');
                }
            }
            $lock = fopen($lockPath, file_exists($lockPath) ? 'r+b' : 'x+b');
            if (!$lock) { throw new \RuntimeException('private-file-invalid'); }
            chmod($lockPath, 0600);
            if (!flock($lock, LOCK_EX | LOCK_NB)) { return self::SUCCESS; }
            $path = $root . '/incidents.json';
            $ledger = file_exists($path) || is_link($path) ? PrivateStore::read($path) : [
                'schema_version' => 1, 'installation_uuid' => $identity['installation_uuid'], 'active' => [],
            ];
            if (($ledger['schema_version'] ?? null) !== 1 || !is_array($ledger['active'] ?? null)
                || ($ledger['installation_uuid'] ?? null) !== $identity['installation_uuid']) {
                throw new \RuntimeException('private-file-invalid');
            }
            try {
                $result = Runtime::command(['observe'], null, 30);
                if (($result['installation_uuid'] ?? null) !== $identity['installation_uuid'] || !is_array($result['checks'] ?? null)) {
                    throw new \RuntimeException('observer-result-invalid');
                }
                $checks = $result['checks'];
            } catch (\Throwable $error) {
                // A short settings save or temporary bridge problem gets another
                // host check before mail. Preserve other incidents while blind.
                $checks = [['key' => 'observer', 'reason' => 'observer-unavailable']];
                foreach ($ledger['active'] as $key => $incident) {
                    if ($key !== 'observer') { $checks[] = ['key' => $key, 'reason' => $incident['reason']]; }
                }
            }
            $active = [];
            foreach ($checks as $check) {
                $key = $check['key'];
                if ($check['reason'] === null) { continue; }
                Messages::incidentMail($key, $check['reason']);
                $incident = $ledger['active'][$key] ?? [
                    'id' => hash('sha256', $identity['installation_uuid'] . ':' . $key . ':' . bin2hex(random_bytes(16))),
                    'opened_at' => time(), 'reason' => $check['reason'], 'sent_at' => null,
                ];
                $active[$key] = $incident;
            }
            $ledger['active'] = $active;
            $ledger['checked_at'] = time();
            // Persist the incident key before attempting transport. A transport
            // failure remains pending for the next invocation, never forgotten.
            PrivateStore::atomic($path, $ledger);
            foreach ($active as $key => $incident) {
                if ($incident['sent_at'] !== null || ($key === 'observer' && time() - $incident['opened_at'] < 300)) { continue; }
                $email = config('settings.system_email_address');
                if (config('settings.mail_disable') || !is_string($email) || !filter_var($email, FILTER_VALIDATE_EMAIL)) {
                    throw new \RuntimeException('staff-mail-not-configured');
                }
                $message = Messages::incidentMail($key, $incident['reason']);
                $body = $message['body'] . "\n\nIncident: " . $incident['id'];
                // Native system notifications queue mail. Synchronous transport
                // is essential here: the failed worker may be that very queue.
                $sent = Mail::raw($body, function ($mail) use ($email, $message) {
                    $mail->to($email)->subject($message['subject']);
                });
                if ($sent === null) { throw new \RuntimeException('staff-mail-not-configured'); }
                $ledger['active'][$key]['sent_at'] = time();
                PrivateStore::atomic($path, $ledger);
            }
            $this->line('Backup incident check complete.');
            return self::SUCCESS;
        } catch (\Throwable $error) {
            // Neither transport errors nor engine stderr belong in public logs.
            $this->error('Backup incident check failed. Check private storage access and Paymenter system mail settings, then run backups:observe again.');
            return self::FAILURE;
        } finally {
            if (is_resource($lock)) { fclose($lock); }
        }
    }
}
