<?php
namespace Paymenter\Extensions\Others\Backups;

use RuntimeException;

/** Owner-private atomic state. No native rows or web-visible paths. */
final class PrivateStore
{
    public static function defaultPath(string $app): string
    {
        return rtrim(dirname($app), '/') . '/paymenter-backups-private';
    }

    /** Check without creating the data directory; savedRoot marks the active persisted path. */
    public static function problem(string $app, ?string $configured = null, array $extraDocroots = [], bool $savedRoot = false): ?array
    {
        $path = $configured ?: self::defaultPath($app);
        if (!str_starts_with($path, '/') || is_link($path) || preg_match('~(?:^|/)\.{1,2}(?:/|$)~', $path)) { return null; }
        $appRoot = realpath($app);
        if ($appRoot === false) { return null; }
        $roots = [$appRoot];
        foreach ($extraDocroots as $docroot) {
            if (!is_string($docroot) || !str_starts_with($docroot, '/') || !is_dir($docroot) || !realpath($docroot)) { return null; }
            $roots[] = realpath($docroot);
        }
        $cursor = $path;
        $suffix = [];
        while (!file_exists($cursor)) {
            if (is_link($cursor)) { return null; }
            array_unshift($suffix, basename($cursor));
            $next = dirname($cursor);
            if ($next === $cursor) { return null; }
            $cursor = $next;
        }
        $canonical = realpath($cursor);
        if ($canonical === false || !is_dir($canonical)) {
            return self::unavailable($path, $cursor, true, $savedRoot);
        }
        $prospective = rtrim($canonical, '/') . ($suffix ? '/' . implode('/', $suffix) : '');
        if ($prospective === '') { $prospective = '/'; }
        foreach ($roots as $root) {
            if ($prospective === $root || str_starts_with($prospective . '/', rtrim($root, '/') . '/')) { return null; }
        }
        if (file_exists($path)) {
            if (!is_dir($path) || !($root = realpath($path))) { return null; }
            $identified = self::hasReadableInstallationMarker($root);
            $repairRequired = $savedRoot && self::installedOrRequiresRepair($root);
            if (fileowner($root) !== posix_geteuid()) {
                $params = ['path' => self::displayPath($root), 'reason' => 'owner', 'identified' => $identified];
                if ($repairRequired) { $params['repair_required'] = true; }
                return ['code' => 'private-root-invalid', 'params' => $params];
            }
            if ((fileperms($root) & 07777) !== 0700) {
                $params = ['path' => self::displayPath($root), 'reason' => 'mode', 'identified' => $identified];
                if ($repairRequired) { $params['repair_required'] = true; }
                return ['code' => 'private-root-invalid', 'params' => $params];
            }
            return null;
        }
        if (!is_writable($canonical) || !is_executable($canonical)) {
            return self::unavailable($prospective, $canonical, false, $savedRoot);
        }
        return null;
    }

    /** Keep an existing or inaccessible data path out of first-time setup until it can be checked. */
    public static function installedOrRequiresRepair(string $path): bool
    {
        if (!str_starts_with($path, '/') || str_contains($path, "\0")
            || preg_match('~(?:^|/)\.{1,2}(?:/|$)~', $path)) { return false; }
        clearstatcache(true, $path);
        $info = @lstat($path);
        if ($info !== false) {
            if (($info['mode'] & 0170000) !== 0040000) { return true; }
            if ($info['uid'] !== posix_geteuid() || ($info['mode'] & 07777) !== 0700) { return true; }
            if (!is_readable($path) || !is_executable($path)) { return true; }
            clearstatcache(true, $path . '/installation.json');
            return @lstat($path . '/installation.json') !== false;
        }
        $parent = dirname($path);
        clearstatcache(true, $parent);
        $parentInfo = @stat($parent);
        return is_array($parentInfo) && (($parentInfo['mode'] & 0170000) === 0040000)
            && !is_executable($parent);
    }

    private static function hasReadableInstallationMarker(string $path): bool
    {
        return self::hasInstallationMarker($path) && @is_readable(rtrim($path, '/') . '/installation.json');
    }

    /** Identify a regular installation marker without needing to read its contents. */
    public static function hasInstallationMarker(string $path): bool
    {
        if (str_contains($path, "\0")) { return false; }
        $marker = rtrim($path, '/') . '/installation.json';
        clearstatcache(true, $marker);
        $info = @lstat($marker);
        return is_array($info) && (($info['mode'] & 0170000) === 0100000);
    }

    private static function unavailable(string $path, string $parent, bool $pathExists, bool $savedRoot): array
    {
        $identified = $pathExists && self::hasReadableInstallationMarker($path);
        $repairRequired = $savedRoot && self::installedOrRequiresRepair($path);
        if (!is_dir($parent)) {
            $params = [
                'path' => self::displayPath($path), 'parent' => self::displayPath($parent), 'parent_is_folder' => false,
                'path_exists' => $pathExists, 'identified' => $identified,
            ];
            if ($repairRequired) { $params['repair_required'] = true; }
            return ['code' => 'private-root-unavailable', 'params' => $params];
        }
        $params = [
            'path' => self::displayPath($path), 'parent' => self::displayPath($parent), 'parent_is_folder' => true,
            'path_exists' => $pathExists, 'identified' => $identified,
        ];
        if ($repairRequired) { $params['repair_required'] = true; }
        return ['code' => 'private-root-unavailable', 'params' => $params];
    }

    private static function displayPath(string $path): string
    {
        $path = preg_replace('/[\x00-\x1f\x7f]/', '?', $path) ?? '/';
        return strlen($path) > 400 ? substr($path, 0, 397) . '...' : $path;
    }

    public static function root(string $app, ?string $configured = null, bool $create = false, array $extraDocroots = []): string
    {
        $path = $configured ?: self::defaultPath($app);
        if (!str_starts_with($path, '/') || is_link($path) || preg_match('~(?:^|/)\.{1,2}(?:/|$)~', $path)) { throw new RuntimeException('private-root-invalid'); }
        $roots = [$app];
        foreach ($extraDocroots as $docroot) {
            if (!is_string($docroot) || !str_starts_with($docroot, '/') || !is_dir($docroot) || !realpath($docroot)) { throw new RuntimeException('document-roots-invalid'); }
            $roots[] = realpath($docroot);
        }
        // Check a canonical prospective path before creating anything.
        $cursor = $path;
        $suffix = [];
        while (!file_exists($cursor)) { array_unshift($suffix, basename($cursor)); $next = dirname($cursor); if ($next === $cursor) { throw new RuntimeException('private-root-invalid'); } $cursor = $next; }
        $prospective = rtrim(realpath($cursor), '/') . ($suffix ? '/' . implode('/', $suffix) : '');
        if ($prospective === '') { $prospective = '/'; }
        foreach ($roots as $root) {
            if ($prospective === $root || str_starts_with($prospective . '/', rtrim($root, '/') . '/')) { throw new RuntimeException('private-root-invalid'); }
        }
        if (!is_dir($path) && !$create) { return $prospective; }
        if (!is_dir($path) && !mkdir($path, 0700, true)) { throw new RuntimeException('private-root-unavailable'); }
        $root = realpath($path);
        if (!$root || fileowner($root) !== posix_geteuid() || (fileperms($root) & 07777) !== 0700) { throw new RuntimeException('private-root-invalid'); }
        return $root;
    }

    public static function atomic(string $path, array $value): void
    {
        if (is_link($path)) {
            throw new RuntimeException('private-file-invalid');
        }
        $temporary = dirname($path) . '/.' . basename($path) . '.' . bin2hex(random_bytes(12));
        $handle = fopen($temporary, 'x+b');
        if (!$handle) {
            throw new RuntimeException('private-root-unavailable');
        }
        try {
            chmod($temporary, 0600);
            $bytes = json_encode($value, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
            if (fwrite($handle, $bytes) !== strlen($bytes) || !fflush($handle) || !fsync($handle)) {
                throw new RuntimeException('private-write-failed');
            }
            fclose($handle);
            $handle = null;
            if (!rename($temporary, $path)) {
                throw new RuntimeException('private-write-failed');
            }
            $dir = fopen(dirname($path), 'r');
            if (!$dir || !fsync($dir)) {
                throw new RuntimeException('private-write-failed');
            }
            fclose($dir);
        } finally {
            if (is_resource($handle)) { fclose($handle); }
            if (file_exists($temporary)) { unlink($temporary); }
        }
    }

    public static function read(string $path): array
    {
        if (is_link($path) || !is_file($path) || fileowner($path) !== posix_geteuid() || (fileperms($path) & 077) !== 0 || filesize($path) > 1048576) {
            throw new RuntimeException('private-file-invalid');
        }
        return json_decode(file_get_contents($path), true, 32, JSON_THROW_ON_ERROR);
    }
}
