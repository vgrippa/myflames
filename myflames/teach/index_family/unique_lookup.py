"""Lesson: Unique Key Lookup (single-row index lookup)."""
from .. import _html


_LESSON_JS = r"""
function uniqueLookupCost(rows, covering) {
  var height = Math.max(2, Math.ceil(Math.log(Math.max(2, rows)) / Math.log(800)));
  var indexReads = height;
  var rowFetches = covering ? 0 : 1;
  return {height: height, indexReads: indexReads, rowFetches: rowFetches, total: indexReads + rowFetches};
}

function buildStage() {
  var svg = document.getElementById("unique-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  function node(x, y, w, h, text, fill, stroke) {
    var r = anim.svgEl("rect", {x: x, y: y, width: w, height: h, rx: 8, ry: 8, fill: fill, stroke: stroke, "stroke-width": 1.5});
    svg.appendChild(r);
    var t = anim.svgEl("text", {x: x + w / 2, y: y + 22, "text-anchor": "middle", "font-size": 12, "font-weight": 700, fill: "#1f2937"});
    t.textContent = text;
    svg.appendChild(t);
    return {rect: r, txt: t};
  }
  var root = node(40, 60, 180, 44, "B+tree root", "#eff6ff", "#93c5fd");
  var leaf = node(300, 60, 180, 44, "leaf entry: id=42", "#eff6ff", "#93c5fd");
  var row = node(560, 60, 180, 44, "clustered row id=42", "#fff7ed", "#fdba74");

  function arrow(x1, y1, x2, y2) {
    var p = anim.svgEl("path", {
      d: "M" + x1 + "," + y1 + " C" + (x1 + 30) + "," + y1 + " " + (x2 - 30) + "," + y2 + " " + x2 + "," + y2,
      stroke: "#9ca3af", "stroke-width": 2, fill: "none"
    });
    svg.appendChild(p);
    var h = anim.svgEl("path", {d: "M" + (x2 - 7) + "," + (y2 - 5) + " L" + x2 + "," + y2 + " L" + (x2 - 7) + "," + (y2 + 5), stroke: "#9ca3af", "stroke-width": 2, fill: "none"});
    svg.appendChild(h);
  }
  arrow(220, 82, 300, 82);
  arrow(480, 82, 560, 82);

  var status = anim.svgEl("text", {x: 400, y: 170, "text-anchor": "middle", "font-size": 12.5, "font-weight": 600, fill: "#1f2937"});
  status.textContent = "";
  svg.appendChild(status);

  return {root: root, leaf: leaf, row: row, status: status};
}

var stage = null;
function resetStage() {
  if (!stage) return;
  stage.root.rect.setAttribute("fill", "#eff6ff");
  stage.leaf.rect.setAttribute("fill", "#eff6ff");
  stage.row.rect.setAttribute("fill", "#fff7ed");
  stage.status.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var c = teachRuntime.readControls();
  var covering = !!c.covering;

  tl.mark("Traverse index");
  tl.call(function() {
    phase.textContent = "Phase 1/2 — traverse B+tree by unique key";
    stage.root.rect.setAttribute("fill", "#bfdbfe");
  });
  tl.delay(260);
  tl.call(function() {
    stage.leaf.rect.setAttribute("fill", "#93c5fd");
    stage.status.textContent = "Found exactly one leaf entry for key=id=42";
  });
  tl.delay(300);

  tl.mark("Fetch row");
  tl.call(function() {
    phase.textContent = covering
      ? "Phase 2/2 — covering lookup: no clustered row fetch needed"
      : "Phase 2/2 — non-covering: fetch one clustered row by row-id";
    if (covering) {
      stage.status.textContent = "Covering unique lookup done in index pages only";
    } else {
      stage.row.rect.setAttribute("fill", "#fed7aa");
      stage.status.textContent = "Fetched clustered row id=42";
    }
  });
  tl.delay(360);
  return tl;
}

function buildCurrentTimeline() { return buildTimeline(); }
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

function renderChart(currentRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e9,
    xLabel: "Rows in table", yLabel: "Pages/rows touched",
    curves: [
      {label: "Unique lookup (non-covering)", color: "#1d4ed8", fn: function(n) { return (Math.log(Math.max(2, n)) / Math.log(2)) + 1; }},
      {label: "Unique lookup (covering)", color: "#059669", fn: function(n) { return (Math.log(Math.max(2, n)) / Math.log(2)); }},
      {label: "Full table scan", color: "#b45309", fn: function(n) { return n; }}
    ],
    current: {x: currentRows},
    xSlider: "rows",
    xSliderTransform: function(x) { return Math.round(Math.max(1000, Math.min(1000000000, x / 1000) * 1000)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = uniqueLookupCost(c.rows, !!c.covering);
  document.getElementById("out-height").textContent = String(cost.height);
  document.getElementById("out-index").textContent = String(cost.indexReads);
  document.getElementById("out-fetch").textContent = String(cost.rowFetches);
  document.getElementById("out-total").textContent = String(cost.total);
  document.getElementById("out-exp").textContent = cost.rowFetches
    ? "Single-row lookup still does one clustered-row fetch when non-covering."
    : "Covering unique lookup finishes in the index without table-row fetch.";
  stage = buildStage();
  resetStage();
  renderChart(c.rows);
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({build: buildCurrentTimeline, reset: resetAnim});
teachRuntime.wirePhaseNav("phase-nav", {build: buildCurrentTimeline, reset: resetAnim});
"""


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (single-row unique lookup)</h2>
  <div class="control-grid">
    <div class="control">
      <label for="rows">Rows in table</label>
      <input type="range" id="rows" name="rows" min="1000" max="1000000000" step="1000" value="1000000">
      <div class="hint">Table rows: <span data-pill-for="rows">1000000</span></div>
    </div>
    <div class="control">
      <label for="covering">Covering index?</label>
      <select id="covering" name="covering">
        <option value="false" selected>No — one clustered row fetch</option>
        <option value="true">Yes — no clustered fetch</option>
      </select>
      <div class="hint">Covering removes the final table-row read.</div>
    </div>
  </div>
</section>
"""
    query_card_html = _html.query_card(
        "SELECT name FROM users WHERE id = 42;",
        "In EXPLAIN this appears as Single-row index lookup: descend the B+tree to one leaf entry; non-covering plans then do one clustered-row fetch.",
    )
    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The lookup token descends root -> internal -> leaf pages by exact key comparison.",
            "Unique key means there is at most one matching leaf entry for id=42.",
            "Non-covering path: leaf gives row-id/PK pointer, then one clustered-row fetch happens.",
            "Covering path: all selected columns are in index payload, so it returns at the leaf.",
        ],
    )
    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="unique-svg" viewBox="0 0 800 240" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""
    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (unique key lookup)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Tree height {ht("B+tree levels to reach leaf entry for a unique key.")}</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Index reads {ht("Pages touched while traversing to the matching unique entry.")}</p><p class="value" id="out-index">—</p></div>
    <div class="item"><p class="label">Row fetches {ht("Clustered table-row fetches after index hit (0 if covering, else 1).")}</p><p class="value" id="out-fetch">—</p></div>
    <div class="item"><p class="label">Total work (rough) {ht("Index reads + row fetches.")}</p><p class="value" id="out-total">—</p></div>
  </div>
  <div class="explanation" id="out-exp"></div>
  <div class="complexity-chart">
    <p class="chart-title">Unique lookup vs full scan (log–log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""
    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — unique vs non-unique lookup</summary>
  <div class="body">
    <p><strong>Unique lookup</strong> returns at most one row for a key value (eq_ref / const style access).</p>
    <p><strong>Non-unique lookup</strong> can return many matches for one value or range, which can require many clustered-row fetches when non-covering.</p>
  </div>
</details>
"""
    return _html.render_page(
        lesson_id="unique_lookup",
        title="Unique Key Lookup — single-row index lookup",
        subtitle="Understand the real background flow: B+tree descent, single leaf hit, then optional clustered-row pointer hop.",
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
    )

