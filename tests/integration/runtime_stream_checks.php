<?php
/** Bounded download stream checks. Uses disposable child programs only; no Paymenter boot. */
function base_path(string $path = ''): string { return sys_get_temp_dir(); }
require dirname(__DIR__, 2) . '/extension/Backups/Runtime.php';

use Paymenter\Extensions\Others\Backups\Runtime;

$python = $argv[1];
$header = '{"ok": true, "size": 6, "filename": "paymenter-backup-fixture.tar.gz"}';
$child = static fn (string $code) => [$python, '-I', '-c', $code];
$result = [];

// 1. Silent at startup: refused after the startup limit, child stopped.
$started = microtime(true);
try {
    Runtime::openStream($child('import time; time.sleep(600)'), 1);
    $result['startup'] = 'not-refused';
} catch (RuntimeException $error) {
    $result['startup'] = $error->getMessage();
}
$result['startup_seconds'] = round(microtime(true) - $started, 1);

// 2. Ignores SIGTERM and stays silent: still stopped within the bounded grace.
$started = microtime(true);
try {
    Runtime::openStream($child('import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'), 1);
    $result['sigterm_ignored'] = 'not-refused';
} catch (RuntimeException $error) {
    $result['sigterm_ignored'] = $error->getMessage();
}
$result['sigterm_ignored_seconds'] = round(microtime(true) - $started, 1);

// 3. Engine refusal: safe code from stderr, no hang.
try {
    Runtime::openStream($child('import sys; sys.stderr.write(\'{"error": "verification-required"}\\n\'); sys.exit(1)'), 5);
    $result['refusal'] = 'not-refused';
} catch (RuntimeException $error) {
    $result['refusal'] = $error->getMessage();
}

// 4. Header then silence: idle limit ends the read, stop reaps the child.
[$process, $stdout, $stderr, $decoded] = Runtime::openStream($child('import sys, time; sys.stdout.write(' . json_encode($header . "\n") . '); sys.stdout.flush(); time.sleep(600)'), 5);
$status = proc_get_status($process);
$pid = $status['pid'];
$started = microtime(true);
try {
    Runtime::readStream($stdout, 1);
    $result['idle'] = 'not-refused';
} catch (RuntimeException $error) {
    $result['idle'] = $error->getMessage();
}
Runtime::stopStream($process, [$stdout, $stderr]);
$result['idle_seconds'] = round(microtime(true) - $started, 1);
$result['idle_child_running'] = file_exists('/proc/' . $pid) && !str_contains((string) @file_get_contents('/proc/' . $pid . '/status'), 'State:	Z');

// 5. Normal stream: exact bytes after the header, which stays out of the body.
[$process, $stdout, $stderr, $decoded] = Runtime::openStream($child('import sys; sys.stdout.write(' . json_encode($header . "\n") . '); sys.stdout.flush(); sys.stdout.write("abc"); sys.stdout.flush(); sys.stdout.write("def")'), 5);
$body = '';
while (!feof($stdout)) { $body .= Runtime::readStream($stdout, 5); }
Runtime::stopStream($process, [$stdout, $stderr]);
$result['body'] = $body;
$result['header_size'] = $decoded['size'];

echo json_encode($result, JSON_THROW_ON_ERROR) . "\n";
