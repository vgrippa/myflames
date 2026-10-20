// Real browser HTTP/review gates, with a replay connection behind KILL.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const home = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-review-auth-'));
const processes = [];
let browser;
async function server(ttl) {
  const code = `import json, sys
from myflames import connector, ui
from myflames.review_tokens import ReviewTokens
from test_locks import KillScenario, FakeFactory, bound_kill_expect
scenario = KillScenario()
s = ui.WorkspaceServer(store_path=sys.argv[1])
connector.MySQLConnection = FakeFactory(scenario.conn)
s.review_tokens = ReviewTokens(ttl=int(sys.argv[2]))
c = {'host': '127.0.0.1', 'port': 3306, 'user': 'app'}
payload = {'connection': c, 'connection_id': 41, 'mode': 'connection', 'confirmed': True, 'expect': bound_kill_expect(scenario.conn, connection=c)}
print(json.dumps({'launch': s.launch_url, 'payload': payload}), flush=True)
s.serve_forever()`;
  const proc = spawn(process.env.PYTHON || 'python3', ['-c', code, path.join(home, `workspace-${ttl}.sqlite3`), String(ttl)], {
    cwd: root, env: { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: 'vault', PYTHONPATH: `${root}/test:${root}` },
  });
  processes.push(proc);
  return new Promise((resolve, reject) => {
    let output = '', error = '';
    const timer = setTimeout(() => reject(Error(error || 'Review fixture startup timed out')), 10000);
    proc.stderr.on('data', c => { error += c; });
    proc.stdout.on('data', c => { output += c; if (output.includes('\n')) { clearTimeout(timer); resolve(JSON.parse(output.split('\n')[0])); } });
    proc.on('error', reject);
  });
}
async function call(page, route, payload) {
  return page.evaluate(async (route, payload) => {
    const session = await (await fetch('/api/auth/session', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Myflames-Bootstrap': '1', 'X-Myflames-Token': localStorage.getItem('myflames.session-proof') || '' }, body: '{}' })).json();
    const result = await fetch(route, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Myflames-Token': session.token }, body: JSON.stringify(payload) });
    return { status: result.status, body: await result.json() };
  }, route, payload);
}
try {
  browser = await puppeteer.launch({ headless: 'shell' });
  const page = await browser.newPage();
  const fixture = await server(300);
  await page.goto(fixture.launch);
  await page.waitForSelector('.sidebar');
  const missing = await call(page, '/api/live/kill', fixture.payload);
  assert.equal(missing.status, 400);
  assert.match(missing.body.error, /Review the action again/);
  const prepared = await call(page, '/api/live/kill/prepare', fixture.payload);
  assert.equal(prepared.status, 200);
  assert.deepEqual(prepared.body.statements, ['KILL CONNECTION 41']);
  const reviewed = { ...fixture.payload, review_token: prepared.body.review_token };
  assert.equal((await call(page, '/api/live/kill', { ...reviewed, connection_id: 42 })).status, 400);
  assert.equal((await call(page, '/api/live/kill', reviewed)).body.status, 'killed');
  const replay = await call(page, '/api/live/kill', reviewed);
  assert.equal(replay.status, 400);
  assert.match(replay.body.error, /already used/);
  const expiredFixture = await server(0);
  await page.goto(expiredFixture.launch);
  await page.waitForSelector('.sidebar');
  const expiredPlan = await call(page, '/api/live/kill/prepare', expiredFixture.payload);
  const expired = await call(page, '/api/live/kill', { ...expiredFixture.payload, review_token: expiredPlan.body.review_token });
  assert.equal(expired.status, 400);
  assert.match(expired.body.error, /expired.*Review the action again/);
  console.log('review-auth: missing, changed, replayed and expired reviews refused; one approved KILL reaches replay connection');
} finally {
  if (browser) await browser.close();
  for (const proc of processes) proc.kill('SIGTERM');
  await fs.rm(home, { recursive: true, force: true });
}
