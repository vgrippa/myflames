# Diagram view: Visual Explain reference

The **diagram** output type (`--type diagram`) produces a Visual Explain–style execution plan. Its behavior and layout are aligned with MySQL Workbench’s Visual Explain.

## Implementation context

The implementation follows the replication guide in:

**`mysql-workbench/docs/VISUAL_EXPLAIN_PLAN_CONTEXT.md`**

(If you have the [mysql-workbench](https://github.com/mysql/mysql-workbench) repo locally, that path refers to the file there.)

That document describes:

- **Data source**: `EXPLAIN FORMAT=JSON` (and classic EXPLAIN). We use **MySQL 8.4 `EXPLAIN ANALYZE FORMAT=JSON`**, which has a root object with `operation` and `inputs[]`; the context doc describes the 5.6/5.7 shape with `query_block` and `nested_loop` array.
- **Node model**: QueryBlockNode, NestedLoopNode (diamond, two children: aside + below), TableNode (rectangle), OperationNode (rounded), etc.
- **Layout**: One child to the side (main line), one below (inner table); diamond centered over the join; vspacing/hspacing ~50, global_padding 20.
- **Rendering**: Cost/rows on nodes and edges; tooltips; optional cost type (Read+Eval vs Data read per join) for 5.7+.

## Our mapping

| Context document | myflames |
|------------------|----------|
| QueryBlockNode | Rightmost “query_block #1” box |
| NestedLoopNode | Diamond “nested loop”; outer input = main line (left), inner input = box below with vertical arrow into diamond |
| TableNode | Access rectangle: operation label (e.g. Unique Key Lookup), table name, key; time and row count |
| Cost display | We show **actual time** (ms/s/µs) above each node and in tooltips; cost (estimated_total_cost) in tooltips when present |
| Color | We use **time-based** hot/cold (blue = less time, red = more). Workbench uses access_type (const/ref/range/ALL) for color. |

See the docstring in `myflames/output_diagram.py` for the full mapping and checklist.

## Output format: SVG for interactive diagrams

The diagram is emitted as **SVG** (Scalable Vector Graphics). For this use case it’s a good choice:

| Aspect | SVG | HTML+Canvas | HTML+div/CSS |
|--------|-----|-------------|----------------|
| **Resolution** | Vector, sharp at any zoom | Pixel-based unless scaled | Pixel/CSS |
| **Interactivity** | Yes (events, `<title>`, script) | Yes (full control) | Yes |
| **File size** | Small for diagrams, one file | Often needs JS/CSS assets | Similar |
| **Viewing** | Browsers, IDEs, image viewers, embed in HTML | Browser only | Browser only |
| **Accessibility** | `<title>` / roles for tooltips | Need ARIA | Need ARIA |

**Recommendation:** Keep using **SVG** for the diagram. It’s standard, single-file, and works well for hover tooltips and a details bar with minimal script. For more complex interactivity (e.g. drag, zoom, drill-down), you could later wrap the same SVG in an HTML page and add extra JavaScript, or generate an HTML+SVG document that embeds the diagram.
