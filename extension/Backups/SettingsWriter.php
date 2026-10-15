<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Support\Facades\DB;

/** Writes nonsecret native setting rows through audited Eloquent. Never touches credentials. */
final class SettingsWriter
{
    /** Full persisted baseline, locked until the caller's enclosing transaction ends. */
    public static function snapshot(\App\Models\Extension $record): array
    {
        $snapshot = [];
        foreach ($record->settings()->lockForUpdate()->get()->all() as $row) { $snapshot[(string) $row->key] = $row->getAttributes(); }
        ksort($snapshot);
        return $snapshot;
    }

    /** Native-editor writes cannot cross the snapshot comparison and secret publication. */
    public static function finalize(\App\Models\Extension $record, array $expected, callable $publish): void
    {
        DB::transaction(function () use ($record, $expected, $publish) {
            ksort($expected);
            if (self::snapshot($record) !== $expected) { throw new \RuntimeException('config-changed'); }
            $publish();
        });
    }

    /** Explicit bootstrap repair when the engine cannot start; never accepts secrets. */
    public static function repairBinaries(\App\Models\Extension $record, array $values, ?array $expected = null): void
    {
        if (array_diff(array_keys($values), ['private_root', 'extra_docroots', 'python', 'php'])) { throw new \RuntimeException('config-invalid'); }
        $write = static function () use ($record, $values, $expected): void {
            DB::transaction(function () use ($record, $values, $expected): void {
                if ($expected !== null && self::snapshot($record) !== $expected) { throw new \RuntimeException('config-changed'); }
                self::write($record, array_map(static fn ($value) => is_array($value) ? json_encode($value, JSON_THROW_ON_ERROR) : $value, $values));
            });
        };
        // Compare before selecting the coordinator directory, then again beneath
        // both coordinator and native row locks before any publication.
        if ($expected !== null && self::snapshot($record) !== $expected) { throw new \RuntimeException('config-changed'); }
        if (array_key_exists('extra_docroots', $values)) {
            $normalized = Settings::normalize(['extra_docroots' => $values['extra_docroots']]);
            $configured = $record->settings()->where('key', 'private_root')->value('value');
            // Presence only: malformed stored roots cannot authorize any work,
            // but an existing identity must still prevent data-directory moves.
            $original = PrivateStore::root(realpath(base_path()), $configured ?: null);
            $existing = is_file($original . '/installation.json');
            $selected = $existing ? $configured : ($values['private_root'] ?? $configured);
            $root = PrivateStore::root(realpath(base_path()), $selected ?: null, false, $normalized['extra_docroots']);
            if ($existing && array_key_exists('private_root', $values)
                && PrivateStore::root(realpath(base_path()), $values['private_root'] ?: null, false,
                    $normalized['extra_docroots']) !== $root) { throw new \RuntimeException('private-root-move-refused'); }
        } else { $root = Runtime::privateRoot(); }
        if (is_file($root . '/installation.json') && array_key_exists('private_root', $values)
            && PrivateStore::root(realpath(base_path()), $values['private_root'] ?: null, false,
                $normalized['extra_docroots'] ?? []) !== $root) { throw new \RuntimeException('private-root-move-refused'); }
        if (!is_file($root . '/installation.json')) {
            $write();
            return;
        }
        $handles = [];
        $deadline = microtime(true) + 10;
        try {
            foreach (['config.lock', 'tick.lock'] as $name) {
                $path = $root . '/' . $name;
                $named = @lstat($path);
                if ($named && (($named['mode'] & 0170000) !== 0100000 || ($named['mode'] & 0777) !== 0600
                    || $named['uid'] !== posix_geteuid() || $named['nlink'] !== 1)) { throw new \RuntimeException('private-state-permissions'); }
                $mask = umask(0077);
                try { $handle = $named ? fopen($path, 'r+b') : @fopen($path, 'x+b'); }
                finally { umask($mask); }
                if (!$handle && !$named) { $handle = fopen($path, 'r+b'); }
                if (!$handle) { throw new \RuntimeException('private-state-permissions'); }
                $handles[] = $handle;
                $info = fstat($handle);
                $named = lstat($path);
                if (!$info || !$named || ($info['mode'] & 0170000) !== 0100000 || $info['uid'] !== posix_geteuid()
                    || ($info['mode'] & 0777) !== 0600 || $info['nlink'] !== 1 || $named['dev'] !== $info['dev'] || $named['ino'] !== $info['ino']) { throw new \RuntimeException('private-state-permissions'); }
                while (!flock($handle, LOCK_EX | LOCK_NB)) {
                    if (microtime(true) >= $deadline) { throw new \RuntimeException('busy'); }
                    usleep(20000);
                }
            }
            $write();
        } finally {
            foreach (array_reverse($handles) as $handle) { fclose($handle); }
        }
    }

    /** @param array<string, scalar> $values already validated string-form values */
    public static function write(\App\Models\Extension $record, array $values): array
    {
        $fields = array_column(Settings::fields(), null, 'name');
        if (array_diff(array_keys($values), array_keys($fields))) { throw new \RuntimeException('config-invalid'); }
        return DB::transaction(function () use ($record, $values, $fields) {
            $receipt = [];
            foreach ($values as $key => $value) {
                $row = $record->settings()->where('key', $key)->lockForUpdate()->first();
                $before = $row?->getAttributes();
                $attributes = ['value' => is_bool($value) ? ($value ? '1' : '0') : (string) $value,
                    'type' => $fields[$key]['database_type'] ?? 'string', 'encrypted' => false];
                if ($row && (string) $row->value === $attributes['value'] && $row->type === $attributes['type'] && !(bool) $row->encrypted) { continue; }
                $row = $record->settings()->updateOrCreate(['key' => $key], $attributes);
                // SQL hydration normalizes attribute scalars (notably encrypted).
                // Receipts must compare against the same persisted representation.
                $receipt[$key] = ['before' => $before, 'after' => $row->refresh()->getAttributes()];
            }
            return $receipt;
        });
    }

    /** Restore only our unchanged published rows; the compare and write share a DB row lock. */
    public static function restore(\App\Models\Extension $record, array $receipt): array
    {
        return DB::transaction(function () use ($record, $receipt) {
            $skipped = [];
            foreach ($receipt as $key => $entry) {
                if (!array_key_exists($key, Settings::DEFAULTS) || !is_array($entry) || !is_array($entry['after'] ?? null)
                    || !array_key_exists('before', $entry) || ($entry['before'] !== null && !is_array($entry['before']))) {
                    throw new \RuntimeException('config-invalid');
                }
                $row = $record->settings()->where('key', $key)->lockForUpdate()->first();
                if (!$row || $row->getAttributes() !== $entry['after']) { $skipped[] = $key; continue; }
                if ($entry['before'] === null) { $row->delete(); }
                else {
                    $row->forceFill(array_intersect_key($entry['before'], array_flip(['value', 'type', 'encrypted'])));
                    $row->save();
                }
            }
            return $skipped;
        });
    }

    /** Stored string form of a structured value. */
    public static function stored(string $key, mixed $value): string
    {
        $default = Settings::DEFAULTS[$key];
        if (is_array($default)) { return json_encode(array_values((array) $value), JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR); }
        if (is_bool($default)) { return $value ? '1' : '0'; }
        return (string) ($value ?? '');
    }
}
