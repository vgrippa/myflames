// The Query editor's schema browser: schemas, their tables and views, and a
// table's columns and indexes, read on demand from the connection in use
// (/api/live/schemas, shared with the Server pickers, then /api/live/catalog).
// Each schema also carries Routines, Triggers and Events groups, loaded when a
// group opens (catalog level "objects"). The visible pane reads the schema
// list once per connection; expanded nodes read their details on demand. A
// switch never shows another server's names.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api, type CatalogEvent, type CatalogObjects, type CatalogRoutine, type CatalogTable, type CatalogTables, type CatalogTrigger, type Connection } from "./api";
import { connectionKey, connectionLabel } from "./binding";
import { Icon, type IconName } from "./components";
import { ActionStatus, IconButton, SearchField, useFocusScope, useSince } from "./kit";
import { useImportCsvGate, type ImportCsvTarget } from "./importCsvApi";
import { useSchemaCatalog } from "./PerfShared";
import { quoteName, showCreateSql } from "./sqlHighlight";

/** Ask the browser to open a schema and focus a table or view in it (the
 * command palette's object search); n makes a repeated target a new ask. */
export type SchemaReveal = { connection: string; schema: string; table: string | null; n: number };

type Loaded<T> = { loading: boolean; error: string; data: T | null };
type ObjectGroup = "routines" | "triggers" | "events";
type TreeNode = {
  id: string;
  level: number;
  kind: "schema" | "table" | "view" | "folder" | "column" | "index" | "routine" | "trigger" | "event" | "status" | "more";
  label: string;
  meta?: string;
  icon?: IconName;
  expandable: boolean;
  expanded: boolean;
  schema?: string;
  table?: string;
  /** A group folder (routines, triggers, events) under a schema. */
  group?: ObjectGroup;
  /** A routine, trigger or event leaf: its name and kind. */
  object?: string;
  objectKind?: "procedure" | "function" | "trigger" | "event";
  /** Text inserted into the editor on double-click. */
  insert?: string;
  /** A status line (loading, error, empty) instead of an object. */
  tone?: "error" | "muted";
  /** A system database (mysql, sys, information_schema, performance_schema). */
  system?: boolean;
  title?: string;
};
type Menu = { x: number; y: number; node: TreeNode; opener: HTMLElement } | null;

const tablesKey = (key: string, schema: string, prefix: string) => JSON.stringify([key, schema, prefix]);
const tableKey = (key: string, schema: string, table: string) => `${key}\u0000${schema}\u0000${table}`;
const groupsKey = (key: string, schema: string, group: string) => `${key}\u0000${schema}\u0000${group}`;
/** The group folders under each schema, in order (icon reused per meaning). */
const OBJECT_GROUPS: [ObjectGroup, string, IconName][] = [
  ["routines", "Routines", "query"],
  ["triggers", "Triggers", "run"],
  ["events", "Events", "history"],
];
const SHOW_CREATE_KIND = { procedure: "PROCEDURE", function: "FUNCTION", trigger: "TRIGGER", event: "EVENT" } as const;

export function SchemaBrowser({
  connection,
  visible,
  width,
  onWidth,
  onClose,
  onInsert,
  onQuery,
  onOpenTable,
  onOpenData,
  onImportCsv,
  reveal,
}: {
  connection: Connection;
  visible: boolean;
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
  /** Open Import CSV with the target filled in (from: the connection key the name was read from). */
  onImportCsv?: (target: ImportCsvTarget, from: string) => void;
  /** Open a schema and focus a table or view (the palette's object search). */
  reveal?: SchemaReveal | null;
}) {
  const key = connectionKey(connection);
  const label = connectionLabel(connection);
  const [asked, setAsked] = useState<Set<string>>(() => new Set());
  const connected = !!(connection.host || connection.profile_id);
  const enabled = connected && asked.has(key);
  useEffect(() => {
    if (visible && connected) setAsked(previous => previous.has(key) ? previous : new Set(previous).add(key));
  }, [visible, connected, key]);
  const catalog = useSchemaCatalog(connection, enabled && visible);
  const [tables, setTables] = useState<Record<string, Loaded<CatalogTables>>>({});
  const [details, setDetails] = useState<Record<string, Loaded<CatalogTable>>>({});
  const [groups, setGroups] = useState<Record<string, Loaded<CatalogObjects>>>({});
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
  const groupRequests = useRef(new Map<string, AbortController>());

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
    if (!enabled || !visible) return;
    const timer = window.setTimeout(() => {
      for (const s of catalog.catalog?.schemas || []) {
        if (open.has(`s:${s.name}`) && !pagesRef.current[tablesKey(key, s.name, filter)]?.data)
          void loadTablesRef.current(s.name, filter);
      }
    }, 200);
    return () => window.clearTimeout(timer);
  }, [key, enabled, visible, open, filter, catalog.catalog]);
  useEffect(() => {
    const active = requests.current;
    const activeDetails = detailRequests.current;
    const activeGroups = groupRequests.current;
    return () => {
      for (const controller of activeGroups.values()) controller.abort();
      activeGroups.clear();
      setGroups((items) => Object.fromEntries(Object.entries(items).filter(([, item]) => !item.loading)));
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
  /** One group's routines, triggers or events, read when the folder opens. */
  async function loadGroup(schema: string, group: ObjectGroup) {
    const k = groupsKey(key, schema, group);
    if (groupRequests.current.has(k)) return;
    const controller = new AbortController();
    groupRequests.current.set(k, controller);
    setGroups((g) => ({ ...g, [k]: { loading: true, error: "", data: g[k]?.data || null } }));
    try {
      const data = await api<CatalogObjects>("live/catalog", { connection: connectionRef.current, schema, group }, controller.signal);
      if (controller.signal.aborted || connectionKey(connectionRef.current) !== key) return;
      setGroups((g) => ({ ...g, [k]: { loading: false, error: "", data } }));
    } catch (e) {
      if (controller.signal.aborted || connectionKey(connectionRef.current) !== key) return;
      setGroups((g) => ({ ...g, [k]: { loading: false, error: (e as Error).message, data: null } }));
    } finally {
      if (groupRequests.current.get(k) === controller) groupRequests.current.delete(k);
    }
  }
  // Expansion survives connection switches. Read the new connection's
  // expanded groups even though no folder toggle took place on that server.
  const loadGroupRef = useRef(loadGroup);
  loadGroupRef.current = loadGroup;
  useEffect(() => {
    if (!enabled || !visible) return;
    for (const s of catalog.catalog?.schemas || []) {
      if (!open.has(`s:${s.name}`)) continue;
      for (const [group] of OBJECT_GROUPS) {
        if (open.has(`s:${s.name}/${group}`) && !groups[groupsKey(key, s.name, group)])
          void loadGroupRef.current(s.name, group);
      }
    }
  }, [key, enabled, visible, open, catalog.catalog, groups]);
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
    if (node.kind === "folder" && node.group && node.schema &&
        !groups[groupsKey(key, node.schema, node.group)]?.data)
      void loadGroup(node.schema, node.group);
  }
  function refresh() {
    for (const controller of groupRequests.current.values()) controller.abort();
    groupRequests.current.clear();
    for (const controller of detailRequests.current.values()) controller.abort();
    detailRequests.current.clear();
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear();
    setTables({});
    setGroups((g) => Object.fromEntries(Object.entries(g).filter(([k]) => !k.startsWith(key + "\u0000"))));
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
        system: s.system,
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
      // Routines, Triggers and Events: folders under the database, read when
      // one opens (loadGroup), each bounded to a page of names.
      for (const [group, groupLabel, groupIcon] of OBJECT_GROUPS) {
        const gid = `${sid}/${group}`;
        const g = groups[groupsKey(key, s.name, group)];
        const gOpen = open.has(gid);
        const count = g?.data ? `${g.data.objects.length}${g.data.truncated ? "+" : ""}` : "";
        out.push({
          id: gid, level: 2, kind: "folder", label: groupLabel, meta: count, icon: groupIcon,
          expandable: true, expanded: gOpen, schema: s.name, group,
          title: g?.data?.truncated ? `${groupLabel} in ${s.name}: the first ${g.data.limit} are shown` : undefined,
        });
        if (!gOpen) continue;
        if (!g || (g.loading && !g.data)) {
          out.push(status(`${gid}/loading`, 3, `Loading ${groupLabel.toLowerCase()}…`, "muted"));
          continue;
        }
        if (g.error) {
          out.push(status(`${gid}/error`, 3, g.error, "error"));
          continue;
        }
        if (!g.data!.objects.length) {
          out.push(status(`${gid}/empty`, 3, `No ${groupLabel.toLowerCase()}`, "muted"));
          continue;
        }
        if (group === "routines")
          for (const o of g.data!.objects as CatalogRoutine[])
            out.push({
              id: `${gid}/${o.kind}:${o.name}`, level: 3, kind: "routine", label: o.name, meta: o.kind,
              icon: "query", expandable: false, expanded: false, schema: s.name, object: o.name, objectKind: o.kind,
              insert: `${quoteName(s.name)}.${quoteName(o.name)}`,
              title: `${o.kind} ${s.name}.${o.name}`,
            });
        else if (group === "triggers")
          for (const o of g.data!.objects as CatalogTrigger[])
            out.push({
              id: `${gid}/${o.name}`, level: 3, kind: "trigger", label: o.name,
              meta: [o.timing, o.event, o.table ? `on ${o.table}` : ""].filter(Boolean).join(" "),
              icon: "run", expandable: false, expanded: false, schema: s.name, object: o.name, objectKind: "trigger",
              insert: `${quoteName(s.name)}.${quoteName(o.name)}`,
              title: `${o.timing} ${o.event} trigger on ${s.name}.${o.table || "?"}: double-click to insert`,
            });
        else
          for (const o of g.data!.objects as CatalogEvent[])
            out.push({
              id: `${gid}/${o.name}`, level: 3, kind: "event", label: o.name, meta: o.status || undefined,
              icon: "history", expandable: false, expanded: false, schema: s.name, object: o.name, objectKind: "event",
              insert: `${quoteName(s.name)}.${quoteName(o.name)}`,
              title: `event ${s.name}.${o.name}${o.status ? `, ${o.status}` : ""}: double-click to insert`,
            });
      }
      if (loaded?.data?.truncated)
        out.push(status(`${sid}/truncated`, 2, `Showing the first ${tableList.length.toLocaleString()} tables`, "muted"));
      if (loaded?.data?.next_after)
        out.push({ ...status(`${sid}/more`, 2, loaded.loading ? "Loading tables…" : "Load more", "muted"),
          kind: "more", schema: s.name });
    }
    return out;
  }, [catalog.catalog, tables, details, groups, open, filter, key]);

  function loadMore(node: TreeNode) {
    if (!node.schema) return;
    const page = tables[tablesKey(key, node.schema, filter)];
    if (!page?.loading && page?.data?.next_after) void loadTables(node.schema, filter, page.data.next_after);
  }

  const focused = nodes.find((n) => n.id === focus) ? focus : nodes[0]?.id || "";
  // A full-name literal prefix makes a target beyond the unfiltered first
  // page reachable with one bounded catalog read. Never scan every page.
  const revealDone = useRef("");
  const revealId = reveal ? JSON.stringify([reveal.connection, reveal.n, reveal.schema, reveal.table]) : "";
  const [revealError, setRevealError] = useState<{ id: string; message: string } | null>(null);
  useEffect(() => {
    if (!reveal || reveal.connection !== key || !visible || !enabled || revealId === revealDone.current) return;
    setRevealError(null);
    setFilter(reveal.table || "");
    setOpen((previous) => new Set(previous).add(`s:${reveal.schema}`));
  }, [reveal, revealId, key, visible, enabled]);
  useEffect(() => {
    if (!reveal || reveal.connection !== key || !visible || !enabled || revealId === revealDone.current ||
        filter !== (reveal.table || "") || !open.has(`s:${reveal.schema}`)) return;
    const wanted = reveal.table
      ? nodes.find((n) => (n.kind === "table" || n.kind === "view") && n.schema === reveal.schema && n.table === reveal.table)
      : nodes.find((n) => n.kind === "schema" && n.schema === reveal.schema);
    if (wanted) {
      revealDone.current = revealId;
      moveTo(wanted.id);
      return;
    }
    if (catalog.loading || !catalog.catalog) return;
    const schemaExists = catalog.catalog.schemas.some((s) => s.name === reveal.schema);
    const page = tables[tablesKey(key, reveal.schema, filter)];
    if (schemaExists && (!page || page.loading)) return;
    revealDone.current = revealId;
    setRevealError({ id: revealId, message: page?.error || (page?.data?.truncated
      ? "The object is outside this filtered page; narrow the prefix or use Load more."
      : "The object is no longer visible on this connection; search again or reload schemas.") });
  }, [nodes, reveal, revealId, key, visible, enabled, filter, open, tables, catalog.catalog, catalog.loading]);
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
  // Import CSV: off with its reason on a Read-only connection, while the
  // write policy is unknown, or when SHOW GRANTS proves a privilege missing.
  const importExisting = useImportCsvGate("existing");
  const importNew = useImportCsvGate("new");
  const importItem = (text: string, blocked: string, target: ImportCsvTarget) => (
    <button type="button" role="menuitem" className={blocked ? "has-hint" : undefined} aria-disabled={blocked ? true : undefined}
      title={blocked || undefined} data-action="import-csv"
      onClick={() => { if (!blocked && onImportCsv) act(() => onImportCsv(target, key)); }}>
      {blocked ? <span className="context-menu-text"><span>{text}</span><small className="context-menu-hint">{blocked}</small></span> : text}
    </button>
  );
  return (
    <aside className="schema-browser" aria-label="Schema browser" style={width ? { width } : undefined} ref={browser}>
      <div className="browser-head">
        <h2>Schemas</h2>
        {enabled && <IconButton icon="refresh" label={reading ? "Reading schemas…" : "Reload schemas"} disabled={!enabled}
          className={reading ? "is-busy" : undefined} aria-busy={reading || undefined} aria-disabled={reading || undefined}
          onClick={() => { if (!reading) refresh(); }} />}
        <IconButton icon="close" label="Hide the schema browser" onClick={onClose} />
      </div>
      {/* Always mounted (above the gate and the tree), so reading and its
          failure are announced; the seconds show after 1 s. */}
      <ActionStatus className="browser-status" state={{
        phase: reading ? "pending" : enabled && catalog.error ? "failed" : "idle",
        message: reading ? `Reading the schemas of ${label}…` : catalog.error || "", startedAt: readingSince,
      }} onRetry={enabled && catalog.error ? refresh : undefined} />
      {!enabled ? (
        // With a connection the list starts reading once the pane is shown
        // (the effect above), so there is no button to press here.
        <div className="browser-gate">
          <p role="status">{connected ? `Reading the schemas of ${label}…` : "Choose a connection to see its schemas."}</p>
        </div>
      ) : (
        <>
          <div className="browser-filter">
            <SearchField label="Table name prefix" value={filter} onChange={(value) => {
              revealDone.current = revealId;
              setRevealError(null);
              setFilter(value);
            }} placeholder="Table name starts with" />
          </div>
          {reveal?.connection === key && revealError?.id === revealId &&
            <ActionStatus className="browser-status" state={{ phase: "failed", message: revealError.message, startedAt: 0 }} />}
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
                <span className="tree-label" title={n.label}><bdi>{n.label}</bdi></span>
                {n.meta && <span className="tree-meta" title={n.meta}><bdi>{n.meta}</bdi></span>}
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
          {onImportCsv && menuNode.kind === "schema" && !menuNode.system &&
            importItem("Import CSV into a new table…", importNew, { mode: "new", schema: menuNode.schema! })}
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
              {onImportCsv && menuNode.kind === "table" &&
                importItem("Import CSV…", importExisting, { mode: "existing", schema: menuNode.schema!, table: menuNode.table! })}
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
          {(menuNode.kind === "routine" || menuNode.kind === "trigger" || menuNode.kind === "event") && menuNode.object && menuNode.objectKind && (
            <button type="button" role="menuitem"
              onClick={() => act(() => onQuery(showCreateSql(menuNode.objectKind!, menuNode.schema!, menuNode.object!)))}>
              Show CREATE {SHOW_CREATE_KIND[menuNode.objectKind]}
            </button>
          )}
        </div>
      )}
    </aside>
  );
}

function status(id: string, level: number, label: string, tone: "error" | "muted"): TreeNode {
  return { id, level, kind: "status", label, expandable: false, expanded: false, tone, title: label };
}
