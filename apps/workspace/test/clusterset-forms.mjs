// ClusterSet action forms (ClusterSetActionForm.tsx): the routing form maps a
// stored target_cluster group UUID back to the cluster name (and never sends a
// UUID), removal offers force only when the report shows the target
// unreachable, promotion offers invalidating the replica clusters the report
// shows unreachable, and dissolve offers force under the same condition. Uses
// the real built workspace and Python server on a throwaway HOME, with
// /api/live/cluster* intercepted.
//
// The uuid field on a ClusterSet cluster is the contract the backend lane
// emits (cluster.py _use_clusterset_metadata): MySQL Shell stores
// target_cluster as the cluster's group UUID (mysql-shell
// modules/adminapi/common/router.cc:382-406), and the form maps it back.
import assert from "node:assert/strict";
import { load, open, pause } from "./perf-harness.mjs";
import { openConnectionPicker } from "./chrome-helpers.mjs";

const base = await load("cluster-ic-8.4-clusterset-primary.json");
const DR_UUID = "dddddddd-2222-11f0-8a00-0242ac130002";

const csAction = (id, label, extra = {}) => ({
  schema_version: "cluster-action-1.1", id, label, scope: "clusterset", kind: "risky", via: "mysqlsh",
  danger: true, description: label, member_key: null, member_name: null, member_uuid: null,
  cluster_name: null, enabled: true, why_not: null, fix: null, missing_privileges: [], grant_sql: null,
  privileges_unreadable: false, target: { self: true, member_name: "mf-ic-1:3306" }, ...extra,
});
const gate = { enabled: true, why_not: null, fix: null };
const routingGates = {
  target_cluster: { ...gate }, invalidated_cluster_policy: { ...gate }, stats_updates_frequency: { ...gate },
  use_replica_primary_as_rw: { ...gate }, read_only_targets: { ...gate },
};
const routingEntry = () => csAction("set_routing_option", "Change ClusterSet routing defaults…", { danger: false, options: routingGates });
const removeEntries = () => [
  csAction("remove_cluster:drcluster", "Remove and dissolve the cluster drcluster…"),
  csAction("remove_cluster_keep:drcluster", "Remove drcluster, keep the cluster running…"),
];

function makeReport(mutate) {
  const report = JSON.parse(JSON.stringify(base));
  report.actions = (report.actions || []).filter((a) =>
    !/^(remove_cluster|remove_cluster_keep|set_routing_option|set_primary_cluster|force_primary_cluster|dissolve_clusterset)(:|$)/.test(a.id));
  mutate(report);
  return report;
}

const plans = [];
const profiles = [
  { id: "local", name: "Local member", host: "mf-ic-1" },
  { id: "remote", name: "DR member", host: "mf-dr-1" },
].map((p) => ({ ...p, port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "",
  group: "production", type: "production", read_only: false, has_saved_password: true }));
const h = await open("/api/live/cluster", "clusterset-forms", {
  "/api/profiles/list": { status: 200, body: { items: profiles,
    secret_store: { backend: "vault", label: "myflames encrypted vault", available: true } } },
  "/api/connect": (body) => {
    assert.equal(body.group_name, "production");
    return { status: 200, body: { version: "8.4.3", engine: "mysql", flavor: "mysql", product: "MySQL",
      databases: [], schemas: [], group_connection: { group_name: "production",
        effective_safety: { type: "production", read_only: false }, active_member_profile_id: "local", primary_profile_id: "local",
        members: profiles.map((p, i) => ({ profile_id: p.id, profile_name: p.name, host: p.host, port: p.port,
          safety: { type: p.type, read_only: false }, server_uuid: null, role: i ? "SECONDARY" : "PRIMARY", state: null, reachable: true })) } } };
  },
  "/api/live/cluster/plan": (body) => {
    plans.push(body);
    return {
      status: 200,
      body: {
        schema_version: "cluster-action-1.1", action: body.action, scope: "clusterset", via: "mysqlsh",
        eyebrow: "ClusterSet", title: `${body.action}?`, confirm_label: "Confirm", target_text: "mfcs",
        description: "Test plan.", statements: ["call();"], facts: [], warnings: [], typed_guard: null,
        undo: null, next: null, expect: {}, timeout_s: 300, checks: [],
        policy: {
          action: "confirm", tone: "danger", title: "Production connection",
          text: "Production connection asks before every write.", connection_type: "production", read_only: false,
        },
        review_token: "tok",
      },
    };
  },
  "/api/audit/declined": { status: 200, body: { audit_id: "d1", status: "declined" } },
}, { fallback: { status: 200, body: base } });

const { page, state } = h;
const view = '.server-view[data-view="cluster"]';
const formSelector = "dialog.clusterset-form[open]";

async function refreshTo(report) {
  const before = state.seen.length;
  state.fallback = { status: 200, body: report };
  await h.refresh();
  for (let i = 0; i < 100 && state.seen.length === before; i++) await pause(20);
  assert.equal(state.seen.length, before + 1, "Refresh sends one read");
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`), {}, view);
}

const clickSetAction = (label) => page.evaluate((sel, text) => {
  const btn = [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
    .find((b) => b.textContent.trim() === text);
  if (!btn) throw new Error(`Missing set action button: ${text}`);
  btn.click();
}, view, label);

const clickFormConfirm = () => page.$eval(formSelector, (d) => [...d.querySelectorAll(".sheet-buttons button")].pop().click());
const cancelForm = () => page.$eval(formSelector, (d) => [...d.querySelectorAll(".sheet-buttons button")][0].click());

const closeReview = async () => {
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
};

/** The form's current routing selects: [option, value]. */
const routingState = () => page.$eval(formSelector, (d) => {
  const selects = [...d.querySelectorAll("select")];
  const confirm = [...d.querySelectorAll(".sheet-buttons button")].pop();
  return {
    option: selects[0]?.value ?? null,
    value: selects[1]?.value ?? null,
    confirmDisabled: !!confirm?.disabled,
    hint: d.querySelector(".hint")?.textContent || "",
  };
});

try {
  // These forms need peer reads. Open the Group connection explicitly so
  // their review requests carry the saved route and group consent.
  await page.click('.main-nav button[data-place="cluster"]');
  await page.waitForSelector(`${view} .cluster-actions`, { visible: true });
  await page.waitForSelector(".window-toolbar .connection-pill", { visible: true });
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection production"]');
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection production"));
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`), {}, view);

  // 1. A stored target_cluster holding the cluster's group UUID (even uppercase)
  // pre-fills as the cluster NAME, and the plan request carries the name (never the UUID).
  await refreshTo(makeReport((r) => {
    const dr = r.clusterset.clusters.find((c) => c.name === "drcluster");
    dr.uuid = DR_UUID;
    r.clusterset.routing_options = { stored_configuration: true, options: { target_cluster: DR_UUID.toUpperCase() } };
    r.actions.push(routingEntry());
  }));
  let plansBefore = plans.length;
  await clickSetAction("Change ClusterSet routing defaults…");
  await page.waitForSelector(formSelector, { visible: true });
  let routing = await routingState();
  assert.equal(routing.option, "target_cluster");
  assert.equal(routing.value, "drcluster", "the stored group UUID pre-fills as its cluster name");
  assert.equal(routing.confirmDisabled, false, "a mapped target is ready to review");
  assert.doesNotMatch(routing.hint, /cannot match/, "a mapped UUID shows no unmapped hint");
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBefore + 1);
  assert.deepEqual(plans.at(-1).parameters, { option: "target_cluster", value: "drcluster" },
    "the plan request sends the cluster name, never the stored UUID");
  await closeReview();

  // 1b. Per-router pinned target_cluster UUID maps via clusters[].uuid too.
  await refreshTo(makeReport((r) => {
    const dr = r.clusterset.clusters.find((c) => c.name === "drcluster");
    dr.uuid = DR_UUID;
    r.clusterset.routers = [{
      id: 7, name: "router-1::system", address: "router-1", version: "8.4.10",
      cluster_name: "mfcluster", options: { target_cluster: DR_UUID }, stored_configuration: true,
    }];
    r.actions.push(csAction("set_routing_option:router-1::system", "Change routing for router-1::system…", { danger: false, options: routingGates }));
  }));
  plansBefore = plans.length;
  await clickSetAction("Change routing for router-1::system…");
  await page.waitForSelector(formSelector, { visible: true });
  routing = await routingState();
  assert.equal(routing.value, "drcluster", "per-router pinned UUID maps to its cluster name");
  assert.equal(routing.confirmDisabled, false);
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBefore + 1);
  assert.equal(plans.at(-1).action, "set_routing_option:router-1::system");
  assert.deepEqual(plans.at(-1).parameters, { option: "target_cluster", value: "drcluster" });
  await closeReview();

  // 2. A stored UUID this report cannot map (e.g. clusters[].uuid absent) never
  // pre-selects primary or lets the user accidentally unpin: confirm stays disabled
  // until an explicit choice is made, and the plan sends that explicit choice.
  await refreshTo(makeReport((r) => {
    r.clusterset.routing_options = { stored_configuration: true, options: { target_cluster: DR_UUID } };
    r.actions.push(routingEntry());
  }));
  await clickSetAction("Change ClusterSet routing defaults…");
  await page.waitForSelector(formSelector, { visible: true });
  routing = await routingState();
  assert.equal(routing.value, "", "an unmappable stored UUID starts unselected instead of pre-selecting primary");
  assert.equal(routing.confirmDisabled, true, "confirm stays disabled until the user explicitly chooses a target");
  assert.match(routing.hint, /dddddddd-2222-11f0-8a00-0242ac130002/, "the hint names the stored value");
  assert.match(routing.hint, /Choose primary or a cluster by name/, "the hint asks for an explicit choice");
  plansBefore = plans.length;
  await clickFormConfirm();
  await pause(100);
  assert.equal(plans.length, plansBefore, "clicking disabled confirm sends no plan");
  // Explicitly choosing primary enables confirm and sends "primary" (never the UUID).
  const targetSelectId = await page.$eval(formSelector, (d) => [...d.querySelectorAll("select")][1].id);
  await page.select(`${formSelector} select[id="${targetSelectId}"]`, "primary");
  routing = await routingState();
  assert.equal(routing.value, "primary");
  assert.equal(routing.confirmDisabled, false, "explicitly selecting primary enables confirm");
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBefore + 1);
  assert.deepEqual(plans.at(-1).parameters, { option: "target_cluster", value: "primary" });
  await closeReview();

  // 3. A stored cluster name pre-fills unchanged.
  await refreshTo(makeReport((r) => {
    r.clusterset.routing_options = { stored_configuration: true, options: { target_cluster: "drcluster" } };
    r.actions.push(routingEntry());
  }));
  await clickSetAction("Change ClusterSet routing defaults…");
  await page.waitForSelector(formSelector, { visible: true });
  routing = await routingState();
  assert.equal(routing.value, "drcluster", "a stored cluster name pre-fills unchanged");
  await cancelForm();
  await page.waitForFunction(() => !document.querySelector("dialog.clusterset-form[open]"));

  // 4. Removal of a cluster the report shows unreachable offers force; without
  // the choice no parameters are sent, with it the plan request carries force.
  await refreshTo(makeReport((r) => {
    const dr = r.clusterset.clusters.find((c) => c.name === "drcluster");
    dr.read = "failed";
    dr.route = null;
    dr.read_error = "DR member could not be read for cluster drcluster: the server did not answer.";
    r.actions.push(...removeEntries());
  }));
  await clickSetAction("Remove drcluster from the ClusterSet…");
  await page.waitForSelector(formSelector, { visible: true });
  const forceBox = await page.$eval(formSelector, (d) => {
    const label = [...d.querySelectorAll("label.checkbox")].find((l) => /Force/.test(l.textContent));
    return label ? { text: label.textContent.replace(/\s+/g, " ").trim(), checked: label.querySelector("input").checked } : null;
  });
  assert.ok(forceBox, "force is offered for a cluster the report shows unreachable");
  assert.match(forceBox.text, /remove without contacting drcluster/);
  assert.equal(forceBox.checked, false, "force starts unchecked");
  assert.match(await page.$eval(formSelector, (d) => d.textContent), /stale metadata and replication configuration/,
    "the force consequence is one sentence beside the choice");
  await page.$eval(`${formSelector} .clusterset-choice[data-choice="dissolve"] .button`, (b) => b.click());
  plansBefore = plans.length;
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBefore + 1);
  assert.equal(plans.at(-1).action, "remove_cluster:drcluster");
  assert.ok(!("parameters" in plans.at(-1)), "removal without force sends no parameters");
  await closeReview();

  await clickSetAction("Remove drcluster from the ClusterSet…");
  await page.waitForSelector(formSelector, { visible: true });
  await page.$eval(`${formSelector} .clusterset-choice[data-choice="dissolve"] .button`, (b) => b.click());
  await page.$eval(formSelector, (d) => [...d.querySelectorAll("label.checkbox")].find((l) => /Force/.test(l.textContent)).querySelector("input").click());
  plansBefore = plans.length;
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBefore + 1);
  assert.deepEqual(plans.at(-1).parameters, { force: true }, "the force choice reaches the plan request");
  await closeReview();

  // 5. Removal of a reachable cluster offers no force control at all.
  await refreshTo(makeReport((r) => { r.actions.push(...removeEntries()); }));
  await clickSetAction("Remove drcluster from the ClusterSet…");
  await page.waitForSelector(formSelector, { visible: true });
  const anyCheckbox = await page.$eval(formSelector, (d) => d.querySelectorAll("label.checkbox").length);
  assert.equal(anyCheckbox, 0, "no force control for a reachable cluster");
  await cancelForm();
  await page.waitForFunction(() => !document.querySelector("dialog.clusterset-form[open]"));

  // 6. Promotion opens the Promote form (switchover and failover): it offers
  // invalidating each replica cluster this read shows unreachable, never the
  // promotion target, and the plan request carries invalidateReplicaClusters
  // only when chosen.
  await refreshTo(makeReport((r) => {
    const lost = JSON.parse(JSON.stringify(r.clusterset.clusters.find((c) => c.name === "drcluster")));
    lost.name = "lostcluster";
    lost.local = false;
    lost.read = "failed";
    lost.route = null;
    lost.read_error = "Lost member could not be read for cluster lostcluster: the server did not answer.";
    r.clusterset.clusters.push(lost);
    r.actions.push(csAction("set_primary_cluster:drcluster", "Make drcluster the primary cluster…", { cluster_name: "drcluster" }));
    r.actions.push(csAction("force_primary_cluster:drcluster", "Fail over to drcluster…", { cluster_name: "drcluster" }));
  }));
  await clickSetAction("Make drcluster the primary cluster…");
  await page.waitForSelector(formSelector, { visible: true });
  let promoteText = await page.$eval(formSelector, (d) => d.textContent);
  assert.match(promoteText, /ClusterSet\.setPrimaryCluster/);
  assert.match(promoteText, /Invalidate lostcluster/);
  assert.doesNotMatch(promoteText, /Invalidate drcluster/, "the promotion target is never an invalidation candidate");
  await page.$eval(formSelector, (d) => [...d.querySelectorAll("label.checkbox")]
    .find((l) => /Invalidate lostcluster/.test(l.textContent)).querySelector("input").click());
  plansBefore = plans.length;
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBefore + 1);
  assert.equal(plans.at(-1).action, "set_primary_cluster:drcluster");
  assert.deepEqual(plans.at(-1).parameters, { invalidateReplicaClusters: ["lostcluster"] },
    "the invalidation choice reaches the plan request");
  await closeReview();

  // Failover uses the same form with its own eyebrow; nothing chosen sends
  // no parameters.
  await clickSetAction("Fail over to drcluster…");
  await page.waitForSelector(formSelector, { visible: true });
  promoteText = await page.$eval(formSelector, (d) => d.textContent);
  assert.match(promoteText, /ClusterSet\.forcePrimaryCluster/);
  assert.match(promoteText, /Invalidate lostcluster/);
  plansBefore = plans.length;
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.at(-1).action, "force_primary_cluster:drcluster");
  assert.ok(!("parameters" in plans.at(-1)), "failover without an invalidation choice sends no parameters");
  await closeReview();

  // With every replica reachable the form is skipped: no invalidation is
  // offered and the review plans directly, without parameters.
  await refreshTo(makeReport((r) => {
    r.actions.push(csAction("set_primary_cluster:drcluster", "Make drcluster the primary cluster…", { cluster_name: "drcluster" }));
  }));
  plansBefore = plans.length;
  await clickSetAction("Make drcluster the primary cluster…");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(await page.$(formSelector), null, "no promotion form without an unreachable replica");
  assert.equal(plans.length, plansBefore + 1);
  assert.ok(!("parameters" in plans.at(-1)), "no invalidation offered, none sent");
  await closeReview();

  // 7. Dissolve opens the Dissolve form: force is offered only when this read
  // shows a cluster it could not reach, and the choice reaches the plan
  // request.
  await refreshTo(makeReport((r) => {
    const dr = r.clusterset.clusters.find((c) => c.name === "drcluster");
    dr.read = "failed";
    dr.route = null;
    dr.read_error = "DR member could not be read for cluster drcluster: the server did not answer.";
    r.actions.push(csAction("dissolve_clusterset", "Dissolve ClusterSet…"));
  }));
  await clickSetAction("Dissolve ClusterSet…");
  await page.waitForSelector(formSelector, { visible: true });
  const dissolveForce = await page.$eval(formSelector, (d) => {
    const label = [...d.querySelectorAll("label.checkbox")].find((l) => /Force/.test(l.textContent));
    return label ? { text: label.textContent.replace(/\s+/g, " ").trim(), checked: label.querySelector("input").checked } : null;
  });
  assert.ok(dissolveForce, "force is offered when a cluster is unreachable");
  assert.match(dissolveForce.text, /Force: dissolve without reaching drcluster/);
  assert.equal(dissolveForce.checked, false, "force starts unchecked");
  await page.$eval(formSelector, (d) => [...d.querySelectorAll("label.checkbox")]
    .find((l) => /Force/.test(l.textContent)).querySelector("input").click());
  plansBefore = plans.length;
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.at(-1).action, "dissolve_clusterset");
  assert.deepEqual(plans.at(-1).parameters, { force: true }, "the force choice reaches the plan request");
  await closeReview();

  // Every cluster reachable: the form is skipped and the review plans
  // directly, without parameters.
  await refreshTo(makeReport((r) => {
    r.actions.push(csAction("dissolve_clusterset", "Dissolve ClusterSet…"));
  }));
  plansBefore = plans.length;
  await clickSetAction("Dissolve ClusterSet…");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(await page.$(formSelector), null, "no dissolve form when every cluster answered");
  assert.equal(plans.length, plansBefore + 1);
  assert.ok(!("parameters" in plans.at(-1)), "dissolve without force sends no parameters");
  await closeReview();

  assert.deepEqual(h.errors, [], `unexpected page errors: ${h.errors.join("; ")}`);
  console.log("ClusterSet forms: target_cluster UUID prefill mapping, unmapped fallback, remove force gating, promote invalidation and dissolve force passed.");
} finally {
  await h.close();
}
