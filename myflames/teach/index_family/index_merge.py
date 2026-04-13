"""Lesson: Index Merge — union / intersection / sort-union.

Animates how MySQL uses two separate index scans on the same table
and combines their row-ID sets instead of falling back to a full
table scan.

Real query: ``SELECT * FROM products WHERE category_id = 5 OR
supplier_id = 12`` with separate indexes on ``category_id`` and
``supplier_id``.
"""
from .. import _html


_LESSON_JS_TEMPLATE = r"""
function indexMergeCost(aRows, bRows, overlapPct, variant) {
  var overlap = Math.floor(Math.min(aRows, bRows) * overlapPct / 100);
  var merged;
  if (variant === "intersection") merged = overlap;
  else merged = aRows + bRows - overlap;
  return {aRows: aRows, bRows: bRows, overlap: overlap, merged: merged, variant: variant};
}

var W = 800, H = 420;
var stage = null;

// Named products so the user can track rows through the merge.
// Each product appears in one or both index scans.
var IDX_A_ROWS = [
  {name: "Laptop",     rid: "A"},
  {name: "Keyboard",   rid: "B"},
  {name: "Webcam",     rid: "C"},
  {name: "Monitor",    rid: "D"},
  {name: "Mouse",      rid: "E"},
  {name: "Headset",    rid: "F"},
  {name: "Charger",    rid: "G"}
];
var IDX_B_ROWS = [
  {name: "Tablet",     rid: "H"},
  {name: "Webcam",     rid: "C"},
  {name: "Cable",      rid: "I"},
  {name: "Monitor",    rid: "D"},
  {name: "Stand",      rid: "J"},
  {name: "Adapter",    rid: "K"}
];
// Products in BOTH scans (same name = same row)
var OVERLAP_NAMES = {"Webcam": true, "Monitor": true};

function buildStage(variant) {
  var svg = document.getElementById("merge-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var midX = W / 2;

  // Index A (left)
  var aTitle = anim.svgEl("text", {
    x: midX / 2, y: 22, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#1d4ed8"
  });
  aTitle.textContent = "idx_category scan";
  svg.appendChild(aTitle);

  var aRect = anim.svgEl("rect", {
    x: 20, y: 32, width: midX - 30, height: 120, rx: 8, ry: 8,
    fill: "#eff6ff", stroke: "#3b82f6", "stroke-width": 1.5
  });
  svg.appendChild(aRect);

  var aEntries = [];
  for (var i = 0; i < IDX_A_ROWS.length; i++) {
    var y = 48 + i * 14;
    var lbl = anim.svgEl("text", {
      x: 30, y: y, "font-size": 10, "font-weight": 600, fill: "#1e40af"
    });
    lbl.textContent = IDX_A_ROWS[i].name;
    svg.appendChild(lbl);
    var dot = anim.svgEl("circle", {
      cx: midX - 40, cy: y - 4, r: 5, fill: "#3b82f6", opacity: 0.3
    });
    svg.appendChild(dot);
    aEntries.push({lbl: lbl, dot: dot, name: IDX_A_ROWS[i].name});
  }

  // Index B (right)
  var bTitle = anim.svgEl("text", {
    x: midX + midX / 2, y: 22, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#c2410c"
  });
  bTitle.textContent = "idx_supplier scan";
  svg.appendChild(bTitle);

  var bRect = anim.svgEl("rect", {
    x: midX + 10, y: 32, width: midX - 30, height: 120, rx: 8, ry: 8,
    fill: "#fff7ed", stroke: "#f97316", "stroke-width": 1.5
  });
  svg.appendChild(bRect);

  var bEntries = [];
  for (var j = 0; j < IDX_B_ROWS.length; j++) {
    var yb = 48 + j * 14;
    var blbl = anim.svgEl("text", {
      x: midX + 20, y: yb, "font-size": 10, "font-weight": 600, fill: "#9a3412"
    });
    blbl.textContent = IDX_B_ROWS[j].name;
    svg.appendChild(blbl);
    var bdot = anim.svgEl("circle", {
      cx: W - 40, cy: yb - 4, r: 5, fill: "#f97316", opacity: 0.3
    });
    svg.appendChild(bdot);
    bEntries.push({lbl: blbl, dot: bdot, name: IDX_B_ROWS[j].name});
  }

  // Merge area (middle)
  var mergeY = 175;
  var variantLabel = variant === "intersection" ? "Intersection (AND)" :
                     variant === "sort_union" ? "Sort-Union (OR, unsorted)" : "Union (OR)";
  var mergeLbl = anim.svgEl("text", {
    x: W / 2, y: mergeY, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#7c3aed"
  });
  mergeLbl.textContent = "Merge: " + variantLabel;
  svg.appendChild(mergeLbl);

  var mergeRect = anim.svgEl("rect", {
    x: 60, y: mergeY + 8, width: W - 120, height: 55, rx: 8, ry: 8,
    fill: "#f5f3ff", stroke: "#8b5cf6", "stroke-width": 1.5
  });
  svg.appendChild(mergeRect);

  var mergeContent = anim.svgEl("text", {
    x: W / 2, y: mergeY + 40, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#5b21b6"
  });
  mergeContent.textContent = "(row-IDs merged here)";
  svg.appendChild(mergeContent);

  // Clustered index fetch area (bottom)
  var fetchY = 270;
  var fetchLbl = anim.svgEl("text", {
    x: W / 2, y: fetchY, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#047857"
  });
  fetchLbl.textContent = "Clustered index \u2192 fetch actual rows";
  svg.appendChild(fetchLbl);

  var fetchRect = anim.svgEl("rect", {
    x: 60, y: fetchY + 8, width: W - 120, height: 55, rx: 8, ry: 8,
    fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.5, opacity: 0.3
  });
  svg.appendChild(fetchRect);

  var fetchContent = anim.svgEl("text", {
    x: W / 2, y: fetchY + 40, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#065f46", opacity: 0.3
  });
  fetchContent.textContent = "";
  svg.appendChild(fetchContent);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 20, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, aEntries: aEntries, bEntries: bEntries,
    mergeRect: mergeRect, mergeContent: mergeContent,
    fetchRect: fetchRect, fetchContent: fetchContent,
    statusLbl: statusLbl, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.aEntries.length; i++) {
    stage.aEntries[i].dot.setAttribute("opacity", 0.3);
  }
  for (var j = 0; j < stage.bEntries.length; j++) {
    stage.bEntries[j].dot.setAttribute("opacity", 0.3);
  }
  stage.mergeContent.textContent = "(row-IDs merged here)";
  stage.fetchRect.setAttribute("opacity", 0.3);
  stage.fetchContent.setAttribute("opacity", 0.3);
  stage.fetchContent.textContent = "";
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function buildTimeline(variant) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var mergedSet = {};
  var mergedCount = 0;

  // Phase 1: scan index A
  tl.mark("Scan idx_category");
  tl.call(function() {
    phase.textContent = "Phase 1/3 \u2014 scanning idx_category for category_id = 5";
  });
  for (var i = 0; i < stage.aEntries.length; i++) {
    (function(idx) {
      tl.call(function() {
        stage.aEntries[idx].dot.setAttribute("opacity", 1);
        stage.aEntries[idx].dot.setAttribute("fill", "#2563eb");
      });
      tl.delay(200);
    })(i);
  }
  tl.call(function() {
    stage.mergeContent.textContent = IDX_A_ROWS.length + " row-IDs from idx_category";
  });
  tl.delay(300);

  // Phase 2: scan index B
  tl.mark("Scan idx_supplier");
  tl.call(function() {
    phase.textContent = "Phase 2/3 \u2014 scanning idx_supplier for supplier_id = 12";
  });
  for (var j = 0; j < stage.bEntries.length; j++) {
    (function(jdx) {
      tl.call(function() {
        stage.bEntries[jdx].dot.setAttribute("opacity", 1);
        stage.bEntries[jdx].dot.setAttribute("fill", "#ea580c");
      });
      tl.delay(200);
    })(j);
  }
  tl.call(function() {
    stage.mergeContent.textContent = IDX_A_ROWS.length + " + " + IDX_B_ROWS.length + " row-IDs collected";
  });
  tl.delay(300);

  // Phase 3: merge
  var mergeLabel = variant === "intersection" ? "intersecting" :
                   variant === "sort_union" ? "sorting + merging" : "merging + deduplicating";
  tl.mark("Merge row-ID sets");
  tl.call(function() {
    phase.textContent = "Phase 3/3 \u2014 " + mergeLabel + " row-ID sets";
  });

  // Highlight overlaps
  tl.call(function() {
    for (var ai = 0; ai < stage.aEntries.length; ai++) {
      if (OVERLAP_NAMES[stage.aEntries[ai].name]) {
        stage.aEntries[ai].dot.setAttribute("fill", "#7c3aed");
        stage.aEntries[ai].dot.setAttribute("r", 7);
      }
    }
    for (var bi = 0; bi < stage.bEntries.length; bi++) {
      if (OVERLAP_NAMES[stage.bEntries[bi].name]) {
        stage.bEntries[bi].dot.setAttribute("fill", "#7c3aed");
        stage.bEntries[bi].dot.setAttribute("r", 7);
      }
    }
    stage.mergeContent.textContent = "2 products in BOTH scans: Webcam, Monitor (purple)";
  });
  tl.delay(600);

  // Show result
  tl.mark("Fetch from clustered index");
  tl.call(function() {
    var result;
    if (variant === "intersection") {
      result = 2;
      stage.mergeContent.textContent = "Intersection: " + result + " row-IDs in BOTH sets";
    } else {
      result = IDX_A_ROWS.length + IDX_B_ROWS.length - 2;
      stage.mergeContent.textContent = "Union: " + result + " unique row-IDs (2 duplicates removed)";
    }
    stage.fetchRect.setAttribute("opacity", 1);
    stage.fetchContent.setAttribute("opacity", 1);
    stage.fetchContent.textContent = "Fetching " + result + " rows from clustered index";
    phase.textContent = "\u2713 Index merge complete \u2014 " + result + " rows fetched vs " +
      "full table scan";
    stage.statusLbl.textContent = "A composite index on (category_id, supplier_id) would avoid the merge entirely.";
  });

  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  return buildTimeline(c.variant);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function renderChart(overlapPct, currentA, variant) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e6,
    xLabel: "Rows per index scan (equal for both)", yLabel: "Rows fetched from clustered index",
    curves: [
      { label: "Index merge union", color: "#7c3aed",
        fn: function(n) {
          var ov = Math.floor(n * overlapPct / 100);
          return 2 * n - ov;
        }
      },
      { label: "Index merge intersection", color: "#0284c7",
        fn: function(n) { return Math.floor(n * overlapPct / 100); }
      },
      { label: "Full table scan (no merge)", color: "#dc2626",
        fn: function(n) { return n * 10; } }
    ],
    current: { x: currentA },
    xSlider: "a_rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(100, Math.min(1000000, xVal / 100) * 100)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = indexMergeCost(c.a_rows, c.b_rows, c.overlap, c.variant);
  document.getElementById("out-a-rows").textContent = teachRuntime.formatInt(cost.aRows);
  document.getElementById("out-b-rows").textContent = teachRuntime.formatInt(cost.bRows);
  document.getElementById("out-overlap").textContent = teachRuntime.formatInt(cost.overlap);
  document.getElementById("out-merged").textContent = teachRuntime.formatInt(cost.merged);
  document.getElementById("out-variant").textContent =
    cost.variant === "intersection" ? "Intersection (AND)" :
    cost.variant === "sort_union" ? "Sort-Union (OR)" : "Union (OR)";
  document.getElementById("out-explanation").textContent =
    cost.variant === "intersection"
      ? "Intersection: only " + cost.overlap + " row-IDs present in BOTH sets are fetched. " +
        "This is used for AND conditions with separate indexes."
      : "Union: " + cost.merged + " unique row-IDs fetched (" + cost.overlap +
        " duplicates removed). Used for OR conditions with separate indexes.";
  buildStage(c.variant);
  resetStage();
  renderChart(c.overlap, c.a_rows, c.variant);
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
"""


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Index Merge)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="a_rows">idx_category rows: <span class="value-pill" data-pill-for="a_rows">5000</span></label>
      <input type="range" id="a_rows" name="a_rows" min="100" max="1000000" step="100" value="5000">
      <div class="hint">Rows returned by the idx_category index scan.</div>
    </div>

    <div class="control">
      <label for="b_rows">idx_supplier rows: <span class="value-pill" data-pill-for="b_rows">3000</span></label>
      <input type="range" id="b_rows" name="b_rows" min="100" max="1000000" step="100" value="3000">
      <div class="hint">Rows returned by the idx_supplier index scan.</div>
    </div>

    <div class="control">
      <label for="overlap">Overlap (%): <span class="value-pill" data-pill-for="overlap">10</span></label>
      <input type="range" id="overlap" name="overlap" min="0" max="100" step="1" value="10">
      <div class="hint">Percentage of rows present in both index scans (duplicates).</div>
    </div>

    <div class="control">
      <label for="variant">Merge variant:</label>
      <select id="variant" name="variant">
        <option value="union" selected>Union (OR condition)</option>
        <option value="intersection">Intersection (AND condition)</option>
        <option value="sort_union">Sort-Union (OR, unsorted ranges)</option>
      </select>
      <div class="hint">Union for OR, intersection for AND, sort-union when row-IDs aren't pre-sorted.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Separate indexes on category_id and supplier_id\n"
            "SELECT *\n"
            "FROM   products\n"
            "WHERE  category_id = 5\n"
            "   OR  supplier_id = 12;"
        ),
        note=(
            "No composite index covers both conditions, but MySQL can scan "
            "both single-column indexes and merge row-ID sets before clustered fetch."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Engine executes two independent index scans and collects row-ID lists.",
            "Merge stage combines lists as union/sort-union (OR) or intersection (AND).",
            "Duplicate row-IDs are removed before table fetch to avoid repeated reads.",
            "Only merged row-IDs are fetched from clustered index in the final step.",
            "Composite indexes can remove this merge stage entirely.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="merge-svg" viewBox="0 0 800 420" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (Index Merge)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">idx_category rows {ht("Row-IDs returned by the first index scan. These are sorted by primary key for InnoDB.")}</p><p class="value" id="out-a-rows">\u2014</p></div>
    <div class="item"><p class="label">idx_supplier rows {ht("Row-IDs returned by the second index scan.")}</p><p class="value" id="out-b-rows">\u2014</p></div>
    <div class="item"><p class="label">Overlapping rows {ht("Row-IDs present in both index scans. For union, these are de-duplicated. For intersection, these are the result.")}</p><p class="value" id="out-overlap">\u2014</p></div>
    <div class="item"><p class="label">Rows fetched {ht("Final number of rows fetched from the clustered index after merging. This is the actual I/O cost.")}</p><p class="value" id="out-merged">\u2014</p></div>
    <div class="item"><p class="label">Variant {ht("Union = OR conditions (combine both sets). Intersection = AND conditions (keep only common rows). Sort-union = OR with unsorted ranges.")}</p><p class="value" id="out-variant">\u2014</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Rows fetched vs index scan size (log\u2013log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when does MySQL choose index merge?</summary>
  <div class="body">
    <p>The optimizer considers index merge when a query has OR or AND conditions
    on columns with <strong>separate single-column indexes</strong> and no
    composite index covers the full predicate.</p>

    <p><strong>Index merge union</strong> (OR): The optimizer scans each
    index separately, then merges the sorted row-ID streams with de-duplication.
    This avoids a full table scan when each index is selective enough.</p>

    <p><strong>Index merge intersection</strong> (AND): Both indexes are scanned,
    and only row-IDs present in <strong>both</strong> streams are kept. This is
    useful when no single index is selective enough, but together they are.</p>

    <p><strong>Index merge sort-union</strong> (OR with ranges): When the row-IDs
    from each index scan are not guaranteed to be in PK order (e.g. range scans),
    MySQL sorts each set first before merging. Slower than union but still faster
    than a full table scan.</p>

    <p>You can control this behaviour with <code>optimizer_switch</code>:
    <code>index_merge=on</code>, <code>index_merge_union=on</code>,
    <code>index_merge_intersection=on</code>,
    <code>index_merge_sort_union=on</code>.</p>

    <p><strong>A composite index is almost always better than index merge.</strong>
    If you see index merge in your EXPLAIN output, consider whether a composite
    index would serve the query more efficiently.</p>

    <p>Sources: MySQL 8.4 reference manual §10.2.1.3 "Index Merge Optimization";
    MariaDB Knowledge Base "Index Merge Optimization".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="index_merge",
        title="Index Merge — combining two index scans",
        subtitle=(
            "Watch MySQL scan two separate indexes, collect row-IDs, and "
            "merge them with union, intersection, or sort-union."
        ),
        version_chip="MySQL 5.1+ \u2022 MariaDB 5.1+",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
