"""Staff schedule summaries use the real Blade and bundled Livewire client.

Set PAYMENTER_BACKUPS_PLAYWRIGHT to the installed Playwright module path.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PHP = r'''
require $argv[1].'/vendor/autoload.php';
function view($name, $data) {
    return new class($name, $data) {
        public function __construct(private string $name, private array $data) {}
        public function render() {
            $compiler = new Illuminate\View\Compilers\BladeCompiler(new Illuminate\Filesystem\Filesystem, $GLOBALS['scratch']);
            $compiled = $compiler->compileString(file_get_contents($GLOBALS['root'].'/extension/Backups/resources/views/'.str_replace('.', '/', substr($this->name, 9)).'.blade.php'));
            extract($this->data); ob_start(); eval('?>'.$compiled); return ob_get_clean();
        }
    };
}
$GLOBALS['root'] = $argv[2]; $GLOBALS['scratch'] = $argv[3];
require $argv[2].'/extension/Backups/Messages.php';
require $argv[2].'/extension/Backups/Admin/Pages/Concerns/BuildsPageContent.php';
$page = new class {
    use Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsPageContent { scheduleSentence as public sentence; }
    private const KINDS = ['full-quiesced' => 'full', 'database-online' => 'database'];
    public int $next = 1794106800; // Sunday Nov 8, 03:00 UTC; Saturday in Pacific.
    public string $frequency = 'weekly';
    private function formValues() { return ['full_schedule' => $this->frequency, 'full_weekdays' => [6], 'full_time' => '03:00', 'schedule_timezone' => 'UTC', 'database_schedule' => 'daily', 'database_time' => '02:00']; }
    private function configuration() { return ['schedules' => ['full' => ['frequency' => $this->frequency, 'next_at' => $this->next], 'database' => ['frequency' => 'daily', 'next_at' => $this->next - 3600]]]; }
};
$rows = [];
foreach ([1794106800, 1783220400] as $next) {
    $page->next = $next; $rows[] = (string) $page->sentence('full-quiesced').'|'.(string) $page->sentence('database-online');
}
$page->frequency = 'off'; $rows[] = (string) $page->sentence('full-quiesced');
echo json_encode($rows, JSON_THROW_ON_ERROR);
'''
JS = r'''
const fs = require('node:fs');
const {chromium} = require(process.env.PAYMENTER_BACKUPS_PLAYWRIGHT);
(async () => {
 const input = JSON.parse(fs.readFileSync(0, 'utf8'));
 const browser = await chromium.launch({headless: true});
 try {
  const page = await browser.newPage({timezoneId: 'America/Los_Angeles'});
  const snapshot = JSON.stringify({data:{},memo:{id:'schedule',name:'backups',children:[],errors:[],locale:'en'},checksum:'fixture'});
  const html = row => `<div wire:id="schedule" wire:snapshot='${snapshot}' wire:effects="{}">${row}</div>`;
  let row = input.rows[0];
  await page.route('**/*', route => route.request().url() === 'http://fixture.invalid/livewire/update' ? route.fulfill({json:{components:[{snapshot,effects:{html:html(row)}}],assets:[]}}) : route.abort());
  await page.setContent(html(row));
  await page.evaluate(() => { window.livewireScriptConfig={uri:'http://fixture.invalid/livewire/update',csrf:'fixture'}; });
  await page.addScriptTag({content:fs.readFileSync(input.livewire,'utf8')});
  await page.evaluate(() => Livewire.start());
  const expect = async (full, daily) => {
   const text = await page.locator('[wire\\:id="schedule"]').innerText();
   if (text !== full+'|'+daily) throw new Error('Schedule weekday/time must match staff zone: '+text);
  };
  await expect('Weekly, Saturday at 7:00 PM PST','Daily at 6:00 PM PST');
  for (const index of [0,1]) {
   row=input.rows[index]; await page.evaluate(async () => { await Livewire.find('schedule').$refresh(); });
   await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
   await expect(index ? 'Weekly, Saturday at 8:00 PM PDT' : 'Weekly, Saturday at 7:00 PM PST', index ? 'Daily at 7:00 PM PDT' : 'Daily at 6:00 PM PST');
  }
  if (input.rows[2] !== 'Off') throw new Error('Off schedule changed');
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class StaffSchedule(unittest.TestCase):
    def test_first_load_and_poll_localize_schedule_weekday_and_time(self):
        app = Path(os.environ['PAYMENTER_TEST_APP'])
        with tempfile.TemporaryDirectory(prefix='panel-schedule-') as scratch:
            rendered = subprocess.run([os.environ['PHP_BINARY'], '-r', PHP, str(app), str(ROOT), scratch], capture_output=True, text=True, check=True)
            result = subprocess.run([os.environ['NODE_BINARY'], '-e', JS], input=json.dumps({'rows': json.loads(rendered.stdout), 'livewire': str(app/'vendor/livewire/livewire/dist/livewire.js')}), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
