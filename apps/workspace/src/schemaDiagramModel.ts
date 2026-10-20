// Canonical schema data comes from Python. Layout and SVG export are local;
// none of these helpers infer keys, constraints or executable SQL.
export type DiagramColumn = { name: string; data_type: string; nullable: boolean | null; primary_key: boolean };
export type DiagramTable = { id: string; schema: string; name: string; columns: DiagramColumn[];
  indexes?: { name: string; columns: string[]; unique: boolean; primary?: boolean }[]; sql_definition?: string; external?: boolean };
export type DiagramRelationship = { id: string; name: string; from_table: string; from_columns: string[];
  to_table: string; to_columns: string[]; cardinality?: { from: string; to: string }; identifying?: boolean | null;
  on_update?: string; on_delete?: string };
export type Point = { x: number; y: number };
export type SchemaDiagram = { schema_version: "schema-diagram-1.0"; tables: DiagramTable[];
  relationships: DiagramRelationship[]; warnings: string[]; incomplete?: boolean;
  layout?: { positions: Record<string, Point> } };
export type DiagramDocument = { diagram: SchemaDiagram; source: { kind: "server" | "import"; label: string; key?: string; database?: string } };
export const TABLE_WIDTH = 280, HEADER_HEIGHT = 52, COLUMN_HEIGHT = 26, MAX_IMPORT_BYTES = 2 * 1024 * 1024;
export const tableHeight = (table: DiagramTable) => HEADER_HEIGHT + Math.max(1, table.columns.length) * COLUMN_HEIGHT + 12;
export const tableLabel = (table: DiagramTable) => table.schema ? `${table.schema}.${table.name}` : table.name;
export const boundedCoordinate = (n: number) => Math.max(-100000, Math.min(100000, n));
const point = (p: unknown): p is Point => !!p && typeof p === "object" && Number.isFinite((p as Point).x) && Number.isFinite((p as Point).y);

/** Validate at the rendering boundary too: a malformed reply never reaches a
 * giant canvas or executable markup. Unknown backend fields remain intact. */
export function validateDiagram(value: unknown): SchemaDiagram {
  const d = value as SchemaDiagram;
  if (!d || d.schema_version !== "schema-diagram-1.0" || !Array.isArray(d.tables) || d.tables.length > 200 ||
      !Array.isArray(d.relationships) || d.relationships.length > 1000 || !Array.isArray(d.warnings))
    throw new Error("The file did not contain a supported database diagram.");
  const ids = new Set<string>(); let columns = 0, keyParts = 0;
  if (d.incomplete !== undefined && typeof d.incomplete !== "boolean") throw new Error("The diagram contains invalid completeness metadata.");
  for (const t of d.tables) {
    if (!t || typeof t.id !== "string" || !t.id || ids.has(t.id) || typeof t.schema !== "string" || typeof t.name !== "string" ||
        !Array.isArray(t.columns) || t.columns.length > 10000) throw new Error("The diagram contains an invalid or duplicate table.");
    if ((t.sql_definition !== undefined && typeof t.sql_definition !== "string") ||
        (t.external !== undefined && typeof t.external !== "boolean")) throw new Error("The diagram contains invalid table metadata.");
    if (t.indexes !== undefined) {
      if (!Array.isArray(t.indexes) || t.indexes.length > 20000) throw new Error("The diagram contains invalid indexes.");
      for (const index of t.indexes) {
        if (!index || typeof index.name !== "string" || !Array.isArray(index.columns) ||
            !index.columns.every(c => typeof c === "string") || typeof index.unique !== "boolean" ||
            (index.primary !== undefined && typeof index.primary !== "boolean")) throw new Error("The diagram contains an invalid index.");
        keyParts += index.columns.length;
      }
    }
    ids.add(t.id); columns += t.columns.length;
    for (const c of t.columns) if (!c || typeof c.name !== "string" || typeof c.data_type !== "string" ||
      ![true, false, null].includes(c.nullable) || typeof c.primary_key !== "boolean") throw new Error("The diagram contains an invalid column.");
  }
  if (columns > 10000) throw new Error("The diagram exceeds the 10,000 column limit.");
  const edges = new Set<string>();
  for (const r of d.relationships) {
    if (!r || typeof r.id !== "string" || edges.has(r.id) || typeof r.name !== "string" || !ids.has(r.from_table) || !ids.has(r.to_table) ||
      !Array.isArray(r.from_columns) || !Array.isArray(r.to_columns) || !r.from_columns.every(c => typeof c === "string") || !r.to_columns.every(c => typeof c === "string"))
      throw new Error("The diagram contains an invalid relationship.");
    if ((r.on_update !== undefined && typeof r.on_update !== "string") ||
        (r.on_delete !== undefined && typeof r.on_delete !== "string") ||
        (r.identifying !== undefined && r.identifying !== null && typeof r.identifying !== "boolean"))
      throw new Error("The diagram contains invalid relationship metadata.");
    if (r.cardinality !== undefined) {
      const values = ["one", "zero-or-one", "zero-or-many", "one-or-many"];
      if (!r.cardinality || !values.includes(r.cardinality.from) || !values.includes(r.cardinality.to))
        throw new Error("The diagram contains an invalid relationship cardinality.");
    }
    keyParts += r.from_columns.length + r.to_columns.length;
    edges.add(r.id);
  }
  if (keyParts > 20000) throw new Error("The diagram exceeds the 20,000 key part limit.");
  if (!d.warnings.every(w => typeof w === "string")) throw new Error("The diagram contains invalid notes.");
  return d;
}

/** Stable grid with related tables nearby. Cycles and disconnected components
 * are visited once; layout makes no claim about dependency or cardinality. */
export function autoLayout(d: SchemaDiagram): Record<string, Point> {
  const ordered: DiagramTable[] = [], seen = new Set<string>(), byId = new Map(d.tables.map(t => [t.id, t]));
  const adjacent = new Map(d.tables.map(t => [t.id, [] as string[]]));
  for (const r of d.relationships) { adjacent.get(r.from_table)?.push(r.to_table); adjacent.get(r.to_table)?.push(r.from_table); }
  for (const table of d.tables) {
    const queue = [table.id];
    for (let id = queue.shift(); id !== undefined; id = queue.shift()) {
      if (seen.has(id)) continue; seen.add(id);
      const item = byId.get(id); if (item) ordered.push(item);
      for (const next of adjacent.get(id) || []) if (!seen.has(next)) queue.push(next);
    }
  }
  const positions: Record<string, Point> = Object.create(null), columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)));
  let y = 40;
  for (let i = 0; i < ordered.length; i += columns) {
    const row = ordered.slice(i, i + columns);
    row.forEach((table, col) => { positions[table.id] = { x: 40 + col * (TABLE_WIDTH + 110), y }; });
    y += Math.max(...row.map(tableHeight)) + 100;
  }
  return positions;
}
export function diagramPositions(d: SchemaDiagram): Record<string, Point> {
  const positions = autoLayout(d);
  for (const t of d.tables) {
    const p = d.layout?.positions?.[t.id];
    if (point(p)) positions[t.id] = { x: boundedCoordinate(p.x), y: boundedCoordinate(p.y) };
  }
  return positions;
}
export function withDiagramLayout(d: SchemaDiagram, positions?: Record<string, Point>): SchemaDiagram {
  return { ...d, layout: { positions: diagramPositions({ ...d, layout: { positions: positions || d.layout?.positions || {} } }) } };
}
/** Keep the latest arrangement when metadata is rebuilt. Newly discovered
 * tables go below retained tables, including when columns changed their height.
 * At the coordinate boundary, find a free horizontal slot instead of clamping
 * several new tables into the same position. */
export function rebuildDiagramLayout(next: SchemaDiagram, previous?: SchemaDiagram): SchemaDiagram {
  if (!previous) return withDiagramLayout(next);
  const positions = diagramPositions(previous), oldIds = new Set(previous.tables.map(t => t.id));
  const kept = next.tables.filter(t => oldIds.has(t.id)), added = next.tables.filter(t => !oldIds.has(t.id));
  if (!kept.length) return withDiagramLayout(next);
  const bottom = Math.max(...kept.map(t => positions[t.id].y + tableHeight(t))) + 48;
  const fresh = autoLayout({ ...next, tables: added, relationships: [] });
  const placed = [...kept];
  for (const table of added) {
    let candidate = { x: boundedCoordinate(fresh[table.id].x), y: boundedCoordinate(bottom + fresh[table.id].y) };
    const overlaps = (p: Point) => placed.some(t => {
      const other = positions[t.id];
      return p.x < other.x + TABLE_WIDTH + 24 && p.x + TABLE_WIDTH + 24 > other.x &&
        p.y < other.y + tableHeight(t) + 24 && p.y + tableHeight(table) + 24 > other.y;
    });
    if (overlaps(candidate)) {
      // At most 200 tables exist. Across the bounded coordinate range there
      // are over 600 slots, and each placed table can occupy at most two.
      for (let x = -100000; x <= 100000; x += TABLE_WIDTH + 48) {
        const available = { x, y: candidate.y };
        if (!overlaps(available)) { candidate = available; break; }
      }
    }
    positions[table.id] = candidate;
    placed.push(table);
  }
  return withDiagramLayout(next, positions);
}
export function diagramBounds(tables: DiagramTable[], positions: Record<string, Point>) {
  if (!tables.length) return { x: 0, y: 0, width: TABLE_WIDTH + 80, height: 200 };
  const x = Math.min(...tables.map(t => positions[t.id].x)) - 40, y = Math.min(...tables.map(t => positions[t.id].y)) - 40;
  return { x, y, width: Math.max(...tables.map(t => positions[t.id].x + TABLE_WIDTH)) - x + 40,
    height: Math.max(...tables.map(t => positions[t.id].y + tableHeight(t))) - y + 40 };
}
export function relationshipEndpoints(r: DiagramRelationship, d: SchemaDiagram, positions: Record<string, Point>) {
  const from = d.tables.find(t => t.id === r.from_table)!, to = d.tables.find(t => t.id === r.to_table)!;
  const a = positions[from.id], b = positions[to.id], right = b.x >= a.x;
  const anchor = (t: DiagramTable, p: Point, names: string[]) => p.y + (names.length && t.columns.findIndex(c => c.name === names[0]) >= 0 ?
    HEADER_HEIGHT + 4 + (t.columns.findIndex(c => c.name === names[0]) + .5) * COLUMN_HEIGHT : HEADER_HEIGHT / 2);
  return { from, to, right, a: {x:a.x+(right?TABLE_WIDTH:0),y:anchor(from,a,r.from_columns)},
    b: {x:b.x+(right?0:TABLE_WIDTH),y:anchor(to,b,r.to_columns)} };
}
export function relationshipPath(r: DiagramRelationship, d: SchemaDiagram, positions: Record<string, Point>) {
  const {from,to,right,a,b}=relationshipEndpoints(r,d,positions),fp=positions[from.id],tp=positions[to.id];
  if(from.id===to.id){const x=fp.x+TABLE_WIDTH,outside=x+50;
    return a.y===b.y?`M${x},${a.y} H${outside} V${b.y+40} H${x+25} V${b.y} H${x}`:`M${x},${a.y} H${outside} V${b.y} H${x}`;}
  const sx=a.x+(right?30:-30),ex=b.x+(right?-30:30);
  if(Math.abs(fp.y-tp.y)>HEADER_HEIGHT){
    const mid=tp.y>fp.y?(fp.y+tableHeight(from)+tp.y)/2:(tp.y+tableHeight(to)+fp.y)/2;
    return `M${a.x},${a.y} H${sx} V${mid} H${ex} V${b.y} H${b.x}`;
  }
  const lo=Math.min(a.x,b.x),hi=Math.max(a.x,b.x);
  const obstacle=d.tables.some(t=>t.id!==from.id&&t.id!==to.id&&positions[t.id].x<hi&&positions[t.id].x+TABLE_WIDTH>lo&&
    positions[t.id].y<Math.max(a.y,b.y)&&positions[t.id].y+tableHeight(t)>Math.min(a.y,b.y));
  if(obstacle){const y=Math.min(fp.y,tp.y)-40;return `M${a.x},${a.y} H${sx} V${y} H${ex} V${b.y} H${b.x}`;}
  return `M${a.x},${a.y} H${(a.x+b.x)/2} V${b.y} H${b.x}`;
}
export const cardinalityLabel = (value: string) => ({"one":"1","zero-or-one":"0..1","zero-or-many":"0..*","one-or-many":"1..*"})[value] || value;
export const conceptualRelationship = (r: DiagramRelationship) => !r.from_columns.length || !r.to_columns.length;
const xml = (s: string) => s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[c]!);

/** Standalone SVG contains only generated geometry and escaped text. No
 * foreignObject, script, event attributes, remote fonts or image references. */
export function diagramSvg(d: SchemaDiagram): string {
  validateDiagram(d);
  const positions = diagramPositions(d), bounds = diagramBounds(d.tables, positions);
  const width = Math.min(4096, Math.ceil(bounds.width)), height = width * bounds.height / bounds.width;
  const parts = [`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${bounds.x} ${bounds.y} ${bounds.width} ${bounds.height}" width="${width}" height="${height}" role="img" aria-label="Database diagram">`,
    `<rect x="${bounds.x}" y="${bounds.y}" width="${bounds.width}" height="${bounds.height}" fill="#f5f7fa"/>`, '<g font-family="system-ui,sans-serif" font-size="12">'];
  for (const r of d.relationships) { parts.push(`<path d="${relationshipPath(r,d,positions)}" fill="none" stroke="#52677e" stroke-width="2"${conceptualRelationship(r) ? ' stroke-dasharray="6 4"' : ''}><title>${xml(r.name || "Relationship")}</title></path>`);
    if(r.cardinality){const ends=relationshipEndpoints(r,d,positions);for(const [p,label] of [[ends.a,r.cardinality.from],[ends.b,r.cardinality.to]] as const)
      parts.push(`<text x="${p.x+(p===ends.a?(ends.right?12:-12):(ends.right?-12:12))}" y="${p.y-8}" text-anchor="middle" fill="#334155">${xml(cardinalityLabel(label))}</text>`);}
  }
  for (const t of d.tables) {
    const p = positions[t.id];
    parts.push(`<g><rect x="${p.x}" y="${p.y}" width="${TABLE_WIDTH}" height="${tableHeight(t)}" rx="8" fill="#ffffff" stroke="#64748b"/>`,
      `<text x="${p.x+14}" y="${p.y+21}" fill="#14243a" font-weight="600">${xml(t.name.slice(0,32))}<title>${xml(tableLabel(t))}</title></text>`,
      `<text x="${p.x+14}" y="${p.y+40}" fill="#475569">${xml(t.schema.slice(0,38))}</text>`);
    t.columns.forEach((c,i)=>{
      const foreign = d.relationships.some(r=>!conceptualRelationship(r)&&r.from_table===t.id&&r.from_columns.includes(c.name));
      const y=p.y+HEADER_HEIGHT+(i+.7)*COLUMN_HEIGHT;
      parts.push(`<text x="${p.x+12}" y="${y}" fill="#475569">${[c.primary_key && "PK", foreign && "FK"].filter(Boolean).join(" ")}</text>`,
        `<text x="${p.x+42}" y="${y}" fill="#14243a">${xml(c.name.slice(0,17))}<title>${xml(c.name)}</title></text>`,
        `<text x="${p.x+TABLE_WIDTH-12}" y="${y}" text-anchor="end" fill="#475569">${xml(c.data_type.slice(0,12))}<title>${xml(c.data_type)}</title></text>`);
    });
    parts.push('</g>');
  }
  parts.push(`<text x="${bounds.x+16}" y="${bounds.y+bounds.height-12}" fill="#475569">Dashed lines: conceptual relationships. PK: primary key. FK: foreign key.</text>`);
  parts.push('</g></svg>'); return parts.join('');
}
