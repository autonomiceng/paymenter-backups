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

    /** Check the data directory without creating it, and return a safe fix message. */
    public static function problem(string $app, ?string $configured = null, array $extraDocroots = []): ?array
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
            return self::unavailable($path, $cursor);
        }
        $prospective = rtrim($canonical, '/') . ($suffix ? '/' . implode('/', $suffix) : '');
        if ($prospective === '') { $prospective = '/'; }
        foreach ($roots as $root) {
            if ($prospective === $root || str_starts_with($prospective . '/', rtrim($root, '/') . '/')) { return null; }
        }
        if (file_exists($path)) {
            if (!is_dir($path) || !($root = realpath($path))) { return null; }
            if (fileowner($root) !== posix_geteuid()) {
                return ['code' => 'private-root-invalid',
                    'message' => 'Data directory ' . self::displayPath($root) . ' isn\'t owned by the web user. Change its owner to the web user and set mode 0700, or choose another folder outside Paymenter.'];
            }
            if ((fileperms($root) & 07777) !== 0700) {
                return ['code' => 'private-root-invalid',
                    'message' => 'Data directory ' . self::displayPath($root) . ' must have mode 0700. Set its mode to 0700, or choose another folder outside Paymenter.'];
            }
            return null;
        }
        if (!is_writable($canonical) || !is_executable($canonical)) {
            return self::unavailable($prospective, $canonical);
        }
        return null;
    }

    private static function unavailable(string $path, string $parent): array
    {
        if (!is_dir($parent)) {
            return ['code' => 'private-root-unavailable',
                'message' => 'Data directory ' . self::displayPath($path) . ' can\'t be created: ' . self::displayPath($parent) . ' isn\'t a folder. Choose a writable folder outside Paymenter.'];
        }
        return ['code' => 'private-root-unavailable',
            'message' => 'Data directory ' . self::displayPath($path) . ' can\'t be created: the web user can\'t write to ' . self::displayPath($parent) . '. Create it for the web user with mode 0700, or choose another folder outside Paymenter.'];
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
