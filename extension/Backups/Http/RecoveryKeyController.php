<?php
namespace Paymenter\Extensions\Others\Backups\Http;

use Filament\Panel;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use Paymenter\Extensions\Others\Backups\Admin\Pages\Backups as BackupsPage;
use Paymenter\Extensions\Others\Backups\Runtime;
use Symfony\Component\HttpFoundation\Response;

/** Dedicated, signed admin response. Its secret never enters Livewire snapshots. */
final class RecoveryKeyController
{
    public static function register(Panel $panel, string $path, string $name, array $middleware, array $without): void
    {
        Route::get($path . '/recovery-key', self::class)
            ->middleware([...$middleware, 'signed'])
            ->withoutMiddleware($without)
            ->name($name . '.recovery-key');
    }

    public function __invoke(Request $request): Response
    {
        abort_unless(BackupsPage::canAccess(), 403);
        // Defense in depth if a panel's excluded middleware removes the signed guard.
        abort_unless($request->hasValidSignature(), 403);
        $fingerprint = $request->query('fingerprint');
        abort_unless(is_string($fingerprint) && preg_match('/^[a-f0-9]{64}$/D', $fingerprint), 404);
        try {
            $record = Runtime::exportRecoveryKey($fingerprint);
        } catch (\Throwable $error) {
            // A safe code only. Never interpolate child output or secret-bearing exceptions.
            return new Response('Recovery key unavailable. Open Backups and try again.', 409, self::headers());
        }
        $uuid = $record['installation_uuid'] ?? '';
        if (!is_string($uuid) || !preg_match('/^[a-f0-9-]{36}$/D', $uuid)) {
            return new Response('Recovery key unavailable.', 409, self::headers());
        }
        $parameters = $record['parameters'];
        $body = "Paymenter Backups recovery key\n\n"
            . "Save this file in a password manager or another secure place outside this server.\n"
            . "Keep every earlier key: earlier S3 copies still require their own key.\n\n"
            . "Installation UUID: " . $uuid . "\n"
            . "Recovery secret: " . $record['secret'] . "\n"
            . "Fingerprint: " . $record['fingerprint'] . "\n"
            . "Fingerprint scheme: " . $parameters['scheme'] . "\n"
            . "Fingerprint salt (hex): " . $parameters['salt'] . "\n"
            . "Fingerprint iterations: " . $parameters['iterations'] . "\n"
            . "Fingerprint domain: paymenter-backups-key-fingerprint-v1 followed by NUL, then the 32-byte salt\n"
            . "Fingerprint output: 32 bytes (64 lowercase hex characters)\n\n"
            . "Restore without the original server, APP_KEY or S3 access keys:\n"
            . "1. Download RUN.tar.gz.gpg, RUN.recovery.json.gpg and RUN.manifest.json from your provider.\n"
            . "2. In a private scratch directory, check ciphertext hashes and sizes in the descriptor.\n"
            . "3. With GnuPG, decrypt the recovery document and archive into .partial files using this secret.\n"
            . "   Require successful GnuPG completion; never extract partial or unverified plaintext.\n"
            . "4. Use the standalone verify_recovery.py shipped with Backups to verify the complete archive\n"
            . "   against the decrypted recovery document before extraction. Follow docs/RECOVERY.md\n"
            . "   for the isolated database/application restore, APP_KEY and Passport key preservation.\n";
        $response = new Response($body, 200, self::headers());
        $response->headers->set('Content-Disposition', $response->headers->makeDisposition('attachment', 'paymenter-backups-recovery-key-' . $uuid . '.txt'));
        return $response;
    }

    private static function headers(): array
    {
        return ['Content-Type' => 'text/plain; charset=UTF-8', 'Cache-Control' => 'no-store, private',
            'Pragma' => 'no-cache', 'Referrer-Policy' => 'no-referrer', 'X-Content-Type-Options' => 'nosniff'];
    }
}
