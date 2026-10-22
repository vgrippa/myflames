import { useEffect, useMemo, useRef, useState } from "react";
import { api, type CatalogTable, type CatalogTables, type Connection, type SchemaCatalog } from "./api";
import { connectionKey } from "./binding";
import { columnTargets, completionContext, suggestions, type Suggestion } from "./sqlCompletion";

type CacheEntry = { until: number; value?: unknown; failed?: boolean };
type CompletionResult = { key: string; items: Suggestion[]; loading: boolean; note: string; owner?: object };
/** Cache only metadata, in page memory, never credentials or workspace storage. */
export function useSqlCompletion(connection: Connection | undefined, sql: string, start: number, end: number,
  enabled: boolean, manual: boolean, scope: string, backslashEscapes = true, request = 0) {
  const identity = connection ? JSON.stringify([connectionKey(connection), connection.database]) : "";
  const owner = useRef({ identity, password: connection?.password, cache: new Map<string, CacheEntry>() });
  // Invalidate synchronously so even a render before effect cleanup cannot show
  // an old identity's suggestions. Password changes also discard cached reads.
  if (owner.current.identity !== identity || owner.current.password !== connection?.password)
    owner.current = { identity, password: connection?.password, cache: new Map() };
  const cacheOwner = owner.current;
  const context = useMemo(() => completionContext(sql, start, end, backslashEscapes), [sql, start, end, backslashEscapes]);
  const wanted = enabled && !!context && (manual || context.prefix.length >= 2 || context.qualifiers.length > 0);
  const key = wanted ? JSON.stringify([identity, scope, sql, start, end, wanted, manual, backslashEscapes, request]) : "";
  const [result, setResult] = useState<CompletionResult>({ key: "", items: [], loading: false, note: "" });
  const latest = useRef(key); latest.current = key;
  useEffect(() => {
    if (!wanted || !context) return;
    const controller = new AbortController();
    let active = true;
    const valid = () => active && !controller.signal.aborted && latest.current === key && owner.current === cacheOwner;
    const publish = (items: Suggestion[], loading: boolean, note: string) => {
      if (valid()) setResult({ key, items, loading, note, owner: cacheOwner });
    };
    const local = suggestions(context, {});
    publish(local, !!connection, "");
    const budget = window.setTimeout(() => {
      publish(local, false, "Catalog suggestions timed out. Try Ctrl+Space again.");
      controller.abort();
    }, 8000);
    const timer = window.setTimeout(async () => {
      if (!connection || !valid()) { clearTimeout(budget); return; }
      let unavailable = false, limited = false;
      const read = async <T,>(path: string, args: object): Promise<T | null> => {
        const cacheKey = JSON.stringify([path, args]);
        const old = cacheOwner.cache.get(cacheKey);
        if (old && old.until > Date.now() && !(manual && old.failed)) {
          if (old.failed) unavailable = true;
          return old.failed ? null : old.value as T;
        }
        try {
          const value = await api<T>(path, { connection, ...args }, controller.signal);
          if (!valid()) return null;
          if (cacheOwner.cache.size >= 32) cacheOwner.cache.delete(cacheOwner.cache.keys().next().value!);
          cacheOwner.cache.set(cacheKey, { until: Date.now() + 30000, value });
          return value;
        } catch {
          if (valid()) {
            unavailable = true;
            // Avoid hammering a denied or unavailable catalog while typing.
            if (cacheOwner.cache.size >= 32) cacheOwner.cache.delete(cacheOwner.cache.keys().next().value!);
            cacheOwner.cache.set(cacheKey, { until: Date.now() + 10000, failed: true });
          }
          return null;
        }
      };
      const targets = columnTargets(context, connection.database || "");
      const columns: { name: string; type: string; table: typeof targets[number] }[] = [];
      const tableRows: { name: string; schema: string }[] = [];
      let schemaNames: string[] = [];
      const schemas = context.qualifiers.length < 2 && !targets.length
        ? await read<SchemaCatalog>("live/schemas", {}) : null;
      if (!valid()) return;
      schemaNames = schemas?.schemas.map(s => s.name) || [];
      let tableSchema = "";
      if (context.place === "table") tableSchema = context.qualifiers[0] || connection.database || "";
      else if (context.place === "expression" && context.qualifiers.length === 1 && !targets.length && schemaNames.includes(context.qualifiers[0]))
        tableSchema = context.qualifiers[0];
      await Promise.all([
        ...(tableSchema ? [read<CatalogTables>("live/catalog", { schema: tableSchema, prefix: context.prefix }).then(data => {
          if (data) { limited = data.truncated; tableRows.push(...data.tables.map(t => ({ name: t.name, schema: tableSchema }))); }
        })] : []),
        ...targets.map(table => read<CatalogTable>("live/catalog", { schema: table.schema, table: table.table }).then(data => {
          if (data) columns.push(...data.columns.map(c => ({ name: c.name, type: c.type, table })));
        })),
      ]);
      columns.sort((a, b) => targets.indexOf(a.table) - targets.indexOf(b.table));
      clearTimeout(budget);
      publish(suggestions(context, { schemas: schemaNames, tables: tableRows, columns }), false,
        unavailable ? "Some catalog suggestions are unavailable." : limited ? "More tables match. Keep typing to narrow the list." : "");
    }, manual ? 0 : 220);
    return () => { active = false; clearTimeout(timer); clearTimeout(budget); controller.abort(); };
  }, [key, cacheOwner]);
  return {
    context,
    items: wanted && result.owner === cacheOwner && result.key === key ? result.items : [],
    loading: wanted && result.owner === cacheOwner && result.key === key && result.loading,
    note: wanted && result.owner === cacheOwner && result.key === key ? result.note : "",
    key,
  };
}
