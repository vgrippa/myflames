"""Lesson: Skip Scan (range access without the leading index column).

Given a composite index (A, B), a query with ``WHERE B > 100`` (no equality
on A) normally cannot use the index. Skip Scan scans for each distinct value
of A, then does a range scan on B within each A-group — converting one full
table scan into N range scans (N = NDV of the leading column).

Real query: ``SELECT * FROM employees WHERE age BETWEEN 25 AND 30`` with a
composite index on ``(gender, age)`` and no equality predicate on ``gender``.
"""
from . import _html


_LESSON_JS_TEMPLATE = r"""
/* ---- cost model ---- */
function skipScanCost(tableRows, ndvLeading, selectivity) {
  var rowsPerGroup = tableRows / ndvLeading;
  var matchingPerGroup = Math.max(1, Math.floor(rowsPerGroup * selectivity / 100));
  var totalMatching = matchingPerGroup * ndvLeading;
  // B+tree height approximation (log base ~500 of tableRows)
  var height = Math.max(2, Math.ceil(Math.log(tableRows) / Math.log(500)));
  // Skip scan: for each distinct A, one seek + range read
  var skipScanReads = ndvLeading * (height + Math.max(1, Math.floor(rowsPerGroup * selectivity / 100)));
  // Full scan: read every row
  var fullScanReads = tableRows;
  // Index scan: read full index (same for covering)
  var indexScanReads = tableRows;
  return {
    rowsPerGroup: Math.floor(rowsPerGroup),
    matchingPerGroup: matchingPerGroup,
    totalMatching: totalMatching,
    skipScanReads: skipScanReads,
    fullScanReads: fullScanReads,
    indexScanReads: indexScanReads,
    savings: fullScanReads - skipScanReads
  };
}

/* ---- stage globals ---- */
var W = 800, H = 400;
var stage = null;

/* Distinct leading-column values displayed in the animation. */
var GROUPS = [
  {label: "gender = M", color: "#3b82f6", light: "#dbeafe"},
  {label: "gender = F", color: "#ec4899", light: "#fce7f3"}
];

/* We show a few sample B-values inside each group. */
var SAMPLE_ROWS = [
  {age: 18, match: false},
  {age: 22, match: false},
  {age: 25, match: true},
  {age: 27, match: true},
  {age: 30, match: true},
  {age: 35, match: false},
  {age: 42, match: false},
  {age: 55, match: false}
];

function buildStage() {
  var svg = document.getElementById("skip-scan-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var groupCount = GROUPS.length;
  var rowCount = SAMPLE_ROWS.length;
  var groupW = (W - 40) / groupCount;
  var cellW = Math.min(60, (groupW - 30) / rowCount);
  var cellH = 28;
  var groupY = 90;
  var groupH = cellH * rowCount + 40;

  /* Title */
  var title = anim.svgEl("text", {
    x: W / 2, y: 24, "text-anchor": "middle",
    "font-size": 14, "font-weight": 700, fill: "#1f2937"
  });
  title.textContent = "Composite Index (gender, age)";
  svg.appendChild(title);

  var subtitle = anim.svgEl("text", {
    x: W / 2, y: 44, "text-anchor": "middle",
    "font-size": 11, fill: "#6b7280"
  });
  subtitle.textContent = "Skip Scan hops between distinct gender values, range-scanning age within each";
  svg.appendChild(subtitle);

  var groups = [];

  for (var g = 0; g < groupCount; g++) {
    var gx = 20 + g * groupW;
    var grp = GROUPS[g];

    /* Group box */
    var box = anim.svgEl("rect", {
      x: gx, y: groupY, width: groupW - 10, height: groupH,
      rx: 8, ry: 8, fill: grp.light, stroke: grp.color,
      "stroke-width": 2, opacity: 0.9
    });
    svg.appendChild(box);

    /* Group label */
    var lbl = anim.svgEl("text", {
      x: gx + (groupW - 10) / 2, y: groupY + 20, "text-anchor": "middle",
      "font-size": 12, "font-weight": 700, fill: grp.color
    });
    lbl.textContent = grp.label;
    svg.appendChild(lbl);

    /* Row cells */
    var cells = [];
    var cellStartY = groupY + 30;
    for (var r = 0; r < rowCount; r++) {
      var cx = gx + 10;
      var cy = cellStartY + r * cellH;

      var cellBg = anim.svgEl("rect", {
        x: cx, y: cy, width: groupW - 30, height: cellH - 4,
        rx: 4, ry: 4, fill: "#ffffff", stroke: "#e5e7eb", "stroke-width": 1
      });
      svg.appendChild(cellBg);

      var cellText = anim.svgEl("text", {
        x: cx + 8, y: cy + 17, "font-size": 11, fill: "#374151"
      });
      cellText.textContent = "age = " + SAMPLE_ROWS[r].age;
      svg.appendChild(cellText);

      var cellIcon = anim.svgEl("text", {
        x: cx + groupW - 42, y: cy + 17, "text-anchor": "end",
        "font-size": 10, "font-weight": 700, fill: "#9ca3af"
      });
      cellIcon.textContent = "";
      svg.appendChild(cellIcon);

      cells.push({bg: cellBg, icon: cellIcon, match: SAMPLE_ROWS[r].match, age: SAMPLE_ROWS[r].age});
    }

    groups.push({box: box, cells: cells, label: grp.label, color: grp.color});
  }

  /* Cursor pill that hops between groups */
  var cursor = anim.svgEl("rect", {
    x: 0, y: 0, width: 10, height: cellH - 4,
    rx: 3, ry: 3, fill: "#f59e0b", opacity: 0
  });
  svg.appendChild(cursor);

  /* Summary area at bottom */
  var summaryY = groupY + groupH + 16;
  var summaryBox = anim.svgEl("rect", {
    x: 20, y: summaryY, width: W - 40, height: 40, rx: 6, ry: 6,
    fill: "#f0fdf4", stroke: "#86efac", "stroke-width": 1
  });
  svg.appendChild(summaryBox);
  var summaryText = anim.svgEl("text", {
    x: W / 2, y: summaryY + 25, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#059669"
  });
  summaryText.textContent = "";
  svg.appendChild(summaryText);

  stage = {
    svg: svg, groups: groups, cursor: cursor,
    summaryText: summaryText, groupW: groupW, cellH: cellH,
    groupY: groupY, cellStartY: groupY + 30
  };
}

function resetStage() {
  if (!stage) return;
  for (var g = 0; g < stage.groups.length; g++) {
    var cells = stage.groups[g].cells;
    for (var r = 0; r < cells.length; r++) {
      cells[r].bg.setAttribute("fill", "#ffffff");
      cells[r].bg.setAttribute("stroke", "#e5e7eb");
      cells[r].icon.textContent = "";
    }
  }
  stage.cursor.setAttribute("opacity", "0");
  stage.summaryText.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var totalMatch = 0;
  var totalSkip = 0;

  tl.mark("Start: Skip Scan");
  tl.call(function() {
    phase.textContent = "Skip Scan begins \u2014 iterating over distinct values of gender";
    stage.cursor.setAttribute("opacity", "1");
  });
  tl.delay(600);

  for (var g = 0; g < stage.groups.length; g++) {
    (function(gi) {
      var grp = stage.groups[gi];
      var gx = 20 + gi * stage.groupW;
      var cells = grp.cells;

      tl.mark("Group: " + grp.label);

      /* Jump cursor to this group */
      tl.call(function() {
        phase.textContent = "Jump to group: " + grp.label;
      });
      tl.tween(stage.cursor, {
        x: gx + 2, y: stage.cellStartY,
        opacity: 1
      }, 300, anim.easeOutCubic);
      tl.delay(300);

      /* Scan within group */
      for (var r = 0; r < cells.length; r++) {
        (function(ri) {
          var cell = cells[ri];
          var cy = stage.cellStartY + ri * stage.cellH;

          tl.call(function() {
            /* Move cursor alongside */
            stage.cursor.setAttribute("y", String(cy));
            /* Highlight current cell being examined */
            cell.bg.setAttribute("fill", "#fffbeb");
            cell.bg.setAttribute("stroke", "#f59e0b");
            phase.textContent = grp.label + " \u2014 checking age = " + cell.age + " (BETWEEN 25 AND 30?)";
          });
          tl.delay(250);

          tl.call(function() {
            if (cell.match) {
              cell.bg.setAttribute("fill", "#dcfce7");
              cell.bg.setAttribute("stroke", "#22c55e");
              cell.icon.textContent = "\u2714 match";
              cell.icon.setAttribute("fill", "#16a34a");
              totalMatch++;
            } else {
              cell.bg.setAttribute("fill", "#f3f4f6");
              cell.bg.setAttribute("stroke", "#d1d5db");
              cell.icon.textContent = "skip";
              cell.icon.setAttribute("fill", "#9ca3af");
              totalSkip++;
            }
          });
          tl.delay(200);
        })(r);
      }

      tl.delay(200);
    })(g);
  }

  tl.delay(400);
  tl.mark("Summary");
  tl.call(function() {
    stage.cursor.setAttribute("opacity", "0");
    var totalRows = stage.groups.length * SAMPLE_ROWS.length;
    phase.textContent = "\u2713 Done \u2014 matched " + totalMatch + " rows from " + stage.groups.length + " groups, skipped " + totalSkip;
    stage.summaryText.textContent = "Skip Scan: " + totalMatch + " matching rows found across " + stage.groups.length + " groups (" + totalSkip + " skipped) \u2014 avoided full table scan of " + totalRows + " rows";
  });

  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}

function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function renderChart(tableRows) {
  var c = teachRuntime.readControls();
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 1000, xMax: 5e6,
    xLabel: "Table rows", yLabel: "Reads",
    curves: [
      { label: "Full table scan", color: "#dc2626",
        fn: function(n) { return n; } },
      { label: "Full index scan", color: "#f59e0b",
        fn: function(n) { return n; } },
      { label: "Skip Scan (NDV=" + c.ndv_leading + ", sel=" + c.selectivity + "%)", color: "#059669",
        fn: function(n) {
          var height = Math.max(2, Math.ceil(Math.log(n) / Math.log(500)));
          var rpg = n / c.ndv_leading;
          return c.ndv_leading * (height + Math.max(1, Math.floor(rpg * c.selectivity / 100)));
        }
      }
    ],
    current: { x: tableRows },
    xSlider: "table_rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(1000, Math.min(5000000, xVal))); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = skipScanCost(c.table_rows, c.ndv_leading, c.selectivity);

  document.getElementById("out-ndv").textContent = teachRuntime.formatInt(c.ndv_leading);
  document.getElementById("out-rows-per-group").textContent = teachRuntime.formatInt(cost.rowsPerGroup);
  document.getElementById("out-matching-per-group").textContent = teachRuntime.formatInt(cost.matchingPerGroup);
  document.getElementById("out-total-matching").textContent = teachRuntime.formatInt(cost.totalMatching);
  document.getElementById("out-skip-scan-reads").textContent = teachRuntime.formatInt(cost.skipScanReads);
  document.getElementById("out-full-scan-reads").textContent = teachRuntime.formatInt(cost.fullScanReads);
  document.getElementById("out-savings").textContent = teachRuntime.formatInt(cost.savings);
  document.getElementById("out-savings").className = "value " + (cost.savings > 0 ? "ok" : "");

  document.getElementById("out-explanation").textContent =
    "Skip Scan does " + teachRuntime.formatInt(cost.skipScanReads) + " reads (" +
    c.ndv_leading + " groups \u00d7 seek+range) vs " +
    teachRuntime.formatInt(cost.fullScanReads) + " for a full table scan \u2014 " +
    (cost.savings > 0 ? "saving " + teachRuntime.formatInt(cost.savings) + " reads." :
     "no savings (NDV too high or selectivity too low).");

  renderChart(c.table_rows);
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
buildStage();
"""


def render():
    # type: () -> str
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Skip Scan)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="table_rows">Table rows: <span class="value-pill" data-pill-for="table_rows">100000</span></label>
      <input type="range" id="table_rows" name="table_rows" min="1000" max="5000000" step="1000" value="100000">
      <div class="hint">Total number of rows in the table.</div>
    </div>

    <div class="control">
      <label for="ndv_leading">Distinct values (leading col): <span class="value-pill" data-pill-for="ndv_leading">5</span></label>
      <input type="range" id="ndv_leading" name="ndv_leading" min="2" max="1000" step="1" value="5">
      <div class="hint">Number of distinct values in column A (e.g. gender). Lower = better for Skip Scan.</div>
    </div>

    <div class="control">
      <label for="selectivity">Selectivity on B (%): <span class="value-pill" data-pill-for="selectivity">10</span></label>
      <input type="range" id="selectivity" name="selectivity" min="1" max="100" step="1" value="10">
      <div class="hint">Percentage of rows matching WHERE condition on trailing column B.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Index on (gender, age) but no equality on gender\n"
            "SELECT * FROM employees\n"
            "WHERE  age BETWEEN 25 AND 30;"
        ),
        note=(
            "Skip Scan iterates over each distinct gender value, "
            "doing a range scan on age within each group \u2014 "
            "avoids reading the whole table."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "A composite index on (gender, age) is shown as colored groups.",
            "The cursor pill jumps to the first distinct gender value.",
            "Within each group, a range scan checks age BETWEEN 25 AND 30.",
            "Matching rows are highlighted green; non-matching rows are greyed out.",
            "The cursor hops to the next group and repeats until all groups are scanned.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %(query_card)s\n"
        "  %(explainer)s\n"
        "  %(toolbar)s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="skip-scan-svg" viewBox="0 0 800 400"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "    %(phase_nav)s\n"
        "  </div>\n"
        "</section>"
    ) % {
        "query_card": query_card_html,
        "explainer": explainer_html,
        "toolbar": _html.stage_toolbar("Ready \u2014 press Play"),
        "phase_nav": _html.phase_nav(),
    }

    ht = _html.help_tip
    readout_html = (
        '<section class="readout">\n'
        "  <h2>Cost readout (Skip Scan)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Distinct values (leading col) '
        + ht("Number of distinct values in the leading index column. Skip Scan does one sub-range-scan per distinct value.")
        + '</p><p class="value" id="out-ndv">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows per group '
        + ht("table_rows / NDV of leading column. Each group is scanned separately.")
        + '</p><p class="value" id="out-rows-per-group">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Matching rows per group '
        + ht("Rows per group that satisfy the WHERE condition on the trailing column.")
        + '</p><p class="value" id="out-matching-per-group">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Total matching rows '
        + ht("Sum of matching rows across all groups.")
        + '</p><p class="value" id="out-total-matching">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Skip scan reads '
        + ht("Total reads: for each distinct value, one B+tree seek plus a range read of matching rows.")
        + '</p><p class="value" id="out-skip-scan-reads">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Full scan reads '
        + ht("A full table scan reads every row \u2014 the baseline without Skip Scan.")
        + '</p><p class="value" id="out-full-scan-reads">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Savings '
        + ht("Reads saved compared to a full table scan: full_scan_reads minus skip_scan_reads.")
        + '</p><p class="value ok" id="out-savings">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Reads vs table size (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more &mdash; when does the optimizer choose Skip Scan?</summary>
  <div class="body">
    <p>Skip Scan was introduced in <strong>MySQL 8.0.13</strong> and is
    controlled by <code>optimizer_switch='skip_scan=on'</code> (on by default).</p>
    <p><strong>When it helps:</strong></p>
    <p>1. The leading column of a composite index has <strong>low NDV</strong>
    (number of distinct values) &mdash; e.g. gender, status, boolean flags.</p>
    <p>2. The trailing column has a <strong>selective range condition</strong>
    (e.g. <code>age BETWEEN 25 AND 30</code>).</p>
    <p>3. The optimizer estimates that doing N sub-range-scans (one per distinct
    leading value) is cheaper than a full table scan or a full index scan.</p>
    <p><strong>When it does NOT help:</strong></p>
    <p>1. The leading column has <strong>high NDV</strong> (thousands of distinct
    values) &mdash; too many sub-scans make it slower than a full scan.</p>
    <p>2. The range on the trailing column is <strong>not selective</strong>
    (most rows match) &mdash; you end up reading almost everything anyway.</p>
    <p>3. A better single-column index on the trailing column exists.</p>
    <p><strong>Note:</strong> Skip Scan is a <strong>MySQL-only</strong>
    optimization. MariaDB does not implement it as of 11.x. In EXPLAIN output
    you will see <code>Using index for skip scan</code> in the Extra column.</p>
    <p>Source: MySQL 8.4 Reference Manual &sect;10.2.1.2
    &ldquo;Range Optimization &mdash; Skip Scan Range Access Method&rdquo;.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="skip_scan",
        title="Skip Scan \u2014 range access without the leading index column",
        subtitle=(
            "How MySQL turns a full table scan into N small range scans "
            "by iterating over distinct values of the leading index column."
        ),
        version_chip="MySQL 8.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
