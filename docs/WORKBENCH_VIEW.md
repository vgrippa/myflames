# Visual Explain

Open `myflames ui`, load a plan, and choose **Visual Explain** above the chart.
Diagram and Workbench now share this single view. Existing `--type diagram`
commands and Python `render_diagram` calls remain compatible aliases.
The command line produces the same view:

```bash
myflames --type workbench explain.json -o workbench.html
myflames --type workbench explain.json -o workbench.svg
```

The HTML report includes the existing findings, teaching links, and JSON-LD.
It draws the chart in the browser with the inlined myflames chart runtime, so
it needs JavaScript; **Export SVG** in the report saves a static, self-styled
copy. A `.svg` output file contains its own interaction script. Open either file
directly in a browser for interactive controls; an SVG embedded as an image is
static.

## Reading the plan

The zoom percentage follows **+**, **−**, wheel gestures, and **Fit plan**.
Click the percentage to reset to 100%. Buttons highlight when activated; a
disabled zoom button means that direction has reached its limit.
Initial view fitting scales the diagram between 75% and 100%, prioritizing width
so that wide trees remain readable instead of shrinking into illegible text.
Trees start positioned at the top inputs, and larger trees remain pannable by
dragging the canvas. In Compare mode, plans stack vertically at narrow viewport
widths to preserve readable card layouts.

Data flows from inputs at the top toward the plan output below. Every node and
parent-child edge in the myflames parsed tree is retained, including filters,
sorts, aggregates, and all branches of unions and materialization.

| Visual | Meaning |
| --- | --- |
| Red header | Table scan |
| Green header | Index lookup |
| Yellow header | Index range access |
| Orange header | Index scan |
| Large Venn circles | Highlighted logical join region; top accent identifies algorithm |
| Olive header | Other operations, such as filtering or sorting |
| Purple header | Materialization |
| Gray header | Unclassified operation |
| Arrow width | Estimated rows, on a capped logarithmic scale |

Colors identify operations, not a performance verdict. A table scan can be the
right plan. Arrows label actual and estimated rows **per loop** separately.
Nested-loop inputs are labeled outer/inner. Hash inputs are labeled build/probe
only when an explicit `Hash` child identifies the build side; otherwise they
retain their input numbers.

Each card shows self and total measured time **across loops**, plus estimated
optimizer cost as a separate value. Missing measurements read `n/a`; zero is
still zero. Cost is not milliseconds. Times adapt between microseconds,
milliseconds, and seconds; this view does not force the CLI's display-unit flag.

## Join symbols

The larger circles depict the **reported plan operator**, not row counts or
how duplicates are handled. L/R mean that operator's logical left/right sides;
SQL text order can differ after optimizer rewrites.

| Join type | Highlighted region |
| --- | --- |
| Inner | Overlap: matching row combinations |
| Left | Whole left circle, including matching and unmatched left rows |
| Right, if reported | Whole right circle |
| Semi | Overlap, labeled Semijoin: left rows with a match |
| Anti | Left circle excluding the overlap: left rows without a match |
| Unknown | Unshaded outlines |

Structured `join_type` takes priority over operation labels. MariaDB's synthetic
join nodes remain unknown because its flat input list does not reliably identify
logical join semantics. The inspector gives a text explanation for each symbol.

The README's [inner and left join samples](../README.md#query-samples-inner-join-and-left-join)
show both shadings on the same `users` and `orders` tables. Interactive copies
live in [`docs/demos/mysql-joins/`](demos/mysql-joins/).

## Interactions

- Drag the chart background to pan; scroll to zoom around the pointer. Pixel, line, and
  page deltas are normalized and animated with bounded speed and momentum, so
  small macOS mouse/trackpad gestures make small changes. Horizontal scrolling
  and zero vertical deltas do not zoom. Fit, buttons, and dragging stop pending
  wheel motion.
- Use **Fit plan**, **+**, **−**, or **100%** to change the viewport. **Fit plan**
  fits width first between 75% and 100%, starting at the top inputs while
  leaving taller trees pannable.
- Search node details to dim nonmatching operations.
- Click a node, or focus it and press Enter/Space, to see its details. In the
  workspace this also selects the matching operator in the inspector.
- Export HTML from the workspace to keep the current visualization type.

## Source reference and scope

The design reference is the downloaded MySQL Workbench **26.7.0** source,
`util_plugin/visual/explain.py`: `BaseExplainWidget`, `JoinWidget`, the tree
layout in `Explain`, and `_arrow_width`. These provide the operator palette,
overlapping-circle joins, downward flow, and estimated-row arrow conventions.
The older Workbench source's `plugins/wb.query.analysis/explain_renderer.py`
uses a different, classic diamond layout. The former **Diagram** view now
forwards to this renderer so interactions and join semantics stay consistent.

This is an independent implementation with two renderers that draw the same
picture. `apps/workspace/src/charts/workbench.ts`, built into the committed
`myflames/assets/myflames-charts.js`, draws HTML reports, `myflames explore`
pages, and the workspace from the `chart-data-1.0` payload that
`myflames/chart_data.py` builds. `myflames/output_workbench.py` and
`myflames/workbench.js` still write standalone `.svg` files. The parity suite
(`apps/workspace/test/parity.mjs`) checks that both produce the same nodes,
edges, positions, and colors for every fixture. Neither adds Workbench code,
runtime, or dependency to the package, and Python remains
standard-library-only.

The view uses myflames' existing parser, including its MySQL and MariaDB
normalization. It is not a complete reproduction of Workbench: it does not add
Workbench's raw-schema support, query-block reconstruction, or metadata that
the current parser does not retain (such as some query-block metadata).
There is one downward orientation; wide plans initially fit width between 75%
and 100%, starting at the top inputs, while larger trees can be panned. In
Compare mode, plans stack vertically at narrow widths to maintain card
legibility. Zoom in or out to inspect individual cards. The separate output
terminal is a visual endpoint, not another measured operator. Existing
analysis and JSON schemas remain unchanged.

The workspace adds shared selection, branch focus/collapse, and metric bars to
this view. SELECT-list subqueries are now preserved as additional branches. See
the [query investigation guide](QUERY_WORKSPACE.md).
