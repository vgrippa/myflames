// Explicit table-grid editing through schema browsing. Synthetic API replies;
// optional live proof is a separate script using only owned disposable servers.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { open, pause } from './perf-harness.mjs';
import { openConnectionPicker, chooseConnection } from './chrome-helpers.mjs';
const text = value => ({kind:'text',value});
const pageData = {snapshot_id:'snapshot',schema:'grid_lab',table:'customers',columns:[
 {name:'id',type:'bigint unsigned',kind:'text',nullable:false},
 {name:'name',type:'varchar(120)',kind:'text',nullable:false},
 {name:'email',type:'varchar(255)',kind:'text',nullable:true},
 {name:'payload',type:'varbinary(32)',kind:'hex',nullable:true}],rows:[
 {id:'first',values:[text('9007199254740993'),text('Alice'),{kind:'null',value:null},{kind:'hex',value:'00FF'}]},
 {id:'second',values:[text('2'),text('Bruno'),text(''),{kind:'hex',value:''}]}],editable:true,reason:'',key:['id'],offset:0,limit:50,has_more:false,expires_in:300};
let prepared, applyCount=0, mode='complete', statusConcurrent=0, maxConcurrent=0;
const ok = body => ({status:200,body});
const extra={
 '/api/live/schemas':ok({schemas:[{name:'grid_lab',tables:1,system:false}],system:[],user_tables:1}),
 '/api/live/catalog':body=>ok(body.table ? {level:'table',schema:body.schema,table:body.table,columns:[],indexes:[]} : {level:'tables',schema:body.schema,tables:[{name:'customers',type:'table'}],limit:200,truncated:false}),
 '/api/table-data/open':()=>ok(pageData),
 '/api/table-data/close':ok({status:'closed'}),
 '/api/table-data/prepare':body=>{prepared=body;return ok({snapshot_id:'snapshot',review_token:'one-use',statements:['SET SESSION autocommit = 0','LOCK TABLES `grid_lab`.`customers` WRITE',"UPDATE `grid_lab`.`customers` SET `name` = CONVERT(X'426F62' USING utf8mb4) WHERE `id` = 9007199254740993 LIMIT 1",'COMMIT','UNLOCK TABLES'],privileges:[],connection:{host:'127.0.0.1',port:3306,user:'root'},connection_type:'development',warnings:['A temporary table WRITE lock blocks other sessions from reading or writing this table.']});},
 '/api/table-data/decline':ok({status:'ready'}),
 '/api/table-data/apply':(_body,request)=>{applyCount++;if(mode==='lost'){void request.abort('failed');return null;}return ok({status:'running',snapshot_id:'snapshot'});},
 '/api/table-data/status':(_body,request)=>{statusConcurrent++;maxConcurrent=Math.max(maxConcurrent,statusConcurrent);setTimeout(()=>{statusConcurrent--;void request.respond({status:mode==='lost'?503:200,contentType:'application/json',body:JSON.stringify(mode==='lost'?{error:'status unavailable'}:{snapshot_id:'snapshot',status:mode,message:mode==='complete'?'Changes committed. Reload the table.':'The outcome is unknown. Changes may have committed.'})});},900);return null;},
 '/api/editor/status':ok({status:'none',session:null,run:null}),
 '/api/editor/history':ok({items:[]}),
};
const h=await open('/unused','table-data',extra), {page}=h;
try {
 const saveProfile=async(name,port)=>page.evaluate(async({name,port})=>{const headers={'Content-Type':'application/json','X-Myflames-Bootstrap':'1','X-Myflames-Token':localStorage.getItem('myflames.session-proof')||''};const token=(await(await fetch('/api/auth/session',{method:'POST',headers,body:'{}'})).json()).token;return(await(await fetch('/api/profiles/save',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':token},body:JSON.stringify({value:{name,host:'127.0.0.1',port,user:'root'}})})).json()).id;},{name,port});
 const first=await saveProfile('First grid connection',3306),other=await saveProfile('Other grid connection',3307);
 await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
 await h.button('Query editor','.main-nav ');
 await chooseConnection(page,first);
 if(await page.$('button[aria-label="Show the schema browser"]')) await page.click('button[aria-label="Show the schema browser"]');
 await h.button('Load schemas','.schema-browser ');
 await page.waitForSelector('.tree-row.kind-schema'); await page.click('.tree-row.kind-schema');
 async function openGrid(){await page.waitForSelector('.tree-row.kind-table');await page.click('.tree-row.kind-table',{button:'right'});await h.button('Edit table data');await page.waitForSelector('.table-data-panel input');}
 await openGrid();
 assert.equal(await page.$eval('input[aria-label="id for first"]',e=>e.value),'9007199254740993');
 assert.equal(await page.$eval('input[aria-label="payload for first"]',e=>e.value),'00FF');
 await page.locator('input[aria-label="name for first"]').fill('Bob');
 await openConnectionPicker(page);
 const original=await page.$eval('.window-toolbar select[aria-label="Connection"]',e=>e.value);
 await chooseConnection(page,other);
 await page.waitForFunction(()=>document.querySelector('.table-data-panel')?.textContent.includes('another connection'));
 assert.equal(await page.$eval('input[aria-label="name for first"]',e=>e.value),'Bob','draft survives connection changes');
 assert.equal(await page.$eval('.table-data-actions .primary',e=>e.disabled),true);
 await chooseConnection(page,original);
 await page.waitForFunction(()=>!document.querySelector('.table-data-actions .primary').disabled);

 await h.button('Review 1 changes','.table-data-panel ');
 await page.waitForSelector('.confirm-sheet');
 assert.match(await h.text('.confirm-sheet'),/LOCK TABLES.*WRITE/s);
 assert.match(await h.text('.confirm-sheet'),/blocks other sessions/);
 assert.equal(prepared.changes[0].values.name.value,'Bob');
 await h.button('Cancel','.confirm-sheet '); assert.equal(applyCount,0);
 await h.button('Review 1 changes','.table-data-panel '); await h.button('Apply changes','.confirm-sheet ');
 await page.waitForFunction(()=>document.querySelector('.table-data-panel [role=status]')?.textContent.includes('Changes committed'));
 assert.equal(applyCount,1);assert.equal(maxConcurrent,1,'status requests do not overlap');
 await h.button('Reload rows','.table-data-panel ');
 await page.locator('input[aria-label="name for first"]').fill('Another');
 mode='unknown';await h.button('Review 1 changes','.table-data-panel ');await h.button('Apply changes','.confirm-sheet ');
 await page.waitForFunction(()=>document.querySelector('.table-data-panel [role=status]')?.textContent.includes('unknown'));
 await h.button('Close table','.table-data-panel ');await page.waitForSelector('.confirm-sheet');
 assert.match(await h.text('.confirm-sheet'),/may have committed/);assert.doesNotMatch(await h.text('.confirm-sheet'),/not been applied/);
 await h.button('Close and inspect separately','.confirm-sheet ');await page.waitForFunction(()=>!document.querySelector('.table-data-panel'));
 assert.equal(applyCount,2,'unknown outcome never replays writes');
 await openGrid();mode='lost';
 await page.locator('input[aria-label="name for first"]').fill('Lost reply');
 await h.button('Review 1 changes','.table-data-panel ');await h.button('Apply changes','.confirm-sheet ');
 await page.waitForFunction(()=>document.querySelector('.table-data-panel .error')?.textContent.includes('Could not verify'));
 assert.ok(await page.locator('.table-data-panel button ::-p-text(Check status)').wait(),'status retry remains available without an outcome');
 await h.button('Close table','.table-data-panel ');await h.button('Close and inspect separately','.confirm-sheet ');
 await page.waitForFunction(()=>!document.querySelector('.table-data-panel'));
 assert.equal(applyCount,3,'lost request does not resend changes');mode='complete';
 await openGrid();
 const shots=process.env.TABLE_DATA_SHOTS||h.shots;await fs.mkdir(shots,{recursive:true});
 for(const scheme of ['light','dark'])for(const width of [1440,390]){await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:scheme}]);await page.setViewport({width,height:960});await pause(100);await page.screenshot({path:`${shots}/table-data-${scheme}-${width}.png`});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no page overflow');}
 assert.deepEqual(h.errors,[]);
 console.log('table-data: exact typed values, mandatory review, single apply, single-flight polling, unknown escape and four appearances passed');
} finally {await h.close();}
