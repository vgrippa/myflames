'use strict';
// Run against an already built macOS bundle. No database connection or SQL
// execution is needed; settings must pass through the real sidebar and backend.
const assert = require('node:assert/strict');
const { spawn, execFile } = require('node:child_process');
const fs = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { promisify } = require('node:util');
const execFileAsync = promisify(execFile);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const EDITOR = '.query-lab .sql-editor.cm-content';
const WRAPPING = '.query-lab .cm-content.cm-lineWrapping';
const CURSORS = '.query-lab .cm-cursor';
const SETTINGS = {
  multi_cursor: {
    label: 'Multiple cursors',
    input: '.settings-page input[aria-describedby~="editor-setting-multi_cursor-hint"]',
  },
  soft_wrap: {
    label: 'Wrap long lines',
    // This switch has no hint/input id; its error paragraph follows its row.
    input: '.settings-page .settings-option-row:has(+ #editor-setting-soft_wrap-error) input',
  },
};
const DEFAULTS = { multi_cursor: true, soft_wrap: false };
const CHANGED = { multi_cursor: false, soft_wrap: true };
let stage = 'preflight', active = null, interrupted = null, check = 0;
const pass = message => console.log(`PASS ${++check}: ${message}`);
const assertNotInterrupted = () => { if (interrupted) throw new Error(interrupted); };

function sameRanges(a, b) {
  return !!a && !!b && a.length === b.length
    && a.every((range, index) => range.anchor === b[index].anchor && range.head === b[index].head);
}

// The automation facade exposes only the main range, so read every selection
// range through CodeMirror's own DOM handle (EditorView.findFromDOM reads
// .cmTile off the content element the same way). Null when handle or view is
// missing, which fails the assertions instead of silently retrying.
async function selectionRanges(page) {
  return page.evaluate(selector => {
    const content = document.querySelector(selector);
    const view = content?.cmTile?.root?.view;
    return view ? view.state.selection.ranges.map(range => ({ anchor: range.anchor, head: range.head })) : null;
  }, EDITOR);
}

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
        if (page) return page;
      }
    }
    return null;
  });
  run.page.setDefaultTimeout(15000);
  await run.page.setViewport({ width: 1440, height: 900 });
  await run.page.waitForSelector('.main-nav button[data-place="query"]', { visible: true });
  await run.page.waitForFunction(() => !location.hash && !document.body.innerText.includes('Opening your workspace'));
  await ownedProcesses(run);
  assert.ok(run.backendPid, 'the packaged app started its bundled backend');
  assert.notEqual(run.backendPid, child.pid, 'the backend owns a separate process group');
  pass(`${stage}: workspace ready with the bundled backend and isolated app data`);
  return run;
}

async function openSettings(page) {
  if (!await page.$('.settings-page')) await page.click('button[aria-label="Settings"]');
  await page.waitForSelector('.settings-page .source-item', { visible: true });
  await page.locator('.settings-page .source-item ::-p-text(Query editor)').click();
  await page.waitForSelector('.settings-page .settings-editor-group', { visible: true });
  for (const { input, label } of Object.values(SETTINGS)) {
    await page.waitForSelector(input);
    assert.equal(await page.$eval(input, node => node.closest('label').textContent.trim()), label);
  }
}

async function closeSettings(page) {
  await page.keyboard.press('Escape');
  await page.waitForSelector('.settings-page', { hidden: true });
}

async function assertSettings(page, expected, message) {
  // Read through the authenticated page, without writing or opening a database
  // session. This prevents transient startup defaults from passing the check.
  const reply = await page.evaluate(async () => {
    const response = await fetch('/api/settings/editor', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Myflames-Token': localStorage.getItem('myflames.session-proof') || '' },
      body: '{}' });
    return { status: response.status, data: await response.json() };
  });
  assert.equal(reply.status, 200, 'settings read answers HTTP 200');
  await openSettings(page);
  for (const [key, on] of Object.entries(expected)) {
    assert.equal(reply.data.settings[key], on, `${SETTINGS[key].label} is stored as ${on}`);
    await page.waitForFunction((selector, value) => {
      const input = document.querySelector(selector);
      return input?.checked === value && input.getAttribute('aria-disabled') !== 'true';
    }, {}, SETTINGS[key].input, on);
  }
  pass(message);
  await closeSettings(page);
}

async function setSetting(page, key, on) {
  await openSettings(page);
  const selector = SETTINGS[key].input;
  await page.waitForFunction(selector => document.querySelector(selector)?.getAttribute('aria-disabled') !== 'true', {}, selector);
  assert.equal(await page.$eval(selector, input => input.checked), !on,
    `${SETTINGS[key].label} begins in the opposite state before the explicit toggle`);
  // Arm before clicking. Match the payload so an initial read cannot satisfy
  // the save wait. Promise.all consumes both failures without a stray waiter.
  const [response] = await Promise.all([
    page.waitForResponse(response => {
      const request = response.request();
      if (request.method() !== 'POST' || new URL(request.url()).pathname !== '/api/settings/editor') return false;
      try { return JSON.parse(request.postData() || '{}').set?.[key] === on; }
      catch { return false; }
    }),
    (async () => {
      const input = await page.$(selector);
      const label = await input.evaluateHandle(node => node.closest('label'));
      try { await label.asElement().click(); }
      finally { await label.dispose(); await input.dispose(); }
    })(),
  ]);
  assert.equal(response.status(), 200, `${SETTINGS[key].label} save answers HTTP 200`);
  assert.equal((await response.json()).settings[key], on, 'save response contains the chosen value');
  await page.waitForFunction((selector, value) => {
    const input = document.querySelector(selector);
    return input?.checked === value && input.getAttribute('aria-disabled') !== 'true';
  }, {}, selector, on);
  pass(`${stage}: ${SETTINGS[key].label} ${on ? 'on' : 'off'} saved with HTTP 200`);
  await closeSettings(page);
}

async function editorChecks(page, expected, helpers) {
  const { setEditorText, selectEditor } = helpers;
  const frames = (count = 4) => page.evaluate(n => new Promise(resolve => {
    let left = n;
    const tick = () => (--left ? requestAnimationFrame(tick) : resolve());
    requestAnimationFrame(tick);
  }), count);
  // Short first/second lines keep Option-click independent of long-line wraps.
  await setEditorText(page, 'SELECT 1;\nSELECT 2;\nSELECT ' + 'long_name, '.repeat(150) + '3;');
  await page.waitForFunction((selector, wraps) => !!document.querySelector(selector) === wraps, {}, WRAPPING, expected.soft_wrap);
  assert.equal(await page.$eval(EDITOR, node => node.classList.contains('cm-lineWrapping')), expected.soft_wrap);
  pass(`${stage}: .cm-content ${expected.soft_wrap ? 'has' : 'does not have'} cm-lineWrapping`);
  await selectEditor(page, 2);
  // Paste leaves the viewport at the document end. The automation facade sets
  // selection without scrollIntoView, and CodeMirror applies the paste's own
  // scrollIntoView asynchronously, on its next measure. Let that pending
  // measure finish before scrolling back to the top; otherwise a late measure
  // can land after scrollTo (or between measuring and mouse.click) and park
  // line 2 under the query tab strip.
  await frames(2);
  await page.$eval(EDITOR, editor => {
    window.scrollTo(0, 0);
    editor.closest('.cm-editor').querySelector('.cm-scroller').scrollTo(0, 0);
  });
  // Each setting is saved on its own and reaches the editor a moment later; the
  // wrap class above proves only Wrap long lines. Wait for Multiple cursors too
  // (seen on CI: Option-click landed before the editor allowed a second cursor).
  const multi = expected.multi_cursor ? 'on' : 'off';
  await page.waitForFunction((selector, value) => document.querySelector(selector)?.dataset.multiCursor === value,
    {}, EDITOR, multi)
    .catch(error => { throw new Error(`the editor never applied Multiple cursors ${multi}: ` + error.message); });
  await page.waitForFunction(selector => document.querySelectorAll(selector).length === 1, {}, CURSORS)
    .catch(error => { throw new Error('the editor never showed exactly one cursor before Option-click: ' + error.message); });
  // The Option-click must land on line 2's text inside the editor. Measure and
  // verify in one wait (fresh coordinates each poll): a closing overlay or a
  // late layout change between measuring and clicking would otherwise swallow
  // the click. Confirm the page and scroller stay at the top with the strict
  // hit test passing across two consecutive polls before taking the point; any
  // poll that had to scroll back to the top resets the count.
  const topStreakKey = '__myflamesSmokeTopStreak';
  await page.evaluate(key => { delete window[key]; }, topStreakKey);
  let measured;
  try {
    measured = await page.waitForFunction((selector, key) => {
      const editor = document.querySelector(selector);
      const scroller = editor?.closest('.cm-editor')?.querySelector('.cm-scroller');
      const second = editor?.querySelectorAll('.cm-line')[1];
      if (!editor || !scroller || !second) {
        window[key] = 0;
        return null;
      }
      if (window.scrollX !== 0 || window.scrollY !== 0 || scroller.scrollTop !== 0 || scroller.scrollLeft !== 0) {
        if (window.scrollX !== 0 || window.scrollY !== 0) window.scrollTo(0, 0);
        if (scroller.scrollTop !== 0 || scroller.scrollLeft !== 0) scroller.scrollTo(0, 0);
        window[key] = 0;
        return null;
      }
      if (second.textContent !== 'SELECT 2;') {
        window[key] = 0;
        return null;
      }
      const range = document.createRange();
      range.selectNodeContents(second);
      const rect = range.getClientRects()[0];
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        window[key] = 0;
        return null;
      }
      const srect = scroller.getBoundingClientRect();
      if (rect.top < Math.max(0, srect.top) || rect.bottom > Math.min(innerHeight, srect.bottom)) {
        window[key] = 0;
        return null;
      }
      const point = { x: rect.left + Math.min(16, rect.width / 2), y: rect.top + rect.height / 2 };
      const hit = document.elementFromPoint(point.x, point.y);
      if (!hit || !editor.contains(hit)) {
        window[key] = 0;
        return null;
      }
      window[key] = (window[key] || 0) + 1;
      return window[key] >= 2 ? point : null;
    }, {}, EDITOR, topStreakKey).catch(async error => {
      const seen = await page.evaluate(selector => {
        const editor = document.querySelector(selector);
        const scroller = editor?.closest('.cm-editor')?.querySelector('.cm-scroller');
        const classes = node => String(node?.className?.baseVal ?? node?.className ?? '');
        const scroll = `scroller top ${scroller?.scrollTop ?? '?'}, page y ${window.scrollY}`;
        const second = editor?.querySelectorAll('.cm-line')[1];
        if (!second) return `line 2 is not rendered (${scroll})`;
        const range = document.createRange();
        range.selectNodeContents(second);
        const rect = range.getClientRects()[0];
        if (!rect || rect.width <= 0 || rect.height <= 0) return `line 2 has no visible text (${scroll})`;
        const hit = document.elementFromPoint(rect.left + Math.min(16, rect.width / 2), rect.top + rect.height / 2);
        return `topmost at the point: ${hit ? `${hit.tagName} "${classes(hit)}"` : 'nothing'} (${scroll})`;
      }, EDITOR).catch(() => 'diagnostic unavailable');
      throw new Error(`the Option-click point never landed inside the editor (${seen}): ` + error.message);
    });
  } finally {
    await page.evaluate(key => { delete window[key]; }, topStreakKey).catch(() => {});
  }
  const point = await measured.jsonValue();
  await measured.dispose();
  const want = expected.multi_cursor ? 2 : 1;
  // Line 2's document span for the line assertions below.
  const line2 = await page.$eval(EDITOR, node => {
    const text = node.value, first = text.indexOf('\n');
    return { start: first + 1, end: text.indexOf('\n', first + 1) };
  });
  const onLine2 = range => range.anchor >= line2.start && range.head <= line2.end;
  // The outcome the assertions need: the expected cursor count and the main
  // cursor on line 2. Wait for it instead of assuming the click took effect.
  const outcome = () => page.waitForFunction((selector, cursors, n) => {
    const editor = document.querySelector(selector);
    return !!editor && document.querySelectorAll(cursors).length === n
      && editor.selectionStart >= editor.value.indexOf('\n') + 1;
  }, { timeout: 5000 }, EDITOR, CURSORS, want).catch(() => false);
  // The selection before the Option-click: one collapsed cursor on line 1.
  const before = await selectionRanges(page);
  assert.ok(before && before.length === 1 && before[0].anchor < line2.start,
    'the editor starts with one cursor on line 1 before the Option-click');
  await page.keyboard.down('Alt');
  try { await page.mouse.click(point.x, point.y); }
  finally { await page.keyboard.up('Alt'); }
  // Let CodeMirror measure/draw both cursors before counting. A slow CI runner
  // can need more than two frames, so wait for the expected outcome (bounded),
  // then let a few more frames pass so the negative case cannot gain a late
  // second cursor. Cursor DOM remains present during its CSS blink off phase.
  let settled = await outcome();
  await frames();
  let count = await page.$$eval(CURSORS, nodes => nodes.length);
  let ranges = await selectionRanges(page);
  let onSecondLine = await page.$eval(EDITOR, node => node.selectionStart >= node.value.indexOf('\n') + 1);
  if (!settled && sameRanges(ranges, before)) {
    // Retry only a click that had no effect: the selection is byte-identical
    // to what it was before the click. A busy macOS CI runner has dropped the
    // first Option-click with the setting applied (seen on CI after the
    // second relaunch, with the editor not even focused). A click that did
    // something wrong (replaced the line-1 cursor instead of adding one, or
    // moved nothing but drew) is not retried: it fails the assertions below
    // instead of masking the incorrect effect. Two misses still fail.
    const state = await page.evaluate((selector, x, y) => {
      const hit = document.elementFromPoint(x, y);
      const classes = node => String(node?.className?.baseVal ?? node?.className ?? '');
      return {
        focused: !!document.querySelector('.query-lab .cm-editor.cm-focused'),
        active: document.activeElement?.className || document.activeElement?.tagName || '',
        multi: document.querySelector(selector)?.dataset.multiCursor || '',
        topmost: hit ? `${hit.tagName} "${classes(hit)}"` : 'nothing',
      };
    }, EDITOR, point.x, point.y);
    console.log(`RETRY (${stage}): Option-click left the selection unchanged at ${JSON.stringify(before)}, want ${want} cursors with one on line 2; ${JSON.stringify(state)}`);
    await page.keyboard.down('Alt');
    try { await page.mouse.click(point.x + 8, point.y); }
    finally { await page.keyboard.up('Alt'); }
    await outcome();
    await frames();
    count = await page.$$eval(CURSORS, nodes => nodes.length);
    ranges = await selectionRanges(page);
    onSecondLine = await page.$eval(EDITOR, node => node.selectionStart >= node.value.indexOf('\n') + 1);
  }
  assert.equal(count, want, 'Option-click obeys Multiple cursors');
  assert.ok(onSecondLine, 'Option-click reached the second line');
  assert.ok(ranges && ranges.length === want, 'Option-click left the expected number of selection ranges');
  if (expected.multi_cursor) {
    assert.ok(ranges.some(range => range.anchor === before[0].anchor && range.head === before[0].head),
      'Option-click with Multiple cursors keeps the original cursor on line 1');
    assert.ok(ranges.some(range => (range.anchor !== before[0].anchor || range.head !== before[0].head) && onLine2(range)),
      'Option-click with Multiple cursors adds a cursor on line 2');
  } else {
    assert.ok(ranges.length === 1 && onLine2(ranges[0]),
      'Option-click with Multiple cursors off moves the single cursor to line 2');
  }
  pass(`${stage}: Option-click on line 2 leaves ${count} cursor${count === 1 ? '' : 's'}`);
  await selectEditor(page, 0);
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
  assert.equal(process.argv.length, 3, 'Usage: node apps/desktop/scripts/editor-settings-smoke.cjs <path to myflames.app>');
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
  const helpers = await import(pathToFileURL(path.resolve(__dirname, '../../workspace/test/editor-helpers.mjs')).href);
  const data = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-editor-settings-smoke-')));
  const onSignal = signal => {
    interrupted = `Interrupted by ${signal}`;
    if (active && !active.exited) active.child.kill('SIGTERM');
  };
  const onTerm = () => onSignal('SIGTERM'), onInt = () => onSignal('SIGINT');
  process.on('SIGTERM', onTerm);
  process.on('SIGINT', onInt);
  const heartbeat = setInterval(() => console.log(`editor-settings-smoke: ${stage}; app ${active?.exited ? 'exited' : 'alive'}; backend ${active?.backendPid || 'pending'}`), 30000);
  const watchdog = setTimeout(() => onSignal('overall 4-minute timeout'), 240000);
  try {
    for (const number of [1, 2]) {
      stage = `launch ${number}`;
      const run = await launch(executable, backendExecutable, data, puppeteer);
      const page = run.page;
      await page.click('.main-nav button[data-place="query"]');
      await page.waitForSelector(EDITOR, { visible: true });
      await assertSettings(page, number === 1 ? DEFAULTS : CHANGED,
        `${stage}: ${number === 1 ? 'fresh defaults' : 'persisted changes'} appear on the Settings page and backend`);
      if (number === 1) {
        await editorChecks(page, DEFAULTS, helpers);
        await setSetting(page, 'soft_wrap', true);
        await setSetting(page, 'multi_cursor', false);
      }
      await editorChecks(page, CHANGED, helpers);
      if (number === 2) {
        await setSetting(page, 'soft_wrap', false);
        await setSetting(page, 'multi_cursor', true);
        await assertSettings(page, DEFAULTS, 'both settings restored to defaults on the Settings page and backend');
        await editorChecks(page, DEFAULTS, helpers);
      }
      assertNotInterrupted();
      await quit(run);
      assertNotInterrupted();
      active = null;
    }
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
