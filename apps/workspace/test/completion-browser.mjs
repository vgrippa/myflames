// Real browser and Python auth/store; catalog data is synthetic, no DB reads.
// Suggestions are CodeMirror's: the tooltip (.sql-completion carrying
// .sql-completion-option rows with .sql-completion-label) and the
// .sql-completion-hint status panel under the editor; .sql-editor carries
// aria-expanded while the list is open.
// Build normally, or pass MYFLAMES_TEST_ASSETS for an isolated Vite output.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { openConnectionPicker, chooseConnection } from './chrome-helpers.mjs';
import { EDITOR, setEditorText, selectEditor, editorUndo } from './editor-helpers.mjs';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const dir=await fs.mkdtemp(path.join(os.tmpdir(),'myflames-completion-'));
const env={...process.env,HOME:dir,USERPROFILE:dir,MYFLAMES_SECRET_BACKEND:'vault'};
delete env.MYFLAMES_VAULT_PASSPHRASE;
const server=spawn(process.env.PYTHON||'python3',['-c',"import os,sys; from pathlib import Path; import myflames.ui as u; u.ASSET_DIR=Path(os.environ.get('MYFLAMES_TEST_ASSETS',u.ASSET_DIR)); s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",path.join(dir,'workspace.sqlite3')],{cwd:root,env});
const pause=ms=>new Promise(r=>setTimeout(r,ms));
let browser;
const seen=[],held=[]; let hold=false, fail=false, catalogDelay=0, delayedCompleted=0;
const columnAnswer=(body,name='name')=>({schema_version:'catalog-1.0',level:'table',schema:body.schema,table:body.table,columns:[{name,type:'varchar(20)',nullable:true,key:'',default:null,extra:'',generated:false},{name:'na`me',type:'text',nullable:true,key:'',default:null,extra:'',generated:false}],indexes:[]});
try {
  const url=await new Promise((resolve,reject)=>{let output=''; const timer=setTimeout(()=>reject(Error(output)),10000);server.stderr.on('data',b=>{output+=b;const m=output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);if(m){clearTimeout(timer);resolve(m[0]);}});server.on('error',reject);});
  browser=await puppeteer.launch({headless:'shell',args:process.env.MYFLAMES_NO_SANDBOX?['--no-sandbox']:(process.env.MYFLAMES_BROWSER_ARGS||'').split(' ').filter(Boolean)});
  const page=await browser.newPage(); const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.setViewport({width:1440,height:1000});
  await page.setRequestInterception(true);
  page.on('request',req=>{
    const p=new URL(req.url()).pathname, body=JSON.parse(req.postData()||'{}');
    if(!p.startsWith('/api/live/')&&!p.startsWith('/api/editor/')&&p!=='/api/config/versions/names')return void req.continue();
    seen.push({path:p,body});
    let answer;
    if(p==='/api/live/schemas')answer={schema_version:'schemas-1.0',schemas:[{name:'app',tables:2,system:false},{name:'odd db',tables:1,system:false}],user_tables:3,system:[],all_limit:2000,all_note:''};
    else if(p==='/api/live/catalog'){
      if(hold&&body.table)return void held.push({req,body});
      if(fail)return void req.respond({status:403,contentType:'application/json',body:'{"error":"Catalog denied"}'});
      answer=body.table?columnAnswer(body,body.connection.user==='b'?'new_name':'name'):{schema_version:'catalog-1.0',level:'tables',schema:body.schema,tables:['users','odd table'].filter(n=>n.startsWith(body.prefix||'')).map(name=>({name,type:'table'})),truncated:false,limit:200,next_after:null};
    } else if(p==='/api/live/privileges')answer={schema_version:'privileges-1.0',actions:{},account:'test@localhost',engine:'mysql',version:'8.4.3'};
    else if(p==='/api/config/versions/names')answer={schema_version:'param-names-1.0',engine:'mysql',requested:body.version,release:'8.4.3',exact:true,names:[
      {name:'max_connections',scope:'global',type:'integer',dynamic:true,deprecated:false},
      {name:'max_connect_errors',scope:'global',type:'integer',dynamic:true,deprecated:false},
      {name:'max_user_connections',scope:'both',type:'integer',dynamic:true,deprecated:false}]};
    else if(p==='/api/editor/status')answer={status:'none',session:null,run:null};
    else if(p==='/api/editor/history')answer={items:[],has_more:false};
    else if(p==='/api/editor/preview')answer={status:'ready',statements:[],aggregate:{state:'unknown',missing:[],reason:''},policy:{action:'confirm',text:'Review before running.'}};
    else return void req.respond({status:400,contentType:'application/json',body:'{"error":"Synthetic test does not execute SQL"}'});
    const respond=()=>req.respond({status:200,contentType:'application/json',body:JSON.stringify(answer)}).catch(()=>{});
    if(catalogDelay&&(p==='/api/live/schemas'||p==='/api/live/catalog')) {
      setTimeout(()=>{delayedCompleted++;void respond();},catalogDelay);
    } else void respond();
  });
  await page.goto(url,{waitUntil:'networkidle0'});
  await page.locator('.main-nav button ::-p-text(Query editor)').click();
  await page.waitForSelector(EDITOR,{visible:true});
  const mod=await page.evaluate(()=>/Mac/.test(navigator.platform)?'Meta':'Control');
  async function shortcut(key,shift=false,modifier='Control'){await page.keyboard.down(modifier);if(shift)await page.keyboard.down('Shift');await page.keyboard.press(key);if(shift)await page.keyboard.up('Shift');await page.keyboard.up(modifier);}
  async function set(marked){const at=marked.indexOf('|'),sql=marked.replace('|','');
    await setEditorText(page,sql);
    await selectEditor(page,at<0?sql.length:at);
    await shortcut('Space');}

  const value=()=>page.$eval(EDITOR,e=>e.value);
  const list=()=>page.$$eval('.sql-completion-option',els=>els.map(e=>e.textContent));
  async function has(label){try {await page.waitForFunction(label=>[...document.querySelectorAll('.sql-completion-label')].some(e=>e.textContent===label),{timeout:8000},label);} catch(e) {console.error({label,seen:seen.slice(-8),ui:await page.$eval(EDITOR,e=>({value:e.value,selection:e.selectionStart,expanded:e.getAttribute('aria-expanded'),active:e.contains(document.activeElement),tag:document.activeElement?.tagName,width:innerWidth})),list:await list(),note:await page.$$eval('.sql-completion-hint',els=>els.map(e=>e.textContent))});throw e;}
    // CodeMirror refuses acceptance within interactionDelay (75 ms) of the
    // list opening; a person sees the list before accepting.
    await pause(120);}
  const saveProfile=value=>page.evaluate(async value=>{const token=(await(await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Bootstrap':'1','X-Myflames-Token':localStorage.getItem('myflames.session-proof')||''},body:'{}'})).json()).token;return(await(await fetch('/api/profiles/save',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':token},body:JSON.stringify({value})})).json()).id;},value);
  const A=await saveProfile({name:'Completion A',host:'127.0.0.1',port:3306,user:'a',database:'app'});
  const B=await saveProfile({name:'Completion B',host:'127.0.0.1',port:3307,user:'b',database:'app'});
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await openConnectionPicker(page);
  await page.waitForFunction(B=>!!document.querySelector(`.window-toolbar option[value="${B}"]`),{},B);await page.keyboard.press('Escape');await chooseConnection(page,A);
  await setEditorText(page,'');
  // Keywords come from the connected engine and release (the mocked MySQL
  // 8.4.3: OPTIMIZER_COSTS is not in the fallback vocabulary). The loop also
  // waits for the privileges report that supplies the engine and version.
  let engineList=false;
  for(let i=0;i<15&&!engineList;i++){
    await setEditorText(page,'');await selectEditor(page,0);await page.keyboard.type('OPTIM');
    engineList=await page.waitForFunction(()=>[...document.querySelectorAll('.sql-completion-label')].some(e=>e.textContent==='OPTIMIZER_COSTS'),{timeout:2000}).then(()=>true,()=>false);
    if(!engineList)await page.keyboard.press('Escape');
  }
  assert.ok(engineList,'keywords come from the connected engine and release (MySQL 8.4.3)');
  const optimizerOption=await page.$$eval('.sql-completion-option',rows=>{
    const row=rows.find(e=>e.querySelector('.sql-completion-label')?.textContent==='OPTIMIZER_COSTS');
    return row&&{detail:row.querySelector('.cm-completionDetail')?.textContent||'',kind:row.querySelector('.sql-completion-kind')?.textContent||''};
  });
  assert.ok(optimizerOption&&optimizerOption.detail==='reserved'&&optimizerOption.kind==='keyword','an engine keyword shows its kind');
  await page.keyboard.press('Escape');
  await setEditorText(page,'');
  await page.keyboard.type('SEL');await has('SELECT');
  assert.equal(await page.$eval(EDITOR,e=>e.getAttribute('aria-expanded')),'true','the editor exposes the open suggestion list');
  await page.keyboard.press('Escape');
  await page.waitForFunction(()=>document.querySelector('.sql-editor')?.getAttribute('aria-expanded')==='false');
  assert.equal(await page.$('.sql-completion'),null,'Escape closes the list');
  // Timing gate only: the worker adds this check; the coordinator runs it.
  // Measure from the actual input event to a visible option in the browser,
  // avoiding CDP round-trip time and polling latency in the measurement.
  async function observeOptionLatency(label) {
    await page.evaluate(label=>{
      window.__completionLatency={start:null,elapsed:null};
      document.addEventListener('beforeinput',()=>{window.__completionLatency.start=performance.now();},{once:true,capture:true});
      const observer=new MutationObserver(()=>{
        const timing=window.__completionLatency;
        const visible=[...document.querySelectorAll('.sql-completion-label')].some(e=>e.textContent===label&&e.getClientRects().length);
        if(timing.start!==null&&visible){timing.elapsed=performance.now()-timing.start;observer.disconnect();}
      });
      observer.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
    },label);
  }
  async function instant(label) {
    await page.waitForFunction(()=>typeof window.__completionLatency?.elapsed==='number',{timeout:3000,polling:10});
    const elapsed=await page.evaluate(()=>window.__completionLatency.elapsed);
    assert.ok(elapsed<150,`${label} appears in ${elapsed.toFixed(1)} ms, before a 600 ms catalog response`);
  }
  catalogDelay=600;
  await setEditorText(page,'');await selectEditor(page,0);
  await observeOptionLatency('SELECT');await page.keyboard.type('S');await instant('SELECT');
  await page.keyboard.press('Escape');
  // Fill the complete table inventory, then expire its freshness while keeping
  // the real browser clock/timers intact. The second statement must use stale
  // names immediately while delayed revalidation runs in the background.
  await set('SELECT * FROM u|');await has('users');await page.keyboard.press('Escape');
  await page.evaluate(()=>{window.__completionOriginalNow=Date.now;const later=Date.now()+30001;Date.now=()=>later;});
  try {
    await setEditorText(page,'SELECT 1; SELECT * FROM u');await selectEditor(page,'SELECT 1; SELECT * FROM u'.length);
    const completed=delayedCompleted;
    await observeOptionLatency('users');await page.keyboard.type('s');await instant('users');
    assert.equal(delayedCompleted,completed,'cached table names appear before any delayed refresh response');
    await page.waitForFunction(()=>!document.querySelector('.sql-completion-hint')?.textContent.includes('Reading'),{timeout:8000});
  } finally {
    await page.evaluate(()=>{Date.now=window.__completionOriginalNow;delete window.__completionOriginalNow;delete window.__completionLatency;});
    catalogDelay=0;
  }
  await page.keyboard.press('Escape');
  await set('SELECT u.na| FROM users AS u');await has('name');
  assert.ok((await list()).some(s=>s.includes('app.users · varchar')));
  // NAME is a MySQL keyword (non-reserved), so the accepted column is quoted.
  const before=await value();await page.keyboard.press('Enter');assert.equal(await value(),'SELECT u.`name` FROM users AS u');
  await editorUndo(page);assert.equal(await value(),before,'accepting a suggestion is one undo step (Mod-Z)');
  // Ctrl+Shift+Space opens the list too; acceptance replaces the whole token.
  await setEditorText(page,'SELECT u.name FROM users AS u');await selectEditor(page,11);await shortcut('Space',true);
  await has('name');await page.keyboard.press('ArrowDown');await page.keyboard.press('Tab');
  assert.equal(await value(),'SELECT u.`na``me` FROM users AS u','quoted name insertion');
  await set('SELECT * FROM odd|');await has('odd table');await page.keyboard.press('Enter');assert.equal(await value(),'SELECT * FROM `odd table`');
  await page.waitForFunction(()=>document.querySelector('.sql-editor')?.getAttribute('aria-expanded')==='false','accepting closes the list (aria-expanded false)');
  await set('USE odd|');await has('odd db');await page.keyboard.press('Enter');assert.equal(await value(),'USE `odd db`');
  // CTE names are offered as tables, marked CTE.
  await set('WITH recent AS (SELECT 1) SELECT * FROM rec|');await has('recent');
  assert.ok((await list()).some(s=>s.includes('recent')&&s.includes('CTE')),'a CTE name is offered as a table');
  await page.keyboard.press('Enter');assert.equal(await value(),'WITH recent AS (SELECT 1) SELECT * FROM recent');
  // @@ offers the release's system variables from /api/config/versions/names,
  // each with its scope.
  await setEditorText(page,'SELECT @@');await selectEditor(page,9);
  await page.keyboard.type('max_conn');await has('max_connections');
  assert.ok(seen.some(s=>s.path==='/api/config/versions/names'),'@@ reads the release variable catalog');
  assert.ok((await list()).some(s=>s.includes('max_connections')&&s.includes('global')),'a variable shows its scope');
  await page.keyboard.press('Escape');
  await set('SELECT u.| FROM users u');await has('name');const reads=seen.filter(s=>s.path==='/api/live/catalog'&&s.body.table==='users').length;
  await page.keyboard.type('na');await has('name');assert.equal(seen.filter(s=>s.path==='/api/live/catalog'&&s.body.table==='users').length,reads,'prefix edits reuse table details');
  await page.keyboard.press('Escape');assert.equal(await page.$('.sql-completion'),null);
  for(const sql of ["SELECT 'na|me'",'SELECT 1 -- na|','SELECT 1 /* na| */']){await set(sql);await pause(260);assert.equal(await page.$('.sql-completion'),null);}
  // An IME composition closes the list and keeps it closed until it ends; the
  // synthetic CompositionEvent never reaches CodeMirror, so drive a real
  // composition through CDP.
  await set('SELECT u.na| FROM users u');await has('name');
  const cdp=await page.createCDPSession();
  await cdp.send('Input.imeSetComposition',{text:'に',selectionStart:1,selectionEnd:1});
  await page.waitForFunction(()=>!document.querySelector('.sql-completion'),{timeout:3000});
  await shortcut('Space');await pause(300);assert.equal(await page.$('.sql-completion'),null,'a composition keeps suggestions closed');
  await cdp.send('Input.insertText',{text:'に'});
  await page.waitForFunction(()=>document.querySelector('.sql-editor')?.value==='SELECT u.naに FROM users u');
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
