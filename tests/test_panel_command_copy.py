"""Command box Copy over HTTPS: real Filament markup, bundled Alpine and Chromium's clipboard.

Set PAYMENTER_BACKUPS_PLAYWRIGHT to the installed Playwright module path.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

from panel_browser import browser_node

ROOT = Path(__file__).resolve().parents[1]
COMMAND = "paymenter_backups export '20261105T020008Z 4c1e9a07' --output /new/folder # 100% \"$@\""
PHP = r'''
require $argv[1].'/vendor/autoload.php';
require $argv[2].'/extension/Backups/Admin/Pages/Concerns/BuildsCommands.php';
$app = new Illuminate\Foundation\Application($argv[1]);
Illuminate\Support\Facades\Facade::setFacadeApplication($app);
final class CommandPage { use Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsCommands { command as public; } }
$livewire = new class extends Livewire\Component implements Filament\Schemas\Contracts\HasSchemas { use Filament\Schemas\Concerns\InteractsWithSchemas; };
$html = [];
foreach (['https', 'http'] as $scheme) {
    $app->instance('request', Illuminate\Http\Request::create($scheme.'://fixture.invalid/admin/backups?tab=help'));
    $text = (new CommandPage)->command($argv[3]);
    Filament\Schemas\Schema::make($livewire)->components([$text]);
    $html[$scheme] = $text->toEmbeddedHtml();
}
echo json_encode($html, JSON_THROW_ON_ERROR);
'''
JS = r'''
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PAYMENTER_BACKUPS_PLAYWRIGHT || 'playwright');
(async () => {
 const input = JSON.parse(fs.readFileSync(0, 'utf8'));
 const browser = await chromium.launch({headless: true});
 try {
  const origin = 'https://fixture.invalid';
  const context = await browser.newContext();
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin});
  const page = await context.newPage();
  // Filament pages put every component inside an Alpine root; so does this page.
  const body = `<!doctype html><main x-data><div id="https">${input.https}</div><div id="http">${input.http}</div></main>`;
  await page.route('**/*', route => route.request().url() === origin + '/admin/backups' ? route.fulfill({contentType: 'text/html', body}) : route.abort());
  await page.goto(origin + '/admin/backups');
  assert.equal(await page.evaluate(() => isSecureContext), true);
  await page.evaluate(() => { window.livewireScriptConfig = {uri: 'https://fixture.invalid/livewire/update', csrf: 'fixture'}; });
  await page.addScriptTag({content: fs.readFileSync(input.livewire, 'utf8')});
  // Filament's tooltip plugin is not under test; the copy itself is.
  await page.evaluate(() => { Alpine.magic('tooltip', () => () => {}); Alpine.directive('tooltip', () => {}); Livewire.start(); });
  const box = page.locator('#https [data-backups-command]');
  const clipboard = () => page.evaluate(() => navigator.clipboard.readText());
  const reset = () => page.evaluate(() => navigator.clipboard.writeText('unchanged'));
  assert.equal(await box.getAttribute('role'), 'button');
  for (const key of ['Enter', 'Space']) {
   await reset(); await box.focus(); await page.keyboard.press(key);
   assert.equal(await clipboard(), input.command, key + ' did not copy the exact command');
  }
  await reset(); await box.click();
  assert.equal(await clipboard(), input.command, 'Click did not copy the exact command');
  // The Copy word is a label only; the command text is copied as shown.
  assert.equal((await box.locator('span').first().textContent()), input.command);
  const plain = page.locator('#http [data-backups-command]');
  assert.equal(await plain.getAttribute('role'), null, 'HTTP offers a Copy that cannot work');
  assert.equal(await plain.locator('.backups-copy').count(), 0);
  await reset(); await plain.click();
  assert.equal(await clipboard(), 'unchanged', 'HTTP box changed the clipboard');
 } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


@unittest.skipUnless(os.environ.get('PHP_BINARY') and os.environ.get('PAYMENTER_TEST_APP'),
                     'PHP_BINARY and PAYMENTER_TEST_APP required')
class CommandCopy(unittest.TestCase):
    def setUp(self):
        self.node = browser_node(self)

    def test_https_command_copies_by_click_enter_and_space(self):
        app = Path(os.environ['PAYMENTER_TEST_APP'])
        rendered = subprocess.run([os.environ['PHP_BINARY'], '-r', PHP, str(app), str(ROOT), COMMAND],
                                  capture_output=True, text=True, check=True, timeout=30)
        markup = json.loads(rendered.stdout)
        result = subprocess.run([self.node, '-e', JS], input=json.dumps({**markup, 'command': COMMAND,
                                'livewire': str(app / 'vendor/livewire/livewire/dist/livewire.js')}),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
