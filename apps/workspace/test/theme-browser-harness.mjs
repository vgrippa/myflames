// Theme suites use a throwaway WorkspaceServer only. No connection is opened.
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
export async function withThemeWorkspace(run) {
  // Launch first so a refused Chromium launch cannot orphan a server.
  const browser = await puppeteer.launch({ headless: 'shell', args: (process.env.MYFLAMES_BROWSER_ARGS || '').split(' ').filter(Boolean) });
  let server, directory;
  try {
    directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-themes-'));
    server = spawn(process.env.PYTHON || 'python3', ['-c', 'from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()', path.join(directory, 'workspace.sqlite3')], {
      cwd: fileURLToPath(new URL('../../../', import.meta.url)),
      env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: 'vault' },
    });
    const url = await new Promise((resolve, reject) => {
      let text = '';
      const timer = setTimeout(() => reject(Error(`WorkspaceServer launch: ${text}`)), 10000);
      server.stderr.on('data', chunk => {
        text += chunk;
        const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(Error(`WorkspaceServer exited ${code}: ${text}`)); });
    });
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setRequestInterception(true);
    const origin = new URL(url).origin;
    page.on('request', request => {
      const target = new URL(request.url());
      if (target.origin !== origin && !['data:', 'blob:', 'about:'].includes(target.protocol)) return request.abort();
      // Keep news and update reads local too: the Python server must not
      // start an external feed/update fetch during a theme-only suite.
      if (target.pathname === '/api/news') return request.respond({status:200,contentType:'application/json',body:JSON.stringify({schema_version:'news-1.0',enabled:false,items:[],fetched_at:null,stale:false,offline:true,sources:[]})});
      if (target.pathname.startsWith('/api/app/update/check')) return request.respond({status:400,contentType:'application/json',body:'{"error":"Theme suite: updates are offline."}'});
      // Refuse every database operation, even if a future restore attempts one.
      if (/^\/api\/(live|connection|editor\/open)/.test(target.pathname)) return request.respond({status:400, contentType:'application/json',body:'{"error":"Theme suite: no database operations."}'});
      return request.continue();
    });
    await page.goto(url);
    await page.waitForSelector('.workspace');
    await run(page, { browser, directory });
  } finally {
    try { await browser.close(); }
    finally {
      if (server?.pid && server.exitCode === null && server.signalCode === null) {
        const stopped = once(server, 'exit'); server.kill('SIGINT'); await stopped;
      }
      if (directory) await fs.rm(directory, { recursive: true, force: true });
    }
  }
}
