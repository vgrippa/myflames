import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Connection } from "./api";
import { connectionKey, connectionLabel, nextGeneration, useConnectionReads } from "./binding";
import { useSchemaCatalog } from "./PerfShared";
import { ConfirmSheet, Disclosure, EmptyState, Fields, IconButton, InspectorSection, Popover, SearchField, Segmented, Select, SummaryLine,
  ViewLayout, ViewStatus, closePopovers, type InspectorContent, type Selection } from "./kit";
import { autoLayout, boundedCoordinate, COLUMN_HEIGHT, conceptualRelationship, diagramBounds, diagramPositions, diagramSvg,
  MAX_IMPORT_BYTES, relationshipPath, relationshipMarkerPath, relationshipEndpoints, cardinalityLabel, TABLE_WIDTH, tableHeight, tableLabel, validateDiagram, withDiagramLayout, rebuildDiagramLayout,
  type DiagramDocument, type DiagramRelationship, type Point, type SchemaDiagram } from "./schemaDiagramModel";
import { Icon } from "./components";
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

// Mermaid has global configuration and a render queue. Keep initialization and
// rendering together so a superseded theme cannot affect a newer preview.
let mermaidRenderQueue: Promise<unknown> = Promise.resolve();
let mermaidRenderId = 0;
const MERMAID_TEMPLATES: Record<string, { label: string; theme: "dark" | "default" | "neutral" | "forest" | "base"; themeVariables?: Record<string, string> }> = {
  dark_modern: {
    label: "Dark Modern",
    theme: "dark",
    themeVariables: {
      primaryColor: "#1e293b",
      primaryTextColor: "#f1f5f9",
      primaryBorderColor: "#3b82f6",
      lineColor: "#64748b",
      secondaryColor: "#0f172a",
      tertiaryColor: "#1e293b",
    },
  },
  apple_light: {
    label: "Apple Light",
    theme: "default",
    themeVariables: {
      primaryColor: "#f8fafc",
      primaryTextColor: "#0f172a",
      primaryBorderColor: "#0071e3",
      lineColor: "#94a3b8",
      secondaryColor: "#f1f5f9",
      tertiaryColor: "#e2e8f0",
    },
  },
  ocean: {
    label: "Ocean Blue",
    theme: "base",
    themeVariables: {
      primaryColor: "#0f172a",
      primaryTextColor: "#38bdf8",
      primaryBorderColor: "#0284c7",
      lineColor: "#0ea5e9",
      secondaryColor: "#0369a1",
      tertiaryColor: "#0c4a6e",
    },
  },
  forest: {
    label: "Forest Emerald",
    theme: "forest",
    themeVariables: {
      primaryColor: "#064e3b",
      primaryTextColor: "#ecfdf5",
      primaryBorderColor: "#10b981",
      lineColor: "#34d399",
    },
  },
  neutral: {
    label: "Neutral Minimal",
    theme: "neutral",
  },
};

function MermaidDiagram({ diagram }: { diagram: SchemaDiagram }) {
  const [file, setFile] = useState<ExportFile | null>(null), [error, setError] = useState("");
  const [renderError, setRenderError] = useState(""), [rendering, setRendering] = useState(false);
  const [svg, setSvg] = useState<{text:string; width:number; height:number} | null>(null);
  const [dark, setDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches);
  const [template, setTemplate] = useState<string>(() => matchMedia("(prefers-color-scheme: dark)").matches ? "dark_modern" : "apple_light");
  const [copied, setCopied] = useState(false), [copyError, setCopyError] = useState("");
  const [viewport, setViewport] = useState<Viewport>({ x: 0, y: 0, zoom: 1 });
  const [size, setSize] = useState({width:800,height:500});
  const frame = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointer: number; start: Point; original: Point } | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)"), update = () => {
      const isDark = media.matches;
      setDark(isDark);
    };
    media.addEventListener("change", update); update();
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const el = frame.current; if (!el) return;
    const observer = new ResizeObserver(() => setSize({width:el.clientWidth,height:el.clientHeight}));
    observer.observe(el); return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setFile(null); setError(""); setCopied(false); setCopyError("");
    const timer = window.setTimeout(() => {
      void api<{text:string;warnings:string[]}>("schema-diagram/export", {format:"mermaid",diagram}, controller.signal)
        .then(result => {
          if (controller.signal.aborted) return;
          if (typeof result.text !== "string" || !Array.isArray(result.warnings) || !result.warnings.every(w=>typeof w==="string"))
            throw new Error("The server returned an invalid export.");
          setFile({...result,extension:"mmd",mime:"text/plain"});
        }).catch(e => { if (!controller.signal.aborted) setError((e as Error).message); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [diagram]);

  function fitView(s = svg, sz = size) {
    if (!s) return;
    const z = clampZoom(Math.min(1, (sz.width - 32) / s.width, (sz.height - 32) / s.height));
    setViewport({
      zoom: z,
      x: (sz.width - s.width * z) / 2,
      y: (sz.height - s.height * z) / 2,
    });
  }

  useEffect(() => {
    let cancelled = false;
    setSvg(null); setRenderError(""); setRendering(!!file);
    if (!file) return;
    const id = `database-mermaid-${++mermaidRenderId}`;
    const render = async () => {
      // Paint the loading state before loading Mermaid or doing layout work.
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      if (cancelled) return;
      try {
        const mermaid = (await import("mermaid")).default;
        if (cancelled) return;
        const tmpl = MERMAID_TEMPLATES[template] || (dark ? MERMAID_TEMPLATES.dark_modern : MERMAID_TEMPLATES.apple_light);
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          maxTextSize: 10000000,
          theme: tmpl.theme,
          ...(tmpl.themeVariables ? { themeVariables: tmpl.themeVariables } : {}),
        });
        const result = await mermaid.render(id, file.text);
        if (cancelled) return;
        const root = new DOMParser().parseFromString(result.svg,"image/svg+xml").documentElement;
        const box = root.getAttribute("viewBox")?.trim().split(/[\s,]+/).map(Number);
        if (root.localName !== "svg" || !box || box.length !== 4 || !box.every(Number.isFinite) || box[2] <= 0 || box[3] <= 0)
          throw new Error("Mermaid returned an invalid preview.");
        root.setAttribute("width",String(box[2])); root.setAttribute("height",String(box[3])); root.style.maxWidth="none";
        const svgObj = {text:new XMLSerializer().serializeToString(root),width:box[2],height:box[3]};
        setSvg(svgObj);
        fitView(svgObj, size);
      } catch(e) {
        if (!cancelled) setRenderError((e as Error).message || "Could not render this Mermaid diagram.");
      } finally {
        // Mermaid leaves its temporary body container behind on parse errors.
        document.getElementById(`d${id}`)?.remove();
        if (!cancelled) setRendering(false);
      }
    };
    mermaidRenderQueue = mermaidRenderQueue.catch(()=>{}).then(render);
    return () => { cancelled = true; };
  }, [file, template, dark]);

  function zoomBy(factor:number) {
    setViewport(v => {
      const zoom = clampZoom(v.zoom * factor);
      const scale = zoom / v.zoom;
      const cx = size.width / 2, cy = size.height / 2;
      return { zoom, x: cx + (v.x - cx) * scale, y: cy + (v.y - cy) * scale };
    });
  }

  function startDrag(e: React.PointerEvent) {
    if (e.button !== 0 || !frame.current) return;
    e.preventDefault(); e.stopPropagation();
    (e.currentTarget as HTMLElement).focus({ preventScroll: true });
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { pointer: e.pointerId, start: { x: e.clientX, y: e.clientY }, original: { x: viewport.x, y: viewport.y } };
    setIsDragging(true);
  }

  function moveDrag(e: React.PointerEvent) {
    const moving = drag.current;
    if (!moving || moving.pointer !== e.pointerId) return;
    const dx = e.clientX - moving.start.x, dy = e.clientY - moving.start.y;
    setViewport(v => ({ ...v, x: moving.original.x + dx, y: moving.original.y + dy }));
  }

  function endDrag(e: React.PointerEvent) {
    if (drag.current?.pointer === e.pointerId) {
      drag.current = null;
      setIsDragging(false);
      try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId); } catch {}
    }
  }

  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const rect = el.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      setViewport(v => {
        const zoom = clampZoom(v.zoom * factor);
        const scale = zoom / v.zoom;
        return { zoom, x: cx + (v.x - cx) * scale, y: cy + (v.y - cy) * scale };
      });
    };
    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, []);

  function onKeyDown(e: React.KeyboardEvent) {
    const step = 40;
    if (e.key === "ArrowUp") { e.preventDefault(); setViewport(v => ({ ...v, y: v.y + step })); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setViewport(v => ({ ...v, y: v.y - step })); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); setViewport(v => ({ ...v, x: v.x + step })); }
    else if (e.key === "ArrowRight") { e.preventDefault(); setViewport(v => ({ ...v, x: v.x - step })); }
    else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomBy(1.2); }
    else if (e.key === "-" || e.key === "_") { e.preventDefault(); zoomBy(1 / 1.2); }
    else if (e.key === "0") { e.preventDefault(); fitView(); }
  }

  async function copy() {
    if (!file) return;
    setCopyError("");
    try { await navigator.clipboard.writeText(file.text); setCopied(true); }
    catch(e) { setCopyError((e as Error).message || "Could not copy Mermaid code."); }
  }
  return <div className="diagram-mermaid">
    <section className="diagram-mermaid-panel" aria-label="Mermaid preview" data-theme={dark?"dark":"default"}>
      <div className="diagram-mermaid-toolbar">
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <strong>Preview</strong>
          <select
            className="select small"
            aria-label="Color template"
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            style={{ fontSize: "11px", padding: "2px 8px", height: "24px" }}
          >
            {Object.entries(MERMAID_TEMPLATES).map(([k, t]) => (
              <option key={k} value={k}>{t.label}</option>
            ))}
          </select>
        </div>
        <div className="diagram-zoom">
        <button className="button" aria-label="Zoom out" disabled={!svg} onClick={()=>zoomBy(1/1.2)}>−</button>
        <output aria-label="Mermaid zoom">{Math.round(viewport.zoom*100)}%</output>
        <button className="button" aria-label="Zoom in" disabled={!svg} onClick={()=>zoomBy(1.2)}>+</button>
        <button className="button" disabled={!svg} onClick={()=>fitView()}>Fit</button>
      </div></div>
      <div
        className={`diagram-mermaid-preview${isDragging ? " is-dragging" : ""}`}
        ref={frame}
        tabIndex={0}
        aria-label="Mermaid preview canvas"
        aria-busy={rendering}
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onKeyDown={onKeyDown}
      >
        {rendering&&<p className="hint" role="status">Rendering…</p>}
        {renderError&&<p className="diagram-mermaid-error" role="alert">{renderError}</p>}
        {svg&&<div
          className="diagram-mermaid-world"
          style={{
            transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`,
            transformOrigin: "0 0",
            width: svg.width,
            height: svg.height,
          }}
          dangerouslySetInnerHTML={{__html:svg.text}}
        />}
      </div>
    </section>
    <section className="diagram-mermaid-panel" aria-label="Mermaid code">
      <div className="diagram-mermaid-toolbar"><strong>Code</strong><div>
        <button className="button" disabled={!file} onClick={()=>void copy()}>Copy</button>
        <button className="button" disabled={!file} onClick={()=>file&&download(file)}>Download .mmd</button>
      </div></div>
      {copied&&<p className="hint" role="status">Copied</p>}
      <ViewStatus error={error||copyError}/>
      <pre className="diagram-mermaid-code" tabIndex={0} aria-label="Mermaid source"><code>{file?.text || (error?"":"Preparing Mermaid…")}</code></pre>
      {!!file?.warnings.length&&<Disclosure title={`Export notes (${file.warnings.length})`}><ul>{file.warnings.map((w,i)=><li key={i}>{w}</li>)}</ul></Disclosure>}
    </section>
  </div>;
}

export function SchemaDiagramView({ connection, disabled = false, onBack, document: doc, onDocument, selection, selectedDatabase, onDatabaseChange }: {
  connection: Connection; disabled?: boolean; onBack?: () => void; document: DiagramDocument | null;
  onDocument: React.Dispatch<React.SetStateAction<DiagramDocument | null>>; selection: Selection;
  selectedDatabase?: string; onDatabaseChange?: (key: string, database: string) => void;
}) {
  const reads = useConnectionReads(connection), key = connectionKey(connection);
  const [view, setView] = useState<"diagram"|"mermaid">("diagram");
  const [database, setDatabase] = useState(selectedDatabase ?? connection.database ?? ""), [search, setSearch] = useState("");
  const [error, setError] = useState(""), [busy, setBusy] = useState("");
  const [importOpen, setImportOpen] = useState(false), [format, setFormat] = useState<Format>("sql");
  const [text, setText] = useState(""), [filename, setFilename] = useState("");
  const [exportFile, setExportFile] = useState<ExportFile | null>(null);
  const [replacement, setReplacement] = useState<{key: string; database: string} | null>(null);
  const [readAt, setReadAt] = useState<{key: string; database: string; at: Date} | null>(null);
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
  const catalog = useSchemaCatalog(connection, canRead && !disabled);
  const databaseNames = catalog.catalog?.schemas.map(s => s.name) || [];

  useEffect(() => {
    const el=canvas.current; if (!el) return;
    const observer = new ResizeObserver(() => setCanvasSize({width:el.clientWidth,height:el.clientHeight}));
    observer.observe(el); return ()=>observer.disconnect();
  }, [!!d, view]);
  useEffect(() => { if(d)fit(d.tables,diagramPositions(d)); }, [d?.tables]);
  // A new connection or default database drops the reads on their way,
  // Refresh all's too. Opening the place (this view mounting) does not:
  // Refresh all started on Server and still reading the diagram keeps its answer.
  const identitySeen = useRef(false);
  useEffect(() => {
    request.current?.abort(); revision.current++; setBusy(""); setError("");
    if (identitySeen.current) nextGeneration("diagram");
    identitySeen.current = true;
    reads.clearError();
    const target = selectedDatabase ?? connection.database ?? "";
    setDatabase(target); setReplacement(null);
    onDatabaseChange?.(key, target);
  }, [key, connection.database]);
  useEffect(() => { mounted.current=true; return ()=>{ mounted.current=false; request.current?.abort(); fileRead.current++; }; }, []);
  const cancelRequest = () => { request.current?.abort(); revision.current++; };
  function chooseDatabase(value: string) {
    cancelRequest(); nextGeneration("diagram"); reads.clearError(); setError("");
    setReplacement(null); setDatabase(value); onDatabaseChange?.(key, value);
  }
  function accept(diagram: SchemaDiagram, source: DiagramDocument["source"]) {
    const previous = latestDoc.current;
    const sameSource = source.kind === "server" && previous?.source.kind === "server" &&
      source.key === previous.source.key && source.database === previous.source.database;
    const validated = validateDiagram(diagram);
    const normalized = rebuildDiagramLayout(validated, sameSource ? previous?.diagram : undefined);
    onDocument({diagram:normalized,source}); selection.select("",false); setSearch(""); setError("");
    fit(normalized.tables,diagramPositions(normalized));
  }
  async function load(target = database.trim()) {
    if (!target || disabled || !canRead || busy || reads.loading) return;
    cancelRequest(); const n=revision.current, controller=new AbortController(); request.current=controller; setError("");
    await reads.run(`diagram:${JSON.stringify(target)}`,"Rebuilding database diagram…",
      (c,signal)=>{
        // Selection changes also cancel this read, independently of the connection identity.
        const abort=()=>controller.abort(); signal.addEventListener("abort",abort,{once:true});
        if(signal.aborted)controller.abort();
        return api<SchemaDiagram>("schema-diagram/read",{connection:c,database:target},controller.signal)
          .finally(()=>signal.removeEventListener("abort",abort));
      }, bound=>{
        if(n!==revision.current) return;
        accept(bound.report,{kind:"server",key:bound.key,label:bound.label,database:target});
        setReadAt({key:bound.key,database:target,at:new Date()});
      },{force:true});
  }
  function rebuild() {
    const target=database.trim() || (doc?.source.kind==="server" && doc.source.key===key ? doc.source.database || "" : "");
    if(!target || disabled || !canRead || busy || reads.loading)return;
    if(doc && (doc.source.kind!=="server" || doc.source.key!==key || doc.source.database!==target)) {
      setReplacement({key,database:target});
    } else void load(target);
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
  const targetDb = database.trim() || (doc?.source.kind === "server" && doc.source.key === key ? doc.source.database || "" : "");
  return <ViewLayout id="diagram" title="Database diagram" className="diagram-view" onBack={onBack}
    info="Read a database schema or import SQL, Mermaid or diagram JSON. Imported SQL is never executed. Drag tables to arrange them; arrow keys move a focused table."
    controls={<>
      <div className="diagram-source">
        <label className="toolbar-field diagram-database">
          <span>Database</span>
          {catalog.error ? (
            <input
              type="text"
              aria-label="Database"
              placeholder="Enter a database"
              value={database}
              disabled={!canRead || disabled || !!busy}
              onChange={e => chooseDatabase(e.target.value)}
            />
          ) : (
            <select
              aria-label="Database"
              value={database}
              disabled={!canRead || disabled || !!busy}
              onChange={e => chooseDatabase(e.target.value)}
            >
              <option value="">{catalog.loading ? "Loading databases…" : "Choose a database"}</option>
              {database && !databaseNames.includes(database) && <option value={database}>{database}</option>}
              {databaseNames.map(name => <option key={name} value={name}>{name}</option>)}
            </select>
          )}
        </label>
        {canRead && (
          <IconButton
            icon="refresh"
            className="diagram-reload-list"
            disabled={catalog.loading || disabled}
            label="Reload database list"
            title="Reload the list of database names from the server"
            onClick={catalog.reload}
          />
        )}
      </div>
      <Segmented label="Diagram view" value={view} options={[{value:"diagram",label:"Diagram"},{value:"mermaid",label:"Mermaid"}]} onChange={setView}/>
      <button className="button" onClick={()=>{setImportOpen(true);setError("");}} disabled={!!busy}>Import</button>
      <Popover label="Export diagram" trigger={<><Icon name="export" size={15}/><span>Export</span></>} buttonClassName="button" disabled={!d||!!busy}>
        <div className="diagram-export-menu">{([["json","Editable JSON"],["svg","SVG image"],["sql","SQL schema"],["mermaid","Mermaid"]] as const).map(([value,label])=>
          <button className="button ghost" key={value} onClick={()=>void exportDiagram(value)}>{label}</button>)}</div>
      </Popover></>}
    onRefresh={rebuild} refreshDisabled={disabled||!!busy||!!reads.loading||!canRead||!targetDb}
    loading={!!busy||!!reads.loading} otherConnection={other} inspector={inspector} inspectorEmpty="Select a table to inspect its columns and relationships."
    status={<ViewStatus loading={busy||reads.loading} error={importOpen?reads.error:error||reads.error}/> }>
    {catalog.error&&<p className="diagram-source-note" role="status">Database list unavailable: {catalog.error} Enter a database name to refresh.</p>}
    {d?<>
      <SummaryLine tone={d.incomplete||d.warnings.length?"watch":"ok"} notes={d.warnings}>
        {d.incomplete?"Partial schema":"Schema"} · {d.tables.length} {d.tables.length === 1 ? "table" : "tables"} · {d.relationships.length} {d.relationships.length === 1 ? "relationship" : "relationships"} · {doc.source.kind==="import"?doc.source.label:doc.source.database}
        {canRead&&<> · Connection: {connectionLabel(connection)}</>}
        {doc.source.kind==="server"&&readAt&&readAt.key===doc.source.key&&readAt.database===doc.source.database&&<> · Read <time dateTime={readAt.at.toISOString()} title={readAt.at.toLocaleString()}>{readAt.at.toLocaleTimeString()}</time></>}
      </SummaryLine>
      {view==="mermaid"&&d.tables.length>0?<MermaidDiagram diagram={d}/>:<>
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
            <path d={relationshipMarkerPath(r,d,positions)} className="diagram-fk-marker" aria-hidden="true"/>
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
      <div className="diagram-caption"><span>Drag background to pan · Drag tables to arrange</span>{relationships.some(conceptualRelationship)&&<span>Dashed lines: conceptual relationships</span>}</div>
      </>}
    </>:<EmptyState icon="table" title="See how your tables connect" action={<div className="diagram-empty-actions">
      <button className="button primary" onClick={()=>setImportOpen(true)}>Import a schema</button>
      <button className="button" onClick={()=>{setFormat("mermaid");setImportOpen(true);setError("");}}>View Mermaid</button>
    </div>}>
      Choose a live database, or import a schema. Mermaid ER diagrams open as an interactive canvas inside myflames.</EmptyState>}
    {replacement&&<ConfirmSheet title="Replace this diagram?" confirmLabel="Rebuild diagram" confirmTone="primary"
      description={`Read ${replacement.database} from ${connectionLabel(connection)} and replace the diagram on this screen. No database changes will be made.`}
      onCancel={()=>setReplacement(null)} onConfirm={()=>{const target=replacement;setReplacement(null);if(target.key===reads.currentKey()&&target.database===(database.trim()||target.database))void load(target.database);}}>
      <p>Export the current diagram first if you want to keep it. Its layout is kept only when rebuilding the same database on the same connection.</p>
    </ConfirmSheet>}
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
