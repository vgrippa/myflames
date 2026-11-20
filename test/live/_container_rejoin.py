"""Live scenario, inside the lab network: a MySQL Shell Rejoin through a Group connection.

Runs in the lab-runner container (see scripts/lab-runner/Dockerfile), started
by test/live/live_shell.py with the worktree mounted read-only at /src and the
lab credentials read-only at /run/creds.env:

  docker run --rm --network mf-ic-net -v <worktree>:/src:ro \
    -v <credentials.env>:/run/creds.env:ro -v <test/live>:/run/live:ro \
    -e HOME=/tmp/home -e MYFLAMES_SECRET_BACKEND=vault \
    myflames-lab-runner:8.4 python3 -I /run/live/_container_rejoin.py

Profiles point at the members by container name (mf-ic-N:3306), as AdminAPI
records them. Prints one PASS line per step; never prints a password. A stop
of mf-ic-3 is always followed by a rejoin attempt, also on failure.
"""
import json
import os
import sys
import time
import types

sys.path.insert(0, "/src")
os.makedirs(os.environ["HOME"], exist_ok=True)
assert os.environ["HOME"].startswith("/tmp/"), "throwaway HOME only"
from myflames import ui  # noqa: E402
from myflames.workspace_store import WorkspaceStore, ENDPOINT_FIELDS  # noqa: E402

password = None
for line in open("/run/creds.env"):
    key, _, value = line.strip().partition("=")
    if key == "MF_ADMIN_PASSWORD":
        password = value.strip().strip("'\"")
assert password, "MF_ADMIN_PASSWORD missing"

store = WorkspaceStore()
ws = types.SimpleNamespace(store=store)
GROUP = "IC-shell"
FAILED = []


def api(path, payload):
    return ui.api_response(path, payload, ws)


def step(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (": " + detail.replace(password, "[redacted]")) if detail else ""))
    sys.stdout.flush()
    if not ok:
        FAILED.append(name)
        raise SystemExit(1)


def conn_for(profile, group=None):
    value = {k: profile.get(k) for k in ENDPOINT_FIELDS if profile.get(k) not in (None, "")}
    value.update(profile_id=profile["id"], password="")
    if group:
        value["group_name"] = group
    return value


ic = []
for n in (1, 2, 3):
    saved = store.save("profile", {"name": "shell-ic-%d" % n, "host": "mf-ic-%d" % n, "port": 3306,
                                   "user": "mf_admin", "type": "development"}, None,
                       password=password, remember=True)
    ic.append(saved)
api("/api/profiles/groups/save", {"name": GROUP, "profile_ids": [p["id"] for p in ic]})
ic = [store.get("profile", p["id"]) for p in ic]
step("group saved", all(p.get("group") == GROUP for p in ic))
for p in ic:
    api("/api/live/cluster", {"connection": conn_for(p)})
step("identities recorded",
     all((store.get("profile", p["id"]).get("topology_identity") or {}).get("server_uuid") for p in ic))


def read_group():
    return api("/api/live/cluster", {"connection": conn_for(ic[0], GROUP)})


def member(report, needle):
    return next(m for m in report.get("members") or () if needle in (m.get("name") or ""))


def run(action, m, target, via):
    plan = api("/api/live/cluster/plan", {"connection": conn_for(via, GROUP), "action": action,
                                          "member_key": m["key"], "target_profile_id": target["id"]})
    reviewed = plan.get("plan") or plan
    token = plan.get("review_token") or reviewed.get("review_token")
    result = api("/api/live/cluster/action", {"connection": conn_for(via, GROUP), "plan": reviewed,
                                              "review_token": token, "target_profile_id": target["id"],
                                              "typed": reviewed.get("typed_guard") or "", "confirmed": True,
                                              "progress_id": "live-%d" % int(time.time() * 1000)})
    return reviewed, result


def state3():
    return member(read_group(), "mf-ic-3").get("state")


def rejoin_member_3():
    """Restore: mf-ic-3 rejoins the cluster through MySQL Shell, whatever failed."""
    try:
        m3 = member(read_group(), "mf-ic-3")
        if m3.get("state") == "ONLINE":
            return
        rejoin = next((a for k, a in {a["id"]: a for a in m3.get("actions") or ()}.items()
                       if k.startswith("rejoin") and a.get("enabled")), None)
        if rejoin is None:
            print("RESTORE no rejoin action offered for mf-ic-3")
            return
        route = (rejoin.get("target") or {}).get("profile_id") or ic[0]["id"]
        run(rejoin["id"], m3, store.get("profile", route), ic[0])
        deadline = time.time() + 120
        while time.time() < deadline and state3() != "ONLINE":
            time.sleep(4)
        print("RESTORE mf-ic-3 state %s" % state3())
    except Exception as exc:  # noqa: BLE001 - a restore failure is reported, not raised
        print("RESTORE failed: %s" % str(exc).replace(password, "[redacted]"))


try:
    report = read_group()
    m3 = member(report, "mf-ic-3")
    step("mf-ic-3 starts ONLINE", m3.get("state") == "ONLINE", "state %s" % m3.get("state"))
    _r, result = run("stop_member", m3, ic[2], ic[0])
    step("stop sent through shell-ic-3", result.get("status") in ("done", "sent", "ok"),
         json.dumps({k: result.get(k) for k in ("status", "outcome", "message")})[:300])
    deadline = time.time() + 60
    while time.time() < deadline:
        if state3() not in ("ONLINE", "RECOVERING"):
            break
        time.sleep(3)
    step("mf-ic-3 left the group", state3() not in ("ONLINE", "RECOVERING"), "state %s" % state3())

    m3 = member(read_group(), "mf-ic-3")
    actions = {a["id"]: a for a in m3.get("actions") or ()}
    rejoin = next((a for k, a in actions.items() if k.startswith("rejoin")), {})
    step("rejoin offered for mf-ic-3", bool(rejoin.get("enabled")),
         json.dumps({k: rejoin.get(k) for k in ("id", "why_not", "fix")}))
    route = (rejoin.get("target") or {}).get("profile_id") or ic[0]["id"]
    step("rejoin routes through an ONLINE member", route != ic[2]["id"], "target %s" % route)
    reviewed, result = run(rejoin["id"], m3, store.get("profile", route), ic[0])
    step("rejoin runs through MySQL Shell", reviewed.get("via") == "mysqlsh", "via %s" % reviewed.get("via"))
    step("rejoin sent", result.get("status") in ("done", "sent", "ok"),
         json.dumps({k: result.get(k) for k in ("status", "outcome", "message", "error")})[:400])
    deadline = time.time() + 120
    while time.time() < deadline:
        if state3() == "ONLINE":
            break
        time.sleep(4)
    step("mf-ic-3 back ONLINE", state3() == "ONLINE", "state %s" % state3())
finally:
    rejoin_member_3()
print("LIVE SHELL REJOIN OK")
