// Shell-only integration: throwaway servers share one store across a restart.
// Every database request and all outbound reads are refused by this harness.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { connectionAction } from './connection-helpers.mjs';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const args = (process.env.MYFLAMES_BROWSER_ARGS || '').split(' ').filter(Boolean);
if (process.env.MYFLAMES_NO_SANDBOX && !args.includes('--no-sandbox')) args.push('--no-sandbox');
// Launch Chromium first so a refused launch cannot leave a server behind.
const browser = await puppeteer.launch({ headless: 'shell', args });
let server, directory;
let updateStatus = null;
async function stopServer() {
  if (server?.pid && server.exitCode === null && server.signalCode === null) {
    const stopped = once(server, 'exit');
    server.kill('SIGINT');
    await stopped;
  }
}
async function startServer(version = '') {
  server = spawn(process.env.PYTHON || 'python3', ['-c', `
import sys
from myflames import connector
import myflames.ui as ui
from myflames.ui import WorkspaceServer
if sys.argv[2]:
    ui.__version__ = sys.argv[2]
def refuse(*args, **kwargs):
    raise AssertionError('Shell suite must never connect to a database')
connector.MySQLConnection.run = refuse
connector.MySQLConnection.run_script = refuse
connector.MySQLConnection.run_stdin = refuse
connector.MySQLConnection.run_write = refuse
s = WorkspaceServer(store_path=sys.argv[1])
print(s.launch_url, file=sys.stderr, flush=True)
try:
    s.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    s.server_close()
`, path.join(directory, 'workspace.sqlite3'), version], {
    cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: 'vault' },
  });
  return new Promise((resolve, reject) => {
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
}
try {
  directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-shell-'));
  let url = await startServer();
  const errors = [];
  const newPage = async () => {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    await page.setViewport({ width: 1440, height: 1000 });
    await page.setRequestInterception(true);
    page.on('request', request => {
      const target = new URL(request.url());
      const json = (body, status = 200) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (target.origin !== new URL(url).origin && !['data:', 'blob:', 'about:'].includes(target.protocol)) return request.abort();
      if (target.pathname === '/api/news') return json({schema_version:'news-1.0',enabled:false,items:[],sources:[],offline:true});
      if (target.pathname === '/api/app/update/status' && updateStatus) return json(updateStatus);
      if (target.pathname === '/api/app/update/check') return json({error:'Shell suite: updates offline.'},400);
      if (target.pathname === '/api/editor/status') return json({status:'none',session:null,run:null});
      if (/^\/api\/(live|connection|editor\/open|table-data)/.test(target.pathname)) return json({error:'Shell suite: no database operations.'},400);
      return request.continue();
    });
    return page;
  };
  let page = await newPage();
  const go = async place => {
    // Rail navigation remains usable at 720 px.
    await page.click(`.main-nav [data-place="${place}"]`);
    await page.waitForSelector(`.workspace[data-place="${place}"]`);
  };
  // A button can have a viewport rect while being clipped by a scrolling
  // pane. Only probe centers inside that pane's visible area, but never
  // discount a center just because the fixed footer covers it.
  const footerHitTargets = async (label, { requireTargets = false } = {}) => {
    const result = await page.evaluate(() => {
      const footer = document.querySelector('.window-statusbar');
      const top = document.querySelector('.topbar-stack').getBoundingClientRect().bottom;
      let checked = 0;
      const covered = [];
      const targets = document.querySelectorAll('.workspace main button, .workspace main a[href], .workspace main [role="button"], .workspace main input, .workspace main select, .workspace main textarea');
      for (const target of targets) {
        const rect = target.getBoundingClientRect(), style = getComputedStyle(target);
        const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
        if (!rect.width || !rect.height || style.visibility !== 'visible' || style.pointerEvents === 'none'
          || x < 0 || x >= innerWidth || y < top || y >= innerHeight) continue;
        let clipped = false;
        for (let parent = target.parentElement; parent; parent = parent.parentElement) {
          const bounds = parent.getBoundingClientRect(), css = getComputedStyle(parent);
          if ((/(auto|scroll|hidden|clip)/.test(css.overflowX) && (x < bounds.left || x >= bounds.right))
            || (/(auto|scroll|hidden|clip)/.test(css.overflowY) && (y < bounds.top || y >= bounds.bottom))) {
            clipped = true; break;
          }
        }
        if (clipped) continue;
        checked++;
        const hit = document.elementFromPoint(x, y);
        if (footer.contains(hit)) covered.push({target:target.getAttribute('aria-label') || target.textContent.trim(), y});
      }
      return {found:targets.length, checked, covered};
    });
    if (requireTargets) assert.ok(result.checked > 0, `${label}: checked visible interactive centers`);
    assert.deepEqual(result.covered, [], `${label}: footer does not cover interactive centers`);
  };
  const lastControlAboveFooter = async (scope, label, {requireControl = false} = {}) => {
    const control = await page.evaluateHandle(scope => [...document.querySelectorAll(`${scope} button, ${scope} input, ${scope} select, ${scope} textarea`)]
      .filter(el => {
        const rect = el.getBoundingClientRect();
        return el.getClientRects().length && rect.width > 0 && rect.height > 0
          && !(el instanceof HTMLInputElement && ['hidden','file'].includes(el.type))
          && getComputedStyle(el).visibility === 'visible';
      }).at(-1), scope);
    if (!await control.evaluate(el => !!el)) {
      await control.dispose();
      assert.ok(!requireControl, `${label}: last control exists`);
      return;
    }
    await control.evaluate(el => el.scrollIntoView({block:'end', inline:'nearest', behavior:'instant'}));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const result = await control.evaluate(el => {
      const rect = el.getBoundingClientRect(), footer = document.querySelector('.window-statusbar').getBoundingClientRect();
      const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
      return {bottom:rect.bottom, footerTop:footer.top, hit:el.contains(document.elementFromPoint(x, y))};
    });
    await control.dispose();
    assert.ok(result.bottom <= result.footerTop && result.hit, `${label}: last control is reachable above the footer: ${JSON.stringify(result)}`);
  };
  const api = (set) => page.evaluate(async set => {
    const reply = await fetch('/api/settings/ui', {method:'POST', headers:{'Content-Type':'application/json','X-Myflames-Token':localStorage.getItem('myflames.session-proof')},body:JSON.stringify(set ? {set} : {})});
    assertResponse(reply);
    return reply.json();
    function assertResponse(reply) { if (!reply.ok) throw Error(`UI preferences HTTP ${reply.status}`); }
  }, set);
  const stored = async (key, expected) => {
    await page.waitForFunction(async ({key,expected}) => {
      const reply=await fetch('/api/settings/ui',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':localStorage.getItem('myflames.session-proof')},body:'{}'}).then(r=>r.json());
      return reply.preferences?.[key]===expected;
    }, {}, {key,expected});
  };
  const width = () => page.$eval('.sidebar', e => Math.round(e.getBoundingClientRect().width));
  const drag = async delta => {
    const handle = await page.$('.sidebar-resizer');
    const box = await handle.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + 100);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + delta, box.y + 100, {steps:8});
    await page.mouse.up();
  };
  await page.goto(url);
  await page.waitForSelector('.window-statusbar');
  assert.equal(await width(),208);
  await drag(64); assert.equal(await width(),272); await stored('sidebar_width',272);
  assert.equal(await page.$eval('.sidebar-resizer',e=>e.getAttribute('aria-valuenow')),'272');
  assert.equal(await page.$eval('.sidebar-resizer',e=>e.getAttribute('aria-orientation')),'vertical');
  await drag(900); assert.equal(await width(),320); await stored('sidebar_width',320);
  await drag(-180); assert.equal(await width(),208); await stored('sidebar_width',208);
  await page.focus('.sidebar-resizer');
  await page.keyboard.press('ArrowRight'); assert.equal(await width(),216);
  await page.keyboard.press('ArrowLeft'); assert.equal(await width(),208);
  await page.keyboard.press('ArrowRight'); await page.keyboard.press('End'); assert.equal(await width(),208);
  await page.keyboard.press('ArrowRight'); await page.keyboard.press('Home'); assert.equal(await width(),208);
  await drag(56); await page.click('.sidebar-resizer',{count: 2}); assert.equal(await width(),208);
  await drag(72); await stored('sidebar_width',280);
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector('.settings-page');
  await page.locator('.settings-page .theme-card[data-theme-id="dark"]').click();
  await stored('theme','dark');
  await go('server');
  await page.waitForSelector('button[aria-label="Dock the list of views"]');
  await page.click('button[aria-label="Dock the list of views"]');
  await stored('server_nav_collapsed',false);
  await page.waitForSelector('.health-panel .auto-refresh select');
  await page.select('.health-panel .auto-refresh select','30');
  await stored('health_auto_production',30);
  // A desktop restart discards the renderer. Closing without beforeunload
  // also avoids an autosave failure from the stopped server blocking goto.
  const previousOrigin = new URL(url).origin;
  await page.close();
  await stopServer();
  url = await startServer();
  page = await newPage();
  // A new port has empty localStorage, like the desktop's new partition.
  assert.notEqual(new URL(url).origin, previousOrigin, 'restart uses a new origin');
  await page.evaluateOnNewDocument(()=>{ window.__shellEmptyStorage = localStorage.length===0; });
  const response = await page.goto(url);
  assert.match(await response.text(), /<html lang="en" data-theme="dark">/,'theme is present before scripts paint');
  await page.waitForSelector('.window-statusbar');
  assert.equal(await page.evaluate(()=>window.__shellEmptyStorage),true,'fresh renderer starts with empty localStorage');
  assert.equal(await page.evaluate(()=>localStorage.getItem('myflames.theme')),null);
  assert.equal(await width(),280,'sidebar width survives server restart');
  assert.equal(await page.$eval('html',e=>e.dataset.theme),'dark');
  assert.equal((await api()).preferences.server_nav_collapsed,false);
  assert.equal((await api()).preferences.health_auto_production,30);
  assert.equal((await api()).preferences.health_auto_development,-1,'Production leaves Development unchanged');
  await go('server');
  await page.waitForSelector('button[aria-label="Float the list of views"]');
  await page.waitForSelector('.health-panel .auto-refresh select');
  assert.equal(await page.$eval('.health-panel .auto-refresh select',e=>e.value),'30','Health control restores its preference');
  await page.click('.sidebar-resizer',{count: 2});
  // A saved connection exposes Delete at the bottom of its edit form,
  // reproducing the Home control that the fixed footer used to cover.
  await page.evaluate(async () => {
    const reply = await fetch('/api/profiles/save', {method:'POST',
      headers:{'Content-Type':'application/json','X-Myflames-Token':localStorage.getItem('myflames.session-proof')},
      body:JSON.stringify({value:{name:'Footer layout connection',host:'layout.example',port:3306,user:'layout',database:'shop',ssl_mode:'REQUIRED',type:'production',read_only:false},remember_password:false})});
    if (!reply.ok) throw Error(`Layout connection HTTP ${reply.status}`);
  });
  for (const theme of ['light','dark']) {
    await api({theme});
    // Reload exercises backend preference application instead of editing CSS.
    await page.waitForFunction(()=>document.querySelector('.session-status')?.dataset.state==='saved');
    await page.reload(); await page.waitForSelector('.window-statusbar');
    for (const viewport of [1440,720]) {
      await page.setViewport({width:viewport,height:900});
      for (const place of ['home','query','server','plans','learn','investigations']) {
        await go(place);
        await page.waitForFunction(()=>document.querySelector('.session-status')?.dataset.state!=='loading');
        const metrics = await page.evaluate(()=>{
          const bar=document.querySelector('.window-statusbar'), brand=document.querySelector('.brand-version');
          return {overflow:document.documentElement.scrollWidth-innerWidth, bars:document.querySelectorAll('.window-statusbar').length,
            version:brand?brand.textContent:'', versionShown:!!(brand&&brand.getClientRects().length),
            footerText:bar.textContent, footerTabIndex:bar.tabIndex,
            footerPadding:parseFloat(getComputedStyle(document.querySelector('.workspace')).paddingBottom), barHeight:bar.getBoundingClientRect().height};
        });
        assert.equal(metrics.bars,1); assert.ok(metrics.overflow<=0,`${theme} ${viewport} ${place}: ${JSON.stringify(metrics)}`);
        assert.equal(metrics.footerText.includes(metrics.version),false,`${place}: no installed version text in the status bar`);
        assert.doesNotMatch(metrics.footerText,/myflames|Check for updates|View releases/,`${place}: the status bar keeps only the view's facts`);
        assert.equal(metrics.footerTabIndex,metrics.footerText.trim()?0:-1,`${place}: only a footer with facts is a tab stop`);
        assert.match(metrics.version,/\d+\.\d+\.\d+|development/,`${place}: the brand names the installed version`);
        assert.equal(metrics.versionShown,viewport>900,`${place}: the version hides on the icon rail`);
        assert.ok(metrics.footerPadding>=metrics.barHeight,`${place}: content reserves the footer`);
        if (place === 'home') {
          if (viewport > 1000) {
            await footerHitTargets(`${theme} ${viewport} ${place}`, {requireTargets:true});
          }
        }
        if (place==='home') {
          await connectionAction(page, 'Footer layout connection', 'Edit');
          await page.waitForSelector('.connection-panel');
          if (viewport > 1000) {
            await footerHitTargets(`${theme} ${viewport} Home connection panel`, {requireTargets:true});
          }
          await lastControlAboveFooter('.connection-panel', `${theme} ${viewport} Home connection panel`, {requireControl:true});
          await page.locator('.connection-panel .delete-connection').click();
          await page.waitForSelector('dialog.confirm-sheet[open]');
          await page.keyboard.press('Escape');
          await page.waitForFunction(()=>!document.querySelector('dialog.confirm-sheet[open]'));
          await page.keyboard.press('Escape');
          await page.waitForFunction(()=>!document.querySelector('.connection-panel'));
        }
        if (place==='query') {
          await page.waitForFunction(()=>document.querySelector('.window-statusbar [data-status="session"]')?.textContent==='No session');
          assert.ok(await page.$('.window-statusbar [data-status="safety"]'));
        }
      }
      if (viewport === 720) {
        // The rail hides the version, but opening the drawer exposes the
        // same control and choosing it closes the drawer and focuses Settings.
        await page.click('#window-sidebar-toggle');
        await page.waitForSelector('.sidebar-drawer.open');
        await page.waitForSelector('.brand-version',{visible:true});
        assert.equal(await page.$eval('.brand-version',e=>e.textContent),
          await page.$eval('meta[name="myflames-version"]',e=>e.content));
        await page.focus('.brand-version');
        await page.keyboard.press('Enter');
        await page.waitForSelector('.settings-page[data-section="updates"]');
        await page.waitForFunction(()=>document.activeElement?.id==='settings-section-title');
        assert.equal(await page.$('.sidebar-drawer.open'),null,'the version closes the 720 px drawer');
      } else await page.click('button[aria-label="Settings"]');
      await page.waitForSelector('.settings-page');
      await page.locator('.settings-page .source-item ::-p-text(Updates and news)').click();
      await page.waitForSelector('.settings-page .settings-update-status');
      const updatesText = await page.$eval('.settings-page .settings-update-status',e=>e.textContent);
      assert.match(updatesText,/myflames \d+\.\d+\.\d+/,'Settings names the installed version');
      assert.match(updatesText,/Check for updates/,'Settings has Check for updates');
      assert.equal(await page.$eval('.settings-page .settings-update-status a',e=>e.textContent),'View releases');
      assert.equal(await page.$eval('.settings-page .settings-update-status a',e=>e.href),'https://github.com/vgrippa/myflames/releases');
      const footerText = await page.$eval('.window-statusbar',e=>e.textContent);
      assert.equal(footerText.includes(await page.$eval('.brand-version',e=>e.textContent)),false,'Settings footer has no installed version');
      assert.doesNotMatch(footerText,/myflames|Check for updates|View releases/);
      assert.equal(await page.$eval('.window-statusbar',e=>e.tabIndex),-1,'the empty Settings footer is not a tab stop');
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await lastControlAboveFooter('.settings-panel', `${theme} ${viewport} Settings`, {requireControl:true});
      await page.keyboard.press('Escape');
      await page.waitForFunction(()=>!document.querySelector('.settings-page'));
    }
  }
  // Use the server's version-to-meta-tag seam, including the untouched Vite
  // placeholder that appVersion() reads as an unpackaged development build.
  for (const injectedVersion of ['3.5.0-rc.12','__MYFLAMES_VERSION__']) {
    await page.close();
    await stopServer();
    url = await startServer(injectedVersion);
    updateStatus = {schema_version:'app-update-status-1.0',current_version:injectedVersion,
      last_check:{checked_at:new Date().toISOString(),outcome:'available',current_version:injectedVersion,
        latest_version:'3.6.0',reason:null},auto_check:false,staging:null,staged:null,active_operations:[]};
    page = await newPage();
    await page.goto(url);
    await page.waitForSelector('.update-badge[data-phase="available"]');
    const version = injectedVersion.startsWith('__') ? 'development' : injectedVersion;
    assert.equal(await page.$eval('meta[name="myflames-version"]',e=>e.content),injectedVersion);
    for (const theme of ['light','dark']) {
      await api({theme});
      await page.reload();
      await page.waitForSelector('.update-badge[data-phase="available"]');
      await page.click('.sidebar-resizer',{count: 2});
      for (const sidebarWidth of [208,264,320]) {
        if (await width() !== sidebarWidth) await drag(sidebarWidth - await width());
        const row = await page.evaluate(() => {
          const head=document.querySelector('.sidebar-head'), version=document.querySelector('.brand-version');
          const brand=document.querySelector('.brand'), name=document.querySelector('.brand-name'), badge=document.querySelector('.update-badge');
          const h=head.getBoundingClientRect(), v=version.getBoundingClientRect(), n=name.getBoundingClientRect(), b=badge.getBoundingClientRect();
          const css=getComputedStyle(version);
          return {text:version.textContent,title:version.title,label:version.getAttribute('aria-label'),
            overflow:head.scrollWidth>head.clientWidth,ellipsis:css.textOverflow,nowrap:css.whiteSpace,
            truncated:version.scrollWidth>version.clientWidth,nameFits:name.scrollWidth<=name.clientWidth,
            brandFlex:getComputedStyle(brand).flexShrink,badgeWidth:b.width,
            inside:n.left>=h.left&&b.right<=h.right+1&&v.left>=n.right&&v.right<=b.left,
            oneRow:Math.abs((n.top+n.height/2)-(b.top+b.height/2))<2&&Math.abs((v.top+v.height/2)-(b.top+b.height/2))<2};
        });
        const label = `${theme} ${version}, sidebar ${sidebarWidth}: ${JSON.stringify(row)}`;
        assert.equal(await width(),sidebarWidth,label);
        assert.equal(row.text,version,label);
        assert.equal(row.label,`myflames ${version}: Updates and news settings`,label);
        assert.equal(row.title,row.label,label);
        assert.equal(row.overflow,false,label);
        assert.ok(row.inside&&row.oneRow&&row.nameFits,label);
        assert.equal(row.brandFlex,'0',label);
        assert.equal(row.badgeWidth,22,label);
        assert.equal(row.ellipsis,'ellipsis',label);
        assert.equal(row.nowrap,'nowrap',label);
        if (sidebarWidth===208) assert.equal(row.truncated,true,label);
      }
    }
  }
  assert.deepEqual(errors,[]);
  console.log('Shell resize, keyboard, clamps, restart persistence and all-place footers passed (1440/720, light/dark).');
} finally {
  try { await browser.close(); } finally {
    await stopServer();
    if (directory) await fs.rm(directory,{recursive:true,force:true});
  }
}
