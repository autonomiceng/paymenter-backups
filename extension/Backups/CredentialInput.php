<?php
namespace Paymenter\Extensions\Others\Backups;

/** Pure validation/merge inside the bridge's existing credential flock. */
final class CredentialInput
{
    public static function merge(array $saved, array $input, string $appUrl): array
    {
        if (array_diff(array_keys($input), ['s3', 'site'])) { throw new \RuntimeException('credentials-invalid'); }
        $s3 = $input['s3'] ?? null;
        $site = $input['site'] ?? null;
        if ($s3 !== null) {
            if (!is_array($s3) || array_diff(array_keys($s3), ['access_key', 'secret_key', 'session_token'])
                || !is_string($s3['access_key'] ?? null) || $s3['access_key'] === ''
                || !is_string($s3['secret_key'] ?? null) || $s3['secret_key'] === '') { throw new \RuntimeException('credentials-invalid'); }
            foreach ($s3 as $value) { if (!is_string($value) || strlen($value) > 8192) { throw new \RuntimeException('credentials-invalid'); } }
        }
        // Complete validation precedes either merge. Never trim secret bytes.
        if ($site !== null) {
            if (!is_array($site)) { throw new \RuntimeException('maintenance-probe-credentials-invalid'); }
            $site = ProbeCredentials::validate($site, $appUrl);
        }
        if ($s3 !== null) { $saved = array_replace(array_diff_key($saved, array_flip(['access_key', 'secret_key', 'session_token'])), $s3); }
        if ($site !== null) { $saved['maintenance_probe'] = $site; }
        return $saved;
    }
}
