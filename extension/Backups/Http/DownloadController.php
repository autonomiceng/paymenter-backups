<?php
namespace Paymenter\Extensions\Others\Backups\Http;

use Filament\Notifications\Notification;
use Illuminate\Support\Facades\Auth;
use Paymenter\Extensions\Others\Backups\Admin\Pages\Backups as BackupsPage;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Runtime;
use Symfony\Component\HttpFoundation\StreamedResponse;

/**
 * Streams one verified archive. Accepts only a run ID; the engine resolves and
 * opens the owned file, checks it against its recorded verification and
 * withholds the final chunk unless the streamed bytes match.
 */
final class DownloadController
{
    public function __invoke(string $run)
    {
        abort_unless(BackupsPage::canAccess(), 403);
        abort_unless((bool) preg_match('/^[A-Za-z0-9_-]{1,100}$/D', $run), 404);
        try {
            [$process, $stdout, $stderr, $header] = Runtime::openArtifact($run, Auth::id() === null ? null : (int) Auth::id());
        } catch (\Throwable $error) {
            $code = $error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable';
            Notification::make()->title('Download not started')->danger()->body(Messages::error($code))->send();
            return redirect()->to(BackupsPage::getUrl());
        }
        $stopped = false;
        $stop = static function () use (&$stopped, $process, $stdout, $stderr) {
            if ($stopped) { return; }
            $stopped = true;
            // A refused final chunk ends the body short of Content-Length, so clients report failure.
            Runtime::stopStream($process, [$stdout, $stderr]);
        };
        // Backstop for a worker that ends the request early; the child never outlives it.
        register_shutdown_function($stop);
        $response = new StreamedResponse(function () use ($stdout, $stop) {
            // Large archives may take a long time; only a silent engine is cut off.
            set_time_limit(0);
            // Keep running after a disconnect so the engine is always stopped below.
            ignore_user_abort(true);
            try {
                while (!feof($stdout)) {
                    $chunk = Runtime::readStream($stdout);
                    if ($chunk !== '') {
                        echo $chunk;
                        // Pass each chunk on instead of collecting the archive in memory.
                        if (ob_get_level() > 0) { ob_flush(); }
                        flush();
                    }
                    if (connection_aborted()) { break; }
                }
            } catch (\RuntimeException $error) {
                // Idle timeout: the body ends short and the client reports a failed download.
            } finally {
                $stop();
            }
        });
        $response->headers->set('Content-Type', 'application/gzip');
        $response->headers->set('Content-Length', (string) $header['size']);
        $response->headers->set('Content-Disposition', $response->headers->makeDisposition('attachment', $header['filename']));
        $response->headers->set('Cache-Control', 'no-store, private');
        $response->headers->set('X-Content-Type-Options', 'nosniff');
        return $response;
    }
}
