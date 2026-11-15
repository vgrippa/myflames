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
    import {setUiPreference} from ${source("uiPreferences.ts")}; import {wheelZoomStep} from ${source("wheelZoom.ts")};
    window.setDiagramZoomSpeed=value=>setUiPreference("diagram_zoom_speed",value); window.wheelZoomStep=wheelZoomStep;
    import {InspectorContext} from ${source("kit.tsx")};import ${source("style.css")};import ${source("kit.css")};import ${source("server.css")};
    window.diagramHelpers=helpers;
    function Fixture(){const [connection,setConnection]=useState({host:"",port:3306,user:"",database:"",ssl_mode:"DISABLED",type:"production"});
      const [document,onDocument]=useState(null),[selected,select]=useState(""),[inspector,setInspector]=useState(false),[width,setWidth]=useState(window.innerWidth);
      React.useEffect(()=>{const f=()=>setWidth(window.innerWidth);window.addEventListener("resize",f);return()=>window.removeEventListener("resize",f);},[]);
      window.diagramDocument=document;window.setDiagramDocument=onDocument;
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
let readDelay=0,readFailure=false,importDelay=0,importFailure=false,exportWarnings=true,lastImport;
let mermaidText=null,exportDelay=0;
let liveModel=structuredClone(model), catalogFailure=false;
// The longest database name MySQL allows (64 characters): the Load button wraps it at 720 px.
const longName="warehouse_inventory_reconciliation_archive_2026_q4_eu_west_shard7";
const temp=await fs.mkdtemp(path.join(os.tmpdir(),"myflames-diagram-files-"));let h;
try{
 h=await open("/api/unused-diagram","diagram",{
  "/":content("text/html",html),"/__diagram.js":content("text/javascript",js),"/__diagram.css":content("text/css",css),
  "/api/live/schemas":()=>({status:catalogFailure?400:200,body:catalogFailure?{error:"Catalog permission denied"}:{schemas:[{name:"shop",tables:6,system:false},{name:"inventory",tables:2,system:false},{name:longName,tables:1,system:false}]}}),
  "/api/schema-diagram/read":body=>({status:readFailure?400:200,delay:readDelay,body:readFailure?{error:"Schema read failed"}:{...structuredClone(liveModel),warnings:[`Read ${body.database}`]}}),
  "/api/schema-diagram/import":body=>{lastImport=body;return{status:importFailure?400:200,delay:importDelay,body:importFailure?{error:"Unsupported SQL statement. Nothing was executed."}:body.format==="json"?JSON.parse(body.text):structuredClone(model)};},
  "/api/schema-diagram/export":body=>({status:200,delay:exportDelay,body:{text:body.format==="mermaid"?mermaidText??`erDiagram\n${body.diagram.tables.map(t=>`  ${t.name} {\n${t.columns.map(c=>`    ${c.data_type.replaceAll(",","-").replaceAll(" ","_")} ${c.name}`).join("\n")}\n  }`).join("\n")}`:"CREATE TABLE customers (id bigint PRIMARY KEY);",warnings:exportWarnings?["Conceptual relationships have no SQL foreign keys."]:[]}})
 });
 const {page,state}=h;if(process.env.PERF_SHOTS)await fs.mkdir(h.shots,{recursive:true});
 const click=async text=>{const handle=await page.evaluateHandle(text=>[...document.querySelectorAll("button")].find(b=>b.textContent.trim()===text&&b.getClientRects().length),text);assert.ok(handle.asElement(),`Missing button ${text}`);await handle.asElement().click();await handle.dispose();};
 const settled=()=>page.evaluate(async()=>{await Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})));});
 const shot=async name=>{await settled();await page.screenshot({path:path.join(h.shots,name),fullPage:true});};
 const input=async(selector,value)=>{await page.$eval(selector,(el,value)=>{const proto=el.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:el.tagName==="SELECT"?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,"value").set.call(el,value);el.dispatchEvent(new Event(el.tagName==="SELECT"?"change":"input",{bubbles:true}));},value);};
 const requests=path=>state.other.filter(r=>r.path===`/api/schema-diagram/${path}`);
 await page.evaluate(()=>{window.diagramDownloads=[];window.diagramClipboard="";Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:async text=>{window.diagramClipboard=text;}}});HTMLAnchorElement.prototype.click=function(){fetch(this.href).then(r=>r.text()).then(text=>window.diagramDownloads.push({name:this.download,text}));};});
 const refreshLabel=()=>page.$eval('[data-action="refresh"]',e=>e.textContent.trim());
 const visibleButtons=()=>page.$$eval("button",els=>els.filter(b=>b.getClientRects().length).map(b=>b.textContent.trim()));
 // Inside the open confirmation sheet (a modal: the page behind it is inert).
 const confirm=async()=>{await page.waitForSelector("dialog[open]");await page.click("dialog[open] .sheet-actions .button.primary, dialog[open] .dialog-actions .button.primary");};
 assert.equal(await page.$eval('[data-action="refresh"]',e=>e.disabled),true);
 assert.equal(await page.$('.diagram-source .primary'),null,"single Refresh in toolbar");
 // Nothing to load without a connection: the toolbar's Refresh reads Load,
 // Import a schema is the one action, and Mermaid waits for a diagram.
 assert.equal(await refreshLabel(),"Load");
 assert.equal(await page.$eval('[data-action="refresh"]',e=>e.getAttribute("aria-label")),"Load");
 assert.equal(await page.$eval(".view-empty .button.primary",e=>e.textContent.trim()),"Import a schema");
 assert.ok(!(await visibleButtons()).includes("View Mermaid"),"no View Mermaid without a diagram");
 assert.equal(await page.$('[aria-label="Diagram view"]'),null,"Diagram | Mermaid waits for a diagram");
 await shot("diagram-empty-desktop.png");
 await click("Import a schema");await page.select('[aria-label="Import format"]',"sql");await input(".diagram-paste textarea","CREATE TABLE customers (id bigint PRIMARY KEY);");
 await click("Import diagram");await page.waitForSelector(".diagram-table");assert.equal(lastImport.format,"sql");assert.equal(requests("read").length,0);
 assert.equal(await page.$$(".diagram-table").then(x=>x.length),6);
 assert.equal(await page.$$(".diagram-lines .is-conceptual").then(x=>x.length),1);
 assert.equal(await page.$$(".diagram-lines .diagram-fk-marker").then(x=>x.length),5);
 assert.match(await page.$eval(".diagram-caption",e=>e.textContent),/Dashed lines: conceptual relationships/);
 assert.match(await page.$eval('[aria-label="Table shop.order_items"]',e=>e.textContent),/PK FK/);
 await shot("diagram-light-desktop.png");
 await click("Import");await page.select('[aria-label="Import format"]',"mermaid");await input(".diagram-paste textarea","erDiagram\n  products ||..o{ reviews : receives");
 await click("Import diagram");await page.waitForFunction(()=>!document.querySelector("dialog[open]"));assert.equal(lastImport.format,"mermaid");
 assert.equal(requests("read").length,0);

 // Native wheel events use distance, not a fixed multiplier. Several events
 // in one frame share one update and use the last pointer as the anchor.
 const close=(actual,expected,message,tolerance=2e-5)=>assert.ok(Math.abs(actual-expected)<tolerance,`${message}: ${actual} vs ${expected}`);
 const setSpeed=speed=>page.evaluate(async speed=>{
   await window.setDiagramZoomSpeed(speed);
   await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);
 },speed);
 const wheel=async (events,mermaid=false)=>page.evaluate(async({events,mermaid})=>{
   const canvas=document.querySelector(mermaid?".diagram-mermaid-preview":".diagram-canvas");
   const world=canvas.querySelector(mermaid?".diagram-mermaid-world":".diagram-world");
   const read=()=>{const m=new DOMMatrix(world.style.transform);return{zoom:m.a,x:m.e,y:m.f};};
   const before=read(),rect=canvas.getBoundingClientRect(),canceled=[];
   const raf=window.requestAnimationFrame; let frames=0;
   window.requestAnimationFrame=callback=>{frames++;return raf.call(window,callback);};
   let point;
   try {
     for (const data of events) {
       point={x:data.x??173,y:data.y??119};
       const event=new WheelEvent("wheel",{bubbles:true,cancelable:true,deltaY:0,...data,
         clientX:rect.left+canvas.clientLeft+point.x,clientY:rect.top+canvas.clientTop+point.y});
       canvas.dispatchEvent(event); canceled.push(event.defaultPrevented);
     }
   } finally {window.requestAnimationFrame=raf;}
   await new Promise(raf);await new Promise(raf);
   return{before,after:read(),point,canceled,frames,height:canvas.clientHeight};
 },{events,mermaid});
 const anchor=result=>{
   const {before,after,point}=result;
   close((point.x-before.x)/before.zoom,(point.x-after.x)/after.zoom,"pointer x is anchored",.003);
   close((point.y-before.y)/before.zoom,(point.y-after.y)/after.zoom,"pointer y is anchored",.003);
 };
 await click("Fit");
 let stepped=await wheel([{deltaY:40}]);
 close(stepped.after.zoom/stepped.before.zoom,Math.exp(-.04),"pixel wheel");anchor(stepped);
 assert.deepEqual(stepped.canceled,[true]);assert.equal(stepped.frames,1);
 stepped=await wheel([{deltaY:-2,deltaMode:1}]);
 close(stepped.after.zoom/stepped.before.zoom,Math.exp(.032),"16 pixels per line");anchor(stepped);
 for (const deltaY of [-10000,10000]) {
   stepped=await wheel([{deltaY}]);
   close(stepped.after.zoom/stepped.before.zoom,Math.exp(-Math.sign(deltaY)*.1),"100 pixel cap");anchor(stepped);
 }
 stepped=await wheel([{deltaY:.05,deltaMode:2}]);
 close(stepped.after.zoom/stepped.before.zoom,Math.exp(-Math.min(100,.05*stepped.height)*.001),"page uses visible height");
 for (const data of [{deltaX:80},{deltaX:80,deltaY:20},{deltaY:0},{deltaY:.001}]) {
   stepped=await wheel([data]);assert.deepEqual(stepped.after,stepped.before);assert.deepEqual(stepped.canceled,[false]);assert.equal(stepped.frames,0);
 }
 // Subscribed preferences change the existing listener's speed immediately.
 const logs={};
 for (const speed of [50,100,200]) {
   await setSpeed(speed);
   stepped=await wheel([{deltaY:30}]);
   logs[speed]=Math.log(stepped.after.zoom/stepped.before.zoom);
   close(logs[speed],-.03*speed/100,`stored speed ${speed}`);anchor(stepped);
 }
 close(logs[50],logs[100]/2,"Slow halves the logarithmic step");
 close(logs[200],logs[100]*2,"Fastest doubles the logarithmic step");
 await setSpeed(100);
 stepped=await wheel([{deltaY:-10,x:50,y:70},{deltaY:-15,x:240,y:160,ctrlKey:true}]);
 close(stepped.after.zoom/stepped.before.zoom,Math.exp(.025),"batched deltas including pinch");
 assert.equal(stepped.frames,1,"one update for two events");anchor(stepped);
 stepped=await wheel(Array.from({length:60},()=>({deltaY:-100})));
 close(stepped.after.zoom,2,"maximum is 200%");anchor(stepped);
 stepped=await wheel([{deltaY:-100}]);assert.deepEqual(stepped.after,stepped.before,"at the upper clamp the viewport stays put");
 stepped=await wheel(Array.from({length:60},()=>({deltaY:100})));
 close(stepped.after.zoom,.15,"minimum is 15%");anchor(stepped);
 stepped=await wheel([{deltaY:100}]);assert.deepEqual(stepped.after,stepped.before,"at the lower clamp the viewport stays put");
 // Non-finite deltas cannot poison the viewport (WheelEvent itself normalizes them).
 assert.equal(await page.evaluate(()=>window.wheelZoomStep({deltaX:0,deltaY:NaN,deltaMode:0},100,600)),null);
 await click("Fit");

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
 await click("Export");await page.click(".diagram-export-menu button:last-child");await page.waitForSelector("dialog[open]");await click("Download file");await page.waitForFunction(()=>window.diagramDownloads.length===3);
 // Mermaid shares the current model without changing the editable layout.
 await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:"light"}]);
 const beforeMermaid=await page.evaluate(()=>structuredClone(window.diagramDocument.diagram.layout.positions));
 await click("Mermaid");await page.waitForSelector(".diagram-mermaid-preview svg");
 assert.equal(await page.$eval('.diagram-mermaid-panel[aria-label="Mermaid preview"]',e=>e.dataset.theme),"default");
 const code=await page.$eval(".diagram-mermaid-code",e=>e.textContent);
 assert.match(code,/erDiagram/);assert.match(code,/customers/);
 assert.equal(await page.$eval(".diagram-mermaid details",e=>e.open),false);
 await click("Copy");await page.waitForFunction(code=>window.diagramClipboard===code,{},code);
 const beforeDownload=await page.evaluate(()=>window.diagramDownloads.length);
 await click("Download .mmd");await page.waitForFunction(n=>window.diagramDownloads.length===n+1,{},beforeDownload);
 assert.deepEqual(await page.evaluate(()=>window.diagramDownloads.at(-1)),{name:"database-diagram.mmd",text:code});
 // Zoom and Fit remain inside the preview frame.
 const zoom=await page.$eval('[aria-label="Mermaid zoom"]',e=>parseInt(e.textContent));
 await page.click('.diagram-mermaid [aria-label="Zoom in"]');
 assert.ok(await page.$eval('[aria-label="Mermaid zoom"]',e=>parseInt(e.textContent))>zoom);
 await click("Fit");
 // A trackpad gesture of forty half-pixel events is just 20 pixels total.
 // The previous fixed 1.15 multiplier would shrink this to near the minimum.
 const trackpad=await wheel(Array.from({length:40},()=>({deltaY:.5,ctrlKey:true})),true);
 close(trackpad.after.zoom/trackpad.before.zoom,Math.exp(-.02),"Mermaid trackpad stays smooth");
 assert.equal(trackpad.frames,1);assert.ok(trackpad.canceled.every(Boolean));anchor(trackpad);
 const horizontalPreview=await wheel([{deltaX:50}],true);
 assert.deepEqual(horizontalPreview.after,horizontalPreview.before);assert.deepEqual(horizontalPreview.canceled,[false]);
 await setSpeed(200);
 const fastPreview=await wheel([{deltaY:20}],true);
 close(fastPreview.after.zoom/fastPreview.before.zoom,Math.exp(-.04),"Mermaid speed updates live");anchor(fastPreview);
 await setSpeed(100);
 await click("Fit");
 for(const theme of ["dark","light"]){
   const oldSvg=await page.$eval(".diagram-mermaid-preview svg",e=>e.id);
   await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:theme}]);
   await page.waitForFunction(({theme,oldSvg})=>{const panel=document.querySelector('.diagram-mermaid-panel[aria-label="Mermaid preview"]'),svg=panel?.querySelector("svg");return panel?.dataset.theme===(theme==="dark"?"dark":"default")&&svg&&svg.id!==oldSvg;},{},{theme,oldSvg});
   await shot(`mermaid-${theme}-1440.png`);
   await page.setViewport({width:390,height:844});await pause(100);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth),0);
   await shot(`mermaid-${theme}-390.png`);await page.setViewport({width:1440,height:1000});
 }
 // Superseded exports cannot replace a newer diagram's code or preview.
 const exportsBefore=requests("export").length;exportDelay=500;
 await page.evaluate(()=>window.setDiagramDocument(old=>({...old,diagram:{...old.diagram,tables:old.diagram.tables.map((t,i)=>i===0?{...t,columns:[...t.columns,{name:"late_column",data_type:"int",nullable:true,primary_key:false}]}:t)}})));
 for(let i=0;i<100&&requests("export").length===exportsBefore;i++)await pause(20);
 assert.equal(requests("export").length,exportsBefore+1);exportDelay=0;
 await page.evaluate(()=>window.setDiagramDocument(old=>({...old,diagram:{...old.diagram,tables:old.diagram.tables.map((t,i)=>i===0?{...t,columns:t.columns.map(c=>c.name==="late_column"?{...c,name:"latest_column"}:c)}:t)}})));
 await page.waitForFunction(()=>document.querySelector(".diagram-mermaid-code")?.textContent.includes("latest_column"));
 await page.waitForSelector(".diagram-mermaid-preview svg");await pause(550);
 assert.doesNotMatch(await page.$eval(".diagram-mermaid-code",e=>e.textContent),/late_column/);
 // Failed rendering leaves readable code and no Mermaid error element in body.
 mermaidText="erDiagram\n  Broken {\n    invalid syntax here !\n  }";
 await page.evaluate(()=>window.setDiagramDocument(old=>({...old,diagram:{...old.diagram}})));
 await page.waitForSelector(".diagram-mermaid-error");
 assert.equal(await page.$eval(".diagram-mermaid-code",e=>e.textContent),mermaidText);
 assert.equal(await page.$$('body > [id^="ddatabase-mermaid-"]').then(x=>x.length),0);
 mermaidText=null;
 await click("Diagram");await page.waitForSelector(".diagram-table");
 assert.deepEqual(await page.evaluate(()=>window.diagramDocument.diagram.layout.positions),beforeMermaid);
 const remounted=await wheel([{deltaY:20}]);
 close(remounted.after.zoom/remounted.before.zoom,Math.exp(-.02),"wheel reattaches after Mermaid");
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
 // Catalog selection reads the active connection, and replacement is explicit.
 await click("Switch connection");await page.waitForFunction(()=>!!document.querySelector('select[aria-label="Database"] option[value="shop"]'));
 await input('[aria-label="Database"]',"shop");
 await page.click('[data-action="refresh"]');await page.waitForSelector("dialog[open]");
 assert.match(await page.$eval("dialog",e=>e.textContent),/No database changes will be made/);
 const beforeCancel=requests("read").length;await click("Cancel");assert.equal(requests("read").length,beforeCancel);
 // Connection switch invalidates late server responses but preserves imports.
 // Another database chosen while a diagram is shown offers to load it, and
 // choosing reads nothing by itself.
 assert.match(await page.$eval(".diagram-choice",e=>e.textContent),/shop is chosen\. The diagram shows /);
 assert.equal(await refreshLabel(),"Load");
 assert.equal(requests("read").length,beforeCancel,"choosing a database never reads it");
 readDelay=150;await page.click('[data-action="refresh"]');
 assert.equal(await page.$eval("dialog[open] .button.primary",e=>e.textContent.trim()),"Load shop");
 await confirm();await click("Switch connection");await pause(200);
 assert.equal(await page.evaluate(()=>window.diagramDocument.source.kind),"import");readDelay=0;
 await input('[aria-label="Database"]',"shop");await page.click('[data-action="refresh"]');await confirm();
 await page.waitForFunction(()=>window.diagramDocument.source.kind==="server");
 // The diagram of the chosen database is on screen: Refresh reads it again.
 await page.waitForFunction(()=>document.querySelector('[data-action="refresh"]')?.textContent.trim()==="Refresh");
 assert.equal(await page.$(".diagram-choice"),null);
 assert.ok(await page.$('time[datetime]'));assert.equal(requests("read").at(-1).body.connection.host,"127.0.0.2");
 assert.match(await page.$eval(".summary-line",e=>e.textContent),/Connection: 127\.0\.0\.2:3306/);
 // An explicit host works with the client's default OS user. Same-source rebuild keeps arrangement.
 await input('[aria-label="Find a table or column"]',"");await page.focus(header);await page.keyboard.press("ArrowRight");
 const livePosition=await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers);
 liveModel.tables.push({id:"shipments",schema:"shop",name:"shipments",columns:[column("id","bigint",true),column("order_id")]});
 liveModel.tables[0].columns.push(column("phone","varchar(40)"));
 liveModel.relationships.push(edge("shipment_order","shipments",["order_id"],"orders",["id"]));
 const readCount=requests("read").length;await page.click('[data-action="refresh"]');
 await page.waitForFunction(()=>window.diagramDocument.diagram.tables.some(t=>t.name==="shipments"));
 assert.equal(requests("read").length,readCount+1);assert.deepEqual(await page.evaluate(()=>window.diagramDocument.diagram.layout.positions.customers),livePosition);
 assert.match(await page.$eval('[aria-label="Table shop.customers"]',e=>e.textContent),/phone/);
 assert.equal(await page.evaluate(()=>{const d=window.diagramDocument.diagram,p=d.layout.positions,s=p.shipments;return d.tables.filter(t=>t.id!=="shipments").every(t=>s.y>=p[t.id].y+window.diagramHelpers.tableHeight(t));}),true,"New tables do not cover existing table positions");
 assert.equal(await page.evaluate(()=>window.diagramDocument.diagram.relationships.some(r=>r.id==="shipment_order")),true);
 // When no conceptual relationships exist, the dashed-line legend is omitted.
 liveModel.relationships=liveModel.relationships.filter(r=>r.id!=="receives");
 await page.click('[data-action="refresh"]');
 await page.waitForFunction(()=>!window.diagramDocument.diagram.relationships.some(r=>r.id==="receives"));
 assert.doesNotMatch(await page.$eval(".diagram-caption",e=>e.textContent),/Dashed lines: conceptual relationships/);
 liveModel.relationships.push({...edge("receives","products",[],"reviews",[]),cardinality:{from:"one",to:"zero-or-many"},identifying:false});
 // Mermaid export receives the rebuilt model, including new entities and columns.
 exportWarnings=false;const downloads=await page.evaluate(()=>window.diagramDownloads.length);
 await click("Export");await page.click(".diagram-export-menu button:last-child");await page.waitForFunction(n=>window.diagramDownloads.length===n+1,{},downloads);
 assert.match(await page.evaluate(()=>window.diagramDownloads.at(-1).text),/shipments/);
 assert.match(await page.evaluate(()=>window.diagramDownloads.at(-1).text),/phone/);
 // Changing the selected database asks before replacing the displayed diagram.
 await input('[aria-label="Database"]',"inventory");await page.click('[data-action="refresh"]');
 await page.waitForSelector("dialog[open]");await click("Cancel");
 assert.equal(requests("read").at(-1).body.database,"shop");
 // Failed rebuilds preserve the last good model and clearly expose the failure.
 await input('[aria-label="Database"]',"shop");readFailure=true;await page.click('[data-action="refresh"]');
 await page.waitForFunction(()=>document.body.textContent.includes("Schema read failed"));
 assert.equal(await page.evaluate(()=>window.diagramDocument.diagram.tables.length),7);readFailure=false;
 // A schema selection change invalidates the outstanding read and its errors.
 readDelay=180;liveModel.tables.push({id:"stale",schema:"shop",name:"stale",columns:[]});
 await page.click('[data-action="refresh"]');await input('[aria-label="Database"]',"inventory");await pause(220);
 assert.equal(await page.evaluate(()=>window.diagramDocument.diagram.tables.some(t=>t.id==="stale")),false);
 readDelay=0;liveModel.tables.pop();await input('[aria-label="Database"]',"shop");
 await page.click('[data-action="refresh"]');await page.waitForFunction(()=>{const b=document.querySelector('[data-action="refresh"]');return b&&!b.disabled&&b.getAttribute("aria-busy")!=="true";});
 for(const theme of ["light","dark"]){
   await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:theme}]);await shot(`diagram-rebuilt-${theme}-1440.png`);
   await page.setViewport({width:390,height:844});await input('[aria-label="Find a table or column"]',"customers");await click("Fit");
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth),0);
   await shot(`diagram-rebuilt-${theme}-390.png`);await page.setViewport({width:1440,height:1000});await input('[aria-label="Find a table or column"]',"");await click("Fit");
 }
 // A catalog failure still permits entering a known database name.
 catalogFailure=true;await click("Reload database list");await page.waitForFunction(()=>document.body.textContent.includes("Catalog permission denied"));
 assert.equal(await page.$eval('[aria-label="Database"]',e=>e.tagName),"INPUT","a failed catalog offers a typed database name");
 const fallbackReads=requests("read").length;
 await input('[aria-label="Database"]',"shop");await page.click('[data-action="refresh"]');await page.waitForFunction(()=>{const b=document.querySelector('[data-action="refresh"]');return b&&!b.disabled&&b.getAttribute("aria-busy")!=="true";});
 assert.equal(requests("read").length,fallbackReads+1,"Refresh still reads the typed database after a catalog failure");
 assert.equal(requests("read").at(-1).body.database,"shop");
 assert.equal(await page.evaluate(()=>window.diagramDocument.diagram.tables.length),7);
 await click("Switch connection");await page.waitForSelector('[data-other-connection="true"]');
 // A diagram read from another connection names it and offers to load the
 // chosen database from the connection in use.
 assert.match(await page.$eval(".other-connection",e=>e.textContent),/This diagram was read from .*127\.0\.0\.\d:3306/);
 assert.equal(await refreshLabel(),"Load");

 // Loading the chosen database is one click from the empty state.
 catalogFailure=false;await page.evaluate(()=>window.setDiagramDocument(null));
 // The switch reads the database list again: wait for it before reloading it.
 await page.waitForFunction(()=>{const b=document.querySelector(".diagram-reload-list");return b&&b.getAttribute("aria-busy")!=="true";});
 await click("Reload database list");await page.waitForFunction(()=>!!document.querySelector('select[aria-label="Database"] option[value="shop"]'));
 await input('[aria-label="Database"]',"");
 assert.equal(await page.$eval(".view-empty h3",e=>e.textContent),"Choose a database");
 assert.ok(!(await visibleButtons()).some(t=>t.startsWith("Load ")),"no Load before a database is chosen");
 const emptyReads=requests("read").length;
 await input('[aria-label="Database"]',"shop");
 assert.equal(requests("read").length,emptyReads,"choosing a database never reads it");
 assert.equal(await page.$eval(".view-empty h3",e=>e.textContent),"See how the tables of shop connect");
 assert.equal(await page.$eval(".view-empty .button.primary",e=>e.textContent.trim()),"Load shop","Load is the primary action");
 assert.equal(await page.$eval(".view-empty .diagram-empty-actions .button:not(.primary)",e=>e.textContent.trim()),"Import a schema","Import is secondary");
 assert.equal(await refreshLabel(),"Load");
 for(const theme of ["light","dark"]){await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:theme}]);await shot(`diagram-load-${theme}-1440.png`);
   await page.setViewport({width:720,height:900});await pause(150);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth),0);
   await shot(`diagram-load-${theme}-720.png`);await page.setViewport({width:1440,height:1000});}
 await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:"light"}]);
 // A failed load shows its error once, under the Load button, with Try again.
 readFailure=true;await page.click(".diagram-load");
 await page.waitForFunction(()=>document.querySelector(".diagram-load-status")?.textContent.includes("Schema read failed"));
 assert.equal(await page.$('.view-status [role="alert"]'),null,"the error is not repeated under the toolbar");
 assert.ok((await page.$$eval(".diagram-load-status button",els=>els.map(b=>b.textContent))).includes("Try again"));
 readFailure=false;
 // While it reads: the button is busy, the line under it says what it reads,
 // and a double click sends one read.
 readDelay=400;const loadReads=requests("read").length;
 await page.click(".diagram-load");await page.click(".diagram-load");
 assert.equal(await page.$eval(".diagram-load",e=>e.getAttribute("aria-busy")),"true");
 assert.match(await page.$eval(".diagram-load-status",e=>e.textContent),/Reading shop…/);
 await page.waitForSelector(".diagram-table");readDelay=0;
 assert.equal(requests("read").length,loadReads+1,"one read for a double click");
 assert.equal(requests("read").at(-1).body.database,"shop");
 await page.waitForFunction(()=>document.activeElement?.classList.contains("diagram-canvas"));
 assert.equal(await refreshLabel(),"Refresh");
 assert.ok(await page.$('[aria-label="Diagram view"]'),"Diagram | Mermaid once a diagram is shown");
 // Choosing another database offers it; Load asks before replacing.
 await input('[aria-label="Database"]',"inventory");
 assert.match(await page.$eval(".diagram-choice",e=>e.textContent),/inventory is chosen\. The diagram shows shop\./);
 assert.equal(await refreshLabel(),"Load");
 await page.click(".diagram-choice .action-button");
 assert.equal(await page.$eval("dialog[open] .button.primary",e=>e.textContent.trim()),"Load inventory");
 await confirm();await page.waitForFunction(()=>window.diagramDocument?.source.database==="inventory");
 assert.equal(requests("read").at(-1).body.database,"inventory");
 assert.equal(await page.$(".diagram-choice"),null);assert.equal(await refreshLabel(),"Refresh");
 // A 64-character database name wraps inside the Load button at 720 px.
 await page.evaluate(()=>window.setDiagramDocument(null));await input('[aria-label="Database"]',longName);
 await page.setViewport({width:720,height:900});await pause(150);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth),0);
 assert.ok(await page.$eval(".diagram-load",e=>e.getBoundingClientRect().right<=document.documentElement.clientWidth));
 await shot("diagram-load-long-name-720.png");await page.setViewport({width:1440,height:1000});
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
  const boundaryPrevious={...d,tables:[d.tables[0]],relationships:[],layout:{positions:{[d.tables[0].id]:{x:40,y:100000}}}};
  const boundaryNext={...d,tables:d.tables.slice(0,2),relationships:[]};
  const rebuilt=h.rebuildDiagramLayout(boundaryNext,boundaryPrevious), a=rebuilt.layout.positions[d.tables[0].id], b=rebuilt.layout.positions[d.tables[1].id];
  const layoutSafe=a.x===40&&a.y===100000&&Math.abs(b.x)<=100000&&Math.abs(b.y)<=100000&&
    (b.x>=a.x+h.TABLE_WIDTH||b.x+h.TABLE_WIDTH<=a.x||b.y>=a.y+h.tableHeight(d.tables[0])||b.y+h.tableHeight(d.tables[1])<=a.y);
  return{layoutSafe,rejected,unsafe:dom.querySelectorAll("script,image,foreignObject,parsererror").length,escaped:svg.includes("&lt;script&gt;"),invalid,finite:Number.isFinite(safe.customers.x)};
 },model);
 assert.deepEqual(checks,{layoutSafe:true,rejected:true,unsafe:0,escaped:true,invalid:true,finite:true});assert.deepEqual(h.errors,[]);
 assert.ok(!state.other.some(r=>/\/(sql|editor|accounts)\//.test(r.path)||(r.path.startsWith("/api/live/")&&r.path!=="/api/live/schemas")),"imports and exports never invoke SQL execution or server writes");
 console.log("schema diagram: smooth wheel, anchors, caps, clamps, live speed, pinch, imports, exports, layout roundtrip, keyboard/pointer, fit, conceptual metadata, stale reads, hostile text, limits, dark/light and phone passed");
}finally{if(h)await h.close();await fs.rm(temp,{recursive:true,force:true});}
