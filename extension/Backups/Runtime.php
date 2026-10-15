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
        $docroots = $extension->settings()->where('key', 'extra_docroots')->value('value') ?: '[]';
        return PrivateStore::root(realpath(base_path()), $configured, $create, json_decode($docroots, true, 16, JSON_THROW_ON_ERROR));
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

    /** Public engine JSON only; runtime secrets are never returned to a browser. */
    public static function command(array $arguments, ?array $input = null, int $timeout = 10): array
    {
        $root = self::privateRoot();
        $binaries = self::binaries();
        $process = new Process([$binaries['python'], '-I', __DIR__ . '/runtime/cli.py', '--app', realpath(base_path()), '--private-root', $root, '--bridge', __DIR__ . '/bridge.php', '--php', $binaries['php'], ...$arguments], base_path(), self::safeEnvironment(), $input === null ? null : json_encode($input === [] ? new \stdClass : $input, JSON_THROW_ON_ERROR));
        $process->setTimeout($timeout);
        $process->run();
        if (!$process->isSuccessful()) { throw new RuntimeException('engine-action-failed'); }
        return json_decode($process->getOutput(), true, 32, JSON_THROW_ON_ERROR);
    }

    public static function credentials(array $input, bool $maintenanceProbe = false): void
    {
        if (!is_file(self::privateRoot() . '/installation.json')) { self::command(['init']); }
        $process = new Process([self::php(), __DIR__ . '/bridge.php', $maintenanceProbe ? 'maintenance-probe-credentials-write' : 'credentials-write', '--app', realpath(base_path()), '--private-root', self::privateRoot()], base_path(), self::safeEnvironment(), json_encode($input, JSON_THROW_ON_ERROR));
        $process->setTimeout(10);
        $process->run();
        if (!$process->isSuccessful()) {
            $safe = json_decode($process->getErrorOutput(), true);
            throw new RuntimeException(($safe['error'] ?? '') === 'credentials-unreadable' ? 'credentials-unreadable' : 'credentials-save-failed');
        }
    }
}
