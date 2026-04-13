"""
Unified CLI: flame graph, bar chart, or treemap from MySQL EXPLAIN ANALYZE JSON.
"""
import argparse
import sys
from . import __version__
from .parser import (
    parse_explain,
    load_explain_json,
    format_sql,
    build_flame_entries,
    flatten_nodes,
    enhance_tooltip_flame,
    analyze_plan,
    render_info_panel,
)
from .flamegraph import folded_to_svg
from .output_bargraph import render_bargraph
from .output_treemap import render_treemap
from .output_diagram import render_diagram
from .output_tree import render_tree
from .teach_hooks import build_teach_hooks, build_teach_index_maps


# ── View recommendation guide ────────────────────────────────────────────

_GUIDE_TEXT = """\
myflames — Which view should I use?

  flamegraph   Start here for total time distribution.
               Width = time.  See where the query spends most of its time
               across the full execution hierarchy.

  bargraph     Start here to find the slowest operators.
               Sorted by self-time.  Quickly spots the single most
               expensive step (scan, sort, join) in the plan.

  diagram      Start here to understand join order and access paths.
               Left-to-right flow like MySQL Workbench Visual Explain.
               Best for understanding how tables are joined.

  treemap      Start here to compare relative cost at a glance.
               Area = total time.  Good for large plans where you want
               a proportional overview of every operator.

  tree         Start here for big, complex plans.
               Collapsible rows with self/total time.  Navigate deep
               plans without losing context.

Quick start:
  myflames sample.json                          # flame graph (default)
  myflames --type bargraph explain.json         # bar chart
  myflames --output report.html explain.json    # self-contained HTML report
  myflames compare before.json after.json       # before vs after diff
"""


def _resolve_query_text(args, json_text):
    """Resolve query text from CLI flags or embedded JSON."""
    query_text = ""
    if args.query:
        query_text = args.query.strip()
    elif args.query_file:
        try:
            with open(args.query_file, "r", encoding="utf-8") as qf:
                query_text = qf.read().strip()
        except OSError as e:
            sys.stderr.write("Cannot read --query-file: %s\n" % e)
    if not query_text:
        try:
            raw_data = load_explain_json(json_text)
            query_text = (raw_data.get("query") or "").strip()
        except Exception:
            query_text = ""
    return query_text


def _make_svg_responsive(svg_text):
    """Add responsive styling + a ``viewBox`` to the root ``<svg>`` element.

    The flame graph / bar / treemap / diagram / tree renderers all emit
    SVGs with a fixed ``width="1800"`` (or similar). When those files are
    opened directly in a browser, they overflow the viewport horizontally
    and don't rescale on resize.

    The fix adds two things to the first ``<svg>`` element:

    * An inline ``style="max-width: 100%; height: auto;"`` so browsers
      clamp the width to the container AND recompute height from the
      scaled width. HTML-embedded SVGs already scale thanks to the rich
      template's CSS — this makes standalone ``.svg`` viewing match.

    * A ``viewBox="0 0 W H"`` attribute derived from the intrinsic
      width/height if one isn't already present. Without a viewBox, CSS
      ``max-width: 100%`` + ``height: auto`` can distort the aspect ratio
      because the browser has no intrinsic ratio to honour. The bargraph
      and some other renderers emit SVGs without a viewBox by default —
      this backfills it.

    Any pre-existing ``style=`` or ``viewBox=`` attribute is left alone.
    """
    import re as _re_local
    if not svg_text or "<svg" not in svg_text:
        return svg_text

    def _inject(match):
        tag = match.group(0)
        inner = tag[4:-1]  # strip leading "<svg" and trailing ">"
        # viewBox
        has_viewbox = "viewBox=" in inner
        if not has_viewbox:
            w_m = _re_local.search(r'\bwidth="([\d.]+)"', inner)
            h_m = _re_local.search(r'\bheight="([\d.]+)"', inner)
            if w_m and h_m:
                inner += ' viewBox="0 0 {} {}"'.format(w_m.group(1), h_m.group(1))
        # style — only inject when not already set so custom themes survive
        if 'style="' not in inner and "style='" not in inner:
            inner += ' style="max-width: 100%; height: auto;"'
        return "<svg" + inner + ">"

    return _re_local.sub(r'<svg\b[^>]*>', _inject, svg_text, count=1)


def _write_output(content, output_path):
    """Write content to file or stdout.

    SVG payloads are passed through :func:`_make_svg_responsive` first so
    standalone ``.svg`` files scale to the browser viewport instead of
    overflowing at the fixed renderer width.
    """
    if content and content.lstrip().startswith(("<?xml", "<svg")):
        content = _make_svg_responsive(content)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        sys.stderr.write("Written to %s\n" % output_path)
    else:
        sys.stdout.write(content)


def _maybe_write_sidecar(args, root, analysis, json_text, live_artifacts, query_text):
    """Write a JSON sidecar alongside the current output, if applicable.

    Keeps the CLI entry point readable by isolating all of the precedence
    logic (explicit --sidecar path vs derived from --output vs suppressed)
    in one place. Never raises on user-visible errors — we print to stderr
    and return so the primary SVG/HTML still gets written.
    """
    if args.no_sidecar:
        return
    sidecar_path = args.sidecar
    if sidecar_path == "-":
        return  # explicit suppression
    if not sidecar_path:
        # Auto-derive from --output
        if not args.output:
            return
        from .output_sidecar import sidecar_path_for
        sidecar_path = sidecar_path_for(args.output)
        if not sidecar_path:
            return

    # Figure out engine metadata from whichever path fed us the plan.
    engine = None
    engine_version = None
    source_type = "file"
    fixture_path = None
    if live_artifacts:
        source_type = "live"
        variables = live_artifacts.get("variables") or {}
        version_str = variables.get("version") or ""
        if "mariadb" in version_str.lower():
            engine = "mariadb"
        elif version_str:
            engine = "mysql"
        if version_str:
            engine_version = version_str
    elif args.input and args.input != "-":
        fixture_path = args.input
        # Detect engine from the top-level JSON shape
        try:
            from .parser import load_explain_json, _is_mariadb_format
            data = load_explain_json(json_text or "")
            engine = "mariadb" if _is_mariadb_format(data) else "mysql"
        except Exception:
            engine = "unknown"
    else:
        source_type = "stdin"

    from .output_sidecar import build_sidecar, write_sidecar
    from .parser import format_sql
    query_beautified = None
    if query_text:
        lines = format_sql(query_text)
        if lines:
            query_beautified = "\n".join(lines)

    try:
        payload = build_sidecar(
            root,
            analysis,
            source_type=source_type,
            engine=engine,
            engine_version=engine_version,
            fixture_path=fixture_path,
            query_raw=query_text or None,
            query_beautified=query_beautified,
        )
        write_sidecar(sidecar_path, payload)
        sys.stderr.write("Sidecar: {}\n".format(sidecar_path))
    except Exception as e:
        sys.stderr.write("WARN: sidecar emission failed: {}\n".format(e))


def _run_live_explain(args):
    """Connect to a live server and run the full collection pipeline.

    Returns ``(explain_json_text, live_artifacts_dict)``. On any failure
    (connection refused, auth error, bad SQL) the error is written to
    stderr and the process exits 2 — we do not try to degrade to a
    file-mode invocation.

    Flow:
      1. Open :class:`MySQLConnection` from the parsed args.
      2. ``EXPLAIN ANALYZE FORMAT=JSON`` on ``args.execute``.
      3. If enabled, collect schema / stats for the referenced tables.
      4. If enabled, collect the session variables the advisor inspects.
      5. Return everything so main() can hand it to analyze_plan + advise.
    """
    from .connector import MySQLConnection, ConnectorError
    from .collectors import (
        extract_table_names,
        collect_schema,
        collect_stats,
        collect_session_variables,
    )

    if not args.execute:
        sys.stderr.write(
            "Live-connection mode needs a query: pass -e 'SELECT ...' "
            "or --execute.\n"
        )
        sys.exit(2)

    password = args.password
    if password == "__PROMPT__":
        import getpass
        try:
            password = getpass.getpass("Enter password: ")
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\nAborted.\n")
            sys.exit(2)

    conn = MySQLConnection(
        host=args.host,
        port=args.port or 3306,
        user=args.user,
        password=password,
        database=args.database,
        ssl_mode=getattr(args, "ssl_mode", None),
        ssl_ca=getattr(args, "ssl_ca", None),
        ssl_cert=getattr(args, "ssl_cert", None),
        ssl_key=getattr(args, "ssl_key", None),
        binary=getattr(args, "mysql_binary", None),
    )

    artifacts = {"schema": {}, "stats": {}, "variables": {}}
    with conn:
        try:
            json_text = conn.explain_analyze(args.execute)
        except ConnectorError as e:
            sys.stderr.write("EXPLAIN ANALYZE failed: {}\n".format(e))
            sys.exit(2)

        tables = extract_table_names(args.execute, default_schema=args.database)

        if not args.no_collect_schema and tables:
            try:
                artifacts["schema"] = collect_schema(conn, tables)
            except ConnectorError as e:
                sys.stderr.write("Schema collection failed: {}\n".format(e))
        if not args.no_collect_stats and tables:
            try:
                artifacts["stats"] = collect_stats(conn, tables)
            except ConnectorError as e:
                sys.stderr.write("Stats collection failed: {}\n".format(e))
        if not args.no_collect_variables:
            try:
                artifacts["variables"] = collect_session_variables(conn)
            except ConnectorError as e:
                sys.stderr.write("Variables collection failed: {}\n".format(e))

    return json_text, artifacts


def _cmd_compare(argv):
    """Run before-vs-after comparison."""
    parser = argparse.ArgumentParser(
        prog="myflames compare",
        description="Generate a before-vs-after comparison HTML report.",
    )
    parser.add_argument("before", help="EXPLAIN JSON file (before)")
    parser.add_argument("after", help="EXPLAIN JSON file (after)")
    parser.add_argument(
        "--title", default="Query Plan Comparison",
        help="Report title (default: Query Plan Comparison)",
    )
    parser.add_argument(
        "--output", "-o", default=None, metavar="PATH",
        help="Write output to file instead of stdout",
    )
    args = parser.parse_args(argv)

    from .output_compare import render_compare

    with open(args.before, "r", encoding="utf-8", errors="replace") as f:
        json_before = f.read()
    with open(args.after, "r", encoding="utf-8", errors="replace") as f:
        json_after = f.read()

    html = render_compare(json_before, json_after, title=args.title)
    _write_output(html, args.output)


def main():
    # Handle subcommands before argparse to avoid conflicts with positional args
    if len(sys.argv) > 1:
        if sys.argv[1] == "guide":
            sys.stdout.write(_GUIDE_TEXT)
            return
        if sys.argv[1] == "compare":
            _cmd_compare(sys.argv[2:])
            return
        if sys.argv[1] == "teach":
            from .teach import cmd_teach
            cmd_teach(sys.argv[2:])
            return

    parser = argparse.ArgumentParser(
        prog="myflames",
        description="MySQL EXPLAIN ANALYZE visualizer: flame graphs, bar charts, treemaps, diagrams, and execution trees.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # add_help=False so the short flag ``-h`` is free for ``--host`` — we
        # add our own ``--help`` below. ``mysql -h host`` users expect this.
        add_help=False,
        epilog="""\
Examples:
  myflames explain.json                                 # flame graph from file
  myflames --type bargraph explain.json > bar.svg       # bar chart
  myflames --output report.html explain.json            # HTML report
  myflames -h db.example.com -u admin -p \\\
           -e 'SELECT * FROM t WHERE id=1'              # connect + explain
  myflames compare before.json after.json               # before vs after diff
  myflames guide                                        # which view?
  myflames teach btree -o btree.html                    # interactive lesson

Subcommands:
  compare   Compare before/after EXPLAIN JSON files
  guide     Show which view to pick for your use case
  teach     Interactive algorithm lessons (btree, bnl, hash, join, lru)
""",
    )
    parser.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )
    parser.add_argument(
        "--version", action="version",
        version="myflames %s" % __version__,
    )
    # ---- Connection flags (match the mysql CLI) ----
    conn = parser.add_argument_group("Connection (live MySQL / MariaDB)")
    conn.add_argument(
        "-h", "--host", default=None, metavar="HOST",
        help="Server hostname (enables live connection mode).",
    )
    conn.add_argument(
        "-P", "--port", type=int, default=None, metavar="PORT",
        help="Server port (default: 3306).",
    )
    conn.add_argument(
        "-u", "--user", default=None, metavar="USER",
        help="Username.",
    )
    # Match ``mysql -ppassword`` exactly: ``nargs="?"`` + ``const`` sentinel
    # lets us distinguish ``-p`` (prompt) from ``-psecret``.
    conn.add_argument(
        "-p", "--password", nargs="?", default=None, const="__PROMPT__",
        metavar="PASSWORD",
        help="Password. Use '-p' alone to be prompted, or '-psecret' inline.",
    )
    conn.add_argument(
        "-D", "--database", default=None, metavar="DB",
        help="Default database.",
    )
    conn.add_argument(
        "--ssl-mode", default=None, metavar="MODE",
        help="SSL mode (DISABLED, PREFERRED, REQUIRED, VERIFY_CA, VERIFY_IDENTITY).",
    )
    conn.add_argument("--ssl-ca",   default=None, metavar="PATH", help="Path to CA bundle.")
    conn.add_argument("--ssl-cert", default=None, metavar="PATH", help="Client certificate.")
    conn.add_argument("--ssl-key",  default=None, metavar="PATH", help="Client key.")
    conn.add_argument(
        "--mysql-binary", default=None, metavar="PATH",
        help="Override the mysql/mariadb client binary path (default: search PATH).",
    )
    conn.add_argument(
        "-e", "--execute", default=None, metavar="SQL", dest="execute",
        help="SQL to EXPLAIN ANALYZE against the live connection.",
    )
    conn.add_argument(
        "--no-collect-schema", action="store_true", dest="no_collect_schema",
        help="Disable SHOW CREATE TABLE collection (enabled by default).",
    )
    conn.add_argument(
        "--no-collect-stats", action="store_true", dest="no_collect_stats",
        help="Disable information_schema.tables stats collection (enabled by default).",
    )
    conn.add_argument(
        "--no-collect-variables", action="store_true", dest="no_collect_variables",
        help="Disable SHOW SESSION VARIABLES collection (enabled by default).",
    )
    # ---- Sidecar flags ----
    sidecar_grp = parser.add_argument_group("Sidecar (machine-readable output)")
    sidecar_grp.add_argument(
        "--sidecar", default=None, metavar="PATH",
        help="Write a JSON sidecar to PATH. When omitted but --output is given, "
             "a sidecar is written automatically at <output>.json. Pass '-' to "
             "suppress sidecar emission.",
    )
    sidecar_grp.add_argument(
        "--no-sidecar", action="store_true", dest="no_sidecar",
        help="Do not emit a sidecar file even when --output is set.",
    )
    parser.add_argument(
        "--type",
        choices=["flamegraph", "bargraph", "treemap", "diagram", "tree"],
        default="flamegraph",
        help="Output type (default: flamegraph)",
    )
    parser.add_argument(
        "--output", "-o", default=None, metavar="PATH",
        help="Write output to file instead of stdout. Use .html extension for a self-contained HTML report",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="SVG width (default: 1800 flamegraph, 1200 others)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=32,
        help="Frame height for flame graph (default: 32)",
    )
    parser.add_argument(
        "--colors",
        default="hot",
        help="Color scheme for flame graph (default: hot)",
    )
    parser.add_argument(
        "--title",
        default="MySQL Query Plan",
        help="Chart title",
    )
    parser.add_argument(
        "--inverted",
        action="store_true",
        help="Flame graph: icicle (inverted)",
    )
    parser.add_argument(
        "--no-enhance",
        action="store_true",
        dest="no_enhance",
        help="Disable detailed tooltips on flame graph",
    )
    parser.add_argument(
        "--query",
        default=None,
        metavar="SQL",
        help="SQL query text to embed in the output",
    )
    parser.add_argument(
        "--query-file",
        default=None,
        metavar="PATH",
        dest="query_file",
        help="Path to a file containing the SQL query to embed",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Input JSON file or - for stdin (default: -)",
    )
    args = parser.parse_args()

    # ── Live-connection mode ────────────────────────────────────────────
    # When --host is given, myflames connects to the server, runs EXPLAIN
    # ANALYZE on --execute, collects schema/stats/session variables, and
    # feeds everything into the advisor. The fetched JSON is then treated
    # exactly like a fixture from disk so the rest of the pipeline is
    # unchanged.
    json_text = None
    live_artifacts = None
    if args.host:
        json_text, live_artifacts = _run_live_explain(args)
        # Live mode synthesises the query text from --execute so the
        # renderer's query panel shows what was actually analyzed.
        if not args.query and not args.query_file and args.execute:
            args.query = args.execute
        # The rest of main() expects args.input to be either "-" or a file;
        # flag it as "-" since we have json_text in-hand.
        args.input = "-"

    # Show help if no input and stdin is a terminal
    if args.input == "-" and sys.stdin.isatty() and json_text is None:
        parser.print_help()
        return

    # Type-specific width default
    if args.width is None:
        args.width = 1200 if args.type in ("bargraph", "treemap", "diagram", "tree") else 1800

    # Read input (skipped in live mode — json_text is already set)
    if json_text is None:
        if args.input == "-":
            json_text = sys.stdin.read()
        else:
            with open(args.input, "r", encoding="utf-8", errors="replace") as f:
                json_text = f.read()

    # Detect output format from --output extension
    output_path = args.output
    is_html = output_path and output_path.lower().endswith(".html")

    if is_html:
        from .output_html_report import render_html_report
        query_text = _resolve_query_text(args, json_text)
        html = render_html_report(
            json_text,
            view_type=args.type,
            width=args.width,
            title=args.title,
            query_text=query_text,
            live_artifacts=live_artifacts,
            frame_height=args.height,
            colors=args.colors,
            inverted=args.inverted,
        )
        _write_output(html, output_path)
        # Sidecar emission for HTML path — same precedence as the SVG path.
        # We re-parse here because the analyze_plan/advise pipeline below is
        # bypassed in the short-circuit branch; this stays cheap because
        # parse_explain is idempotent and the input is already in memory.
        try:
            root_for_sc = parse_explain(json_text)
            analysis_for_sc = analyze_plan(root_for_sc)
            if query_text:
                analysis_for_sc["query_text_lines"] = format_sql(query_text)
            if live_artifacts:
                from .advisor import advise
                advise(
                    analysis_for_sc,
                    schema=live_artifacts.get("schema"),
                    stats=live_artifacts.get("stats"),
                    variables=live_artifacts.get("variables"),
                )
            _maybe_write_sidecar(
                args, root_for_sc, analysis_for_sc, json_text,
                live_artifacts, query_text,
            )
        except Exception as e:
            sys.stderr.write(
                "WARN: sidecar emission failed for HTML output: {}\n".format(e)
            )
        return

    try:
        root = parse_explain(json_text)
    except Exception as e:
        sys.stderr.write("Failed to parse EXPLAIN JSON: %s\n" % e)
        sys.exit(1)

    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    unit = "\u00b5s" if use_microseconds else "ms"
    multiplier = 1000 if use_microseconds else 1

    analysis = analyze_plan(root)

    query_text = _resolve_query_text(args, json_text)
    if query_text:
        analysis["query_text_lines"] = format_sql(query_text)

    # Feed collected environment data (if any) into the advisor.
    if live_artifacts:
        from .advisor import advise
        advise(
            analysis,
            schema=live_artifacts.get("schema"),
            stats=live_artifacts.get("stats"),
            variables=live_artifacts.get("variables"),
        )
    teach_maps = build_teach_index_maps(
        build_teach_hooks(
            root,
            query_sql=query_text,
            variables=(live_artifacts or {}).get("variables") or analysis.get("collected_variables"),
            stats=(live_artifacts or {}).get("stats") or analysis.get("collected_stats") or {},
        )
    )

    # ---- Sidecar emission ----------------------------------------------
    # The sidecar path is derived from --output by default. Precedence:
    #   1. --no-sidecar        → never emit
    #   2. --sidecar -         → suppress (matches shell "to stdout" convention
    #                            without forcing us to stream JSON to stdout)
    #   3. --sidecar PATH      → write at PATH exactly
    #   4. --output PATH       → write at <output>.json (auto)
    #   5. no --output         → skip (no place to land)
    _maybe_write_sidecar(args, root, analysis, json_text, live_artifacts, query_text)

    if args.type == "flamegraph":
        entries = list(build_flame_entries(root))
        folded_lines = []
        for path, time in entries:
            t = time * multiplier
            t = int(t + 0.5)
            t = 1 if t == 0 and len(path) == 1 else t
            if t <= 0:
                continue
            folded_lines.append(";".join(path) + " " + str(t))
        folded_text = "\n".join(folded_lines)
        if not folded_text.strip():
            sys.stderr.write("No flame graph data (all zero self-time).\n")
            sys.exit(1)
        svg = folded_to_svg(
            folded_text,
            title=args.title,
            width=args.width,
            height=args.height,
            countname=unit,
            inverted=args.inverted,
            colors=args.colors,
            teach_index_by_folded=teach_maps["by_folded_label"],
        )
        if not args.no_enhance:
            from .parser import xml_escape
            op_details = {n["folded_label"]: n["details"] for n in flatten_nodes(root)}
            import re
            def repl(m):
                original = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
                enhanced = enhance_tooltip_flame(original, op_details)
                return "<title>" + xml_escape(enhanced).replace("\n", "&#10;") + "</title>"
            svg = re.sub(r"<title>([^<]+)</title>", repl, svg)
        # Inject consolidated info panel (How to read + Query Analysis) into flamegraph SVG
        import re as _re
        m_height = _re.search(r'<svg[^>]+height="(\d+)"', svg)
        m_width = _re.search(r'<svg[^>]+width="(\d+)"', svg)
        if m_height and m_width:
            old_h = int(m_height.group(1))
            svg_w = int(m_width.group(1))
            panel_lines, panel_h = render_info_panel(analysis, 4, old_h + 8, svg_w - 8, view_type="flamegraph")
            new_h = old_h + 8 + panel_h
            panel_svg = "\n".join(panel_lines)
            svg = _re.sub(r'(height=")(\d+)(")', lambda mo: '%s%s%s' % (mo.group(1), new_h, mo.group(3)), svg, count=1)
            svg = _re.sub(
                r'(viewBox="0 0 \d+ )(\d+)(")',
                lambda mo: '%s%s%s' % (mo.group(1), new_h, mo.group(3)),
                svg, count=1
            )
            svg = svg.replace("</svg>", panel_svg + "\n</svg>", 1)
        _write_output(svg, output_path)
        return

    if args.type == "bargraph":
        total_time = root["total_time"]
        if use_microseconds:
            for n in flatten_nodes(root):
                n["self_time"] *= multiplier
            total_time *= multiplier
        svg = render_bargraph(
            root,
            width=args.width,
            title=args.title,
            unit_display=unit,
            total_time=total_time,
            analysis=analysis,
            teach_index_by_folded=teach_maps["by_folded_label"],
        )
        _write_output(svg, output_path)
        return

    if args.type == "treemap":
        svg = render_treemap(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis, teach_index_by_folded=teach_maps["by_folded_label"])
        _write_output(svg, output_path)
        return

    if args.type == "diagram":
        svg = render_diagram(
            root,
            width=args.width,
            title=args.title,
            unit_display=unit,
            analysis=analysis,
            teach_index_by_folded=teach_maps["by_folded_label"],
        )
        _write_output(svg, output_path)
        return

    if args.type == "tree":
        svg = render_tree(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis, teach_index_by_folded=teach_maps["by_folded_label"])
        _write_output(svg, output_path)
        return

    sys.exit(1)


if __name__ == "__main__":
    main()
