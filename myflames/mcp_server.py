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

from .parser import parse_explain, analyze_plan, OPTIMIZER_SWITCH_EXPLANATIONS
from .output_sidecar import build_sidecar
from .output_compare_sidecar import build_compare_sidecar
from . import digest as dg


# ---------------------------------------------------------------------------
# Tool logic (pure, stdlib-only, unit-tested)
# ---------------------------------------------------------------------------

def _engine_of(plan_json):
    try:
        from .parser import load_explain_json, _is_mariadb_format
        return "mariadb" if _is_mariadb_format(load_explain_json(plan_json)) else "mysql"
    except Exception:
        return "unknown"


def analyze_plan_tool(plan_json):
    """Parse + analyze an EXPLAIN ANALYZE FORMAT=JSON plan; return the sidecar dict.

    The sidecar carries plan_summary, optimizer_switches, warnings, suggestions,
    index suggestions, the node-id'd plan tree, and the executive summary —
    everything an agent needs to reason about the plan, none of the SVG.
    """
    root = parse_explain(plan_json)
    analysis = analyze_plan(root)
    return build_sidecar(
        root, analysis, source_type="stdin", engine=_engine_of(plan_json),
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


# ---------------------------------------------------------------------------
# MCP transport (optional — imports `mcp` lazily)
# ---------------------------------------------------------------------------

def build_server():
    """Construct the FastMCP server. Imports ``mcp`` lazily.

    Raises a clear :class:`ImportError` if the optional extra isn't installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "The MCP server needs the optional 'mcp' dependency. "
            "Install it with: pip install 'myflames[mcp]'"
        ) from exc

    server = FastMCP("myflames")

    @server.tool()
    def analyze_plan(plan_json: str) -> dict:
        """Analyze a MySQL/MariaDB EXPLAIN ANALYZE FORMAT=JSON plan and return a
        compact, source-grounded analysis: summary, warnings, suggestions, index
        hints, optimizer-switch explanations, and the operator tree. Call this
        when you have a query plan and need to know why it's slow or how to fix
        it — it avoids feeding raw plan JSON into the model."""
        return analyze_plan_tool(plan_json)

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

    return server


def main():
    """Console entry point (``myflames-mcp``). Runs the server over stdio."""
    build_server().run()


if __name__ == "__main__":
    main()
