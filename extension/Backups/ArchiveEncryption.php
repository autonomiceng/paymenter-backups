<?php
namespace Paymenter\Extensions\Others\Backups;

use RuntimeException;

/** Recovery keys live only inside the APP_KEY-wrapped credential payload. */
final class ArchiveEncryption
{
    public const SCHEME = 'pbkdf2-hmac-sha256-v1';
    public const ITERATIONS = 600000;
    private const DOMAIN = "paymenter-backups-key-fingerprint-v1\0";

    public static function secret(mixed $value): string
    {
        if (!is_string($value) || strlen($value) > 1024 || preg_match('//u', $value) !== 1
            || preg_match('/\p{Cc}/u', $value) || preg_match_all('/./us', $value) < 20) {
            throw new RuntimeException('archive-key-invalid');
        }
        return $value; // Never trim or normalize a user passphrase.
    }

    public static function fingerprint(string $secret, ?array $parameters = null): array
    {
        self::secret($secret);
        $salt = $parameters['salt'] ?? bin2hex(random_bytes(32));
        if (!is_string($salt) || !preg_match('/^[a-f0-9]{64}$/D', $salt)
            || ($parameters !== null && (count($parameters) !== 4 || array_diff(array_keys($parameters), ['scheme', 'salt', 'iterations', 'value'])
                || !array_key_exists('salt', $parameters) || !array_key_exists('value', $parameters)
                || !is_string($parameters['value']) || !preg_match('/^[a-f0-9]{64}$/D', $parameters['value'])
                || ($parameters['scheme'] ?? null) !== self::SCHEME || ($parameters['iterations'] ?? null) !== self::ITERATIONS))) {
            throw new RuntimeException('archive-key-invalid');
        }
        $value = bin2hex(hash_pbkdf2('sha256', $secret, self::DOMAIN . hex2bin($salt), self::ITERATIONS, 32, true));
        if (isset($parameters['value']) && (!is_string($parameters['value']) || !hash_equals($value, $parameters['value']))) {
            throw new RuntimeException('archive-key-mismatch');
        }
        return ['scheme' => self::SCHEME, 'salt' => $salt, 'iterations' => self::ITERATIONS, 'value' => $value];
    }

    public static function namespace(?array $value): array
    {
        $value ??= ['version' => 1, 'active_fingerprint' => null, 'pending_fingerprint' => null, 'keys' => []];
        if (($value['version'] ?? null) !== 1 || !is_array($value['keys'] ?? null) || count($value['keys']) > 128
            || array_diff(array_keys($value), ['version', 'active_fingerprint', 'pending_fingerprint', 'keys'])) {
            throw new RuntimeException('archive-key-invalid');
        }
        foreach ($value['keys'] as $id => $record) {
            if (!is_string($id) || !preg_match('/^[a-f0-9]{64}$/D', $id) || !is_array($record)
                || !in_array($record['source'] ?? null, ['generated', 'passphrase'], true)
                || !in_array($record['state'] ?? null, ['pending', 'active', 'retired'], true)
                || (!is_int($record['created_at'] ?? null) && !is_float($record['created_at'] ?? null))
                || !is_finite((float) $record['created_at']) || ($record['created_at'] <= 0)
                || !is_array($record['fingerprint'] ?? null)) { throw new RuntimeException('archive-key-invalid'); }
            self::secret($record['secret'] ?? null);
            $parameters = $record['fingerprint'];
            if (count($parameters) !== 4 || array_diff(array_keys($parameters), ['scheme', 'salt', 'iterations', 'value'])
                || ($parameters['scheme'] ?? null) !== self::SCHEME || ($parameters['iterations'] ?? null) !== self::ITERATIONS
                || ($parameters['value'] ?? null) !== $id || !is_string($parameters['salt'] ?? null)
                || !preg_match('/^[a-f0-9]{64}$/D', $parameters['salt'])) { throw new RuntimeException('archive-key-invalid'); }
            $ack = $record['acknowledgement'] ?? null;
            if ($ack !== null && (!is_array($ack) || ($ack['fingerprint'] ?? null) !== $id
                || count($ack) !== 3 || (!is_int($ack['at'] ?? null) && !is_float($ack['at'] ?? null))
                || !is_finite((float) $ack['at']) || $ack['at'] <= 0 || !array_key_exists('actor', $ack)
                || ($ack['actor'] !== null && (!is_int($ack['actor']) || $ack['actor'] < 0)))) { throw new RuntimeException('archive-key-invalid'); }
            if ($record['state'] === 'active' && $ack === null) { throw new RuntimeException('archive-key-invalid'); }
        }
        foreach (['active_fingerprint' => 'active', 'pending_fingerprint' => 'pending'] as $field => $state) {
            if (!array_key_exists($field, $value) || ($value[$field] !== null
                && (!is_string($value[$field]) || ($value['keys'][$value[$field]]['state'] ?? null) !== $state))) {
                throw new RuntimeException('archive-key-invalid');
            }
            foreach ($value['keys'] as $id => $record) {
                if ($record['state'] === $state && $value[$field] !== $id) { throw new RuntimeException('archive-key-invalid'); }
            }
        }
        return $value;
    }

    /** JSON key records remain an object even after the last pending key is discarded. */
    public static function wire(array $namespace): array
    {
        if (isset($namespace['keys']) && is_array($namespace['keys'])) { $namespace['keys'] = (object) $namespace['keys']; }
        return $namespace;
    }

    public static function payload(array $credentials): string
    {
        if (isset($credentials['archive_encryption']) && is_array($credentials['archive_encryption'])) {
            $credentials['archive_encryption'] = self::wire($credentials['archive_encryption']);
        }
        return json_encode($credentials, JSON_THROW_ON_ERROR);
    }

    /** Pure namespace transition. Caller holds the shared credential flock. */
    public static function change(?array $namespace, string $action, array $input): array
    {
        $value = self::namespace($namespace);
        if (isset($input['actor']) && (!is_int($input['actor']) || $input['actor'] < 0)) { throw new RuntimeException('archive-key-invalid'); }
        if (in_array($action, ['generate', 'import'], true)) {
            if ($value['pending_fingerprint'] !== null) { throw new RuntimeException('archive-key-pending-exists'); }
            if (count($value['keys']) >= 128) { throw new RuntimeException('archive-key-limit'); }
            $secret = $action === 'generate' ? bin2hex(random_bytes(32)) : self::secret($input['passphrase'] ?? null);
            if (array_key_exists('fingerprint', $input) && !is_array($input['fingerprint'])) { throw new RuntimeException('archive-key-invalid'); }
            $parameters = self::fingerprint($secret, $input['fingerprint'] ?? null);
            $id = $parameters['value'];
            if (isset($value['keys'][$id])) { throw new RuntimeException('archive-key-already-exists'); }
            $value['keys'][$id] = ['secret' => $secret, 'source' => $action === 'generate' ? 'generated' : 'passphrase',
                'fingerprint' => $parameters, 'created_at' => microtime(true), 'state' => 'pending', 'acknowledgement' => null];
            $value['pending_fingerprint'] = $id;
            return [$value, ['fingerprint' => $id, 'short' => substr($id, 0, 16), 'state' => 'pending']];
        }
        if ($action === 'discard-pending') {
            $id = $value['pending_fingerprint'];
            if ($id !== null) { unset($value['keys'][$id]); }
            $value['pending_fingerprint'] = null;
            return [$value, ['state' => $value['active_fingerprint'] === null ? 'none' : 'active', 'fingerprint' => $id]];
        }
        if ($action === 'acknowledge') {
            $id = $input['fingerprint'] ?? null;
            if (!is_string($id) || !isset($value['keys'][$id])
                || !in_array($id, [$value['pending_fingerprint'], $value['active_fingerprint']], true)) { throw new RuntimeException('archive-key-mismatch'); }
            if ($value['active_fingerprint'] !== null && $value['active_fingerprint'] !== $id) {
                $value['keys'][$value['active_fingerprint']]['state'] = 'retired';
            }
            $value['keys'][$id]['acknowledgement'] = ['fingerprint' => $id, 'at' => microtime(true), 'actor' => $input['actor'] ?? null];
            $value['keys'][$id]['state'] = 'active';
            $value['active_fingerprint'] = $id;
            if ($value['pending_fingerprint'] === $id) { $value['pending_fingerprint'] = null; }
            return [$value, ['state' => 'active', 'fingerprint' => $id, 'short' => substr($id, 0, 16)]];
        }
        if (in_array($action, ['status', 'reveal', 'export'], true)) {
            $id = $input['fingerprint'] ?? ($value['pending_fingerprint'] ?? $value['active_fingerprint']);
            if ($id === null && $action === 'status') { return [$value, ['state' => 'none']]; }
            if (!is_string($id) || !isset($value['keys'][$id])) { throw new RuntimeException('archive-key-required'); }
            $record = $value['keys'][$id];
            if ($action === 'status') { return [$value, ['fingerprint' => $id, 'short' => substr($id, 0, 16), 'state' => $record['state']]]; }
            // Check the selected identity before any sensitive response is released.
            self::fingerprint($record['secret'], $record['fingerprint']);
            $result = ['secret' => $record['secret'], 'fingerprint' => $id];
            if ($action === 'export') { $result['parameters'] = $record['fingerprint']; }
            return [$value, $result];
        }
        throw new RuntimeException('bridge-command-invalid');
    }

    /** Short run-lock acquisition precedes credentials.lock for activation. */
    public static function lock(string $path, bool $blocking = true, bool $append = false)
    {
        if (is_link($path)) { throw new RuntimeException('private-file-invalid'); }
        $handle = fopen($path, $append ? 'a+b' : 'c+b');
        if (!$handle) { throw new RuntimeException('private-write-failed'); }
        $info = fstat($handle);
        $named = lstat($path);
        if (!$info || !$named || ($info['mode'] & 0170000) !== 0100000 || $info['uid'] !== posix_geteuid()
            || ($info['mode'] & 0777) !== 0600 || $info['nlink'] !== 1 || $info['dev'] !== $named['dev'] || $info['ino'] !== $named['ino']) {
            fclose($handle); throw new RuntimeException('private-file-invalid');
        }
        if (!flock($handle, LOCK_EX | ($blocking ? 0 : LOCK_NB))) {
            fclose($handle); throw new RuntimeException($blocking ? 'private-write-failed' : 'archive-key-change-busy');
        }
        return $handle;
    }

    /** A durable authorization receipt must exist before releasing a secret. */
    public static function audit(string $root, string $action, ?string $fingerprint, ?int $actor): void
    {
        $path = $root . '/audit.jsonl';
        $handle = null;
        try {
            $handle = self::lock($path, true, true);
            fseek($handle, 0, SEEK_END);
            $line = json_encode(['at' => microtime(true), 'event' => 'archive-key-' . $action, 'fingerprint' => $fingerprint,
                'actor' => $actor, 'outcome' => 'authorized'], JSON_THROW_ON_ERROR) . "\n";
            if (fwrite($handle, $line) !== strlen($line) || !fflush($handle) || !fsync($handle)) { throw new RuntimeException('archive-key-audit-failed'); }
            $directory = fopen($root, 'r');
            if (!$directory) { throw new RuntimeException('archive-key-audit-failed'); }
            try { if (!fsync($directory)) { throw new RuntimeException('archive-key-audit-failed'); } }
            finally { fclose($directory); }
        } catch (\Throwable $error) { throw new RuntimeException('archive-key-audit-failed'); }
        finally { if (is_resource($handle)) { fclose($handle); } }
    }
}
