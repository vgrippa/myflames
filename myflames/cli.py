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


def _write_output(content, output_path):
    """Write content to file or stdout."""
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        sys.stderr.write("Written to %s\n" % output_path)
    else:
        sys.stdout.write(content)


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

    parser = argparse.ArgumentParser(
        prog="myflames",
        description="MySQL EXPLAIN ANALYZE visualizer: flame graphs, bar charts, treemaps, diagrams, and execution trees.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  myflames explain.json                           # flame graph to stdout
  myflames --type bargraph explain.json > bar.svg # bar chart
  myflames --output report.html explain.json      # self-contained HTML report
  myflames compare before.json after.json         # before vs after diff
  myflames guide                                  # which view should I use?

Subcommands:
  compare   Compare before/after EXPLAIN JSON files
  guide     Show which view to pick for your use case
""",
    )
    parser.add_argument(
        "--version", action="version",
        version="myflames %s" % __version__,
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

    # Show help if no input and stdin is a terminal
    if args.input == "-" and sys.stdin.isatty():
        parser.print_help()
        return

    # Type-specific width default
    if args.width is None:
        args.width = 1200 if args.type in ("bargraph", "treemap", "diagram", "tree") else 1800

    # Read input
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
            frame_height=args.height,
            colors=args.colors,
            inverted=args.inverted,
        )
        _write_output(html, output_path)
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
        svg = render_bargraph(root, width=args.width, title=args.title, unit_display=unit, total_time=total_time, analysis=analysis)
        _write_output(svg, output_path)
        return

    if args.type == "treemap":
        svg = render_treemap(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis)
        _write_output(svg, output_path)
        return

    if args.type == "diagram":
        svg = render_diagram(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis)
        _write_output(svg, output_path)
        return

    if args.type == "tree":
        svg = render_tree(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis)
        _write_output(svg, output_path)
        return

    sys.exit(1)


if __name__ == "__main__":
    main()
