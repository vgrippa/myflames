"""
Programmatic API: render EXPLAIN ANALYZE JSON to SVG by type.
Used by HTTP APIs (e.g. releem_flames) without subprocess.
"""
import re

from .parser import (
    parse_explain,
    load_explain_json,
    format_sql,
    build_flame_entries,
    flatten_nodes,
    enhance_tooltip_flame,
    analyze_plan,
    render_info_panel,
    xml_escape,
)
from .flamegraph import folded_to_svg
from .output_bargraph import render_bargraph
from .output_treemap import render_treemap
from .output_diagram import render_diagram
from .output_tree import render_tree


def render_explain(
    json_text,
    output_type,
    title="MySQL Query Plan",
    width=None,
    height=32,
    colors="hot",
    inverted=False,
    no_enhance=False,
):
    """
    Render EXPLAIN ANALYZE JSON to SVG.

    :param json_text: Raw JSON string (EXPLAIN ANALYZE FORMAT=JSON output).
    :param output_type: One of "flamegraph", "bargraph", "treemap", "diagram", "tree".
    :param title: Chart title.
    :param width: SVG width (default 1800 for flamegraph, 1200 for others).
    :param height: Frame height for flamegraph only (default 32).
    :param colors: Flamegraph color scheme: "hot", "blue", "green", "mem", "io".
    :param inverted: Flamegraph only: icicle (inverted).
    :param no_enhance: Flamegraph only: disable detailed tooltips.
    :return: SVG string.
    """
    if width is None:
        width = 1200 if output_type in ("bargraph", "treemap", "diagram", "tree") else 1800

    root = parse_explain(json_text)
    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    unit = "µs" if use_microseconds else "ms"
    multiplier = 1000 if use_microseconds else 1

    analysis = analyze_plan(root)
    try:
        raw_data = load_explain_json(json_text)
        query_text = (raw_data.get("query") or "").strip()
    except Exception:
        query_text = ""
    if query_text:
        analysis["query_text_lines"] = format_sql(query_text)

    if output_type == "flamegraph":
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
            raise ValueError("No flame graph data (all zero self-time)")
        svg = folded_to_svg(
            folded_text,
            title=title,
            width=width,
            height=height,
            countname=unit,
            inverted=inverted,
            colors=colors,
        )
        if not no_enhance:
            op_details = {n["folded_label"]: n["details"] for n in flatten_nodes(root)}

            def repl(m):
                original = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
                enhanced = enhance_tooltip_flame(original, op_details)
                return "<title>" + xml_escape(enhanced).replace("\n", "&#10;") + "</title>"

            svg = re.sub(r"<title>([^<]+)</title>", repl, svg)
        m_height = re.search(r'<svg[^>]+height="(\d+)"', svg)
        m_width = re.search(r'<svg[^>]+width="(\d+)"', svg)
        if m_height and m_width:
            old_h = int(m_height.group(1))
            svg_w = int(m_width.group(1))
            panel_lines, panel_h = render_info_panel(analysis, 4, old_h + 8, svg_w - 8, view_type="flamegraph")
            new_h = old_h + 8 + panel_h
            panel_svg = "\n".join(panel_lines)
            svg = re.sub(r'(height=")(\d+)(")', lambda mo: mo.group(1) + str(new_h) + mo.group(3), svg, count=1)
            svg = re.sub(
                r'(viewBox="0 0 \d+ )(\d+)(")',
                lambda mo: mo.group(1) + str(new_h) + mo.group(3),
                svg,
                count=1,
            )
            svg = svg.replace("</svg>", panel_svg + "\n</svg>", 1)
        return svg

    if output_type == "bargraph":
        total_time = root["total_time"]
        if use_microseconds:
            for n in flatten_nodes(root):
                n["self_time"] *= multiplier
            total_time *= multiplier
        return render_bargraph(
            root, width=width, title=title, unit_display=unit, total_time=total_time, analysis=analysis
        )

    if output_type == "treemap":
        return render_treemap(root, width=width, title=title, unit_display=unit, analysis=analysis)

    if output_type == "diagram":
        return render_diagram(root, width=width, title=title, unit_display=unit, analysis=analysis)

    if output_type == "tree":
        return render_tree(root, width=width, title=title, unit_display=unit, analysis=analysis)

    raise ValueError("output_type must be one of: flamegraph, bargraph, treemap, diagram, tree")
