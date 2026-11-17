"""Shared helpers for the live lab suites, in the spirit of mtr include files.

Every suite gets a throwaway HOME with the lab's saved connections (created by
``scripts/cluster-matrix.sh profiles --home``), talks to the workspace through
``myflames.ui.api_response`` exactly as the app does, waits on server state
with ``wait_for`` instead of fixed sleeps, and never prints the lab password:
``redact`` is applied to every error message that can leave a test.

The lab password is read from ``$CLUSTER_LAB/credentials.env`` (mode 0600 on a
maintainer machine) only to redact it and, for the suites that save their own
profile, to store it in the throwaway home's vault. It is never logged.

Suites run only with ``MYFLAMES_LIVE_LABS=1``; ``require_live`` enforces it so
an accidental discovery or direct run skips instead of touching the labs.
Python 3.7+, standard library only.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    # The runner is executed as a file (sys.path[0] is test/live), so the
    # myflames package at the repository root is not importable yet.
    sys.path.insert(0, REPO)
MATRIX = os.path.join(REPO, "scripts", "cluster-matrix.sh")
LIVE_ENV = "MYFLAMES_LIVE_LABS"
SKIP_REASON = "set %s=1 (scripts/live-tests.sh) to run the live lab suites" % LIVE_ENV

_PASSWORD = None
# The real HOME, captured before any suite replaces it with a throwaway home:
# cluster-matrix.sh refuses a --home that equals the caller's HOME, so the
# matrix subprocesses always run with this one.
_REAL_HOME = os.path.realpath(os.path.expanduser("~"))


def enabled():
    return os.environ.get(LIVE_ENV) == "1"


def require_live():
    if not enabled():
        raise unittest.SkipTest(SKIP_REASON)


def cluster_lab():
    return os.environ.get("CLUSTER_LAB") or os.path.join(REPO, "local", "cluster-matrix")


def credentials_path():
    return os.path.join(cluster_lab(), "credentials.env")


def lab_password():
    """MF_ADMIN_PASSWORD from the lab credentials file; never printed."""
    global _PASSWORD
    if _PASSWORD is None:
        with open(credentials_path(), encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key == "MF_ADMIN_PASSWORD":
                    _PASSWORD = value.strip().strip("'\"")
        if not _PASSWORD:
            raise LabError("MF_ADMIN_PASSWORD missing in %s" % credentials_path())
    return _PASSWORD


def redact(text):
    """The lab password never appears in a message that leaves a test."""
    if text is None:
        return ""
    password = _PASSWORD
    if password:
        text = str(text).replace(password, "[redacted]")
    return str(text)


class LabError(Exception):
    """A lab or workspace call failed; the message is already redacted."""


def matrix(*args):
    """Run scripts/cluster-matrix.sh with CLUSTER_LAB set; fail with redacted output."""
    env = dict(os.environ)
    env["CLUSTER_LAB"] = cluster_lab()
    env["HOME"] = _REAL_HOME
    env["USERPROFILE"] = _REAL_HOME
    proc = subprocess.run(["bash", MATRIX] + [str(a) for a in args],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, env=env)
    if proc.returncode != 0:
        raise LabError("cluster-matrix.sh %s exited %d:\n%s" % (
            " ".join(str(a) for a in args), proc.returncode, redact(proc.stdout)[-2000:]))
    return proc.stdout


def check_lab(*clusters):
    """The mtr-style precondition: refuse to run a scenario against a sick lab."""
    require_live()
    matrix("check", *clusters)


def make_home(suite):
    """A throwaway HOME with the lab profiles saved by cluster-matrix.sh.

    Never the real ~/.myflames: the directory comes from tempfile and the
    matrix script itself refuses a home that is or contains the real one.
    One home is shared per run (MYFLAMES_LIVE_HOME, set by the runner or
    created here), so the profiles step runs once per lane, not per suite.
    """
    require_live()
    home = os.environ.get("MYFLAMES_LIVE_HOME")
    if not home:
        home = tempfile.mkdtemp(prefix="mf-live-%s-" % suite)
        os.environ["MYFLAMES_LIVE_HOME"] = home
    os.makedirs(home, exist_ok=True)
    os.chmod(home, 0o700)
    if os.path.realpath(home) == _REAL_HOME:
        raise LabError("refusing the real HOME as a test home")
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    os.environ["MYFLAMES_SECRET_BACKEND"] = "vault"
    marker = os.path.join(home, ".profiles-ready")
    if not os.path.exists(marker):
        matrix("profiles", "--home", home)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("ok\n")
    return home


def conn_for(profile, group=None):
    """A connection payload for the saved profile, with the group when given."""
    from myflames.workspace_store import ENDPOINT_FIELDS
    value = {k: profile.get(k) for k in ENDPOINT_FIELDS if profile.get(k) not in (None, "")}
    value.update(profile_id=profile["id"], password="")
    if group:
        value["group_name"] = group
    return value


class Lab(object):
    """The workspace API over the throwaway home's store, redacting failures."""

    def __init__(self):
        from myflames.workspace_store import WorkspaceStore
        self.store = WorkspaceStore()
        self.ws = types.SimpleNamespace(store=self.store)

    def api(self, path, payload=None):
        from myflames import ui
        try:
            return ui.api_response(path, payload or {}, self.ws)
        except Exception as exc:  # noqa: BLE001 - re-raised redacted
            text = redact(str(exc))
            try:
                raise type(exc)(text)
            except Exception:
                raise LabError(text)

    def profile(self, name):
        for entry in self.store.list("profile"):
            if entry.get("name") == name:
                return entry
        raise LabError("no saved connection named %s in the lab home" % name)

    def refresh(self, profile):
        return self.store.get("profile", profile["id"])

    def identity(self, profile, key="server_uuid"):
        return (self.refresh(profile).get("topology_identity") or {}).get(key)


def member_named(report, needle):
    """The one report member whose name or address mentions the needle."""
    for member in report.get("members") or ():
        if needle in (member.get("name") or "") or needle in (member.get("address") or ""):
            return member
    raise LabError("no member matching %r in report" % needle)


def wait_for(fn, until=bool, timeout=90.0, interval=3.0, what="server state"):
    """Poll fn until it satisfies `until`, instead of a fixed sleep."""
    deadline = time.monotonic() + timeout
    value = fn()
    while not until(value):
        if time.monotonic() >= deadline:
            raise AssertionError("timed out after %.0fs waiting for %s (last: %s)" % (
                timeout, what, redact(repr(value))[:300]))
        time.sleep(interval)
        value = fn()
    return value


def cluster_action(lab, conn, action, member, target):
    """Plan and run a reviewed Cluster action, exactly as the workspace sends it."""
    plan = lab.api("/api/live/cluster/plan", {
        "connection": conn, "action": action,
        "member_key": member["key"], "target_profile_id": target["id"]})
    reviewed = plan.get("plan") or plan
    result = lab.api("/api/live/cluster/action", {
        "connection": conn, "plan": reviewed,
        "review_token": plan.get("review_token") or reviewed.get("review_token"),
        "target_profile_id": target["id"],
        "typed": reviewed.get("typed_guard") or "", "confirmed": True,
        "progress_id": "live-%d" % int(time.time() * 1000)})
    return reviewed, result


def save_group(lab, name, profiles, previous_name=None):
    payload = {"name": name, "profile_ids": [p["id"] for p in profiles]}
    if previous_name is not None:
        payload["previous_name"] = previous_name
    return lab.api("/api/profiles/groups/save", payload)


def result_summary(result):
    return redact(json.dumps({k: result.get(k) for k in
                              ("status", "error", "message", "outcome")}))[:300]
