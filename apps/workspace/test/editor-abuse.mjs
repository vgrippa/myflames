// Security C8: hostile server-controlled identifiers never become markup in
// the Query editor. A schema, a table, two columns and a column type named as
// HTML/script payloads go through completion (labels and accepted text), hover
// details and the unknown-column lint tooltip. Asserted everywhere: no img,
// svg or script element under .cm-editor or any .cm-tooltip*, window.__xss
// stays undefined, each option label's textContent is the raw name, and an
// accepted name inserts backtick-quoted with embedded backticks doubled.
// Mocked catalog; no database. Run after `npm run build`: node test/editor-abuse.mjs
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { openConnectionPicker, chooseConnection } from './chrome-helpers.mjs';
import { EDITOR, setEditorText, selectEditor } from './editor-helpers.mjs';

const root=fileURLToPath(new URL('../../../',import.meta.url));
const dir=await fs.mkdtemp(path.join(os.tmpdir(),'myflames-editor-abuse-'));
const env={...process.env,HOME:dir,USERPROFILE:dir,MYFLAMES_SECRET_BACKEND:'vault'};
delete env.MYFLAMES_VAULT_PASSPHRASE;
const server=spawn(process.env.PYTHON||'python3',['-c',"import os,sys; from pathlib import Path; import myflames.ui as u; u.ASSET_DIR=Path(os.environ.get('MYFLAMES_TEST_ASSETS',u.ASSET_DIR)); s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",path.join(dir,'workspace.sqlite3')],{cwd:root,env});

// The four hostile names and the hostile type. Plain string concatenation
// everywhere: ${x} would interpolate in a template literal.
const IMG='<img src=x onerror="window.__xss=1">';   // a schema name
const SVG='"><svg onload=window.__xss=1>';          // a table name
const CLOSE='</script>';                            // a column name
const TPL='${x}';                                   // a column name
const ENUMT="enum('<img src=x onerror=window.__xss=1>','b')"; // a column type
const UNKNOWN='<img src=y onerror="window.__xss=1">'; // a qualified name no column has
const quoted=name=>'`'+name.replace(/`/g,'``')+'`';

const seen=[];
const pause=ms=>new Promise(r=>setTimeout(r,ms));
let browser;
try {
  const url=await new Promise((resolve,reject)=>{let output=''; const timer=setTimeout(()=>reject(Error(output)),10000);server.stderr.on('data',b=>{output+=b;const m=output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);if(m){clearTimeout(timer);resolve(m[0]);}});server.on('error',reject);});
  browser=await puppeteer.launch({headless:'shell',args:process.env.MYFLAMES_NO_SANDBOX?['--no-sandbox']:(process.env.MYFLAMES_BROWSER_ARGS||'').split(' ').filter(Boolean)});
  const page=await browser.newPage(); const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.setViewport({width:1440,height:1000});
  await page.setRequestInterception(true);
  page.on('request',req=>{
    const p=new URL(req.url()).pathname, body=JSON.parse(req.postData()||'{}');
    if(!p.startsWith('/api/live/')&&!p.startsWith('/api/editor/'))return void req.continue();
    seen.push({path:p,body});
    let answer;
    if(p==='/api/live/schemas')answer={schema_version:'schemas-1.0',schemas:[{name:IMG,tables:1,system:false}],user_tables:1,system:[],all_limit:2000,all_note:''};
    else if(p==='/api/live/catalog'){
      answer=body.table
        ?{schema_version:'catalog-1.0',level:'table',schema:body.schema,table:body.table,columns:[
            {name:CLOSE,type:ENUMT,nullable:true,key:'',default:null,extra:'',generated:false},
            {name:TPL,type:'text',nullable:true,key:'',default:null,extra:'',generated:false}],indexes:[]}
        :{schema_version:'catalog-1.0',level:'tables',schema:body.schema,tables:[{name:SVG,type:'table'}],truncated:false,limit:200,next_after:null};
    }
    else if(p==='/api/live/privileges')answer={schema_version:'privileges-1.0',actions:{},account:'test@localhost'};
    else if(p==='/api/editor/status')answer={status:'none',session:null,run:null};
    else if(p==='/api/editor/history')answer={items:[],has_more:false};
    else if(p==='/api/editor/preview')answer={status:'ready',statements:[],aggregate:{state:'unknown',missing:[],reason:''},policy:{action:'confirm',text:'Review before running.'}};
    else return void req.respond({status:400,contentType:'application/json',body:'{"error":"Synthetic test does not execute SQL"}'});
    void req.respond({status:200,contentType:'application/json',body:JSON.stringify(answer)});
  });
  await page.goto(url,{waitUntil:'networkidle0'});
  await page.locator('.main-nav button ::-p-text(Query editor)').click();
  await page.waitForSelector(EDITOR,{visible:true});
  const value=()=>page.$eval(EDITOR,e=>e.value);
  async function ctrlSpace(){await page.keyboard.down('Control');await page.keyboard.press('Space');await page.keyboard.up('Control');}
  async function waitLabel(label){try{await page.waitForFunction(l=>[...document.querySelectorAll('.sql-completion-label')].some(e=>e.textContent===l),{timeout:8000},label);}catch(e){console.error({label,list:await page.$$eval('.sql-completion-option',els=>els.map(x=>x.textContent)),hint:await page.$$eval('.sql-completion-hint',els=>els.map(x=>x.textContent))});throw e;}
    // CodeMirror ignores acceptance within interactionDelay (75 ms) of the list opening.
    await pause(120);}
  async function acceptLabel(label){
    const handle=await page.evaluateHandle(l=>[...document.querySelectorAll('.sql-completion-option')].find(e=>e.querySelector('.sql-completion-label')?.textContent===l)||null,label);
    const option=handle.asElement();assert.ok(option,'the option for the raw name is listed');
    await option.click();
  }
  /** The center of the on-screen rendering of *needle* inside the editor. */
  async function pointAt(needle){
    const point=await page.evaluate(needle=>{
      const content=document.querySelector('.cm-content');
      if(!content)return null;
      const walker=document.createTreeWalker(content,NodeFilter.SHOW_TEXT);
      const nodes=[]; let node;
      while((node=walker.nextNode()))nodes.push(node);
      let full=''; for(const n of nodes)full+=n.data;
      const at=full.indexOf(needle);
      if(at<0)return null;
      const mid=at+Math.max(1,Math.floor(needle.length/2));
      let base=0;
      for(const n of nodes){
        if(base+n.data.length>mid){
          const range=document.createRange();
          const off=mid-base;
          range.setStart(n,off);range.setEnd(n,Math.min(n.data.length,off+1));
          const r=range.getBoundingClientRect();
          return r.width||r.height?{x:r.left+r.width/2,y:r.top+r.height/2}:null;
        }
        base+=n.data.length;
      }
      return null;
    },needle);
    assert.ok(point,'the hostile name is rendered as editor text: '+needle.slice(0,40));
    return point;
  }
  /** C8 itself: nothing executable was built from the server strings. */
  async function assertClean(where){
    assert.equal(await page.evaluate(()=>window.__xss),undefined,'no payload ran ('+where+')');
    const host=await page.evaluate(()=>{
      const roots=new Set(document.querySelectorAll('.cm-editor'));
      for(const t of document.querySelectorAll('[class*="cm-tooltip"]'))roots.add(t);
      for(const rootEl of roots){
        const bad=rootEl.querySelector('img, svg, script');
        if(bad)return (rootEl.className||rootEl.tagName)+' contains <'+bad.tagName.toLowerCase()+'>';
      }
      return null;
    });
    assert.equal(host,null,'no img/svg/script element rendered ('+where+')');
  }
  const saveProfile=value=>page.evaluate(async value=>{const token=(await(await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Bootstrap':'1','X-Myflames-Token':localStorage.getItem('myflames.session-proof')||''},body:'{}'})).json()).token;return(await(await fetch('/api/profiles/save',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':token},body:JSON.stringify({value})})).json()).id;},value);
  const A=await saveProfile({name:'Abuse',host:'127.0.0.1',port:3306,user:'a',database:''});
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await openConnectionPicker(page);
  await page.waitForFunction(A=>!!document.querySelector(`.window-toolbar option[value="${A}"]`),{},A);await page.keyboard.press('Escape');await chooseConnection(page,A);

  // 1. A schema name: completion label is the raw name; accepting quotes it.
  await setEditorText(page,'USE ');await selectEditor(page,4);await ctrlSpace();
  await waitLabel(IMG);
  assert.equal(await page.$eval('.sql-completion-option .sql-completion-label',e=>e.textContent),IMG,'the schema label is the raw name');
  await assertClean('schema options');
  await acceptLabel(IMG);
  assert.equal(await value(),'USE '+quoted(IMG),'the schema name inserts backtick-quoted');
  await assertClean('accepted schema');

  // 2. A table name under that schema.
  const tableSql='SELECT * FROM '+quoted(IMG)+'.';
  await setEditorText(page,tableSql);await selectEditor(page,tableSql.length);await ctrlSpace();
  await waitLabel(SVG);
  assert.equal(await page.$eval('.sql-completion-option .sql-completion-label',e=>e.textContent),SVG,'the table label is the raw name');
  await assertClean('table options');
  await acceptLabel(SVG);
  assert.equal(await value(),'SELECT * FROM '+quoted(IMG)+'.'+quoted(SVG),'the table name inserts backtick-quoted');
  await assertClean('accepted table');

  // 3. Column names and a column type in the option details.
  const fromClause=' FROM '+quoted(IMG)+'.'+quoted(SVG)+' t';
  const columnSql='SELECT t.'+fromClause;
  await setEditorText(page,columnSql);await selectEditor(page,'SELECT t.'.length);await ctrlSpace();
  await waitLabel(CLOSE);await waitLabel(TPL);
  const labels=await page.$$eval('.sql-completion-option .sql-completion-label',els=>els.map(e=>e.textContent));
  assert.ok(labels.includes(CLOSE)&&labels.includes(TPL),'column labels are the raw names');
  const options=await page.$$eval('.sql-completion-option',els=>els.map(e=>e.textContent));
  assert.ok(options.some(s=>s.includes(ENUMT)),'the hostile column type stays text in the detail');
  await assertClean('column options');
  await acceptLabel(CLOSE);
  assert.equal(await value(),'SELECT t.'+quoted(CLOSE)+fromClause,'the column name inserts backtick-quoted');
  await assertClean('accepted column');

  // 4. Hover details on the table: heading and column rows are text.
  await page.mouse.move(5,5);
  await page.waitForFunction(()=>!document.querySelector('.cm-tooltip-hover'));
  const tablePoint=await pointAt('svg onload');
  await page.mouse.move(tablePoint.x,tablePoint.y);
  await page.waitForFunction(()=>{const h=document.querySelector('.cm-tooltip-hover .sql-hover');return !!h&&h.textContent.includes('svg onload');},{timeout:6000});
  assert.ok(await page.$eval('.cm-tooltip-hover .sql-hover',(e,t)=>e.textContent.includes(t),ENUMT),'the hover lists the hostile column type as text');
  await assertClean('table hover');

  // 5. Hover details on the column.
  await page.mouse.move(5,5);
  await page.waitForFunction(()=>!document.querySelector('.cm-tooltip-hover'));
  const columnPoint=await pointAt('/script');
  await page.mouse.move(columnPoint.x,columnPoint.y);
  await page.waitForFunction(()=>{const h=document.querySelector('.cm-tooltip-hover .sql-hover');return !!h&&h.textContent.includes('enum(');},{timeout:8000});
  await assertClean('column hover');

  // 6. A qualified name the cached column list does not have: the warning and
  // its lint tooltip carry the raw names as text.
  await page.mouse.move(5,5);
  await page.waitForFunction(()=>!document.querySelector('.cm-tooltip-hover'));
  await setEditorText(page,'SELECT t.'+quoted(UNKNOWN)+fromClause);
  await page.waitForSelector('.cm-lintRange-warning',{timeout:9000});
  const warnPoint=await pointAt('src=y');
  await page.mouse.move(warnPoint.x,warnPoint.y);
  await page.waitForSelector('.cm-tooltip-lint',{visible:true,timeout:9000});
  const message=await page.$eval('.cm-tooltip-lint .cm-diagnosticText',e=>e.textContent);
  assert.equal(message,'No column '+quoted(UNKNOWN)+' in '+quoted(IMG+'.'+SVG)+'.','the lint message keeps the raw names');
  await assertClean('lint tooltip');

  assert.equal(seen.filter(s=>['/api/editor/run','/api/editor/open','/api/live/start'].includes(s.path)).length,0,'suggestions, hover and lint never execute SQL');
  assert.deepEqual(errors,[]);
  console.log('Editor abuse checks passed.');
} finally {await browser?.close();server.kill('SIGTERM');await fs.rm(dir,{recursive:true,force:true}).catch(()=>{});}
