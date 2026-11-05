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
$page = new class {
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
    $action = $page->createBackupAction();
    $result[] = array_values(array_map(fn ($button) => $button->getLabel(), $action->getModalFooterActions()));
}
echo json_encode($result, JSON_THROW_ON_ERROR);
'''


class CreateActions(unittest.TestCase):
    def test_cancel_precedes_create_and_blocked_dialog_has_only_close(self):
        result = subprocess.run(
            [os.environ['PHP_BINARY'], '-r', PROGRAM,
             os.environ['PAYMENTER_TEST_APP'], str(ROOT)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), [['Cancel', 'Create backup'], ['Close']])
