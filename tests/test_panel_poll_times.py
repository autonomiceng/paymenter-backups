"""Render the native time partial and morph it with Paymenter's bundled Alpine."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PHP = r'''
require $argv[1].'/vendor/autoload.php';
$compiler = new Illuminate\View\Compilers\BladeCompiler(new Illuminate\Filesystem\Filesystem, $argv[3]);
$compiled = $compiler->compileString(file_get_contents($argv[2]));
$rows = [];
foreach (['2026-11-05T06:01:00Z', '2026-07-05T06:01:00Z'] as $value) {
    $instant = Carbon\CarbonImmutable::parse($value);
    ob_start(); eval('?>'.$compiled); $rows[] = ob_get_clean();
}
echo json_encode($rows, JSON_THROW_ON_ERROR);
'''

JS = r'''
const fs = require('node:fs');
const { chromium } = require(process.env.PAYMENTER_BACKUPS_PLAYWRIGHT || 'playwright');
(async () => {
    const { rows, livewire } = JSON.parse(fs.readFileSync(0, 'utf8'));
    const browser = await chromium.launch({headless: true});
    try {
        const page = await browser.newPage({timezoneId: 'America/Los_Angeles'});
        const snapshot = JSON.stringify({data: {}, memo: {id: 'poll', name: 'backups', children: [], errors: [], locale: 'en'}, checksum: 'synthetic'});
        const html = row => `<div wire:id="poll" id="poll" wire:snapshot='${snapshot}' wire:effects="{}">` + row.repeat(3) + '</div>';
        let polledRow = rows[0];
        await page.route('**/*', route => route.request().url() === 'http://fixture.invalid/livewire/update'
            ? route.fulfill({json: {components: [{snapshot, effects: {html: html(polledRow)}}], assets: []}})
            : route.abort());
        await page.setContent(html(rows[0]));
        await page.evaluate(() => { window.livewireScriptConfig = {uri: 'http://fixture.invalid/livewire/update', csrf: 'synthetic'}; });
        await page.addScriptTag({content: fs.readFileSync(livewire, 'utf8')});
        await page.evaluate(() => window.Livewire.start());
        const texts = () => page.locator('time').allTextContents();
        const initial = await texts();
        if (initial.some(text => !text.includes('PST') || !text.includes('10:01'))) {
            throw new Error('Initial staff timezone changed: ' + JSON.stringify(initial));
        }
        for (const [row, zone, hour] of [[rows[0], 'PST', '10:01'], [rows[1], 'PDT', '11:01']]) {
            polledRow = row;
            await page.evaluate(async () => { await Livewire.find('poll').$refresh(); });
            await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
            const polled = await texts();
            if (polled.some(text => !text.includes(zone) || !text.includes(hour))) {
                throw new Error('Header, history and schedule must stay in staff timezone after poll: ' + JSON.stringify(polled));
            }
        }
        console.log('Native Alpine morph retains Pacific timestamps across polls and DST.');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


@unittest.skipUnless(os.environ.get('PHP_BINARY') and os.environ.get('PAYMENTER_TEST_APP'),
                     'PHP_BINARY and PAYMENTER_TEST_APP required')
class PollTimes(unittest.TestCase):
    def setUp(self):
        node = shutil.which(os.environ.get('NODE_BINARY', 'node'))
        if not node:
            self.skipTest('Node required')
        probe = subprocess.run([node, '-e',
                                "const p = require(process.env.PAYMENTER_BACKUPS_PLAYWRIGHT || 'playwright'); "
                                "if (!require('node:fs').existsSync(p.chromium.executablePath())) process.exit(1)"],
                               capture_output=True, text=True)
        if probe.returncode:
            self.skipTest('Playwright and its Chromium browser required')

    def test_staff_times_survive_repeated_poll_morphs_and_changed_instants(self):
        app = Path(os.environ['PAYMENTER_TEST_APP'])
        with tempfile.TemporaryDirectory(prefix='panel-times-') as scratch:
            rendered = subprocess.run(
                [os.environ['PHP_BINARY'], '-r', PHP, str(app),
                 str(ROOT / 'extension/Backups/resources/views/partials/moment.blade.php'), scratch],
                capture_output=True, text=True, check=True)
            result = subprocess.run(
                [os.environ.get('NODE_BINARY', 'node'), '-e', JS],
                input=json.dumps({'rows': json.loads(rendered.stdout),
                                 'livewire': str(app / 'vendor/livewire/livewire/dist/livewire.js')}),
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
