'use strict';
// Run against an already built macOS bundle. No database connection or SQL
// execution is needed; settings must pass through the real sidebar and backend.
const assert = require('node:assert/strict');
const { spawn, execFile } = require('node:child_process');
const fs = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const execFileAsync = promisify(execFile);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const SIDEBAR = '.sidebar-resizer';
const SPLITTER = '.query-lab .editor-splitter';
const DEFAULTS = { theme: 'system', sidebar_width: 208, server_nav_collapsed: true,
  health_auto_production: -1, health_auto_development: -1 };
const DEFAULT_HEIGHT = 260;
let stage = 'preflight', active = null, interrupted = null, check = 0;
const pass = message => console.log(`PASS ${++check}: ${message}`);
const assertNotInterrupted = () => { if (interrupted) throw new Error(interrupted); };

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

// Read executable names, never process arguments (which can contain secrets).
// The app and backend have separate process groups; remember both before quit
// so a reparented backend cannot disappear from the shutdown check.
async function processes() {
  const { stdout } = await execFileAsync('/bin/ps', ['-ax', '-o', 'pid=,ppid=,pgid=,stat=,comm='],
    { timeout: 2000, maxBuffer: 4 * 1024 * 1024 });
  return stdout.split('\n').flatMap(line => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+)$/);
    return match ? [{ pid: +match[1], parent: +match[2], group: +match[3], state: match[4], command: match[5] }] : [];
  });
}

async function ownedProcesses(run) {
  const rows = await processes();
  const owned = new Set([run.child.pid]);
  for (const row of rows) {
    if (run.known.get(row.pid) === row.command || run.groups.has(row.group)) owned.add(row.pid);
  }
  let changed;
  do {
    changed = false;
    for (const row of rows) if (owned.has(row.parent) && !owned.has(row.pid)) {
      owned.add(row.pid);
      changed = true;
    }
  } while (changed);
  const found = rows.filter(row => owned.has(row.pid));
  for (const row of found) {
    run.known.set(row.pid, row.command);
    if (row.command === run.backendExecutable) {
      run.backendPid ||= row.group;
      // backend.cjs starts its child detached, with pgid == pid. Do not
      // require the group leader to remain present during backend shutdown.
      run.groups.add(row.group);
    }
  }
  // Zombies have exited and cannot run, even before their parent reaps them.
  return found.filter(row => !row.state.startsWith('Z'));
}

async function until(description, timeout, probe, allowInterrupted = false) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (!allowInterrupted) assertNotInterrupted();
    const result = await probe();
    if (result) return result;
    await pause(100);
  }
  throw new Error(`${description} timed out after ${timeout / 1000} s`);
}

async function launch(executable, backendExecutable, data, puppeteer) {
  assertNotInterrupted();
  const port = await freePort();
  assertNotInterrupted();
  const env = { ...process.env, HOME: data, USERPROFILE: data,
    MYFLAMES_DESKTOP_DATA: data, MYFLAMES_SECRET_BACKEND: 'vault',
    MYFLAMES_DESKTOP_PYTHON: '/missing/python-must-not-be-used' };
  for (const key of Object.keys(env)) if (key.toUpperCase() === 'PATH') delete env[key];
  env.PATH = '/usr/bin:/bin:/usr/sbin:/sbin';
  for (const key of ['ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS', 'MYFLAMES_DESKTOP_SMOKE', 'MYFLAMES_VAULT_PASSPHRASE']) delete env[key];
  // No --smoke: exercise ordinary startup and ordinary quit. A dedicated app
  // process group also bounds emergency cleanup to processes this test owns.
  const child = spawn(executable, [`--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1'],
    { env, detached: true, stdio: ['ignore', 'ignore', 'ignore'] });
  const run = active = { child, backendExecutable, browser: null, page: null, backendPid: null,
    known: new Map(), groups: new Set(child.pid ? [child.pid] : []), exited: false, code: null, error: null };
  child.once('error', error => { run.error = error; run.exited = true; });
  child.once('exit', code => { run.code = code; run.exited = true; });
  const alive = async () => {
    if (run.error) throw new Error(`Packaged app spawn failed: ${run.error.code || 'unknown error'}`);
    if (run.exited) throw new Error('Packaged app exited during startup');
    await ownedProcesses(run);
  };
  const browserURL = `http://127.0.0.1:${port}`;
  await until('Remote debugging endpoint', 45000, async () => {
    await alive();
    try {
      const response = await fetch(`${browserURL}/json/version`, { signal: AbortSignal.timeout(750) });
      return response.ok && typeof (await response.json()).webSocketDebuggerUrl === 'string';
    } catch { return false; }
  });
  run.browser = await puppeteer.connect({ browserURL, defaultViewport: null, protocolTimeout: 10000 });
  run.page = await until('Workspace page', 45000, async () => {
    await alive();
    for (const target of run.browser.targets()) {
      if (target.type() === 'page' && /^http:\/\/127\.0\.0\.1:\d+\//.test(target.url())) {
        const page = await target.page();
        if (page) {
          // This earliest renderer read is first-paint evidence: it happens
          // before viewport changes, readiness waits or any click.
          const initial = await until('Initial document root', 15000, () => page.evaluate(() =>
            document.documentElement ? { theme: document.documentElement.dataset.theme ?? null } : null));
          run.startupTheme = initial.theme;
          return page;
        }
      }
    }
    return null;
  });
  run.page.setDefaultTimeout(15000);
  await run.page.setViewport({ width: 1440, height: 900 });
  await run.page.waitForSelector('.main-nav button[data-place="query"]', { visible: true });
  await run.page.waitForFunction(() => !location.hash && !document.body.innerText.includes('Opening your workspace'));
  // This no-store GET reads the server-rendered index to prove the saved theme
  // is present in each response's HTML; startupTheme above proves first paint.
  const html = await run.page.evaluate(async () =>
    (await fetch(location.origin + '/', { cache: 'no-store', credentials: 'same-origin' })).text());
  const root = html.match(/<html\b[^>]*>/i)?.[0];
  assert.ok(root, 'the server-rendered index contains an HTML root');
  run.htmlTheme = root.match(/\bdata-theme=["']([^"']*)["']/i)?.[1] ?? null;
  await ownedProcesses(run);
  assert.ok(run.backendPid, 'the packaged app started its bundled backend');
  assert.notEqual(run.backendPid, child.pid, 'the backend owns a separate process group');
  pass(`${stage}: workspace ready with the bundled backend and isolated app data`);
  return run;
}

// Reads use the authenticated page, never write a preference or workspace
// snapshot, and never open a database connection. Every change below is UI.
async function stored(page, endpoint) {
  return page.evaluate(async endpoint => {
    const response = await fetch(`/api/${endpoint}`, { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Myflames-Token': localStorage.getItem('myflames.session-proof') || '' },
      body: '{}' });
    if (!response.ok) throw new Error(`${endpoint} read answers HTTP ${response.status}`);
    return response.json();
  }, endpoint);
}

async function assertPreferences(run, expected) {
  const { page } = run;
  const theme = expected.theme === 'system' ? null : expected.theme;
  assert.equal(run.startupTheme, theme, 'the earliest renderer read has the saved theme at first paint');
  assert.equal(run.htmlTheme, theme, 'the server-rendered index has the saved theme on GET /');
  const reply = await stored(page, 'settings/ui');
  assert.deepEqual(reply.preferences, expected, 'backend preferences match the expected choices');
  assert.deepEqual(reply.defaults, DEFAULTS, 'backend defaults match this smoke');
  assert.equal(await page.evaluate(() => document.documentElement.dataset.theme ?? null), theme);
  await assertWidth(page, expected.sidebar_width);
  pass(`${stage}: original HTML, earliest renderer read and backend agree on ${expected.theme}; sidebar is ${expected.sidebar_width} px`);
}

async function assertWidth(page, width) {
  await page.waitForFunction((selector, width) => {
    const handle = document.querySelector(selector);
    const sidebar = document.querySelector('#workspace-sidebar');
    return Number(handle?.getAttribute('aria-valuenow')) === width && sidebar
      && Math.abs(sidebar.getBoundingClientRect().width - width) <= 1;
  }, {}, SIDEBAR, width);
}

async function savePreference(page, key, value, action) {
  // Arm before the UI action and match the write payload, so a startup read
  // cannot masquerade as a successful save.
  const [response] = await Promise.all([
    page.waitForResponse(response => {
      const request = response.request();
      if (request.method() !== 'POST' || new URL(request.url()).pathname !== '/api/settings/ui') return false;
      try { return JSON.parse(request.postData() || '{}').set?.[key] === value; }
      catch { return false; }
    }),
    action(),
  ]);
  assert.equal(response.status(), 200, `${key} save answers HTTP 200`);
  assert.equal((await response.json()).preferences[key], value, `${key} save contains the chosen value`);
  pass(`${stage}: ${key}=${value} saved through the UI with HTTP 200`);
}

async function chooseTheme(page, id) {
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector('.settings-page .source-item', { visible: true });
  await page.locator('.settings-page .source-item ::-p-text(Appearance)').click();
  const card = `.settings-page .theme-card[data-theme-id="${id}"]`;
  await page.waitForSelector(card, { visible: true });
  await savePreference(page, 'theme', id, () => page.click(card));
  await page.waitForFunction((selector, id) => {
    return document.querySelector(selector)?.getAttribute('aria-checked') === 'true'
      && (document.documentElement.dataset.theme || 'system') === id;
  }, {}, card, id);
  await page.keyboard.press('Escape');
  await page.waitForSelector('.settings-page', { hidden: true });
}

async function drag(page, selector, dx, dy) {
  const handle = await page.waitForSelector(selector, { visible: true });
  const box = await handle.boundingBox();
  await handle.dispose();
  assert.ok(box && box.width > 0 && box.height > 0, `${selector} has a visible drag target`);
  const x = box.x + box.width / 2, y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  try { await page.mouse.move(x + dx, y + dy, { steps: 16 }); }
  finally { await page.mouse.up(); }
}

async function resizeSidebar(page, width, reset = false) {
  const before = await page.$eval(SIDEBAR, node => Number(node.getAttribute('aria-valuenow')));
  await savePreference(page, 'sidebar_width', width, () => reset
    ? page.click(SIDEBAR, { clickCount: 2 }) : drag(page, SIDEBAR, width - before, 0));
  await assertWidth(page, width);
}

async function openEditor(page) {
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector(SPLITTER, { visible: true });
  await page.waitForSelector('.query-lab .sql-editor.cm-content', { visible: true });
}

async function assertHeight(page, height) {
  await page.waitForFunction((selector, height) => {
    const handle = document.querySelector(selector);
    const editor = handle?.parentElement.querySelector('.sql-editor-wrap');
    return Number(handle?.getAttribute('aria-valuenow')) === height && editor
      && Math.abs(editor.getBoundingClientRect().height - height) <= 2;
  }, {}, SPLITTER, height);
}

async function saved(page, height) {
  // Navigation/layout saves can leave the status saying saved while their
  // debounce is pending. Check the stored height AND final place as well as
  // the same save status used by test/session.mjs before allowing quit.
  const mode = await page.$eval('.main-nav button[aria-current="page"]', node => node.dataset.place);
  await until('Autosaved workspace layout and current place', 15000, async () => {
    const reply = await stored(page, 'session/load');
    return reply.restore && reply.state?.query_lab?.editor_layout?.height === height && reply.state.mode === mode;
  });
  await page.waitForFunction(() => document.querySelector('.session-status')?.dataset.state === 'saved');
  pass(`${stage}: autosaved editor height ${height} px and ${mode} place; All changes saved`);
}

async function resizeEditor(page, height) {
  const before = await page.$eval(SPLITTER, node => Number(node.getAttribute('aria-valuenow')));
  assert.ok(Math.abs(height - before) >= 64, 'the editor drag changes the height clearly');
  await drag(page, SPLITTER, 0, height - before);
  await assertHeight(page, height);
  await saved(page, height);
}

async function assertStoredHeight(page, height, fresh = false) {
  const reply = await stored(page, 'session/load');
  const actual = reply.state?.query_lab?.editor_layout?.height;
  if (fresh) assert.equal(actual, undefined, 'a fresh workspace has no saved editor layout');
  else assert.equal(actual, height, 'the backend retains the editor height before opening the editor');
  await openEditor(page);
  await assertHeight(page, height);
  pass(`${stage}: ${fresh ? 'no stored layout; default' : 'stored and restored'} editor split is ${height} px in the rendered editor`);
}

async function serverList(page, collapsed, change = false) {
  await page.click('.main-nav button[data-place="server"]');
  const button = '.place-toolbar .server-sidebar-toggle';
  await page.waitForSelector(button, { visible: true });
  if (change) {
    assert.equal(await page.$eval(button, node => node.getAttribute('aria-label')),
      collapsed ? 'Float the list of views' : 'Dock the list of views', 'the list starts in the opposite state');
    await savePreference(page, 'server_nav_collapsed', collapsed, () => page.click(button));
  }
  await page.waitForFunction((selector, collapsed) => {
    const button = document.querySelector(selector);
    const layout = document.querySelector('.server-place .server-layout');
    const list = layout?.querySelector('.server-source-wrap .source-list');
    return button?.getAttribute('aria-label') === (collapsed ? 'Dock the list of views' : 'Float the list of views')
      && layout?.classList.contains('is-nav-collapsed') === collapsed && !!list === !collapsed;
  }, {}, button, collapsed);
  assert.equal((await stored(page, 'settings/ui')).preferences.server_nav_collapsed, collapsed);
  pass(`${stage}: Server list is ${collapsed ? 'floating' : 'docked'} in the UI and backend`);
}

async function quit(run) {
  stage = 'quitting app and backend';
  await ownedProcesses(run);
  // main.cjs handles SIGTERM with app.quit(); before-quit awaits backend.stop()
  // and then app.exit(). browser.disconnect() alone would leave the app open.
  if (!run.exited) run.child.kill('SIGTERM');
  await until('App and owned backend shutdown', 20000, async () => {
    const remaining = await ownedProcesses(run);
    return run.exited && remaining.length === 0;
  }, true);
  run.browser?.disconnect();
  assert.equal(run.code, 0, 'normal app quit exits successfully');
  pass('SIGTERM quit stopped the app, bundled backend and all observed children within 20 s');
}

async function cleanup(run) {
  if (!run) return;
  run.browser?.disconnect();
  try {
    await ownedProcesses(run);
    if (!run.exited) run.child.kill('SIGTERM');
    await until('Cleanup shutdown', 20000, async () => {
      const remaining = await ownedProcesses(run);
      return run.exited && remaining.length === 0;
    }, true);
  } catch {
    // Cleanup can force only groups descended from the detached app we spawned.
    // Even if ps becomes unavailable, signal the two groups already proved
    // owned during startup rather than abandoning the spawned app/backend.
    const remaining = await ownedProcesses(run).catch(() => []);
    for (const group of run.groups) {
      try { process.kill(-group, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    }
    for (const row of remaining) {
      try { process.kill(row.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    }
    await until('Forced cleanup', 5000, async () => run.exited && (await ownedProcesses(run)).length === 0, true);
  }
}

(async () => {
  assert.equal(process.argv.length, 3, 'Usage: node apps/desktop/scripts/preferences-smoke.cjs <path to myflames.app>');
  assert.equal(process.platform, 'darwin', 'This smoke requires a packaged macOS .app');
  const appPath = await fs.realpath(path.resolve(process.argv[2]));
  const executable = await fs.realpath(path.join(appPath, 'Contents/MacOS/myflames'));
  const backendExecutable = await fs.realpath(path.join(appPath, 'Contents/Resources/backend/myflames-backend'));
  // Resolve existing workspace test dependencies, never download a browser.
  const modulePaths = [path.resolve(__dirname, '../../workspace/node_modules')];
  let puppeteerPath;
  try { puppeteerPath = require.resolve('puppeteer-core', { paths: modulePaths }); }
  catch { puppeteerPath = require.resolve('puppeteer', { paths: modulePaths }); }
  const puppeteer = require(puppeteerPath);
  const data = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-preferences-smoke-')));
  const onSignal = signal => {
    interrupted = `Interrupted by ${signal}`;
    if (active && !active.exited) active.child.kill('SIGTERM');
  };
  const onTerm = () => onSignal('SIGTERM'), onInt = () => onSignal('SIGINT');
  process.on('SIGTERM', onTerm);
  process.on('SIGINT', onInt);
  const heartbeat = setInterval(() => console.log(`preferences-smoke: ${stage}; app ${active?.exited ? 'exited' : 'alive'}; backend ${active?.backendPid || 'pending'}`), 30000);
  const watchdog = setTimeout(() => onSignal('overall 6-minute timeout'), 360000);
  const changed = { ...DEFAULTS, theme: 'dracula', sidebar_width: 280, server_nav_collapsed: false };
  const changedHeight = 160;
  try {
    for (const number of [1, 2, 3]) {
      stage = `launch ${number}`;
      const run = await launch(executable, backendExecutable, data, puppeteer);
      const page = run.page;
      // No UI action precedes these startup and stored-layout assertions.
      await assertPreferences(run, number === 2 ? changed : DEFAULTS);
      await assertStoredHeight(page, number === 2 ? changedHeight : DEFAULT_HEIGHT, number === 1);
      await serverList(page, number !== 2);
      if (number === 1) {
        await chooseTheme(page, 'dracula');
        await resizeSidebar(page, changed.sidebar_width);
        await openEditor(page);
        await resizeEditor(page, changedHeight);
        await serverList(page, false, true);
      } else if (number === 2) {
        await chooseTheme(page, 'system');
        await resizeSidebar(page, DEFAULTS.sidebar_width, true);
        await openEditor(page);
        await resizeEditor(page, DEFAULT_HEIGHT);
        await serverList(page, true, true);
      }
      const expected = number === 1 ? changed : DEFAULTS;
      assert.deepEqual((await stored(page, 'settings/ui')).preferences, expected,
        'all UI preferences, including unchanged Health defaults, are stored before quit');
      await saved(page, number === 1 ? changedHeight : DEFAULT_HEIGHT);
      assertNotInterrupted();
      await quit(run);
      assertNotInterrupted();
      active = null;
    }
    pass('three launches proved changed preferences survive restart and UI resets survive the next restart');
  } finally {
    try { await cleanup(active); }
    finally {
      clearInterval(heartbeat);
      clearTimeout(watchdog);
      process.removeListener('SIGTERM', onTerm);
      process.removeListener('SIGINT', onInt);
      await fs.rm(data, { recursive: true, force: true });
    }
  }
})().catch(error => {
  // Do not expose launch/session secrets if Puppeteer includes a URL in errors.
  console.error(`FAIL (${stage}): ${String(error.message).replace(/https?:\/\/\S+/g, '[private URL]')}`);
  process.exitCode = 1;
});
