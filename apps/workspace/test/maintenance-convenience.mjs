// Tools updates/downloads and native folder selection. All external calls are mocked.
import assert from 'node:assert/strict';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {open,load} from './perf-harness.mjs';
const tools=await load('lifecycle-tools-mysql.json');
const catalogs=JSON.parse(execFileSync(process.env.PYTHON || 'python3',['-c',
 'import json; from myflames.dump_options import capability_catalog; print(json.dumps({n:capability_catalog(n,tool_version="8.4.0") for n in ("mysqlsh","mysqldump")}))'],{cwd:new URL('../../../',import.meta.url),encoding:'utf8'}));
for(const name of ['mysqlsh','mysqldump'])tools.tools[name].dump_capabilities=catalogs[name];
const tableCatalogs=JSON.parse(execFileSync(process.env.PYTHON || 'python3',['-c',
 'import json; from myflames.dump_options import capability_catalog; print(json.dumps({n:capability_catalog(n,table_scope=True,tool_version="8.4.0") for n in ("mysqlsh","mysqldump")}))'],{cwd:new URL('../../../',import.meta.url),encoding:'utf8'}));
for(const name of ['mysqlsh','mysqldump'])tools.tools[name].dump_table_capabilities=tableCatalogs[name];
const schemas=await load('schemas-mysql-8.4.json');
// catalog-1.0 with stats, 200 per page: the first page of mftab is truncated, the second ends it.
const table=(name,extra={})=>({name,type:'table',engine:'InnoDB',rows:1200,bytes:16384*3,...extra});
const pages={first:[table('customers'),table('orders',{rows:2400000,bytes:410e6}),table('audit_log',{engine:'MyISAM',rows:1}),{name:'v_daily',type:'view',engine:null,rows:null,bytes:null}],
 second:[table('payments')]};
let reloadWithout=null;
const catalogAnswer=(b)=>{
 // reloadWithout: one complete page without that table (it was dropped).
 if(reloadWithout)return {status:200,body:{schema_version:'catalog-1.0',level:'tables',schema:b.schema,tables:[...pages.first,...pages.second].filter(t=>t.name!==reloadWithout),truncated:false,limit:200,prefix:null,after:null,next_after:null,stats:true}};
 const rows=b.after?pages.second:pages.first;
 return {status:200,body:{schema_version:'catalog-1.0',level:'tables',schema:b.schema,tables:rows,truncated:!b.after,limit:200,prefix:null,after:b.after||null,next_after:b.after?null:rows.at(-1).name,stats:true}};
};
const refused=await load('lifecycle-refused-dump.json');
for (const [name, tool] of Object.entries(tools.tools)) {
 tool.download_url=name.startsWith('mariadb')?'https://mariadb.org/download/':'https://dev.mysql.com/downloads/';
 tool.update={status:tool.deprecated?'deprecated':tool.available?'current':'not_installed',latest_version:null,checked_at:'2026-10-16T16:00:00Z',source_url:tool.download_url,message:'Official release check'};
}
tools.tools.mysqlsh.update={...tools.tools.mysqlsh.update,status:'available',latest_version:'9.7.1'};
tools.tools.mysqldump.update={...tools.tools.mysqldump.update,status:'unknown',message:'Could not reach official release page.'};
// An untrusted URL must never become an executable link.
tools.tools.mysqlpump.download_url='javascript:alert(1)';
let folder={status:200,body:{path:'/tmp/Backup folder – café',cancelled:false}};
const picker='/api/lifecycle/choose-directory';
const h=await open('/api/lifecycle/tools','maintenance-convenience',{
 '/api/lifecycle/jobs':{status:200,body:{jobs:[]}},
 [picker]:()=>folder,
 '/api/lifecycle/prepare':{status:200,body:refused},
 '/api/live/schemas':{status:200,body:schemas},
 '/api/live/catalog':catalogAnswer,
});
const {page,state}=h;
const view='.server-view[data-view="maintenance"]';
const field=()=>page.$eval(`${view} .ops-folder-input input`,e=>e.value);
try {
 await h.server();await h.view('maintenance');state.fallback={status:200,body:tools};
 assert.equal(state.seen.length,0);
 await h.button('Detect tools',`${view} .view-empty `);
 await page.waitForSelector('[data-row-key="tool:mysqlsh"]');
 assert.equal(state.seen[0].check_updates,true);
 assert.match(await h.text('[data-row-key="tool:mysqlsh"]'),/Update available: 9.7.1/);
 assert.match(await h.text('[data-row-key="tool:mysqldump"]'),/Could not check/);
 assert.match(await h.text('.ops-tool-updates'),/mysqlsh/);
 assert.equal(await page.$eval('[data-row-key="tool:mysqlsh"] a',e=>e.href),'https://dev.mysql.com/downloads/');
 assert.equal(await page.$('[data-row-key="tool:mysqlpump"] a'),null);
 for(const theme of ['light','dark']) {
  await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:theme}]);
  await page.screenshot({path:path.join(h.shots,`tools-${theme}-1440.png`)});
  await h.narrow(`tools-${theme}-390.png`);
 }
 await page.locator(`${view} .segmented button ::-p-text(Dump)`).click();
 await page.click('button[aria-label="Browse for output folder"]');
 await page.waitForFunction(()=>document.querySelector('.ops-folder-input input')?.value.includes('café'));
 assert.equal(await field(),folder.body.path);
 assert.deepEqual(state.other.find(r=>r.path===picker).body,{});
 assert.equal(state.other.some(r=>/prepare|start/.test(r.path)),false,'selection does not prepare or start a backup');
 folder={status:200,body:{path:null,cancelled:true}};
 await page.click('button[aria-label="Browse for output folder"]');
 await page.waitForFunction(()=>!document.querySelector('.ops-folder-input button').disabled);
 assert.equal(await field(),'/tmp/Backup folder – café','cancel preserves the previous path');
 folder={status:400,body:{error:'Native folder selection is unavailable.'}};
 await page.click('button[aria-label="Browse for output folder"]');
 await page.waitForSelector('.ops-field [role=alert]');
 assert.match(await h.text('.ops-field [role=alert]'),/enter the path/);
 const input=await page.$('.ops-folder-input input');await input.click({clickCount:3});await page.keyboard.press('Backspace');await input.type('/tmp/manual-backup');
 assert.equal(await field(),'/tmp/manual-backup');
 assert.equal(await page.$('.ops-field [role=alert]'),null);
 await page.click('.ops-dump-options summary');
 await page.select('select[aria-label="Scope"]','instance');
 // The options are a two-column form in sections: Performance, Contents, Compression; then one encryption line.
 assert.deepEqual(await h.texts(`${view} .dump-group > legend`),['Performance','Contents','Compression']);
 assert.equal(await page.$eval(`${view} .dump-group[data-group=performance] .form-row:first-child > label`,e=>e.textContent),'Threads');
 assert.equal(await page.$eval(`${view} .dump-group[data-group=performance] .number-unit`,e=>e.textContent),'threads');
 assert.deepEqual(await page.$$eval(`${view} .dump-group[data-group=contents] .checkbox`,es=>es.map(e=>e.textContent)),['Triggers','Stored routines','Events']);
 assert.equal(await page.$eval(`${view} .dump-group[data-group=contents] [role=group]`,e=>document.getElementById(e.getAttribute('aria-labelledby')).textContent),'Include');
 // Every label sits left of its control, and each checkbox on the line of its own label.
 const geometry=await page.$$eval(`${view} .dump-options .form-row`,rows=>rows.map(r=>{
  const label=r.firstElementChild.getBoundingClientRect(),control=r.lastElementChild.getBoundingClientRect();
  return {left:label.right<=control.left+1,top:Math.abs(label.top-control.top)<12};
 }));
 assert.ok(geometry.length>=6 && geometry.every(g=>g.left&&g.top),JSON.stringify(geometry));
 const boxes=await page.$$eval(`${view} .dump-options .checkbox`,ls=>ls.map(l=>{const i=l.querySelector('input').getBoundingClientRect(),t=l.getBoundingClientRect();
  return {beside:i.right<=t.right&&i.left-t.left<4,middle:Math.abs((i.top+i.bottom)/2-(t.top+t.bottom)/2)<3};}));
 assert.ok(boxes.length>=4 && boxes.every(b=>b.beside&&b.middle),JSON.stringify(boxes));
 assert.equal(await h.text(`${view} .dump-encryption > span`),'Backup files are not encrypted.');
 await page.select(`${view} .dump-group[data-group=compression] select`,'gzip');
 const level=await page.$(`${view} .dump-group[data-group=compression] input[type=number]`);await level.type('7');
 assert.equal(await level.evaluate(e=>e.max),'9');
 assert.match(await h.text(`${view} .ops-dump-options > summary`),/4 threads · Gzip, level 7/);
 await page.locator(`${view} .ops-actions button ::-p-text(Run preflight)`).click();
 await page.waitForFunction(()=>document.querySelector('.ops-actions')?.textContent.includes('Unverified'));
 for(let i=0;i<100&&!state.other.some(r=>r.path==='/api/lifecycle/prepare');i++)await new Promise(r=>setTimeout(r,25));
 assert.deepEqual(state.other.find(r=>r.path==='/api/lifecycle/prepare').body.options,{compression:'gzip',compression_level:7});
 if(await page.$('.detail-back'))await page.click('.detail-back');
 // Wait for the preflight to finish before changing its disabled form.
 await page.waitForFunction(()=>!document.querySelector('select[aria-label="Tool"]')?.disabled);
 await page.select('select[aria-label="Tool"]','sql');
 await page.waitForFunction(()=>document.querySelector('.ops-dump-options')?.textContent.includes('Options for mysqldump'));
 assert.equal(await page.$(`${view} .dump-group[data-group=compression] select`),null);
 assert.deepEqual(await h.texts(`${view} .dump-group > legend`),['Contents','SQL output','Compression']);
 assert.match(await h.text('.ops-dump-options'),/backup file stays uncompressed/);
 assert.match(await h.text('.ops-dump-options'),/do not offer encryption/);
 assert.match(await h.text('select[aria-label="Tool"] option[value=sql]'),/^SQL dump \(mysqldump\)$/);
 await page.select('select[aria-label="Tool"]','auto');
 await page.waitForFunction(()=>document.querySelector('.ops-dump-options')?.textContent.includes('Options for mysqlsh'));

 // One database: its tables are read and offered in a searchable list, All tables by default.
 const catalogReads=()=>state.other.filter(r=>r.path==='/api/live/catalog');
 await page.select('select[aria-label="Scope"]','schemas');
 assert.equal(catalogReads().length,0,'no table read before a database is chosen');
 await page.click(`${view} #ops-dump-schemas`);
 await page.waitForSelector(`${view} .perf-picker-panel`);
 await page.locator(`${view} .perf-picker-option ::-p-text(mftab)`).click();
 await page.keyboard.press('Escape');
 await page.waitForSelector(`${view} [data-dump-tables="mftab"] #ops-dump-tables`);
 for(let i=0;i<100&&!catalogReads().length;i++)await new Promise(r=>setTimeout(r,20));
 assert.deepEqual([catalogReads()[0].body.schema,catalogReads()[0].body.stats,catalogReads()[0].body.after],['mftab',true,null]);
 await page.waitForFunction(()=>document.querySelector('#ops-dump-tables')?.textContent==='All tables');
 await page.click(`${view} [data-dump-tables] .perf-picker-button`);
 await page.waitForSelector('.dump-table-panel');
 // Focus: the search field on open; Done and Escape return it to the Tables button.
 const tablesButton=`${view} [data-dump-tables] .perf-picker-button`;
 const focused=selector=>page.evaluate(s=>document.activeElement===document.querySelector(s),selector);
 await page.waitForFunction(()=>document.activeElement?.matches('.dump-table-panel input[type=search]'));
 assert.deepEqual(await page.$$eval('.dump-table-panel [data-table]',es=>es.map(e=>e.dataset.table)),['customers','orders','audit_log','v_daily']);
 assert.match(await h.text('.dump-table-panel [data-table=orders]'),/InnoDB · ≈2\.40M rows · 391\.0 MiB/);
 assert.match(await h.text('.dump-table-panel [data-table=audit_log]'),/MyISAM · ≈1 row · /);
 assert.match(await h.text('.dump-table-panel [data-table=v_daily]'),/view$/);
 assert.match(await h.text('.dump-table-panel .perf-picker-hint'),/first 4 tables/);
 await page.locator('.dump-table-panel button ::-p-text(Load more tables)').click();
 await page.waitForSelector('.dump-table-panel [data-table=payments]');
 assert.equal(catalogReads().at(-1).body.after,'v_daily');
 assert.equal(await page.$('.dump-table-panel button ::-p-text(Load more tables)'),null);
 await page.type('.dump-table-panel input[type=search]','or');
 assert.deepEqual(await page.$$eval('.dump-table-panel [data-table]',es=>es.map(e=>e.dataset.table)),['orders']);
 await page.click('.dump-table-panel [data-table=orders] input');
 await page.$eval('.dump-table-panel input[type=search]',e=>{e.select();});await page.keyboard.press('Backspace');
 await page.click('.dump-table-panel [data-table=customers] input');
 await page.locator('.dump-table-panel button ::-p-text(Done)').click();
 assert.equal(await h.text('#ops-dump-tables'),'orders, customers');
 assert.equal(await page.$('.dump-table-panel'),null);
 assert.ok(await focused(tablesButton),'Done returns focus to the Tables button');
 // A table scope offers the table options of the tool: mysqlsh dumps no routines or events with dumpTables.
 await page.waitForFunction(v=>[...document.querySelectorAll(`${v} .dump-group[data-group=contents] .checkbox`)].map(e=>e.textContent).join()==='Triggers',{},view);
 const before=state.other.filter(r=>r.path==='/api/lifecycle/prepare').length;
 await page.locator(`${view} .ops-actions button ::-p-text(Run preflight)`).click();
 for(let i=0;i<100&&state.other.filter(r=>r.path==='/api/lifecycle/prepare').length===before;i++)await new Promise(r=>setTimeout(r,25));
 const asked=state.other.filter(r=>r.path==='/api/lifecycle/prepare').at(-1).body;
 assert.deepEqual([asked.schemas,asked.tables],[['mftab'],['orders','customers']],'the plan uses the chosen names exactly');
 if(await page.$('.detail-back'))await page.click('.detail-back');
 await page.waitForFunction(()=>!document.querySelector('select[aria-label="Tool"]')?.disabled);
 for(const theme of ['light','dark']) {
  await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:theme}]);
  await page.screenshot({path:path.join(h.shots,`dump-tables-${theme}-1440.png`)});
 }
 await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:'light'}]);
 const blocked=()=>page.$eval(`${view} .ops-actions button.primary`,e=>e.disabled||e.getAttribute('aria-disabled')==='true');
 // Leaving Dump keeps the loaded pages: back on Dump, a table chosen from page 2 still counts and nothing is read again.
 await page.click(`${view} [data-dump-tables] .perf-picker-button`);
 await page.click('.dump-table-panel [data-table=payments] input');
 await page.keyboard.press('Escape');
 await page.waitForFunction(()=>!document.querySelector('.dump-table-panel'));
 assert.ok(await focused(tablesButton),'Escape returns focus to the Tables button');
 const readsBefore=catalogReads().length;
 await page.locator(`${view} .segmented button ::-p-text(Restore)`).click();
 await page.waitForFunction(v=>!document.querySelector(`${v} [data-dump-tables]`),{},view);
 await page.locator(`${view} .segmented button ::-p-text(Dump)`).click();
 await page.waitForSelector(`${view} [data-dump-tables] #ops-dump-tables`);
 await new Promise(r=>setTimeout(r,300));
 assert.equal(catalogReads().length,readsBefore,'no table read again after coming back to Dump');
 assert.equal(await h.text('#ops-dump-tables'),'3 tables');
 assert.equal(await page.$(`${view} [data-dump-tables] .form-error`),null);
 assert.equal(await blocked(),false);
 // A chosen table from a later page is not confirmed by a reload of the first page: Load more confirms it.
 await page.click(`${view} [data-dump-tables] .perf-picker-button`);
 await page.locator('.dump-table-panel button ::-p-text(Reload list)').click();
 await page.waitForFunction(()=>!document.querySelector('.dump-table-panel [data-table=payments]'));
 await page.keyboard.press('Escape');
 await page.waitForSelector(`${view} [data-dump-tables] .form-error[role=alert]`);
 assert.match(await h.text(`${view} [data-dump-tables] .form-error`),/Not in the tables loaded so far: payments\. Load more tables/);
 assert.equal(await blocked(),true);
 await page.click(`${view} [data-dump-tables] .perf-picker-button`);
 await page.locator('.dump-table-panel button ::-p-text(Load more tables)').click();
 await page.waitForSelector('.dump-table-panel [data-table=payments]');
 await page.keyboard.press('Escape');
 await page.waitForFunction(v=>!document.querySelector(`${v} [data-dump-tables] .form-error`),{},view);
 // A reload that no longer lists a chosen table blocks the preflight and says which.
 reloadWithout='orders';
 await page.click(`${view} [data-dump-tables] .perf-picker-button`);
 await page.locator('.dump-table-panel button ::-p-text(Reload list)').click();
 await page.waitForFunction(()=>!document.querySelector('.dump-table-panel [data-table=orders]'));
 await page.keyboard.press('Escape');
 await page.waitForSelector(`${view} [data-dump-tables] .form-error[role=alert]`);
 assert.match(await h.text(`${view} [data-dump-tables] .form-error`),/No longer in mftab: orders/);
 assert.equal(await blocked(),true);
 reloadWithout=null;
 // Two databases: no table list; the chosen tables are dropped.
 await page.click(`${view} #ops-dump-schemas`);
 await page.waitForSelector(`${view} .perf-picker-panel`);
 await page.locator(`${view} .perf-picker-option ::-p-text(mfother)`).click();
 await page.keyboard.press('Escape');
 await page.waitForFunction(v=>!document.querySelector(`${v} [data-dump-tables]`),{},view);
 assert.match(await h.text(`${view} .ops-dump .form-row-control > .hint`),/Choose one database to pick its tables/);
 for(const theme of ['light','dark']) {
  await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:theme}]);
  await page.screenshot({path:path.join(h.shots,`dump-${theme}-1440.png`)});
  await h.narrow(`dump-${theme}-390.png`);
 }
 assert.deepEqual(h.errors,[]);
 console.log('PASS maintenance downloads, update notices, folder selection/cancel/error/manual fallback and responsive layouts');
}finally{await h.close()}
