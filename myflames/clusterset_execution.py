# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""ClusterSet preparation and outcome handling around the shared review gate.

This module never constructs a write at execution time. reviewed_writes sends
the bound JavaScript after a fresh plan matches, and this layer qualifies its
result using independent SQL evidence and completes the same audit record.
"""
from contextlib import contextmanager
import threading
import time

from .connector import ConnectorError


SHELL_OUTPUT_LIMIT = 1024 * 1024
# Four UUID-bound SQL logins verify a spare before the separate Shell
# configuration check. This on-demand read does not share the display's
# five-second route-search budget.
CANDIDATE_READ_TIMEOUT = 120.0
SHELL_SHUTDOWN_MESSAGE = (
    "The workspace closed while MySQL Shell was running; the outcome is unknown. "
    "Stopping the client does not roll back a server operation. Check the target's clone status, "
    "server restart, ClusterSet membership and replication with dba.getClusterSet().status() "
    "and direct SQL before retrying.")


@contextmanager
def supervise_shell(conn, lifecycle, audit):
    """Join the desktop's tool registry and bounded shutdown/audit grace.

    The request thread owns the reviewed write, while LifecycleJobs owns the
    process groups. Complete the durable attempt as soon as observation is
    interrupted, before independent SQL verification can wait on a server.
    """
    if lifecycle is None:
        yield
        return
    with lifecycle.runner("cluster_action", {}) as runner:
        runner.thread = threading.current_thread()
        previous = {key: getattr(conn, key, None) for key in
                    ("_tool_groups", "_shell_shutdown_audit")}
        conn._tool_groups = runner.groups

        def interrupted():
            if audit is not None:
                audit.complete("unknown", outcome="unknown or partial", message=SHELL_SHUTDOWN_MESSAGE,
                               after={"outcome_class": "unknown or partial", "observation": "shutdown"})

        conn._shell_shutdown_audit = interrupted
        try:
            yield
        finally:
            for key, value in previous.items():
                setattr(conn, key, value)


@contextmanager
def verification_reads(conn, options, state):
    """Keep post-action SQL inside the workspace's shutdown supervision.

    Guard every read, including saved peers, and register real client starts
    atomically with ToolGroups.close_spawning. An entry-only closed check
    would still allow later verification queries to start after shutdown.
    Connection objects belong to this request; restore their methods before
    their opener closes them or the action returns.
    """
    groups = getattr(conn, "_tool_groups", None)

    def check():
        info = getattr(state.get("shell_stderr"), "error_info", None) or {}
        if (state.get("workspace_shutdown") or info.get("stage") == "shutdown" or
                (groups is not None and groups.closed)):
            state["workspace_shutdown"] = True
            state["verification"] = {
                "facts": [], "missing": ["SQL verification stopped because the workspace is closing; check "
                                         "clone status, server restart, ClusterSet membership and replication"],
                "mismatches": [], "summary": SHELL_SHUTDOWN_MESSAGE}
            raise ConnectorError(SHELL_SHUTDOWN_MESSAGE)

    @contextmanager
    def guarded(peer):
        check()
        original_run = peer.run
        original_client = getattr(peer, "_run_client", None)
        saved = {key: peer.__dict__.get(key) for key in ("run", "_run_client") if key in peer.__dict__}

        def run(*args, **kwargs):
            check()
            return original_run(*args, **kwargs)

        def run_client(*args, **kwargs):
            check()
            if groups is not None:
                kwargs["tool_groups"] = groups
            return original_client(*args, **kwargs)

        peer.run = run
        if original_client is not None:
            peer._run_client = run_client
        try:
            yield peer
        finally:
            for key in ("run", "_run_client"):
                if key in saved:
                    setattr(peer, key, saved[key])
                elif key in peer.__dict__:
                    delattr(peer, key)

    check()
    read_options = dict(options or {})
    opener = read_options.get("open_profile")
    if opener is not None:
        @contextmanager
        def open_saved(profile):
            check()
            with opener(profile) as peer:
                with guarded(peer):
                    yield peer
        # Preserve the original consent scope. A prepare shortcut is not
        # copied: every connection must pass through the guarded opener.
        for key in ("group_scope", "check", "consent", "current_profile"):
            if hasattr(opener, key):
                setattr(open_saved, key, getattr(opener, key))
        read_options["open_profile"] = open_saved
    with guarded(conn):
        try:
            yield conn, read_options
        finally:
            # Shutdown may interrupt the final query after its pre-spawn
            # check, or race the verifier's return. Never call that verified.
            check()


def candidate_binding(profile):
    """Store-owned consent and endpoint, bound at review and checked at login."""
    from .workspace_store import ENDPOINT_FIELDS, endpoint_value
    return {"profile_id": profile["id"], "read_only": bool(profile.get("read_only")),
            "endpoint": {key: endpoint_value(profile, key) for key in ENDPOINT_FIELDS},
            "saved_group": profile.get("group") or "", "group_generation": profile.get("group_generation") or "",
            "server_uuid": (profile.get("topology_identity") or {}).get("server_uuid")}


def bounded_output(value):
    """Keep a UTF-8 tail of at most 1 MiB without a partial leading character.

    ShellWriter has already redacted the complete text, so truncation cannot
    expose the suffix of a password crossing the retention boundary.
    """
    data = (value or "").encode("utf-8")
    return data[-SHELL_OUTPUT_LIMIT:].decode("utf-8", "ignore"), len(data) > SHELL_OUTPUT_LIMIT


def _hex(value):
    data = str(value).encode("utf-8").hex()
    return "CONVERT(0x%s USING utf8mb4)" % data if data else "''"


def read_candidate(conn, report, action, checks):
    """Read only the selected, consenting spare, during review and pre-send."""
    from . import cluster as c
    from .cluster_shell import ShellWriter, target_session_guard
    pid = c._action_name(action)[1]
    options = checks.get("read_options") or {}
    profiles = options.get("profiles") or ()
    profile = next((p for p in profiles if p.get("id") == pid), None)
    opener = options.get("open_profile")
    if profile is None or opener is None:
        raise ValueError("The standalone saved connection is not available in this group.")
    binding = candidate_binding(profile)
    if binding["read_only"]:
        raise ValueError("The standalone saved connection is Read-only in myflames. Turn off Read-only for this "
                         "saved connection or choose another standalone server before reviewing creation.")
    if profile.get("ssh_host"):
        raise ValueError("Replica creation needs a direct saved connection reachable by MySQL Shell.")
    expected_uuid = (profile.get("topology_identity") or {}).get("server_uuid")
    known = c._clusterset_known_instances(report)
    if not expected_uuid or expected_uuid in known:
        raise ValueError("Choose a known group member outside the ClusterSet's instance metadata.")
    primary = c._clusterset_primary(report)
    route = c._clusterset_route(report, primary)
    if not route or not primary.get("primary") or route["server_uuid"] != primary["primary"]["server_uuid"]:
        raise ValueError("The primary cluster's primary must be readable before checking the target GTIDs.")

    def primary_gtids(source):
        rows = c.Reader(source, c.CLUSTER_TAG).rows("SELECT @@global.server_uuid, @@global.gtid_executed")
        if not rows or rows[0][0] != route["server_uuid"]:
            raise ValueError("The primary route's server UUID changed before checking GTIDs.")
        return rows[0][1]

    if route.get("self"):
        gtids = primary_gtids(conn)
    else:
        primary_profile = next((p for p in profiles if p.get("id") == route.get("profile_id")), None)
        if primary_profile is None:
            raise ValueError("The primary's saved route is no longer in this group.")
        with opener(primary_profile) as source:
            gtids = primary_gtids(source)
    with opener(profile) as target:
        # Reader starts a client for every query. Each result, including an
        # empty GR/metadata table, must carry the reviewed UUID in that same
        # login, with reconnect disabled. Direct server routes are required;
        # a proxy that routes separate packets is outside this guarantee.
        reader = c._ClusterSetReader(target, deadline=time.monotonic() + CANDIDATE_READ_TIMEOUT,
                                     server_uuid=expected_uuid)
        variables = c.show_variables(reader, (
            "server_uuid", "version", "version_comment", "report_host", "hostname", "port", "report_port",
            "group_replication_group_name", "super_read_only", "gtid_mode", "enforce_gtid_consistency",
            "log_bin", "log_replica_updates", "binlog_format", "server_id"))
        uuid = variables.get("server_uuid")
        if uuid != expected_uuid:
            raise ValueError("The standalone saved connection's server UUID changed. Refresh before review.")
        address = c.join_address(binding["endpoint"]["host"], binding["endpoint"]["port"] or 3306)
        # Shell's canonical address also feeds metadata and GR configuration
        # (mysql-shell instance.cc:96-112; dba/create_cluster.cc:379-417).
        reported_address = c.join_address(variables.get("report_host") or variables.get("hostname"),
                                          c._number(variables.get("report_port") or variables.get("port")))
        if not isinstance(address, str) or not c._ADDRESS.fullmatch(address):
            raise ValueError("The standalone saved connection has no valid direct host:port for MySQL Shell.")
        if address != reported_address:
            raise ValueError("The standalone server's reported address (%s) differs from its saved endpoint (%s). "
                             "Make report_host and report_port match the saved direct endpoint, then read and review "
                             "again; myflames will not send Shell to another reported address." %
                             (reported_address or "not reported", address))
        if c._version_of({"server_version": variables.get("version")}) < c.SHELL_CLUSTERSET_MIN_VERSION or "mariadb" in str(
                variables.get("version")).lower():
            raise ValueError("The standalone server needs MySQL 8.0.27 or later.")
        members = reader.rows("SELECT MEMBER_ID, MEMBER_STATE FROM performance_schema.replication_group_members")
        if any(len(row) < 2 or row[1] != "OFFLINE" for row in members):
            raise ValueError("Group Replication is running on the selected server; choose a standalone instance.")
        schemas = reader.rows("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                              "WHERE SCHEMA_NAME = 'mysql_innodb_cluster_metadata'")
        if schemas:
            raise ValueError("The selected server still has InnoDB Cluster metadata; inspect its membership first.")
        subset = reader.rows("SELECT GTID_SUBSET(@@global.gtid_executed, %s)" % _hex(gtids))
        if not subset or str(subset[0][0]) != "1":
            raise ValueError("The standalone server's GTID_EXECUTED is not a subset of the primary cluster's.")
        fenced = c._on(variables.get("super_read_only"))
        if not isinstance(fenced, bool):
            raise ValueError("The standalone server's super_read_only could not be read.")
        # checkInstanceConfiguration is read-only. Unlike a create dryRun, it
        # performs no provisioning and never changes super_read_only.
        script = target_session_guard({"server_uuid": uuid}) + [
            'print("MYFLAMES_CONFIG=" + JSON.stringify(dba.checkInstanceConfiguration()));']
        output = ShellWriter(target, checks.get("shell")).run_write("\n".join(script), timeout=120)
        # ShellWriter validates raw private output before redaction. A
        # password can overlap the marker, a JSON key or the "ok" value.
        if getattr(output, "configuration_ok", False) is not True:
            raise ValueError("MySQL Shell reports that this standalone server is not configured for InnoDB Cluster; "
                             "run checkInstanceConfiguration and configureInstance before reviewing creation.")
        candidate = dict(binding, read="route", read_error=None, server_uuid=uuid)
        candidate.update({
                "route": {"profile_id": pid, "profile_name": profile.get("name") or pid,
                          "self": False, "server_uuid": uuid},
                "address": address, "reported_address": reported_address,
                "server_version": variables["version"], "engine": "mysql",
                "kind": "none", "group_name": "", "gtid_subset": True, "super_read_only": fenced,
                "configuration": {"status": "ok", "variables": {k: variables.get(k) for k in (
                    "gtid_mode", "enforce_gtid_consistency", "log_bin", "log_replica_updates",
                    "binlog_format", "server_id")}}})
        return candidate


class DeferredAudit(object):
    """Keep the existing durable attempt, finalize once verification is known."""
    def __init__(self, audit):
        self.audit = audit
        self.completion = None

    def __getattr__(self, name):
        return getattr(self.audit, name)

    def complete(self, status, **kwargs):
        self.completion = (status, kwargs)

    def outcome(self, statements, status, target=None, verified=None, **kwargs):
        # Unsent refusals also finish one record with the public outcome
        # class. Keep the durable pending/complete pattern for all paths.
        audit_id = self.audit.attempt(statements, target=target, verified=verified)
        self.complete(status, **kwargs)
        return audit_id


def finish(out, plan, state, audit=None, progress=None):
    """Qualify the Shell transport result without treating its exit as proof."""
    from . import cluster as c
    base = c._action_name(plan["action"])[0]
    verification = state.get("verification") or {
        "facts": [], "missing": ["SQL verification could not be read on the ClusterSet routes"], "mismatches": []}
    out["verification"] = verification
    for key in ("shell_output", "shell_stderr"):
        out[key], out[key + "_truncated"] = bounded_output(state.get(key, ""))
    missing, mismatch = verification.get("missing") or [], verification.get("mismatches") or []
    error = state.get("shell_error") or ""
    error_info = getattr(state.get("shell_stderr"), "error_info", None)
    if error:
        out["shell_error_info"] = error_info or {"source": "unknown", "code": None,
                                               "line": None, "stage": "unknown"}
    # Number ranges and a call's input line do not establish the stage inside
    # AdminAPI. Shell can refresh credentials before changing any visible
    # topology (cluster_set_impl.cc:2556-2586); MySQL SIGNAL can use the same
    # number as a Shell precondition. The shared gate already turns proven
    # guard-stage failures into unsent refusals. No action-level exception
    # carries proof of the internal pre-mutation stage, even from Shell.
    # In particular, dissolve's genuine MYSQLSH 51004 missing-member check
    # (cluster_set/dissolve.cc:297-301,423) follows lock acquisition (:408).
    # Those locks may install persistent locking-service UDFs with binary
    # logging disabled (cluster_set_impl.cc:3633-3644; lock_service.cc:53-61).
    # Unchanged topology or GTIDs do not establish that nothing changed.
    if out["status"] == "refused" or (out["status"] == "failed" and out.get("not_sent") == plan["statements"]):
        outcome = "failed, nothing changed"
        verification = {"facts": [], "missing": [], "mismatches": [],
                        "summary": "No action was sent; no changed state needs verification."}
        out["verification"] = verification
    elif state.get("workspace_shutdown") or (error_info and error_info.get("stage") == "shutdown"):
        outcome = "unknown or partial"
        out["status"] = "unknown"
        out["states"] = ["unknown"] * len(plan["statements"])
        out["message"] = SHELL_SHUTDOWN_MESSAGE
    elif error:
        outcome = "unknown or partial"
        out["status"] = "partial" if out.get("effect") else "unknown"
        out["message"] = "Unknown or partial. MySQL Shell reported an error; inspect the SQL facts before another action."
    elif base == "check_clusterset" and out["status"] == "done":
        # ShellWriter validated the complete raw report against the identity
        # in the bound script before redaction or retention. Never parse a
        # display string here: a password can equal a key or the domain name.
        diagnostic = getattr(state.get("shell_output"), "diagnostic", None) or {}
        valid = diagnostic.get("valid") is True
        verification = {"facts": diagnostic.get("facts") or [], "missing": [], "mismatches": [],
                        "summary": "MySQL Shell returned the requested diagnostic report; health is reported, not required."
                                   if valid else "The requested diagnostic report could not be validated."}
        out["verification"] = verification
        missing, mismatch = verification["missing"], verification["mismatches"]
        if not valid:
            missing.append("MySQL Shell: an extended status JSON report for the reviewed ClusterSet was not returned")
        outcome = "verified" if valid else "done, not fully verified"
        out["message"] = "MySQL Shell status output is included." if valid else "Shell did not return the requested status report."
    elif out["status"] == "done" and not mismatch:
        outcome = "done, not fully verified" if missing else "verified"
        out["message"] = "Done, not fully verified." if missing else "Verified by SQL on every required route."
    else:
        outcome = "unknown or partial"
        if out["status"] == "done":
            out["status"] = "partial"
        out["message"] = "Unknown or partial. The SQL reads did not establish every expected effect."
    if outcome not in ("verified", "failed, nothing changed"):
        details = mismatch + missing
        if details:
            out["message"] += " Check: " + "; ".join(details) + "."
    out["outcome_class"] = outcome
    out["outcome"] = outcome
    if audit is not None and audit.completion is not None:
        status, kwargs = audit.completion
        kwargs.update(outcome=outcome, message=out["message"], after={
            "state": out.get("after"), "verification": verification, "outcome_class": outcome})
        if error:
            kwargs["after"]["shell_error_info"] = out["shell_error_info"]
        status = "ok" if outcome in ("verified", "done, not fully verified") else (
            "refused" if out["status"] == "refused" else "unknown" if out["status"] == "unknown" else "error")
        audit.audit.complete(status, **kwargs)
    if progress is not None:
        progress.set("finished", out["states"], out["status"])
    return out
