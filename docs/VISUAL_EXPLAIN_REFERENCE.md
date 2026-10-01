# Visual Explain reference

Diagram and Workbench now share one renderer: **Visual Explain**, implemented in
`myflames/output_workbench.py` and `myflames/workbench.js`. `--type diagram` and
`output_diagram.render_diagram` remain compatibility aliases. See the
[Visual Explain guide](WORKBENCH_VIEW.md) for the current layout, large Venn join
symbols, operator palette, and smooth zoom controls.

The original diagram used the classic Workbench diamond layout documented in
`mysql-workbench/docs/VISUAL_EXPLAIN_PLAN_CONTEXT.md`. The shared renderer uses
Workbench 26.7's downward flow and preserves every canonical operator and edge.
Colors identify operations, arrow width represents estimated rows, and measured
time remains a separate value. Historical generated demos may show the older
layout until regenerated.

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
| `output_workbench.py` / `output_diagram.py` | No complexity chip in the chart; the report and workspace inspector provide complexity details. |

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
