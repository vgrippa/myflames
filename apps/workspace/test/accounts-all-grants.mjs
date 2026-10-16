// Real recorded SHOW PRIVILEGES catalogs, synthetic accounts, real Python plans.
// Every checkbox reaches the actual review UI; Apply is explicitly forbidden.
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import os from "node:os";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { build, transformWithEsbuild } from "vite";
import { open } from "./perf-harness.mjs";
const workspace = fileURLToPath(new URL("../", import.meta.url));
const root = path.resolve(workspace,"../..");
const source = name => JSON.stringify(path.join(workspace,"src",name));
const temporary = await fs.mkdtemp(path.join(os.tmpdir(),"myflames-all-grants-"));
const env = {...process.env, HOME:temporary, USERPROFILE:temporary, MYFLAMES_SECRET_BACKEND:"vault", PYTHONPATH:root};
delete env.MYFLAMES_VAULT_PASSPHRASE;
const python = (args=[],input) => JSON.parse(execFileSync(process.env.PYTHON || "python3",[path.join(root,"test/_all_grant_cases.py"),...args],{cwd:root,env,input,encoding:"utf8",maxBuffer:4*1024*1024}));
const cases = python();
const entry = "\0accounts-fixture.tsx";
const built = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
  plugins: [{ name: "accounts-fixture", resolveId(id) { if (id === entry) return id; }, async load(id) {
    if (id !== entry) return null;
    const fixture = `
      import React, {useState} from ${JSON.stringify(path.join(workspace, "node_modules/react/index.js"))};
      import {createRoot} from ${JSON.stringify(path.join(workspace, "node_modules/react-dom/client.js"))};
      import {AccountsView, compactChanges} from ${source("Accounts.tsx")};
      import {PrivilegesProvider, PrivilegeGate, usePrivileges, privilegeResult} from ${source("privileges.ts")};
      import {InspectorContext} from ${source("kit.tsx")};
      import {WritePolicyContext} from ${source("Connections.tsx")};
      import ${source("style.css")}; import ${source("kit.css")}; import ${source("server.css")};
      window.accountTest = { compactChanges, privilegeResult };
      function Probe() { const p = usePrivileges(); return <div className="test-probes">
        <button onClick={() => p.refresh()}>Refresh privileges</button>
        <button onClick={() => p.accountChanged()}>Account changed</button>
        <button onClick={() => p.refresh(true)}>Fresh privileges</button>
        <PrivilegeGate action="kill" onClick={() => window.killCalls = (window.killCalls || 0) + 1}>Kill probe</PrivilegeGate>
        <PrivilegeGate action="explain" write={false} onClick={() => window.explainCalls = (window.explainCalls || 0) + 1}>Explain probe</PrivilegeGate>
      </div>; }
      function Fixture() {
        const [connection, setConnection] = useState({name:"Test A", host:"127.0.0.1",port:3306,user:"operator",database:"",ssl_mode:"DISABLED",type:"production"});
        const [report,setReport] = useState(null), [selected, select] = useState("");
        const [inspector,setInspector] = useState(true), [revision,setRevision] = useState(0);
        const [width,setWidth] = useState(window.innerWidth);
        React.useEffect(() => { const f = () => setWidth(window.innerWidth); window.addEventListener("resize", f); return () => window.removeEventListener("resize", f); }, []);
        return <WritePolicyContext.Provider value={{blocked:"",check:async()=>connection}}>
          <PrivilegesProvider connection={connection} connectRevision={revision}>
            <div className="test-probes"><button onClick={()=>setConnection(c=>({...c,host:c.host === "127.0.0.1" ? "127.0.0.2" : "127.0.0.1"}))}>Switch connection</button>
              <button onClick={()=>setConnection(c=>({...c,read_only:!c.read_only}))}>Toggle read-only</button>
              <button onClick={()=>setRevision(n=>n+1)}>Reconnect</button></div><Probe/>
            <InspectorContext.Provider value={{open:inspector,setOpen:setInspector,width:440,setWidth:()=>{},sheet:width<1040}}>
              <AccountsView connection={connection} report={report} onReport={setReport} onBack={()=>setInspector(false)}
                selection={{key:selected,select:(key,open)=>{select(key);if(open)setInspector(true);}}}/>
            </InspectorContext.Provider>
          </PrivilegesProvider>
        </WritePolicyContext.Provider>;
      }
      createRoot(document.getElementById("root")).render(<Fixture/>);
    `;
    return (await transformWithEsbuild(fixture, path.join(workspace, "fixture.tsx"), { loader: "tsx", jsx: "automatic" })).code;
  } }], build: { write: false, target: "es2020", minify: false, rollupOptions: { input: entry, output: { format: "iife", inlineDynamicImports: true } } } });
if (process.argv.includes("--compile-only")) { console.log("accounts browser fixture compiled; interactions not run"); process.exit(0); }
const output = built.output;
const js = output.find(f => f.type === "chunk").code;
const css = output.filter(f => f.type === "asset" && f.fileName.endsWith(".css")).map(f => f.source).join("\n");
const html = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__accounts.css"></head><body><div id="root"></div><script src="/__accounts.js"></script></body></html>';
const content = (contentType, body) => (_body, request) => { void request.respond({ status: 200, contentType, body }); return null; };

const actions = Object.fromEntries(["create_user","create_role","lock_account","unlock_account","expire_password","set_own_password","rename_user","drop_user","drop_role","set_password","grant","revoke","grant_role","revoke_role","default_roles"].map(k=>[k,{state:"allowed",missing:[],reason:"Synthetic operator is allowed."}]));
let active=cases[0], present=false, role=false, requests=[], rolesMode=false, applied=0;
const kinds = c => c.engine==="mariadb" ? ["PROCEDURE","FUNCTION","PACKAGE","PACKAGE BODY"] : parseInt(c.version)>=9 ? ["PROCEDURE","FUNCTION","LIBRARY"] : ["PROCEDURE","FUNCTION"];
const scope = (level,kind="PROCEDURE") => ({level,...(level==="global"?{}:level==="proxy"?{proxy:{name:"proxied",host:"%"}}:{database:"grant_db",...(level==="table"||level==="column"?{table:"grant_table"}:{}),...(level==="column"?{columns:["grant_column"]}:{}),...(level==="routine"?{kind,routine:"grant_routine"}:{})})});
const detail = () => ({schema_version:"account-1.0",name:"subject",host:"%",role,engine:active.engine,version:active.version,exists:true,fingerprint:"synthetic-fingerprint",grants:[],attributes:{},roles:rolesMode&&present?[{name:"r",host:active.engine==="mariadb"?"":"%",role:true,admin_option:false}]:[],default_roles:[],defaults_checked:true,warnings:[],
 model:{global:[],dynamic:active.catalog.global.filter(p=>p.dynamic).map(p=>p.name),global_grantable:[],schemas:[],tables:[],columns:[],routines:[],proxies:[],restrictions:[],roles:[],unparsed:[]},
 privilege_catalog:active.catalog,privilege_catalog_meta:active.meta,
 grant_scopes:Object.keys(active.catalog).flatMap(level=>(level==="routine"?kinds(active):[""]).map(kind=>({...scope(level,kind),id:level+kind,label:level+" "+kind,cells:present?active.catalog[level].map(p=>({privilege:p.name,direct:true,grantable:false})):[]})))});
const listing = () => ({schema_version:"accounts-1.0",engine:active.engine,version:active.version,complete:true,warnings:[],accounts:[{name:"subject",host:"%",role,locked:role},...(rolesMode?[{name:"r",host:active.engine==="mariadb"?"":"%",role:true,locked:true}]:[])],role_grants:[],default_roles:[]});
let h;
try {
 h=await open("/api/unused","all-grants",{
  "/":content("text/html",html),"/__accounts.js":content("text/javascript",js),"/__accounts.css":content("text/css",css),
  "/api/accounts/list":()=>({status:200,body:listing()}),
  "/api/accounts/read":()=>({status:200,body:detail()}),
  "/api/live/privileges":()=>({status:200,body:{schema_version:"privileges-1.0",account:"operator@localhost",actions}}),
  "/api/accounts/scope-objects":()=>({status:200,body:{database:"grant_db",tables:[{name:"grant_table"}],procedures:[{name:"grant_routine"}],functions:[{name:"grant_routine"}],warnings:[],truncated:false}}),
  "/api/accounts/plan":body=>{requests.push(body);try{return {status:200,body:python(["--plan"],JSON.stringify({case:active.id,request:body,present}))};}catch(e){return {status:400,body:{error:e.message}};}},
  "/api/accounts/apply":()=>{applied++;return {status:500,body:{error:"Apply is forbidden in this synthetic suite."}};},
  "/api/audit/declined":{status:200,body:{status:"declined",audit_id:"synthetic-decline"}},
 });
 const {page}=h;
 const click=async text=>{const handle=await page.evaluateHandle(text=>[...document.querySelectorAll("button")].find(b=>b.textContent.trim()===text&&b.getClientRects().length),text);assert.ok(handle.asElement(),text);await handle.asElement().click();await handle.dispose();};
 const reset=async()=>{requests=[];await page.reload();await click("Load users and roles");await page.waitForSelector(".accounts-view tbody tr[data-row-key]");await page.click(".accounts-view tbody tr[data-row-key]");await page.waitForSelector(".account-matrix");};
 const review=async()=>{await page.locator("button ::-p-text(Review changes)").click();await page.waitForSelector(".account-review[open]",{timeout:30000});assert.ok(requests.length);};
 let checkboxes=0, reviews=0;
 // Each server, user/role and direction covers the complete captured catalog
 // at every legal scope, including each supported routine object kind.
 for(active of cases) for(role of [false,true]) for(present of [false,true]) {
  await reset();const expected=[];
  for(const level of Object.keys(active.catalog)) {
   await page.select('select[aria-label="Grant level"]',level);
   for(const kind of level==="routine"?kinds(active):[""]) {
    if(level==="routine") await page.select('select[aria-label="Grant scope"]',level+kind);
    const shown=await page.$$eval('.account-matrix .account-privilege-name',els=>els.map(e=>e.textContent));
    assert.deepEqual([...shown].sort(),active.catalog[level].map(p=>p.name).sort(),active.id+level+kind);
    for(const privilege of shown) {
     const hit=await page.$$eval('.account-matrix tbody tr',(rows,name)=>{const row=rows.find(r=>r.querySelector('.account-privilege-name')?.textContent===name);const input=row?.querySelector('td input');if(!input||input.getAttribute('aria-disabled')==='true')return false;input.click();return true;},privilege);
     assert.equal(hit,true,`${active.id} ${level} ${kind} ${privilege} ${present?'revoke':'grant'}`);checkboxes++;
    }
    expected.push({level,kind,names:shown});
   }
  }
  await review();reviews++;
  for(const e of expected) {const request=requests.find(r=>r.level===e.level&&(r.kind||"")===e.kind);assert.ok(request,JSON.stringify(e));assert.equal(request.operation,present?'revoke':'grant');assert.deepEqual([...request.privileges].sort(),[...e.names].sort());}
  assert.equal(requests.length,expected.length);
  const text=await page.$eval('.account-review',e=>e.textContent);
  for(const request of requests) for(const privilege of request.privileges) assert.ok(text.includes(privilege),privilege);
  console.log(active.id,role?'role':'user',present?'revoke':'grant',expected.length,'scopes reviewed');
 }
 // Every dynamic privilege has an independently selectable grant option.
 role=false;present=false;
 for(active of cases.filter(c=>c.engine==="mysql")) {
  await reset();const dynamic=active.catalog.global.filter(p=>p.dynamic).map(p=>p.name);
  for(const name of dynamic) for(const index of [0,1]) await page.$$eval('.account-matrix tbody tr',(rows,{name,index})=>{const row=rows.find(r=>r.querySelector('.account-privilege-name')?.textContent===name);const input=row.querySelectorAll('td input')[index];if(input.getAttribute('aria-disabled')==='true')throw Error(name+' option blocked');input.click();},{name,index});
  await review();reviews++;assert.equal(requests.length,1);assert.equal(requests[0].grant_option,true);assert.deepEqual([...requests[0].privileges].sort(),dynamic.sort());
 }
 // Scope-wide static options and per-account proxy options also reach review.
 for(active of cases) {
  await reset();const expected=[];
  for(const level of ["global","database","table","routine"]) {
   await page.select('select[aria-label="Grant level"]',level);
   for(const kind of level==="routine"?kinds(active):[""]) {
    if(level==="routine")await page.select('select[aria-label="Grant scope"]',level+kind);
    await page.click('.account-inspector > .inspector-block .account-checkbox input');expected.push({level,kind});
   }
  }
  await page.select('select[aria-label="Grant level"]','proxy');await page.click('.account-matrix tbody td input');
  await page.$$eval('.account-matrix tbody td input',inputs=>inputs[1].click());
  await review();reviews++;
  for(const e of expected)assert.deepEqual(requests.find(r=>r.level===e.level&&(r.kind||"")===e.kind)?.privileges,["GRANT OPTION"]);
  assert.equal(requests.find(r=>r.level==="proxy").grant_option,true);
 }
 // Role membership, admin option and default-role review use the same matrix.
 rolesMode=true;
 for(active of cases) for(const mode of ["grant","default","revoke"]) {
  present=mode!=="grant";await reset();await click('Roles');await page.waitForSelector('.account-role-row');
  if(mode==="grant"){await page.click('.account-role-row input');await page.$$eval('.account-role-row input',inputs=>inputs[1].click());}
  else await page.$$eval('.account-role-row input',(inputs,index)=>inputs[index].click(),mode==="default"?2:0);
  await review();reviews++;assert.equal(requests.length,1);assert.equal(requests[0].operation,'roles');assert.equal(requests[0].mode,mode);
  if(mode==="grant")assert.equal(requests[0].admin_option,true);
 }
 rolesMode=false;present=false;
 // Adding scopes must expose every legal routine kind and anonymous proxy.
 for(active of cases) {
  await reset();await page.select('select[aria-label="Grant level"]','routine');await click('Add scope');
  const options=await page.$$eval('select[aria-label="Routine kind"] option',els=>els.map(e=>e.value));assert.deepEqual(options,kinds(active));
  for(const kind of kinds(active)) {
   await page.select('select[aria-label="Routine kind"]',kind);await page.select('select[aria-label="Database"]','grant_db');
   if(['PACKAGE','PACKAGE BODY','LIBRARY'].includes(kind)) {await page.waitForSelector('.account-scope-form input');assert.ok(await page.$eval('.account-scope-form',e=>e.textContent.includes('exact name')));}
  }
  await click('Cancel');await page.select('select[aria-label="Grant level"]','proxy');await click('Add scope');
  const inputs=await page.$$('.account-scope-form input');await inputs[1].type('%');
  const enabled=await page.$$eval('.account-scope-form button',els=>!els.find(e=>e.textContent==='Use scope').disabled);assert.equal(enabled,true,'anonymous proxy accepted');await click('Use scope');
  await page.click('.account-matrix tbody td input');await review();reviews++;assert.equal(requests[0].proxy.name,'');assert.equal(requests[0].proxy.host,'%');
 }
 assert.equal(applied,0);assert.deepEqual(h.errors,[]);
 console.log(JSON.stringify({checkboxes,reviews,servers:cases.length,applied}));
} finally {if(h)await h.close();await fs.rm(temporary,{recursive:true,force:true});}
