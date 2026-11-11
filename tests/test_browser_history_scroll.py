"""Exercise the populated phone history assertions against browser geometry."""
import json
import os
from pathlib import Path
import subprocess
import unittest

from panel_browser import browser_node

ROOT = Path(__file__).resolve().parents[1]


class PhoneHistoryScrollChecks(unittest.TestCase):
    def test_history_check_requires_overflow_and_scroll_movement(self):
        node = browser_node(self)
        script = (ROOT / 'tools/browser_ui_checks.mjs').read_text()
        start = script.index("      const surface = await mobile.locator('.backups-history')")
        end = script.index('      const mobileLink =', start)
        # Execute the actual browser check, including its assertions, on both layouts.
        check = script[start:end] + '\nreturn surface;'
        result = subprocess.run([node, '--input-type=module', '-e', r'''
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PAYMENTER_BACKUPS_PLAYWRIGHT || 'playwright');
const browser = await chromium.launch({headless:true});
try {
  const mobile = await browser.newPage({viewport:{width:390,height:844}});
  const check = new (Object.getPrototypeOf(async function(){}).constructor)('mobile', 'assert', process.argv[1]);
  const results = [];
  for (const width of [280, 900]) {
    await mobile.setContent(`<style>
      .fi-ta-content-ctn {width:320px;overflow-x:auto}
      table {width:${width}px;table-layout:fixed;border-spacing:0}
    </style><div class="backups-history"><div class="fi-ta-content-ctn">
      <table class="fi-ta-table"><thead><tr><th>Type</th><th>Size</th><th>Location</th></tr></thead>
      <tbody><tr><td>Database</td><td>1 MB</td><td>This server</td></tr></tbody></table>
    </div></div>`);
    if (process.env.BACKUPS_RENDER_DIR) {
      const directory = process.env.BACKUPS_RENDER_DIR;
      await mkdir(directory, {recursive:true});
      await writeFile(path.join(directory, `history-${width}.html`), await mobile.content());
      await mobile.screenshot({path:path.join(directory, `history-${width}.png`)});
    }
    try { results.push({width, surface:await check(mobile, assert)}); }
    catch (error) { results.push({width, error:error.message}); }
  }
  console.log(JSON.stringify(results));
} finally { await browser.close(); }
''', check], cwd=ROOT, env=os.environ.copy(), text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fitting, overflowing = json.loads(result.stdout)
        self.assertIn('error', fitting, 'A fitting table must fail the phone scroll check')
        self.assertIn('does not overflow its card', fitting['error'])
        self.assertNotIn('error', overflowing, overflowing)
        surface = overflowing['surface']
        self.assertGreater(surface['tableWidth'], surface['cardWidth'])
        self.assertGreater(surface['scrollAfter'], surface['scrollBefore'])


if __name__ == '__main__':
    unittest.main()
