// Synthetic >200-row reports prove complete client browsing, not a live gate.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { open, load } from './perf-harness.mjs';
const tables = await load('tables-mysql-8.4.json');
const catalog = await load('schemas-mysql-8.4.json');
const indexes = await load('indexes-mysql-8.4.json');
tables.tables = Array.from({length:251},(_,i)=>({schema:'mftab',table:`table_${String(i).padStart(3,'0')}`,engine:i%2?'InnoDB':'MyISAM',rows:i,data_bytes:i*10,index_bytes:i,size_bytes:i*11,partitioned:false}));
tables.more.tables=0;
indexes.unused=Array.from({length:251},(_,i)=>({...indexes.unused[0],schema:'mftab',table:`table_${String(i).padStart(3,'0')}`,index:`index_${String(i).padStart(3,'0')}`}));
indexes.more.unused=0;
indexes.indexes=indexes.unused.map(r=>({...r,type:"BTREE",unique:false,visible:true}));
indexes.more.indexes=0;
indexes.available=false; // Inventory must not depend on Performance Schema usage counters.
const h=await open('/api/live/tables','table-pagination',{'/api/live/schemas':{status:200,body:catalog},'/api/live/indexes':{status:200,body:indexes}});
const {page,state}=h;
state.fallback={status:200,body:tables};
try {
 await h.server(); await h.view('tables'); await h.refresh();
 const wrap='[data-report-table="tables.inventory"]';
 await page.waitForSelector(`${wrap} tbody tr`);
 assert.equal(await page.$$eval(`${wrap} tbody tr`,e=>e.length),50);
 await page.click(`${wrap} .report-table-pages button:last-child`);
 await page.waitForFunction(s=>document.querySelectorAll(`${s} tbody tr`).length===1,{},wrap);
 assert.equal(await page.$$eval(`${wrap} tbody tr`,e=>e.length),1);
 assert.equal(await page.$eval(`${wrap} tbody tr`,e=>e.dataset.table),'mftab.table_250');
 // A new sort applies to all rows before paging, and returns to page one.
 await page.click(`${wrap} th[data-col="rows"] button`);
 await page.waitForFunction(s=>document.querySelectorAll(`${s} tbody tr`).length===50,{},wrap);
 assert.equal(await page.$eval(`${wrap} tbody tr`,e=>e.dataset.table),'mftab.table_250');
 await page.evaluate(()=>{window.__exports=[];const old=URL.createObjectURL;URL.createObjectURL=b=>{b.text().then(t=>window.__exports.push(t));return old(b);};});
 await page.select(`${wrap} select[aria-label^="Export"]`,'csv');
 await page.waitForFunction(()=>window.__exports?.length===1);
 const csv=await page.evaluate(()=>window.__exports[0]);
 assert.equal(csv.trim().split('\r\n').length,252);
 assert.ok(csv.indexOf('table_250')<csv.indexOf('table_000'));
 await page.select(`${wrap} select[aria-label^="Export"]`,'html');
 await page.waitForFunction(()=>window.__exports?.length===2);
 assert.equal(await page.evaluate(()=>(window.__exports[1].match(/<tr>/g)||[]).length),252);
 // Exercise the real export iframe path, substituting only the blocking system dialog.
 await page.evaluate(()=>{const append=document.body.append.bind(document.body);document.body.append=(...nodes)=>{for(const node of nodes){if(node instanceof HTMLIFrameElement){const load=node.onload;node.onload=event=>{node.contentWindow.print=()=>{window.__printRows=node.contentDocument.querySelectorAll('tbody tr').length;};load.call(node,event);};}}append(...nodes);};});
 await page.select(`${wrap} select[aria-label^="Export"]`,'pdf');
 await page.waitForFunction(()=>window.__printRows===251);
 const printPage=await page.browser().newPage();
 await printPage.setContent(await page.evaluate(()=>window.__exports[1]));
 await fs.mkdir('/tmp/myflames-table-export-proof',{recursive:true});
 await printPage.pdf({path:'/tmp/myflames-table-export-proof/tables.pdf',printBackground:true,preferCSSPageSize:true});
 assert.ok(await printPage.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
 await printPage.screenshot({path:'/tmp/myflames-table-export-proof/tables-export.png',fullPage:true});
 await printPage.close();
 await page.select(`${wrap} select[aria-label^="Rows per page"]`,'100');
 await page.waitForFunction(s=>document.querySelectorAll(`${s} tbody tr`).length===100,{},wrap);
 assert.equal(await page.$$eval(`${wrap} tbody tr`,e=>e.length),100);
 await h.view('indexes'); await h.refresh();
 const idx='[data-report-table="indexes.inventory"]';
 await page.waitForSelector(`${idx} tbody tr`);
 assert.equal(await page.$$eval(`${idx} tbody tr`,e=>e.length),50);
 await page.click(`${idx} .report-table-pages button:last-child`);
 await page.waitForFunction(s=>document.querySelectorAll(`${s} tbody tr`).length===1,{},idx);
 assert.equal(await page.$$eval(`${idx} tbody tr`,e=>e.length),1);
 await page.type('input[aria-label="Filter indexes"]','table_2');
 await page.waitForFunction(s=>document.querySelectorAll(`${s} tbody tr`).length===50,{},idx);
 assert.equal(await page.$$eval(`${idx} tbody tr`,e=>e.length),50);
 assert.match(await page.$eval(`${idx} .report-table-tools`,e=>e.textContent),/of 51/);
 for(const theme of ['light','dark']) for(const width of [1440,390]) {
   await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:theme}]);
   await page.setViewport({width,height:900});
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
   await page.screenshot({path:`/tmp/myflames-table-export-proof/indexes-${theme}-${width}.png`,fullPage:true});
 }
 await page.setViewport({width:1440,height:900});
 await h.view('tables');
 for(const theme of ['light','dark']) for(const width of [1440,390]) {
   await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:theme}]);
   await page.setViewport({width,height:900});
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
   await page.screenshot({path:`/tmp/myflames-table-export-proof/tables-${theme}-${width}.png`,fullPage:true});
 }
 console.log('Tables/Indexes pagination: 251 rows, last page, global sort, full CSV/HTML exports, filtering and 390px layout passed.');
} finally {await h.close();}
