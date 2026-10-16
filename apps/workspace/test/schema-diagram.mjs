// Synthetic UI models exercise interaction and safe rendering. SQL/Mermaid
// parsing and live metadata correctness are covered by the Python backend.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build, transformWithEsbuild } from "vite";
import { open, pause } from "./perf-harness.mjs";
const workspace=fileURLToPath(new URL("../",import.meta.url)),source=name=>JSON.stringify(path.join(workspace,"src",name));
const entry="\0diagram-fixture.tsx";
const built=await build({root:workspace,configFile:false,publicDir:false,logLevel:"error",plugins:[{name:"diagram-fixture",resolveId:id=>id===entry?id:null,async load(id){
  if(id!==entry)return null;
  const code=`import React,{useState} from ${JSON.stringify(path.join(workspace,"node_modules/react/index.js"))};
    import {createRoot} from ${JSON.stringify(path.join(workspace,"node_modules/react-dom/client.js"))};
    import {SchemaDiagramView} from ${source("SchemaDiagram.tsx")};import * as helpers from ${source("schemaDiagramModel.ts")};
    import {InspectorContext} from ${source("kit.tsx")};import ${source("style.css")};import ${source("kit.css")};import ${source("server.css")};
    window.diagramHelpers=helpers;
    function Fixture(){const [connection,setConnection]=useState({host:"",port:3306,user:"",database:"",ssl_mode:"DISABLED",type:"production"});
      const [document,onDocument]=useState(null),[selected,select]=useState(""),[inspector,setInspector]=useState(false),[width,setWidth]=useState(window.innerWidth);
      React.useEffect(()=>{const f=()=>setWidth(window.innerWidth);window.addEventListener("resize",f);return()=>window.removeEventListener("resize",f);},[]);
      window.diagramDocument=document;
      return <><div className="test-probes"><button onClick={()=>setConnection(c=>({...c,user:"",host:c.host==="127.0.0.1"?"127.0.0.2":"127.0.0.1"}))}>Switch connection</button></div>
        <InspectorContext.Provider value={{open:inspector,setOpen:setInspector,width:340,setWidth:()=>{},sheet:width<1040}}>
          <SchemaDiagramView connection={connection} document={document} onDocument={onDocument} onBack={()=>{}} selection={{key:selected,select:(key,show)=>{select(key);if(show)setInspector(true);}}}/>
        </InspectorContext.Provider></>;}
    createRoot(document.getElementById("root")).render(<Fixture/>);`;
  return(await transformWithEsbuild(code,path.join(workspace,"diagram-fixture.tsx"),{loader:"tsx",jsx:"automatic"})).code;
}}],build:{write:false,target:"es2020",rollupOptions:{input:entry,output:{format:"iife",inlineDynamicImports:true}}}});
const js=built.output.find(f=>f.type==="chunk").code,css=built.output.filter(f=>f.type==="asset"&&f.fileName.endsWith(".css")).map(f=>f.source).join("\n");
const html='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__diagram.css"></head><body><div id="root"></div><script src="/__diagram.js"></script></body></html>';
const content=(contentType,body)=>(_b,request)=>{void request.respond({status:200,contentType,body});return null;};
const column=(name,data_type="bigint",primary_key=false,nullable=false)=>({name,data_type,primary_key,nullable});
const tables=[
  {id:"customers",schema:"shop",name:"customers",columns:[column("id","bigint",true),column("name","varchar(100)"),column("email","varchar(255)"),column("created_at","datetime")]},
  {id:"orders",schema:"shop",name:"orders",columns:[column("id","bigint",true),column("customer_id"),column("ordered_at","datetime"),column("status","varchar(20)")]},
  {id:"order_items",schema:"shop",name:"order_items",columns:[column("order_id","bigint",true),column("product_id","bigint",true),column("quantity","int"),column("price","decimal(10,2)")]},
  {id:"products",schema:"shop",name:"products",columns:[column("id","bigint",true),column("name","varchar(100)"),column("price","decimal(10,2)"),column("category_id")]},
  {id:"categories",schema:"catalog",name:"categories",columns:[],external:true},
  {id:"reviews",schema:"",name:"reviews",columns:[column("id","bigint",true),column("rating","int")]}
];
const edge=(id,from_table,from_columns,to_table,to_columns)=>({id,name:id,from_table,from_columns,to_table,to_columns});
const model={schema_version:"schema-diagram-1.0",tables,relationships:[edge("orders_customer","orders",["customer_id"],"customers",["id"]),edge("items_order","order_items",["order_id"],"orders",["id"]),edge("items_product","order_items",["product_id"],"products",["id"]),edge("product_category","products",["category_id"],"categories",["id"]),{...edge("receives","products",[],"reviews",[]),cardinality:{from:"one",to:"zero-or-many"},identifying:false}],warnings:[]};
let readDelay=0,importDelay=0,importFailure=false,exportWarnings=true,lastImport;
const temp=await fs.mkdtemp(path.join(os.tmpdir(),"myflames-diagram-files-"));let h;
try{
 h=await open("/api/unused-diagram","diagram",{
  "/":content("text/html",html),"/__diagram.js":content("text/javascript",js),"/__diagram.css":content("text/css",css),
  "/api/schema-diagram/read":body=>({status:200,delay:readDelay,body:{...structuredClone(model),warnings:[`Read ${body.database}`]}}),
  "/api/schema-diagram/import":body=>{lastImport=body;return{status:importFailure?400:200,delay:importDelay,body:importFailure?{error:"Unsupported SQL statement. Nothing was executed."}:body.format==="json"?JSON.parse(body.text):structuredClone(model)};},
  "/api/schema-diagram/export":body=>({status:200,body:{text:body.format==="mermaid"?"erDiagram\n  customers ||--o{ orders : places":"CREATE TABLE customers (id bigint PRIMARY KEY);",warnings:exportWarnings?["Conceptual relationships have no SQL foreign keys."]:[]}})
 });
 const {page,state}=h;if(process.env.PERF_SHOTS)await fs.mkdir(h.shots,{recursive:true});
 const click=async text=>{const handle=await page.evaluateHandle(text=>[...document.querySelectorAll("button")].find(b=>b.textContent.trim()===text&&b.getClientRects().length),text);assert.ok(handle.asElement(),`Missing button ${text}`);await handle.asElement().click();await handle.dispose();};
 const settled=()=>page.evaluate(async()=>{await Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})));});
 const shot=async name=>{await settled();await page.screenshot({path:path.join(h.shots,name),fullPage:true});};
 const input=async(selector,value)=>{await page.$eval(selector,(el,value)=>{Object.getOwnPropertyDescriptor(el.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,"value").set.call(el,value);el.dispatchEvent(new Event("input",{bubbles:true}));},value);};
 const requests=path=>state.other.filter(r=>r.path===`/api/schema-diagram/${path}`);
 await page.evaluate(()=>{window.diagramDownloads=[];HTMLAnchorElement.prototype.click=function(){fetch(this.href).then(r=>r.text()).then(text=>window.diagramDownloads.push({name:this.download,text}));};});
 assert.equal(await page.$eval('.diagram-source button',e=>e.disabled),true);
 await shot("diagram-empty-desktop.png");
 await click("Import a schema");await input(".diagram-paste textarea","CREATE TABLE customers (id bigint PRIMARY KEY);");
 await click("Import diagram");await page.waitForSelector(".diagram-table");assert.equal(lastImport.format,"sql");assert.equal(requests("read").length,0);
 assert.equal(await page.$$(".diagram-table").then(x=>x.length),6);
 assert.equal(await page.$$(".diagram-lines .is-conceptual").then(x=>x.length),1);
 assert.match(await page.$eval('[aria-label="Table shop.order_items"]',e=>e.textContent),/PK FK/);
 await shot("diagram-light-desktop.png");
 await click("Import");await page.select('[aria-label="Import format"]',"mermaid");await input(".diagram-paste textarea","erDiagram\n  products ||..o{ reviews : receives");
 await click("Import diagram");await page.waitForFunction(()=>!document.querySelector("dialog[open]"));assert.equal(lastImport.format,"mermaid");
 assert.equal(requests("read").length,0);

 // Focused-table arrows move layout, shift changes step; exports preserve it.
 const header='[data-table-id="customers"]';await page.focus(header);const original=await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers);
 await page.keyboard.press("ArrowRight");await page.keyboard.down("Shift");await page.keyboard.press("ArrowDown");await page.keyboard.up("Shift");
 assert.deepEqual(await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers),{x:original.x+16,y:original.y+64});
 await page.keyboard.press("Enter");await page.waitForSelector(".diagram-position");assert.match(await page.$eval(".view-inspector",e=>e.textContent),/customer_id → id/);
 await click("Reset table position");assert.deepEqual(await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers),original);
 await page.keyboard.press("Escape");await click("Fit");
 // Pointer drag changes coordinates independently of zoom.
 await page.evaluate(async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);});
 const box=await page.$eval(header,e=>{const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2,zoom:new DOMMatrix(getComputedStyle(document.querySelector(".diagram-world")).transform).a};});
 await page.mouse.move(box.x,box.y);await page.mouse.down();await page.mouse.move(box.x+45,box.y+30,{steps:5});await page.mouse.up();
 await page.waitForFunction(({original,zoom})=>{const p=window.diagramDocument.diagram.layout.positions.customers;return Math.abs(p.x-original.x-45/zoom)<.001&&Math.abs(p.y-original.y-30/zoom)<.001;},{},{original,zoom:box.zoom});
 const moved=await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers);assert.ok(moved.x>original.x&&moved.y>original.y);
 await page.keyboard.press("Escape");
 await click("Export");await click("Editable JSON");await page.waitForFunction(()=>window.diagramDownloads.length===1);
 const exported=await page.evaluate(()=>JSON.parse(window.diagramDownloads[0].text));assert.deepEqual(exported.layout.positions.customers,moved);
 await click("Export");await click("SVG image");await page.waitForFunction(()=>window.diagramDownloads.length===2);
 const svg=await page.evaluate(()=>window.diagramDownloads[1].text);assert.match(svg,/xmlns="http:\/\/www.w3.org\/2000\/svg"/);
 assert.equal(await page.evaluate(svg=>new DOMParser().parseFromString(svg,"image/svg+xml").querySelectorAll("parsererror,script,foreignObject").length,svg),0);
 const aspect = await page.evaluate(svg=>{const root=new DOMParser().parseFromString(svg,"image/svg+xml").documentElement,box=root.getAttribute("viewBox").split(" ").map(Number);return {actual:Number(root.getAttribute("width"))/Number(root.getAttribute("height")),expected:box[2]/box[3]};},svg);
 assert.ok(Math.abs(aspect.actual-aspect.expected)<1e-9,"SVG dimensions retain the diagram viewBox aspect ratio");
 // Lossy export is reviewed before any download.
 await click("Export");await click("SQL schema");await page.waitForSelector("dialog[open]");assert.equal(await page.evaluate(()=>window.diagramDownloads.length),2);
 assert.match(await page.$eval("dialog",e=>e.textContent),/Conceptual relationships/);await click("Cancel");assert.equal(await page.evaluate(()=>window.diagramDownloads.length),2);
 await click("Export");await click("Mermaid");await page.waitForSelector("dialog[open]");await click("Download file");await page.waitForFunction(()=>window.diagramDownloads.length===3);
 // JSON file reimport retains layout. File API limits are checked before parsing.
 const jsonFile=path.join(temp,"layout.json");await fs.writeFile(jsonFile,JSON.stringify(exported));
 await click("Import");const chooser=await page.waitForSelector('input[type="file"]');await chooser.uploadFile(jsonFile);await page.waitForFunction(()=>document.querySelector(".diagram-paste textarea").value.includes("schema-diagram-1.0"));
 await click("Import diagram");await page.waitForFunction(()=>!document.querySelector("dialog[open]"));assert.deepEqual(await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers),moved);
 const huge=path.join(temp,"large.sql");await fs.writeFile(huge,"x".repeat(2*1024*1024+1));await click("Import");await page.$('input[type="file"]').then(e=>e.uploadFile(huge));
 await page.waitForFunction(()=>document.querySelector("dialog")?.textContent.includes("2 MiB"));await click("Cancel");
 // Parser failure retains the diagram and stays in the import sheet.
 importFailure=true;await click("Import");await input(".diagram-paste textarea","DROP DATABASE shop");await click("Import diagram");await page.waitForFunction(()=>document.querySelector("dialog")?.textContent.includes("Unsupported SQL"));
 assert.equal(await page.$$(".diagram-table").then(x=>x.length),6);await click("Cancel");importFailure=false;
 // Conceptual relationships have explicit metadata, no inferred FK columns.
 await input('[aria-label="Find a table or column"]',"reviews");await click("Fit");await page.click('[data-table-id="reviews"]');await page.waitForSelector(".diagram-relationship-details");
 assert.match(await page.$eval(".view-inspector",e=>e.textContent),/Conceptual relationship; no column mapping supplied/);assert.match(await page.$eval(".view-inspector",e=>e.textContent),/one → zero-or-many/);
 await page.keyboard.press("Escape");await input('[aria-label="Find a table or column"]',"");await click("Auto layout");
 await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:"dark"}]);await shot("diagram-dark-desktop.png");
 await page.setViewport({width:390,height:844});await pause(250);await input('[aria-label="Find a table or column"]',"customers");await click("Fit");
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth),0);await shot("diagram-dark-phone.png");
 await page.click(header);await page.waitForSelector(".view-inspector");await shot("diagram-inspector-phone.png");await page.keyboard.press("Escape");
 await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:"light"}]);await shot("diagram-light-phone.png");await page.setViewport({width:1440,height:1000});
 // Connection switch invalidates late server responses but preserves imports.
 await click("Switch connection");await input('[aria-label="Database name"]',"shop");readDelay=150;await click("Load database");await click("Switch connection");await pause(200);
 assert.equal(await page.evaluate(()=>window.diagramDocument.source.kind),"import");readDelay=0;
 await click("Load database");await page.waitForFunction(()=>window.diagramDocument.source.kind==="server");
 // An explicit host works with the client's default OS user. Same-source refresh keeps the arrangement.
 await input('[aria-label="Find a table or column"]',"");await page.focus(header);await page.keyboard.press("ArrowRight");
 const livePosition=await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers);
 const readCount=requests("read").length;await page.click('[data-action="refresh"]');
 await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled);
 assert.equal(requests("read").length,readCount+1);assert.deepEqual(await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers),livePosition);
 await click("Switch connection");
 await page.waitForSelector('[data-other-connection="true"]');
 // Escaping and malformed model checks exercise pure helpers with hostile text.
 const checks=await page.evaluate(model=>{const h=window.diagramHelpers,d=structuredClone(model);d.tables[0].name='<script>alert(1)</script>';d.tables[0].columns[0].name='x"><image href="https://invalid/">';
  const svg=h.diagramSvg(d),dom=new DOMParser().parseFromString(svg,"image/svg+xml");let invalid=false;
  try{h.validateDiagram({...d,tables:[d.tables[0],d.tables[0]]});}catch{invalid=true;}
  const badMetadata=[{tables:[{...d.tables[0],indexes:[{name:{bad:true},columns:[],unique:false}]},...d.tables.slice(1)]},
    {tables:[{...d.tables[0],indexes:[{name:"i",columns:[{}],unique:false}]},...d.tables.slice(1)]},
    {tables:[{...d.tables[0],external:{bad:true}},...d.tables.slice(1)]},
    {relationships:[{...d.relationships[0],cardinality:{from:{bad:true},to:"one"}}]},
    {relationships:[{...d.relationships[0],on_delete:{bad:true}}]},
    {relationships:[{...d.relationships[0],identifying:"false"}]}];
  const rejected=badMetadata.every(change=>{try{h.validateDiagram({...d,...change});return false;}catch{return true;}});
  // The shared contract limits total columns, not each table to 1000.
  const wide={...d,tables:[{...d.tables[0],columns:Array.from({length:1001},(_,i)=>({name:`c${i}`,data_type:"int",nullable:true,primary_key:false}))}],relationships:[]};
  h.validateDiagram(wide);
  let tooWide=false;try{h.validateDiagram({...wide,tables:[{...wide.tables[0],columns:Array.from({length:10001},()=>wide.tables[0].columns[0])}]});}catch{tooWide=true;}
  if(!tooWide)throw Error("Total column bound must reject 10001 columns");
  const safe=h.diagramPositions({...d,layout:{positions:{customers:{x:Infinity,y:NaN}}}});
  return{rejected,unsafe:dom.querySelectorAll("script,image,foreignObject,parsererror").length,escaped:svg.includes("&lt;script&gt;"),invalid,finite:Number.isFinite(safe.customers.x)};
 },model);
 assert.deepEqual(checks,{rejected:true,unsafe:0,escaped:true,invalid:true,finite:true});assert.deepEqual(h.errors,[]);
 assert.ok(!state.other.some(r=>/\/(sql|editor|accounts|live)\//.test(r.path)),"imports and exports never invoke SQL execution or server writes");
 console.log("schema diagram: imports, exports, layout roundtrip, keyboard/pointer, fit, conceptual metadata, stale reads, hostile text, limits, dark/light and phone passed");
}finally{if(h)await h.close();await fs.rm(temp,{recursive:true,force:true});}
