// SQL preparation is the real Python endpoint. Capture execution is mocked:
// prove no original DML reaches it and review requires a second user action.
import assert from 'node:assert/strict';
import { open } from './perf-harness.mjs';
import { chooseConnection } from './chrome-helpers.mjs';

const original = "UPDATE orders SET status = 'paid' WHERE id = 7";
const projected = 'SELECT * FROM orders WHERE id = 7';
const warning = 'The original statement can change data. Review this target row selection; assignment expressions are omitted.';
const row = {digest:'d'.repeat(64),digest_text:'UPDATE orders SET status = ? WHERE id = ?',sample_text:original,
  analysis_sql:projected,analysis_warning:warning,capture_problem:null,sample_truncated:false,schema:'shop',
  count:10,total_ms:100,avg_ms:10,max_ms:15,rows_examined:10,rows_sent:0,examined_per_sent:null,
  no_index_used:0,no_good_index_used:0,tmp_disk_tables:0,sort_merge_passes:0,flags:[],first_seen:'',last_seen:''};
let h;
try {
  h=await open('/api/live/start','analysis-projection',{
    '/api/live/status':{status:200,body:{id:'projection-job',state:'failed',error:'Capture fixture finished.',result:null}},
    '/api/live/top':{status:200,body:{schema_version:'top-1.0',engine:'mysql',server_version:'8.4.3',order:'total',notes:[],statements:[row]}},
  });
  const {page,state}=h;
  state.fallback={status:200,body:{id:'projection-job',state:'queued'}};
  let preparations=0;
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/analysis/prepare-query')preparations++;});
  const profile=await page.evaluate(async()=>{
    const token=(await(await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Bootstrap':'1','X-Myflames-Token':localStorage.getItem('myflames.session-proof')||''},body:'{}'})).json()).token;
    const response=await fetch('/api/profiles/save',{method:'POST',headers:{'Content-Type':'application/json','X-Myflames-Token':token},body:JSON.stringify({value:{name:'Projection fixture',host:'127.0.0.1',port:3306,user:'tester',database:'shop',ssl_mode:'PREFERRED'}})});
    return(await response.json()).id;
  });
  await page.reload({waitUntil:'networkidle0'});
  await h.button('Query editor','.main-nav ');await chooseConnection(page,profile);
  const editor='[aria-label="SQL editor"]', analyze='.editor-actions [data-action="analyze"]';
  const fill=async sql=>{await page.locator(editor).fill(sql);await page.$eval(editor,e=>{e.setSelectionRange(e.value.length,e.value.length);e.dispatchEvent(new Event('select',{bubbles:true}));});};
  await fill(original);
  const tabsBefore=await page.$$('.query-tablist [role="tab"]').then(items=>items.length);
  await page.click(analyze);
  await page.waitForFunction(sql=>document.querySelector('[aria-label="SQL editor"]').value===sql,{},projected);
  assert.equal(await page.$$('.query-tablist [role="tab"]').then(items=>items.length),tabsBefore+1);
  assert.equal(state.seen.length,0,'Converting a write never starts capture');
  assert.ok(preparations>0,'Preparation used the real Python endpoint');
  assert.match(await page.$eval('.editor-panel .notice[role="alert"]',e=>e.textContent),/original statement can change data/i);
  const newTabName=await page.$eval('.query-tablist [aria-selected="true"]',e=>e.textContent);
  assert.match(newTabName,/SELECT/);
  // Preserve the original text in its existing tab, then return to the projection.
  const tabs=await page.$$('.query-tablist [role="tab"]');await tabs[0].click();
  assert.equal(await page.$eval(editor,e=>e.value),original);await tabs.at(-1).click();
  await page.click(analyze);
  await page.waitForFunction(()=>document.querySelector('.editor-panel .error')?.textContent.includes('Capture fixture finished.'));
  assert.equal(state.seen.length,1);assert.equal(state.seen[0].query,projected);assert.equal(state.seen[0].mode,'analyze');
  // No read projection exists for TRUNCATE. Keep it visible, refuse capture.
  await fill('TRUNCATE TABLE orders');await page.click(analyze);
  await page.waitForFunction(()=>document.querySelector('.editor-panel .error')?.textContent.includes('analysis is blocked'));
  assert.equal(state.seen.length,1);assert.equal(await page.$eval(editor,e=>e.value),'TRUNCATE TABLE orders');
  // A Top statements projection enters the editor for review even after its
  // usual analysis confirmation. It still requires the editor's explicit run.
  await h.server();await h.view('top');
  await page.waitForSelector('.server-view[data-view="top"] tbody tr');
  await page.click('.server-view[data-view="top"] tbody tr');
  await page.click('[aria-label="Run analysis on statement 1"]');
  const confirmation=await page.$('.run-confirm');
  if(confirmation)await h.button('Execute and analyze','.run-confirm ');
  await page.waitForFunction(sql=>document.querySelector('[aria-label="SQL editor"]').value===sql&&document.querySelector('.main-nav button.active')?.textContent.includes('Query editor'),{},projected);
  assert.equal(state.seen.length,1,'Top statement projection waits for review');
  assert.match(await page.$eval('.editor-panel .notice[role="alert"]',e=>e.textContent),/target row selection/);
  await page.click(analyze);
  await page.waitForFunction(()=>document.querySelector('.editor-panel .error')?.textContent.includes('Capture fixture finished.'));
  assert.equal(state.seen.length,2);assert.equal(state.seen[1].query,projected);
  assert.ok(state.seen.every(request=>/^SELECT\b/.test(request.query)));
  assert.deepEqual(h.errors,[]);
  console.log('Analysis projection: real preparation, separate review tab, original retained, second-action SELECT only, TRUNCATE refused, Top statement review passed');
} finally { if(h)await h.close(); }
