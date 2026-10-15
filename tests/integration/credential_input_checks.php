<?php
require __DIR__ . '/../../extension/Backups/ProbeCredentials.php';
require __DIR__ . '/../../extension/Backups/CredentialInput.php';
use Paymenter\Extensions\Others\Backups\CredentialInput;
$checks = 0;
$assert = static function ($condition) use (&$checks): void {
    if (!$condition) { throw new RuntimeException('assertion-failed'); }
    $checks++;
};
$saved = ['access_key' => 'old', 'secret_key' => 'old', 'session_token' => 'old-token', 'maintenance_probe' => ['origin' => 'https://fixture.invalid', 'username' => 'old', 'password' => 'old'], 'archive_encryption' => ['fixture' => 'preserved']];
$s3 = ['access_key' => 'new', 'secret_key' => ' secret with spaces '];
$site = ['origin' => 'https://fixture.invalid', 'username' => 'new', 'password' => ' password with spaces '];
$merged = CredentialInput::merge($saved, ['s3' => $s3, 'site' => $site], 'https://fixture.invalid');
$assert($merged['secret_key'] === $s3['secret_key'] && $merged['maintenance_probe']['password'] === $site['password']);
$assert(!isset($merged['session_token']) && $merged['archive_encryption'] === $saved['archive_encryption']);
$assert(CredentialInput::merge($saved, ['s3' => null, 'site' => null], 'https://fixture.invalid') === $saved);
$assert(CredentialInput::merge($saved, ['s3' => $s3], 'https://fixture.invalid')['maintenance_probe'] === $saved['maintenance_probe']);
$assert(CredentialInput::merge($saved, ['site' => $site], 'https://fixture.invalid')['session_token'] === 'old-token');
foreach ([['s3' => ['session_token' => 'only']], ['s3' => $s3, 'site' => ['origin' => 'https://changed.invalid', 'username' => 'new', 'password' => 'new']], ['s3' => $s3, 'site' => ['origin' => 'https://fixture.invalid', 'username' => 'incomplete']]] as $input) {
    try { CredentialInput::merge($saved, $input, 'https://fixture.invalid'); throw new RuntimeException('should-refuse'); }
    catch (RuntimeException $error) { $assert($error->getMessage() !== 'should-refuse' && $saved['access_key'] === 'old'); }
}
echo json_encode(['checks' => $checks]) . "\n";
