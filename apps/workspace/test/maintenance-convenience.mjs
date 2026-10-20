// Tools updates/downloads and native folder selection. All external calls are mocked.
import assert from 'node:assert/strict';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {open,load} from './perf-harness.mjs';
const tools=await load('lifecycle-tools-mysql.json');
const catalogs=JSON.parse(execFileSync(process.env.PYTHON || 'python3',['-c',
 'import json; from myflames.dump_options import capability_catalog; print(json.dumps({n:capability_catalog(n,tool_version="8.4.0") for n in ("mysqlsh","mysqldump")}))'],{cwd:new URL('../../../',import.meta.url),encoding:'utf8'}));
for(const name of ['mysqlsh','mysqldump'])tools.tools[name].dump_capabilities=catalogs[name];
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
 await page.select('select[aria-label="Backup compression"]','gzip');
 const level=await page.$('input[aria-label="Compression level"]');await level.type('7');
 assert.equal(await level.evaluate(e=>e.max),'9');
 await page.locator(`${view} .ops-actions button ::-p-text(Run preflight)`).click();
 await page.waitForFunction(()=>document.querySelector('.ops-actions')?.textContent.includes('Unverified'));
 for(let i=0;i<100&&!state.other.some(r=>r.path==='/api/lifecycle/prepare');i++)await new Promise(r=>setTimeout(r,25));
 assert.deepEqual(state.other.find(r=>r.path==='/api/lifecycle/prepare').body.options,{compression:'gzip',compression_level:7});
 if(await page.$('.detail-back'))await page.click('.detail-back');
 // Wait for the preflight to finish before changing its disabled form.
 await page.waitForFunction(()=>!document.querySelector('select[aria-label="Tool"]')?.disabled);
 await page.select('select[aria-label="Tool"]','sql');
 await page.waitForFunction(()=>document.querySelector('.ops-dump-options')?.textContent.includes('Options for mysqldump'));
 assert.equal(await page.$('select[aria-label="Backup compression"]'),null);
 assert.match(await h.text('.ops-dump-options'),/backup file remains uncompressed/);
 assert.match(await h.text('.ops-dump-options'),/do not offer encryption/);
 for(const theme of ['light','dark']) {
  await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:theme}]);
  await page.screenshot({path:path.join(h.shots,`dump-${theme}-1440.png`)});
  await h.narrow(`dump-${theme}-390.png`);
 }
 assert.deepEqual(h.errors,[]);
 console.log('PASS maintenance downloads, update notices, folder selection/cancel/error/manual fallback and responsive layouts');
}finally{await h.close()}
