"""Exercise the extension's Create action using the installed Filament actions."""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
require $argv[1].'/vendor/autoload.php';
Illuminate\Container\Container::setInstance(new Illuminate\Container\Container);
require $argv[2].'/extension/Backups/Messages.php';
require $argv[2].'/extension/Backups/Admin/Pages/Concerns/QueuesBackupActions.php';
$page = new class extends Livewire\Component {
    use Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\QueuesBackupActions;
    public bool $blocked = false;
    public static function getUrl(array $parameters = []): string { return '/admin/backups'; }
    private function link(string $name, string $label, string $url): Filament\Actions\Action {
        return Filament\Actions\Action::make($name)->label($label)->url($url);
    }
    private function nothingCanStart(): bool { return $this->blocked; }
};
$result = [];
foreach ([false, true] as $blocked) {
    $page->blocked = $blocked;
    $action = $page->createBackupAction()->livewire($page);
    $result[] = array_values(array_map(fn ($button) => $button->getLabel(), $action->getModalFooterActions()));
}
echo json_encode($result, JSON_THROW_ON_ERROR);
'''


class CreateActions(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('PHP_BINARY') and os.environ.get('PAYMENTER_TEST_APP'),
                         'PHP_BINARY and PAYMENTER_TEST_APP required')
    def test_cancel_precedes_create_and_blocked_dialog_has_only_close(self):
        result = subprocess.run(
            [os.environ['PHP_BINARY'], '-r', PROGRAM,
             os.environ['PAYMENTER_TEST_APP'], str(ROOT)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), [['Cancel', 'Create backup'], ['Close']])

@unittest.skipUnless(os.environ.get('PHP_BINARY') and os.environ.get('PAYMENTER_TEST_APP'),
                     'PHP_BINARY and PAYMENTER_TEST_APP required')
class SettingsSections(unittest.TestCase):
    def test_native_settings_include_the_site_password_form(self):
        program = r"""
require $argv[1].'/vendor/autoload.php';
Illuminate\Container\Container::setInstance(new Illuminate\Container\Container);
$root = $argv[2].'/extension/Backups';
new Illuminate\Foundation\Application($argv[1]);
foreach (['Messages', 'Settings', 'PrivateStore', 'Admin/Support/Sheet', 'Admin/Pages/Concerns/BuildsSettingsForm',
          'Admin/Pages/Concerns/ManagesCredentials'] as $file) { require $root.'/'.$file.'.php'; }
$page = new class extends Livewire\Component {
    use Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsSettingsForm;
    use Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\ManagesCredentials;
    private const HOURLY_SENTENCE_JS = '';
    public static function getUrl(array $parameters = []): string { return '/admin/backups'; }
    private function link(string $name, string $label, string $url): Filament\Actions\Action {
        return Filament\Actions\Action::make($name)->label($label)->url($url);
    }
    private function formValues(): array { return []; }
    private function detectedChecks(): array { return []; }
};
$sections = [];
$walk = function ($components) use (&$walk, &$sections) {
    foreach ($components as $component) {
        if ($component instanceof Filament\Schemas\Components\Section) {
            $sections[] = $component->getHeading();
        }
        // Embedded forms are registered independently by Livewire; inspect the actual parent schema.
        if ($component instanceof Filament\Schemas\Components\EmbeddedSchema) {
            $sections[] = $component->getName();
        } elseif ($component instanceof Filament\Schemas\Components\Actions) {
            continue; // Dialog actions do not contain form sections.
        } elseif ($component instanceof Filament\Schemas\Components\Component) {
            $walk($component->getDefaultChildComponents());
        }
    }
};
$walk($page->settingsForm(Filament\Schemas\Schema::make())->getComponents(withHidden: true));
echo json_encode($sections, JSON_THROW_ON_ERROR);
"""
        result = subprocess.run([os.environ['PHP_BINARY'], '-r', program,
                                 os.environ['PAYMENTER_TEST_APP'], str(ROOT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sections = json.loads(result.stdout)
        self.assertIn('Site password (HTTP Basic)', sections)
        self.assertIn('siteAccessForm', sections)
