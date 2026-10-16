"""
Programmatic API: render EXPLAIN ANALYZE JSON to SVG by type.
Used by HTTP APIs (e.g. releem_flames) without subprocess.
"""
import re
import warnings

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
from .flamegraph import folded_to_svg, plan_folded
from .output_bargraph import render_bargraph
from .output_diagram import render_diagram
from .output_tree import render_tree
from .teach_hooks import build_teach_hooks, build_teach_index_maps


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
    :param output_type: One of "flamegraph", "bargraph", "diagram", "workbench", "tree".
        "treemap" is a deprecated alias for "bargraph".
    :param title: Chart title.
    :param width: SVG width (default 1800 for flamegraph, 1200 for others).
    :param height: Frame height for flamegraph only (default 32).
    :param colors: Flamegraph color scheme: "hot", "blue", "green", "mem", "io".
    :param inverted: Flamegraph only: icicle (inverted).
    :param no_enhance: Flamegraph only: disable detailed tooltips.
    :return: SVG string.
    """
    if output_type == "treemap":
        warnings.warn(
            "output_type='treemap' is deprecated; the treemap view was removed. "
            "Rendering the bar chart instead.",
            DeprecationWarning, stacklevel=2,
        )
        output_type = "bargraph"
    if width is None:
        width = 1200 if output_type in ("bargraph", "diagram", "workbench", "tree") else 1800

    root = parse_explain(json_text)
    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    unit = "µs" if use_microseconds else "ms"

    analysis = analyze_plan(root)
    try:
        raw_data = load_explain_json(json_text)
        query_text = (raw_data.get("query") or "").strip()
    except Exception:
        query_text = ""
    if query_text:
        analysis["query_text_lines"] = format_sql(query_text)
    teach_maps = build_teach_index_maps(build_teach_hooks(root, query_sql=query_text))

    if output_type == "flamegraph":
        # Whole microseconds per measured operator (flamegraph.flame_scale);
        # tooltips print readable units (flamegraph.flame_value_text).
        folded_text, flame_unit = plan_folded(build_flame_entries(root), root["total_time"],
                                              keep_root=True)
        if not folded_text.strip():
            raise ValueError("No flame graph data (all zero self-time)")
        # Build a folded-label → complexity map so flamegraph bars can show
        # the severity dot and (where space permits) the compact O(...) form.
        _complexity_by_folded = {}
        for _n in flatten_nodes(root):
            _c = (_n.get("details") or {}).get("complexity")
            if isinstance(_c, dict) and _c.get("big_o"):
                _complexity_by_folded.setdefault(_n.get("folded_label") or "", _c)
        svg = folded_to_svg(
            folded_text,
            title=title,
            width=width,
            height=height,
            countname=flame_unit,
            inverted=inverted,
            colors=colors,
            teach_index_by_folded=teach_maps["by_folded_label"],
            complexity_by_folded=_complexity_by_folded or None,
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
        return render_bargraph(
            root,
            width=width,
            title=title,
            unit_display=unit,
            total_time=total_time,
            analysis=analysis,
            teach_index_by_folded=teach_maps["by_folded_label"],
        )


    if output_type in ("diagram", "workbench"):
        return render_diagram(
            root,
            width=width,
            title=title,
            unit_display=unit,
            analysis=analysis,
            teach_index_by_folded=teach_maps["by_folded_label"],
        )

    if output_type == "tree":
        return render_tree(root, width=width, title=title, unit_display=unit, analysis=analysis, teach_index_by_folded=teach_maps["by_folded_label"])

    raise ValueError("output_type must be one of: flamegraph, bargraph, diagram, workbench, tree")
