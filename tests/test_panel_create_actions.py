"""Exercise the extension's Create action using the installed Filament actions."""
import json
import os
from pathlib import Path
import subprocess
import unittest

from panel_browser import browser_node

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
    $result[] = ['labels'=>array_values(array_map(fn ($button) => $button->getLabel(), $action->getModalFooterActions())), 'alignment'=>$action->getModalFooterActionsAlignment()->value];
}
echo json_encode($result, JSON_THROW_ON_ERROR);
'''


class CreateActions(unittest.TestCase):
    def setUp(self):
        self.node = browser_node(self)

    @unittest.skipUnless(os.environ.get('PHP_BINARY') and os.environ.get('PAYMENTER_TEST_APP'),
                         'PHP_BINARY and PAYMENTER_TEST_APP required')
    def test_cancel_precedes_create_and_blocked_dialog_has_only_close(self):
        result = subprocess.run(
            [os.environ['PHP_BINARY'], '-r', PROGRAM,
             os.environ['PAYMENTER_TEST_APP'], str(ROOT)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(rows[1]['labels'], ['Close'])
        app = Path(os.environ['PAYMENTER_TEST_APP'])
        script = r'''const fs = require('node:fs');
const { chromium } = require(process.env.PAYMENTER_BACKUPS_PLAYWRIGHT || 'playwright');
(async () => {
 const input = JSON.parse(fs.readFileSync(0, 'utf8'));
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  await page.setContent(`<div class="fi-modal"><div class="fi-modal-window-ctn"><div class="fi-modal-window" style="width:500px"><div class="fi-modal-footer fi-align-${input.row.alignment}"><div class="fi-modal-footer-actions">${input.row.labels.map(s=>`<button>${s}</button>`).join('')}</div></div></div></div></div>`);
  await page.addStyleTag({content:fs.readFileSync(input.css,'utf8')});
  const buttons = await page.locator('button').evaluateAll(nodes=>nodes.map(n=>({label:n.textContent,x:n.getBoundingClientRect().x,right:n.getBoundingClientRect().right})));
  const cancel=buttons.find(b=>b.label==='Cancel'), create=buttons.find(b=>b.label==='Create backup');
  const footer=await page.locator('.fi-modal-footer-actions').boundingBox();
  if (cancel.x >= create.x) throw new Error('Cancel must be left of Create: '+JSON.stringify(buttons));
  if (Math.abs(create.right-(footer.x+footer.width))>1) throw new Error('Create must be right aligned');
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        browser = subprocess.run([self.node, '-e', script],
            input=json.dumps({'row': rows[0], 'css': str(app/'vendor/filament/filament/dist/theme.css')}),
            capture_output=True, text=True)
        self.assertEqual(browser.returncode, 0, browser.stdout+browser.stderr)
