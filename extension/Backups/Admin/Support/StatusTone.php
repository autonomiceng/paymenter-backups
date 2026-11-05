<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Support;

/** Explicit meanings, independent of Filament's fallback colors. Unknown words stay stock. */
final class StatusTone
{
    public const LABELS = [
        'Verified' => 'ok', 'In S3' => 'ok',
        'Queued' => 'prog', 'Waiting' => 'prog', 'Starting' => 'prog', 'Running' => 'prog', 'Deleting' => 'prog',
        'Deleted' => 'off', 'Skipped' => 'off',
        'Not verified' => 'warn', 'Upload failed' => 'warn', 'Needs recovery' => 'warn', 'Deletion incomplete' => 'warn',
        'S3 copy missing' => 'warn', 'Old S3 folder' => 'warn', 'S3 copy unchecked' => 'warn',
        'Damaged' => 'err', 'Failed' => 'err', 'Interrupted' => 'err', 'Expired' => 'err', "Didn't start" => 'err',
        'S3 copy damaged' => 'err', 'Not usable' => 'err',
    ];

    /** Filament colors => marks, for words that come with their color. */
    public const COLORS = ['success' => 'ok', 'gray' => 'off', 'info' => 'prog', 'warning' => 'warn', 'danger' => 'err'];

    public static function forLabel(string $label): ?string { return self::LABELS[$label] ?? null; }

    public static function forColor(?string $color): ?string { return self::COLORS[$color] ?? null; }
}
