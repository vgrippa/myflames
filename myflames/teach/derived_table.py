"""Lesson: Derived Table Materialization.

Animates how a FROM-clause subquery (derived table) that cannot be
merged into the outer query is materialized into a temporary table,
optionally auto-indexed, and then probed by the outer query.

Concrete example: departments joined to a grouped subquery computing
average salary per department.
"""
from . import _html


_LESSON_JS_TEMPLATE = r"""
function derivedCost(subqueryRows, rowSize, outerRows, hasIndex) {
  var materializeWrites = subqueryRows;
  var tmpSize = subqueryRows * rowSize;
  var spillsToDisk = tmpSize > 16 * 1024 * 1024;  // 16 MiB
  var totalReads;
  var readsPerProbe = 0;
  var height = 0;
  if (hasIndex) {
    height = Math.max(1, Math.ceil(Math.log(Math.max(2, subqueryRows)) / Math.log(800)));
    readsPerProbe = height;
    totalReads = outerRows * readsPerProbe;
  } else {
    totalReads = subqueryRows;  // full scan once
  }
  var totalIO = materializeWrites + totalReads;
  return {
    materializeWrites: materializeWrites,
    tmpSize: tmpSize,
    spillsToDisk: spillsToDisk,
    hasIndex: hasIndex,
    height: height,
    readsPerProbe: readsPerProbe,
    totalReads: totalReads,
    totalIO: totalIO
  };
}

var W = 800, H = 440;
var stage = null;

function buildStage() {
  var svg = document.getElementById("derived-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // Subquery source (top)
  var srcLbl = anim.svgEl("text", {
    x: 20, y: 28, "font-size": 13, "font-weight": 700, fill: "#0c4a6e"
  });
  srcLbl.textContent = "Subquery: SELECT dept_id, AVG(salary) FROM employees GROUP BY dept_id";
  svg.appendChild(srcLbl);

  var srcRect = anim.svgEl("rect", {
    x: 20, y: 40, width: W - 40, height: 50, rx: 8, ry: 8,
    fill: "#f0f9ff", stroke: "#0284c7", "stroke-width": 1.5
  });
  svg.appendChild(srcRect);

  // Temp table (middle)
  var tmpY = 130;
  var tmpLbl = anim.svgEl("text", {
    x: 20, y: tmpY, "font-size": 13, "font-weight": 700, fill: "#92400e"
  });
  tmpLbl.textContent = "Materialized temp table (stats)";
  svg.appendChild(tmpLbl);

  var tmpRect = anim.svgEl("rect", {
    x: 20, y: tmpY + 12, width: W - 40, height: 70, rx: 8, ry: 8,
    fill: "#fffbeb", stroke: "#d97706", "stroke-width": 1.5
  });
  svg.appendChild(tmpRect);

  var tmpContent = anim.svgEl("text", {
    x: W / 2, y: tmpY + 52, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#92400e"
  });
  tmpContent.textContent = "(subquery rows materialize here)";
  svg.appendChild(tmpContent);

  // Capacity bar inside temp table
  var barX = 30, barY = tmpY + 22, barW = W - 60, barH = 14;
  var barBg = anim.svgEl("rect", {
    x: barX, y: barY, width: barW, height: barH, rx: 3, ry: 3,
    fill: "#fef3c7", stroke: "#fde68a", "stroke-width": 1
  });
  svg.appendChild(barBg);
  var barFill = anim.svgEl("rect", {
    x: barX, y: barY, width: 0, height: barH, rx: 3, ry: 3,
    fill: "#f59e0b"
  });
  svg.appendChild(barFill);
  var barPct = anim.svgEl("text", {
    x: barX + barW / 2, y: barY + 11, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#78350f"
  });
  barPct.textContent = "0%";
  svg.appendChild(barPct);

  // Spill indicator
  var spillLbl = anim.svgEl("text", {
    x: W - 30, y: tmpY + 70, "text-anchor": "end",
    "font-size": 10, "font-weight": 700, fill: "#dc2626", opacity: 0
  });
  spillLbl.textContent = "\u26a0 spills to disk";
  svg.appendChild(spillLbl);

  // Index badge on temp table
  var indexBadge = anim.svgEl("rect", {
    x: W - 180, y: tmpY + 12, width: 140, height: 20, rx: 10, ry: 10,
    fill: "#dbeafe", stroke: "#3b82f6", "stroke-width": 1.5, opacity: 0
  });
  svg.appendChild(indexBadge);
  var indexLbl = anim.svgEl("text", {
    x: W - 110, y: tmpY + 26, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#1e40af", opacity: 0
  });
  indexLbl.textContent = "auto-index (dept_id)";
  svg.appendChild(indexLbl);

  // Outer query area (bottom-left)
  var outerY = 260;
  var outerLbl = anim.svgEl("text", {
    x: 20, y: outerY, "font-size": 13, "font-weight": 700, fill: "#4338ca"
  });
  outerLbl.textContent = "Outer query: departments d JOIN stats ON d.id = stats.dept_id";
  svg.appendChild(outerLbl);

  var outerRect = anim.svgEl("rect", {
    x: 20, y: outerY + 12, width: 340, height: 50, rx: 8, ry: 8,
    fill: "#eef2ff", stroke: "#6366f1", "stroke-width": 1.5
  });
  svg.appendChild(outerRect);

  // Result area (bottom-right)
  var resultRect = anim.svgEl("rect", {
    x: 440, y: outerY + 12, width: 340, height: 50, rx: 8, ry: 8,
    fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.5
  });
  svg.appendChild(resultRect);
  var resultLbl = anim.svgEl("text", {
    x: 610, y: outerY + 42, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#065f46"
  });
  resultLbl.textContent = "Result rows";
  svg.appendChild(resultLbl);

  // Arrow from outer to temp table
  var probeArrow = anim.svgEl("line", {
    x1: 190, y1: outerY + 12, x2: 190, y2: tmpY + 82,
    stroke: "#6366f1", "stroke-width": 1.5,
    "stroke-dasharray": "4,3", "marker-end": "", opacity: 0
  });
  svg.appendChild(probeArrow);

  // Arrow from temp table to result
  var resultArrow = anim.svgEl("line", {
    x1: 610, y1: tmpY + 82, x2: 610, y2: outerY + 12,
    stroke: "#059669", "stroke-width": 1.5,
    "stroke-dasharray": "4,3", "marker-end": "", opacity: 0
  });
  svg.appendChild(resultArrow);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 20, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, srcRect: srcRect,
    tmpRect: tmpRect, tmpContent: tmpContent, tmpLbl: tmpLbl,
    barFill: barFill, barBg: barBg, barPct: barPct,
    barX: barX, barW: barW,
    spillLbl: spillLbl,
    indexBadge: indexBadge, indexLbl: indexLbl,
    outerRect: outerRect, outerLbl: outerLbl,
    resultRect: resultRect, resultLbl: resultLbl,
    probeArrow: probeArrow, resultArrow: resultArrow,
    statusLbl: statusLbl, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  stage.tmpRect.setAttribute("fill", "#fffbeb");
  stage.tmpRect.setAttribute("stroke", "#d97706");
  stage.tmpContent.textContent = "(subquery rows materialize here)";
  stage.barFill.setAttribute("width", 0);
  stage.barFill.setAttribute("fill", "#f59e0b");
  stage.barPct.textContent = "0%";
  stage.spillLbl.setAttribute("opacity", 0);
  stage.indexBadge.setAttribute("opacity", 0);
  stage.indexLbl.setAttribute("opacity", 0);
  stage.probeArrow.setAttribute("opacity", 0);
  stage.resultArrow.setAttribute("opacity", 0);
  stage.resultLbl.textContent = "Result rows";
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function spawnTuple(text, color, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var tw = Math.max(70, text.length * 5.5 + 14);
  var bg = anim.svgEl("rect", {
    x: -tw / 2, y: -9, width: tw, height: 18, rx: 9, ry: 9,
    fill: color, stroke: "#1f2937", "stroke-width": 1
  });
  g.appendChild(bg);
  var lbl = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle",
    "font-size": 8, "font-weight": 700, fill: "#ffffff"
  });
  lbl.textContent = text;
  g.appendChild(lbl);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

var SUBQUERY_ROWS = [
  {dept: 1, avg: 62000},
  {dept: 2, avg: 71000},
  {dept: 3, avg: 58000},
  {dept: 4, avg: 85000},
  {dept: 5, avg: 69000},
  {dept: 6, avg: 77000},
  {dept: 7, avg: 54000},
  {dept: 8, avg: 93000}
];

var OUTER_ROWS = [
  {id: 1, name: "Sales"},
  {id: 3, name: "Eng"},
  {id: 5, name: "Mktg"},
  {id: 7, name: "Ops"},
  {id: 2, name: "HR"},
  {id: 4, name: "Finance"}
];

function buildTimeline(spillsToDisk, hasIndex) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var total = SUBQUERY_ROWS.length;

  // Phase 1: Materialize subquery
  tl.mark("Materialize subquery");
  tl.call(function() {
    phase.textContent = "Phase 1 \u2014 executing subquery, materializing into temp table";
  });

  for (var i = 0; i < total; i++) {
    (function(idx) {
      var row = SUBQUERY_ROWS[idx];
      var label = "dept=" + row.dept + " avg=$" + (row.avg / 1000).toFixed(0) + "K";

      tl.call(function() {
        var pct = Math.round(((idx + 1) / total) * 100);
        var fillW = stage.barW * (idx + 1) / total;
        stage.barFill.setAttribute("width", fillW);
        stage.barPct.textContent = pct + "%";
        stage.tmpContent.textContent = (idx + 1) + " / " + total + " rows materialized";

        var cx = 60 + (idx % 4) * 190;
        var cy = 175;
        var tuple = spawnTuple(label, "#d97706", cx, cy);
        anim.tween({from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
          onUpdate: function(v) { tuple.setAttribute("opacity", v); }});
      });

      tl.delay(250);
    })(i);
  }

  // Show spill indicator if applicable
  if (spillsToDisk) {
    tl.delay(200);
    tl.call(function() {
      stage.spillLbl.setAttribute("opacity", 1);
      stage.tmpRect.setAttribute("stroke", "#dc2626");
      stage.barFill.setAttribute("fill", "#f87171");
      phase.textContent = "Phase 1 \u2014 temp table exceeds 16 MiB, spills to disk!";
    });
    tl.delay(400);
  }

  // Fade out materialized tuples
  tl.call(function() {
    stage.tuples.forEach(function(t) {
      anim.tween({from: 1, to: 0.15, duration: 200, ease: anim.easeInCubic,
        onUpdate: function(v) { t.setAttribute("opacity", v); }});
    });
  });
  tl.delay(300);

  // Phase 2: Auto-index creation
  if (hasIndex) {
    tl.mark("Auto-index creation");
    tl.call(function() {
      phase.textContent = "Phase 2 \u2014 creating auto-index on temp table (dept_id)";
      // Blue flash on temp table
      stage.tmpRect.setAttribute("fill", "#dbeafe");
      stage.tmpRect.setAttribute("stroke", "#3b82f6");
    });
    tl.delay(200);
    tl.call(function() {
      stage.indexBadge.setAttribute("opacity", 1);
      stage.indexLbl.setAttribute("opacity", 1);
    });
    tl.delay(150);
    // Flash effect
    tl.call(function() {
      anim.tween({from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
        onUpdate: function(v) {
          var c = anim.lerpColor("#93c5fd", "#dbeafe", v);
          stage.tmpRect.setAttribute("fill", c);
        }});
    });
    tl.delay(400);
    tl.call(function() {
      stage.tmpRect.setAttribute("fill", "#fffbeb");
      stage.tmpRect.setAttribute("stroke", "#d97706");
    });
    tl.delay(200);
  }

  // Phase 3: Outer query probing
  tl.mark("Outer query probing");
  tl.call(function() {
    phase.textContent = "Phase 3 \u2014 outer query probes temp table" + (hasIndex ? " via auto-index" : " (full scan)");
    stage.probeArrow.setAttribute("opacity", 0.6);
    stage.resultArrow.setAttribute("opacity", 0.6);
  });
  tl.delay(200);

  for (var j = 0; j < OUTER_ROWS.length; j++) {
    (function(idx) {
      var outer = OUTER_ROWS[idx];
      var probeLabel = outer.name + " (id=" + outer.id + ")";

      tl.call(function() {
        // Probe tuple from outer
        var probeTuple = spawnTuple(probeLabel, "#6366f1", 190, 278);
        anim.tween({from: 0, to: 1, duration: 150, ease: anim.easeOutCubic,
          onUpdate: function(v) { probeTuple.setAttribute("opacity", v); }});
      });
      tl.delay(180);

      tl.call(function() {
        // Lookup flash on temp table
        anim.tween({from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
          onUpdate: function(v) {
            var c = anim.lerpColor("#fffbeb", hasIndex ? "#dbeafe" : "#fef3c7", v);
            stage.tmpRect.setAttribute("fill", c);
          }});
      });
      tl.delay(200);

      // Check if match exists
      var matched = false;
      for (var m = 0; m < SUBQUERY_ROWS.length; m++) {
        if (SUBQUERY_ROWS[m].dept === outer.id) { matched = true; break; }
      }

      if (matched) {
        tl.call(function() {
          var resultTuple = spawnTuple(probeLabel + " \u2713", "#059669", 610, 278);
          anim.tween({from: 0, to: 1, duration: 150, ease: anim.easeOutCubic,
            onUpdate: function(v) { resultTuple.setAttribute("opacity", v); }});
        });
      }
      tl.delay(180);

      // Reset temp table color
      tl.call(function() {
        stage.tmpRect.setAttribute("fill", "#fffbeb");
        // Fade probe tuples
        stage.tuples.forEach(function(t) {
          if (parseFloat(t.getAttribute("opacity") || 1) > 0.3) {
            anim.tween({from: 1, to: 0, duration: 120, ease: anim.easeInCubic,
              onUpdate: function(v) { t.setAttribute("opacity", v); }});
          }
        });
      });
      tl.delay(150);
    })(j);
  }

  tl.delay(300);
  tl.mark("Done");
  tl.call(function() {
    var msg = hasIndex
      ? "\u2713 Done \u2014 auto-index made each probe O(log n) via B+tree lookup"
      : "\u2713 Done \u2014 no auto-index, temp table was fully scanned";
    phase.textContent = msg;
    stage.statusLbl.textContent = hasIndex
      ? "Auto-index on the materialized temp table keeps probe cost logarithmic."
      : "Without an auto-index, the optimizer scans the entire temp table.";
  });

  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var cost = derivedCost(c.subquery_rows, c.row_size, c.outer_rows, c.has_index);
  return buildTimeline(cost.spillsToDisk, cost.hasIndex);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function renderChart(currentSubqueryRows, hasIndex) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e6,
    xLabel: "Subquery rows", yLabel: "Total I/O operations",
    curves: [
      {label: "Materialized + auto-index", color: "#2563eb", fn: function(n) {
        var h = Math.max(1, Math.ceil(Math.log(Math.max(2, n)) / Math.log(800)));
        return n + 1000 * h;
      }},
      {label: "Materialized + full scan", color: "#dc2626", fn: function(n) {
        return n + n;
      }},
      {label: "Merged (no temp table)", color: "#059669", fn: function(n) {
        return 1000;  // just outer_rows, no materialization overhead
      }}
    ],
    current: { x: currentSubqueryRows }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = derivedCost(c.subquery_rows, c.row_size, c.outer_rows, c.has_index);

  document.getElementById("out-subquery-rows").textContent = teachRuntime.formatInt(cost.materializeWrites);
  document.getElementById("out-mat-writes").textContent = teachRuntime.formatInt(cost.materializeWrites);
  document.getElementById("out-tmp-size").textContent = teachRuntime.formatBytes(cost.tmpSize);
  document.getElementById("out-spills").textContent = cost.spillsToDisk ? "Yes \u2014 exceeds 16 MiB" : "No \u2014 fits in memory";
  document.getElementById("out-spills").className = "value " + (cost.spillsToDisk ? "hot" : "ok");
  document.getElementById("out-auto-index").textContent = cost.hasIndex ? "Yes \u2014 B+tree on join key" : "No \u2014 full scan";
  document.getElementById("out-auto-index").className = "value " + (cost.hasIndex ? "ok" : "hot");
  document.getElementById("out-probe-reads").textContent = teachRuntime.formatInt(cost.totalReads);
  document.getElementById("out-total-io").textContent = teachRuntime.formatInt(cost.totalIO);

  var explanation;
  if (cost.hasIndex) {
    explanation = "Subquery materializes " + teachRuntime.formatInt(cost.materializeWrites) +
      " rows (" + teachRuntime.formatBytes(cost.tmpSize) + ") into temp table." +
      " Auto-index (B+tree height " + cost.height + ") gives O(log n) probe." +
      " Each of " + teachRuntime.formatInt(c.outer_rows) + " outer rows needs " +
      cost.readsPerProbe + " page reads = " + teachRuntime.formatInt(cost.totalReads) + " total probe reads.";
  } else {
    explanation = "Subquery materializes " + teachRuntime.formatInt(cost.materializeWrites) +
      " rows (" + teachRuntime.formatBytes(cost.tmpSize) + ") into temp table." +
      " No auto-index: full scan of " + teachRuntime.formatInt(cost.totalReads) +
      " rows to answer outer query.";
  }
  if (cost.spillsToDisk) {
    explanation += " Temp table exceeds 16 MiB \u2014 spills to disk, adding I/O latency.";
  }
  document.getElementById("out-explanation").textContent = explanation;

  renderChart(c.subquery_rows, c.has_index);
  buildStage();
  resetStage();
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


def render():
    # type: () -> str
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (derived table materialization)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="subquery_rows">Subquery rows: <span class="value-pill" data-pill-for="subquery_rows">10000</span></label>
      <input type="range" id="subquery_rows" name="subquery_rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Rows produced by the FROM-clause subquery.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Average size of each materialized row.</div>
    </div>

    <div class="control">
      <label for="outer_rows">Outer query rows: <span class="value-pill" data-pill-for="outer_rows">1000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="500000" step="100" value="1000">
      <div class="hint">Rows in the outer query that probe the temp table.</div>
    </div>

    <div class="control">
      <label for="has_index">Auto-index on temp table:</label>
      <input type="checkbox" id="has_index" name="has_index" checked>
      <div class="hint">MySQL can auto-generate a B+tree index on the join key of the temp table.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Derived table: subquery materialized into tmp\n"
            "SELECT d.dept_name, stats.avg_salary\n"
            "FROM   departments d\n"
            "JOIN   (SELECT dept_id, AVG(salary) AS avg_salary\n"
            "        FROM employees\n"
            "        GROUP BY dept_id) AS stats\n"
            "  ON   d.id = stats.dept_id;"
        ),
        note=(
            "MySQL materializes the grouped subquery into a temp table, "
            "optionally adds an auto-index, then probes it from the outer query."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The subquery executes fully, writing rows into a temporary table (yellow).",
            "If the temp table exceeds 16 MiB (tmp_table_size), it spills to disk.",
            "MySQL may auto-generate a B+tree index on the join column for fast lookups.",
            "The outer query probes the temp table for each of its rows.",
            "Matched results flow out as green result tuples.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  " + query_card_html + "\n"
        "  " + explainer_html + "\n"
        "  " + _html.stage_toolbar("Ready \u2014 press Play") + "\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="derived-svg" viewBox="0 0 800 440"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "    " + _html.phase_nav() + "\n"
        "  </div>\n"
        "</section>"
    )

    ht = _html.help_tip
    readout_html = (
        '<section class="readout">\n'
        "  <h2>Cost readout (derived table materialization)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Subquery rows '
        + ht("Total rows produced by the FROM-clause subquery.") +
        '</p><p class="value" id="out-subquery-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Materialization writes '
        + ht("Rows written into the temp table during materialization.") +
        '</p><p class="value" id="out-mat-writes">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Temp table size '
        + ht("Estimated size of the materialized temp table (rows \u00d7 row_size).") +
        '</p><p class="value" id="out-tmp-size">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Spills to disk? '
        + ht("If the temp table exceeds 16 MiB, it spills to an on-disk temp table.") +
        '</p><p class="value ok" id="out-spills">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Auto-index? '
        + ht("MySQL can auto-generate a B+tree index on the join key for faster lookups.") +
        '</p><p class="value ok" id="out-auto-index">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Probe reads '
        + ht("Read operations when the outer query probes the temp table.") +
        '</p><p class="value" id="out-probe-reads">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Total I/O '
        + ht("Materialization writes + probe reads.") +
        '</p><p class="value" id="out-total-io">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Materialized (indexed vs scan) vs merged (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more &mdash; derived table materialization vs merging</summary>
  <div class="body">
    <p>The <code>derived_merge</code> optimizer switch (ON by default since MySQL 5.7)
    lets the optimizer merge a derived table into the outer query, eliminating the
    temp table entirely. When merge is possible, there is <strong>zero materialization
    overhead</strong> &mdash; the subquery's tables are accessed directly.</p>
    <p><strong>When can't MySQL merge?</strong> Materialization is the fallback when
    the subquery contains: <code>GROUP BY</code>, <code>DISTINCT</code>,
    <code>LIMIT</code>, <code>UNION</code>, aggregate functions, or
    user-defined variables. In these cases, the result must be fully computed
    before the outer query can use it.</p>
    <p><strong>Auto-key generation</strong> (since MySQL 5.7, refined in 8.0):
    when the outer query has an equi-join condition on the derived table,
    MySQL automatically creates a hash or B+tree index on the temp table's
    join column. This turns an O(n) full-scan probe into an O(log n) indexed
    lookup per outer row.</p>
    <p><strong>Temp table sizing:</strong> The materialized temp table starts in
    memory. If it exceeds <code>tmp_table_size</code> (default 16 MiB), it spills
    to disk. Large derived tables with many rows or wide rows are more likely
    to spill, adding disk I/O overhead.</p>
    <p><strong>Optimization tips:</strong></p>
    <ul>
      <li>Check <code>EXPLAIN</code> for <code>&lt;derived2&gt;</code> or
      <code>MATERIALIZED</code> to confirm materialization.</li>
      <li>If <code>derived_merge=on</code> and it still materializes, the subquery
      likely has GROUP BY/DISTINCT/LIMIT preventing merge.</li>
      <li>Consider rewriting the query to avoid the derived table, or ensure
      the join column has an index hint for the auto-key.</li>
    </ul>
    <p>Sources: MySQL 8.4 reference manual &sect;10.2.2.4 &ldquo;Optimizing
    Derived Tables&rdquo;; MySQL 8.4 reference manual &sect;7.1.8 &ldquo;Server
    System Variables&rdquo;.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="derived_table",
        title="Derived Table Materialization",
        subtitle=(
            "Watch a FROM-clause subquery materialize into a temp table, "
            "get an auto-index, and then get probed by the outer query."
        ),
        version_chip="MySQL 8.4 \u2022 MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS_TEMPLATE,
    )
