// Run with: node test/accounts.mjs (after npm run build). A fixture shell
// exercises provider races and account interactions; server-nav.mjs checks
// the packaged registration. --unit needs no browser or loopback server.
// No existing source or packaged asset is rewritten by this suite.
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build, transformWithEsbuild } from "vite";
import { open, pause } from "./perf-harness.mjs";

const workspace = fileURLToPath(new URL("../", import.meta.url));
const source = name => JSON.stringify(path.join(workspace, "src", name));
// Allows meaningful executable checks when loopback servers are forbidden.
// This uses the real TS modules and mocked fetch; it does not claim UI QA.
if (process.argv.includes("--unit")) {
  const id = "\0account-unit";
  const result = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
    plugins: [{ name: "account-unit", resolveId(value) { if (value === id) return id; }, load(value) {
      if (value === id) return `export { compactChanges, AccountsView } from ${source("Accounts.tsx")};
        export { privilegeResult, privilegeButtonProps } from ${source("privileges.ts")};
        export { observedScopes, planAccount, applyAccount, declineAccount, planAccountGroups, applyAccountGroups, declineAccountGroups, previewAccountPlan, roleLabel } from ${source("accountsApi.ts")};`;
    } }], build: { ssr: true, write: false, rollupOptions: { input: id, output: { inlineDynamicImports: true } } } });
  const temporary = await fs.mkdtemp(path.join(workspace, ".accounts-unit-"));
  try {
    const module = path.join(temporary, "unit.mjs");
    await fs.writeFile(module, result.output.find(f => f.type === "chunk").code);
    const { compactChanges, privilegeResult, privilegeButtonProps, observedScopes, planAccount, applyAccount, declineAccount, planAccountGroups, applyAccountGroups, declineAccountGroups, previewAccountPlan, roleLabel, AccountsView } = await import(pathToFileURL(module));
    assert.equal(privilegeResult().state, "unknown");
    assert.equal(privilegeResult().disabledReason, "");
    const gate = privilegeResult({ state: "denied", missing: [["SUPER"], ["CONNECTION_ADMIN"]], reason: "Denied" }, "app@%");
    assert.deepEqual(gate.missing, ["SUPER", "CONNECTION_ADMIN"]); assert.match(gate.disabledReason, /app@%/);
    let clicks = 0, prevented = 0;
    const e = { preventDefault() { prevented++; }, stopPropagation() {} };
    const off = privilegeButtonProps(gate, () => clicks++, "", "missing-privilege");
    assert.equal(off["aria-describedby"], "missing-privilege"); assert.ok(!Object.hasOwn(off, "aria-description")); off.onClick(e);
    assert.equal(off["aria-disabled"], true); assert.equal(clicks, 0); assert.equal(prevented, 1);
    privilegeButtonProps(privilegeResult(), () => clicks++).onClick(e); assert.equal(clicks, 1);
    privilegeButtonProps(privilegeResult(), () => clicks++, "Stale connection").onClick(e); assert.equal(clicks, 1);
    assert.equal(privilegeResult({ ...gate, grey_out: false }).disabledReason, "");
    assert.equal(privilegeResult({ state: "denied", missing: [], reason: "Unsupported", available: false }).available, false);
    const draft = [{ operation: "grant", level: "global", privileges: ["SELECT"] }, { operation: "grant", level: "global", privileges: ["UPDATE"] },
      { operation: "revoke", level: "global", privileges: ["INSERT"] }, { operation: "grant", level: "table", database: "a", table: "b", privileges: ["SELECT"] }];
    const compact = compactChanges(draft);
    assert.equal(compact.length, 3); assert.deepEqual(compact[0].privileges, ["SELECT", "UPDATE"]); assert.deepEqual(draft[0].privileges, ["SELECT"]);
    assert.equal(compactChanges([{ operation: "revoke", privileges: ["GRANT OPTION"] }, { operation: "revoke", privileges: ["SELECT"] }]).length, 2);
    const model = { global: ["USAGE", "SELECT"], dynamic: ["SYSTEM_USER"], global_grantable: ["SELECT"], schemas: [{ database: "db", pattern: "db%", wildcard: true, privs: ["SELECT"], grantable: false }], tables: [], columns: [], routines: [], proxies: [], restrictions: [], roles: [], unparsed: [] };
    const scopes = observedScopes({ model });
    assert.deepEqual(scopes[0].cells.map(c => c.privilege), ["SELECT", "SYSTEM_USER"]); assert.equal(scopes[1].database, "db%"); assert.equal(scopes[1].pattern, true);
    const projected = [{ id: "x", cells: [{ direct: false, via: "role reader" }] }];
    assert.equal(observedScopes({ grant_scopes: projected, model }), projected);
    const requests = [];
    const connection = { host: "server", port: 3306, user: "actor", name: "test", database: "", ssl_mode: "DISABLED" };
    const target = { name: "app", host: "%" };
    let response = { before: { model, roles: [], default_roles: [], attributes: {}, grants: [] }, request: { operation: "password", user: "app", host: "%" }, plan_id: "opaque", expect: "fingerprint", statements: ["GRANT SELECT ON *.* TO `app`@`%`"], policy: { action: "confirm", text: "Confirm" }, guards: { blocked: [], typed_name: "" } };
    globalThis.fetch = async (url, options) => { if (url === "/api/auth/session") return { ok: true, json: async () => ({ token: "test-token" }) }; requests.push({ url, body: JSON.parse(options.body), token: options.headers["X-Myflames-Token"] }); return { ok: true, json: async () => response }; };
    const plan = await planAccount(connection, target, [{ operation: "password" }], "one-request-secret");
    assert.equal(requests[0].body.user, "app"); assert.equal(requests[0].body.password, "one-request-secret"); assert.equal(requests[0].token, "test-token");
    await applyAccount(connection, plan, "app@%", "reentered-secret");
    assert.equal(requests[1].body.plan_id, "opaque"); assert.equal(requests[1].body.expect, "fingerprint"); assert.equal(requests[1].body.confirmed, true);
    assert.equal(requests[1].body.password, "reentered-secret"); assert.ok(!Object.hasOwn(requests[1].body, "statements"));
    await declineAccount(connection, { ...plan, operation: "grant", name: "app", host: "%", change_set_id: "set" });
    assert.equal(requests[2].url, "/api/audit/declined"); assert.equal(requests[2].body.target.plan_id, "opaque");
    await assert.rejects(planAccount(connection, target, draft.slice(0, 2)), /one operation/);
    assert.equal(requests.length, 3, "unsupported batches never reach the endpoint");
    await applyAccount(connection, { ...plan, generates_password: true }, "");
    assert.ok(!Object.hasOwn(requests[3].body, "password"), "generated passwords are supplied only by the backend");
    const roles = [{ operation: "roles", mode: "grant", roles: [{ name: "r1", host: "%" }] }, { operation: "roles", mode: "grant", roles: [{ name: "r2", host: "%" }] }];
    assert.deepEqual(compactChanges(roles)[0].roles.map(r => r.name), ["r1", "r2"]);
    assert.equal(compactChanges([...roles, { ...roles[0], admin_option: true }]).length, 2, "mixed admin options need separate groups");
    const preview = previewAccountPlan({ ...response, request: { operation: "revoke", privileges: ["SELECT"], level: "global" } });
    assert.equal(preview[0].before, "Granted; grant option"); assert.equal(preview[0].after, "Direct privilege removed");
    assert.equal(previewAccountPlan({ ...response, request: { operation: "grant", privileges: ["INSERT"], level: "global" } })[0].before, "No direct grant read");
    assert.equal(previewAccountPlan({ ...response, request: { operation: "roles", mode: "grant", roles: roles[0].roles } })[0].after, "Granted");
    assert.equal(roleLabel({ name: "reader", host: "" }), "reader", "MariaDB roles have no host suffix");
    assert.equal(previewAccountPlan({ ...response, request: { operation: "roles", mode: "default", roles: [{ name: "reader", host: "" }] } })[0].after, "reader");
    // Exercise the real batch HTTP adapter with one-shot handles and a
    // fingerprint that changes after every applied operation.
    const batchEdits = [draft[0], draft[3], draft[2]];
    let serial = 0, fp = 0, failPlan = -1, failApply = -1, drift = "", plannedIndex = 0, appliedIndex = 0;
    let batchEvents = [], handles = new Map();
    const resetBatch = () => { serial = fp = plannedIndex = appliedIndex = 0; failPlan = failApply = -1; drift = ""; batchEvents = []; handles = new Map(); };
    globalThis.fetch = async (url, options) => {
      const body = JSON.parse(options.body); batchEvents.push({ url, body });
      if (url.endsWith("/plan")) {
        if (plannedIndex++ === failPlan) return { ok: false, status: 400, json: async () => ({ error: "Scope cannot be planned." }) };
        const p = { ...response, name: target.name, host: target.host, operation: body.operation, status: "ready",
          plan_id: `batch-${++serial}`, change_set_id: `set-${serial}`, expect: `fp-${fp}`,
          request: { ...body }, warnings: drift === "warning" ? ["New warning"] : [],
          guards: { blocked: [], typed_name: drift === "guard" ? "other@%" : "app@%" },
          statements: [`${body.operation} ${body.level} ${body.privileges.join(",")} ${drift === "sql" ? "changed" : ""}`] };
        handles.set(p.plan_id, p); return { ok: true, json: async () => p };
      }
      if (url.endsWith("/apply")) {
        assert.ok(handles.has(body.plan_id), "one-shot plan must exist"); handles.delete(body.plan_id);
        assert.equal(body.expect, `fp-${fp}`, "fingerprint must include previous applied groups");
        assert.equal(body.typed_name, "app@%");
        const status = appliedIndex++ === failApply ? "refused" : "applied";
        if (status === "applied") fp++;
        return { ok: true, json: async () => ({ status, message: status, statements: [], diff: {}, warnings: [], audit_ids: [] }) };
      }
      return { ok: true, json: async () => ({ status: "declined" }) };
    };
    const typed = { '["app","%"]': "app@%" }, check = async () => {};
    let groups = await planAccountGroups(connection, target, batchEdits);
    assert.equal(groups.length, 3);
    const reviewedHandles = groups.map(g => g.plan.plan_id);
    let outcome = await applyAccountGroups(connection, groups, typed, check);
    assert.deepEqual(outcome.map(o => o.status), ["applied", "applied", "applied"]);
    assert.deepEqual(batchEvents.map(e => e.url.split("/").at(-1)), ["plan", "plan", "plan", "plan", "apply", "plan", "apply", "plan", "apply"]);
    assert.ok(batchEvents.filter(e => e.url.endsWith("/apply")).every(e => !reviewedHandles.includes(e.body.plan_id)));
    assert.deepEqual(batchEvents.filter(e => e.url.endsWith("/apply")).map(e => e.body.expect), ["fp-0", "fp-1", "fp-2"]);
    resetBatch(); failPlan = 1;
    groups = await planAccountGroups(connection, target, batchEdits);
    assert.match(groups[1].error, /Scope cannot be planned/); assert.ok(groups[2].plan, "later groups still get a review");
    outcome = await applyAccountGroups(connection, groups, typed, check);
    assert.ok(outcome.every(o => o.status === "not_sent")); assert.equal(batchEvents.length, 3);
    await declineAccountGroups(connection, groups);
    assert.equal(batchEvents.filter(e => e.url.endsWith("/declined")).length, 2, "one decline per planned group");
    outcome = await applyAccountGroups(connection, groups.filter(g => g.plan), typed, check);
    assert.deepEqual(outcome.map(o => o.status), ["applied", "applied"]);
    resetBatch(); groups = await planAccountGroups(connection, target, batchEdits); failApply = 1;
    outcome = await applyAccountGroups(connection, groups, typed, check);
    assert.deepEqual(outcome.map(o => o.status), ["applied", "failed", "not_sent"]);
    assert.equal(batchEvents.filter(e => e.url.endsWith("/apply")).length, 2);
    for (const change of ["sql", "guard", "warning"]) {
      resetBatch(); groups = await planAccountGroups(connection, target, batchEdits); drift = change;
      outcome = await applyAccountGroups(connection, groups, typed, check);
      assert.deepEqual(outcome.map(o => o.status), ["failed", "not_sent", "not_sent"]);
      assert.equal(batchEvents.filter(e => e.url.endsWith("/apply")).length, 0, `changed ${change} requires review`);
    }
    resetBatch(); groups = await planAccountGroups(connection, target, batchEdits);
    let checks = 0;
    outcome = await applyAccountGroups(connection, groups, typed, async () => { if (++checks === 2) throw new Error("Connection switched while planning"); });
    assert.equal(outcome[0].status, "failed"); assert.match(outcome[0].message, /Connection switched/);
    assert.equal(batchEvents.filter(e => e.url.endsWith("/apply")).length, 0);
    resetBatch(); groups = await planAccountGroups(connection, target, batchEdits);
    const fetchBatch = globalThis.fetch;
    globalThis.fetch = async (url, options) => { if (url.endsWith("/apply")) throw new Error("Lost reply"); return fetchBatch(url, options); };
    outcome = await applyAccountGroups(connection, groups, typed, check);
    assert.deepEqual(outcome.map(o => o.status), ["failed", "not_sent", "not_sent"]); assert.match(outcome[0].message, /may have run/);
    globalThis.fetch = async () => ({ ok: true, json: async () => response });
    // Re-render real Python account-list reports from all five recorded
    // servers. Fixtures are credential-free backend outputs, not UI guesses.
    const reports = JSON.parse(execFileSync("python3", ["-c", `
import json, sys
sys.path.insert(0, 'test')
import _accounts_replay as replay
from myflames import accounts, privileges
out = []
for server in replay.SERVERS:
    conn, data = replay.reads(server)
    catalog = json.loads((replay.FIXTURES / ('show-privileges-' + server + '.json')).read_text())
    for call in catalog['sessions']['root']['calls']:
        conn.answers.setdefault(call['sql'], call)
    listing = accounts.list_accounts(conn)
    detail = accounts.read_account(conn, 'acct_app', '%')
    actor, _ = replay.session(server, 'acct_ops')
    out.append({'listing': listing, 'detail': detail, 'privileges': privileges.read_privileges(actor)})
print(json.dumps(out))
`], { cwd: path.resolve(workspace, "../.."), env: { ...process.env, HOME: temporary, USERPROFILE: temporary, MYFLAMES_SECRET_BACKEND: "vault", PYTHONDONTWRITEBYTECODE: "1" }, encoding: "utf8" }));
    const { createElement } = await import("react");
    const { renderToStaticMarkup } = await import("react-dom/server");
    for (const { listing, detail, privileges } of reports) {
      assert.ok(Array.isArray(listing.role_grants)); assert.equal(listing.roles, undefined);
      assert.ok(!Object.hasOwn(privileges.actions, "alter_user"));
      for (const key of ["lock_account", "unlock_account", "expire_password", "set_own_password"]) assert.ok(privileges.actions[key]);
      assert.ok(observedScopes(detail).length);
      const markup = renderToStaticMarkup(createElement(AccountsView, { connection, report: { key: "fixture", label: "fixture", report: listing }, onReport() {}, selection: { key: "", select() {} } }));
      assert.ok(markup.includes("acct_app")); assert.ok(markup.includes("Accounts read")); assert.ok(!markup.includes("No threshold"));
    }
    const unreadableReason = "Only the connected account is listed: listing users and roles needs SELECT on the mysql database.";
    const unreadable = { schema_version: "accounts-1.0", engine: "mysql", version: "8.4.0", complete: false,
      accounts: [{ name: "limited", host: "%", connected: true }], role_grants: [], default_roles: [], warnings: [unreadableReason] };
    const unreadableMarkup = renderToStaticMarkup(createElement(AccountsView, { connection,
      report: { key: "fixture", label: "fixture", report: unreadable }, onReport() {}, selection: { key: "", select() {} } }));
    assert.match(unreadableMarkup, /Not judged/);
    assert.ok(unreadableMarkup.includes(unreadableReason), "the actual unreadable-list reason is visible");
    assert.match(unreadableMarkup, /data-col="details"/);
    assert.match(unreadableMarkup, /Not readable/);
    for (const key of ["type", "locked", "expired", "risk", "plugin"]) assert.ok(!unreadableMarkup.includes(`data-col="${key}"`), `unreadable metadata omits ${key}`);
    response = { ...response, plan_id: "" };
    await assert.rejects(planAccount(connection, target, [{ operation: "lock" }]), /complete reviewable/);
    await assert.rejects(planAccount(connection, target, []), /No pending/);
    console.log("accounts unit: gate states, disabled activation, pending compaction, grant projection, exact API payloads, password re-entry, real backend list rendering batch order, fresh fingerprints, per-group failures, cancel audits and malformed-plan refusal passed");
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
  process.exit(0);
}
const entry = "\0accounts-fixture.tsx";
const built = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
  plugins: [{ name: "accounts-fixture", resolveId(id) { if (id === entry) return id; }, async load(id) {
    if (id !== entry) return null;
    const fixture = `
      import React, {useState} from ${JSON.stringify(path.join(workspace, "node_modules/react/index.js"))};
      import {createRoot} from ${JSON.stringify(path.join(workspace, "node_modules/react-dom/client.js"))};
      import {AccountsView, compactChanges} from ${source("Accounts.tsx")};
      import {PrivilegesProvider, PrivilegeGate, usePrivileges, privilegeResult} from ${source("privileges.ts")};
      import {InspectorContext, ColumnStoreContext, useColumnStore} from ${source("kit.tsx")};
      import {ViewOnScreenContext} from ${source("viewOnScreen.ts")};
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
        const [onScreen,setOnScreen] = useState(false), [columns,setColumns] = useState({});
        window.accountColumns = columns;
        const columnStore = useColumnStore(columns, setColumns);
        React.useEffect(() => { const f = () => setWidth(window.innerWidth); window.addEventListener("resize", f); return () => window.removeEventListener("resize", f); }, []);
        return <WritePolicyContext.Provider value={{blocked:"",check:async()=>connection}}>
          <PrivilegesProvider connection={connection} connectRevision={revision}>
            <div className="test-probes"><button onClick={()=>setConnection(c=>({...c,host:c.host === "127.0.0.1" ? "127.0.0.2" : "127.0.0.1"}))}>Switch connection</button>
              <button onClick={()=>setConnection(c=>({...c,read_only:!c.read_only}))}>Toggle read-only</button>
              <button onClick={()=>setRevision(n=>n+1)}>Reconnect</button>
              <button onClick={()=>setOnScreen(v=>!v)}>Toggle Accounts visibility</button></div><Probe/>
            <InspectorContext.Provider value={{open:inspector,setOpen:setInspector,width:440,setWidth:()=>{},sheet:width<1040}}>
              <ColumnStoreContext.Provider value={columnStore}>
                <ViewOnScreenContext.Provider value={onScreen}><div hidden={!onScreen}>
                  <AccountsView connection={connection} report={report} onReport={setReport} onBack={()=>setInspector(false)}
                    selection={{key:selected,select:(key,open)=>{select(key);if(open)setInspector(true);}}}/>
                </div></ViewOnScreenContext.Provider>
              </ColumnStoreContext.Provider>
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
const allowed = { state: "allowed", missing: [], reason: "Required privileges are present." };
const unknown = { state: "unknown", missing: ["SELECT"], reason: "Object privileges cannot be proven absent." };
const denied = { state: "denied", missing: [["CONNECTION_ADMIN"], ["SUPER"]], need: "CONNECTION_ADMIN or SUPER", reason: "Global privileges are absent.", grey_out: true };
let actions = Object.fromEntries(["create_user", "create_role", "lock_account", "unlock_account", "expire_password", "set_own_password", "rename_user", "drop_user", "drop_role", "set_password", "grant", "revoke", "grant_role", "revoke_role", "default_roles"].map(k => [k, allowed]));
let privilegeReads = 0, privilegeDelay = 0, planDelay = 0, planFailure = false, applyFailure = false;
let planFailureAt = -1, applyRefusalAt = -1, applyCount = 0;
let planCount = 0, nextTyped = "", nextWarnings = [], defaultRole = [], engine = "mysql";
const model = { global: ["SELECT", "INSERT", "UPDATE"], dynamic: ["BACKUP_ADMIN"], global_grantable: ["SELECT", "INSERT", "UPDATE", "BACKUP_ADMIN"], static_grant_option: true, schemas: [], tables: [{ database: "inventory", table: "items", privs: ["SELECT"], grantable: false }], columns: [], routines: [], proxies: [], restrictions: [], roles: [], unparsed: [] };
const target = { name: "app<probe>", host: "%" };
let fingerprint = 1;
let grants = ["GRANT SELECT ON *.* TO `app<probe>`@`%`"];
const detail = user => ({ schema_version: "account-1.0", name: user, host: "%", engine, version: engine === "mysql" ? "8.4.0" : "11.4.13-MariaDB", exists: true,
  fingerprint: `fp-${fingerprint}`, grants, attributes: { locked: false, expired: false, plugin: "caching_sha2_password" }, model,
  roles: [{ name: "reader", host: "%", admin_option: false }], default_roles: defaultRole, defaults_checked: true, warnings: [],

});
const list = { schema_version: "accounts-1.0", engine: "mysql", version: "8.4.0", complete: true, warnings: [],
  accounts: [{ ...target, plugin: "caching_sha2_password", locked: false, expired: false, role: false }, { name: "reader", host: "%", role: true, locked: true }, { name: "mysql.sys", host: "localhost", locked: true }],
  role_grants: [{ role: { name: "reader", host: "%" }, grantee: target, admin_option: false }], default_roles: [] };
const grantMode = process.argv.includes("--grants");
let scopeDelay = 0, scopeFailure = false;
const granted = new Set();
const catalog = {
  global: [{name:"SELECT"}, {name:"FLUSH_TABLES", dynamic:true, grantable_here:true, description:"Flush tables."}],
  database: [{name:"CREATE ROUTINE",dynamic:false,grantable_here:true,description:"Create stored routines."}, {name:"SELECT",grantable_here:true}, {name:"GLOBAL_ONLY_TEST",grantable_here:false}],
  table: [{name:"SELECT",grantable_here:true}], column:[{name:"SELECT",grantable_here:true}],
  routine:[{name:"EXECUTE",grantable_here:true}], proxy:[]
};
const baseDetail = detail;
const readDetail = user => {
  const value = baseDetail(user);
  if (!grantMode) return value;
  return {...value, role:user === "reader", privilege_catalog:catalog, privilege_catalog_meta:{engine,version:value.version,source:"server"}, grant_scopes:[
    {id:"server:global",label:"*.*",level:"global",cells:model.global.map(privilege=>({privilege,direct:true,grantable:false}))},
    ...["shop","inventory","literal_name","mysql"].map(database=>({id:`server:${database}`,label:`${database}.*`,level:"database",database,pattern:false,system:database==="mysql",
      cells:granted.has(`${user}:${database}`)?[{privilege:"CREATE ROUTINE",direct:true,grantable:false}]:[]})),
    {id:"server:items",label:"inventory.items",level:"table",database:"inventory",table:"items",cells:[{privilege:"SELECT",direct:true,grantable:false}]}
  ]};
};
let latestPlan;
let h;
try {
  h = await open("/api/accounts/list", "accounts", {
    "/": content("text/html", html), "/__accounts.js": content("text/javascript", js), "/__accounts.css": content("text/css", css),
    "/api/live/privileges": () => { privilegeReads++; return { status: 200, delay: privilegeDelay, body: { schema_version: "privileges-1.0", account: "operator@localhost", actions: { ...actions, kill: denied, explain: unknown } } }; },
    "/api/accounts/read": body => ({ status: 200, body: readDetail(body.user) }),
    "/api/accounts/scope-objects": body => ({status:scopeFailure ? 400 : 200, delay:scopeDelay,
      body:scopeFailure ? {error:"Object list unavailable."} : {database:body.database,tables:[{name:body.database==="shop"?"orders":"items"}],procedures:[{name:"recount"}],functions:[{name:"total"}],truncated:true,warnings:[]}}),
    "/api/accounts/plan": body => {
      planCount++;
      if (planFailure || planCount === planFailureAt) return { status: 400, body: { error: "The account changed during review." } };
      latestPlan = { schema_version: "account-plan-1.0", plan_id: `plan-${planCount}`, change_set_id: `set-${planCount}`, name: body.user, host: body.host, role: body.role,
        operation: body.operation, account: body.user, engine, version: engine === "mysql" ? "8.4.0" : "11.4.13-MariaDB",
        request: Object.fromEntries(Object.entries(body).filter(([k]) => !["connection", "password"].includes(k))), before: structuredClone(readDetail(body.user)), generates_password: !!body.random_password, expect: `fp-${fingerprint}`, status: "ready",
        statements: body.operation === "password" || body.operation === "create" ? ["ALTER USER `app<probe>`@`%` IDENTIFIED BY '[redacted]'"] : [`${body.operation.toUpperCase()} ${(body.privileges || ["reader"]).join(", ")} ON *.* ${body.operation === "revoke" ? "FROM" : "TO"} ` + "`app<probe>`@`%`"],
        warnings: nextWarnings, guards: { blocked: [], typed_name: nextTyped }, privileges: unknown, policy: { action: "confirm", text: "Production: confirm this account change." } };
      if (grantMode && body.level === "database") latestPlan.statements = [`GRANT ${body.privileges.join(", ")} ON \`${body.database.replaceAll("_", "\\_")}\`.* TO \`${body.user}\`@\`${body.host}\``];
      return { status: 200, body: latestPlan, delay: planDelay };
    },
    "/api/accounts/apply": body => {
      applyCount++;
      assert.equal(body.confirmed, true); assert.equal(body.plan_id, latestPlan.plan_id); assert.equal(body.expect, latestPlan.expect);
      assert.equal(Object.hasOwn(body, "password"), ["create", "password"].includes(latestPlan.operation) && !latestPlan.generates_password && !latestPlan.role);
      assert.ok(!Object.hasOwn(body, "plan"));
      if (applyFailure) return { status: 500, body: { error: "Connection lost." } };
      if (applyCount === applyRefusalAt) return { status: 200, body: { schema_version: "account-change-1.0", status: "refused", message: "Account changed before sending.", statements: [], diff: {}, warnings: [], audit_ids: ["refused"] } };
      assert.equal(body.expect, `fp-${fingerprint}`);
      const before = structuredClone(detail(target.name));
      fingerprint++;
      if (grantMode && latestPlan.request.level === "database") granted.add(`${latestPlan.name}:${latestPlan.request.database}`);
      if (latestPlan.request.privileges?.includes("GRANT OPTION")) {
        model.static_grant_option = latestPlan.operation === "grant";
        model.global_grantable = [...(model.static_grant_option ? model.global : []), ...model.dynamic];
      }
      grants = ["GRANT SELECT, INSERT, UPDATE ON *.* TO `app<probe>`@`%`" + (model.static_grant_option ? " WITH GRANT OPTION" : "")];
      return { status: 200, body: { schema_version: "account-change-1.0", change_set_id: latestPlan.change_set_id, status: "applied", message: "Statements accepted.",
        before, after: readDetail(latestPlan.name), diff: { added: grants, removed: ["GRANT SELECT ON *.*"] },
        statements: [{ index: 0, statement: latestPlan.statements[0], status: "ok" }], warnings: [], audit_ids: ["audit-1"],
        ...(latestPlan.generates_password ? { generated_password: "shown-once-secret" } : {}) } };
    },
    "/api/audit/declined": { status: 200, body: { status: "declined", audit_id: "declined-1" } },
  });
  const { page, state } = h;
  state.fallback = { status: 200, body: list };
  const count = endpoint => state.other.filter(r => r.path === `/api/${endpoint}`).length;
  const click = async text => {
    const handle = await page.evaluateHandle(text => [...document.querySelectorAll("button")].find(b => b.textContent.trim() === text && b.getClientRects().length), text);
    assert.ok(await handle.asElement(), `button ${text}`); await handle.asElement().click(); await handle.dispose();
  };
  const backToAccounts = async () => {
    const back = await page.$(".accounts-view .detail-back");
    if (back) await back.click();
  };
  const inspect = async () => {
    await backToAccounts();
    await page.click(".accounts-view tbody tr[data-row-key]");
    await page.waitForSelector(".account-matrix");
  };
  const tick = async privilege => {
    await page.$$eval(".account-matrix tbody tr", (rows, p) => rows.find(r => r.querySelector("th")?.textContent === p)?.querySelector("input")?.click(), privilege);
  };
  const review = async () => { await page.click(".account-inspector > .inspector-block .account-checkbox input"); await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review[open]"); };
  const dismiss = async () => { await page.keyboard.press("Escape"); await page.waitForFunction(() => !document.querySelector(".account-review")); };

  if (grantMode) {
    if (process.env.PERF_SHOTS) await fs.mkdir(process.env.PERF_SHOTS, {recursive:true});
    const field = (label) => `select[aria-label="${label}"]`;
    const settled = async () => page.evaluate(async () => { await Promise.all(document.getAnimations().map(a => a.finished.catch(()=>{}))); });
    const rowInput = async (name, index=0, toggle=true) => page.$$eval(".account-matrix tbody tr", (rows,{name,index,toggle})=>{
      const row=rows.find(r=>(r.querySelector(".account-privilege-name")?.textContent || r.querySelector("th")?.textContent)===name);
      const input=row?.querySelectorAll("input")[index]; if (!input) throw new Error(`Missing ${name} checkbox ${index}`);
      if(toggle) input.click(); return {checked:input.checked,disabled:input.getAttribute("aria-disabled")};
    },{name,index,toggle});
    const selectScope = async database => {
      const value=await page.$$eval(`${field("Grant scope")} option`,(options,db)=>options.find(o=>o.textContent===`${db}.*`)?.value,database);
      assert.ok(value); await page.select(field("Grant scope"),value);
    };
    await click("Toggle Accounts visibility"); await page.waitForSelector(".accounts-view tbody tr"); await inspect();
    // Newly supported dynamic privilege offers its own option despite being unheld.
    await rowInput("FLUSH_TABLES"); await rowInput("FLUSH_TABLES",1);
    assert.match(await h.text(".account-pending"),/with grant option/); await click("Discard");
    actions.grant=unknown; await click("Fresh privileges");
    await page.select(field("Grant level"),"database"); await selectScope("shop");
    assert.equal((await rowInput("CREATE ROUTINE",0,false)).checked,false);
    assert.equal((await rowInput("GLOBAL_ONLY_TEST",0,false)).disabled,"true");
    await rowInput("CREATE ROUTINE"); assert.equal(count("accounts/apply"),0);
    await click("Roles"); await click("Grants");
    assert.equal(await page.$eval(field("Grant level"),e=>e.value),"database");
    assert.equal((await rowInput("CREATE ROUTINE",0,false)).checked,true);
    await settled(); await page.screenshot({path:path.join(h.shots,"grant-pending-desktop.png"),fullPage:true});
    await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
    assert.match(await h.text(".account-review"),/GRANT CREATE ROUTINE ON `shop`\.\* TO `app<probe>`@`%`/);
    await settled(); await page.screenshot({path:path.join(h.shots,"grant-review-desktop.png"),fullPage:true});
    await page.click(".account-review .danger-solid"); await page.waitForFunction(()=>!document.querySelector(".account-review"));
    await page.waitForSelector(".account-matrix"); await page.select(field("Grant level"),"database"); await selectScope("shop");
    assert.equal((await rowInput("CREATE ROUTINE",0,false)).checked,true); assert.equal(count("accounts/apply"),1);
    const change=state.other.find(r=>r.path==="/api/accounts/plan"&&r.body.database==="shop").body;
    assert.deepEqual(change.privileges,["CREATE ROUTINE"]); assert.equal(change.pattern,false);
    // Catalog and manually entered copies of a scope share one identity.
    const scopeCount=await page.$$eval(`${field("Grant scope")} option`,o=>o.length);
    await click("Add scope"); await page.select(field("Database"),"shop"); await click("Use scope");
    assert.equal(await page.$$eval(`${field("Grant scope")} option`,o=>o.length),scopeCount);
    await selectScope("literal_name"); await rowInput("CREATE ROUTINE");
    await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
    assert.equal(latestPlan.request.pattern,false); assert.equal(latestPlan.request.database,"literal_name");
    await dismiss(); await click("Discard");
    // Objects are lazy, replaceable and exact; manual names survive partial catalogs.
    await page.select(field("Grant level"),"table"); await click("Add scope");
    scopeDelay=150; await page.select(field("Database"),"shop");
    scopeDelay=0; await page.select(field("Database"),"inventory");
    await page.waitForFunction(()=>document.querySelector('select[aria-label="Table"]')?.textContent.includes("items"));
    await pause(180); assert.ok(!(await page.$eval(field("Table"),e=>e.textContent)).includes("orders"));
    await settled(); await page.screenshot({path:path.join(h.shots,"grant-object-picker.png"),fullPage:true});
    await click("Enter an exact name"); await page.type('.account-scope-form input',"archived_items"); await click("Use scope");
    await rowInput("SELECT"); await click("Roles"); await click("Grants");
    assert.match(await page.$eval(field("Grant scope"),e=>e.selectedOptions[0].textContent),/archived_items/);
    await click("Discard");
    await page.select(field("Grant level"),"routine"); await click("Add scope"); await page.select(field("Database"),"shop");
    await page.waitForFunction(()=>document.querySelector('select[aria-label="Routine"]')?.textContent.includes("recount"));
    await page.select(field("Routine kind"),"FUNCTION"); await page.select(field("Routine"),"total"); await click("Use scope");
    await rowInput("EXECUTE"); await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
    assert.equal(latestPlan.request.kind,"FUNCTION"); assert.equal(latestPlan.request.routine,"total"); await dismiss(); await click("Discard");
    scopeFailure=true; await click("Add scope"); await page.select(field("Database"),"shop");
    await page.waitForSelector('.account-scope-form [role="alert"]');
    assert.match(await h.text('.account-scope-form'),/Enter the exact name/);
    await page.type('.account-scope-form input',"manual_routine"); await click("Use scope"); scopeFailure=false;
    await click("Add scope"); await click("Enter a database name");
    const objectsBeforeTyping=count("accounts/scope-objects");
    await page.type('.account-scope-form input',"manual_db");
    assert.equal(count("accounts/scope-objects"),objectsBeforeTyping);
    await click("Read objects"); await page.waitForFunction(()=>document.querySelector('select[aria-label="Routine"]')?.textContent.includes("recount"));
    assert.equal(count("accounts/scope-objects"),objectsBeforeTyping+1); await click("Cancel");
    // Roles receive the same unheld privilege controls and reviewed requests.
    await backToAccounts();
    await page.$$eval('.accounts-view tbody tr[data-row-key]',rows=>rows.find(r=>r.textContent.includes("reader@%"))?.click());
    await page.waitForFunction(()=>document.querySelector('.account-inspector')&&document.querySelector('.inspector-title')?.textContent.includes('reader'));
    await page.select(field("Grant level"),"database"); await selectScope("shop"); await rowInput("CREATE ROUTINE");
    await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
    assert.equal(latestPlan.role,true);
    await page.click(".account-review .danger-solid"); await page.waitForFunction(()=>!document.querySelector(".account-review"));
    await page.waitForSelector(".account-matrix"); await page.select(field("Grant level"),"database"); await selectScope("shop");
    assert.equal((await rowInput("CREATE ROUTINE",0,false)).checked,true); assert.equal(count("accounts/apply"),2);
    await click("Dismiss result"); await rowInput("SELECT");
    await settled(); await page.screenshot({path:path.join(h.shots,"grant-desktop.png"),fullPage:true});
    await h.narrow(); await page.setViewport({width:390,height:844}); await pause(250); await settled();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth),0);
    await page.screenshot({path:path.join(h.shots,"grant-phone.png"),fullPage:true});
    await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review"); await settled();
    await page.screenshot({path:path.join(h.shots,"grant-review-phone.png"),fullPage:true});
    await dismiss();
    assert.deepEqual(h.errors,[]);
    console.log("grant matrix: unchecked grant, exact review/apply, roles, dynamic option, literal scopes, deduplication, tab retention, stale objects, routines, manual fallback and 390px passed");
    await h.close(); h=null; process.exit(0);
  }

  // Accounts reads once when opened, never while retained off screen. An
  // unsuccessful first read stops until the explicit Try again action.
  assert.equal(state.seen.length, 0, "Accounts has not read off screen");
  state.queue.push({ status: 400, body: { error: "Cannot read account metadata in this session." } });
  await click("Toggle Accounts visibility");
  await page.waitForSelector(".accounts-view [role=alert]");
  assert.equal(state.seen.length, 1, "opening attempts one read");
  await page.waitForFunction(() => !document.querySelector(".accounts-view .capture-status"));
  assert.match(await h.text(".accounts-view [role=alert]"), /Cannot read account metadata/);
  await click("Try again");
  await page.waitForSelector(".accounts-view tbody tr");
  assert.equal(state.seen.length, 2, "Try again starts a fresh read");
  await click("Toggle Accounts visibility");
  await click("Toggle Accounts visibility");
  assert.equal(state.seen.length, 2, "reopening keeps the current report");

  // Exact global denials stay visible and focusable, unknown remains enabled.
  assert.equal(await page.$eval('[data-privilege-action="kill"]', e => e.getAttribute("aria-disabled")), "true");
  assert.match(await page.$eval('[data-privilege-action="kill"]', e => e.title), /CONNECTION_ADMIN.*SUPER/);
  await page.focus('[data-privilege-action="kill"]'); await page.keyboard.press("Enter"); await page.keyboard.press("Space");
  assert.equal(await page.evaluate(() => window.killCalls || 0), 0);
  await page.click('[data-privilege-action="explain"]');
  assert.equal(await page.evaluate(() => window.explainCalls), 1);
  assert.doesNotMatch(await h.text('[data-privilege-action="explain"]'), /Unverified/);
  await page.focus('[data-privilege-action="explain"]');
  assert.match(await page.$eval('[data-privilege-action="explain"]', e =>
    document.getElementById(e.getAttribute('aria-describedby')).textContent), /Unverified/);
  const probes = await page.evaluate(() => ({
    read: window.accountTest.privilegeResult({ state: "denied", missing: ["PROCESS"], reason: "Needs PROCESS", grey_out: false }),
    missing: window.accountTest.privilegeResult(),
    compact: window.accountTest.compactChanges([{ operation:"grant",level:"global",privileges:["INSERT"] },{ operation:"grant",level:"global",privileges:["UPDATE"] }]),
  }));
  assert.equal(probes.read.disabledReason, ""); assert.equal(probes.missing.state, "unknown"); assert.equal(probes.compact.length, 1);
  assert.deepEqual(probes.compact[0].privileges, ["INSERT", "UPDATE"]);

  await inspect();
  assert.equal(await h.text(".accounts-view .summary-text"), "2 users · 1 role", "counts are pluralized");
  assert.equal(await page.$("probe"), null); // User names are rendered as text.
  assert.equal(count("accounts/apply"), 0);
  const selectedKey = await page.$eval('.accounts-view tr[aria-current="true"]', row => row.dataset.rowKey);
  const chooseReader = async () => {
    await backToAccounts();
    await page.$$eval('.accounts-view tbody tr[data-row-key]', rows => rows.find(row => row.textContent.includes("reader@%"))?.click());
    await page.waitForSelector(".account-selection-review[open]");
  };
  await tick("INSERT");
  const detailReads = count("accounts/read"), plansBeforeChoice = count("accounts/plan");
  await chooseReader();
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), "Cancel");
  assert.deepEqual(await page.$$eval(".account-selection-review .sheet-actions button", buttons => buttons.map(b => b.textContent)), ["Cancel", "Discard", "Review changes"]);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".account-selection-review"));
  assert.equal(await page.$eval('.accounts-view tr[aria-current="true"]', row => row.dataset.rowKey), selectedKey);
  assert.ok(await page.$(".account-pending"));
  assert.equal(count("accounts/read"), detailReads, "cancel never reads the other account");
  assert.equal(count("accounts/plan"), plansBeforeChoice, "selection alone never prepares a write");
  await chooseReader();
  await page.locator('.account-selection-review button ::-p-text(Cancel)').click();
  assert.ok(await page.$(".account-pending"));
  await chooseReader();
  await page.locator('.account-selection-review button ::-p-text(Discard)').click();
  await page.waitForFunction(() => document.querySelector('.account-inspector') && document.querySelector('.inspector-title')?.textContent.includes('reader'));
  assert.equal(await page.$(".account-pending"), null);
  await inspect();
  await page.click(".account-inspector > .inspector-block .account-checkbox input");
  await chooseReader();
  await page.locator('.account-selection-review button ::-p-text(Review changes)').click();
  await page.waitForSelector(".account-review[open]");
  assert.equal(latestPlan.name, target.name, "review remains bound to the account with pending edits");
  assert.equal(await page.$eval('.accounts-view tr[aria-current="true"]', row => row.dataset.rowKey), selectedKey);
  assert.equal(count("accounts/apply"), 0, "Review changes only prepares statements");
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), "Cancel");
  assert.match(await h.text(".account-review"), /Production: confirm/);
  assert.match(await h.text('[aria-label="Before and after grants"]'), /Before.*After.*GRANT OPTION/s);
  assert.ok(await page.$('.account-review [aria-label="Before and after grants"] tbody tr'));
  // Revoking the static scope option never flips a dynamic privilege's option.
  assert.equal(await page.$$eval('.account-inspector .account-matrix tbody tr', rows => rows.find(r => r.textContent.includes("BACKUP_ADMIN")).querySelectorAll("input")[1].checked), true);
  await dismiss(); await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  assert.equal(count("audit/declined"), 1); assert.equal(count("accounts/apply"), 0);

  // Protected targets need the server's exact name. Cancel and double-click
  // cannot dispatch writes; apply uses the plan's immutable fingerprint.
  await click("Discard"); nextTyped = `${target.name}@%`;
  await review(); assert.equal(await page.$eval(".account-review .danger-solid", b => b.disabled), true);
  await page.type(".account-typed input", "wrong"); assert.equal(await page.$eval(".account-review .danger-solid", b => b.disabled), true);
  await page.$eval(".account-typed input", e => { const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; setter.call(e, ""); e.dispatchEvent(new Event("input", { bubbles: true })); });
  await page.type(".account-typed input", nextTyped);
  const readsBeforeApply = privilegeReads;
  await page.click(".account-review .danger-solid", { clickCount: 2 });
  await page.waitForSelector('[aria-label="Account change result"]');
  await page.waitForFunction(() => !document.querySelector(".account-review"));
  assert.equal(count("accounts/apply"), 1); assert.ok(privilegeReads > readsBeforeApply);
  assert.match(await h.text('[aria-label="Account change result"]'), /Added:.*GRANT SELECT, INSERT, UPDATE/s);
  nextTyped = "";

  // Switching identity leaves the old list grey. A reviewed write on A
  // cannot be sent through B even when B permits account changes.
  await page.waitForSelector(".account-matrix"); await review();
  const listsBeforeSwitch = state.seen.length;
  await page.evaluate(() => [...document.querySelectorAll(".test-probes button")].find(b => b.textContent === "Switch connection").click());
  await page.waitForFunction(() => document.querySelector(".account-review .danger-solid")?.disabled === true);
  assert.match(await h.text(".account-review"), /connection changed/); await dismiss();
  assert.equal(await page.$eval(".accounts-view", e => e.dataset.otherConnection), "true");
  assert.equal(state.seen.length, listsBeforeSwitch, "switching connections retains the grey report until Refresh");
  assert.equal(count("accounts/apply"), 1);
  await h.refresh(); await inspect();

  // Directly denied administration greys the action, without hiding it.
  actions = { ...actions, lock_account: { state: "denied", missing: ["CREATE USER"], reason: "Needs CREATE USER.", grey_out: true } };
  await click("Refresh privileges");
  await page.waitForFunction(() => document.querySelector('.account-actions [data-privilege-action="lock_account"]')?.getAttribute("aria-disabled") === "true");
  actions.lock_account = allowed; await click("Refresh privileges");

  // Account administration and grant controls remain keyboard reachable
  // on denial, and their descriptions name the missing privilege.
  actions.create_user = { ...denied, missing: ["CREATE USER"], need: "CREATE USER" };
  actions.grant = actions.revoke = { ...denied, missing: ["GRANT OPTION"], need: "GRANT OPTION" };
  await click("Refresh privileges");
  await page.waitForFunction(() => document.querySelector('[data-privilege-action="create_user"]')?.getAttribute("aria-disabled") === "true");
  assert.match(await page.$eval('[data-privilege-action="create_user"]', e => document.getElementById(e.getAttribute("aria-describedby")).textContent), /CREATE USER/);
  await page.focus('[data-privilege-action="create_user"]'); await page.keyboard.press("Enter");
  assert.equal(await page.$("dialog[open]"), null);
  const matrixInput = '.account-inspector .account-matrix input';
  assert.equal(await page.$eval(matrixInput, e => e.disabled), false);
  assert.equal(await page.$eval(matrixInput, e => e.getAttribute("aria-disabled")), "true");
  const checked = await page.$eval(matrixInput, e => e.checked);
  await page.focus(matrixInput); await page.keyboard.press("Space");
  assert.equal(await page.$eval(matrixInput, e => e.checked), checked);
  assert.equal(await page.$(".account-pending"), null);
  actions.create_user = actions.grant = actions.revoke = allowed;
  await click("Refresh privileges");
  await page.waitForFunction(() => document.querySelector('[data-privilege-action="create_user"]')?.getAttribute("aria-disabled") === "false");

  // Password input is uncontrolled and cleared as soon as the request
  // starts. A plan failure cannot leave it in a form or storage.
  planFailure = true; await page.click('.account-actions [data-privilege-action="set_password"]');
  await page.type('dialog input[type="password"]', "typed-only-secret"); await click("Review statements");
  await page.waitForFunction(() => document.querySelector("dialog [role=alert]")?.textContent.includes("account changed"));
  assert.equal(await page.$('dialog input[type="password"]'), null);
  assert.equal(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }).includes("typed-only-secret")), false);
  await page.keyboard.press("Escape"); planFailure = false;

  // A generated result is separate from the persistent change report.
  await page.click('.account-actions [data-privilege-action="set_password"]');
  await page.click("dialog .checkbox input"); await click("Review statements");
  await page.waitForSelector(".account-review"); await page.click(".account-review .danger-solid");
  await page.waitForSelector('[aria-label="Generated password"]', { visible: true });
  await page.waitForFunction(() => !document.querySelector(".account-review"));
  assert.match(await h.text('[aria-label="Generated password"]'), /shown-once-secret/);
  await click("Dismiss password"); assert.ok(!(await page.content()).includes("shown-once-secret"));

  // Typed passwords must cross plan and apply separately: the adapter keeps
  // only a non-secret request. Both fields clear immediately after sending.
  await page.click('.account-actions [data-privilege-action="set_password"]');
  await page.type('dialog input[type="password"]', "typed-apply-secret"); await click("Review statements");
  await page.waitForSelector(".account-review");
  assert.equal(await page.$eval(".account-review .danger-solid", e => e.disabled), true);
  await page.type('.account-review input[type="password"]', "typed-apply-secret");
  await page.click(".account-review .danger-solid"); await page.waitForFunction(() => !document.querySelector(".account-review"));
  assert.ok(!await page.$('[aria-label="Generated password"]'));
  assert.ok(!(await page.content()).includes("typed-apply-secret"));

  // Role defaults and grants are staged without issuing a write.
  await click("Roles");
  const beforeRoles = count("accounts/apply");
  await page.$$eval(".account-role-row label", labels => labels.find(l => l.querySelector(":scope > span")?.textContent === "Default").querySelector("input").click());
  assert.equal(count("accounts/apply"), beforeRoles);
  assert.match(await h.text(".account-pending"), /Set default roles/); await click("Discard");

  // Unknown outcome destroys the sent plan and never offers a retry.
  await click("Grants"); applyFailure = true; await review(); await page.click(".account-review .danger-solid");
  await page.waitForFunction(() => document.querySelector('.accounts-view [role="alert"]')?.textContent.includes("may have run"));
  // The alert can render a frame before the review sheet unmounts.
  await page.waitForFunction(() => !document.querySelector(".account-review"), { timeout: 5000 }); applyFailure = false;
  await h.refresh(); await inspect();

  // Mixed groups share a review, one typed guard, and one decline per plan.
  await tick("INSERT"); await page.click(".account-inspector > .inspector-block .account-checkbox input");
  nextTyped = `${target.name}@%`;
  const beforeDeclines = count("audit/declined");
  await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
  assert.equal(await page.$$eval(".account-review .account-review-group", rows => rows.length), 2);
  assert.equal(await page.$$eval(".account-typed input", rows => rows.length), 1);
  await dismiss(); await page.waitForFunction(() => !document.querySelector(".account-review"));
  for (let i = 0; i < 50 && count("audit/declined") < beforeDeclines + 2; i++) await pause(20);
  assert.equal(count("audit/declined"), beforeDeclines + 2);
  const beforeBatch = count("accounts/apply"), beforePlans = count("accounts/plan");
  await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
  await page.type(".account-typed input", nextTyped);
  await page.click(".account-review .danger-solid"); await page.waitForFunction(() => !document.querySelector(".account-review"));
  assert.equal(count("accounts/apply"), beforeBatch + 2);
  assert.equal(count("accounts/plan"), beforePlans + 4);
  assert.match(await h.text('[aria-label="Account change result"]'), /2 groups applied, 0 failed, 0 not sent/);
  assert.equal(await page.$(".account-pending"), null);
  assert.equal(state.other.filter(r => r.path === "/api/accounts/plan").some(r => r.body?.operation === "changes"), false);
  nextTyped = "";

  async function stageThreeGroups() {
    await page.select('select[aria-label="Grant level"]', "global");
    await tick("INSERT"); await page.click(".account-inspector > .inspector-block .account-checkbox input");
    await page.select('select[aria-label="Grant level"]', "table");
    await tick("SELECT");
  }
  // A planning error stays beside its group. Remove only that group, then
  // the remaining reviewed statements can be applied or cancelled together.
  await stageThreeGroups(); planFailureAt = planCount + 2;
  const beforePlanError = count("accounts/apply");
  await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
  assert.equal(await page.$$eval(".account-review .account-review-group", rows => rows.length), 3);
  assert.match(await h.text('[aria-label="Change group 2"]'), /account changed/);
  assert.equal(await page.$eval(".account-review .danger-solid", e => e.disabled), true);
  await click("Remove group 2");
  assert.equal(await page.$$eval(".account-review .account-review-group", rows => rows.length), 2);
  assert.equal(await page.$eval(".account-review .danger-solid", e => e.disabled), false);
  await dismiss(); planFailureAt = -1;
  assert.equal(count("accounts/apply"), beforePlanError); await click("Discard");

  // The second refusal preserves the first result and never sends the third.
  await stageThreeGroups(); applyRefusalAt = applyCount + 2;
  const beforePartial = count("accounts/apply");
  await page.locator("button ::-p-text(Review changes)").click(); await page.waitForSelector(".account-review");
  await page.click(".account-review .danger-solid"); await page.waitForFunction(() => !document.querySelector(".account-review"));
  assert.equal(count("accounts/apply"), beforePartial + 2);
  assert.deepEqual(await page.$$eval("[data-group-status]", rows => rows.map(r => r.dataset.groupStatus)), ["applied", "failed", "not_sent"]);
  assert.match(await h.text('[aria-label="Account change result"]'), /1 group applied, 1 failed, 1 not sent/);
  assert.equal(await page.$(".account-pending"), null); applyRefusalAt = -1;

  // Multiple refresh clicks join one request. Focus within 60s does not
  // reread; reconnect and an account change always refresh.
  privilegeDelay = 200; const n = privilegeReads;
  await page.evaluate(() => { const b = [...document.querySelectorAll("button")].find(b => b.textContent === "Refresh privileges"); b.click(); b.click(); b.click(); });
  await pause(250); assert.equal(privilegeReads, n + 1); privilegeDelay = 0;
  await page.evaluate(() => window.dispatchEvent(new Event("focus"))); await pause(50); assert.equal(privilegeReads, n + 1);
  await click("Reconnect"); await pause(100); assert.equal(privilegeReads, n + 2);
  await click("Account changed"); await pause(100); assert.equal(privilegeReads, n + 3);

  // Explicit fresh=true replaces an active pre-change read, rather than
  // joining it. Its late denial cannot overwrite the fresh allowed answer.
  privilegeDelay = 200; actions.lock_account = denied;
  await click("Refresh privileges"); await pause(30);
  privilegeDelay = 0; actions.lock_account = allowed;
  await click("Fresh privileges"); await pause(250);
  assert.equal(await page.$eval('[data-privilege-action="lock_account"]', e => e.getAttribute("aria-disabled")), "false");

  // A late plan after a switch is declined, never opened on the new server.
  await page.select('select[aria-label="Grant level"]', "global");
  planDelay = 200; await tick("INSERT"); await page.locator("button ::-p-text(Review changes)").click();
  await pause(30); await click("Switch connection"); await pause(250);
  assert.equal(await page.$(".account-review"), null); planDelay = 0;
  await h.refresh(); await inspect();

  await click("Toggle read-only"); await h.refresh(); await inspect();
  assert.equal(await page.$eval('[data-privilege-action="create_user"]', e => e.getAttribute("aria-disabled")), "true");
  assert.equal(await page.$eval(".account-matrix input", e => e.getAttribute("aria-disabled")), "true");
  await click("Toggle read-only"); await h.refresh(); await inspect();

  // MariaDB uses the same random-password control and once-only result.
  engine = "mariadb"; list.engine = engine; list.version = "11.4.13-MariaDB";
  nextWarnings = ["A password validation plugin is active (simple_password_check).", "This protects the binary log only. The general and slow logs can contain the typed password."];
  await h.refresh(); await inspect();
  await page.click('.account-actions [data-privilege-action="set_password"]');
  assert.match(await h.text("dialog[open]"), /binary log only/);
  await page.click("dialog .checkbox input"); await click("Review statements");
  await page.waitForSelector(".account-review"); assert.match(await h.text(".account-review"), /simple_password_check/);
  await page.click(".account-review .danger-solid"); await page.waitForSelector('[aria-label="Generated password"]');
  await page.waitForFunction(() => !document.querySelector(".account-review"));
  await click("Dismiss password");

  // Account names use measured, capped widths. Layout changes persist under
  // the stable table id even when the unreadable report swaps its columns.
  await backToAccounts();
  const accountWidth = await page.$eval('.accounts-view th[data-col="account"]', th => th.getBoundingClientRect().width);
  assert.ok(accountWidth <= 360, "Account is content-sized instead of consuming spare table width");
  await page.focus('.accounts-view [role="separator"][aria-label="Width of Account"]');
  await page.keyboard.press("ArrowRight");
  await page.waitForFunction(() => window.accountColumns["accounts.list"]?.widths?.account > 0);
  const savedWidth = await page.evaluate(() => window.accountColumns["accounts.list"].widths.account);
  const unreadableReason = "Only the connected account is listed: listing users and roles needs SELECT on the mysql database.";
  state.queue.push({ status: 200, body: { ...list, complete: false, warnings: [unreadableReason],
    accounts: [{ name: "limited", host: "%", connected: true }], role_grants: [], default_roles: [] } });
  await h.refresh();
  await page.waitForFunction(() => document.querySelector('.accounts-view .summary-line')?.textContent.includes("Not judged"));
  assert.equal(await h.text(".accounts-view .summary-text"), unreadableReason);
  assert.deepEqual(await page.$$eval('.accounts-view thead th[data-col]', ths => ths.map(th => th.dataset.col)), ["account", "details"]);
  assert.equal(await h.text('.accounts-view td[data-col="details"]'), "Not readable");
  assert.doesNotMatch(await h.text('.accounts-view tbody'), /Unknown|Not checked/);
  await h.refresh();
  await page.waitForSelector('.accounts-view th[data-col="plugin"]');
  assert.equal(await page.evaluate(() => window.accountColumns["accounts.list"].widths.account), savedWidth);

  if (process.env.PERF_SHOTS) await fs.mkdir(process.env.PERF_SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(h.shots, "accounts-desktop.png"), fullPage: true });
  await h.narrow("accounts-phone.png");
  assert.deepEqual(h.errors, []);
  console.log("accounts: gates, draft grants, review, audit, connection binding, passwords, roles, unknown outcome, refresh and 390px passed");
} finally { if (h) await h.close(); }

// Keep the new grant workflows in the ordinary accounts suite as well.
if (!grantMode) execFileSync(process.execPath, [fileURLToPath(import.meta.url), "--grants"], { stdio: "inherit", env: process.env });
