<?php
namespace Paymenter\Extensions\Others\Backups;

use RuntimeException;
use Symfony\Component\Process\Process;

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
        $python = $extension->settings()->where('key', 'python')->value('value') ?: '/usr/bin/python3';
        if (!str_starts_with($php, '/') || str_contains($php, "\0")) { throw new RuntimeException('php-binary-mismatch'); }
        if (!str_starts_with($python, '/') || str_contains($python, "\0")) { throw new RuntimeException('python-binary-invalid'); }
        // This probe checks only public executable paths. Private state/credentials retain PHP's own guards.
        $probe = <<<'PYTHON'
import json, os, sys
python, php, current = sys.argv[1:]
def executable(path):
    return os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)
print(json.dumps({"python_valid": executable(python) and os.path.realpath(python) == os.path.realpath(sys.executable),
                  "php_valid": executable(php), "php_canonical": os.path.realpath(php),
                  "current_canonical": os.path.realpath(current)}))
PYTHON;
        try {
            $process = new Process([$python, '-I', '-B', '-c', $probe, $python, $php, PHP_BINARY], base_path(), self::safeEnvironment());
            $process->setTimeout(5);
            $process->mustRun();
            $result = json_decode($process->getOutput(), true, 8, JSON_THROW_ON_ERROR);
        } catch (\Throwable $error) { throw new RuntimeException('python-binary-invalid'); }
        if (!is_array($result) || ($result['python_valid'] ?? false) !== true) { throw new RuntimeException('python-binary-invalid'); }
        if (($result['php_valid'] ?? false) !== true || (PHP_SAPI === 'cli' && $result['php_canonical'] !== $result['current_canonical'])) {
            throw new RuntimeException('php-binary-mismatch');
        }
        return ['python' => $python, 'php' => $php];
    }

    /** Engine argv; the run ID or other public arguments follow the global flags. */
    private static function engine(array $arguments): array
    {
        $root = self::privateRoot();
        $binaries = self::binaries();
        return [$binaries['python'], '-I', __DIR__ . '/runtime/cli.py', '--app', realpath(base_path()), '--private-root', $root, '--bridge', __DIR__ . '/bridge.php', '--php', $binaries['php'], ...$arguments];
    }

    /** Engine stderr carries only {"error": code}; anything else becomes a generic code. */
    public static function errorCode(string $stderr, string $fallback = 'engine-unavailable'): string
    {
        $decoded = json_decode(trim($stderr), true);
        $code = is_array($decoded) ? ($decoded['error'] ?? null) : null;
        return is_string($code) && preg_match('/^[a-z0-9][a-z0-9-]{0,63}$/D', $code) ? $code : $fallback;
    }

    /** Public engine JSON only; runtime secrets are never returned to a browser. */
    public static function command(array $arguments, ?array $input = null, int $timeout = 10): array
    {
        $process = new Process(self::engine($arguments), base_path(), self::safeEnvironment(), $input === null ? null : json_encode($input === [] ? new \stdClass : $input, JSON_THROW_ON_ERROR));
        $process->setTimeout($timeout);
        $process->run();
        if (!$process->isSuccessful()) { throw new RuntimeException(self::errorCode($process->getErrorOutput())); }
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

    public static function credentials(array $input, bool $maintenanceProbe = false): void
    {
        // Secrets never create private storage; Save settings does that first.
        $process = new Process([self::php(), __DIR__ . '/bridge.php', $maintenanceProbe ? 'maintenance-probe-credentials-write' : 'credentials-write', '--app', realpath(base_path()), '--private-root', self::privateRoot()], base_path(), self::safeEnvironment(), json_encode($input, JSON_THROW_ON_ERROR));
        $process->setTimeout(10);
        $process->run();
        if (!$process->isSuccessful()) {
            throw new RuntimeException(self::errorCode($process->getErrorOutput(), 'credentials-save-failed'));
        }
    }
}
