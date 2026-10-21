// The Query editor's schema browser: schemas, their tables and views, and a
// table's columns and indexes, read on demand from the connection in use
// (/api/live/schemas, shared with the Server pickers, then /api/live/catalog).
// Nothing is read until the user asks; every read is keyed by the connection
// (binding.ts), so a switch never shows another server's names.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api, type CatalogTable, type CatalogTables, type Connection } from "./api";
import { connectionKey, connectionLabel } from "./binding";
import { Icon, type IconName } from "./components";
import { ActionStatus, IconButton, SearchField, useFocusScope, useSince } from "./kit";
import { useSchemaCatalog } from "./PerfShared";
import { quoteName } from "./sqlHighlight";

type Loaded<T> = { loading: boolean; error: string; data: T | null };
type TreeNode = {
  id: string;
  level: number;
  kind: "schema" | "table" | "view" | "folder" | "column" | "index" | "status" | "more";
  label: string;
  meta?: string;
  icon?: IconName;
  expandable: boolean;
  expanded: boolean;
  schema?: string;
  table?: string;
  /** Text inserted into the editor on double-click. */
  insert?: string;
  /** A status line (loading, error, empty) instead of an object. */
  tone?: "error" | "muted";
  title?: string;
};
type Menu = { x: number; y: number; node: TreeNode; opener: HTMLElement } | null;

const tablesKey = (key: string, schema: string, prefix: string) => JSON.stringify([key, schema, prefix]);
const tableKey = (key: string, schema: string, table: string) => `${key}\u0000${schema}\u0000${table}`;

export function SchemaBrowser({
  connection,
  width,
  onWidth,
  onClose,
  onInsert,
  onQuery,
  onOpenTable,
  onOpenData,
}: {
  connection: Connection;
  width: number;
  onWidth: (width: number) => void;
  onClose: () => void;
  /** Insert text at the editor's cursor. */
  onInsert: (text: string) => void;
  /** Add a statement to the editor and run it. */
  onQuery: (sql: string) => void;
  /** Open a Server view on one table (from: the connection key the name was read from). */
  onOpenData?: (schema: string, table: string, from: string) => void;
  onOpenTable?: (view: "indexes" | "tables", schema: string, table: string, from: string) => void;
}) {
  const key = connectionKey(connection);
  const label = connectionLabel(connection);
  const [asked, setAsked] = useState<Set<string>>(() => new Set());
  const enabled = asked.has(key);
  const catalog = useSchemaCatalog(connection, enabled);
  const [tables, setTables] = useState<Record<string, Loaded<CatalogTables>>>({});
  const [details, setDetails] = useState<Record<string, Loaded<CatalogTable>>>({});
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const [filter, setFilter] = useState("");
  const [focus, setFocus] = useState("");
  const [menu, setMenu] = useState<Menu>(null);
  const tree = useRef<HTMLDivElement>(null);
  const browser = useRef<HTMLElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const connectionRef = useRef(connection);
  connectionRef.current = connection;
  const pagesRef = useRef(tables);
  pagesRef.current = tables;
  const requests = useRef(new Map<string, AbortController>());
  const detailRequests = useRef(new Map<string, AbortController>());

  async function loadTables(schema: string, prefix: string, after: string | null = null) {
    const k = tablesKey(key, schema, prefix);
    if (requests.current.has(k)) return;
    const controller = new AbortController();
    requests.current.set(k, controller);
    setTables((t) => ({ ...t, [k]: { loading: true, error: "", data: t[k]?.data || null } }));
    try {
      const data = await api<CatalogTables>("live/catalog", { connection: connectionRef.current, schema, prefix, after }, controller.signal);
      if (controller.signal.aborted) return;
      setTables((t) => {
        const previous = after ? t[k]?.data?.tables || [] : [];
        const names = new Set(previous.map((table) => table.name));
        return { ...t, [k]: { loading: false, error: "", data: {
          ...data, tables: [...previous, ...data.tables.filter((table) => !names.has(table.name))],
        } } };
      });
    } catch (e) {
      if (!controller.signal.aborted)
        setTables((t) => ({ ...t, [k]: { loading: false, error: (e as Error).message, data: t[k]?.data || null } }));
    } finally {
      if (requests.current.get(k) === controller) requests.current.delete(k);
    }
  }
  // Read only expanded schemas, with a literal server-side prefix. Separate
  // cache entries keep late pages from one search out of another search.
  const loadTablesRef = useRef(loadTables);
  loadTablesRef.current = loadTables;
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setTimeout(() => {
      for (const s of catalog.catalog?.schemas || []) {
        if (open.has(`s:${s.name}`) && !pagesRef.current[tablesKey(key, s.name, filter)]?.data)
          void loadTablesRef.current(s.name, filter);
      }
    }, 200);
    return () => window.clearTimeout(timer);
  }, [key, enabled, open, filter, catalog.catalog]);
  useEffect(() => {
    const active = requests.current;
    const activeDetails = detailRequests.current;
    return () => {
      for (const controller of activeDetails.values()) controller.abort();
      activeDetails.clear();
      setDetails((items) => Object.fromEntries(Object.entries(items).filter(([, item]) => !item.loading)));
      for (const controller of active.values()) controller.abort();
      active.clear();
      // Aborted pages may be requested again when returning to a connection.
      setTables((pages) => Object.fromEntries(Object.entries(pages).filter(([, page]) => !page.loading)));
    };
  }, [key]);
  async function loadTable(schema: string, table: string) {
    const k = tableKey(key, schema, table);
    if (detailRequests.current.has(k)) return;
    const controller = new AbortController();
    detailRequests.current.set(k, controller);
    const from = { ...connectionRef.current };
    setDetails((t) => ({ ...t, [k]: { loading: true, error: "", data: t[k]?.data || null } }));
    try {
      const data = await api<CatalogTable>("live/catalog", { connection: from, schema, table }, controller.signal);
      if (controller.signal.aborted || connectionKey(connectionRef.current) !== key) return;
      setDetails((t) => ({ ...t, [k]: { loading: false, error: "", data } }));
    } catch (e) {
      if (controller.signal.aborted || connectionKey(connectionRef.current) !== key) return;
      setDetails((t) => ({ ...t, [k]: { loading: false, error: (e as Error).message, data: null } }));
    } finally {
      if (detailRequests.current.get(k) === controller) detailRequests.current.delete(k);
    }
  }
  function toggle(node: TreeNode, force?: boolean) {
    if (!node.expandable) return;
    const next = new Set(open);
    const opening = force ?? !next.has(node.id);
    if (opening) next.add(node.id);
    else next.delete(node.id);
    setOpen(next);
    if (!opening) return;
    if ((node.kind === "table" || node.kind === "view") && node.schema && node.table) {
      if (!details[tableKey(key, node.schema, node.table)]?.data) void loadTable(node.schema, node.table);
      // Columns open with their table; Indexes stay folded.
      setOpen(new Set(next).add(`${node.id}/columns`));
    }
  }
  function refresh() {
    for (const controller of detailRequests.current.values()) controller.abort();
    detailRequests.current.clear();
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear();
    setTables({});
    setDetails((t) => Object.fromEntries(Object.entries(t).filter(([k]) => !k.startsWith(key + "\u0000"))));
    setOpen(new Set());
    if (enabled) catalog.reload();
    else setAsked(new Set(asked).add(key));
  }

  // The visible rows of the tree, in order.
  const nodes = useMemo(() => {
    const out: TreeNode[] = [];
    const schemas = catalog.catalog?.schemas || [];
    for (const s of schemas) {
      const sid = `s:${s.name}`;
      const loaded = tables[tablesKey(key, s.name, filter)];
      const tableList = loaded?.data?.tables || [];
      const expanded = open.has(sid);
      out.push({
        id: sid,
        level: 1,
        kind: "schema",
        label: s.name,
        meta: s.system ? "system" : `${s.tables.toLocaleString()}`,
        title: `${s.name}: ${s.tables.toLocaleString()} ${s.tables === 1 ? "table" : "tables"}${s.system ? " (system schema)" : ""}`,
        icon: "server",
        expandable: true,
        expanded,
        schema: s.name,
        insert: quoteName(s.name),
      });
      if (!expanded) continue;
      if (loaded?.loading && !loaded.data) out.push(status(`${sid}/loading`, 2, "Loading tables…", "muted"));
      if (loaded?.error) out.push(status(`${sid}/error`, 2, loaded.error, "error"));
      if (loaded?.data && !tableList.length) out.push(status(`${sid}/empty`, 2, "No tables", "muted"));
      for (const t of tableList) {
        const tid = `t:${s.name}\u001f${t.name}`;
        const isView = t.type === "view" || t.type === "system view";
        const tExpanded = open.has(tid);
        out.push({
          id: tid,
          level: 2,
          kind: isView ? "view" : "table",
          label: t.name,
          meta: t.type === "table" ? "" : t.type,
          icon: isView ? "view" : "table",
          expandable: true,
          expanded: tExpanded,
          schema: s.name,
          table: t.name,
          insert: `${quoteName(s.name)}.${quoteName(t.name)}`,
        });
        if (!tExpanded) continue;
        const d = details[tableKey(key, s.name, t.name)];
        if (d?.loading && !d.data) out.push(status(`${tid}/loading`, 3, "Loading columns…", "muted"));
        if (d?.error) out.push(status(`${tid}/error`, 3, d.error, "error"));
        if (!d?.data) continue;
        const cid = `${tid}/columns`;
        const cOpen = open.has(cid);
        out.push({ id: cid, level: 3, kind: "folder", label: "Columns", meta: String(d.data.columns.length), icon: "columns", expandable: true, expanded: cOpen });
        if (cOpen)
          for (const c of d.data.columns)
            out.push({
              id: `${cid}/${c.name}`,
              level: 4,
              kind: "column",
              label: c.name,
              meta: [c.type, c.key === "PRI" ? "PK" : c.key === "UNI" ? "unique" : c.key === "MUL" ? "indexed" : "", c.nullable ? "" : "not null"]
                .filter(Boolean)
                .join(" · "),
              title: [
                `${c.name} ${c.type}${c.nullable ? "" : " NOT NULL"}`,
                c.default !== null ? `DEFAULT ${c.default}` : "",
                c.extra,
              ]
                .filter(Boolean)
                .join(" "),
              expandable: false,
              expanded: false,
              schema: s.name,
              table: t.name,
              insert: quoteName(c.name),
            });
        const iid = `${tid}/indexes`;
        const iOpen = open.has(iid);
        out.push({ id: iid, level: 3, kind: "folder", label: "Indexes", meta: String(d.data.indexes.length), icon: "index", expandable: true, expanded: iOpen });
        if (iOpen)
          for (const ix of d.data.indexes)
            out.push({
              id: `${iid}/${ix.name}`,
              level: 4,
              kind: "index",
              label: ix.name,
              meta: [
                ix.primary ? "primary" : ix.unique ? "unique" : "",
                ix.columns.map((c) => (c.name ? c.name + (c.sub_part ? `(${c.sub_part})` : "") : c.expression || "?")).join(", "),
              ]
                .filter(Boolean)
                .join(" · "),
              expandable: false,
              expanded: false,
              schema: s.name,
              table: t.name,
              insert: quoteName(ix.name),
            });
      }
      if (loaded?.data?.truncated)
        out.push(status(`${sid}/truncated`, 2, `Showing the first ${tableList.length.toLocaleString()} tables`, "muted"));
      if (loaded?.data?.next_after)
        out.push({ ...status(`${sid}/more`, 2, loaded.loading ? "Loading tables…" : "Load more", "muted"),
          kind: "more", schema: s.name });
    }
    return out;
  }, [catalog.catalog, tables, details, open, filter, key]);

  function loadMore(node: TreeNode) {
    if (!node.schema) return;
    const page = tables[tablesKey(key, node.schema, filter)];
    if (!page?.loading && page?.data?.next_after) void loadTables(node.schema, filter, page.data.next_after);
  }

  const focused = nodes.find((n) => n.id === focus) ? focus : nodes[0]?.id || "";
  useEffect(() => {
    if (!menu) return;
    const close = (e: Event) => {
      if (!menuRef.current?.contains(e.target as globalThis.Node)) setMenu(null);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menu]);
  useFocusScope(menuRef, {
    open: !!menu, onClose: () => setMenu(null), menu: true,
    returnFocus: () => menu?.opener,
  });
  // A menu's actions always belong to the connection and tree that opened it.
  useEffect(() => setMenu(null), [key, filter]);

  function moveTo(id: string) {
    setFocus(id);
    window.setTimeout(() => tree.current?.querySelector<HTMLElement>(`[data-node="${CSS.escape(id)}"]`)?.focus());
  }
  function onKey(e: KeyboardEvent<HTMLDivElement>, node: TreeNode, i: number) {
    if (e.target !== e.currentTarget && e.key !== "ContextMenu" && !(e.shiftKey && e.key === "F10")) return;
    const parent = () => {
      for (let j = i - 1; j >= 0; j--) if (nodes[j].level < node.level) return nodes[j];
      return null;
    };
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = nodes[Math.max(0, Math.min(nodes.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next) moveTo(next.id);
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      moveTo(nodes[e.key === "Home" ? 0 : nodes.length - 1].id);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (node.expandable && !node.expanded) toggle(node, true);
      else if (node.expanded && nodes[i + 1]) moveTo(nodes[i + 1].id);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (node.expandable && node.expanded) toggle(node, false);
      else {
        const p = parent();
        if (p) moveTo(p.id);
      }
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (node.expandable) toggle(node);
      else if (node.kind === "more") loadMore(node);
      else if (node.insert) onInsert(node.insert);
    } else if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
      e.preventDefault();
      const box = (e.currentTarget as HTMLElement).getBoundingClientRect();
      if (node.insert) setMenu({ x: box.left + 24, y: box.bottom, node, opener: e.target as HTMLElement });
    }
  }

  function act(fn: () => void) {
    setMenu(null);
    fn();
  }
  const menuNode = menu?.node;
  const qualified = menuNode?.schema && menuNode.table ? `${quoteName(menuNode.schema)}.${quoteName(menuNode.table)}` : "";
  const reading = enabled && catalog.loading;
  const readingSince = useSince(reading);
  return (
    <aside className="schema-browser" aria-label="Schema browser" style={width ? { width } : undefined} ref={browser}>
      <div className="browser-head">
        <h2>Schemas</h2>
        <IconButton icon="refresh" label={reading ? "Reading schemas…" : "Reload schemas"} disabled={!enabled}
          className={reading ? "is-busy" : undefined} aria-busy={reading || undefined} aria-disabled={reading || undefined}
          onClick={() => { if (!reading) refresh(); }} />
        <IconButton icon="close" label="Hide the schema browser" onClick={onClose} />
      </div>
      {/* Always mounted (above the gate and the tree), so reading and its
          failure are announced; the seconds show after 1 s. */}
      <ActionStatus className="browser-status" state={{
        phase: reading ? "pending" : enabled && catalog.error ? "failed" : "idle",
        message: reading ? `Reading the schemas of ${label}…` : catalog.error || "", startedAt: readingSince,
      }} onRetry={enabled && catalog.error ? refresh : undefined} />
      {!enabled ? (
        <div className="browser-gate">
          <p>Read the schemas of {label}.</p>
          <button type="button" className="button small" onClick={() => {
            setAsked(new Set(asked).add(key));
            // The gate goes away: focus moves to the filter, not to the page.
            window.setTimeout(() => browser.current?.querySelector<HTMLInputElement>(".browser-filter input")?.focus());
          }}>
            Load schemas
          </button>
        </div>
      ) : (
        <>
          <div className="browser-filter">
            <SearchField label="Table name prefix" value={filter} onChange={setFilter} placeholder="Table name starts with" />
          </div>
          <div className="browser-tree" role="tree" aria-label={`Schemas on ${label}`} ref={tree}>
            {nodes.map((n, i) => (
              <div
                key={n.id}
                role="treeitem"
                data-node={n.id}
                aria-level={n.level}
                aria-expanded={n.expandable ? n.expanded : undefined}
                aria-selected={n.id === focused}
                tabIndex={n.id === focused ? 0 : -1}
                className={`tree-row kind-${n.kind}${n.tone ? ` is-${n.tone}` : ""}`}
                style={{ paddingLeft: 6 + (n.level - 1) * 14 }}
                title={n.title || (n.insert ? `${n.label}: double-click to insert` : n.label)}
                onClick={() => {
                  setFocus(n.id);
                  if (n.expandable) toggle(n);
                  else if (n.kind === "more") loadMore(n);
                }}
                // Two clicks toggle a branch twice, so a double-click only inserts.
                onDoubleClick={() => {
                  if (n.insert) onInsert(n.insert);
                }}
                onContextMenu={(e) => {
                  if (!n.insert) return;
                  e.preventDefault();
                  setFocus(n.id);
                  setMenu({ x: e.clientX, y: e.clientY, node: n, opener: e.currentTarget });
                }}
                onKeyDown={(e) => onKey(e, n, i)}
              >
                <span className="tree-twisty" aria-hidden="true">
                  {n.expandable ? <Icon name="disclosure" size={12} /> : null}
                </span>
                {n.icon && <Icon name={n.icon} size={14} />}
                <span className="tree-label" title={n.label}>{n.label}</span>
                {n.meta && <span className="tree-meta" title={n.meta}>{n.meta}</span>}
                {n.insert && <button type="button" className="icon-button row-more" aria-label={`More actions for ${n.label}`}
                  title="More actions" aria-haspopup="menu" aria-expanded={menu?.node.id === n.id}
                  onDoubleClick={(e) => e.stopPropagation()}
                  onClick={(e) => {
                    e.stopPropagation();
                    setFocus(n.id);
                    const box = e.currentTarget.getBoundingClientRect();
                    setMenu({ x: box.left, y: box.bottom, node: n, opener: e.currentTarget });
                  }}><Icon name="more" /></button>}
              </div>
            ))}
          </div>
        </>
      )}
      <span
        className="browser-resize"
        role="separator"
        aria-orientation="vertical"
        aria-label="Width of the schema browser"
        aria-valuemin={180}
        aria-valuemax={480}
        aria-valuenow={width}
        tabIndex={0}
        onPointerDown={(e) => {
          e.preventDefault();
          const start = e.clientX;
          const from = width;
          const target = e.currentTarget;
          target.setPointerCapture?.(e.pointerId);
          const move = (m: PointerEvent) => onWidth(Math.max(180, Math.min(480, from + m.clientX - start)));
          const up = () => {
            target.removeEventListener("pointermove", move);
            target.removeEventListener("pointerup", up);
            target.removeEventListener("pointercancel", up);
            target.removeEventListener("lostpointercapture", up);
          };
          target.addEventListener("pointermove", move);
          target.addEventListener("pointerup", up);
          target.addEventListener("pointercancel", up);
          target.addEventListener("lostpointercapture", up);
        }}
        onKeyDown={(e) => {
          const step = e.shiftKey ? 48 : 16;
          if (e.key === "ArrowRight") onWidth(Math.min(480, width + step));
          else if (e.key === "ArrowLeft") onWidth(Math.max(180, width - step));
          else return;
          e.preventDefault();
        }}
      />
      {menu && menuNode && (
        <div
          ref={menuRef}
          className="context-menu"
          role="menu"
          aria-label={`Actions for ${menuNode.label}`}
          style={{ left: Math.min(menu.x, window.innerWidth - 240), top: Math.min(menu.y, window.innerHeight - 200) }}
          tabIndex={-1}
        >
          <button type="button" role="menuitem" onClick={() => act(() => onInsert(menuNode.insert || ""))}>
            Insert name
          </button>
          {(menuNode.kind === "table" || menuNode.kind === "view") && (
            <>
              <button type="button" role="menuitem" onClick={() => act(() => onQuery(`SELECT * FROM ${qualified} LIMIT 100`))}>
                SELECT * … LIMIT 100
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => act(() => onQuery(`SHOW CREATE ${menuNode.kind === "view" ? "VIEW" : "TABLE"} ${qualified}`))}
              >
                Show CREATE {menuNode.kind === "view" ? "VIEW" : "TABLE"}
              </button>
              {onOpenData && menuNode.kind === "table" && <button type="button" role="menuitem"
                onClick={() => act(() => onOpenData(menuNode.schema!, menuNode.table!, key))}>Edit table data…</button>}
              {onOpenTable && menuNode.kind === "table" && (
                <>
                  <button type="button" role="menuitem" onClick={() => act(() => onOpenTable("indexes", menuNode.schema!, menuNode.table!, key))}>
                    Open in Indexes
                  </button>
                  <button type="button" role="menuitem" onClick={() => act(() => onOpenTable("tables", menuNode.schema!, menuNode.table!, key))}>
                    Open in Tables
                  </button>
                </>
              )}
            </>
          )}
        </div>
      )}
    </aside>
  );
}

function status(id: string, level: number, label: string, tone: "error" | "muted"): TreeNode {
  return { id, level, kind: "status", label, expandable: false, expanded: false, tone, title: label };
}
