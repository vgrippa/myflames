// Responsive review evidence with mocked reports and the real workspace shell.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { open, load, pause } from './perf-harness.mjs';
// Screenshots only when TABLE_GUTTER_SHOTS names a folder (never into the repository).
const out = process.env.TABLE_GUTTER_SHOTS ? path.resolve(process.env.TABLE_GUTTER_SHOTS) : null;
if (out) await fs.mkdir(out, { recursive: true });
const mocks = {};
for (const [endpoint, fixture] of Object.entries({
  '/api/live/indexes': 'indexes-mysql-8.4.json', '/api/live/tables': 'tables-mysql-8.4.json',
  '/api/live/config': 'parameters-mysql.json', '/api/live/schemas': 'schemas-mysql-8.4.json',
  '/api/live/sessions': 'sessions-mysql-8.4-active.json', '/api/live/locks': 'locks-mysql-8.4.json',
  '/api/lifecycle/tools': 'lifecycle-tools-mysql.json',
})) mocks[endpoint] = { status: 200, body: await load(fixture) };
mocks['/api/lifecycle/jobs'] = {status:200,body:{jobs:[]}};
mocks['/api/audit/list'] = {status:200,body:{schema_version:'audit-1.0',retention_days:90,has_more:false,
  records:Array.from({length:12},(_,i)=>({id:String(i),attempted_at:'2026-10-28T12:00:00Z',finished_at:'2026-10-28T12:00:01Z',
    status:'done',outcome:'done',outcome_unknown:false,surface:'ui',os_user:'demo',operation:'kill',
    connection:{profile_id:null,profile_name:'Local MySQL',type:'development',read_only:false,host:'127.0.0.1',port:3306,user:'demo',engine:'mysql',server_version:'8.4.11'},
    statements:['KILL QUERY '+(93+i)],target:null,verified:null,policy:null,error_code:null,message:'Completed.',rows_affected:null,after:null,duration_ms:906+i}))}};
const h = await open('/unused', 'w1kit-review', mocks);
const {page} = h;
const metrics = [];
// Save the normal view and its fully scrolled right edge. Report actual
// pixels, including offscreen columns, rather than infer them from CSS.
async function capture(name) {
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  if (out) await page.screenshot({path:path.join(out,`${name}.png`)});
  const result = await page.$$eval('.data-table-frame', frames => frames.filter(f => f.offsetParent !== null).map(f => {
    const wrap=f.querySelector('.data-table-wrap'), t=f.querySelector('table');
    const heads=[...t.querySelectorAll('thead th[data-col]')], last=heads.at(-1);
    const menu=f.querySelector('.table-columns-cell') || t.querySelector('thead .col-menu');
    const end=wrap.scrollWidth-wrap.clientWidth, samples=[];
    for (const offset of [0,end/2,end]) {
      wrap.scrollLeft=offset;
      samples.push({offset:wrap.scrollLeft,viewportRight:wrap.getBoundingClientRect().right,menuLeft:menu.getBoundingClientRect().left});
    }
    const lr=last.getBoundingClientRect(), mr=menu.getBoundingClientRect();
    return {label:t.getAttribute('aria-label'),frame:wrap.clientWidth,scrollWidth:wrap.scrollWidth,
      last:last.dataset.col,lastRight:lr.right,menuLeft:mr.left,gap:mr.left-lr.right,menuWidth:mr.width,samples,
      columns:heads.map(e=>[e.dataset.col,e.getBoundingClientRect().width]),
      rows:[...t.querySelectorAll('tbody tr[data-row-key]')].filter(e=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=Math.min(wrap.getBoundingClientRect().bottom,innerHeight)}).length};
  }));
  metrics.push({name,tables:result});
  if (out) await fs.writeFile(path.join(out,'metrics.json'),JSON.stringify(metrics,null,2));
  if (out && result.some(t=>t.scrollWidth>t.frame)) await page.screenshot({path:path.join(out,`${name}-right-end.png`)});
  if (!process.env.W1KIT_BASELINE) for (const table of result) {
    for (const sample of table.samples) assert.ok(sample.viewportRight<=sample.menuLeft+1,JSON.stringify({name,...table}));
    assert.ok(table.lastRight<=table.menuLeft+1,JSON.stringify({name,...table}));
    if (name.startsWith('config-720-')) assert.deepEqual(table.columns.map(c=>c[0]),['name','value','apply_type','source']);
  }
}
async function sizes(name) {
  for (const width of [1440,720]) for (const theme of ['light','dark']) {
    await page.setViewport({width,height:width===1440?900:600});
    await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
    await capture(`${name}-${width}-${theme}`);
  }
}

try {
  await h.server();
  for (const view of ['indexes','tables','config','locks','maintenance','sessions']) {
    if (view === 'sessions') await h.workload();
    await h.view(view);
    await h.refresh();
    await h.idle(`.server-view[data-view=${view}]`);
    await page.waitForSelector(`.server-view[data-view=${view}] .data-table`);
    for (const width of [1440,720]) for (const theme of ['light','dark']) {
      await page.setViewport({width,height:width===1440?900:600});
      await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
      await capture(`${view}-${width}-${theme}`);
      if (['indexes','tables','config'].includes(view)) {
        const row = await page.$(`.server-view[data-view=${view}] tbody tr[data-row-key]`);
        await row.click();
        await pause(100);
        await capture(`${view}-inspector-${width}-${theme}`);
        if (await page.$('.detail-back')) await page.click('.detail-back');
        else await page.keyboard.press('Escape');
      }
    }
    await page.setViewport({width:1440,height:900});
  }
  await page.click('button[aria-label="Settings"]');
  await page.locator('#settings-popover button ::-p-text(Audit log)').click();
  await page.waitForSelector('table[aria-label="Audit records"]');
  await sizes('audit');
  await h.button('Home','.main-nav ');
  await h.button('Explore sample plan');
  // The chart view buttons show at desktop width (a select replaces them below 900 px).
  await page.setViewport({ width: 1440, height: 900 });
  await page.waitForSelector('.chart-views');
  await h.button('Stats', '.chart-views ');
  await page.waitForSelector('.plan-statistics table');
  await sizes('plans-stats');
  assert.deepEqual(h.errors,[]);
  console.log(JSON.stringify(metrics));
} finally {await h.close();}
