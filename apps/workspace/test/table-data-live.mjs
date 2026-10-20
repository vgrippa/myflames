// Opt-in live proof. Only owned disposable servers; credentials never logged.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {open} from './perf-harness.mjs';
import {chooseConnection} from './chrome-helpers.mjs';
if(!process.env.MYFLAMES_GRID_LIVE_CONFIG)throw Error('Set MYFLAMES_GRID_LIVE_CONFIG to the owned disposable server config.');
const servers=JSON.parse(await fs.readFile(process.env.MYFLAMES_GRID_LIVE_CONFIG,'utf8'));
assert.ok(servers.every(s=>s.owner==='myflames-macos-editor-20261016'&&s.name.startsWith('myflames-macos-editor-20261016-')));
const extra={};
for(const route of ['live/schemas','live/catalog','live/privileges','editor/status','editor/history',...['open','prepare','apply','decline','status','cancel','close'].map(x=>'table-data/'+x)])extra['/api/'+route]=(body,request)=>{const target=servers.find(s=>s.port===Number(body.connection?.port));if(target)body.connection={...body.connection,password:target.password,ssl_mode:'PREFERRED'};void request.continue({postData:JSON.stringify(body)});return null;};
const h=await open('/unused','table-data-live',extra),{page}=h;
try{
 const saved=[];
 for(const server of servers){
  const id=await page.evaluate(async value=>{const proof=localStorage.getItem('myflames.session-proof')||'';const token=(await(await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Bootstrap':'1','X-Myflames-Token':proof},body:'{}'})).json()).token;const response=await fetch('/api/profiles/save',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':token},body:JSON.stringify({value})});const result=await response.json();if(!response.ok)throw Error(result.error);return result.id;},{name:'Grid '+server.engine,host:'127.0.0.1',port:server.port,user:'root',ssl_mode:'PREFERRED',type:'development'});saved.push({server,id});
 }
 await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await h.button('Query editor','.main-nav ');
 for(const {server,id} of saved){
  await chooseConnection(page,id);
  if(await page.$('button[aria-label="Show the schema browser"]'))await page.click('button[aria-label="Show the schema browser"]');
  await h.button('Load schemas','.schema-browser ');
  await page.waitForFunction(()=>[...document.querySelectorAll('.tree-row.kind-schema')].some(e=>e.querySelector('.tree-label')?.textContent==='grid_lab'));
  for(const node of await page.$$('.tree-row.kind-schema'))if(await node.$eval('.tree-label',e=>e.textContent==='grid_lab')){if(await node.evaluate(e=>e.getAttribute('aria-expanded')!=='true'))await node.click();break;}
  await page.waitForFunction(()=>[...document.querySelectorAll('.tree-row.kind-table')].some(e=>e.querySelector('.tree-label')?.textContent==='customers'));
  for(const node of await page.$$('.tree-row.kind-table'))if(await node.$eval('.tree-label',e=>e.textContent==='customers')){await node.click({button:'right'});break;}
  await h.button('Edit table data');await page.waitForSelector('.table-data-panel input');
  const value='Browser applied '+server.engine;
  await page.locator('.table-data-panel tbody tr:first-child input[aria-label^="name for "]').fill(value);
  await h.button('Review 1 changes','.table-data-panel ');await page.waitForSelector('.confirm-sheet');
  assert.match(await h.text('.confirm-sheet'),/LOCK TABLES.*WRITE/s);assert.match(await h.text('.confirm-sheet'),/blocks other sessions/);
  await h.button('Apply changes','.confirm-sheet ');
  await page.waitForFunction(()=>document.querySelector('.table-data-panel [role=status]')?.textContent.includes('Changes committed'),{timeout:20000});
  await h.button('Reload rows','.table-data-panel ');
  await page.waitForFunction(expected=>document.querySelector('.table-data-panel tbody tr:first-child input[aria-label^="name for "]')?.value===expected,{},value);
  assert.equal(await page.$eval('.table-data-panel tbody tr:nth-child(3) input[aria-label^="id for "]',e=>e.value),'9007199254740993');
  // Insert defaults are omitted, then delete only the server-owned row key.
  await h.button('Add row','.table-data-panel ');
  for(const [column,value] of [['id','77'],['name','Browser inserted']]){
    await page.select(`.table-data-panel tbody tr:last-child select[aria-label^="${column} value type"]`,'text');
    await page.locator(`.table-data-panel tbody tr:last-child input[aria-label^="${column} for "]`).fill(value);
  }
  await h.button('Review 1 changes','.table-data-panel ');await page.waitForSelector('.confirm-sheet');
  assert.match(await h.text('.confirm-sheet'),/INSERT INTO/);await h.button('Apply changes','.confirm-sheet ');
  await page.waitForFunction(()=>document.querySelector('.table-data-panel [role=status]')?.textContent.includes('Changes committed'),{timeout:20000});
  await h.button('Reload rows','.table-data-panel ');
  await page.waitForFunction(()=>[...document.querySelectorAll('.table-data-panel input[aria-label^="id for "]')].some(e=>e.value==='77'));
  for(const row of await page.$$('.table-data-panel tbody tr'))if(await row.$eval('input[aria-label^="id for "]',e=>e.value==='77')){await row.$eval('button',e=>e.click());break;}
  await h.button('Review 1 changes','.table-data-panel ');await page.waitForSelector('.confirm-sheet');
  assert.match(await h.text('.confirm-sheet'),/DELETE FROM.*WHERE.*77/s);await h.button('Apply changes','.confirm-sheet ');
  await page.waitForFunction(()=>document.querySelector('.table-data-panel [role=status]')?.textContent.includes('Changes committed'),{timeout:20000});
  await h.button('Reload rows','.table-data-panel ');
  await page.waitForFunction(()=>![...document.querySelectorAll('.table-data-panel input[aria-label^="id for "]')].some(e=>e.value==='77'));
  const dir=process.env.TABLE_DATA_SHOTS||h.shots;await fs.mkdir(dir,{recursive:true});await page.screenshot({path:dir+'/table-data-live-'+server.engine+'.png'});
  await h.button('Close table','.table-data-panel ');await page.waitForFunction(()=>!document.querySelector('.table-data-panel'));
  console.log(server.engine+': actual schema context menu, cell update/insert/delete, exact reviews, Apply and persisted rereads passed');
 }
 assert.deepEqual(h.errors,[]);
}finally{await h.close();}
