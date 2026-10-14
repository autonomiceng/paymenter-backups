<?php
/**
 * Checks Settings::fromLegacy() against the shared v1 to v2 vectors that the
 * Python adapter also uses. Plain PHP, no fixture:
 *   php tests/integration/settings_migration.php [tests/fixtures/settings-migration.json]
 */
$root = dirname(__DIR__, 2);
require $root . '/extension/Backups/Settings.php';
use Paymenter\Extensions\Others\Backups\Settings;

$path = $argv[1] ?? $root . '/tests/fixtures/settings-migration.json';
$document = json_decode(file_get_contents($path), true, 64, JSON_THROW_ON_ERROR);
$vectors = $document['vectors'] ?? $document;
$failures = [];
foreach ($vectors as $index => $vector) {
    $name = $vector['name'] ?? (string) $index;
    $legacy = array_map(static fn ($value) => is_array($value) ? json_encode($value) : (is_bool($value) ? ($value ? '1' : '0') : $value), $vector['legacy'] ?? $vector['rows'] ?? []);
    $actual = Settings::fromLegacy($legacy);
    foreach ($vector['expected'] as $key => $expected) {
        if (!array_key_exists($key, $actual)) {
            if ($key !== 'settings_version') { $failures[] = "$name: missing $key"; }
            continue;
        }
        if ($actual[$key] !== $expected) {
            $failures[] = "$name: $key expected " . json_encode($expected) . ', got ' . json_encode($actual[$key]);
        }
    }
}
if ($failures) { fwrite(STDERR, implode("\n", $failures) . "\n"); exit(1); }
echo json_encode(['settings_migration_vectors' => count($vectors)]) . "\n";
