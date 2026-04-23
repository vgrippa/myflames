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

## Big O complexity annotations (schema 1.2+)

Every operator node parsed by `myflames.parser.parse_node` gets a `complexity` dict attached to its `details` map. The single source of truth lives in `myflames/complexity.py`; every renderer and the JSON sidecar consume the same structure.

### Node shape addition

```python
node["details"]["complexity"] = {
    "big_o":     "O(n · log m)",     # the formula, exact
    "short":     "n · log m",        # compact form used on chips
    "severity":  "good" | "medium" | "bad",
    "rationale": "Indexed nested loop: each outer row probes the inner tree.",
    "confidence":"exact" | "typical" | "worst_case",
    "learn_more":"nested_loop_join",  # glossary key, optional
}
```

For `access_type == "materialize"` (two-phase operator) the dict also carries `build_complexity` and `scan_complexity` sub-dicts rather than collapsing into one `big_o` string.

`compute_complexity(node)` returns `None` when the operator is unrecognised or when a required signal is missing — renderers omit the chip in that case rather than display a misleading value.

### Renderer surface area

| Renderer | Big O surface |
|---|---|
| `flamegraph.py` | Colored severity dot at the right edge of every bar; compact `O(...)` appended to the bar label when there is ≥ 120 px of width available; tooltip gains a `Complexity: O(...)` line. |
| `output_bargraph.py` | Dedicated **COMPLEXITY** column with a color-coded pill between the operation label and the loops count (hidden at canvas widths below 900 px). |
| `output_treemap.py` | `data-complexity="O(...)"` attribute on every tile; colored corner chip on tiles larger than `80 × 40 px`. |
| `output_diagram.py` | Colored chip below the operator box; for join diamonds the chip sits below the diamond. Chip lives on the join node only — inner children of a join never display their own chip to avoid duplication. |

All three non-flamegraph renderers embed the same legend block (rendered by `myflames/complexity_legend.py`) at the bottom of the canvas so a newcomer can decode the chips without leaving the page.

### Severity palette

Reused across every surface — do not hard-code these values elsewhere:

| Severity | Color | Meaning |
|---|---|---|
| `good` | `rgb(100,180,180)` | constant / logarithmic — stays fast as data grows |
| `medium` | `rgb(255,200,50)` | scales linearly or with a log factor |
| `bad` | `rgb(255,90,90)` | quadratic or worse — risks timeout at scale |

### JSON sidecar (schema 1.2)

When any node carries complexity metadata, the sidecar payload emits an additional top-level array:

```json
"operator_complexities": [
  {
    "folded_label": "NESTED LOOP",
    "short_label": "Nested loop inner join",
    "complexity": {
      "big_o": "O(n · log m)",
      "short": "n · log m",
      "severity": "medium",
      "rationale": "Indexed nested loop: each outer row probes the inner table via an index descent.",
      "confidence": "exact",
      "learn_more": "nested_loop_join"
    }
  }
]
```

The array is **omitted entirely** when no node has complexity metadata, so consumers pinned to the 1.1 shape only need to handle an additional optional key. `validate_sidecar()` rejects entries that violate the severity / confidence enums or that are missing required string fields.
