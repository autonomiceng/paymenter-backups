<?php
namespace Paymenter\Extensions\Others\Backups;

/** Validate the only HTTP credential use: the configured HTTPS maintenance origin. */
final class ProbeCredentials
{
    public static function origin(string $url, bool $originOnly = false): string
    {
        $parts = parse_url($url);
        if (!$parts || preg_match('/[\x00-\x20\x7f\\\\]/', $url) || ($parts['scheme'] ?? '') !== 'https' || empty($parts['host']) || isset($parts['user']) || isset($parts['pass']) || str_contains($url, '?') || str_contains($url, '#') || ($originOnly && !in_array($parts['path'] ?? '', ['', '/'], true))) {
            throw new \RuntimeException('maintenance-probe-credentials-invalid');
        }
        if (!preg_match('~^https://(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]{1,5})?(?:/|$)~', $url)) { throw new \RuntimeException('maintenance-probe-credentials-invalid'); }
        $host = strtolower($parts['host']);
        if (str_starts_with($host, '[')) {
            $ip = substr($host, 1, -1);
            if (!filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_IPV6) || str_starts_with(bin2hex(inet_pton($ip)), '00000000000000000000ffff')) { throw new \RuntimeException('maintenance-probe-credentials-invalid'); }
            $host = '[' . inet_ntop(inet_pton($ip)) . ']';
        } elseif (strlen($host) > 253 || array_filter(explode('.', $host), static fn ($label) => !preg_match('/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/D', $label))) {
            throw new \RuntimeException('maintenance-probe-credentials-invalid');
        }
        $port = $parts['port'] ?? 443;
        if ($port < 1 || $port > 65535) { throw new \RuntimeException('maintenance-probe-credentials-invalid'); }
        return 'https://' . $host . ($port === 443 ? '' : ':' . $port);
    }

    public static function validate(array $input, string $appUrl): array
    {
        if (count($input) !== 3 || array_diff(array_keys($input), ['origin', 'username', 'password'])) { throw new \RuntimeException('maintenance-probe-credentials-invalid'); }
        foreach (['origin', 'username', 'password'] as $key) {
            if (!is_string($input[$key] ?? null) || $input[$key] === '' || strlen($input[$key]) > 8192 || preg_match('/[\x00-\x1f\x7f]/', $input[$key])) { throw new \RuntimeException('maintenance-probe-credentials-invalid'); }
        }
        if (str_contains($input['username'], ':')) { throw new \RuntimeException('maintenance-probe-credentials-invalid'); }
        $origin = self::origin($input['origin'], true);
        if ($origin !== self::origin($appUrl)) { throw new \RuntimeException('maintenance-probe-origin-mismatch'); }
        return array_replace($input, ['origin' => $origin]);
    }
}
