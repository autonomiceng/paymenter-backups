<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

trait BuildsHelp
{
    private function encryptionHelp(): string
    {
        if ($this->settingsVersion() < 2 && $this->uploadsToS3() && !($this->encryption()['enabled'] ?? true)) {
            return 'Backups in S3 aren\'t encrypted yet. After you save, uploads wait until you save the recovery key.';
        }
        return 'Backups are encrypted with a recovery key before upload. Copies on this server and downloads aren\'t encrypted.';
    }

    private function recoveryKeySentence(): string
    {
        $key = $this->keyInfo();
        $pending = $this->pendingKeyShort();
        if ($this->keyState() === 'active' && $pending === null) {
            return 'Recovery key ' . $key['short'] . ($key['created_at'] ?? null ? ', created ' . $this->time($key['created_at']) : '') . '.';
        }
        if ($this->keyState() === 'active') {
            return 'New recovery key ' . $pending . ' isn\'t confirmed yet. Uploads use key ' . $key['short'] . ' until you save the new key and confirm.';
        }
        return 'Recovery key ' . ($pending ?? '') . ' isn\'t confirmed yet. Uploads to S3 start after you save it outside this server and confirm.';
    }
}
