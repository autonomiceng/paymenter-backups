"""The native Extension Settings card points at the Backups page and holds no setting."""
import json
import os
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')

# Minimal framework stand-ins: the route table is the only input getConfig reads.
SCRIPT = r'''
namespace App\Attributes {
    #[\Attribute] final class ExtensionMeta { public function __construct(...$arguments) {} }
}
namespace App\Classes\Extension {
    abstract class Extension {}
}
namespace Illuminate\Support {
    final class HtmlString { public function __construct(private string $value) {} public function __toString(): string { return $this->value; } }
}
namespace Illuminate\Support\Facades {
    final class Route { public static array $names = []; public static function has($name): bool { return in_array($name, self::$names, true); } }
}
namespace Paymenter\Extensions\Others\Backups\Admin\Pages {
    final class Backups { public static function getRouteName(): string { return 'filament.admin.pages.backups'; } }
}
namespace {
    function route($name) { return 'https://billing.example.com/admin/' . ($name === 'filament.admin.pages.backups' ? 'backups?a="&b' : 'wrong'); }
    require $argv[1];
    require $argv[2];
    $describe = static fn (array $config): array => array_map(static fn (array $item): array => array_map(
        static fn ($value) => is_object($value) ? ['html' => (string) $value] : $value, $item), $config);
    $backups = new Paymenter\Extensions\Others\Backups\Backups;
    $disabled = $describe($backups->getConfig());
    Illuminate\Support\Facades\Route::$names = ['filament.admin.pages.backups'];
    echo json_encode(['disabled' => $disabled, 'enabled' => $describe($backups->getConfig(['private_root' => '/x']))], JSON_THROW_ON_ERROR);
}
'''


@unittest.skipUnless(PHP, 'PHP required for the native settings card contract')
class NativeSettingsPointerTests(unittest.TestCase):
    def config(self):
        result = subprocess.run([PHP, '-r', SCRIPT, str(ROOT / 'extension/Backups/Messages.php'),
                                 str(ROOT / 'extension/Backups/Backups.php')], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        return json.loads(result.stdout)

    def test_card_links_to_the_backups_page_route(self):
        enabled = self.config()['enabled']
        self.assertEqual(enabled, [{'name': 'backup_settings', 'type': 'placeholder', 'label': {'html':
            'Edit them on the <a class="font-medium" style="text-decoration: underline" '
            'href="https://billing.example.com/admin/backups?a=&quot;&amp;b">Backups page</a>.'}}])

    def test_card_names_the_page_when_its_route_is_not_registered(self):
        # Paymenter registers extension pages only while the extension is enabled.
        disabled = self.config()['disabled']
        self.assertEqual(disabled, [{'name': 'backup_settings', 'type': 'placeholder', 'label':
            'Edit them on the Backups page (System > Backups). Enable this extension to see it.'}])


if __name__ == '__main__':
    unittest.main()
