<?php
namespace Paymenter\Extensions\Others\Backups;

use RuntimeException;
use Symfony\Component\Process\Process;

require_once __DIR__ . '/DatabaseConnectionContext.php';

final class EngineCommandException extends RuntimeException
{
    public function __construct(string $error, public readonly ?string $field = null, public readonly ?string $cause = null)
    {
        parent::__construct($error);
    }
}

final class Runtime
{
    /** Child startup excludes loader, resolver, TLS and application overrides. */
    private static function safeEnvironment(): array
    {
        $environment = [];
        foreach (array_keys(array_merge(getenv(), $_SERVER, $_ENV)) as $name) {
            if (is_string($name)) { $environment[$name] = false; }
        }
        return array_replace($environment, ['PATH' => '/usr/local/bin:/usr/bin:/bin', 'LANG' => 'C.UTF-8', 'LC_ALL' => 'C.UTF-8', 'TZ' => 'UTC']);
    }

    public static function privateRoot(bool $create = false): string
    {
        $extension = \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->firstOrFail();
        $configured = $extension->settings()->where('key', 'private_root')->value('value');
        $rawDocroots = $extension->settings()->where('key', 'extra_docroots')->value('value');
        $rawDocroots = $rawDocroots ?? '[]';
        try { $docroots = json_decode($rawDocroots, true, 16, JSON_THROW_ON_ERROR); }
        catch (\JsonException $error) { throw new \RuntimeException('document-roots-invalid'); }
        if (!is_array($docroots) || !array_is_list($docroots) || !str_starts_with(ltrim($rawDocroots), '[')) {
            throw new \RuntimeException('document-roots-invalid');
        }
        return PrivateStore::root(realpath(base_path()), $configured, $create, $docroots);
    }

    public static function php(): string
    {
        return self::binaries()['php'];
    }

    /** open_basedir limits PHP filesystem access, while configured process executables remain usable. */
    private static function binaries(): array
    {
        $extension = \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->firstOrFail();
        $configured = $extension->settings()->where('key', 'php')->value('value');
        $php = $configured ?: (PHP_SAPI === 'cli' ? PHP_BINARY : PHP_BINDIR . '/php');
        $python = $extension->settings()->where('key', 'python')->value('value');
        $pythonCandidates = $python ? [$python] : ['/usr/local/bin/python3', '/usr/bin/python3', '/bin/python3'];
        if (!str_starts_with($php, '/') || str_contains($php, "\0")) { throw new RuntimeException('php-binary-mismatch'); }
        foreach ($pythonCandidates as $candidate) {
            if (!str_starts_with($candidate, '/') || str_contains($candidate, "\0")) { throw new RuntimeException('python-binary-invalid'); }
        }
        // This probe checks only public executable paths. Private state/credentials retain PHP's own guards.
        $probe = <<<'PYTHON'
import json, os, sys
python, php, current = sys.argv[1:]
def executable(path):
    return os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)
print(json.dumps({"python_valid": sys.version_info >= (3, 10) and executable(python) and os.path.realpath(python) == os.path.realpath(sys.executable),
                  "php_valid": executable(php), "php_canonical": os.path.realpath(php),
                  "current_canonical": os.path.realpath(current)}))
PYTHON;
        $result = null;
        foreach ($pythonCandidates as $candidate) {
            try {
                $process = new Process([$candidate, '-I', '-B', '-c', $probe, $candidate, $php, PHP_BINARY], base_path(), self::safeEnvironment());
                $process->setTimeout(5);
                $process->mustRun();
                $result = json_decode($process->getOutput(), true, 8, JSON_THROW_ON_ERROR);
                if (($result['python_valid'] ?? false) === true) { $python = $candidate; break; }
            } catch (\Throwable $error) { $result = null; }
        }
        if (!is_array($result) || ($result['python_valid'] ?? false) !== true) { throw new RuntimeException('python-binary-invalid'); }
        if (($result['php_valid'] ?? false) !== true || (PHP_SAPI === 'cli' && $result['php_canonical'] !== $result['current_canonical'])) {
            throw new RuntimeException('php-binary-mismatch');
        }
        // Automatic PHP follows this hosting PHP version; never choose a
        // different system PHP when the corresponding CLI is missing.
        try {
            $check = new Process([$php, '-r', 'echo PHP_SAPI, " ", PHP_VERSION_ID;'], base_path(), self::safeEnvironment());
            $check->setTimeout(5);
            $check->mustRun();
            if (!preg_match('/^cli ([0-9]+)$/D', $check->getOutput(), $version) || (int) $version[1] < 80300) { throw new RuntimeException('php-binary-mismatch'); }
        } catch (\Throwable $error) { throw new RuntimeException('php-binary-mismatch'); }
        return ['python' => $python, 'php' => $php];
    }

    /** Engine argv; the run ID or other public arguments follow the global flags. */
    private static function engine(array $arguments, ?array $databaseContext = null): array
    {
        $root = self::privateRoot();
        $binaries = self::binaries();
        return [$binaries['python'], '-I', __DIR__ . '/runtime/cli.py', '--app', realpath(base_path()), '--private-root', $root, '--bridge', __DIR__ . '/bridge.php', '--php', $binaries['php'], ...($databaseContext !== null ? ['--database-connection-stdin'] : []), ...$arguments];
    }

    /** The active request's resolved DB settings, kept off argv and the child environment. */
    private static function databaseConnectionContext(): array
    {
        return DatabaseConnectionContext::capture();
    }

    /** Commands that load or validate the Paymenter database envelope. */
    private static function needsDatabaseConnectionContext(array $arguments): bool
    {
        $command = $arguments[0] ?? '';
        if ($command === 'config') { return in_array($arguments[1] ?? '', ['show', 'set', 'check', 'validate'], true); }
        return in_array($command, ['init', 'tick', 'overview', 'plan', 'run', 'verify', 'export', 'delete', 'schedule',
            'deliver', 'retention', 'retrieve', 'destination-test', 'request-worker'], true);
    }

    /** Keep only a fixed error code and a known setting field from engine stderr. */
    public static function errorDetails(string $stderr, string $fallback = 'engine-unavailable'): array
    {
        $decoded = json_decode(trim($stderr), true);
        $code = is_array($decoded) ? ($decoded['error'] ?? null) : null;
        if (!is_string($code) || !preg_match('/^[a-z0-9][a-z0-9-]{0,63}$/D', $code)) { $code = $fallback; }
        $field = is_array($decoded) && is_string($decoded['field'] ?? null) ? $decoded['field'] : null;
        if ($field === null || !array_key_exists($field, Settings::LABELS)) { $field = null; }
        $details = ['code' => $code, 'field' => $field];
        $cause = self::safeWriterCause(is_array($decoded) ? ($decoded['detail']['writer_cause'] ?? null) : null);
        if ($cause !== null) { $details['cause'] = $cause; }
        return $details;
    }

    /** Remove credentials and terminal control bytes before a writer cause reaches logs or UI. */
    public static function safeWriterCause(mixed $cause): ?string
    {
        if (!is_string($cause) || $cause === '') { return null; }
        if ($cause === 'command-not-registered') { return $cause; }
        $cause = preg_replace('/\x1B\[[0-?]*[ -\/]*[@-~]/', '', $cause) ?? '';
        $cause = preg_replace('/(?i)\b(password|passwd|pwd|username|user|secret|token|api[_-]?key|access[_-]?key|authorization)\b(\s*[:=]\s*)(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)/', '$1$2[redacted]', $cause) ?? '';
        $cause = preg_replace('/(?i)(:\/\/)[^\/@\s:]+:[^\/@\s]+@/', '$1[redacted]@', $cause) ?? '';
        $cause = strip_tags($cause);
        $cause = preg_replace('/[\x00-\x1F\x7F]/', ' ', $cause) ?? '';
        $cause = trim(preg_replace('/\s+/', ' ', $cause) ?? '');
        return $cause === '' ? null : substr($cause, 0, 160);
    }

    /** Record a fixed event code and only the bounded, redacted writer cause. */
    public static function recordNativeWriterFailure(string $stderr): array
    {
        $details = self::errorDetails($stderr, 'config-write-outcome-unknown');
        $cause = $details['cause'] ?? 'writer-failed';
        \Illuminate\Support\Facades\Log::warning('Backups native writer failed', [
            'code' => 'native-writer-failed',
            'cause' => $cause,
        ]);
        $details['cause'] = $cause;
        return $details;
    }

    /** Compatibility helper for stream commands that only consume the fixed code. */
    public static function errorCode(string $stderr, string $fallback = 'engine-unavailable'): string
    {
        return self::errorDetails($stderr, $fallback)['code'];
    }

    /** Public engine JSON only; runtime secrets are never returned to a browser. */
    public static function command(array $arguments, ?array $input = null, int $timeout = 10): array
    {
        if (in_array($arguments[0] ?? '', ['encryption-key', 'recovery-key', 'credentials-rewrap'], true)) { throw new RuntimeException('sensitive-command-refused'); }
        $databaseContext = null;
        if (self::needsDatabaseConnectionContext($arguments)) {
            $databaseContext = self::databaseConnectionContext();
            $input ??= [];
            $input['_database_connection'] = $databaseContext;
        }
        $process = new Process(self::engine($arguments, $databaseContext), base_path(), self::safeEnvironment(), $input === null ? null : json_encode($input === [] ? new \stdClass : $input, JSON_THROW_ON_ERROR));
        $process->setTimeout($timeout);
        $process->run();
        if (!$process->isSuccessful()) {
            $failure = self::errorDetails($process->getErrorOutput());
            throw new EngineCommandException($failure['code'], $failure['field'], $failure['cause'] ?? null);
        }
        return json_decode($process->getOutput(), true, 64, JSON_THROW_ON_ERROR);
    }

    /** Seconds the engine may take to resolve, check and announce an archive. */
    public const STREAM_STARTUP_SECONDS = 30;
    /** Seconds a started download may go without receiving any bytes. */
    public const STREAM_IDLE_SECONDS = 60;

    /**
     * Start the engine's verified archive stream. Returns [process, stdout, stderr]
     * after the header line was read, or throws a safe engine code. A child that
     * does not answer within the startup limit is stopped.
     */
    public static function openArtifact(string $runId, ?int $actor, int $startupSeconds = self::STREAM_STARTUP_SECONDS): array
    {
        if (!preg_match('/^[A-Za-z0-9_-]{1,100}$/D', $runId)) { throw new RuntimeException('invalid-run-id'); }
        $arguments = ['artifact-stream', $runId];
        if ($actor !== null) { array_push($arguments, '--actor', (string) $actor); }
        return self::openStream(self::engine($arguments), $startupSeconds);
    }

    /** @internal Starts any stream command; split out so fixtures can drive a stuck child. */
    public static function openStream(array $command, int $startupSeconds): array
    {
        $environment = array_filter(self::safeEnvironment(), static fn ($value) => $value !== false);
        $process = proc_open($command, [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, base_path(), $environment);
        if (!is_resource($process)) { throw new RuntimeException('engine-unavailable'); }
        try {
            stream_set_blocking($pipes[1], false);
            stream_set_blocking($pipes[2], false);
            $deadline = microtime(true) + $startupSeconds;
            $header = '';
            $error = '';
            while (!str_contains($header, "\n") && strlen($header) <= 4096) {
                $remaining = $deadline - microtime(true);
                if ($remaining <= 0) { throw new RuntimeException('artifact-stream-timeout'); }
                $read = array_filter([$pipes[1], $pipes[2]], static fn ($pipe) => !feof($pipe));
                if (!$read) { break; }
                $write = $except = null;
                if (@stream_select($read, $write, $except, (int) $remaining, (int) (fmod($remaining, 1) * 1e6)) === false) { throw new RuntimeException('engine-unavailable'); }
                foreach ($read as $pipe) {
                    // Only the header line is read here; archive bytes stay in the pipe for the response.
                    if ($pipe === $pipes[1]) { $header .= (string) fgets($pipe, 4097 - strlen($header)); }
                    else { $error .= (string) fread($pipe, 4096 - min(strlen($error), 4095)); }
                }
            }
            $decoded = str_ends_with($header, "\n") ? json_decode($header, true) : null;
            if (!is_array($decoded) || ($decoded['ok'] ?? false) !== true || !is_int($decoded['size'] ?? null) || !is_string($decoded['filename'] ?? null)
                || !preg_match('/^paymenter-backup-[A-Za-z0-9_-]{1,100}\.tar\.gz$/D', $decoded['filename'])) {
                throw new RuntimeException(self::errorCode($error));
            }
        } catch (\Throwable $failure) {
            self::stopStream($process, $pipes);
            throw $failure instanceof RuntimeException ? $failure : new RuntimeException('engine-unavailable');
        }
        return [$process, $pipes[1], $pipes[2], $decoded];
    }

    /**
     * Next chunk of a started stream, '' at the end, or an exception after the
     * idle limit. A slow client is fine; a silent child is not.
     */
    public static function readStream($stdout, int $idleSeconds = self::STREAM_IDLE_SECONDS): string
    {
        $read = [$stdout];
        $write = $except = null;
        $ready = @stream_select($read, $write, $except, $idleSeconds);
        if ($ready === false) { throw new RuntimeException('engine-unavailable'); }
        if ($ready === 0) { throw new RuntimeException('artifact-stream-timeout'); }
        $chunk = fread($stdout, 1048576);
        return $chunk === false ? '' : $chunk;
    }

    /**
     * Close the pipes, give the engine a moment to record an interrupted
     * download, then stop it. Never waits without a bound.
     */
    public static function stopStream($process, array $pipes): void
    {
        foreach ($pipes as $pipe) { if (is_resource($pipe)) { fclose($pipe); } }
        if (!is_resource($process)) { return; }
        foreach ([[null, 2.0], [15, 2.0], [9, 2.0]] as [$signal, $grace]) {
            if ($signal !== null) { proc_terminate($process, $signal); }
            $deadline = microtime(true) + $grace;
            while (($status = proc_get_status($process))['running'] && microtime(true) < $deadline) { usleep(20000); }
            if (!$status['running']) { break; }
        }
        proc_close($process);
    }

    /** Merge requested namespaces under one private credential flock. Null keeps saved values. */
    public static function saveConfiguration(array $settings, ?array $credentials = null): array
    {
        $input = ['settings' => $settings ?: new \stdClass];
        $databaseContext = self::databaseConnectionContext();
        $input['_database_connection'] = $databaseContext;
        if ($credentials !== null) {
            $input['credentials'] = $credentials['s3'] ?? null;
            $input['maintenance_probe_credentials'] = $credentials['site'] ?? null;
        }
        $actor = \Illuminate\Support\Facades\Auth::id();
        if ($actor !== null) { $input['_actor'] = (int) $actor; }
        $arguments = self::engine(['config', 'set'], $databaseContext);
        if (array_key_exists('private_root', $settings) && !is_file(self::privateRoot() . '/installation.json')) {
            // A first save may choose its private location before any identity exists.
            $index = array_search('--private-root', $arguments, true);
            $arguments[$index + 1] = PrivateStore::root(realpath(base_path()), $settings['private_root'] ?: null,
                false, $settings['extra_docroots'] ?? []);
        }
        $process = new Process($arguments, base_path(), self::safeEnvironment(), json_encode($input, JSON_THROW_ON_ERROR));
        // Finite bridge/native/probe phases and rollback can outlive the ordinary
        // status timeout. A killed writer is an unknown outcome, never "not saved".
        $process->setTimeout(300);
        try { $process->run(); }
        catch (\Symfony\Component\Process\Exception\ProcessTimedOutException $error) {
            $failure = self::recordNativeWriterFailure('{"error":"config-write-outcome-unknown","detail":{"writer_cause":"writer-timeout"}}');
            throw new EngineCommandException($failure['code'], $failure['field'], $failure['cause'] ?? null);
        }
        if (!$process->isSuccessful()) {
            $failure = self::errorDetails($process->getErrorOutput(), 'config-save-outcome-unknown');
            if (in_array($failure['code'], ['command-not-registered', 'config-write-outcome-unknown', 'config-write-failed'], true)) {
                $failure = self::recordNativeWriterFailure($process->getErrorOutput());
                throw new EngineCommandException($failure['code'], $failure['field'], $failure['cause'] ?? null);
            }
            throw new RuntimeException($failure['code']);
        }
        try { $result = json_decode($process->getOutput(), true, 32, JSON_THROW_ON_ERROR); }
        catch (\JsonException $error) {
            $failure = self::recordNativeWriterFailure('{"error":"config-write-outcome-unknown","detail":{"writer_cause":"writer-output-invalid"}}');
            throw new EngineCommandException($failure['code'], $failure['field'], $failure['cause'] ?? null);
        }
        if (!is_array($result) || !is_bool($result['updated'] ?? null)) {
            $failure = self::recordNativeWriterFailure('{"error":"config-write-outcome-unknown","detail":{"writer_cause":"writer-output-invalid"}}');
            throw new EngineCommandException($failure['code'], $failure['field'], $failure['cause'] ?? null);
        }
        $cause = self::safeWriterCause($result['writer_cause'] ?? null);
        if ($cause !== null) {
            $result['writer_cause'] = $cause;
            self::recordNativeWriterFailure(json_encode(['error' => $result['error'] ?? 'config-save-outcome-unknown',
                'detail' => ['writer_cause' => $cause]], JSON_THROW_ON_ERROR));
        }
        return $result;
    }

    /** Standalone secret edits use the same coordinator as combined Save. */
    public static function publishCredentialsWhileLocked(array $input, string $root): void
    {
        // Internal native finalizer owns both inherited flocks and native row
        // locks. Read committed rows in the cold child; it writes only secrets.
        $process = new Process([PHP_BINARY, __DIR__ . '/bridge.php', 'credentials-merge', '--database-connection-stdin', '--app', realpath(base_path()), '--private-root', $root],
            base_path(), self::safeEnvironment(), json_encode(['database_connection' => self::databaseConnectionContext(), 'payload' => $input], JSON_THROW_ON_ERROR));
        $process->setTimeout(30);
        try { $process->run(); }
        catch (\Throwable $error) { throw new RuntimeException('config-save-outcome-unknown'); }
        if (!$process->isSuccessful()) { throw new RuntimeException(self::errorCode($process->getErrorOutput(), 'config-save-outcome-unknown')); }
        $result = json_decode($process->getOutput(), true);
        if (($result['ok'] ?? false) !== true) { throw new RuntimeException('config-save-outcome-unknown'); }
    }

    public static function saveCredentials(array $input): void
    {
        $result = self::saveConfiguration([], $input);
        if (($result['partial'] ?? false) || ($result['updated'] ?? false) !== true) { throw new RuntimeException('config-save-outcome-unknown'); }
    }

    /** Encryption setup returns only public metadata; secrets have dedicated paths below. */
    public static function encryptionKey(string $action, array $input = []): array
    {
        if (!in_array($action, ['generate', 'import', 'acknowledge', 'discard-pending'], true)) {
            throw new RuntimeException('bridge-command-invalid');
        }
        $allowed = match ($action) { 'import' => ['passphrase', 'fingerprint'], 'acknowledge' => ['fingerprint'], default => [] };
        if (array_diff(array_keys($input), $allowed)) { throw new RuntimeException('archive-key-invalid'); }
        $result = self::keyResponse('archive-key-' . $action, $input);
        if (array_diff(array_keys($result), ['fingerprint', 'short', 'state'])) { throw new RuntimeException('archive-key-invalid'); }
        return $result;
    }

    /** Fresh selected identity only; never key material or the overview's shortened identity. */
    public static function recoveryKeyFingerprint(): ?string
    {
        $result = self::keyResponse('archive-key-status');
        if (($result['state'] ?? null) === 'none') { return null; }
        $fingerprint = $result['fingerprint'] ?? null;
        if (!is_string($fingerprint) || !preg_match('/^[a-f0-9]{64}$/D', $fingerprint)) {
            throw new RuntimeException('archive-key-invalid');
        }
        return $fingerprint;
    }

    /** Called inside a confirmation modal renderer, never assigned to Livewire state. */
    public static function revealRecoveryKey(string $fingerprint): array
    {
        if (!preg_match('/^[a-f0-9]{64}$/D', $fingerprint)) { throw new RuntimeException('archive-key-mismatch'); }
        $result = self::keyResponse('archive-key-reveal', ['fingerprint' => $fingerprint]);
        if (!is_string($result['secret'] ?? null) || !is_string($result['fingerprint'] ?? null)
            || array_diff(array_keys($result), ['secret', 'fingerprint'])) { throw new RuntimeException('archive-key-invalid'); }
        if (!hash_equals($fingerprint, $result['fingerprint'])) { throw new RuntimeException('archive-key-mismatch'); }
        return $result;
    }

    /** The short-lived link is bound to one fingerprint, even if rotation follows. */
    public static function recoveryKeyUrl(): string
    {
        $result = self::keyResponse('archive-key-status');
        if (!is_string($result['fingerprint'] ?? null)) { throw new RuntimeException('archive-key-required'); }
        $url = \Illuminate\Support\Facades\URL::temporarySignedRoute(
            \Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::getRouteName() . '.recovery-key',
            now()->addMinutes(5), ['fingerprint' => $result['fingerprint']]
        );
        \Filament\Support\Facades\FilamentView::spaUrlExceptions([$url]);
        return $url;
    }

    /** Sensitive export data may only be used by the protected attachment response. */
    public static function exportRecoveryKey(string $fingerprint): array
    {
        if (!preg_match('/^[a-f0-9]{64}$/D', $fingerprint)) { throw new RuntimeException('archive-key-invalid'); }
        return self::keyResponse('archive-key-export', ['fingerprint' => $fingerprint]);
    }

    /** Refresh APP_KEY wrapping without changing the independent recovery secrets. */
    public static function rewrapCredentials(): void
    {
        self::keyResponse('credentials-rewrap');
    }

    /** Private bridge pipe; authenticated callers cannot supply their own audit actor. */
    private static function keyResponse(string $verb, array $input = []): array
    {
        abort_unless(\Illuminate\Support\Facades\Auth::user()?->hasPermission('admin.extensions.update') ?? false, 403);
        $actor = \Illuminate\Support\Facades\Auth::id();
        $input['actor'] = $actor === null ? null : (int) $actor;
        $process = new Process([self::php(), __DIR__ . '/bridge.php', $verb, '--database-connection-stdin', '--app', realpath(base_path()), '--private-root', self::privateRoot()],
            base_path(), self::safeEnvironment(), json_encode(['database_connection' => self::databaseConnectionContext(), 'payload' => $input], JSON_THROW_ON_ERROR));
        // PBKDF2 and durable credential writes are bounded; this never starts a transfer.
        $process->setTimeout(10);
        $process->run();
        if (!$process->isSuccessful()) { throw new RuntimeException(self::errorCode($process->getErrorOutput(), 'archive-key-unavailable')); }
        $result = json_decode($process->getOutput(), true, 16, JSON_THROW_ON_ERROR);
        if (!is_array($result)) { throw new RuntimeException('archive-key-invalid'); }
        return $result;
    }

    public static function credentials(array $input, bool $maintenanceProbe = false): void
    {
        // Secrets never create private storage; Save settings does that first.
        $process = new Process([self::php(), __DIR__ . '/bridge.php', $maintenanceProbe ? 'maintenance-probe-credentials-write' : 'credentials-write', '--database-connection-stdin', '--app', realpath(base_path()), '--private-root', self::privateRoot()], base_path(), self::safeEnvironment(), json_encode(['database_connection' => self::databaseConnectionContext(), 'payload' => $input], JSON_THROW_ON_ERROR));
        $process->setTimeout(10);
        $process->run();
        if (!$process->isSuccessful()) {
            throw new RuntimeException(self::errorCode($process->getErrorOutput(), 'credentials-save-failed'));
        }
    }
}
