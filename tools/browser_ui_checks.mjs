#!/usr/bin/env node
/** Real browser checks for a disposable native fixture. No package or browser is installed by this script.
 * node tools/browser_ui_checks.mjs --workroot /path/to/TMPDIR/paymenter-backups-ui-ID/ui-ID --base-url http://127.0.0.1:PORT
 *   --package-path /path/to/playwright --phase inspect|defaults|validation|secrets|recovery|lifecycle|s3|last-good-edge|all
 * Optional: --output /private/output --browser-library-path /private/extracted/libs
 * Mutating phases require --allow-fixture-mutations. Fixture helpers launch only exact request IDs.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, writeFile, mkdir, readdir, stat, lstat, realpath } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { hasSafeTemporaryAncestors } from './fixture_modes.mjs';
const run = promisify(execFile);
const args = {};
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i].replace(/^--/, '');
  if (key === 'allow-fixture-mutations') args[key] = true;
  else args[key] = process.argv[++i];
}
for (const key of ['workroot', 'base-url', 'package-path']) assert(args[key], `Missing --${key}`);
const root = path.resolve(import.meta.dirname, '..');
const work = path.resolve(args.workroot);
const workbase = path.dirname(work);
const temporary = await realpath(process.env.TMPDIR || tmpdir());
assert.equal(path.dirname(workbase), temporary);
assert.equal(await hasSafeTemporaryAncestors(temporary), true,
  'Temporary parent and ancestors must be trusted directories');
assert(path.basename(workbase).startsWith('paymenter-backups-ui-'), 'Require a private random UI fixture base');
assert(path.basename(work).startsWith('ui-'), 'Require a disposable ui- fixture');
for (const directory of [workbase, work]) {
  const info = await lstat(directory);
  assert(info.isDirectory() && !info.isSymbolicLink(), 'Fixture directories must not be links');
  assert.equal(info.uid, process.getuid(), 'Fixture directories must belong to this user');
  assert.equal(info.mode & 0o077, 0, 'Fixture directories must be owner-private');
}
const metadata = JSON.parse(await readFile(path.join(work, 'ui-fixture.json'), 'utf8'));
assert.equal(metadata.work, work);
assert.equal(metadata.app, path.join(work, 'app'));
assert.equal(metadata.private_root, path.join(work, 'private'));
assert.equal(metadata.fixture_only, true);
assert.equal(metadata.native_scheduler_started, false);
assert.equal(args['base-url'].replace(/\/$/, ''), metadata.url);
assert((await readFile(`/proc/${metadata.owner_pid}/cmdline`, 'utf8')).includes('native_ui_fixture.py'), 'Fixture owner must be alive');
const phase = args.phase || 'inspect';
assert(['inspect', 'defaults', 'validation', 'secrets', 'recovery', 'lifecycle', 's3', 'last-good-edge', 'all'].includes(phase));
if (phase !== 'inspect') assert(args['allow-fixture-mutations'], 'Mutating phases require --allow-fixture-mutations');
const output = path.resolve(args.output || path.join(work, `browser-${phase}`));
assert(output.startsWith(work + path.sep), 'Evidence must remain inside ignored fixture');
await mkdir(output, { recursive: true, mode: 0o700 });
const { chromium } = createRequire(import.meta.url)(path.resolve(args['package-path']));
const browser = await chromium.launch({headless: true, env: {...process.env,
  ...(args['browser-library-path'] ? {LD_LIBRARY_PATH: path.resolve(args['browser-library-path'])} : {})}});
const report = {phase, candidate: metadata.release_sha256, harnessSha256: createHash('sha256').update(await readFile(import.meta.filename)).digest('hex'), browserVersion: browser.version(), started: new Date().toISOString(), checks: [], screenshots: [], browserErrors: []};
const contexts = [];
async function check(name, callback) {
  try { const evidence = await callback(); report.checks.push({name, passed: true, evidence}); console.log(`PASS ${name}`); }
  catch (error) { report.checks.push({name, passed: false, error: error.message}); console.log(`FAIL ${name}: ${error.message}`); }
}
async function login(email = metadata.admin_email, viewport = {width: 1440, height: 1000}, colorScheme = 'light') {
  const context = await browser.newContext({viewport, colorScheme, hasTouch: viewport.width < 640, acceptDownloads: true}); contexts.push(context);
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  page.on('pageerror', error => report.browserErrors.push(error.message));
  page.on('console', entry => { if (entry.type() === 'error') if (!/Failed to load resource: the server responded with a status of (403|409)/.test(entry.text())) report.browserErrors.push(entry.text()); });
  await page.goto(metadata.login_url);
  if (email === metadata.admin_email && report.screenshots.length === 0) await shot(page, 'login');
  await page.getByLabel('Email', {exact: true}).fill(email);
  await page.getByLabel('Password', {exact: true}).fill(metadata.fake_password);
  await page.getByRole('button', {name: 'Sign in', exact: true}).click();
  await page.waitForURL(url => !url.pathname.endsWith('/login'));
  return page;
}
async function load(page, tab) {
  await page.goto(metadata.admin_url); await page.getByRole('heading', {name: 'Backups', exact: true}).waitFor();
  if (tab) await page.getByRole('tab', {name: tab, exact: true}).click();
  await page.waitForTimeout(650);
}
async function shot(page, name) {
  const file = path.join(output, name + '.png'); await page.screenshot({path: file, fullPage: true}); report.screenshots.push(file);
  if (/create-|help-dialog|confirmation|preview|details/.test(name)) { const viewport = path.join(output, name + '-viewport.png'); await page.screenshot({path: viewport, fullPage: false}); report.screenshots.push(viewport); }
}
async function livewire(page, callback) {
  const updateURI = await page.evaluate(() => document.querySelector('script[data-update-uri]')?.getAttribute('data-update-uri') || window.livewireScriptConfig?.uri);
  assert(updateURI, 'Native Livewire update URI missing from page configuration');
  const updateURL = new URL(updateURI, page.url()).href;
  // Match a request emitted after the interaction, not an older in-flight modal mount.
  const request = page.waitForRequest(r => r.url() === updateURL && r.method() === 'POST');
  const response = request.then(sent => page.waitForResponse(r => r.request() === sent));
  void response.catch(() => {}); // The callback can fail first; keep its pending waiter from aborting the process.
  await callback(); const result = await response; assert.equal(result.status(), 200);
  const body = await result.text(); await page.waitForTimeout(250); return body;
}
const field = (page, key) => page.locator(`[id="settingsForm.${key}"]`);
async function browserState(callback, message) {
  const deadline = Date.now() + 15000;
  while (!await callback()) {
    assert(Date.now() < deadline, message);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
}
// Settings use deferred browser state and visibleJs. Only Save and server actions
// have a Livewire POST; waiting for a POST here hangs legitimate client changes.
async function checked(page, locator, on = true) {
  if (await locator.isChecked() !== on) await locator.setChecked(on);
  await browserState(() => locator.isChecked().then(value => value === on), 'Checkbox state did not settle');
}
async function selectedOption(page, locator, value) {
  if (await locator.inputValue() !== value) await locator.selectOption(value);
  await browserState(() => locator.inputValue().then(current => current === value), 'Select state did not settle');
}
async function visible(locator, shown) { await locator.waitFor({state: shown ? 'visible' : 'hidden'}); }

async function save(page) {
  // Browser input min/required checks are also covered by native attributes. Disable only
  // the HTML gate here to exercise server validation and prove persistence stays atomic.
  await page.locator('form').evaluateAll(forms => forms.forEach(form => form.noValidate = true));
  return livewire(page, () => page.getByRole('button', {name: /^Save (settings|and finish setup)$/}).click());
}
async function snapshot() {
  const {stdout} = await run('mariadb', ['--no-defaults', '--socket=' + metadata.socket, '-uroot', '-N', '-B', 'paymenter_fixture', '-e',
    'SELECT id,`key`,value,type,encrypted,settingable_id,settingable_type FROM settings ORDER BY id']);
  const files = {};
  for (const name of await readdir(metadata.private_root)) {
    if (/credential|secret/i.test(name)) {
      const file = path.join(metadata.private_root, name); if ((await stat(file)).isFile()) files[name] = createHash('sha256').update(await readFile(file)).digest('hex');
    }
  }
  return {rows: stdout, files};
}
async function unchanged(before) { assert.deepEqual(await snapshot(), before, 'Invalid action changed native rows or private credentials'); }
async function control(action, flags = []) {
  if (action === 'worker') flags = [...flags, '--loopback-s3'];
  const {stdout} = await run('python3', [path.join(root, 'tools/native_ui_fixture.py'), action, '--work', work, ...flags], {timeout: 300000, maxBuffer: 1024 * 1024});
  return JSON.parse(stdout);
}
async function nativeOverview() {
  const {stdout} = await run('python3', ['-I', '-B', path.join(metadata.app, 'extensions/Others/Backups/runtime/cli.py'), '--app', metadata.app, '--private-root', metadata.private_root, '--bridge', path.join(metadata.app, 'extensions/Others/Backups/bridge.php'), '--php', metadata.php, 'overview'], {timeout:30000});
  return JSON.parse(stdout);
}
async function localDeletionPreview(runId) {
  const {stdout} = await run('python3', ['-I', '-B', path.join(metadata.app, 'extensions/Others/Backups/runtime/cli.py'), '--app', metadata.app, '--private-root', metadata.private_root, '--bridge', path.join(metadata.app, 'extensions/Others/Backups/bridge.php'), '--php', metadata.php, 'delete', runId, '--location', 'local'], {timeout:30000});
  return JSON.parse(stdout);
}
async function requests() {
  const directory = path.join(metadata.private_root, 'requests');
  const files = await readdir(directory).catch(e => e.code === 'ENOENT' ? [] : Promise.reject(e));
  return Promise.all(files.filter(f => f.endsWith('.json')).map(async name => JSON.parse(await readFile(path.join(directory, name), 'utf8'))));
}
async function newRequest(before, action) {
  const old = new Set(before.map(item => item.request_id));
  let added = [];
  for (let attempt = 0; attempt < 20; attempt++) {
    added = (await requests()).filter(item => !old.has(item.request_id) && item.action === action);
    if (added.length) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.equal(added.length, 1, 'Exactly one durable request expected'); assert.equal(added[0].status, 'pending'); return added[0];
}
async function menu(page, row, name) {
  await row.getByRole('button', {name: 'More actions', exact: true}).click();
  await livewire(page, () => page.getByRole('button', {name, exact: true}).filter({visible: true}).click());
  const modal = page.locator('.fi-modal.fi-modal-open');
  if (['Details', 'Delete'].includes(name)) await modal.locator('.fi-modal-heading').waitFor();
  if (name === 'Delete') { const local = modal.getByRole('radio', {name:'This server',exact:true}); if (await local.count() && !await local.isChecked()) await livewire(page, () => local.check()); }
}
async function closeDialog(page) {
  const modal = page.locator('.fi-modal.fi-modal-open'); const id = await modal.getAttribute('id');
  await modal.getByRole('button', {name: /^(Close|Cancel)$/}).last().click();
  await page.locator(`[id="${id}.heading"]`).waitFor({state: 'hidden'});
}
async function archivePath(runId) {
  // New installations keep backups in DATA_DIR/backups; earlier ones in DATA_DIR/artifacts.
  for (const directory of ['backups', 'artifacts']) {
    const candidate = path.join(metadata.private_root, directory, runId + '.tar.gz');
    if (await stat(candidate).then(() => true, () => false)) return candidate;
  }
  return path.join(metadata.private_root, 'backups', runId + '.tar.gz');
}
async function setToggle(page, locator, on) {
  if ((await locator.getAttribute('aria-checked')) !== String(on)) await locator.click();
  await browserState(() => locator.getAttribute('aria-checked').then(value => value === String(on)), 'Toggle state did not settle');
  if (await locator.getAttribute('id') === 'settingsForm.s3_encryption') await visible(field(page, 'plaintext_offsite_acknowledged'), !on);
}
async function uploadToS3(page) {
  await checked(page, field(page, 'upload_s3'));
  await field(page, 's3_bucket').waitFor();
}
async function submitDatabaseBackup(page) {
  const dialog = page.locator('.fi-modal.fi-modal-open');
  const database = dialog.locator('input[type=radio][value="database-online"]');
  assert.equal(await database.isEnabled(), true, 'Database backup must be admissible');
  if (!await database.isChecked()) await livewire(page, () => database.check());
  assert.equal(await database.isChecked(), true, 'Manual captures must explicitly select Database');
  await livewire(page, () => dialog.getByRole('button', {name: 'Create backup', exact: true}).click());
}
async function findRow(page, runId) {
  // Filament's stable record key identifies the same row before and after retrieval;
  // a remote-only backup deliberately has no Download anchor yet.
  return page.locator(`tr[wire\\:key$=".table.records.${runId}"]`).first();
}
try {
  const page = await login(); await load(page);
  if (phase === 'defaults' || phase === 'all') {
    await check('native defaults preserve 10 GiB budget and 1 GiB reserve', async () => {
      const installedSettings = await readFile(path.join(metadata.app, 'extensions/Others/Backups/Settings.php'), 'utf8');
      assert.match(installedSettings, /'local_byte_budget'\s*=>\s*10737418240/);
      assert.match(installedSettings, /'minimum_free_bytes'\s*=>\s*1073741824/);
      await load(page, 'Settings'); await field(page, 'local_byte_budget').fill('10'); await field(page, 'minimum_free_bytes').fill('1');
      const before = await snapshot(); await save(page);
      assert.deepEqual((await snapshot()).files, before.files, 'Saving nonsecrets changed encrypted credentials');
      await load(page, 'Settings'); assert.equal(await field(page, 'local_byte_budget').inputValue(), '10'); assert.equal(await field(page, 'minimum_free_bytes').inputValue(), '1');
      await shot(page, 'settings-default-space'); await load(page); assert.match(await page.locator('main').innerText(), /of 10 GiB/); await shot(page, 'overview-default-space');
      return {budgetBytes: 10737418240, reserveBytes: 1073741824, budgetGiB: 10, reserveGiB: 1};
    });
  }
  if (phase === 'inspect' || phase === 'all') {
    await check('default directory is the placeholder and setup names the cron line', async () => {
      await load(page, 'Settings');
      const placeholder = await field(page, 'local_path').getAttribute('placeholder');
      assert(placeholder === metadata.private_root + '/backups' || placeholder === metadata.private_root + '/artifacts', `Unexpected default directory ${placeholder}`);
      await load(page, 'Backups'); const setup = page.locator('[id="setup"]');
      assert.equal(await setup.isVisible(), true, 'Setup block hidden while the cron job never ran');
      const text = await setup.innerText();
      assert.doesNotMatch(text, /need \d+ (items|changes)/, 'Setup counts items instead of naming them');
      assert.match(text, /schedule:run|\/usr\/local\/bin\/php \/opt\/paymenter\/scheduler\.php/, 'Setup does not give the cron line');
      const groups = setup.locator('fieldset');
      const full = groups.filter({has:page.locator('legend').filter({hasText:/^Full backups$/})});
      const database = groups.filter({has:page.locator('legend').filter({hasText:/^Database backups$/})});
      assert.equal(await full.count(), 1); assert.equal(await database.count(), 1);
      assert.match(await database.innerText(), /Ready|Not ready/);
      assert.doesNotMatch(await database.innerText(), /maintenance|queue worker|No other program|Redis/i, 'Database readiness leaked full-only requirements');
      assert.equal(await page.getByRole('link', {name: 'Paymenter scheduler setup', exact: true}).count(), 0, 'External scheduler link returned');
      await shot(page, 'setup-readiness');
      return {defaultDirectory: placeholder, cronLineShown: true};
    });
    await check('desktop light, dark, mobile tabs and overflow', async () => {
      for (const scheme of ['light', 'dark']) {
        await page.evaluate(theme => {localStorage.setItem('theme', theme); document.documentElement.classList.toggle('dark', theme === 'dark');}, scheme);
        for (const tab of ['Backups', 'Settings', 'Help']) {
          await load(page, tab); await shot(page, `desktop-${scheme}-${tab.toLowerCase()}`);
          assert.equal(await page.getByRole('tab', {name: tab, exact: true}).getAttribute('aria-selected'), 'true');
        }
      }
      const mobile = await login(metadata.admin_email, {width: 390, height: 844});
      for (const tab of ['Backups', 'Settings', 'Help']) {
        await load(mobile, tab); await shot(mobile, `mobile-${tab.toLowerCase()}`);
        const geometry = await mobile.evaluate(() => ({viewport: innerWidth, scroll: document.documentElement.scrollWidth, bodyLeft: document.body.getBoundingClientRect().left}));
        assert(geometry.scroll <= geometry.viewport + 1, `Horizontal page overflow on ${tab}: ${JSON.stringify(geometry)}`);
        assert(geometry.bodyLeft >= -1, `Page clipped on ${tab}`);
      }
      await mobile.context().close(); return 'Fresh mobile context; three tabs at 390px; desktop light and dark';
    });
    await check('mobile backup actions fit the visible table', async () => {
      const mobile = await login(metadata.admin_email, {width: 390, height: 844}); await load(mobile);
      const geometry = await mobile.getByRole('button', {name: 'More actions', exact: true}).evaluateAll(buttons => buttons.map(button => {
        const rect = button.getBoundingClientRect();
        const clips = []; let ancestor = button.parentElement;
        while (ancestor) {
          if (['auto', 'hidden', 'scroll', 'clip'].includes(getComputedStyle(ancestor).overflowX)) clips.push(ancestor.getBoundingClientRect());
          ancestor = ancestor.parentElement;
        }
        return {left: rect.left, right: rect.right, viewport: innerWidth, clipLeft: Math.max(0, ...clips.map(clip => clip.left)), clipRight: Math.min(innerWidth, ...clips.map(clip => clip.right))};
      }));
      await shot(mobile, 'mobile-action-containment');
      await mobile.context().close();
      for (const action of geometry) assert(action.left >= action.clipLeft - 1 && action.right <= action.clipRight + 1, `More actions is clipped: ${JSON.stringify(action)}`);
      return geometry;
    });
    await check('one Save and independent per-type schedule choices stay in browser state', async () => {
      await load(page, 'Settings'); const before = await snapshot();
      assert.equal(await page.getByRole('button', {name: /^Save (settings|and finish setup)$/}).count(), 1);
      await selectedOption(page, field(page, 'full_schedule'), 'weekly');
      await visible(field(page, 'full_time'), true);
      await selectedOption(page, field(page, 'database_schedule'), 'hourly');
      await visible(field(page, 'database_every_hours'), true); await visible(field(page, 'database_minute'), true);
      await visible(field(page, 'database_time'), false);
      await selectedOption(page, field(page, 'database_every_hours'), '2'); await field(page, 'database_minute').fill('15');
      assert.equal(await field(page, 'full_schedule').inputValue(), 'weekly');
      await uploadToS3(page);
      assert.equal(await page.getByRole('button', {name: /^Save (settings|and finish setup)$/}).count(), 1, 'S3 fields added another Save');
      await visible(page.locator('[id="s3KeysForm.access_key"]'), true);
      await unchanged(before); await shot(page, 'per-type-schedule-choices');
      await load(page, 'Settings');
      return {singleSave: true, independentBrowserChoices: true, deferredPersistence: true};
    });
    await check('native keyboard focus', async () => {
      await load(page, 'Settings'); await page.getByRole('tab', {name: 'Settings', exact: true}).focus(); await page.keyboard.press('Tab');
      const focus = await page.evaluate(() => ({tag: document.activeElement.tagName, outline: getComputedStyle(document.activeElement).outlineStyle, shadow: getComputedStyle(document.activeElement).boxShadow, background: getComputedStyle(document.activeElement).backgroundColor, focusVisible: document.activeElement.matches(':focus-visible')}));
      assert.notEqual(focus.tag, 'BODY'); await shot(page, 'keyboard-focus');
      assert(focus.focusVisible, 'Keyboard tab did not produce focus-visible');
      assert(focus.outline !== 'none' || focus.shadow !== 'none' || !['rgba(0, 0, 0, 0)', 'transparent'].includes(focus.background), 'Keyboard focus has no visible native indicator');
      return focus;
    });
    await check('help commands use HTTP manual selection or actual secure clipboard', async () => {
      await load(page, 'Help');
      for (const name of ['Command line', 'Restoring']) await page.getByRole('heading', {name, exact:true}).click();
      const commands = page.locator('[data-backups-command]:visible');
      const commandText = (await commands.allTextContents()).join('\n');
      assert.match(commandText, /schedule:run|\/usr\/local\/bin\/php \/opt\/paymenter\/scheduler\.php/, 'Missing scheduler command workflow');
      for (const workflow of ['paymenter_backups()', 'overview --offline', 'recover']) assert(commandText.includes(workflow), 'Missing command workflow: ' + workflow);
      const canCopy = await commands.first().getAttribute('role') === 'button';
      if (!canCopy) {
        assert.match(await page.locator('main').innerText(), /Copying needs HTTPS/);
        for (const command of await commands.all()) {
          assert.equal(await command.getAttribute('role'), null, 'HTTP offers a broken copy button');
          const expected = (await command.textContent()).trim();
          const selected = await command.evaluate(code => {
            const range = document.createRange(); range.selectNodeContents(code);
            const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
            return selection.toString().trim();
          });
          assert.equal(selected, expected, 'Manual selection changed command text');
        }
        await shot(page, 'http-manual-command-selection');
        return {commands: await commands.count(), manualCopyOnHttp: true, clipboardVerified: false};
      }
      assert(await page.evaluate(() => isSecureContext && typeof navigator.clipboard?.readText === 'function'), 'Secure clipboard API unavailable');
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], {origin: new URL(page.url()).origin});
      for (let index = 0; index < await commands.count(); index++) {
        const command = commands.nth(index); const expected = (await command.textContent()).trim();
        await command.focus(); await page.keyboard.press(index % 2 ? 'Space' : 'Enter');
        assert.equal(await page.evaluate(() => navigator.clipboard.readText()), expected, 'Actual keyboard clipboard changed command text');
        await command.click(); assert.equal(await page.evaluate(() => navigator.clipboard.readText()), expected, 'Actual click clipboard changed command text');
        assert.equal(await command.getAttribute('tabindex'), '0', 'Command cannot receive keyboard focus');
      }
      const functionText = (await commands.allTextContents()).find(text => text.includes('paymenter_backups()'));
      assert(functionText && functionText.includes('"$@"'), 'Shell function argument quoting missing');
      return {commands: await commands.count(), clipboardVerified: true, actualKeyboardAndClickCopy: true};
    });
    await check('help stays inline and site password purpose stays readable', async () => {
      for (const viewport of [{width:1440,height:1000}, {width:390,height:844}]) {
        const client = await login(metadata.admin_email, viewport); await load(client, 'Settings');
        assert.equal(await client.getByRole('button', {name: /^Help for /}).count(), 0, 'A field still opens a help dialog');
        assert.equal(await client.getByLabel('Backup directory', {exact:true}).count(), 1);
        const heading = client.getByRole('heading', {name:'Site password (HTTP Basic)',exact:true});
        if (!await heading.isVisible()) await client.getByRole('heading', {name:'Full backup requirements',exact:true}).click();
        await heading.waitFor(); const section = client.locator('.fi-section').filter({has:heading}).last();
        assert.match(await section.innerText(), /asks for a username and password[\s\S]*maintenance page/);
        await shot(client, viewport.width < 640 ? 'mobile-site-password-collapsed' : 'desktop-site-password-collapsed');
        await heading.click(); await client.waitForTimeout(300);
        assert.match(await section.innerText(), /uses http:\/\/|Saved for|No site password saved/);
        await shot(client, viewport.width < 640 ? 'mobile-site-password-expanded' : 'desktop-site-password-expanded');
        await client.context().close();
      }
    });
    await check('settings and create-backup mode explanations', async () => {
      await load(page, 'Settings');
      assert.match(await page.locator('main').innerText(), /maintenance page|site pauses/i);
      assert.match(await page.locator('main').innerText(), /stays online/i);
      for (const tab of ['Backups', 'Settings', 'Help']) {
        await load(page, tab); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
        const dialog = page.locator('.fi-modal.fi-modal-open'); await dialog.getByRole('heading', {name: 'Create backup', exact: true}).waitFor();
        await shot(page, 'create-from-' + tab.toLowerCase());
        assert.match(await dialog.innerText(), /maintenance page|site pauses/i); assert.match(await dialog.innerText(), /stays online/i);
        await closeDialog(page);
      }
    });
  }
  if (phase === 'validation' || phase === 'all') {
    await check('positive integer server validation preserves all native rows', async () => {
      for (const value of ['0', '-1', '1.5']) {
        await load(page, 'Settings'); const before = await snapshot();
        await field(page, 'retention_keep_full').fill(value); await save(page); await unchanged(before);
        assert.match(await page.locator('main').innerText(), /at least 1|greater than zero|integer|whole number/i);
      }
      return '0, negative and fractional Backups to keep rejected before persistence';
    });
    await check('independent schedules persist hourly database choices in one Save', async () => {
      await load(page, 'Settings');
      await selectedOption(page, field(page, 'full_schedule'), 'off');
      await selectedOption(page, field(page, 'database_schedule'), 'hourly');
      await visible(field(page, 'database_every_hours'), true);
      await selectedOption(page, field(page, 'database_every_hours'), '2'); await field(page, 'database_minute').fill('15');
      await save(page); await load(page, 'Settings');
      assert.equal(await field(page, 'full_schedule').inputValue(), 'off');
      assert.equal(await field(page, 'database_schedule').inputValue(), 'hourly');
      assert.equal(await field(page, 'database_every_hours').inputValue(), '2');
      assert.equal(await field(page, 'database_minute').inputValue(), '15');
      // The fixture never enumerates schedules, and leaves both off for subsequent work.
      await selectedOption(page, field(page, 'full_schedule'), 'off'); await selectedOption(page, field(page, 'database_schedule'), 'off'); await save(page);
      return {full:'off', database:'hourly', everyHours:2, minute:15, schedulesResetOff:true};
    });
    await check('weekly schedule requires a selected day before persistence', async () => {
      await load(page, 'Settings'); const before = await snapshot();
      await selectedOption(page, field(page, 'full_schedule'), 'weekly');
      // Database backups are off in the fixture, so these are the full backup days.
      for (const checkbox of await page.getByRole('checkbox', {name: /^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$/}).all()) await checkbox.uncheck();
      await save(page); await unchanged(before); assert.match(await page.locator('main').innerText(), /Choose at least one day/i);
    });
    await check('invalid S3 settings preserve native rows and credential file', async () => {
      for (const invalid of ['endpoint', 'prefix', 'acknowledgement']) {
        await load(page, 'Settings'); const before = await snapshot();
        await uploadToS3(page);
        await field(page, 's3_bucket').fill('fake-browser-bucket');
        await field(page, 's3_endpoint').fill(invalid === 'endpoint' ? 'http://fake.example.invalid?query=bad' : 'https://fake.example.invalid');
        await field(page, 's3_prefix').fill(invalid === 'prefix' ? '../wrong' : 'fixture');
        await setToggle(page, field(page, 's3_encryption'), false);
        await page.getByRole('checkbox', {name: 'I understand backups in S3 won\'t be encrypted', exact: true}).setChecked(invalid !== 'acknowledgement');
        await save(page); await unchanged(before);
        assert.match(await page.locator('main').innerText(), /https:\/\/ API URL|can't contain|Confirm this/i);
      }
    });
  }
  if (phase === 'validation' || phase === 'all') {
    await check('S3 count, age and keep-all expose only relevant retention fields', async () => {
      await load(page,'Settings'); const before = await snapshot();
      await uploadToS3(page);
      const policy = field(page,'s3_retention_policy_full');
      for (const selected of ['count','age','keep-all','local-count']) {
        await selectedOption(page, policy, selected);
        await visible(field(page,'s3_retention_keep_full'), selected === 'count');
        await visible(field(page,'s3_retention_days_full'), selected === 'age');
        await shot(page,'s3-retention-'+selected);
      }
      await unchanged(before);
    });
    await check('invalid independent S3 retention values fail before native persistence', async () => {
      for (const [policy,key] of [['count','s3_retention_keep_database'],['age','s3_retention_days_database']]) {
        await load(page,'Settings'); const before = await snapshot();
        await uploadToS3(page);
        await selectedOption(page, field(page,'s3_retention_policy_database'), policy);
        await field(page,'s3_bucket').fill('fake-browser-bucket'); await setToggle(page, field(page, 's3_encryption'), false);
        await page.getByRole('checkbox',{name:'I understand backups in S3 won\'t be encrypted',exact:true}).check();
        await field(page,key).fill('0'); await save(page); await unchanged(before);
        assert.match(await page.locator('main').innerText(),/at least 1|greater than zero/i);
      }
    });
  }
  if (phase === 'secrets' || phase === 'all') {
    await check('secret canaries clear after validation and unrelated Livewire response; one Save stores keys', async () => {
      await load(page, 'Settings'); await uploadToS3(page);
      if (await page.getByText('Keys saved.', {exact: true}).isVisible()) {
        // Only this explicitly disposable fixture is accepted above. Reconcile fake
        // keys left by an interrupted earlier secret check through the real UI.
        await livewire(page, () => page.getByRole('button', {name: 'Remove saved keys', exact: true}).click());
        await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Remove keys', exact: true}).click());
        await load(page, 'Settings'); await uploadToS3(page);
      }
      await field(page, 's3_bucket').fill('fake-browser-bucket');
      await setToggle(page, field(page, 's3_encryption'), false);
      await page.getByRole('checkbox', {name: 'I understand backups in S3 won\'t be encrypted', exact: true}).check();
      const access = page.locator('[id="s3KeysForm.access_key"]'); const secret = page.locator('[id="s3KeysForm.secret_key"]');
      const canary = 'FAKE-BROWSER-SECRET-CANARY-42'; await access.fill(canary);
      await selectedOption(page, field(page, 's3_retention_policy_full'), 'count');
      await visible(field(page, 's3_retention_keep_full'), true); assert.equal(await access.inputValue(), canary, 'Client choice cleared typed key');
      const before = await snapshot();
      let response = await save(page);
      assert(!response.includes(canary), 'Secret echoed in response'); assert.equal(await access.inputValue(), ''); assert.equal(await secret.inputValue(), '');
      await unchanged(before);
      assert.match(await page.locator('main').innerText(), /Enter your secret access key/);
      await access.fill(canary); await secret.fill(canary);
      response = await livewire(page, () => page.getByRole('button', {name:'Create backup',exact:true}).first().click());
      assert(!response.includes(canary), 'Unrelated server response echoed secret');
      assert.equal(await access.inputValue(), ''); assert.equal(await secret.inputValue(), ''); await closeDialog(page);
      await access.fill(canary); await secret.fill(canary);
      response = await save(page);
      assert(!response.includes(canary), 'Saved secret echoed in response');
      await load(page, 'Settings');
      assert.equal(await page.getByText('Keys saved.', {exact: true}).isVisible(), true, 'Saved keys were not acknowledged by native UI');
      assert.equal(await access.getAttribute('placeholder'), 'Saved. Leave empty to keep.');
      assert.equal(await access.inputValue(), ''); assert.equal(await secret.inputValue(), '');
      const credentialNames = (await readdir(metadata.private_root)).filter(name => /credential|secret/i.test(name));
      assert(credentialNames.length, 'Encrypted credential file missing after save');
      for (const name of credentialNames) {
        const file = path.join(metadata.private_root, name);
        if ((await stat(file)).isFile()) assert(!(await readFile(file, 'utf8')).includes(canary), 'Plaintext canary persisted');
      }
      // A later settings-only save keeps the keys without asking again.
      const keyFiles = (await snapshot()).files; await field(page, 's3_region').fill('eu-west-1'); await save(page);
      assert.deepEqual((await snapshot()).files, keyFiles, 'Settings-only save rewrote saved keys');
      await livewire(page, () => page.getByRole('button', {name: 'Remove saved keys', exact: true}).click());
      let removal = page.locator('.fi-modal.fi-modal-open');
      await removal.getByRole('heading', {name: 'Remove saved S3 keys?', exact: true}).waitFor();
      assert.match(await removal.innerText(), /Backups already in S3 are kept/); await shot(page, 'remove-keys-confirmation');
      const beforeCancel = await snapshot(); await closeDialog(page); await unchanged(beforeCancel);
      await livewire(page, () => page.getByRole('button', {name: 'Remove saved keys', exact: true}).click());
      removal = page.locator('.fi-modal.fi-modal-open'); await removal.getByRole('heading', {name: 'Remove saved S3 keys?', exact: true}).waitFor();
      const rowsBeforeRemoval = (await snapshot()).rows;
      await livewire(page, () => removal.getByRole('button', {name: 'Remove keys', exact: true}).click());
      assert.equal((await snapshot()).rows, rowsBeforeRemoval, 'Removing keys changed native settings');
      await load(page, 'Settings'); await uploadToS3(page);
      assert.equal(await page.getByText('Keys saved.', {exact: true}).isVisible(), false, 'Keys remain configured after removal');
      // Turn uploads off again so later phases start from local storage.
      await load(page, 'Settings'); await checked(page, field(page, 'upload_s3'), false); await visible(field(page, 's3_bucket'), false); await save(page);
      // The fixture address is plain http: no site password form is offered, and the reason is shown.
      await load(page, 'Settings');
      const heading = page.getByRole('heading', {name:'Site password (HTTP Basic)',exact:true});
      if (!await heading.isVisible()) await page.getByRole('heading', {name:'Full backup requirements',exact:true}).click();
      await heading.click();
      assert.match(await page.locator('main').innerText(), /Your App URL uses http:\/\/\. A site password can only be sent over https:\/\//);
      assert.equal(await page.locator('[id="siteAccessForm.username"]').count(), 0, 'Site password form offered over http');
    });
  }
  if (phase === 'secrets' || phase === 'recovery' || phase === 'all') {
    await check('encrypted one Save offers a recovery key; download and checkbox confirmation activate it', async () => {
      await load(page, 'Settings'); await uploadToS3(page);
      await field(page, 's3_bucket').fill('fake-browser-bucket'); await field(page, 's3_endpoint').fill('https://browser-s3.invalid');
      await field(page, 's3_region').fill('us-east-1'); await field(page, 's3_prefix').fill('browser-fixture');
      await page.locator('[id="s3KeysForm.access_key"]').fill('fake-browser-access');
      await page.locator('[id="s3KeysForm.secret_key"]').fill('fake-browser-secret');
      await setToggle(page, field(page, 's3_encryption'), true);
      const existingKey = await page.getByRole('button', {name:'Show recovery key',exact:true}).count();
      await save(page);
      if (existingKey) {
        const rotate = page.getByRole('button', {name:'Create a new recovery key',exact:true});
        if (await rotate.count()) {
          await livewire(page, () => rotate.click());
          await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name:'Create key',exact:true}).click());
          await closeDialog(page);
        }
        await livewire(page, () => page.getByRole('button', {name:'Show recovery key',exact:true}).first().click());
      }
      let dialog = page.locator('.fi-modal.fi-modal-open');
      await dialog.getByRole('heading', {name: 'Show the recovery key?', exact:true}).waitFor();
      await livewire(page, () => dialog.getByRole('button', {name:'Show key', exact:true}).click());
      dialog = page.locator('.fi-modal.fi-modal-open'); await dialog.getByRole('heading', {name:'Recovery key',exact:true}).waitFor();
      const secret = (await dialog.locator('[data-backups-command]').innerText()).trim(); assert.match(secret, /^[a-f0-9]{64}$/);
      const downloading = page.waitForEvent('download'); void downloading.catch(() => {});
      await livewire(page, () => dialog.getByRole('button', {name:'Download recovery key',exact:true}).click());
      const download = await downloading; assert.equal(await download.failure(), null);
      const file = path.join(output, 'recovery-key.json'); await download.saveAs(file);
      assert((await readFile(file, 'utf8')).includes(secret), 'Recovery download omitted the displayed key');
      await livewire(page, () => dialog.getByRole('button', {name:'I saved it',exact:true}).click());
      dialog = page.locator('.fi-modal.fi-modal-open');
      await dialog.getByRole('heading', {name:'Confirm the recovery key is saved',exact:true}).waitFor();
      const before = await snapshot();
      const pendingFingerprint = (await nativeOverview()).configuration.encryption.key.pending_fingerprint;
      assert.match(pendingFingerprint, /^[a-f0-9]{64}$/, 'A specific pending key is required before confirmation');
      await livewire(page, () => dialog.getByRole('button', {name:'Confirm',exact:true}).click()); await unchanged(before);
      assert.equal((await nativeOverview()).configuration.encryption.key.pending_fingerprint, pendingFingerprint, 'Unchecked confirmation activated or replaced the pending key');
      await page.getByText('Save the key first, then check this.', {exact:true}).waitFor();
      // The modal root can have no visible geometry while its fixed-position
      // window is displayed. Assert the actual window and validation feedback.
      const confirmationHeading = page.getByRole('heading', {name:'Confirm the recovery key is saved',exact:true});
      report.recoveryRefusal = await page.locator('.fi-modal').filter({has:confirmationHeading}).evaluate(modal => ({
        openClass: modal.classList.contains('fi-modal-open'), display: getComputedStyle(modal).display,
        rootWidth: modal.getBoundingClientRect().width, rootHeight: modal.getBoundingClientRect().height,
        windowWidth: modal.querySelector('.fi-modal-window').getBoundingClientRect().width,
        visibleValidation: true, pendingFingerprintUnchanged: true,
      }));
      dialog = page.locator('.fi-modal-window').filter({has:confirmationHeading}).filter({visible:true});
      assert.equal(await dialog.isVisible(), true);
      await checked(page, dialog.getByRole('checkbox', {name:'I saved this recovery key outside this server.',exact:true}));
      await livewire(page, () => dialog.getByRole('button', {name:'Confirm',exact:true}).click());
      await load(page, 'Settings');
      const confirmedKey = (await nativeOverview()).configuration.encryption.key;
      assert.equal(confirmedKey.acknowledged, true, 'Checked confirmation did not activate the key');
      assert(!confirmedKey.pending_fingerprint, 'The confirmed pending key remains pending');
      assert.equal(await page.getByRole('button', {name:'I saved it',exact:true}).count(), 0, 'Recovery key still awaits acknowledgement');
      assert.equal(await page.getByRole('button', {name:'Show recovery key',exact:true}).count(), 1);
      await checked(page, field(page, 'upload_s3'), false); await save(page);
      return {recoveryDownload: true, uncheckedConfirmationRefused: true, acknowledged: true};
    });
  }
  if (phase === 'lifecycle' || phase === 'all') {
    await check('create queues, cancellation confirms, exact worker completes, download matches archive', async () => {
      await control('seed-runs', ['--count', '3']); await load(page);
      await page.evaluate(() => {localStorage.setItem('theme', 'light'); document.documentElement.classList.remove('dark');});
      let before = await requests(); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
      let dialog = page.locator('.fi-modal.fi-modal-open'); await submitDatabaseBackup(page);
      const pending = await newRequest(before, 'run'); assert.equal(pending.arguments.mode, 'database-online'); assert.match(await page.locator('main').innerText(), /Waiting|Queued/i);
      await livewire(page, () => page.getByRole('button', {name: 'Cancel', exact: true}).filter({visible: true}).first().click()); dialog = page.locator('.fi-modal.fi-modal-open');
      assert.match(await dialog.innerText(), /hasn't started/i);
      await livewire(page, () => dialog.getByRole('button', {name: 'Cancel backup', exact: true}).click());
      assert.equal((await requests()).find(r => r.request_id === pending.request_id).status, 'cancelled');
      before = await requests(); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
      await submitDatabaseBackup(page);
      const capture = await newRequest(before, 'run'); assert.equal(capture.arguments.mode, 'database-online'); await control('worker', ['--request-id', capture.request_id]); await load(page);
      const completed = (await requests()).find(r => r.request_id === capture.request_id); assert.equal(completed.status, 'complete');
      assert(completed.result?.run_id, 'Worker result must identify its capture');
      const link = page.locator(`a[href$="/${completed.result.run_id}"]`); const href = await link.getAttribute('href');
      const runId = href.split('/').pop(); const archive = await readFile(await archivePath(runId));
      const reply = await page.context().request.get(href); assert.equal(reply.status(), 200);
      assert.match(reply.headers()['content-disposition'], /attachment/); assert.match(reply.headers()['content-disposition'], new RegExp(runId));
      assert.equal(reply.headers()['content-type'], 'application/gzip'); assert.equal(Number(reply.headers()['content-length']), archive.length);
      assert.match(reply.headers()['cache-control'], /no-store/); assert.equal(reply.headers()['x-content-type-options'], 'nosniff'); assert.deepEqual(await reply.body(), archive);
      const row = await findRow(page, runId);
      const beforeVerify = await requests(); await menu(page, row, 'Verify');
      const verification = await newRequest(beforeVerify, 'verify'); assert.equal(verification.arguments.run_id, runId);
      await control('worker', ['--request-id', verification.request_id]); await load(page);
      assert.equal((await requests()).find(item => item.request_id === verification.request_id).status, 'complete');
      await menu(page, await findRow(page, runId), 'Details');
      assert.match(await page.locator('.fi-modal.fi-modal-open').innerText(), /Last verified|Verified/); await shot(page, 'verified-details'); await closeDialog(page);
      await shot(page, 'populated-desktop-light');
      await page.evaluate(() => {localStorage.setItem('theme', 'dark'); document.documentElement.classList.add('dark');});
      await shot(page, 'populated-desktop-dark');
      const mobile = await login(metadata.admin_email, {width: 390, height: 844}); await load(mobile);
      await shot(mobile, 'populated-mobile');
      const mobileLink = mobile.getByRole('link', {name: 'Download', exact: true}).first();
      await mobileLink.scrollIntoViewIfNeeded(); await shot(mobile, 'mobile-history-actions');
      const geometry = await mobile.evaluate(() => ({viewport: innerWidth, scroll: document.documentElement.scrollWidth}));
      assert(geometry.scroll <= geometry.viewport + 1, `Populated mobile overflow: ${JSON.stringify(geometry)}`);
      await mobile.context().close();
      await page.evaluate(() => {localStorage.setItem('theme', 'light'); document.documentElement.classList.remove('dark');});
      report.download = {runId, verificationRequest:verification.request_id, bytes: archive.length, sha256: createHash('sha256').update(archive).digest('hex')};
      return report.download;
    });
    for (const device of ['desktop', 'mobile']) {
      await check(`actual ${device} Download event and archive bytes`, async () => {
        assert(report.download, 'Capture and download route must be verified first');
        const viewport = device === 'mobile' ? {width: 390, height: 844} : {width: 1440, height: 1000};
        const client = await login(metadata.admin_email, viewport); await load(client);
        const link = client.locator(`a[href$="/${report.download.runId}"]`); await link.scrollIntoViewIfNeeded();
        if (device === 'mobile') {
          const row = await findRow(client, report.download.runId);
          const more = row.getByRole('button', {name: 'More actions', exact: true});
          const rect = await more.evaluate(button => button.getBoundingClientRect().toJSON());
          assert(rect.left >= -1 && rect.right <= viewport.width + 1, `Mobile More actions outside viewport: ${JSON.stringify(rect)}`);
          await more.click(); await client.getByRole('button', {name: 'Details', exact: true}).filter({visible: true}).waitFor();
          await shot(client, 'mobile-more-menu'); await client.keyboard.press('Escape');
        }
        const downloading = client.waitForEvent('download'); void downloading.catch(() => {});
        await link.click(); const download = await downloading; assert.equal(await download.failure(), null);
        const file = path.join(output, device + '-download.tar.gz'); await download.saveAs(file);
        const bytes = await readFile(file); assert.equal(bytes.length, report.download.bytes);
        assert.equal(createHash('sha256').update(bytes).digest('hex'), report.download.sha256);
        await shot(client, device + '-after-download'); await client.context().close();
        return {bytes: bytes.length, sha256: report.download.sha256, suggestedFilename: download.suggestedFilename()};
      });
    }
    await check('delete confirmation, queued truthfulness, worker deletion and last-good refusal', async () => {
      await load(page);
      // Remove damaged candidates while a verified survivor exists. An execution-time
      // full check is authoritative; a damaged journal alone is never a good survivor.
      while (await page.getByRole('button', {name: 'Verify', exact: true}).count()) {
        const row = page.locator('tr').filter({has: page.getByRole('button', {name: 'Verify', exact: true})}).first();
        const before = await requests(); await menu(page, row, 'Delete');
        await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Delete', exact: true}).click());
        const pending = await newRequest(before, 'delete'); await control('worker', ['--request-id', pending.request_id]);
        assert.equal((await requests()).find(r => r.request_id === pending.request_id).status, 'complete'); await load(page);
      }
      // Delete all verified rows except the final one through actual UI confirmations.
      while (await page.getByRole('link', {name: 'Download', exact: true}).count() > 1) {
        const link = page.getByRole('link', {name: 'Download', exact: true}).last(); const runId = (await link.getAttribute('href')).split('/').pop();
        const row = await findRow(page, runId); const before = await requests(); await menu(page, row, 'Delete');
        const dialog = page.locator('.fi-modal.fi-modal-open'); const api = await localDeletionPreview(runId); assert.equal(api.refusal,null); assert(api.local_copy && !api.remote_copy); assert.equal(await dialog.getByRole('button',{name:'Delete',exact:true}).isVisible(),true); (report.localDeletionPreviews ||= []).push({runId,api,submitVisible:true,localSelected:await dialog.getByRole('radio',{name:'This server',exact:true}).count() ? await dialog.getByRole('radio',{name:'This server',exact:true}).isChecked() : null}); assert.match(await dialog.innerText(), /The backup file on this server|The backup in S3/); if (await dialog.getByRole('radio', {name:'This server',exact:true}).count()) assert.equal(await dialog.getByRole('radio', {name:'This server',exact:true}).isChecked(),true); await shot(page, 'delete-confirmation');
        const archiveFile = await archivePath(runId);
        await livewire(page, () => dialog.getByRole('button', {name: 'Delete', exact: true}).click());
        const pending = await newRequest(before, 'delete'); assert((await stat(archiveFile)).isFile(), 'Queued delete removed bytes during FPM request');
        await control('worker', ['--request-id', pending.request_id]); assert.equal((await requests()).find(r => r.request_id === pending.request_id).status, 'complete');
        await assert.rejects(stat(archiveFile), {code: 'ENOENT'}); await load(page);
      }
      const link = page.getByRole('link', {name: 'Download', exact: true}).first(); const runId = (await link.getAttribute('href')).split('/').pop();
      const before = await requests(); await menu(page, await findRow(page, runId), 'Delete');
      const dialog = page.locator('.fi-modal.fi-modal-open'); assert.match(await dialog.innerText(), /Can't delete|only usable backup|no other verified/i);
      assert.equal(await dialog.getByRole('button', {name: 'Delete', exact: true}).count(), 0); assert.deepEqual(await requests(), before);
      await shot(page, 'last-good-protection'); await closeDialog(page);
    });
    await check('limited admin gets 403 for page and download', async () => {
      const limited = await login(metadata.limited_email);
      assert.equal((await limited.goto(metadata.admin_url)).status(), 403);
      const href = report.download ? metadata.admin_url + '/download/' + report.download.runId : await page.getByRole('link', {name: 'Download', exact: true}).first().getAttribute('href');
      assert(href, 'A verified download route is needed');
      const response = await limited.context().request.get(href); assert.equal(response.status(), 403);
      await limited.context().close();
    });
    await check('changed archive blocks download and verification finishes failed', async () => {
      await load(page); const link = page.getByRole('link', {name: 'Download', exact: true}).first(); const href = await link.getAttribute('href'); const runId = href.split('/').pop();
      await control('corrupt', ['--run-id', runId]); const response = await page.context().request.get(href, {maxRedirects: 0}); assert.equal(response.status(), 302); assert.match(response.headers().location, /\/admin\/backups$/);
      await load(page); assert.equal(await page.locator(`a[href="${href}"]`).count(), 0);
      const before = await requests(); await livewire(page, () => page.getByRole('button', {name: 'Verify', exact: true}).first().click());
      const task = await newRequest(before, 'verify'); await control('worker', ['--request-id', task.request_id]); await load(page);
      assert.equal((await requests()).find(r => r.request_id === task.request_id).status, 'failed');
      assert.match(await page.locator('main').innerText(), /Damaged|Failed|changed/i); await shot(page, 'corrupted-archive');
    });
  }
  if (phase === 'last-good-edge' || phase === 'all') {
    await check('preview refuses last good with known damaged candidate; deleting damaged preserves good', async () => {
      const active = (await requests()).filter(item => ['pending', 'deferred', 'launching', 'running'].includes(item.status));
      assert.equal(active.length, 0, 'Resolve active requests explicitly before this edge check');
      assert.equal(await page.getByRole('link', {name: 'Download', exact: true}).count(), 0, 'Edge fixture must begin with no good copy');
      assert.equal(await page.getByRole('button', {name: 'Verify', exact: true}).count(), 1, 'Exactly one known damaged copy is required');
      let before = await requests(); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
      await submitDatabaseBackup(page);
      const capture = await newRequest(before, 'run'); assert.equal(capture.arguments.mode, 'database-online'); await control('worker', ['--request-id', capture.request_id]); await load(page);
      assert.equal((await requests()).find(item => item.request_id === capture.request_id).status, 'complete');
      const href = await page.getByRole('link', {name: 'Download', exact: true}).first().getAttribute('href'); const goodId = href.split('/').pop();
      const goodPath = await archivePath(goodId); const bytes = await readFile(goodPath);
      // A copy whose last full check failed is not counted as a survivor, so the preview refuses.
      // Execution-time refusal for undetected damage stays covered by the engine tests.
      before = await requests(); await menu(page, await findRow(page, goodId), 'Delete'); await shot(page, 'damaged-candidate-preview');
      const preview = page.locator('.fi-modal.fi-modal-open'); assert.match(await preview.innerText(), /Can't delete[\s\S]*only usable backup/);
      assert.equal(await preview.getByRole('button', {name: 'Delete', exact: true}).count(), 0); await closeDialog(page);
      assert.deepEqual(await requests(), before); assert.deepEqual(await readFile(goodPath), bytes);
      const damaged = page.locator('tr').filter({has: page.getByRole('button', {name: 'Verify', exact: true})}).first();
      before = await requests(); await menu(page, damaged, 'Delete'); await shot(page, 'delete-damaged-preview');
      await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Delete', exact: true}).click());
      const cleanup = await newRequest(before, 'delete'); await control('worker', ['--request-id', cleanup.request_id]);
      assert.equal((await requests()).find(item => item.request_id === cleanup.request_id).status, 'complete'); assert.deepEqual(await readFile(goodPath), bytes);
      await load(page); assert.equal(await page.getByRole('link', {name: 'Download', exact: true}).count(), 1); await shot(page, 'good-survivor');
      return {capture: capture.request_id, goodId, previewRefused: true, damagedDelete: cleanup.request_id};
    });
  }
  if (phase === 'lifecycle' || phase === 's3' || phase === 'all') {
    await check('Delete old backups now queues cleanup and preserves the newest database backups', async () => {
      await control('seed-runs', ['--count', '3']); await load(page, 'Settings');
      await field(page, 'retention_keep_database').fill('2'); await selectedOption(page, field(page, 'full_schedule'), 'off');
      await selectedOption(page, field(page, 'database_schedule'), 'off'); await save(page); await load(page);
      const before = await requests(); const cleanup = page.getByRole('button', {name:'Delete old backups now',exact:true});
      assert.equal(await cleanup.isEnabled(), true);
      await livewire(page, () => cleanup.click());
      const dialog = page.locator('.fi-modal.fi-modal-open');
      await dialog.getByRole('heading', {name:'Delete old backups now?',exact:true}).waitFor();
      await livewire(page, () => dialog.getByRole('button', {name:'Delete old backups',exact:true}).click());
      const task = await newRequest(before, 'retention'); assert.equal(task.arguments.execute, true);
      await control('worker', ['--request-id', task.request_id]); await load(page);
      assert.equal((await requests()).find(item => item.request_id === task.request_id).status, 'complete');
      assert.equal(await page.getByRole('link', {name:'Download',exact:true}).count(), 2);
      assert.equal(await cleanup.isDisabled(), true); return {request:task.request_id, localDatabaseSurvivors:2};
    });
    await check('Upload to S3 and S3-only Database capture retrieve verified Download bytes', async () => {
      await load(page, 'Settings'); await uploadToS3(page);
      await field(page, 's3_bucket').fill('fake-browser-bucket'); await field(page, 's3_endpoint').fill('https://browser-s3.invalid');
      await field(page, 's3_region').fill('us-east-1'); await field(page, 's3_prefix').fill('browser-fixture');
      await page.locator('[id="s3KeysForm.access_key"]').fill('fake-browser-access');
      await page.locator('[id="s3KeysForm.secret_key"]').fill('fake-browser-secret');
      // In standalone lifecycle there is no prior recovery-key setup. Plaintext
      // acknowledgement still exercises the same transport and durable requests.
      if (phase === 'lifecycle') {
        await setToggle(page, field(page, 's3_encryption'), false);
        await checked(page, field(page, 'plaintext_offsite_acknowledged'));
      }
      await save(page); await load(page);
      const beforeStorage = await requests();
      await livewire(page, () => page.getByRole('button', {name:'Test storage',exact:true}).first().click());
      const storageTest = await newRequest(beforeStorage, 'destination-test');
      await control('worker', ['--request-id', storageTest.request_id]); await load(page);
      const storageCompleted = (await requests()).find(item => item.request_id === storageTest.request_id);
      assert.equal(storageCompleted.status, 'complete'); assert.equal(storageCompleted.result.verified, true);
      assert.equal(storageCompleted.result.destination, 's3');
      const first = page.getByRole('link', {name:'Download',exact:true}).first(); const uploadId = (await first.getAttribute('href')).split('/').pop();
      let before = await requests(); await menu(page, await findRow(page, uploadId), 'Upload to S3');
      const delivery = await newRequest(before, 'deliver'); assert.equal(delivery.arguments.run_id, uploadId);
      await control('worker', ['--request-id', delivery.request_id]);
      assert.equal((await requests()).find(item => item.request_id === delivery.request_id).status, 'complete');
      await load(page, 'Settings'); await checked(page, field(page, 'keep_local'), false); await save(page); await load(page);
      before = await requests(); await livewire(page, () => page.getByRole('button', {name:'Create backup',exact:true}).first().click());
      await submitDatabaseBackup(page); const capture = await newRequest(before, 'run'); assert.equal(capture.arguments.mode, 'database-online');
      await control('worker', ['--request-id', capture.request_id]); await load(page);
      const completed = (await requests()).find(item => item.request_id === capture.request_id); assert.equal(completed.status, 'complete');
      assert(completed.result?.run_id, 'S3-only worker result must identify its capture');
      const captureId = completed.result.run_id;
      const journal = JSON.parse(await readFile(path.join(metadata.private_root, 'runs', captureId, 'journal.json'), 'utf8'));
      assert.equal(journal.mode, 'database-online'); assert.equal(journal.local_complete, false); assert.equal(journal.remote_delivered, true);
      if (phase === 'all' || phase === 's3') assert.equal(journal.remote?.format, 'openpgp-symmetric-aes256-seipd-v1', 'All workflows must exercise encrypted S3 delivery');
      const remote = (await findRow(page, captureId)).getByRole('button', {name:'Download from S3',exact:true});
      assert.equal(await remote.isVisible(), true, 'S3-only capture lacks retrieval action');
      before = await requests(); await livewire(page, () => remote.click());
      const retrieve = await newRequest(before, 'retrieve'); const runId = retrieve.arguments.run_id; assert.equal(runId, captureId);
      await assert.rejects(stat(await archivePath(runId)), {code:'ENOENT'});
      await control('worker', ['--request-id', retrieve.request_id]); await load(page);
      assert.equal((await requests()).find(item => item.request_id === retrieve.request_id).status, 'complete');
      const link = page.locator(`a[href$="/${runId}"]`); await link.waitFor();
      const downloading = page.waitForEvent('download'); void downloading.catch(() => {}); await link.click();
      const download = await downloading; assert.equal(await download.failure(), null);
      const file = path.join(output, 's3-retrieved.tar.gz'); await download.saveAs(file);
      assert.deepEqual(await readFile(file), await readFile(await archivePath(runId)));
      await menu(page, await findRow(page, runId), 'Details');
      assert.match(await page.locator('.fi-modal.fi-modal-open').innerText(), /Downloaded copy kept until/);
      await shot(page, 's3-retrieved-details'); await closeDialog(page);
      await load(page, 'Settings'); await field(page, 'retention_keep_database').fill('1');
      await selectedOption(page, field(page, 's3_retention_policy_database'), 'count');
      await visible(field(page, 's3_retention_keep_database'), true); await field(page, 's3_retention_keep_database').fill('1');
      await save(page); await load(page);
      before = await requests(); await livewire(page, () => page.getByRole('button', {name:'Delete old backups now',exact:true}).click());
      await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name:'Delete old backups',exact:true}).click());
      const retention = await newRequest(before, 'retention'); await control('worker', ['--request-id', retention.request_id]); await load(page);
      const retentionCompleted = (await requests()).find(item => item.request_id === retention.request_id);
      assert.equal(retentionCompleted.status, 'complete');
      assert(retentionCompleted.result.removed.s3.includes(uploadId), 'Independent S3 count did not remove the older uploaded copy');
      assert(!retentionCompleted.result.removed.s3.includes(runId), 'Cleanup removed the newest remote copy');
      assert.deepEqual(await readFile(await archivePath(runId)), await readFile(file), 'Cleanup removed or changed the retrieved copy');
      await page.locator(`a[href$="/${runId}"]`).waitFor();
      await load(page, 'Settings'); await checked(page, field(page, 'keep_local')); await checked(page, field(page, 'upload_s3'), false); await save(page);
      return {storageTest:storageTest.request_id, delivery:delivery.request_id, capture:capture.request_id, retrieve:retrieve.request_id, retention:retention.request_id, runId, remoteFormat:journal.remote?.format, verifiedDownload:true, retrievedSurvivesCleanup:true, loopbackOnly:true};
    });
  }

} finally {
  report.finished = new Date().toISOString();
  const finalMetadata = JSON.parse(await readFile(path.join(work, 'ui-fixture.json'), 'utf8'));
  report.candidateUnchanged = finalMetadata.release_sha256 === report.candidate;
  report.passed = report.checks.every(item => item.passed) && !report.browserErrors.length && report.candidateUnchanged;
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', {mode: 0o600});
  await browser.close(); console.log(`Evidence: ${output}`); if (!report.passed) process.exitCode = 1;
}
