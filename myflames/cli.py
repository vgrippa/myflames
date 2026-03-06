"""
Unified CLI: flame graph, bar chart, or treemap from MySQL EXPLAIN ANALYZE JSON.
"""
import argparse
import sys
from .parser import (
    parse_explain,
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


def main():
    parser = argparse.ArgumentParser(
        description="Generate flame graph, bar chart, or treemap from MySQL EXPLAIN ANALYZE JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m myflames explain.json > query.svg
  python -m myflames --type bargraph explain.json > query-bar.svg
  python -m myflames --type treemap explain.json > query-treemap.svg
  python -m myflames --type diagram explain.json > query-diagram.svg
  python -m myflames --type diagram --diagram-engine graphviz explain.json > query-diagram.svg
        """,
    )
    parser.add_argument(
        "--type",
        choices=["flamegraph", "bargraph", "treemap", "diagram"],
        default="flamegraph",
        help="Output type: flamegraph, bargraph, treemap, diagram (default: flamegraph)",
    )
    parser.add_argument(
        "--diagram-engine",
        choices=["svg", "graphviz"],
        default="svg",
        help="Diagram layout: svg (built-in) or graphviz (requires Graphviz installed; default: svg)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="SVG width (default: 1800 flamegraph, 1200 bargraph/treemap)",
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
        "input",
        nargs="?",
        default="-",
        help="Input JSON file or - for stdin (default: -)",
    )
    args = parser.parse_args()

    # Type-specific width default
    if args.width is None:
        args.width = 1200 if args.type in ("bargraph", "treemap", "diagram") else 1800

    # Read input
    if args.input == "-":
        json_text = sys.stdin.read()
    else:
        with open(args.input, "r", encoding="utf-8", errors="replace") as f:
            json_text = f.read()

    try:
        root = parse_explain(json_text)
    except Exception as e:
        sys.stderr.write(f"Failed to parse EXPLAIN JSON: {e}\n")
        sys.exit(1)

    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    unit = "µs" if use_microseconds else "ms"
    multiplier = 1000 if use_microseconds else 1

    analysis = analyze_plan(root)

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
            svg = _re.sub(r'(height=")(\d+)(")', lambda mo: f'{mo.group(1)}{new_h}{mo.group(3)}', svg, count=1)
            # Also extend viewBox height so the panel is not clipped
            svg = _re.sub(
                r'(viewBox="0 0 \d+ )(\d+)(")',
                lambda mo: f'{mo.group(1)}{new_h}{mo.group(3)}',
                svg, count=1
            )
            svg = svg.replace("</svg>", panel_svg + "\n</svg>", 1)
        sys.stdout.write(svg)
        return

    if args.type == "bargraph":
        total_time = root["total_time"]
        if use_microseconds:
            for n in flatten_nodes(root):
                n["self_time"] *= multiplier
            total_time *= multiplier
        svg = render_bargraph(root, width=args.width, title=args.title, unit_display=unit, total_time=total_time, analysis=analysis)
        sys.stdout.write(svg)
        return

    if args.type == "treemap":
        svg = render_treemap(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis)
        sys.stdout.write(svg)
        return

    if args.type == "diagram":
        if args.diagram_engine == "graphviz":
            from .output_diagram_graphviz import render_diagram_graphviz
            svg = render_diagram_graphviz(root, width=args.width, title=args.title, unit_display=unit)
            if svg is None:
                sys.stderr.write("Graphviz not found (install it and ensure 'dot' is on PATH); using built-in diagram.\n")
                svg = render_diagram(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis)
        else:
            svg = render_diagram(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis)
        sys.stdout.write(svg)
        return

    sys.exit(1)


if __name__ == "__main__":
    main()
