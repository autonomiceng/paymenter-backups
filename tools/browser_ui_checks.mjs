#!/usr/bin/env node
/** Real browser checks for a disposable native fixture. No package or browser is installed by this script.
 * node tools/browser_ui_checks.mjs --workroot /path/to/.native-work/ui-ID --base-url http://127.0.0.1:PORT
 *   --package-path /path/to/playwright --phase inspect|defaults|validation|secrets|lifecycle|last-good-edge|all
 * Optional: --output /private/output --browser-library-path /private/extracted/libs
 * Mutating phases require --allow-fixture-mutations. Fixture helpers launch only exact request IDs.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, writeFile, mkdir, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
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
assert.equal(path.dirname(work), path.join(root, '.native-work'));
assert(path.basename(work).startsWith('ui-'), 'Require a disposable ui- fixture');
const metadata = JSON.parse(await readFile(path.join(work, 'ui-fixture.json'), 'utf8'));
assert.equal(metadata.work, work);
assert.equal(metadata.app, path.join(work, 'app'));
assert.equal(metadata.private_root, path.join(work, 'private'));
assert.equal(metadata.fixture_only, true);
assert.equal(metadata.native_scheduler_started, false);
assert.equal(args['base-url'].replace(/\/$/, ''), metadata.url);
assert((await readFile(`/proc/${metadata.owner_pid}/cmdline`, 'utf8')).includes('native_ui_fixture.py'), 'Fixture owner must be alive');
const phase = args.phase || 'inspect';
assert(['inspect', 'defaults', 'validation', 'secrets', 'lifecycle', 'last-good-edge', 'all'].includes(phase));
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
async function checked(page, locator) { if (!await locator.isChecked()) await livewire(page, () => locator.check()); }
async function selected(page, locator, value) { if (await locator.inputValue() !== value) await livewire(page, () => locator.selectOption(value)); }
async function save(page) {
  // Browser input min/required checks are also covered by native attributes. Disable only
  // the HTML gate here to exercise server validation and prove persistence stays atomic.
  await page.locator('form').evaluateAll(forms => forms.forEach(form => form.noValidate = true));
  return livewire(page, () => page.getByRole('button', {name: 'Save settings', exact: true}).click());
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
  const {stdout} = await run('python3', [path.join(root, 'tools/native_ui_fixture.py'), action, '--work', work, ...flags], {timeout: 300000, maxBuffer: 1024 * 1024});
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
  if (name === 'Delete') { const local = modal.getByRole('radio', {name:'This server only',exact:true}); if (await local.count()) await checked(page, local); }
}
async function closeDialog(page) {
  const modal = page.locator('.fi-modal.fi-modal-open'); const id = await modal.getAttribute('id');
  await modal.getByRole('button', {name: /^(Close|Cancel)$/}).last().click();
  await page.locator(`[id="${id}.heading"]`).waitFor({state: 'hidden'});
}
async function findRow(page, runId) {
  // IDs deliberately do not appear in the first column. The authorized download route
  // identifies its row without exposing implementation-only IDs in the main table.
  return page.locator('tr').filter({has: page.locator(`a[href$="/${runId}"]`)}).first();
}
try {
  const page = await login(); await load(page);
  if (phase === 'defaults' || phase === 'all') {
    await check('native defaults preserve 10 GiB budget and 1 GiB reserve', async () => {
      const installedSettings = await readFile(path.join(metadata.app, 'extensions/Others/Backups/Settings.php'), 'utf8');
      assert.match(installedSettings, /'local_byte_budget'\s*=>\s*10737418240/);
      assert.match(installedSettings, /'minimum_free_bytes'\s*=>\s*1073741824/);
      await load(page, 'Settings'); await field(page, 'local_byte_budget').fill('10240'); await field(page, 'minimum_free_bytes').fill('1024');
      const before = await snapshot(); await save(page);
      assert.deepEqual((await snapshot()).files, before.files, 'Saving nonsecrets changed encrypted credentials');
      await load(page, 'Settings'); assert.equal(await field(page, 'local_byte_budget').inputValue(), '10240'); assert.equal(await field(page, 'minimum_free_bytes').inputValue(), '1024');
      await shot(page, 'settings-default-space'); await load(page); assert.match(await page.locator('main').innerText(), /of 10 GiB/); await shot(page, 'overview-default-space');
      return {budgetBytes: 10737418240, reserveBytes: 1073741824, budgetMiB: 10240, reserveMiB: 1024};
    });
  }
  if (phase === 'inspect' || phase === 'all') {
    await check('default directory is selectable and scheduler link is discoverable', async () => {
      await load(page, 'Settings'); const main = page.locator('main');
      const directory = main.locator('code').filter({hasText: metadata.private_root + '/'}).first();
      await directory.waitFor(); const value = await directory.innerText();
      assert(value === metadata.private_root + '/artifacts' || value === metadata.private_root + '/backups');
      await directory.evaluate(element => {const range=document.createRange();range.selectNodeContents(element);const selection=getSelection();selection.removeAllRanges();selection.addRange(range)});
      assert.equal(await page.evaluate(() => getSelection().toString()), value);
      await load(page, 'Backups'); const scheduler = page.getByRole('link', {name:'Paymenter scheduler setup',exact:true});
      assert.equal(await scheduler.isVisible(), true); assert.equal(await scheduler.getAttribute('href'),'https://paymenter.org/docs/installation/install');
      return {defaultDirectory:value,manualTextSelection:true,schedulerLinkVisible:true};
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
      const commands = page.locator('[data-backups-command]:visible');
      assert((await commands.count()) >= 6, 'Expected scheduler and terminal command blocks');
      const canCopy = await commands.first().getAttribute('role') === 'button';
      if (!canCopy) {
        assert.match(await page.locator('main').innerText(), /Copy needs HTTPS/);
        for (const command of await commands.all()) {
          assert.equal(await command.getAttribute('role'), null, 'HTTP offers a broken copy button');
          assert.equal(await command.locator('code').count(), 1, 'Command is not escaped code');
          const expected = (await command.locator('code').textContent()).trim();
          const selected = await command.locator('code').evaluate(code => {
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
    await check('field help opens by keyboard and closes without submission', async () => {
      await load(page, 'Settings');
      const help = page.getByRole('button', {name: 'Help for Job lock files', exact: true});
      if (!await help.count()) {
        await livewire(page, () => page.locator('[id="settingsForm.mode-full-quiesced"]').check());
      }
      const before = await snapshot();
      await help.focus();
      await livewire(page, () => page.keyboard.press('Enter'));
      const dialog = page.locator('.fi-modal.fi-modal-open');
      await dialog.getByRole('heading', {name: 'Job lock files', exact: true}).waitFor();
      assert.match(await dialog.innerText(), /lock paths/);
      await closeDialog(page);
      await unchanged(before);
      await help.click();
      await dialog.getByRole('heading', {name: 'Job lock files', exact: true}).waitFor();
      await page.keyboard.press('Escape');
      await dialog.locator('.fi-modal-heading').waitFor({state: 'hidden'});
      await unchanged(before);
      return 'Native help supports Enter, click, Close and Escape';
    });
    await check('Directory labels and collapsed HTTP Basic purpose stay readable', async () => {
      for (const viewport of [{width:1440,height:1000}, {width:390,height:844}]) {
        const client = await login(metadata.admin_email, viewport); await load(client, 'Settings');
        assert.equal(await client.getByLabel('Backup directory', {exact:true}).count(), 1);
        assert.equal(await client.getByLabel('Backup folder', {exact:true}).count(), 0, 'Filesystem label still says Folder');
        await livewire(client, () => client.locator('[id="settingsForm.mode-full-quiesced"]').check());
        const heading = client.getByRole('heading', {name:'HTTP Basic authentication (advanced)',exact:true});
        await heading.waitFor(); const section = client.locator('.fi-section').filter({has:heading});
        assert.match(await section.innerText(), /web server password prompt[\s\S]*maintenance check/);
        assert.equal(await client.getByText('These credentials are separate from your Paymenter account login.',{exact:true}).isVisible(), false, 'Advanced HTTP Basic section starts expanded');
        await shot(client, viewport.width < 640 ? 'mobile-http-basic-collapsed' : 'desktop-http-basic-collapsed');
        await heading.click(); await client.getByText('These credentials are separate from your Paymenter account login.',{exact:true}).waitFor();
        assert.match(await section.innerText(), /App URL|https/); await shot(client, viewport.width < 640 ? 'mobile-http-basic-expanded' : 'desktop-http-basic-expanded');
        await client.context().close();
      }
    });
    await check('mobile help tap opens a visible dialog without changing settings', async () => {
      const mobile = await login(metadata.admin_email, {width:390,height:844}); await load(mobile, 'Settings');
      await livewire(mobile, () => mobile.locator('[id="settingsForm.mode-full-quiesced"]').check());
      const before = await snapshot(); await livewire(mobile, () => mobile.getByRole('button',{name:'Help for Job lock files',exact:true}).tap());
      const dialog = mobile.locator('.fi-modal.fi-modal-open'); await dialog.getByRole('heading',{name:'Job lock files',exact:true}).waitFor();
      assert.match(await dialog.innerText(), /flock|lock paths/); await shot(mobile,'mobile-help-dialog'); await closeDialog(mobile); await unchanged(before); await mobile.context().close();
    });
    await check('settings and create-backup mode explanations', async () => {
      await load(page, 'Settings');
      assert.match(await page.locator('main').innerText(), /maintenance page|site pauses/i);
      assert.match(await page.locator('main').innerText(), /stays online/i);
      for (const tab of ['Backups', 'Settings', 'Help']) {
        await load(page, tab); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
        const dialog = page.locator('.fi-modal.fi-modal-open'); await dialog.getByRole('heading', {name: 'Create a backup', exact: true}).waitFor();
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
        await field(page, 'retention_keep').fill(value); await save(page); await unchanged(before);
        assert.match(await page.locator('main').innerText(), /at least 1|greater than zero|integer|whole number/i);
      }
      return '0, negative and fractional Backups to keep rejected before persistence';
    });
    await check('weekly schedule requires a selected day before persistence', async () => {
      await load(page, 'Settings'); const before = await snapshot();
      await livewire(page, () => page.getByRole('switch', {name: 'Create backups automatically', exact: true}).click());
      await livewire(page, () => page.getByRole('radio', {name: 'Weekly', exact: true}).check());
      for (const checkbox of await page.getByRole('checkbox', {name: /^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$/}).all()) await checkbox.uncheck();
      await save(page); await unchanged(before); assert.match(await page.locator('main').innerText(), /Choose at least one day/i);
    });
    await check('invalid S3 settings preserve native rows and credential file', async () => {
      for (const invalid of ['endpoint', 'prefix', 'acknowledgement']) {
        await load(page, 'Settings'); const before = await snapshot();
        await checked(page, page.getByRole('radio', {name: /^This server and S3/}));
        await field(page, 's3_bucket').fill('fake-browser-bucket');
        await field(page, 's3_endpoint').fill(invalid === 'endpoint' ? 'http://fake.example.invalid?query=bad' : 'https://fake.example.invalid');
        await field(page, 's3_prefix').fill(invalid === 'prefix' ? '../wrong' : 'fixture');
        await page.getByRole('checkbox', {name: 'I understand archives in S3 are not encrypted', exact: true}).setChecked(invalid !== 'acknowledgement');
        await save(page); await unchanged(before);
        assert.match(await page.locator('main').innerText(), /https:\/\/ address|prefix parts|folder names|Confirm this/i);
      }
    });
  }
  if (phase === 'validation' || phase === 'all') {
    await check('S3 count, age and keep-all expose only relevant retention fields', async () => {
      await load(page,'Settings'); const before = await snapshot();
      await checked(page, page.getByRole('radio',{name:/^This server and S3/}));
      const policy = field(page,'s3_retention_policy');
      for (const selected of ['count','age','keep-all','local-count']) {
        if (await policy.inputValue() !== selected) await livewire(page, () => policy.selectOption(selected));
        assert.equal(await field(page,'s3_retention_keep').isVisible(), selected === 'count');
        assert.equal(await field(page,'s3_retention_days').isVisible(), selected === 'age');
        await shot(page,'s3-retention-'+selected);
      }
      await unchanged(before);
    });
    await check('invalid independent S3 retention values fail before native persistence', async () => {
      for (const [policy,key] of [['count','s3_retention_keep'],['age','s3_retention_days']]) {
        await load(page,'Settings'); const before = await snapshot();
        await checked(page, page.getByRole('radio',{name:/^This server and S3/}));
        await selected(page, field(page,'s3_retention_policy'), policy);
        await field(page,'s3_bucket').fill('fake-browser-bucket'); await page.getByRole('checkbox',{name:'I understand archives in S3 are not encrypted',exact:true}).check();
        await field(page,key).fill('0'); await save(page); await unchanged(before);
        assert.match(await page.locator('main').innerText(),/at least 1|greater than zero/i);
      }
    });
  }
  if (phase === 'secrets' || phase === 'all') {
    await check('secret canaries clear after validation and unrelated Livewire response', async () => {
      await load(page, 'Settings'); await checked(page, page.getByRole('radio', {name: /^This server and S3/}));
      if (await page.getByRole('button', {name: 'Replace S3 keys', exact: true}).count()) {
        // Only this explicitly disposable fixture is accepted above. Reconcile fake
        // keys left by an interrupted earlier secret check through the real UI.
        await livewire(page, () => page.getByRole('button', {name: 'Remove S3 keys', exact: true}).and(page.locator('button[type="button"]')).click());
        await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Remove S3 keys', exact: true}).click());
      }
      const access = page.locator('[id="s3KeysForm.access_key"]'); const secret = page.locator('[id="s3KeysForm.secret_key"]');
      const canary = 'FAKE-BROWSER-SECRET-CANARY-42'; await access.fill(canary);
      await page.locator('form').evaluateAll(forms => forms.forEach(form => form.noValidate = true));
      let response = await livewire(page, () => page.getByRole('button', {name: 'Save S3 keys', exact: true}).click());
      assert(!response.includes(canary), 'Secret echoed in response'); assert.equal(await access.inputValue(), ''); assert.equal(await secret.inputValue(), '');
      await access.fill(canary); await secret.fill(canary);
      const rowsBeforeKeySave = (await snapshot()).rows;
      response = await livewire(page, () => page.getByRole('button', {name: 'Save S3 keys', exact: true}).click());
      assert(!response.includes(canary), 'Saved secret echoed in response');
      assert.equal(await page.getByRole('button', {name: 'Replace S3 keys', exact: true}).count(), 1, 'Saved keys were not acknowledged by native UI');
      assert.equal(await access.inputValue(), ''); assert.equal(await secret.inputValue(), '');
      assert.equal((await snapshot()).rows, rowsBeforeKeySave, 'Saving keys changed native settings');
      const credentialNames = (await readdir(metadata.private_root)).filter(name => /credential|secret/i.test(name));
      assert(credentialNames.length, 'Encrypted credential file missing after save');
      for (const name of credentialNames) {
        const file = path.join(metadata.private_root, name);
        if ((await stat(file)).isFile()) assert(!(await readFile(file, 'utf8')).includes(canary), 'Plaintext canary persisted');
      }
      await livewire(page, () => page.getByRole('button', {name: 'Remove S3 keys', exact: true}).and(page.locator('button[type="button"]')).click());
      let removal = page.locator('.fi-modal.fi-modal-open');
      await removal.getByRole('heading', {name: 'Remove S3 keys?', exact: true}).waitFor();
      assert.match(await removal.innerText(), /remove|keys/i); await shot(page, 'remove-keys-confirmation');
      const beforeCancel = await snapshot(); await closeDialog(page); await unchanged(beforeCancel);
      assert.equal(await page.getByRole('button', {name: 'Replace S3 keys', exact: true}).count(), 1, 'Cancelling removal lost saved keys');
      await livewire(page, () => page.getByRole('button', {name: 'Remove S3 keys', exact: true}).and(page.locator('button[type="button"]')).click());
      removal = page.locator('.fi-modal.fi-modal-open'); await removal.getByRole('heading', {name: 'Remove S3 keys?', exact: true}).waitFor();
      await livewire(page, () => removal.getByRole('button', {name: 'Remove S3 keys', exact: true}).click());
      assert.equal((await snapshot()).rows, rowsBeforeKeySave, 'Removing keys changed native settings');
      assert.equal(await page.getByRole('button', {name: 'Save S3 keys', exact: true}).count(), 1, 'Keys remain configured after removal');
      await access.fill(canary); await secret.fill(canary);
      response = await save(page); assert(!response.includes(canary)); assert.equal(await access.inputValue(), ''); assert.equal(await secret.inputValue(), '');
      // The fixture address is plain http: no site password form is offered, and the reason is shown.
      await load(page, 'Settings');
      await livewire(page, () => page.locator('[id="settingsForm.mode-full-quiesced"]').check());
      await page.getByRole('heading', {name:'HTTP Basic authentication (advanced)',exact:true}).click();
      assert.match(await page.locator('main').innerText(), /App URL in Paymenter settings uses http:\/\/[\s\S]*HTTP Basic credentials require HTTPS/);
      assert.equal(await page.locator('[id="siteAccessForm.username"]').count(), 0, 'Site password form offered over http');
    });
  }
  if (phase === 'lifecycle' || phase === 'all') {
    await check('create queues, cancellation confirms, exact worker completes, download matches archive', async () => {
      await control('seed-runs', ['--count', '3']); await load(page);
      await page.evaluate(() => {localStorage.setItem('theme', 'light'); document.documentElement.classList.remove('dark');});
      let before = await requests(); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
      let dialog = page.locator('.fi-modal.fi-modal-open'); await livewire(page, () => dialog.getByRole('button', {name: 'Create backup', exact: true}).click());
      const pending = await newRequest(before, 'run'); assert.match(await page.locator('main').innerText(), /Waiting|Queued/i);
      await livewire(page, () => page.getByRole('button', {name: 'Cancel', exact: true}).filter({visible: true}).first().click()); dialog = page.locator('.fi-modal.fi-modal-open');
      assert.match(await dialog.innerText(), /has not started|Nothing is changed/i);
      await livewire(page, () => dialog.getByRole('button', {name: 'Cancel backup', exact: true}).click());
      assert.equal((await requests()).find(r => r.request_id === pending.request_id).status, 'cancelled');
      before = await requests(); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
      await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Create backup', exact: true}).click());
      const capture = await newRequest(before, 'run'); await control('worker', ['--request-id', capture.request_id]); await load(page);
      const completed = (await requests()).find(r => r.request_id === capture.request_id); assert.equal(completed.status, 'complete');
      const link = page.getByRole('link', {name: 'Download', exact: true}).first(); const href = await link.getAttribute('href');
      const runId = href.split('/').pop(); const archive = await readFile(path.join(metadata.private_root, 'artifacts', runId + '.tar.gz'));
      const reply = await page.context().request.get(href); assert.equal(reply.status(), 200);
      assert.match(reply.headers()['content-disposition'], /attachment/); assert.match(reply.headers()['content-disposition'], new RegExp(runId));
      assert.equal(reply.headers()['content-type'], 'application/gzip'); assert.equal(Number(reply.headers()['content-length']), archive.length);
      assert.match(reply.headers()['cache-control'], /no-store/); assert.equal(reply.headers()['x-content-type-options'], 'nosniff'); assert.deepEqual(await reply.body(), archive);
      const row = await findRow(page, runId); await menu(page, row, 'Details');
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
      report.download = {runId, bytes: archive.length, sha256: createHash('sha256').update(archive).digest('hex')};
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
        await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Delete selected copies', exact: true}).click());
        const pending = await newRequest(before, 'delete'); await control('worker', ['--request-id', pending.request_id]);
        assert.equal((await requests()).find(r => r.request_id === pending.request_id).status, 'complete'); await load(page);
      }
      // Delete all verified rows except the final one through actual UI confirmations.
      while (await page.getByRole('link', {name: 'Download', exact: true}).count() > 1) {
        const link = page.getByRole('link', {name: 'Download', exact: true}).last(); const runId = (await link.getAttribute('href')).split('/').pop();
        const row = await findRow(page, runId); const before = await requests(); await menu(page, row, 'Delete');
        const dialog = page.locator('.fi-modal.fi-modal-open'); const api = await localDeletionPreview(runId); assert.equal(api.refusal,null); assert(api.local_copy && !api.remote_copy); assert.equal(await dialog.getByRole('button',{name:'Delete selected copies',exact:true}).isVisible(),true); (report.localDeletionPreviews ||= []).push({runId,api,submitVisible:true,localSelected:await dialog.getByRole('radio',{name:'This server only',exact:true}).isChecked()}); assert.match(await dialog.innerText(), /Deletes the archive|Deletes this backup/); assert.match(await dialog.innerText(), /Paymenter's scheduler has not run yet\. Nothing is deleted until it does\./); if (await dialog.getByRole('radio', {name:'This server only',exact:true}).count()) assert.equal(await dialog.getByRole('radio', {name:'This server only',exact:true}).isChecked(),true); await shot(page, 'delete-confirmation');
        await livewire(page, () => dialog.getByRole('button', {name: 'Delete selected copies', exact: true}).click());
        const pending = await newRequest(before, 'delete'); assert((await stat(path.join(metadata.private_root, 'artifacts', runId + '.tar.gz'))).isFile(), 'Queued delete removed bytes during FPM request');
        await control('worker', ['--request-id', pending.request_id]); assert.equal((await requests()).find(r => r.request_id === pending.request_id).status, 'complete');
        await assert.rejects(stat(path.join(metadata.private_root, 'artifacts', runId + '.tar.gz')), {code: 'ENOENT'}); await load(page);
      }
      const link = page.getByRole('link', {name: 'Download', exact: true}).first(); const runId = (await link.getAttribute('href')).split('/').pop();
      const before = await requests(); await menu(page, await findRow(page, runId), 'Delete');
      const dialog = page.locator('.fi-modal.fi-modal-open'); assert.match(await dialog.innerText(), /cannot be deleted|last verified|only verified|another verified/i);
      assert.equal(await dialog.getByRole('button', {name: 'Delete selected copies', exact: true}).count(), 0); assert.deepEqual(await requests(), before);
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
  if (phase === 'last-good-edge') {
    await check('preview refuses last good with known damaged candidate; deleting damaged preserves good', async () => {
      const active = (await requests()).filter(item => ['pending', 'deferred', 'launching', 'running'].includes(item.status));
      assert.equal(active.length, 0, 'Resolve active requests explicitly before this edge check');
      assert.equal(await page.getByRole('link', {name: 'Download', exact: true}).count(), 0, 'Edge fixture must begin with no good copy');
      assert.equal(await page.getByRole('button', {name: 'Verify', exact: true}).count(), 1, 'Exactly one known damaged copy is required');
      let before = await requests(); await livewire(page, () => page.getByRole('button', {name: 'Create backup', exact: true}).first().click());
      await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Create backup', exact: true}).click());
      const capture = await newRequest(before, 'run'); await control('worker', ['--request-id', capture.request_id]); await load(page);
      assert.equal((await requests()).find(item => item.request_id === capture.request_id).status, 'complete');
      const href = await page.getByRole('link', {name: 'Download', exact: true}).first().getAttribute('href'); const goodId = href.split('/').pop();
      const goodPath = path.join(metadata.private_root, 'artifacts', goodId + '.tar.gz'); const bytes = await readFile(goodPath);
      // A copy whose last full check failed is not counted as a survivor, so the preview refuses.
      // Execution-time refusal for undetected damage stays covered by the engine tests.
      before = await requests(); await menu(page, await findRow(page, goodId), 'Delete'); await shot(page, 'damaged-candidate-preview');
      const preview = page.locator('.fi-modal.fi-modal-open'); assert.match(await preview.innerText(), /cannot be deleted[\s\S]*only usable backup/);
      assert.equal(await preview.getByRole('button', {name: 'Delete selected copies', exact: true}).count(), 0); await closeDialog(page);
      assert.deepEqual(await requests(), before); assert.deepEqual(await readFile(goodPath), bytes);
      const damaged = page.locator('tr').filter({has: page.getByRole('button', {name: 'Verify', exact: true})}).first();
      before = await requests(); await menu(page, damaged, 'Delete'); await shot(page, 'delete-damaged-preview');
      await livewire(page, () => page.locator('.fi-modal.fi-modal-open').getByRole('button', {name: 'Delete selected copies', exact: true}).click());
      const cleanup = await newRequest(before, 'delete'); await control('worker', ['--request-id', cleanup.request_id]);
      assert.equal((await requests()).find(item => item.request_id === cleanup.request_id).status, 'complete'); assert.deepEqual(await readFile(goodPath), bytes);
      await load(page); assert.equal(await page.getByRole('link', {name: 'Download', exact: true}).count(), 1); await shot(page, 'good-survivor');
      return {capture: capture.request_id, goodId, previewRefused: true, damagedDelete: cleanup.request_id};
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
