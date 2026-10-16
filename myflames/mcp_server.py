"""
myflames MCP server — the agent-facing surface.

This exposes myflames as tools an AI agent (Claude Code, Cursor, any MCP client)
can call directly, so the agent gets a token-cheap, source-grounded analysis of
a query plan without ever parsing raw ``EXPLAIN`` JSON or OCR'ing an SVG.

Packaging
---------
The MCP transport is an **optional extra**: ``pip install myflames[mcp]``. The
core myflames package stays stdlib-only. To keep ``import myflames.mcp_server``
working without the dependency, the *tool logic* (the ``*_tool`` functions
below) is pure and stdlib-only and is unit-tested directly; only the thin
:func:`main` / :func:`build_server` wrapper imports ``mcp``, and it does so
lazily with a clear error if the extra isn't installed.

Run it
------
``myflames-mcp`` (console script) or ``python -m myflames.mcp_server``.
Then register with an MCP client, e.g. ``claude mcp add myflames -- myflames-mcp``.
"""
import json
import re
from typing import List, Optional

from .parser import parse_explain, analyze_plan, OPTIMIZER_SWITCH_EXPLANATIONS
from .output_sidecar import build_sidecar
from .output_compare_sidecar import build_compare_sidecar
from . import __version__, digest as dg


# ---------------------------------------------------------------------------
# Tool logic (pure, stdlib-only, unit-tested)
# ---------------------------------------------------------------------------

def _engine_of(plan_json):
    try:
        from .parser import load_explain_json, _is_mariadb_format
        return "mariadb" if _is_mariadb_format(load_explain_json(plan_json)) else "mysql"
    except Exception:
        return "unknown"


def analyze_plan_tool(plan_json, detail="full"):
    """Parse + analyze an EXPLAIN ANALYZE FORMAT=JSON plan; return the sidecar dict.

    The sidecar carries plan_summary, optimizer_switches, warnings, suggestions,
    index suggestions, the node-id'd plan tree, and the executive summary —
    everything an agent needs to reason about the plan, none of the SVG.

    ``detail="full"`` (default) returns the complete record — required by the
    digest path (:func:`digest_plan_tool`) and ``explain_query``. The MCP
    ``analyze_plan`` tool passes ``detail="core"`` to drop the heavy
    ``collected``/``query``/``teach_hooks`` blocks and hand the agent the
    token-cheap decision-grade subset by default.
    """
    root = parse_explain(plan_json)
    analysis = analyze_plan(root)
    return build_sidecar(
        root, analysis, source_type="stdin", engine=_engine_of(plan_json),
        detail=detail,
    )


def digest_plan_tool(plan_json):
    """Return a sub-500-token text digest of a query plan (for pasting into a prompt)."""
    return dg.build_digest(analyze_plan_tool(plan_json))


def compare_plans_tool(before_json, after_json):
    """Diff two EXPLAIN plans; return the structured compare-1.0 delta sidecar."""
    return build_compare_sidecar(before_json, after_json)


def explain_optimizer_switch_tool(name):
    """Explain a MySQL/MariaDB ``optimizer_switch`` flag, source-verified.

    Returns ``{name, known, explanation}``; when the flag is unknown, lists the
    available flags so the agent can correct itself instead of guessing.
    """
    key = (name or "").strip().lower()
    explanation = OPTIMIZER_SWITCH_EXPLANATIONS.get(key)
    if explanation is None:
        return {
            "name": key,
            "known": False,
            "explanation": None,
            "available": sorted(OPTIMIZER_SWITCH_EXPLANATIONS),
        }
    return {"name": key, "known": True, "explanation": explanation}


def explain_query_tool(query, host, port=3306, user=None, password=None,
                       database=None, digest=True):
    """Connect to a live MySQL/MariaDB server, run EXPLAIN ANALYZE, return analysis.

    Returns the text digest by default (``digest=True``) or the full sidecar dict
    (``digest=False``). Requires network access to the server; raises on failure.
    """
    from .connector import MySQLConnection
    conn = MySQLConnection(
        host=host, port=port, user=user, password=password, database=database,
    )
    with conn:
        plan_json = conn.explain_analyze(query)
    if digest:
        return digest_plan_tool(plan_json)
    return analyze_plan_tool(plan_json)


def _read_sql(sql):
    """Adapt the two SYSTEM-zone transaction reads without issuing SET.

    INNODB_TRX dates use the server's OS zone; the client session can inherit
    a different global zone. Convert NOW() in the SELECT instead of changing
    the session. Starting from UTC also avoids ambiguous local times at a
    daylight-saving transition. Only exact module-owned prefixes are rewritten.
    """
    from .live import TOKEN
    for tag in ("/*myflames:sessions*/ ", "/*myflames:innodb*/ "):
        prefix = tag + "SET SESSION time_zone = 'SYSTEM'; "
        if sql.startswith(prefix):
            sql = sql[len(prefix):].replace("NOW()", "CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', 'SYSTEM')")
            break
    # No arbitrary SQL reaches this adapter: tools call a fixed set of module
    # readers. Check every statement anyway, including the multi-read privilege
    # script, so a future module change cannot add SET or a write here.
    statements = [part.strip() for part in TOKEN.sub(" ", sql).split(";") if part.strip()]
    if not statements or any(not re.match(r"^(SELECT|SHOW)\b", part, re.I) for part in statements):
        raise ValueError("MCP server analysis permits only SELECT and SHOW statements.")
    return sql


class _ReadConnection(object):
    def __init__(self, conn):
        self._conn = conn

    def run(self, sql, extra_flags=None, timeout=None):
        return self._conn.run(_read_sql(sql), extra_flags=extra_flags, timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# The new server tools deliberately take only a saved profile name or ID.
# Besides keeping credentials out of tool transcripts, this avoids sending a
# profile's saved password to an endpoint overridden by an agent.
def _server_read(profile, detail, read, formatter):
    from types import SimpleNamespace
    from .connector import MySQLConnection
    from .live import connection_options
    from .workspace_cli import _resolve_connection
    if detail not in ("core", "full"):
        raise ValueError('Choose detail="core" or detail="full".')
    if not isinstance(profile, str) or not profile.strip() or len(profile) > 4096:
        raise ValueError("Pass a saved connection profile name or ID.")
    args = SimpleNamespace(profile=profile, host=None, port=None, user=None,
                           password=None, database=None, ssl_mode=None, ssl_ca=None)
    options = _resolve_connection(args)
    password = options.get("password")
    try:
        with MySQLConnection(**connection_options(options)) as conn:
            result = read(_ReadConnection(conn))
        output = result if detail == "full" else formatter(result)
        # Some server errors are returned as notes inside reports. Sanitize
        # both projections, including nested values, before they reach MCP.
        if password:
            output = _without_secret(output, password)
        return output
    except Exception as exc:
        # The client and report modules can include diagnostics in exceptions.
        # Never propagate the saved password, even if a backend echoes it.
        raise ValueError(str(exc).replace(password, "[redacted]") if password else str(exc)) from None


def _without_secret(value, password):
    if isinstance(value, str):
        return value.replace(password, "[redacted]")
    if isinstance(value, dict):
        return {_without_secret(k, password): _without_secret(v, password) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_secret(v, password) for v in value]
    return value


def _text_with_notes(formatter, result):
    text = formatter(result).rstrip()
    notes = result.get("notes") or []
    return text + ("\nnotes: " + "\n".join(notes) if notes else "") + "\n"


def _top_digest(result):
    # Keep normalized SQL and attribution caveats, not sample literals or the
    # duplicate averages/cost fields. Counts remain cumulative, not rates.
    lines = ["top-1.0 %s %s order=%s" % (result["engine"], result["server_version"], result["order"])]
    for item in result["statements"]:
        fields = ("digest", "schema", "count", "total_ms", "rows_examined", "no_index_used",
                  "select_full_join", "select_range_check", "select_scan", "table_match", "digest_text")
        lines.append(json.dumps({k: item[k] for k in fields if k in item}, ensure_ascii=False, separators=(",", ":")))
    for key in ("coverage", "match"):
        if result.get(key):
            lines.append(key + ": " + json.dumps(result[key], ensure_ascii=False, separators=(",", ":")))
    lines.extend(result.get("notes") or [])
    return "\n".join(lines) + "\n"


def _privileges_digest(result):
    # SHOW GRANTS is already stripped of authentication material by privileges.
    # Keep its scope/role information and unknown states; never infer denial.
    model = result.get("model", result)
    lines = ["privileges-1.0 %s %s account=%s" % (result["engine"], result["version"], result["account"]),
             result["session_note"], "checked=%s; partial_revokes=%s" % (model["checked"], model["partial_revokes"]),
             "roles: " + json.dumps(result["current_roles"], ensure_ascii=False, separators=(",", ":"))]
    lines.extend(model["grants"])
    for state in ("allowed", "denied", "unknown"):
        lines.append(state + ": " + ", ".join(k for k, v in result["actions"].items() if v["state"] == state))
    lines.extend(result.get("warnings") or [])
    return "\n".join(lines) + "\n"


def server_report_tool(profile: str, detail: str = "core", interval: int = 5,
                       schemas: Optional[List[str]] = None) -> object:
    """Read-only full server analysis. Core returns report.format_digest; full returns report-1.0 JSON.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import report
    from .perf_common import check_window, check_schemas
    interval = check_window(interval, report.MIN_INTERVAL, report.MAX_INTERVAL)
    schemas = check_schemas(schemas)
    return _server_read(profile, detail, lambda c: report.analysis_report(c, interval=interval, schemas=schemas),
                        report.format_digest)


def top_statements_tool(profile: str, detail: str = "core", limit: int = 20,
                        schema: Optional[str] = None, order: str = "total",
                        table: Optional[str] = None) -> object:
    """Read-only statement digests, including full_join, scan, no_index orders and schema.table attribution.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import discover
    _tool_limit(limit, 200)
    if not isinstance(order, str) or order not in discover.ORDERS:
        raise ValueError("Choose an order from: " + ", ".join(discover.ORDERS))
    if schema is not None and (not isinstance(schema, str) or not 1 <= len(schema) <= 64 or "\x00" in schema):
        raise ValueError("Schema names are 1 to 64 characters.")
    if table is not None:
        if not isinstance(table, str):
            raise ValueError("Pass a schema.table name.")
        table = discover.parse_table_name(table)
    return _server_read(profile, detail, lambda c: discover.top_report(c, limit=limit, schema=schema, order=order,
                                                                     table=table), _top_digest)


def _tool_limit(limit, maximum):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError("Choose a limit from 1 to %d." % maximum)


def sessions_tool(profile: str, detail: str = "core", active_only: bool = True,
                  min_seconds: float = 0, limit: int = 1000) -> object:
    """Read-only sessions and idle transactions; never explains or kills a connection.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import sessions
    from .live import finite_number
    _tool_limit(limit, sessions.MAX_LIMIT)
    if not isinstance(active_only, bool):
        raise ValueError("active_only must be a boolean.")
    if not finite_number(min_seconds) or min_seconds < 0:
        raise ValueError("min_seconds must be a finite nonnegative number.")
    return _server_read(profile, detail, lambda c: sessions.sessions_report(c, active_only=active_only,
                        min_seconds=min_seconds, limit=limit), lambda r: _text_with_notes(sessions.format_text, r))


def _locks_digest(result):
    from . import locks
    if result.get("deadlock"):
        result = dict(result, deadlock=dict(result["deadlock"], raw=""))
    return locks.format_text(result)


def locks_report_tool(profile: str, detail: str = "core", limit: int = 500,
                      waits_only: bool = False, deadlock_only: bool = False) -> object:
    """Read-only blockers, metadata locks and deadlocks; no KILL capability.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import locks
    _tool_limit(limit, locks.MAX_LIMIT)
    if not isinstance(waits_only, bool) or not isinstance(deadlock_only, bool) or waits_only and deadlock_only:
        raise ValueError("Choose boolean waits_only or deadlock_only, not both.")
    return _server_read(profile, detail, lambda c: locks.lock_report(c, limit=limit, waits_only=waits_only,
                        deadlock_only=deadlock_only), _locks_digest)


def health_sample_tool(profile: str, detail: str = "core", interval: int = 5) -> object:
    """Read-only two-sample health; interval is 1 to 60 seconds. No settings are changed.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import health
    from .perf_common import check_window
    interval = check_window(interval, health.MIN_INTERVAL, health.MAX_INTERVAL)
    return _server_read(profile, detail, lambda c: health.health_report(c, interval=interval),
                        lambda r: _text_with_notes(health.format_text, r))


def innodb_status_tool(profile: str, detail: str = "core") -> object:
    """Read-only parsed InnoDB status; full returns innodb-1.0 JSON without raw status text.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import innodb_status
    return _server_read(profile, detail, innodb_status.innodb_report,
                        lambda r: _text_with_notes(innodb_status.format_text, r))


def my_privileges_tool(profile: str, detail: str = "core") -> object:
    """Read-only effective privileges and can() decisions for a fresh session; never changes accounts or grants.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import privileges
    return _server_read(profile, detail, privileges.read_privileges, _privileges_digest)


def replication_status_tool(profile: str, detail: str = "core") -> object:
    """Read-only replication channels and lag caveats; never starts, stops or skips replication.

    profile is a saved workspace connection name or ID; its saved password is
    resolved locally. detail="core" returns compact text; detail="full" returns
    the complete module JSON report. No password or SQL arguments are accepted.
    """
    from . import replication
    return _server_read(profile, detail, replication.read_status,
                        lambda r: _text_with_notes(replication.format_text, r))


# ---------------------------------------------------------------------------
# MCP transport (optional — imports `mcp` lazily)
# ---------------------------------------------------------------------------

def _mcp_version():
    """Describe the installed optional SDK without a metadata backport on 3.7."""
    import sys
    declared = getattr(sys.modules.get("mcp"), "__version__", None)
    if declared:
        return str(declared)
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("mcp")
        except PackageNotFoundError:
            return "not installed"
    except ImportError:
        pass
    if sys.modules.get("mcp") is None:
        try:
            import importlib.util
            if importlib.util.find_spec("mcp") is None:
                return "not installed"
        except (ImportError, ValueError, AttributeError):
            return "not installed"
    return "unknown (version metadata unavailable on this Python)"


def build_server():
    """Construct the FastMCP server. Imports ``mcp`` lazily.

    Raises :class:`ImportError` with the SDK version and original import failure.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "MCP transport import failed (mcp version: %s): %s. "
            "This transport requires mcp>=1.2,<2. "
            "Install it with: pip install --upgrade 'myflames[mcp]'" % (_mcp_version(), " ".join(str(exc).split()).rstrip("."))
        ) from exc

    server = FastMCP("myflames")
    # FastMCP 1.2 accepts arbitrary settings but does not forward a version.
    # Set the low-level identity used to build the initialization response.
    server._mcp_server.version = __version__

    @server.tool()
    def analyze_plan(plan_json: str, detail: str = "core") -> dict:
        """Analyze a MySQL/MariaDB EXPLAIN ANALYZE FORMAT=JSON plan and return a
        compact, source-grounded analysis: summary, warnings, suggestions, index
        hints, optimizer-switch explanations, and the operator tree. Call this
        when you have a query plan and need to know why it's slow or how to fix
        it — it avoids feeding raw plan JSON into the model.

        detail="core" (default) is the token-cheap decision-grade projection.
        Pass detail="full" only when you need the collected environment data
        (schema/stats/variables) and the raw query text as well."""
        return analyze_plan_tool(plan_json, detail=detail)

    @server.tool()
    def digest_plan(plan_json: str) -> str:
        """Return a sub-500-token plain-text digest of a query plan. Call this
        when you want the cheapest possible representation of a plan to reason
        over (it strips all JSON structure overhead)."""
        return digest_plan_tool(plan_json)

    @server.tool()
    def compare_plans(before_json: str, after_json: str) -> dict:
        """Diff two EXPLAIN plans (before vs after a change). Returns per-operator
        time/row deltas and counts of improvements/regressions. Call this to
        verify whether an index, rewrite, or config change actually helped."""
        return compare_plans_tool(before_json, after_json)

    @server.tool()
    def explain_optimizer_switch(name: str) -> dict:
        """Explain a MySQL/MariaDB optimizer_switch flag (e.g. hash_join, mrr,
        index_merge, batched_key_access), verified against server source. Call
        this instead of guessing what an optimizer flag does."""
        return explain_optimizer_switch_tool(name)

    @server.tool()
    def explain_query(query: str, host: str, port: int = 3306,
                      user: str = "", password: str = "", database: str = "",
                      digest: bool = True) -> object:
        """Connect to a live MySQL/MariaDB server, run EXPLAIN ANALYZE on the
        query, and return the analysis (text digest by default, or the full
        sidecar with digest=False). Use when you can reach the database and want
        a plan you don't already have."""
        return explain_query_tool(
            query, host=host, port=port, user=user or None,
            password=password or None, database=database or None, digest=digest,
        )

    for name, tool in (
        ("server_report", server_report_tool),
        ("top_statements", top_statements_tool),
        ("sessions", sessions_tool),
        ("locks_report", locks_report_tool),
        ("health_sample", health_sample_tool),
        ("innodb_status", innodb_status_tool),
        ("my_privileges", my_privileges_tool),
        ("replication_status", replication_status_tool),
    ):
        server.tool(name=name)(tool)

    return server


def main():
    """Console entry point (``myflames-mcp``). Runs the server over stdio."""
    import sys
    try:
        server = build_server()
    except ImportError as exc:
        print("myflames-mcp: %s" % " ".join(str(exc).split()), file=sys.stderr)
        raise SystemExit(2) from None
    server.run()


if __name__ == "__main__":
    main()
