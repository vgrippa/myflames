import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Connection } from "./api";
import { connectionKey, nextGeneration, useConnectionReads } from "./binding";
import { ConfirmSheet, Disclosure, EmptyState, Fields, InspectorSection, Popover, SearchField, Select, SummaryLine,
  ViewLayout, ViewStatus, closePopovers, type InspectorContent, type Selection } from "./kit";
import { autoLayout, boundedCoordinate, COLUMN_HEIGHT, conceptualRelationship, diagramBounds, diagramPositions, diagramSvg,
  MAX_IMPORT_BYTES, relationshipPath, relationshipEndpoints, cardinalityLabel, TABLE_WIDTH, tableHeight, tableLabel, validateDiagram, withDiagramLayout,
  type DiagramDocument, type DiagramRelationship, type Point, type SchemaDiagram } from "./schemaDiagramModel";
import "./schema-diagram.css";

type Format = "sql" | "mermaid" | "json";
type Viewport = { x: number; y: number; zoom: number };
type ExportFile = { text: string; extension: string; mime: string; warnings: string[] };
const clampZoom = (zoom: number) => Math.max(.15, Math.min(2, zoom));
function download(file: ExportFile) {
  const url = URL.createObjectURL(new Blob([file.text], { type: file.mime }));
  const a = document.createElement("a"); a.href = url; a.download = `database-diagram.${file.extension}`; a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function SchemaDiagramView({ connection, disabled = false, onBack, document: doc, onDocument, selection }: {
  connection: Connection; disabled?: boolean; onBack?: () => void; document: DiagramDocument | null;
  onDocument: React.Dispatch<React.SetStateAction<DiagramDocument | null>>; selection: Selection;
}) {
  const reads = useConnectionReads(connection), key = connectionKey(connection);
  const [database, setDatabase] = useState(connection.database || ""), [search, setSearch] = useState("");
  const [error, setError] = useState(""), [busy, setBusy] = useState("");
  const [importOpen, setImportOpen] = useState(false), [format, setFormat] = useState<Format>("sql");
  const [text, setText] = useState(""), [filename, setFilename] = useState("");
  const [exportFile, setExportFile] = useState<ExportFile | null>(null);
  const [viewport, setViewport] = useState<Viewport>({ x: 30, y: 30, zoom: 1 });
  const [canvasSize, setCanvasSize] = useState({ width: 800, height: 600 });
  const canvas = useRef<HTMLDivElement>(null), fileInput = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null), revision = useRef(0), mounted = useRef(true), fileRead = useRef(0);
  const latestDoc = useRef(doc); latestDoc.current = doc;
  const dragged = useRef(false);
  const drag = useRef<{ pointer: number; start: Point; original: Point; table?: string; zoom: number } | null>(null);
  const d = doc?.diagram;
  const positions = useMemo(() => d ? diagramPositions(d) : {}, [d]);
  const tables = useMemo(() => d?.tables.filter(t => `${tableLabel(t)} ${t.columns.map(c=>c.name).join(" ")}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())) || [], [d, search]);
  const visible = new Set(tables.map(t=>t.id));
  const relationships = d?.relationships.filter(r=>visible.has(r.from_table)&&visible.has(r.to_table)) || [];
  const selected = d?.tables.find(t=>t.id===selection.key);
  const other = doc?.source.kind === "server" && doc.source.key !== key ? doc.source.label : null;
  const canRead = !!(connection.host || connection.user || connection.profile_id);

  useEffect(() => {
    const el=canvas.current; if (!el) return;
    const observer = new ResizeObserver(() => setCanvasSize({width:el.clientWidth,height:el.clientHeight}));
    observer.observe(el); return ()=>observer.disconnect();
  }, [!!d]);
  useEffect(() => { if(d)fit(d.tables,diagramPositions(d)); }, [d?.tables]);
  useEffect(() => { request.current?.abort(); revision.current++; setBusy(""); setError(""); }, [key]);
  useEffect(() => { mounted.current=true; return ()=>{ mounted.current=false; request.current?.abort(); fileRead.current++; }; }, []);
  const cancelRequest = () => { request.current?.abort(); revision.current++; };
  function accept(diagram: SchemaDiagram, source: DiagramDocument["source"]) {
    const previous = latestDoc.current;
    const sameSource = source.kind === "server" && previous?.source.kind === "server" &&
      source.key === previous.source.key && source.database === previous.source.database;
    const normalized = withDiagramLayout(validateDiagram(diagram), sameSource ? previous.diagram.layout?.positions : undefined);
    onDocument({diagram:normalized,source}); selection.select("",false); setSearch(""); setError("");
    fit(normalized.tables,diagramPositions(normalized));
  }
  async function load() {
    if (!database.trim() || disabled || !canRead || busy || reads.loading) return;
    cancelRequest(); const n=revision.current, target=database.trim(); setError("");
    await reads.run(`diagram:${JSON.stringify(target)}`,"Reading database schema…",
      (c,signal)=>api<SchemaDiagram>("schema-diagram/read",{connection:c,database:target},signal), bound=>{
        if(n!==revision.current) return;
        accept(bound.report,{kind:"server",key:bound.key,label:bound.label,database:target});
      },{force:true});
  }
  async function chooseFile(file?: File) {
    if (!file) return;
    const n=++fileRead.current;
    try {
      if(file.size>MAX_IMPORT_BYTES) throw new Error("Import files must be 2 MiB or smaller.");
      const extension=file.name.split(".").pop()?.toLowerCase();
      if(!["sql","mmd","mermaid","json"].includes(extension || "")) throw new Error("Choose a SQL, Mermaid or diagram JSON file.");
      const value=await file.text(); if(n!==fileRead.current||!mounted.current)return;
      setText(value);setFilename(file.name);setFormat(extension==="json"?"json":extension==="sql"?"sql":"mermaid");setError("");
    } catch(e) { if(n===fileRead.current)setError((e as Error).message); }
  }
  async function importDiagram() {
    if(busy||!text.trim())return;
    cancelRequest(); nextGeneration("diagram"); const n=revision.current;
    const controller=new AbortController();request.current=controller; setBusy("Reading diagram…");setError("");
    try {
      if(new TextEncoder().encode(text).byteLength>MAX_IMPORT_BYTES) throw new Error("Imports must be 2 MiB or smaller.");
      const result=await api<SchemaDiagram>("schema-diagram/import",{format,text},controller.signal);
      if(controller.signal.aborted||n!==revision.current||!mounted.current)return;
      accept(result,{kind:"import",label:filename||`Imported ${format === "mermaid" ? "Mermaid" : format.toUpperCase()}`});
      setImportOpen(false);setText("");setFilename("");
    }catch(e){if(!controller.signal.aborted)setError((e as Error).message);}
    finally{if(n===revision.current&&mounted.current)setBusy("");}
  }
  async function exportDiagram(format: "json"|"svg"|"sql"|"mermaid") {
    if(!d||busy)return; closePopovers();setError("");
    try {
      if(format==="json")return download({text:JSON.stringify(withDiagramLayout(d,positions),null,2)+"\n",extension:"json",mime:"application/json",warnings:[]});
      if(format==="svg")return download({text:diagramSvg(d),extension:"svg",mime:"image/svg+xml",warnings:[]});
      cancelRequest();const n=revision.current,controller=new AbortController();request.current=controller;setBusy("Preparing export…");
      try{
        const result=await api<{text:string;warnings:string[]}>("schema-diagram/export",{format,diagram:d},controller.signal);
        if(controller.signal.aborted||n!==revision.current||latestDoc.current?.diagram!==d)return;
        if(typeof result.text!=="string"||!Array.isArray(result.warnings)||!result.warnings.every(w=>typeof w==="string"))throw new Error("The server returned an invalid export.");
        const file={text:result.text,warnings:result.warnings,extension:format==="mermaid"?"mmd":"sql",mime:"text/plain"};
        if(file.warnings.length)setExportFile(file);else download(file);
      }finally{if(n===revision.current&&mounted.current)setBusy("");}
    }catch(e){if((e as Error).name!=="AbortError")setError((e as Error).message);}
  }
  function fit(items=tables, points=positions) {
    const bounds=diagramBounds(items,points),el=canvas.current;
    const width=el?.clientWidth||canvasSize.width,height=el?.clientHeight||canvasSize.height;
    const zoom=clampZoom(Math.min(1,(width-32)/bounds.width,(height-32)/bounds.height));
    setViewport({zoom,x:(width-bounds.width*zoom)/2-bounds.x*zoom,y:(height-bounds.height*zoom)/2-bounds.y*zoom});
  }
  function zoomBy(factor:number) {
    setViewport(v=>{const zoom=clampZoom(v.zoom*factor),scale=zoom/v.zoom,cx=canvasSize.width/2,cy=canvasSize.height/2;
      return {zoom,x:cx+(v.x-cx)*scale,y:cy+(v.y-cy)*scale};});
  }
  function moveTable(id:string,p:Point) {
    onDocument(old=>old?{...old,diagram:withDiagramLayout(old.diagram,{...diagramPositions(old.diagram),[id]:{x:boundedCoordinate(p.x),y:boundedCoordinate(p.y)}})}:old);
  }
  function startDrag(e:React.PointerEvent,table?:string) {
    if(e.button!==0 || !canvas.current)return;
    if(!table&&(e.target as HTMLElement).closest(".diagram-table"))return;
    e.preventDefault();e.stopPropagation(); (e.currentTarget as HTMLElement).focus({preventScroll:true}); e.currentTarget.setPointerCapture(e.pointerId); dragged.current=false;
    drag.current={pointer:e.pointerId,start:{x:e.clientX,y:e.clientY},original:table?positions[table]:{x:viewport.x,y:viewport.y},table,zoom:viewport.zoom};
    if(table)selection.select(table,false);
  }
  function moveDrag(e:React.PointerEvent) {
    const moving=drag.current;if(!moving||moving.pointer!==e.pointerId)return;
    const dx=e.clientX-moving.start.x,dy=e.clientY-moving.start.y;
    if(Math.abs(dx)+Math.abs(dy)>4)dragged.current=true;
    if(moving.table)moveTable(moving.table,{x:moving.original.x+dx/moving.zoom,y:moving.original.y+dy/moving.zoom});
    else setViewport(v=>({...v,x:moving.original.x+dx,y:moving.original.y+dy}));
  }
  function tableKeys(e:React.KeyboardEvent,id:string) {
    const delta:Record<string,Point>={ArrowLeft:{x:-1,y:0},ArrowRight:{x:1,y:0},ArrowUp:{x:0,y:-1},ArrowDown:{x:0,y:1}};
    if(delta[e.key]){e.preventDefault();e.stopPropagation();const step=e.shiftKey?64:16,p=positions[id];moveTable(id,{x:p.x+delta[e.key].x*step,y:p.y+delta[e.key].y*step});}
    else if(e.key==="Enter"||e.key===" "){e.preventDefault();e.stopPropagation();selection.select(id,true);}
  }
  function connectionRows(r:DiagramRelationship) {
    const from=d!.tables.find(t=>t.id===r.from_table)!,to=d!.tables.find(t=>t.id===r.to_table)!;
    return <li key={r.id}><strong>{r.name||"Relationship"}</strong><span>{tableLabel(from)} → {tableLabel(to)}</span>
      <span>{conceptualRelationship(r)?"Conceptual relationship; no column mapping supplied.":`${r.from_columns.join(", ")} → ${r.to_columns.join(", ")}`}</span>
      {r.cardinality&&<span>Cardinality: {r.cardinality.from} → {r.cardinality.to}</span>}
      {r.identifying!=null&&<span>{r.identifying?"Identifying":"Non-identifying"}</span>}
      {(r.on_delete||r.on_update)&&<span>{[r.on_delete&&`On delete: ${r.on_delete}`,r.on_update&&`On update: ${r.on_update}`].filter(Boolean).join("; ")}</span>}
      <button className="button ghost" onClick={()=>selection.select(r.from_table===selected?.id?r.to_table:r.from_table,true)}>Show related table</button></li>;
  }
  const inspector:InspectorContent|null=selected?{key:selected.id,title:selected.name,meta:selected.schema||"Imported schema",body:<>
    <InspectorSection title="Table"><Fields rows={[["Columns",String(selected.columns.length)],["Source",doc!.source.label]]}/></InspectorSection>
    <InspectorSection title="Columns"><ul className="diagram-column-details">{selected.columns.map((c,i)=><li key={`${c.name}:${i}`}><strong>{c.name}</strong><code>{c.data_type}</code>
      <span>{[c.primary_key&&"Primary key",c.nullable===null?"Nullability not supplied":c.nullable?"Nullable":"Not null"].filter(Boolean).join(" · ")}</span></li>)}</ul></InspectorSection>
    {!!selected.indexes?.length&&<InspectorSection title="Indexes"><ul className="diagram-column-details">{selected.indexes.map((index,i)=><li key={i}><strong>{index.name}</strong><span>{index.primary?"Primary key":index.unique?"Unique":"Index"}: {index.columns.join(", ")}</span></li>)}</ul></InspectorSection>}
    {selected.external&&<p className="hint">External reference. Columns were not loaded from the referenced database.</p>}
    <InspectorSection title="Relationships"><ul className="diagram-relationship-details">{d!.relationships.filter(r=>r.from_table===selected.id||r.to_table===selected.id).map(connectionRows)}</ul>
      {!d!.relationships.some(r=>r.from_table===selected.id||r.to_table===selected.id)&&<p className="hint">No relationships were supplied for this table.</p>}</InspectorSection>
    <InspectorSection title="Position"><div className="diagram-position"><label>X<input type="number" value={Math.round(positions[selected.id].x)} onChange={e=>{if(Number.isFinite(e.target.valueAsNumber))moveTable(selected.id,{...positions[selected.id],x:e.target.valueAsNumber});}}/></label>
      <label>Y<input type="number" value={Math.round(positions[selected.id].y)} onChange={e=>{if(Number.isFinite(e.target.valueAsNumber))moveTable(selected.id,{...positions[selected.id],y:e.target.valueAsNumber});}}/></label></div>
      <button className="button" onClick={()=>moveTable(selected.id,autoLayout(d!)[selected.id])}>Reset table position</button></InspectorSection>
  </>}:null;
  return <ViewLayout id="diagram" title="Database diagram" className="diagram-view" onBack={onBack}
    info="Read a database schema or import SQL, Mermaid or diagram JSON. Imported SQL is never executed. Drag tables to arrange them; arrow keys move a focused table."
    controls={<><button className="button" onClick={()=>{setImportOpen(true);setError("");}} disabled={!!busy}>Import</button>
      <Popover label="Export diagram" trigger="Export" buttonClassName="button" disabled={!d||!!busy}>
        <div className="diagram-export-menu">{([["json","Editable JSON"],["svg","SVG image"],["sql","SQL schema"],["mermaid","Mermaid"]] as const).map(([value,label])=>
          <button className="button ghost" key={value} onClick={()=>void exportDiagram(value)}>{label}</button>)}</div>
      </Popover></>}
    onRefresh={doc?.source.kind==="server"?()=>void load():undefined} refreshDisabled={disabled||!!busy||!!reads.loading||!database.trim()}
    loading={!!busy||!!reads.loading} otherConnection={other} inspector={inspector} inspectorEmpty="Select a table to inspect its columns and relationships."
    status={<ViewStatus loading={busy||reads.loading} error={importOpen?reads.error:error||reads.error}/> }>
    <div className="diagram-source"><label>Database<input aria-label="Database name" placeholder="Database name" value={database} onChange={e=>setDatabase(e.target.value)}/></label>
      <button className="button" disabled={disabled||!!busy||!!reads.loading||!canRead||!database.trim()} title={!canRead?"Choose a connection to read a database. Import works without a connection.":undefined} onClick={()=>void load()}>Load database</button></div>
    {d?<>
      <SummaryLine tone={d.incomplete||d.warnings.length?"watch":"info"} toneLabel={d.incomplete?"Partial schema":"Schema"} notes={d.warnings}>
        {d.tables.length} {d.tables.length === 1 ? "table" : "tables"} · {d.relationships.length} {d.relationships.length === 1 ? "relationship" : "relationships"} · {doc.source.kind==="import"?doc.source.label:doc.source.database}
      </SummaryLine>
      <div className="diagram-tools"><SearchField label="Find a table or column" value={search} onChange={setSearch}/>
        <div className="diagram-zoom"><button className="button" aria-label="Zoom out" onClick={()=>zoomBy(1/1.2)}>−</button><output aria-label="Diagram zoom">{Math.round(viewport.zoom*100)}%</output>
          <button className="button" aria-label="Zoom in" onClick={()=>zoomBy(1.2)}>+</button><button className="button" onClick={()=>fit()}>Fit</button>
          <button className="button" onClick={()=>{const p=autoLayout(d);onDocument(old=>old?{...old,diagram:withDiagramLayout(old.diagram,p)}:old);fit(tables,p);}}>Auto layout</button></div></div>
      <div className="diagram-canvas" ref={canvas} tabIndex={0} aria-label="Database diagram canvas" onPointerDown={e=>startDrag(e)} onPointerMove={moveDrag}
        onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}
        onKeyDown={e=>{if(e.target!==e.currentTarget)return;const arrows:Record<string,Point>={ArrowLeft:{x:32,y:0},ArrowRight:{x:-32,y:0},ArrowUp:{x:0,y:32},ArrowDown:{x:0,y:-32}};
          if(arrows[e.key]){e.preventDefault();setViewport(v=>({...v,x:v.x+arrows[e.key].x,y:v.y+arrows[e.key].y}));}
          if(e.key==="+"||e.key==="="){e.preventDefault();zoomBy(1.2);}if(e.key==="-"){e.preventDefault();zoomBy(1/1.2);}if(e.key==="0"){e.preventDefault();fit();}}}>
        {!tables.length&&<p className="diagram-no-matches" role="status">{search?"No tables match this search.":"No tables were supplied in this schema."}</p>}
        <div className="diagram-world" style={{transform:`translate(${viewport.x}px,${viewport.y}px) scale(${viewport.zoom})`}}>
          <svg className="diagram-lines" width="1" height="1" aria-label="Relationships">{relationships.map(r=><g key={r.id}>
            <path d={relationshipPath(r,d,positions)} className={conceptualRelationship(r)?"is-conceptual":""}><title>{r.name||"Relationship"}{conceptualRelationship(r)?" (conceptual)":""}</title></path>
            {r.cardinality&&["from","to"].map(side=>{const ends=relationshipEndpoints(r,d,positions),p=side==="from"?ends.a:ends.b;
              return <text key={side} x={p.x+(side==="from"?(ends.right?12:-12):(ends.right?-12:12))} y={p.y-8} textAnchor="middle">{cardinalityLabel(r.cardinality![side as "from"|"to"])}</text>;})}
          </g>)}</svg>
          {tables.map(t=><section key={t.id} className={`diagram-table${selected?.id===t.id?" is-selected":""}`} style={{left:positions[t.id].x,top:positions[t.id].y,width:TABLE_WIDTH,height:tableHeight(t)}} aria-label={`Table ${tableLabel(t)}`}>
            <button className="diagram-table-heading" data-table-id={t.id} data-row-key={t.id} aria-label={`Move or inspect ${tableLabel(t)}`} aria-pressed={selected?.id===t.id}
              onPointerDown={e=>startDrag(e,t.id)} onPointerMove={moveDrag} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}
              onKeyDown={e=>tableKeys(e,t.id)} onClick={()=>{if(dragged.current){dragged.current=false;return;}selection.select(t.id,true);}} title="Drag to move. Arrow keys move; Shift moves farther. Enter opens details.">
              <strong>{t.name}</strong><span>{t.external?"External reference":t.schema||"Imported table"}</span></button>
            <ul>{!t.columns.length&&<li><span/><span className="diagram-column-name">{t.external?"Columns not loaded":"No column metadata"}</span></li>}{t.columns.map((c,i)=>{const fk=d.relationships.some(r=>!conceptualRelationship(r)&&r.from_table===t.id&&r.from_columns.includes(c.name));return <li key={`${c.name}:${i}`} style={{height:COLUMN_HEIGHT}}>
              <span className="diagram-key" title={[c.primary_key&&"Primary key",fk&&"Foreign key"].filter(Boolean).join(", ")}>{[c.primary_key&&"PK",fk&&"FK"].filter(Boolean).join(" ")}</span>
              <span className="diagram-column-name" title={c.name}>{c.name}</span><code title={c.data_type}>{c.data_type}</code></li>;})}</ul>
          </section>)}
        </div>
      </div>
      <div className="diagram-caption"><span>Drag background to pan · Drag tables to arrange</span><span>Dashed lines: conceptual relationships</span></div>
    </>:<EmptyState icon="table" title="See how your tables connect" action={<button className="button primary" onClick={()=>setImportOpen(true)}>Import a schema</button>}>
      Load a database above, or import SQL, Mermaid or editable diagram JSON.</EmptyState>}
    {importOpen&&<ConfirmSheet className="diagram-import" title="Import a schema" confirmLabel="Import diagram" confirmTone="primary" busy={!!busy} confirmDisabled={!text.trim()}
      description="Import creates a diagram. SQL in the file is never run on a server." onCancel={()=>{fileRead.current++;setImportOpen(false);setError("");}} onConfirm={()=>void importDiagram()}>
      <div className="diagram-import-controls"><Select label="Import format" value={format} onChange={v=>setFormat(v as Format)} options={[["sql","SQL schema"],["mermaid","Mermaid"],["json","Editable JSON"]]}/>
        <button className="button" onClick={()=>fileInput.current?.click()}>Choose file</button><input ref={fileInput} type="file" accept=".sql,.mmd,.mermaid,.json" hidden onChange={e=>void chooseFile(e.target.files?.[0])}/></div>
      {filename&&<p className="hint">{filename}</p>}<label className="diagram-paste">Schema text<textarea spellCheck={false} value={text} onChange={e=>{fileRead.current++;setText(e.target.value);setFilename("");}} placeholder={format==="sql"?"CREATE TABLE …":format==="mermaid"?"erDiagram":"{\"schema_version\": \"schema-diagram-1.0\", …}"}/></label>
      <p className="hint">SQL, Mermaid or JSON · Up to 2 MiB</p><ViewStatus loading={busy} error={error}/>
    </ConfirmSheet>}
    {exportFile&&<ConfirmSheet title="Review export notes" confirmTone="primary" confirmLabel="Download file" onCancel={()=>setExportFile(null)} onConfirm={()=>{download(exportFile);setExportFile(null);}}>
      <p>Some schema details cannot be represented in this format.</p><ul>{exportFile.warnings.map((w,i)=><li key={i}>{w}</li>)}</ul>
      <Disclosure title="Export preview"><pre className="diagram-export-preview">{exportFile.text}</pre></Disclosure>
    </ConfirmSheet>}
  </ViewLayout>;
}
