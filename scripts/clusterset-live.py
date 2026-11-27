#!/usr/bin/env python3
# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""Run one reviewed ClusterSet action through the workspace's real endpoints.

The host wrapper uses the existing mf-cs-runner image on mf-cs-net, mounts
this checkout read-only, and writes password-free evidence in local/cs-live.
It never starts or stops a database, resets the lab, or retries a write.
Credentials must be supplied as MF_ADMIN_USER and MF_ADMIN_PASSWORD in the
environment, not arguments. ROOT_PASSWORD and MF_READ_PASSWORD, if present,
are only included in output redaction and are not forwarded to the runner.

Examples, with credentials already exported:
  python3 scripts/clusterset-live.py --step check
  python3 scripts/clusterset-live.py --step routing-csb
  python3 scripts/clusterset-live.py --step failover-csb --source b1
  python3 scripts/clusterset-live.py --step rejoin-a2 --source a1

Run in this order, doing the indicated lab operations separately:
  check; routing-csb; routing-primary; switchover-csb; switchover-csa;
  remove-keep (reset); remove-dissolve (reset); create-csc;
  (stop a1/a2) failover-csb (start a1/a2); rejoin-csa;
  rejoin-a2 (from a1 after csa recovery); dissolve (reset).
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid


NODES = ("a1", "a2", "b1", "b2", "c1")
GROUP = "ClusterSet live lab"
STEPS = {
    "check": ("check_clusterset", {}),
    "routing-csb": ("set_routing_option", {"option": "target_cluster", "value": "csb"}),
    "routing-primary": ("set_routing_option", {"option": "target_cluster", "value": "primary"}),
    "switchover-csb": ("set_primary_cluster:csb", {"timeout": 60}),
    "switchover-csa": ("set_primary_cluster:csa", {"timeout": 60}),
    "remove-keep": ("remove_cluster_keep:csb", {"timeout": 60}),
    "remove-csc-keep": ("remove_cluster_keep:csc", {"timeout": 60}),
    "remove-csc-dissolve": ("remove_cluster:csc", {"timeout": 60}),
    "remove-dissolve": ("remove_cluster:csb", {"timeout": 60}),
    "create-csc": ("create_replica_cluster:mf-cs-c1",
                   {"name": "csc", "recoveryMethod": "clone", "recoveryProgress": 1, "timeout": 120}),
    "create-csc-incremental": ("create_replica_cluster:mf-cs-c1",
                   {"name": "csc", "recoveryMethod": "incremental", "recoveryProgress": 1, "timeout": 120}),
    "switchover-csc": ("set_primary_cluster:csc", {"timeout": 60}),
    "rejoin-csb": ("rejoin_cluster:csb", {}),
    "failover-csb": ("force_primary_cluster:csb", {"timeout": 60}),
    "rejoin-csa": ("rejoin_cluster:csa", {}),
    "rejoin-a2": ("rejoin_instance", {}),
    "dissolve": ("dissolve_clusterset", {"timeout": 60}),
    "read": (None, {}),
}
PROOF_PREFIX = "MFCS_PROOF="


def secrets():
    return [os.environ[name] for name in
            ("MF_ADMIN_PASSWORD", "ROOT_PASSWORD", "MF_READ_PASSWORD") if os.environ.get(name)]


def clean_text(value):
    for secret in secrets():
        value = value.replace(secret, "[REDACTED]")
    return value


def clean(value):
    """Defence in depth before any server output or request reaches the log."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if str(key).lower() in
                ("password", "review_token", "secret", "token") else clean(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return clean_text(value) if isinstance(value, str) else value


def event(name, value):
    print(json.dumps({"event": name, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "value": clean(value)}, ensure_ascii=True), flush=True)


def connection(store, node):
    from myflames.workspace_store import ENDPOINT_FIELDS
    profile = store.get("profile", "mf-cs-" + node)
    value = {key: profile[key] for key in ENDPOINT_FIELDS if profile.get(key) not in (None, "")}
    value.update(profile_id=profile["id"], group_name=GROUP)
    return value


def select_action(report, step):
    action, parameters = STEPS[step]
    entries = report.get("actions") or []
    if step == "rejoin-a2":
        members = [member for member in report.get("members") or []
                   if member.get("address") == "mf-cs-a2:3306"]
        if len(members) != 1:
            raise RuntimeError("Expected exactly one csa member at mf-cs-a2:3306.")
        entries = members[0].get("actions") or []
    if step.startswith("routing-"):
        # Exercise a Router-specific override, not the set's global default.
        routers = (report.get("clusterset") or {}).get("routers") or []
        names = sorted(router["name"] for router in routers if router.get("name"))
        if not names:
            raise RuntimeError("The live report did not expose a registered Router name.")
        action += ":" + names[0]
    entry = next((item for item in entries if item.get("id") == action), None)
    if entry is None:
        raise RuntimeError("The action is absent from the live catalog: " + action)
    if not entry.get("enabled"):
        raise RuntimeError("The live catalog refused %s: %s %s" %
                           (action, entry.get("why_not") or "", entry.get("fix") or ""))
    return action, dict(parameters), entry


def outcome_summary(step, action, result):
    classification = result.get("outcome_class") or result.get("verification_status") or result.get("outcome")
    after = result.get("after") or {}
    verification = result.get("verification") or after.get("verification") or {}
    if not classification and isinstance(verification, dict):
        classification = verification.get("outcome") or verification.get("status")
    classification = classification or result.get("status") or "unverified"
    facts = (verification.get("facts") if isinstance(verification, dict) else verification) or result.get("effect")
    facts = facts or after or result.get("message") or "No verification facts returned."
    counts = {state: sum(1 for fact in verification.get("facts") or [] if fact.get("status") == state)
              for state in ("verified", "missing", "mismatch")} if isinstance(verification, dict) else {}
    return {"step": step, "action": action, "outcome_class": classification,
            "status": result.get("status"), "verification_facts": facts,
            "verification_counts": counts,
            "message": result.get("message"), "audit_id": result.get("audit_id")}


def routing_change(active, password, value):
    """Deliberate out-of-band lab change for the stale-review refusal proof.

    Use the backend's private option-file writer, keeping credentials off argv
    and stdin. This is separate from the reviewed action being tested.
    """
    from myflames import lifecycle
    import shutil
    path = shutil.which("mysqlsh")
    if not path:
        raise RuntimeError("MySQL Shell is unavailable for the routing drift proof.")
    options = dict(active, password=password)
    tool = {"name": "mysqlsh", "path": path, "engine": "mysql"}
    script = 'dba.getClusterSet().setRoutingOption("stats_updates_frequency", %s);\n' % json.dumps(value)
    event("routing_drift_call", {"statement": script.strip()})
    with lifecycle._credentials(options, tool) as (argv, env):
        result = subprocess.run(argv + ["--js", "--no-wizard", "--quiet-start=2"],
                                input=script, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=env, timeout=120)
    event("routing_drift_result", {"returncode": result.returncode,
                                    "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode:
        raise RuntimeError("The out-of-band routing change failed; see its redacted evidence.")


def inside(args):
    """Use a disposable store but preserve all real consent and review gates."""
    root = str(Path(__file__).resolve().parent.parent)
    sys.path.insert(0, root)
    password = os.environ.get("MF_ADMIN_PASSWORD")
    user = os.environ.get("MF_ADMIN_USER") or "mf_admin"
    if not password:
        raise RuntimeError("Missing MF_ADMIN_PASSWORD in the environment.")
    os.environ["MYFLAMES_SECRET_BACKEND"] = "vault"
    # Each foreground runner has a fresh home. No user's saved connections,
    # secrets or mysqlsh startup files can take part in the live proof.
    home = tempfile.mkdtemp(prefix="myflames-cs-live-")
    os.environ["HOME"] = os.environ["USERPROFILE"] = home
    from myflames import dbops, ui
    from myflames.workspace_store import WorkspaceStore
    store = WorkspaceStore()
    workspace = types.SimpleNamespace(store=store)
    for node in NODES:
        host = "mf-cs-" + node
        store.save("profile", {"name": host, "host": host, "port": 3306, "user": user,
                               "group": GROUP, "type": "development", "ssl_mode": "PREFERRED"},
                   host, password=password, remember=True)

    source = args.source or ("b1" if args.step in ("failover-csb", "rejoin-csa", "dissolve") else "a1")
    if args.read_only:
        store.set_profile_safety("mf-cs-" + source, read_only=True)
        event("saved_connection_safety", {"profile_id": "mf-cs-" + source,
                                           "read_only": store.get("profile", "mf-cs-" + source)["read_only"]})
    if args.step.startswith("create-csc"):
        # Ordinary ClusterSet reads do not probe unclassified group members;
        # read the standalone candidate once in this group so its identity is known.
        preread = ui.api_response("/api/live/cluster", {"connection": connection(store, "c1")}, workspace)
        event("preread", preread)
    active = connection(store, source)
    report = ui.api_response("/api/live/cluster", {"connection": active}, workspace)
    event("before", report)
    if args.step == "read":
        result = {"status": "read", "outcome_class": "read", "after": report}
        proof = outcome_summary(args.step, "/api/live/cluster", result)
        print(PROOF_PREFIX + json.dumps(clean(proof), ensure_ascii=True), flush=True)
        return 0

    action, parameters, entry = select_action(report, args.step)
    if args.parameters:
        parameters.update(json.loads(args.parameters))
    route = (entry.get("target") or {}).get("profile_id")
    request = {"connection": active, "action": action, "parameters": parameters}
    if entry.get("member_key"):
        request["member_key"] = entry["member_key"]
    if route:
        request["target_profile_id"] = route
    plan = ui.api_response("/api/live/cluster/plan", request, workspace)
    event("plan", plan)
    reviewed = dict(plan)
    token = reviewed.pop("review_token")
    progress_id = uuid.uuid4().hex
    apply = dict(request, plan=reviewed, review_token=token, confirmed=True,
                 typed=plan.get("typed_guard"), progress_id=progress_id)
    event("confirmation", {"typed": apply["typed"], "confirmed": True})
    before_audit = {record["id"] for record in dbops.audit_report(store)["records"]}
    previous_routing = None
    if args.routing_drift:
        previous_routing = plan["expect"]["routing_configuration"]["defaults"]["options"].get(
            "stats_updates_frequency")
        routing_change(active, password, 1 if previous_routing != 1 else 2)
        drifted = ui.api_response("/api/live/cluster", {"connection": active}, workspace)
        event("routing_drift_read", drifted)
    # A single action call runs to completion. Polling is read-only and never
    # retries the reviewed write, even when Shell returns an uncertain result.
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(ui.api_response, "/api/live/cluster/action", apply, workspace)
            last = None
            heartbeat = time.monotonic()
            while not pending.done():
                snapshot = ui.api_response("/api/live/action/progress", {"progress_id": progress_id}, workspace)
                comparable = {key: value for key, value in snapshot.items() if key not in ("elapsed_s", "waiting_s")}
                now = time.monotonic()
                if comparable != last or now - heartbeat >= 30:
                    event("progress", snapshot)
                    last, heartbeat = comparable, now
                time.sleep(1)
            result = pending.result()
    finally:
        if args.routing_drift:
            # Restore only the lab mutation, never retry the reviewed action.
            routing_change(active, password, previous_routing)
            event("routing_restored_read", ui.api_response("/api/live/cluster", {"connection": active}, workspace))
    event("result", result)
    records = [record for record in dbops.audit_report(store)["records"] if record["id"] not in before_audit]
    event("audit", records)
    if len(records) != 1:
        raise RuntimeError("Expected one action audit record; observed %d." % len(records))
    if result.get("audit_id") != records[0]["id"]:
        raise RuntimeError("The returned audit_id did not identify this action's audit record.")
    proof = outcome_summary(args.step, action, result)
    proof["read_only"] = args.read_only
    proof["routing_drift"] = args.routing_drift
    print(PROOF_PREFIX + json.dumps(clean(proof), ensure_ascii=True), flush=True)
    # A successful process exit alone never proves verification. The actual
    # backend class and SQL facts are preserved for the coordinator to assess.
    if args.routing_drift:
        if result.get("status") != "refused" or result.get("not_sent") != plan["statements"]:
            raise RuntimeError("A changed routing configuration did not refuse every reviewed call before send.")
        return 0
    return 0 if result.get("status") == "done" else 2


def host(args):
    root = Path(__file__).resolve().parent.parent
    log_dir = root / "local" / "cs-live"
    log_dir.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("MF_ADMIN_PASSWORD"):
        raise RuntimeError("Missing MF_ADMIN_PASSWORD in the environment.")
    name = "mf-cs-runner-%d" % os.getpid()
    command = ["docker", "run", "--rm", "--name", name, "--network", "mf-cs-net",
               "--mount", "type=bind,source=%s,target=/work,readonly" % root,
               "-e", "MF_ADMIN_PASSWORD", "-e", "MF_ADMIN_USER", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "--entrypoint", "python3", "mf-cs-runner", "/work/scripts/clusterset-live.py",
               "--inside", "--step", args.step]
    if args.source:
        command.extend(["--source", args.source])
    if args.read_only:
        command.append("--read-only")
    if args.routing_drift:
        command.append("--routing-drift")
    if args.parameters:
        command.extend(["--parameters", args.parameters])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = ("-read-only" if args.read_only else "") + ("-routing-drift" if args.routing_drift else "")
    logfile = log_dir / (stamp + "-" + args.step + suffix + ".jsonl")
    lines = queue.Queue()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

    def read_output():
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, name="clusterset-live-output")
    reader.start()
    proof = None
    next_check = time.monotonic() + 30
    with logfile.open("w", encoding="utf-8") as stream:
        try:
            finished = False
            while not finished:
                try:
                    line = lines.get(timeout=max(0.1, next_check - time.monotonic()))
                except queue.Empty:
                    line = ""
                if line is None:
                    finished = True
                elif line:
                    safe = clean_text(line)
                    stream.write(safe)
                    stream.flush()
                    if safe.startswith(PROOF_PREFIX):
                        proof = json.loads(safe[len(PROOF_PREFIX):])
                    else:
                        try:
                            item = json.loads(safe)
                        except ValueError:
                            print(safe.rstrip(), flush=True)
                        else:
                            if item.get("event") in ("progress", "confirmation", "error"):
                                print(safe.rstrip(), flush=True)
                if time.monotonic() >= next_check:
                    print("LIVE %s: runner %s, evidence %s" %
                          (args.step, "alive" if proc.poll() is None else "finished", logfile), flush=True)
                    next_check = time.monotonic() + 30
            code = proc.wait()
        finally:
            # Never leave the Python proof process running after this wrapper.
            # We deliberately do not stop a server or attempt an implicit retry.
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
            reader.join()
            proc.stdout.close()
    if proof is not None:
        with (log_dir / "summary.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(proof, ensure_ascii=True) + "\n")
        facts = json.dumps(proof["verification_counts"], ensure_ascii=True, separators=(",", ":"))
        print("%s | %s | %s" % (proof["action"], proof["outcome_class"], facts), flush=True)
    print("Evidence: %s" % logfile, flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step", required=True, choices=tuple(STEPS))
    parser.add_argument("--source", choices=NODES, help="Active saved connection (a1 normally; b1 during failover).")
    parser.add_argument("--read-only", action="store_true", help="Mark the active saved connection Read-only.")
    parser.add_argument("--routing-drift", action="store_true", help="Change and restore routing around send; require refusal.")
    parser.add_argument("--parameters", help="JSON parameter overrides, still validated and bound by the real plan endpoint.")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.routing_drift and not args.step.startswith("switchover-"):
        parser.error("--routing-drift requires a switchover step")
    if args.parameters and not isinstance(json.loads(args.parameters), dict):
        parser.error("--parameters must be a JSON object")
    try:
        return inside(args) if args.inside else host(args)
    except Exception as exc:
        event("error", {"type": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
