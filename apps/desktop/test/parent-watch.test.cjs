'use strict';
// The packaged backend entry stops when the desktop that owns it dies without
// running its normal cleanup (force quit or crash). Uses the source entry with
// a temporary HOME and the vault backend, never the user's workspace.
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.resolve(__dirname, '../../..');
const entry = path.join(root, 'apps/desktop/scripts/backend-entry.py');
const alive = pid => { try { process.kill(pid, 0); return true; } catch (error) { return error.code !== 'ESRCH'; } };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const skip = process.platform === 'win32' || !fs.existsSync(path.join(root, 'myflames/ui_assets/index.html'));

// A stand-in for Electron: names itself as the owner, starts the backend as a
// group leader, reports its pid at once, again when the launch line appears
// and when the backend exits, then waits to be killed.
const holderSource = `
  const { spawn } = require('node:child_process');
  const env = { ...process.env };
  if (process.env.MARK === '1') env.MYFLAMES_DESKTOP_PARENT = String(process.pid);
  const child = spawn(process.env.PY, [process.env.ENTRY, 'ui', '--port', '0', '--no-browser'],
    { detached: true, stdio: ['ignore', 'ignore', 'pipe'], env });
  process.stdout.write('pid ' + child.pid + '\\n');
  let text = '';
  child.stderr.on('data', chunk => { process.stderr.write(chunk); text += chunk; if (/myflames UI: http/.test(text)) { process.stdout.write('ready\\n'); text = ''; } });
  child.on('exit', (code, signal) => process.stdout.write('exit ' + code + ' ' + signal + '\\n'));
  setInterval(() => {}, 1000);`;

async function run(mark, home) {
  const env = { PATH: process.env.PATH, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: 'vault', MARK: mark ? '1' : '0',
    PYTHONPATH: root, PYTHONDONTWRITEBYTECODE: '1', PY: process.env.MYFLAMES_DESKTOP_PYTHON || 'python3', ENTRY: entry,
    // On SIGABRT Python then prints every thread's stack, which shows where a stuck start waits.
    PYTHONFAULTHANDLER: '1' };
  const holder = spawn(process.execPath, ['-e', holderSource], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  const state = { holder, pid: 0 };
  // The backend's stderr explains a start that never came (its tail, long
  // enough for a stack dump's most recent frames, which come first).
  let err = '';
  holder.stderr.on('data', chunk => { err = (err + chunk).slice(-20000); });
  const why = () => (err.trim() ? '; backend stderr: ' + err.trim() : '; the backend wrote nothing to stderr');
  state.ready = new Promise((resolve, reject) => {
    let aborted = false;
    const timer = setTimeout(() => {
      // Ask the stuck backend for its stacks, give it a second to print them,
      // then fail. A launch line after this no longer counts as a start.
      aborted = true;
      if (state.pid) try { process.kill(state.pid, 'SIGABRT'); } catch {}
      setTimeout(() => reject(new Error('backend did not start' + why())), 1000);
    }, 20000);
    let out = '';
    holder.stdout.on('data', chunk => {
      out += chunk;
      const match = /pid (\d+)/.exec(out);
      if (match) state.pid = Number(match[1]);
      const ended = /exit (\S+) (\S+)/.exec(out);
      if (ended && !aborted) {
        // Never signal a pid that may already be reused; let the last stderr
        // lines arrive before failing.
        aborted = true;
        clearTimeout(timer);
        setTimeout(() => reject(new Error('backend exited before its launch line (code ' + ended[1] + ', signal ' +
          ended[2] + ')' + why())), 300);
      }
      if (out.includes('ready') && !aborted) { clearTimeout(timer); resolve(); }
    });
    holder.once('exit', () => { clearTimeout(timer); reject(new Error('stand-in parent exited early' + why())); });
  });
  return state;
}
async function cleanup(state, home) {
  state.holder.kill('SIGKILL');
  if (state.pid && alive(state.pid)) try { process.kill(-state.pid, 'SIGKILL'); } catch {}
  for (let i = 0; i < 50 && state.pid && alive(state.pid); i++) await sleep(100);
  fs.rmSync(home, { recursive: true, force: true });
}
async function stopsWithin(pid, ms) {
  const deadline = Date.now() + ms;
  while (alive(pid) && Date.now() < deadline) await sleep(100);
  return !alive(pid);
}

test('backend entry stops after its desktop parent is killed', { skip }, async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'myflames-parent-watch-'));
  const state = await run(true, home);
  try {
    await state.ready;
    assert.ok(alive(state.pid));
    state.holder.kill('SIGKILL');
    assert.equal(await stopsWithin(state.pid, 8000), true, 'backend stopped after its parent was killed');
  } finally { await cleanup(state, home); }
});

test('backend entry without the desktop marker is not watched', { skip }, async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'myflames-parent-watch-'));
  const state = await run(false, home);
  try {
    await state.ready;
    state.holder.kill('SIGKILL');
    assert.equal(await stopsWithin(state.pid, 2500), false, 'an unmarked ui keeps running');
  } finally { await cleanup(state, home); }
});
