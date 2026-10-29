// SYNTHETIC replies for the Parameters > Versions browser suite and its
// screenshots, in the shapes of PLAN's parameter-version-catalog-1.0,
// parameter-version-compare-1.0 and parameter-history-1.0. The names come
// from myflames/mysql_sysvars.py; the releases, facts and changes are made up
// (a few follow well-known changes so the screens look real). Never use this
// file as a source of MySQL or MariaDB facts: the real catalog is generated
// from running servers (scripts/gen_sysvar_history.py).
const FIELDS = ["scope", "type", "dynamic", "default", "default_note", "min", "max", "values", "deprecated", "replacement", "alias_of", "plugin"];
const KINDS = ["added", "removed", "deprecated", "undeprecated", "default", "dynamic", "scope", "type", "values", "range"];

function hash(text) {
  let h = 2166136261;
  for (const c of text) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0; }
  return h;
}
const cmp = (a, b) => { const x = a.split(".").map(Number), y = b.split(".").map(Number); for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; };
const facts = (o) => Object.fromEntries(FIELDS.map(f => [f, o[f] ?? null]));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function mysqlReleases() {
  const list = [];
  let day = Date.UTC(2018, 3, 8);
  const add = (version, maturity, previous) => { list.push({ version, series: version.split(".").slice(0, 2).join("."), maturity, date: new Date(day).toISOString().slice(0, 10), previous }); day += 86400000 * 80; };
  for (let p = 11; p <= 43; p++) {
    if (p === 29) continue;
    add(`8.0.${p}`, "GA", p === 11 ? null : `8.0.${p === 30 ? 28 : p - 1}`);
    if (p === 34) add("8.1.0", "INNOVATION", "8.0.34");
    if (p === 35) add("8.2.0", "INNOVATION", "8.1.0");
    if (p === 36) add("8.3.0", "INNOVATION", "8.2.0");
    if (p === 37) { add("8.4.0", "LTS", "8.3.0"); }
    if (p >= 38 && p <= 43) add(`8.4.${p - 37}`, "LTS", `8.4.${p - 38}`);
    if (p === 39) add("9.0.1", "INNOVATION", "8.4.1");
    if (p === 40) add("9.1.0", "INNOVATION", "9.0.1");
    if (p === 41) add("9.2.0", "INNOVATION", "9.1.0");
    if (p === 42) add("9.3.0", "INNOVATION", "9.2.0");
    if (p === 43) add("9.4.0", "INNOVATION", "9.3.0");
  }
  return list.sort((a, b) => cmp(a.version, b.version));
}
function mariadbReleases() {
  const list = [];
  let day = Date.UTC(2023, 1, 1);
  const series = [["10.6", 12, 21], ["10.11", 6, 11], ["11.4", 2, 5], ["11.8", 1, 3]];
  let last = null;
  for (const [s, a, b] of series) for (let p = a; p <= b; p++) {
    const version = `${s}.${p}`;
    list.push({ version, series: s, maturity: "stable", date: new Date(day).toISOString().slice(0, 10), previous: last });
    last = version; day += 86400000 * 40;
  }
  return list;
}

/** Names, scope, type and dynamic from myflames/mysql_sysvars.py text. */
export function parseSysvars(text) {
  const values = new Map();
  for (const m of text.matchAll(/^\s+'([a-z0-9_]+)': \(((?:'[^']*',?\s*)+)\),$/gm)) {
    if (!/^\s+'[a-z0-9_]+': \('(global|session|both)'/.test(m[0])) values.set(m[1], [...m[2].matchAll(/'([^']*)'/g)].map(v => v[1]));
  }
  const out = [];
  for (const m of text.matchAll(/^\s+'([a-z0-9_]+)': \('(global|session|both)', (?:'(\w+)'|None), (?:'([^']*)'|None)\),$/gm)) {
    if (/^(performance_schema_|ndb|mysqlx_|debug)/.test(m[1])) continue;
    out.push({ name: m[1], scope: m[2], type: m[3] || "string", dynamic: !(m[4] || "").split(" ").includes("ro"), values: values.get(m[1]) || null });
  }
  return out;
}

function baseFacts(v, r) {
  const t = v.type;
  const dflt = t === "boolean" ? (r % 3 ? "OFF" : "ON") : t === "integer" ? String([0, 1, 8, 64, 100, 200, 1024, 4096, 65536, 8388608, 16777216, 134217728][r % 12])
    : t === "double" ? "10.000000" : (t === "enum" || t === "set") && v.values ? v.values[r % v.values.length] : t === "flagset" ? "index_merge=on" : r % 4 ? "" : "/var/lib/mysql/";
  const numeric = t === "integer" || t === "double";
  return facts({ scope: v.scope, type: t, dynamic: v.dynamic, default: dflt, min: numeric ? "0" : null,
    max: numeric ? (r % 2 ? "4294967295" : "18446744073709551615") : null, values: t === "enum" || t === "set" ? v.values : null,
    plugin: v.name.startsWith("group_replication_") ? "group_replication" : v.name.startsWith("rpl_semi_sync_") ? "rpl_semi_sync_source" : null });
}

/** Build the catalog: runs of [first release index, facts or null] per name. */
function buildEngine(engine, product, releases, vars, specials, skipped) {
  const index = new Map(releases.map((r, i) => [r.version, i]));
  const runs = new Map();
  for (const v of vars) {
    const r = hash(v.name);
    const f0 = baseFacts(v, r);
    const run = [[0, f0]];
    // About half the names change once or twice somewhere along the releases.
    let cur = f0;
    for (const salt of ["a", "b"]) {
      const h = hash(v.name + salt);
      if (h % 100 >= (salt === "a" ? 40 : 12)) continue;
      const at = 1 + (h >>> 8) % (releases.length - 1);
      if (run.some(([i]) => i >= at)) continue;
      const next = { ...cur };
      const pick = (h >>> 4) % 100;
      if (pick < 55 && cur.type !== "string") {
        next.default = cur.type === "boolean" ? (cur.default === "ON" ? "OFF" : "ON") : cur.type === "integer" ? String(Number(cur.default || 0) * 2 + 1) : cur.values ? cur.values[(cur.values.indexOf(cur.default) + 1) % cur.values.length] : cur.default;
        if (pick < 6) { next.default = null; next.default_note = "host"; }
        else if (pick < 16 && cur.max) next.max = cur.max === "4294967295" ? "18446744073709551615" : "4294967295";
      } else if (pick < 70 && cur.max) next.max = cur.max === "4294967295" ? "18446744073709551615" : "4294967295";
      else if (pick < 80) next.dynamic = !cur.dynamic;
      else if (pick < 90 && cur.values) next.values = [...cur.values, "AUTO"];
      else if (!cur.deprecated) next.deprecated = ["source"];
      else continue;
      run.push([at, next]); cur = next;
    }
    runs.set(v.name, run);
  }
  for (const [name, list] of Object.entries(specials)) runs.set(name, list.map(([version, f]) => [index.get(version), f && facts(f)]));
  const factsAt = (name, version) => {
    const run = runs.get(name); const i = index.get(version);
    if (!run || i === undefined) return null;
    let found = null;
    for (const [at, f] of run) if (at <= i) found = f;
    return found;
  };
  return { engine, product, releases, runs, index, factsAt, skipped };
}

const value = (kind, f) => {
  if (kind === "added" || kind === "removed") return !!f;
  if (!f) return null;
  if (kind === "deprecated" || kind === "undeprecated") return !!f.deprecated;
  if (kind === "default") return [f.default, f.default_note];
  if (kind === "range") return [f.min, f.max];
  return f[kind];
};
function kindsBetween(a, b) {
  if (!a && !b) return [];
  if (!a) return ["added"];
  if (!b) return ["removed"];
  const out = [];
  if (!a.deprecated && b.deprecated) out.push("deprecated");
  if (a.deprecated && !b.deprecated) out.push("undeprecated");
  // An unknown default on either side is never a default change.
  const known = f => f.default_note !== "unknown" && !(f.default === null && f.default_note === null);
  if (known(a) && known(b) && !same(value("default", a), value("default", b))) out.push("default");
  for (const k of ["dynamic", "scope", "type", "values"]) if (!same(a[k], b[k])) out.push(k);
  if (!same(value("range", a), value("range", b))) out.push("range");
  return KINDS.filter(k => out.includes(k));
}
const show = (f, kind) => kind === "default" ? (f.default_note === "host" ? "a host-dependent value" : f.default === null ? "NULL" : f.default === "" ? "an empty string" : f.default)
  : kind === "range" ? `${f.min} to ${f.max}` : kind === "values" ? (f.values || []).join(", ") : kind === "dynamic" ? (f.dynamic ? "dynamic" : "static") : String(f[kind]);
function sentenceFor(kind, a, b, version) {
  if (kind === "added") return version ? `Added in ${version}.` : "Present in the To release only.";
  if (kind === "removed") return version ? `Removed in ${version}.` : "Present in the From release only.";
  if (kind === "deprecated") return `Deprecated${b.replacement ? `; use ${b.replacement} instead` : ""}.`;
  if (kind === "undeprecated") return "No longer deprecated.";
  if (kind === "dynamic") return `Became ${b.dynamic ? "dynamic" : "static"}.`;
  const label = { default: "The default", scope: "The scope", type: "The data type", values: "The allowed values", range: "The range" }[kind];
  return `${label} changed from ${show(a, kind)} to ${show(b, kind)}.`;
}

export function buildMock(sysvarsText) {
  const vars = parseSysvars(sysvarsText);
  const my = mysqlReleases();
  const g = { scope: "global", type: "integer" };
  const mysqlSpecials = {
    innodb_log_file_size: [["8.0.11", { ...g, dynamic: false, default: "50331648", min: "4194304", max: "18446744073709551615" }],
      ["8.0.30", { ...g, dynamic: false, default: "50331648", min: "4194304", max: "18446744073709551615", deprecated: ["manual", "server", "source"], replacement: "innodb_redo_log_capacity" }]],
    innodb_redo_log_capacity: [["8.0.30", { ...g, dynamic: true, default: "104857600", min: "8388608", max: "137438953472" }],
      ["8.4.0", { ...g, dynamic: true, default: "104857600", min: "8388608", max: "549755813888" }]],
    innodb_log_files_in_group: [["8.0.11", { ...g, dynamic: false, default: "2", min: "2", max: "100" }],
      ["8.0.30", { ...g, dynamic: false, default: "2", min: "2", max: "100", deprecated: ["server", "source"], replacement: "innodb_redo_log_capacity" }]],
    innodb_io_capacity: [["8.0.11", { ...g, dynamic: true, default: "200", min: "100", max: "18446744073709551615" }],
      ["8.4.0", { ...g, dynamic: true, default: "10000", min: "100", max: "18446744073709551615" }]],
    innodb_change_buffering: [["8.0.11", { scope: "global", type: "enum", dynamic: true, default: "all", values: ["none", "inserts", "deletes", "changes", "purges", "all"] }],
      ["8.4.0", { scope: "global", type: "enum", dynamic: true, default: "none", values: ["none", "inserts", "deletes", "changes", "purges", "all"] }]],
    innodb_adaptive_hash_index: [["8.0.11", { scope: "global", type: "boolean", dynamic: true, default: "ON" }],
      ["8.4.0", { scope: "global", type: "boolean", dynamic: true, default: "OFF" }]],
    temptable_max_ram: [["8.0.11", { ...g, dynamic: true, default: "1073741824", min: "2097152", max: "18446744073709551615" }],
      ["8.4.0", { ...g, dynamic: true, default: null, default_note: "host", min: "2097152", max: "18446744073709551615" }]],
    default_authentication_plugin: [["8.0.11", { scope: "global", type: "enum", dynamic: false, default: "caching_sha2_password", values: ["mysql_native_password", "sha256_password", "caching_sha2_password"] }],
      ["8.0.27", { scope: "global", type: "enum", dynamic: false, default: "caching_sha2_password", values: ["mysql_native_password", "sha256_password", "caching_sha2_password"], deprecated: ["manual", "server", "source"], replacement: "authentication_policy" }],
      ["8.4.0", null]],
    expire_logs_days: [["8.0.11", { ...g, dynamic: true, default: "0", min: "0", max: "99" }],
      ["8.0.30", { ...g, dynamic: true, default: "0", min: "0", max: "99", deprecated: ["manual", "server"], replacement: "binlog_expire_logs_seconds" }], ["8.4.0", null]],
    master_info_repository: [["8.0.11", { scope: "global", type: "enum", dynamic: true, default: "TABLE", values: ["FILE", "TABLE"] }],
      ["8.0.23", { scope: "global", type: "enum", dynamic: true, default: "TABLE", values: ["FILE", "TABLE"], deprecated: ["manual", "server", "source"] }], ["8.4.0", null]],
    restrict_fk_on_non_standard_key: [["8.4.0", { scope: "both", type: "boolean", dynamic: true, default: "ON" }]],
    slave_parallel_workers: [["8.0.11", { ...g, dynamic: true, default: "0", min: "0", max: "1024" }],
      ["8.0.26", { ...g, dynamic: true, default: "0", min: "0", max: "1024", deprecated: ["source"], replacement: "replica_parallel_workers", alias_of: "replica_parallel_workers" }],
      ["8.0.27", { ...g, dynamic: true, default: "4", min: "0", max: "1024", deprecated: ["source"], replacement: "replica_parallel_workers", alias_of: "replica_parallel_workers" }]],
    innodb_doublewrite_pages: [["8.0.20", { ...g, dynamic: false, default: "4", min: "1", max: "512" }], ["8.4.0", { ...g, dynamic: false, default: "128", min: "1", max: "512" }]],
    innodb_numa_interleave: [["8.0.11", { scope: "global", type: "boolean", dynamic: false, default: "OFF" }],
      ["8.4.0", { scope: "both", type: "boolean", dynamic: true, default: "ON", deprecated: ["source"] }]],
    replica_parallel_workers: [["8.0.26", { ...g, dynamic: true, default: "0", min: "0", max: "1024" }], ["8.0.27", { ...g, dynamic: true, default: "4", min: "0", max: "1024" }]],
  };
  const mysqlVars = vars.filter(v => !(v.name in mysqlSpecials));
  // Some names exist only in later releases (added) or end early (removed).
  const engines = [
    buildEngine("mysql", "MySQL", my, mysqlVars, mysqlSpecials, [{ version: "8.0.29", reason: "No image or package for this release could be started." }, { version: "9.0.0", reason: "No image or package for this release could be started." }]),
    buildEngine("mariadb", "MariaDB", mariadbReleases(), vars.filter(v => hash(v.name) % 5 === 0).map(v => ({ ...v, name: v.name.replace(/^replica_/, "slave_") })), {
      innodb_log_file_size: [["10.6.12", { ...g, dynamic: true, default: "100663296", min: "1048576", max: "549755813888" }]],
    }, []),
  ];
  // Added late and removed: re-time a share of the MySQL runs.
  const mysql = engines[0];
  for (const [name, run] of mysql.runs) {
    if (name in mysqlSpecials) continue;
    const h = hash(name + "life") % 100;
    if (h < 6) {
      const start = mysql.index.get(["8.0.30", "8.1.0", "8.2.0", "8.3.0", "8.4.0", "9.1.0"][h]);
      const later = run.slice(1).filter(([at]) => at > start);
      run.splice(0, run.length, [start, run[0][1]], ...later);
    } else if (h < 10) {
      const end = mysql.index.get(["8.1.0", "8.2.0", "8.3.0", "8.4.0"][h - 6]);
      const kept = run.filter(([at]) => at < end);
      run.splice(0, run.length, ...kept, [end, null]);
    }
  }
  const byEngine = Object.fromEntries(engines.map(e => [e.engine, e]));
  const chain = (e, version) => { const out = []; let v = version; while (v) { out.push(v); v = e.releases.find(r => r.version === v)?.previous || null; } return out; };

  function catalog() {
    return { schema: "parameter-version-catalog-1.0", engines: engines.map(e => ({
      engine: e.engine, product: e.product, available: true, versions: e.releases.map(({ version, series, maturity, date }) => ({ version, series, maturity, date })),
      skipped: e.skipped, names: [...e.runs.keys()].sort(), generated: { on: "2026-10-28", runtime: "read from running servers in lab containers" },
      notes: e.engine === "mysql" ? ["Variables Percona Server adds are not cataloged."] : [],
    })) };
  }
  function compare(engineName, from, to) {
    const e = byEngine[engineName];
    if (!e || !e.index.has(from) || !e.index.has(to)) { const error = new Error(`${engineName === "mariadb" ? "MariaDB" : "MySQL"} ${!e?.index.has(from) ? from : to} is not in the catalog.`); error.status = 400; throw error; }
    const changes = [];
    let unchanged = 0;
    for (const name of [...e.runs.keys()].sort()) {
      const a = e.factsAt(name, from), b = e.factsAt(name, to);
      if (!a && !b) continue;
      const kinds = kindsBetween(a, b);
      if (!kinds.length) { unchanged += 1; continue; }
      const since = {};
      for (const k of kinds) {
        const want = value(k, b); let earliest = to;
        for (const v of chain(e, to).slice(1)) { if (!same(value(k, e.factsAt(name, v)), want)) break; earliest = v; }
        since[k] = earliest;
      }
      changes.push({ name, kinds, text: kinds.map(k => sentenceFor(k, a, b, null)), from: a, to: b, since, replacement: (b || a)?.replacement || null });
    }
    const counts = Object.fromEntries([...KINDS.map(k => [k, changes.filter(c => c.kinds.includes(k)).length]), ["changed", changes.length], ["unchanged", unchanged]]);
    const direction = cmp(from, to) < 0 ? "upgrade" : cmp(from, to) > 0 ? "downgrade" : "same";
    const summary = direction === "same" ? `${e.product} ${from} compared with itself: no changes.`
      : `${changes.length} parameters differ from ${e.product} ${from} to ${to}: ${counts.added} added, ${counts.removed} removed, ${counts.deprecated} deprecated.`;
    return { schema: "parameter-version-compare-1.0", engine: e.engine, product: e.product, from, to, direction, summary, counts, changes,
      notes: ["Plugin variables are listed only for plugins the lab servers loaded."] };
  }
  function history(engineName, name) {
    const e = byEngine[engineName];
    const base = { schema: "parameter-history-1.0", engine: e.engine, product: e.product, name, build_only: null, default_rule: null, aliases: [], notes: [] };
    if (!e.runs.has(name)) {
      const words = name.toLowerCase().split("_");
      const score = n => { const t = n.split("_"); let p = 0; while (p < n.length && n[p] === name[p]) p += 1; return t.filter(w => words.includes(w)).length * 10 + p; };
      const suggestions = [...e.runs.keys()].map(n => [n, score(n)]).filter(([, s]) => s >= 20).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 6).map(([n]) => n);
      return { ...base, found: false, summary: `${e.product} has no parameter named ${name} in any cataloged release.`, first: null, latest: null, current: null, series: [], events: [], suggestions };
    }
    const present = e.releases.filter(r => e.factsAt(name, r.version));
    const first = present[0]?.version || null, latest = present[present.length - 1]?.version || null;
    const events = [];
    for (const r of e.releases) {
      if (!r.previous) continue;
      const a = e.factsAt(name, r.previous), b = e.factsAt(name, r.version);
      const kinds = kindsBetween(a, b);
      if (kinds.length) events.push({ version: r.version, previous: r.previous, series: r.series, kinds, text: kinds.map(k => sentenceFor(k, a, b, r.version)), before: a, after: b });
    }
    const series = [];
    for (const r of e.releases) {
      let s = series.find(x => x.series === r.series);
      if (!s) series.push(s = { series: r.series, ranges: [] });
      const f = e.factsAt(name, r.version), last = s.ranges[s.ranges.length - 1];
      if (last && same(last.facts, f)) last.to = r.version; else s.ranges.push({ from: r.version, to: r.version, facts: f });
    }
    const firstCataloged = e.releases[0].version;
    const summary = `${name} is in ${e.product} ${first === firstCataloged ? "since the first cataloged release" : `from ${first}`}${latest !== e.releases[e.releases.length - 1].version ? ` to ${latest}` : ""}, with ${events.length} ${events.length === 1 ? "change" : "changes"}.`;
    return { ...base, found: true, summary, first, latest, current: e.factsAt(name, latest), series, events, suggestions: [] };
  }
  return { catalog, compare, history };
}
