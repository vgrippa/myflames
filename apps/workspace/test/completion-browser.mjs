// Real browser and Python auth/store; catalog data is synthetic, no DB reads.
// Build normally, or pass MYFLAMES_TEST_ASSETS for an isolated Vite output.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { openConnectionPicker, chooseConnection } from './chrome-helpers.mjs';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const dir=await fs.mkdtemp(path.join(os.tmpdir(),'myflames-completion-'));
const env={...process.env,HOME:dir,USERPROFILE:dir,MYFLAMES_SECRET_BACKEND:'vault'};
delete env.MYFLAMES_VAULT_PASSPHRASE;
const server=spawn(process.env.PYTHON||'python3',['-c',"import os,sys; from pathlib import Path; import myflames.ui as u; u.ASSET_DIR=Path(os.environ.get('MYFLAMES_TEST_ASSETS',u.ASSET_DIR)); s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",path.join(dir,'workspace.sqlite3')],{cwd:root,env});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
let browser;
const seen=[],held=[]; let hold=false, fail=false;
const columnAnswer=(body,name='name')=>({schema_version:'catalog-1.0',level:'table',schema:body.schema,table:body.table,columns:[{name,type:'varchar(20)',nullable:true,key:'',default:null,extra:'',generated:false},{name:'na`me',type:'text',nullable:true,key:'',default:null,extra:'',generated:false}],indexes:[]});
try {
  const url=await new Promise((resolve,reject)=>{let output=''; const timer=setTimeout(()=>reject(Error(output)),10000);server.stderr.on('data',b=>{output+=b;const m=output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);if(m){clearTimeout(timer);resolve(m[0]);}});server.on('error',reject);});
  browser=await puppeteer.launch({headless:'shell'});
  const page=await browser.newPage(); const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.setViewport({width:1440,height:1000});
  await page.setRequestInterception(true);
  page.on('request',req=>{
    const p=new URL(req.url()).pathname, body=JSON.parse(req.postData()||'{}');
    if(!p.startsWith('/api/live/')&&!p.startsWith('/api/editor/'))return void req.continue();
    seen.push({path:p,body});
    let answer;
    if(p==='/api/live/schemas')answer={schema_version:'schemas-1.0',schemas:[{name:'app',tables:2,system:false},{name:'odd db',tables:1,system:false}],user_tables:3,system:[],all_limit:2000,all_note:''};
    else if(p==='/api/live/catalog'){
      if(hold&&body.table)return void held.push({req,body});
      if(fail)return void req.respond({status:403,contentType:'application/json',body:'{"error":"Catalog denied"}'});
      answer=body.table?columnAnswer(body,body.connection.user==='b'?'new_name':'name'):{schema_version:'catalog-1.0',level:'tables',schema:body.schema,tables:['users','odd table'].filter(n=>n.startsWith(body.prefix||'')).map(name=>({name,type:'table'})),truncated:false,limit:200,next_after:null};
    } else if(p==='/api/live/privileges')answer={schema_version:'privileges-1.0',actions:{},account:'test@localhost'};
    else if(p==='/api/editor/status')answer={status:'none',session:null,run:null};
    else if(p==='/api/editor/history')answer={items:[],has_more:false};
    else if(p==='/api/editor/preview')answer={status:'ready',statements:[],aggregate:{state:'unknown',missing:[],reason:''},policy:{action:'confirm',text:'Review before running.'}};
    else return void req.respond({status:400,contentType:'application/json',body:'{"error":"Synthetic test does not execute SQL"}'});
    void req.respond({status:200,contentType:'application/json',body:JSON.stringify(answer)});
  });
  await page.goto(url,{waitUntil:'networkidle0'});
  await page.locator('.main-nav button ::-p-text(Query editor)').click();
  const ed='[aria-label="SQL editor"]'; await page.waitForSelector(ed,{visible:true});
  const mod=await page.evaluate(()=>/Mac/.test(navigator.platform)?'Meta':'Control');
  async function shortcut(key,shift=false,modifier='Control'){await page.keyboard.down(modifier);if(shift)await page.keyboard.down('Shift');await page.keyboard.press(key);if(shift)await page.keyboard.up('Shift');await page.keyboard.up(modifier);}
  async function set(marked){const at=marked.indexOf('|'),sql=marked.replace('|','');
    await page.$eval(ed,(el,text)=>{el.focus();el.select();document.execCommand('insertText',false,text);},sql);
    await page.waitForFunction(s=>document.querySelector('[aria-label="SQL editor"]').value===s,{timeout:3000},sql);
    await page.$eval(ed,(el,n)=>{el.focus();el.setSelectionRange(n,n);},at<0?sql.length:at);await page.waitForFunction(()=>document.activeElement===document.querySelector('[aria-label="SQL editor"]'));await shortcut('Space');}

  const value=()=>page.$eval(ed,e=>e.value);
  const list=()=>page.$$eval('.sql-completion-option',els=>els.map(e=>e.textContent));
  async function has(label){try {await page.waitForFunction(label=>[...document.querySelectorAll('.sql-completion-label')].some(e=>e.textContent===label),{timeout:8000},label);} catch(e) {console.error({label,seen:seen.slice(-8),ui:await page.$eval(ed,e=>({value:e.value,selection:e.selectionStart,expanded:e.getAttribute('aria-expanded'),active:document.activeElement===e,tag:document.activeElement?.tagName,width:innerWidth})),list:await list(),note:await page.$$eval('.sql-completion-hint',els=>els.map(e=>e.textContent))});throw e;}}
  const saveProfile=value=>page.evaluate(async value=>{const token=(await(await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Bootstrap':'1','X-Myflames-Token':localStorage.getItem('myflames.session-proof')||''},body:'{}'})).json()).token;return(await(await fetch('/api/profiles/save',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':token},body:JSON.stringify({value})})).json()).id;},value);
  const A=await saveProfile({name:'Completion A',host:'127.0.0.1',port:3306,user:'a',database:'app'});
  const B=await saveProfile({name:'Completion B',host:'127.0.0.1',port:3307,user:'b',database:'app'});
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await openConnectionPicker(page);
  await page.waitForFunction(B=>!!document.querySelector(`.window-toolbar option[value="${B}"]`),{},B);await page.keyboard.press('Escape');await chooseConnection(page,A);
  await page.$eval(ed,el=>{el.focus();el.select();document.execCommand('delete');});
  await page.keyboard.type('SEL');await has('SELECT');await page.keyboard.press('Escape');
  await set('SELECT u.na| FROM users AS u');await has('name');
  assert.ok((await list()).some(s=>s.includes('app.users · varchar')));
  const before=await value();await page.keyboard.press('Enter');assert.equal(await value(),'SELECT u.name FROM users AS u');
  await page.evaluate(()=>document.execCommand('undo'));assert.equal(await value(),before,'completion is one native undo edit');
  await page.$eval(ed,el=>el.setSelectionRange(11,11));await shortcut('Space',true);await has('name');await page.keyboard.press('ArrowDown');await page.keyboard.press('Tab');
  assert.equal(await value(),'SELECT u.`na``me` FROM users AS u','quoted name insertion');
  await set('SELECT * FROM odd|');await has('odd table');await page.keyboard.press('Enter');assert.equal(await value(),'SELECT * FROM `odd table`');
  await set('USE odd|');await has('odd db');await page.keyboard.press('Enter');assert.equal(await value(),'USE `odd db`');
  await set('SELECT u.| FROM users u');await has('name');const reads=seen.filter(s=>s.path==='/api/live/catalog'&&s.body.table==='users').length;
  await page.keyboard.type('na');await has('name');assert.equal(seen.filter(s=>s.path==='/api/live/catalog'&&s.body.table==='users').length,reads,'prefix edits reuse table details');
  await page.keyboard.press('Escape');assert.equal(await page.$('.sql-completion'),null);
  for(const sql of ["SELECT 'na|me'",'SELECT 1 -- na|','SELECT 1 /* na| */']){await set(sql);await pause(260);assert.equal(await page.$('.sql-completion'),null);}
  await set('SELECT u.na| FROM users u');await has('name');
  await page.$eval(ed,el=>el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true})));
  assert.equal(await page.$('.sql-completion'),null);await shortcut('Space');await pause(100);assert.equal(await page.$('.sql-completion'),null);
  await page.$eval(ed,el=>el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true})));
  // Delay an uncached A catalog read, then switch. A must never populate B.
  hold=true;await set('SELECT u.na| FROM uncached u');await page.waitForFunction(()=>document.querySelector('.sql-completion-hint')?.textContent.includes('Reading'));
  for(let i=0;i<100&&!held.length;i++)await pause(20);assert.ok(held.length);
  await chooseConnection(page,B);hold=false;
  for(const h of held.splice(0))await h.req.respond({status:200,contentType:'application/json',body:JSON.stringify(columnAnswer(h.body,'stale_name'))}).catch(()=>{});
  await set('SELECT u.ne| FROM users u');await has('new_name');assert.ok(!(await list()).some(s=>s.includes('stale_name')));
  fail=true;await set('SELECT x.na| FROM denied_table x');await page.waitForFunction(()=>document.querySelector('.sql-completion-hint')?.textContent.includes('unavailable'));fail=false;await shortcut('Space');await has('na`me');
  assert.equal(seen.filter(s=>['/api/editor/run','/api/editor/open','/api/live/start'].includes(s.path)).length,0,'suggestions never execute SQL or open editor sessions');
  for(const appearance of ['light','dark'])for(const width of [1440,390]){
    await page.setViewport({width,height:900});await page.emulateMediaFeatures([{name:'prefers-color-scheme',value:appearance}]);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    await set('SELECT u.ne| FROM users u');await has('new_name');
    const box=await page.$eval('.sql-completion',el=>{const r=el.getBoundingClientRect();return{left:r.left,right:r.right,bottom:r.bottom};});
    assert.ok(box.left>=0&&box.right<=width&&box.bottom<=900);
    await page.screenshot({path:path.join(dir,`completion-${appearance}-${width}.png`)});
  }
  await set('SELECT u.ne| FROM users u');await has('new_name');await shortcut('Enter',false,mod);
  for(let i=0;i<100&&!seen.some(s=>s.path==='/api/editor/run');i++)await pause(20);
  assert.ok(seen.some(s=>s.path==='/api/editor/run'),'Run shortcut is never captured by completion');
  assert.deepEqual(errors,[]);console.log(`Completion browser checks passed. Screenshots: ${dir}`);
} finally {await browser?.close();server.kill('SIGTERM');}
