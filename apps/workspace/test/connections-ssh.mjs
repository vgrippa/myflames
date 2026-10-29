// Connect through SSH in the connection form (Connections.tsx SshSettings,
// myflames/ssh_tunnel.py): the SSH host pick list comes from the backend's
// reading of a temporary ~/.ssh/config, the fields are saved with the
// connection, the connection key and the Test result name the SSH host, and
// a value the server would refuse is shown before anything is sent. Real
// profile storage and /api/ssh/hosts; database requests are mocked. Run after
// npm run build: node test/connections-ssh.mjs
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
import puppeteer from 'puppeteer';
import { connectionAction } from './connection-helpers.mjs';

// Every write review and confirmation sheet starts with ConnectionTypeBand:
// for a connection through SSH it names the SSH host, like connectionLabel,
// whether or not the connection has a name or the sheet passes a label.
{
  const bundle = await build({
    stdin: {
      contents: `import React from 'react';
        import { renderToStaticMarkup } from 'react-dom/server';
        import { ConnectionTypeBand } from './src/Connections';
        export const band = (props) => renderToStaticMarkup(<ConnectionTypeBand {...props}/>);`,
      loader: 'tsx', resolveDir: fileURLToPath(new URL('..', import.meta.url)),
    },
    bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
    loader: { '.css': 'empty', '.svg': 'text' }, external: ['react', 'react-dom', 'react-dom/server'], logLevel: 'silent',
  });
  const unit = { exports: {} };
  new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(createRequire(import.meta.url), unit, unit.exports);
  const text = props => unit.exports.band(props).replace(/<[^>]+>/g, '').replace(/&amp;/g, '&');
  const through = { name: 'Prod', host: '127.0.0.1', port: 3306, user: 'app', ssh_host: 'bastion', type: 'production' };
  assert.equal(text({ connection: through }), 'Production·Prod (app@127.0.0.1:3306 via bastion)');
  assert.equal(text({ connection: through, label: 'Prod (app@127.0.0.1:3306 via bastion)' }), 'Production·Prod (app@127.0.0.1:3306 via bastion)');
  assert.equal(text({ connection: { ...through, name: '', type: 'development' } }), 'Development·app@127.0.0.1:3306 via bastion');
  assert.equal(text({ connection: { ...through, ssh_host: ' bastion ' }, readOnly: true }), 'Read-only (Production)·Prod (app@127.0.0.1:3306 via bastion)');
  // Without SSH the band is unchanged.
  assert.equal(text({ connection: { ...through, ssh_host: '' } }), 'Production·Prod (app@127.0.0.1:3306)');
  assert.match(unit.exports.band({ connection: through }), /title="Prod \(app@127\.0\.0\.1:3306 via bastion\)"/);
}

const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-connections-ssh-'));
const database = path.join(directory, 'workspace.sqlite3');
const sshHome = path.join(directory, 'real-home');
await fs.mkdir(path.join(sshHome, '.ssh'), { recursive: true });
await fs.writeFile(path.join(sshHome, '.ssh', 'config'), 'Host bastion prod-jump *.internal\n  HostName 10.0.0.1\nHost db-tunnel\n');
const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: 'vault', MYFLAMES_SSH_HOME: sshHome };
delete env.MYFLAMES_VAULT_PASSPHRASE;
const seeded = spawnSync(process.env.PYTHON || 'python3', ['-c', `from myflames.workspace_store import WorkspaceStore
import sys
s=WorkspaceStore(sys.argv[1])
s.save('profile', dict(name='Behind bastion', host='127.0.0.1', port=3306, user='app', ssh_host='bastion', ssh_user='deploy', ssh_port=2222))
s.save('profile', dict(name='Direct', host='db1.example', port=3306, user='app'))`, database], { cwd: root, env, encoding: 'utf8' });
assert.equal(seeded.status, 0, seeded.stderr);
const server = spawn(process.env.PYTHON || 'python3', ['-c',
  'from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()', database], { cwd: root, env });
let browser;
const tests = [];
try {
  const url = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error(output || 'Server did not start')), 10000);
    server.stderr.on('data', chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
  });
  browser = await puppeteer.launch({ headless: 'shell' });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on('request', request => {
    const route = new URL(request.url()).pathname;
    if (route === '/api/connection/test') {
      const { connection } = JSON.parse(request.postData() || '{}');
      tests.push(connection);
      return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({
        version: '8.4.11', engine: 'mysql', flavor: 'mysql', product: 'MySQL', schemas: ['shop'],
        ...(connection.ssh_host ? { through_ssh: `Through SSH host ${connection.ssh_host}` } : {}) }) });
    }
    if (route.startsWith('/api/live/') || route.startsWith('/api/editor/'))
      return request.respond({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'Database access is mocked in this suite.' }) });
    return request.continue();
  });
  const noOverflow = async () => assert.equal(await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - innerWidth)), 0);
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: 'networkidle0' });
  await page.waitForFunction(() => document.querySelectorAll('.connection-row').length === 3); // two saved, and the default connection in use
  // The table names the SSH host of a connection through SSH, only that one.
  const endpoints = await page.$$eval('.connections [data-col=endpoint]', els => els.map(el => el.textContent.trim()).filter(Boolean));
  assert.ok(endpoints.includes('app@127.0.0.1:3306 via bastion'), JSON.stringify(endpoints));
  assert.ok(endpoints.includes('app@db1.example:3306'), JSON.stringify(endpoints));

  for (const scheme of ['light', 'dark']) {
    for (const width of [1440, 720]) {
      await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
      await page.setViewport({ width, height: 900 });
      await connectionAction(page, 'Behind bastion', 'Edit');
      await page.waitForSelector('.connection-panel details[data-section=ssh][open]');
      const fields = await page.$$eval('.connection-panel details[data-section=ssh] input', els => els.map(el => el.value));
      assert.deepEqual(fields, ['bastion', '2222', 'deploy']);
      assert.match(await page.$eval('.connection-panel details[data-section=ssh] summary', el => el.textContent), /Connect through SSH · bastion/);
      assert.match(await page.$eval('.connection-panel details[data-section=ssh]', el => el.textContent),
        /Uses your SSH keys or agent\. Password sign-in to SSH is not supported\./);
      // Host aliases read from ~/.ssh/config, without patterns.
      await page.waitForFunction(() => document.querySelectorAll('details[data-section=ssh] datalist option').length === 3);
      assert.deepEqual(await page.$$eval('details[data-section=ssh] datalist option', els => els.map(el => el.value)), ['bastion', 'db-tunnel', 'prod-jump']);
      await noOverflow();
      // Scroll the pane that holds the form (the window itself does not scroll).
      const scrolled = await page.$eval('.connection-panel details[data-section=ssh]', el => {
        let pane = el.parentElement;
        while (pane && !(/(auto|scroll)/.test(getComputedStyle(pane).overflowY) && pane.scrollHeight > pane.clientHeight)) pane = pane.parentElement;
        if (pane) pane.scrollTop += el.getBoundingClientRect().top - pane.getBoundingClientRect().top - 120;
        return { pane: pane ? pane.className : null, document: document.scrollingElement.scrollTop };
      });
      assert.equal(scrolled.document, 0, JSON.stringify(scrolled));
      await page.screenshot({ path: path.join(directory, `ssh-${scheme}-${width}.png`) });
      await page.keyboard.press('Escape');
      const close = await page.$('.connections .inspector-close');
      if (close) await close.click();
    }
  }

  // Test connection sends the SSH fields and says it went through SSH.
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.setViewport({ width: 1440, height: 900 });
  await connectionAction(page, 'Behind bastion', 'Edit');
  await page.waitForSelector('.connection-panel details[data-section=ssh][open]');
  await page.locator('.connection-panel button ::-p-text(Test connection)').click();
  await page.waitForFunction(() => /Connected · 8\.4\.11 · Through SSH host bastion/.test(document.querySelector('.connection-panel')?.textContent || ''));
  const sent = tests.at(-1);
  assert.deepEqual([sent.ssh_host, sent.ssh_user, sent.ssh_port, sent.host, sent.port], ['bastion', 'deploy', 2222, '127.0.0.1', 3306]);

  // A value the server would refuse is shown at once, and Use sends nothing.
  const sshHost = await page.$('.connection-panel details[data-section=ssh] input');
  await sshHost.click({ clickCount: 3 });
  await sshHost.type('-oProxyCommand=x');
  await page.waitForSelector('.connection-panel details[data-section=ssh] .form-error');
  assert.match(await page.$eval('.connection-panel details[data-section=ssh] .form-error', el => el.textContent), /not starting with a hyphen/);
  assert.equal(await page.$eval('.connection-panel details[data-section=ssh] input', el => el.getAttribute('aria-invalid')), 'true');

  // Clearing the SSH host makes it a direct connection again; Save stores that.
  await sshHost.click({ clickCount: 3 });
  await page.keyboard.press('Backspace');
  const user = (await page.$$('.connection-panel details[data-section=ssh] input'))[2];
  await user.click({ clickCount: 3 });
  await page.keyboard.press('Backspace');
  const port = (await page.$$('.connection-panel details[data-section=ssh] input'))[1];
  await port.click({ clickCount: 3 });
  await page.keyboard.press('Backspace');
  assert.equal(await page.$('.connection-panel details[data-section=ssh] .form-error'), null);
  await page.locator('.connection-panel button ::-p-text(Save connection)').click();
  await page.waitForFunction(() => /Connection "Behind bastion" saved/.test(document.querySelector('.connections')?.textContent || ''));
  const stored = spawnSync(process.env.PYTHON || 'python3', ['-c', `from myflames.workspace_store import WorkspaceStore
import json, sys
print(json.dumps(WorkspaceStore(sys.argv[1]).find_profile('Behind bastion')))`, database], { cwd: root, env, encoding: 'utf8' });
  assert.equal(stored.status, 0, stored.stderr);
  const profile = JSON.parse(stored.stdout);
  assert.equal(profile.ssh_host, undefined);
  assert.equal(profile.ssh_user, undefined);
  assert.deepEqual(errors, []);
  console.log(`Connections SSH checks passed. Screenshots: ${directory}`);
} finally {
  await browser?.close();
  server.kill();
}
