<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Support\Facades\DB;

/** Writes nonsecret native setting rows through audited Eloquent. Never touches credentials. */
final class SettingsWriter
{
    /** @param array<string, scalar> $values already validated string-form values */
    public static function write(\App\Models\Extension $record, array $values): void
    {
        $fields = array_column(Settings::fields(), null, 'name');
        if (array_diff(array_keys($values), array_keys($fields))) { throw new \RuntimeException('config-invalid'); }
        (new Backups)->installed();
        DB::transaction(function () use ($record, $values, $fields) {
            foreach ($values as $key => $value) {
                $record->settings()->updateOrCreate(['key' => $key], ['value' => is_bool($value) ? ($value ? '1' : '0') : (string) $value, 'type' => $fields[$key]['database_type'] ?? 'string', 'encrypted' => false]);
            }
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
