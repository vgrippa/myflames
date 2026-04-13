"""Lesson: Rowid Filter (MariaDB).

Animates MariaDB's rowid pre-filter optimisation: scan a filtering index
to build a bitmap of qualifying rowids, then scan the main index and
skip table-row fetches for rowids NOT in the bitmap.

Real query: ``SELECT * FROM orders WHERE created_date > '2024-01-01'
AND status = 'shipped'`` with indexes on ``(status)`` and
``(created_date)``.
"""
from . import _html


_LESSON_JS_TEMPLATE = r"""
function rowidFilterCost(mainRows, filterSelectivity, rowSize) {
  var bitmapRows = mainRows;
  var rowsAfterFilter = Math.max(1, Math.ceil(mainRows * filterSelectivity / 100));
  var rowsSkipped = mainRows - rowsAfterFilter;
  var withoutFilterFetches = mainRows;
  var withFilterFetches = rowsAfterFilter;
  var savings = rowsSkipped;
  return {
    mainRows: mainRows,
    filterSelectivity: filterSelectivity,
    rowsAfterFilter: rowsAfterFilter,
    rowsSkipped: rowsSkipped,
    withoutFilterFetches: withoutFilterFetches,
    withFilterFetches: withFilterFetches,
    savings: savings,
    rowSize: rowSize
  };
}

var W = 800, H = 400;
var stage = null;

// Sample rows for animation.  "pass" = rowid IS in the bitmap (status='shipped').
var SAMPLE_ROWS = [
  {id: 1001, date: "2024-02-14", status: "shipped",   pass: true},
  {id: 1002, date: "2024-03-01", status: "pending",   pass: false},
  {id: 1003, date: "2024-03-18", status: "shipped",   pass: true},
  {id: 1004, date: "2024-04-02", status: "cancelled", pass: false},
  {id: 1005, date: "2024-04-25", status: "shipped",   pass: true},
  {id: 1006, date: "2024-05-10", status: "pending",   pass: false},
  {id: 1007, date: "2024-06-03", status: "shipped",   pass: true},
  {id: 1008, date: "2024-06-20", status: "pending",   pass: false},
  {id: 1009, date: "2024-07-11", status: "shipped",   pass: true},
  {id: 1010, date: "2024-07-30", status: "cancelled", pass: false}
];

function buildStage() {
  var svg = document.getElementById("rowid-filter-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var zoneH = 100;
  // --- Zone 1: Filtering index scan -> bitmap (top, blue) ---
  var z1y = 10;
  var z1Title = anim.svgEl("text", {
    x: W / 2, y: z1y + 14, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#1d4ed8"
  });
  z1Title.textContent = "Phase 1: Scan idx_status \u2192 build rowid bitmap";
  svg.appendChild(z1Title);

  var bitmapBg = anim.svgEl("rect", {
    x: 40, y: z1y + 24, width: W - 80, height: 36, rx: 6, ry: 6,
    fill: "#eff6ff", stroke: "#3b82f6", "stroke-width": 1.5
  });
  svg.appendChild(bitmapBg);

  // Bitmap cells — one per sample row
  var cellW = (W - 100) / SAMPLE_ROWS.length;
  var bitmapCells = [];
  for (var i = 0; i < SAMPLE_ROWS.length; i++) {
    var cx = 50 + i * cellW;
    var cell = anim.svgEl("rect", {
      x: cx, y: z1y + 30, width: cellW - 4, height: 24, rx: 3, ry: 3,
      fill: "#e5e7eb", stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(cell);
    var bitLbl = anim.svgEl("text", {
      x: cx + (cellW - 4) / 2, y: z1y + 46,
      "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "#9ca3af"
    });
    bitLbl.textContent = "?";
    svg.appendChild(bitLbl);
    bitmapCells.push({cell: cell, lbl: bitLbl, pass: SAMPLE_ROWS[i].pass});
  }

  // --- Zone 2: Main index scan + bitmap check (middle, purple) ---
  var z2y = z1y + 80;
  var z2Title = anim.svgEl("text", {
    x: W / 2, y: z2y + 14, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#7c3aed"
  });
  z2Title.textContent = "Phase 2: Scan idx_date \u2192 check bitmap per row";
  svg.appendChild(z2Title);

  var rowEntries = [];
  var rowH = 22;
  var rowStartY = z2y + 24;
  for (var j = 0; j < SAMPLE_ROWS.length; j++) {
    var r = SAMPLE_ROWS[j];
    var ry2 = rowStartY + j * rowH;
    var bg = anim.svgEl("rect", {
      x: 40, y: ry2, width: W - 80, height: rowH - 3, rx: 4, ry: 4,
      fill: "#fafafa", stroke: "#e5e7eb", "stroke-width": 1
    });
    svg.appendChild(bg);
    var idLbl = anim.svgEl("text", {
      x: 52, y: ry2 + 14, "font-size": 10, "font-weight": 600, fill: "#374151"
    });
    idLbl.textContent = "id=" + r.id + "  date=" + r.date;
    svg.appendChild(idLbl);
    var statusLbl = anim.svgEl("text", {
      x: 380, y: ry2 + 14, "font-size": 10, "font-weight": 600, fill: "#6b7280"
    });
    statusLbl.textContent = "status=" + r.status;
    svg.appendChild(statusLbl);
    var icon = anim.svgEl("text", {
      x: W - 60, y: ry2 + 14, "text-anchor": "end",
      "font-size": 10, "font-weight": 700, fill: "#9ca3af"
    });
    icon.textContent = "";
    svg.appendChild(icon);
    rowEntries.push({bg: bg, icon: icon, pass: r.pass, id: r.id});
  }

  // --- Zone 3: Result counter (bottom, green) ---
  var z3y = rowStartY + SAMPLE_ROWS.length * rowH + 12;
  var resultBox = anim.svgEl("rect", {
    x: 40, y: z3y, width: W - 80, height: 48, rx: 6, ry: 6,
    fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.5
  });
  svg.appendChild(resultBox);
  var resultLbl = anim.svgEl("text", {
    x: W / 2, y: z3y + 22, "text-anchor": "middle",
    "font-size": 14, "font-weight": 700, fill: "#047857"
  });
  resultLbl.textContent = "Rows fetched: 0 | Skipped random reads: 0";
  svg.appendChild(resultLbl);
  var resultSub = anim.svgEl("text", {
    x: W / 2, y: z3y + 40, "text-anchor": "middle",
    "font-size": 10, fill: "#065f46"
  });
  resultSub.textContent = "Only rows in the bitmap proceed to full table fetch";
  svg.appendChild(resultSub);

  stage = {
    svg: svg, bitmapCells: bitmapCells, rowEntries: rowEntries,
    resultLbl: resultLbl, resultSub: resultSub
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.bitmapCells.length; i++) {
    stage.bitmapCells[i].cell.setAttribute("fill", "#e5e7eb");
    stage.bitmapCells[i].cell.setAttribute("stroke", "#d1d5db");
    stage.bitmapCells[i].lbl.textContent = "?";
    stage.bitmapCells[i].lbl.setAttribute("fill", "#9ca3af");
  }
  for (var j = 0; j < stage.rowEntries.length; j++) {
    stage.rowEntries[j].bg.setAttribute("fill", "#fafafa");
    stage.rowEntries[j].bg.setAttribute("stroke", "#e5e7eb");
    stage.rowEntries[j].icon.textContent = "";
  }
  stage.resultLbl.textContent = "Rows fetched: 0 | Skipped random reads: 0";
  stage.resultSub.textContent = "Only rows in the bitmap proceed to full table fetch";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var fetched = 0;
  var skipped = 0;

  // Phase 1: build bitmap from idx_status
  tl.mark("Build rowid bitmap");
  tl.call(function() {
    phase.textContent = "Phase 1/2 \u2014 scanning idx_status (status='shipped') to build rowid bitmap";
  });
  tl.delay(400);

  for (var i = 0; i < SAMPLE_ROWS.length; i++) {
    (function(idx) {
      var row = SAMPLE_ROWS[idx];
      tl.call(function() {
        var bc = stage.bitmapCells[idx];
        if (row.pass) {
          bc.cell.setAttribute("fill", "#dbeafe");
          bc.cell.setAttribute("stroke", "#3b82f6");
          bc.lbl.textContent = "1";
          bc.lbl.setAttribute("fill", "#1d4ed8");
        } else {
          bc.cell.setAttribute("fill", "#fee2e2");
          bc.cell.setAttribute("stroke", "#f87171");
          bc.lbl.textContent = "0";
          bc.lbl.setAttribute("fill", "#dc2626");
        }
        phase.textContent = "Bitmap: id=" + row.id + " status=" + row.status +
          " \u2192 " + (row.pass ? "1 (in bitmap)" : "0 (not in bitmap)");
      });
      tl.delay(250);
    })(i);
  }

  tl.delay(400);

  // Phase 2: main index scan with bitmap check
  tl.mark("Main scan + bitmap check");
  tl.call(function() {
    phase.textContent = "Phase 2/2 \u2014 scanning idx_date, checking bitmap before each table fetch";
  });
  tl.delay(400);

  for (var j = 0; j < SAMPLE_ROWS.length; j++) {
    (function(idx) {
      var row = SAMPLE_ROWS[idx];

      if (idx %% 3 === 0) {
        tl.mark("Check id=" + row.id);
      }

      // Highlight bitmap cell being checked
      tl.call(function() {
        var bc = stage.bitmapCells[idx];
        bc.cell.setAttribute("stroke-width", "3");
        var re = stage.rowEntries[idx];
        re.bg.setAttribute("fill", "#f5f3ff");
        re.bg.setAttribute("stroke", "#8b5cf6");
        phase.textContent = "id=" + row.id + " \u2192 bitmap check: " +
          (row.pass ? "bit=1 \u2192 FETCH row" : "bit=0 \u2192 SKIP (no random I/O)");
      });
      tl.delay(300);

      tl.call(function() {
        var bc = stage.bitmapCells[idx];
        bc.cell.setAttribute("stroke-width", "1");
        var re = stage.rowEntries[idx];
        if (row.pass) {
          re.bg.setAttribute("fill", "#dcfce7");
          re.bg.setAttribute("stroke", "#22c55e");
          re.icon.textContent = "\u2714 fetched";
          re.icon.setAttribute("fill", "#16a34a");
          fetched++;
        } else {
          re.bg.setAttribute("fill", "#fef2f2");
          re.bg.setAttribute("stroke", "#fca5a5");
          re.icon.textContent = "\u2718 skipped";
          re.icon.setAttribute("fill", "#dc2626");
          skipped++;
        }
        stage.resultLbl.textContent = "Rows fetched: " + fetched +
          " | Skipped random reads: " + skipped;
      });
      tl.delay(250);
    })(j);
  }

  tl.delay(400);
  tl.mark("Summary");
  tl.call(function() {
    phase.textContent = "\u2713 Done \u2014 fetched " + fetched + " rows, skipped " +
      skipped + " random reads thanks to rowid bitmap filter";
    stage.resultSub.textContent = "Without filter: " + SAMPLE_ROWS.length +
      " table fetches. With filter: " + fetched + ". Saved " + skipped + " random I/Os.";
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

function renderChart(mainRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 500000,
    xLabel: "Main index rows scanned", yLabel: "Table-row random I/Os",
    curves: [
      { label: "Without rowid filter (all rows fetched)", color: "#dc2626",
        fn: function(n) { return n; } },
      { label: "With rowid filter (20%% selectivity)", color: "#059669",
        fn: function(n) { return Math.max(1, Math.ceil(n * 0.20)); } },
      { label: "With rowid filter (5%% selectivity)", color: "#0284c7",
        fn: function(n) { return Math.max(1, Math.ceil(n * 0.05)); } }
    ],
    current: { x: mainRows },
    xSlider: "main_rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(100, Math.min(500000, xVal / 100) * 100)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = rowidFilterCost(c.main_rows, c.filter_selectivity, c.row_size);
  document.getElementById("out-main-rows").textContent = teachRuntime.formatInt(cost.mainRows);
  document.getElementById("out-selectivity").textContent = cost.filterSelectivity + "%%";
  document.getElementById("out-after-filter").textContent = teachRuntime.formatInt(cost.rowsAfterFilter);
  document.getElementById("out-skipped").textContent = teachRuntime.formatInt(cost.rowsSkipped);
  document.getElementById("out-skipped").className = "value " + (cost.rowsSkipped > 0 ? "ok" : "");
  document.getElementById("out-without").textContent = teachRuntime.formatInt(cost.withoutFilterFetches);
  document.getElementById("out-with").textContent = teachRuntime.formatInt(cost.withFilterFetches);
  document.getElementById("out-saved").textContent = teachRuntime.formatInt(cost.savings);
  document.getElementById("out-saved").className = "value " + (cost.savings > 0 ? "ok" : "");
  document.getElementById("out-explanation").textContent =
    "Without rowid filter: all " + teachRuntime.formatInt(cost.withoutFilterFetches) +
    " main-index rows trigger a table fetch. With filter: bitmap passes " +
    cost.filterSelectivity + "%% of rows \u2014 only " +
    teachRuntime.formatInt(cost.withFilterFetches) + " fetches, saving " +
    teachRuntime.formatInt(cost.savings) + " random I/Os.";
  renderChart(c.main_rows);
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
  <h2 id="controls-h">Parameters (Rowid Filter)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="main_rows">Main index rows: <span class="value-pill" data-pill-for="main_rows">10000</span></label>
      <input type="range" id="main_rows" name="main_rows" min="100" max="500000" step="100" value="10000">
      <div class="hint">Rows returned by the main index scan (idx_date).</div>
    </div>

    <div class="control">
      <label for="filter_selectivity">Filter selectivity (%%): <span class="value-pill" data-pill-for="filter_selectivity">20</span></label>
      <input type="range" id="filter_selectivity" name="filter_selectivity" min="1" max="100" step="1" value="20">
      <div class="hint">Percentage of rows passing the filtering index (idx_status bitmap).</div>
    </div>

    <div class="control">
      <label for="row_size">Average row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="8" value="200">
      <div class="hint">Larger rows make skipped fetches more valuable (more I/O saved).</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- idx_status on (status), idx_date on (created_date)\n"
            "-- Main access via idx_date, rowid filter from idx_status\n"
            "SELECT * FROM orders\n"
            "WHERE  created_date > '2024-01-01'\n"
            "AND    status = 'shipped';"
        ),
        note=(
            "MariaDB scans idx_status first to build a bitmap, then uses "
            "idx_date for the main scan \u2014 skipping table-row fetches "
            "for rows not in the bitmap."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Phase 1 scans the filtering index (idx_status) and sets a 1/0 bit per rowid.",
            "Phase 2 scans the main index (idx_date) and checks the bitmap for each rowid.",
            "Rows with bit=1 proceed to a full table fetch (green check).",
            "Rows with bit=0 are skipped entirely (red cross) \u2014 no random I/O.",
            "Watch the skipped-reads counter grow: every skip saves one random page read.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %(query_card)s\n"
        "  %(explainer)s\n"
        "  %(toolbar)s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="rowid-filter-svg" viewBox="0 0 800 400"'
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
        "  <h2>Cost readout (Rowid Filter)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Main index rows '
        + ht("Total rows returned by the main index scan (idx_date). Without a rowid filter all would trigger table fetches.")
        + '</p><p class="value" id="out-main-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Filter selectivity '
        + ht("Percentage of rows whose rowid appears in the bitmap (status=shipped). Lower is better for the filter.")
        + '</p><p class="value" id="out-selectivity">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows after filter '
        + ht("Rows passing the bitmap check. Only these trigger a full table-row fetch.")
        + '</p><p class="value" id="out-after-filter">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows skipped '
        + ht("Rows NOT in the bitmap. Each skip avoids one random I/O page read.")
        + '</p><p class="value ok" id="out-skipped">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Table fetches (without filter) '
        + ht("Without rowid filter, every main-index row triggers a table fetch.")
        + '</p><p class="value" id="out-without">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Table fetches (with filter) '
        + ht("With rowid filter, only bitmap-passing rows trigger a table fetch.")
        + '</p><p class="value" id="out-with">\u2014</p></div>\n'
        '    <div class="item"><p class="label">I/O saved '
        + ht("Random I/Os saved by skipping non-matching rowids. Equal to rows_skipped.")
        + '</p><p class="value ok" id="out-saved">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Table fetches vs main index rows (log\u2013log, selectivity fixed)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more &mdash; when does MariaDB use rowid filter?</summary>
  <div class="body">
    <p>Rowid filtering was introduced in <strong>MariaDB 10.4</strong> and is
    enabled by default
    (<code>optimizer_switch='rowid_filter=on'</code>).
    This optimisation is <strong>not available in MySQL</strong>.</p>

    <p>The optimizer considers rowid filter when:</p>
    <p>1. The query accesses a table through one index (the <em>main</em>
    access path) but another index could filter out a large fraction of
    rows before the expensive table-row fetch.</p>
    <p>2. The filtering index is selective enough that building the
    in-memory rowid bitmap and checking it per row is cheaper than
    fetching every row from the table.</p>
    <p>3. The bitmap fits in memory. MariaDB allocates a compact
    bit-array keyed by rowid, so even millions of rows consume only
    a few megabytes.</p>

    <p>Rowid filter works best when the <strong>filter index is very
    selective</strong> (few rows match) but the <strong>main index returns
    many rows</strong>. The more rows the bitmap can eliminate, the more
    random I/O is saved.</p>

    <p>In <code>EXPLAIN</code> output you will see
    <strong>Rowid-ordered scan</strong> or <strong>Using rowid filter</strong>
    in the <code>Extra</code> column.</p>

    <p>Sources: MariaDB Knowledge Base &ldquo;Rowid Filtering Optimization&rdquo;;
    MariaDB Server 10.4 Release Notes.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="rowid_filter",
        title="Rowid Filter \u2014 bitmap pre-filter before table access",
        subtitle=(
            "See how MariaDB scans a filtering index to build a rowid bitmap, "
            "then skips table-row fetches for non-matching rows."
        ),
        version_chip="MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
