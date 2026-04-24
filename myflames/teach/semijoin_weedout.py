"""Lesson: Semijoin Duplicate Weedout.

Shows how MySQL rewrites an IN/EXISTS subquery as a semijoin (inner join)
and then uses a temporary table keyed on the outer-table rowid to remove
duplicate outer rows. Concrete sample data: customers and their orders.

Animation zones:
  Zone 1 (top)    — inner join producing rows with duplicates
  Zone 2 (middle) — temp table with rowid column (inserts succeed/fail)
  Zone 3 (bottom) — deduplicated result set
"""
from . import _html


_LESSON_JS_TEMPLATE = r"""
function weedoutCost(outerRows, innerMatches, rowSize) {
  var joinRows = outerRows * innerMatches;
  var uniqueRows = outerRows;
  var duplicatesDiscarded = joinRows - uniqueRows;
  var rowidSize = 8;
  var tmpTableSize = 16 * 1024 * 1024;  // 16 MiB default
  var tempFitsMemory = (uniqueRows * rowidSize) <= tmpTableSize;
  var tempInserts = joinRows;
  var tempDupChecks = joinRows;
  var dupFactor = innerMatches;
  // Materialization alternative: materialize subquery once, probe per outer row
  var materializeCost = outerRows + outerRows;  // build once + probe once per outer
  var weedoutWork = joinRows;  // every join row must be checked
  return {
    joinRows: joinRows,
    uniqueRows: uniqueRows,
    duplicatesDiscarded: duplicatesDiscarded,
    tempFitsMemory: tempFitsMemory,
    tempInserts: tempInserts,
    tempDupChecks: tempDupChecks,
    dupFactor: dupFactor,
    materializeCost: materializeCost,
    weedoutWork: weedoutWork
  };
}

var W = 800, H = 440;
var stage = null;

// ---- Sample data ----
var CUSTOMERS = [
  { id: 1, name: "Alice" },
  { id: 2, name: "Bob" },
  { id: 3, name: "Carol" },
  { id: 4, name: "Dave" },
  { id: 5, name: "Eve" }
];
var ORDERS = [
  { custId: 1, desc: "order $1200" },
  { custId: 1, desc: "order $1500" },
  { custId: 2, desc: "order $2000" },
  { custId: 3, desc: "order $1100" },
  { custId: 3, desc: "order $3000" },
  { custId: 3, desc: "order $1800" },
  { custId: 5, desc: "order $5000" }
];

// Build join pairs (inner join result — with duplicates)
var JOIN_PAIRS = [];
for (var ci = 0; ci < CUSTOMERS.length; ci++) {
  for (var oi = 0; oi < ORDERS.length; oi++) {
    if (ORDERS[oi].custId === CUSTOMERS[ci].id) {
      JOIN_PAIRS.push({ cust: CUSTOMERS[ci], order: ORDERS[oi] });
    }
  }
}

function buildStage() {
  var svg = document.getElementById("weedout-svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // ---- Zone 1: Inner join result (top) ----
  var joinLbl = anim.svgEl("text", {
    x: 20, y: 24, "font-size": 12, "font-weight": 700, fill: "#1e40af"
  });
  joinLbl.textContent = "Inner join result (customers \u00d7 orders) \u2014 contains duplicates";
  svg.appendChild(joinLbl);

  var joinRect = anim.svgEl("rect", {
    x: 20, y: 34, width: W - 40, height: 90, rx: 8, ry: 8,
    fill: "#eff6ff", stroke: "#3b82f6", "stroke-width": 1.5
  });
  svg.appendChild(joinRect);

  // Pre-render join pair labels
  var pairLabels = [];
  for (var p = 0; p < JOIN_PAIRS.length; p++) {
    var px = 50 + (p % 4) * 185;
    var py = 55 + Math.floor(p / 4) * 30;
    var pl = anim.svgEl("text", {
      x: px, y: py, "font-size": 10, "font-weight": 600, fill: "#1e3a8a", opacity: 0
    });
    pl.textContent = JOIN_PAIRS[p].cust.name + " \u00d7 " + JOIN_PAIRS[p].order.desc;
    svg.appendChild(pl);
    pairLabels.push(pl);
  }

  // ---- Zone 2: Temp table (middle, yellow) ----
  var tempY = 155;
  var tempLbl = anim.svgEl("text", {
    x: 20, y: tempY, "font-size": 12, "font-weight": 700, fill: "#92400e"
  });
  tempLbl.textContent = "Weedout temp table (key = customer rowid)";
  svg.appendChild(tempLbl);

  var tempRect = anim.svgEl("rect", {
    x: 20, y: tempY + 10, width: W - 40, height: 110, rx: 8, ry: 8,
    fill: "#fefce8", stroke: "#ca8a04", "stroke-width": 1.5
  });
  svg.appendChild(tempRect);

  // Column headers for temp table
  var colHdrRowid = anim.svgEl("text", {
    x: 60, y: tempY + 30, "font-size": 10, "font-weight": 700, fill: "#78350f"
  });
  colHdrRowid.textContent = "rowid";
  svg.appendChild(colHdrRowid);
  var colHdrName = anim.svgEl("text", {
    x: 140, y: tempY + 30, "font-size": 10, "font-weight": 700, fill: "#78350f"
  });
  colHdrName.textContent = "customer";
  svg.appendChild(colHdrName);
  var colHdrResult = anim.svgEl("text", {
    x: 280, y: tempY + 30, "font-size": 10, "font-weight": 700, fill: "#78350f"
  });
  colHdrResult.textContent = "insert result";
  svg.appendChild(colHdrResult);

  // Slots for temp table inserts (up to 7 visible rows)
  var tempSlots = [];
  for (var s = 0; s < 7; s++) {
    var sy = tempY + 42 + s * 11;
    var sRowid = anim.svgEl("text", {
      x: 60, y: sy, "font-size": 9, "font-weight": 600, fill: "#78350f", opacity: 0
    });
    svg.appendChild(sRowid);
    var sName = anim.svgEl("text", {
      x: 140, y: sy, "font-size": 9, "font-weight": 600, fill: "#78350f", opacity: 0
    });
    svg.appendChild(sName);
    var sResult = anim.svgEl("text", {
      x: 280, y: sy, "font-size": 9, "font-weight": 700, fill: "#78350f", opacity: 0
    });
    svg.appendChild(sResult);
    tempSlots.push({ rowid: sRowid, name: sName, result: sResult });
  }

  // Duplicate counter on the right side of temp table
  var dupCounterLbl = anim.svgEl("text", {
    x: W - 60, y: tempY + 50, "text-anchor": "middle",
    "font-size": 11, "font-weight": 700, fill: "#991b1b"
  });
  dupCounterLbl.textContent = "Duplicates: 0";
  svg.appendChild(dupCounterLbl);

  var newCounterLbl = anim.svgEl("text", {
    x: W - 60, y: tempY + 70, "text-anchor": "middle",
    "font-size": 11, "font-weight": 700, fill: "#047857"
  });
  newCounterLbl.textContent = "New: 0";
  svg.appendChild(newCounterLbl);

  // ---- Zone 3: Deduplicated result (bottom, green) ----
  var resultY = 310;
  var resultLbl = anim.svgEl("text", {
    x: 20, y: resultY, "font-size": 12, "font-weight": 700, fill: "#065f46"
  });
  resultLbl.textContent = "Deduplicated result set";
  svg.appendChild(resultLbl);

  var resultRect = anim.svgEl("rect", {
    x: 20, y: resultY + 10, width: W - 40, height: 60, rx: 8, ry: 8,
    fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.5
  });
  svg.appendChild(resultRect);

  var resultContent = anim.svgEl("text", {
    x: W / 2, y: resultY + 45, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#065f46"
  });
  resultContent.textContent = "(unique customers appear here)";
  svg.appendChild(resultContent);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 16, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  // Result row labels (up to 5 customers)
  var resultLabels = [];
  for (var r = 0; r < 5; r++) {
    var rx = 80 + r * 150;
    var rl = anim.svgEl("text", {
      x: rx, y: resultY + 35, "font-size": 10, "font-weight": 700, fill: "#047857", opacity: 0
    });
    svg.appendChild(rl);
    resultLabels.push(rl);
  }

  stage = {
    svg: svg,
    pairLabels: pairLabels,
    tempSlots: tempSlots,
    dupCounterLbl: dupCounterLbl,
    newCounterLbl: newCounterLbl,
    resultRect: resultRect,
    resultContent: resultContent,
    resultLabels: resultLabels,
    statusLbl: statusLbl,
    tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  stage.pairLabels.forEach(function(l) { l.setAttribute("opacity", 0); });
  stage.tempSlots.forEach(function(s) {
    s.rowid.setAttribute("opacity", 0);
    s.name.setAttribute("opacity", 0);
    s.result.setAttribute("opacity", 0);
  });
  stage.dupCounterLbl.textContent = "Duplicates: 0";
  stage.newCounterLbl.textContent = "New: 0";
  stage.resultContent.textContent = "(unique customers appear here)";
  stage.resultLabels.forEach(function(l) { l.setAttribute("opacity", 0); l.textContent = ""; });
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var seen = {};        // rowid -> true
  var dupCount = 0;
  var newCount = 0;
  var slotIdx = 0;
  var resultIdx = 0;

  // ---- Phase 1: Show the inner join producing rows ----
  tl.mark("Inner join (with duplicates)");
  tl.call(function() {
    phase.textContent = "Phase 1 \u2014 MySQL runs the subquery as an inner join (produces duplicates)";
  });

  for (var i = 0; i < JOIN_PAIRS.length; i++) {
    (function(idx) {
      tl.call(function() {
        var pair = JOIN_PAIRS[idx];
        stage.pairLabels[idx].setAttribute("opacity", 1);
        phase.textContent = "Join row " + (idx + 1) + "/" + JOIN_PAIRS.length +
          ": " + pair.cust.name + " \u00d7 " + pair.order.desc;
      });
      tl.delay(350);
    })(i);
  }

  tl.delay(600);
  tl.call(function() {
    phase.textContent = "Inner join produced " + JOIN_PAIRS.length +
      " rows \u2014 but some customers appear multiple times. Now weedout begins\u2026";
  });
  tl.delay(800);

  // ---- Phase 2: Weedout — try inserting each rowid into temp table ----
  tl.mark("Weedout (dedup via temp table)");
  tl.call(function() {
    phase.textContent = "Phase 2 \u2014 for each join row, try INSERT into temp table keyed on customer rowid";
  });
  tl.delay(400);

  for (var j = 0; j < JOIN_PAIRS.length; j++) {
    (function(idx) {
      var pair = JOIN_PAIRS[idx];
      var rowid = pair.cust.id;
      var isDuplicate = false;

      tl.call(function() {
        // Highlight the current join pair
        stage.pairLabels[idx].setAttribute("fill", "#7c3aed");

        isDuplicate = !!seen[rowid];
        if (!isDuplicate) {
          seen[rowid] = true;
        }

        // Show in temp table
        if (slotIdx < stage.tempSlots.length) {
          var slot = stage.tempSlots[slotIdx];
          slot.rowid.textContent = "rowid=" + rowid;
          slot.name.textContent = pair.cust.name;
          slot.rowid.setAttribute("opacity", 1);
          slot.name.setAttribute("opacity", 1);
          slot.result.setAttribute("opacity", 1);

          if (isDuplicate) {
            slot.result.textContent = "\u2717 duplicate \u2014 discard";
            slot.result.setAttribute("fill", "#dc2626");
            slot.rowid.setAttribute("fill", "#dc2626");
            slot.name.setAttribute("fill", "#dc2626");
            dupCount++;
            stage.dupCounterLbl.textContent = "Duplicates: " + dupCount;
          } else {
            slot.result.textContent = "\u2713 new \u2014 emit to result";
            slot.result.setAttribute("fill", "#059669");
            slot.rowid.setAttribute("fill", "#059669");
            slot.name.setAttribute("fill", "#059669");
            newCount++;
            stage.newCounterLbl.textContent = "New: " + newCount;

            // Add to result zone
            if (resultIdx < stage.resultLabels.length) {
              stage.resultLabels[resultIdx].textContent = pair.cust.name;
              anim.tween({
                from: 0, to: 1, duration: 250, ease: anim.easeOutCubic,
                onUpdate: function() {
                  var ri = resultIdx - 1;
                  if (ri >= 0 && ri < stage.resultLabels.length) {
                    stage.resultLabels[ri].setAttribute("opacity", 1);
                  }
                }
              });
              stage.resultLabels[resultIdx].setAttribute("opacity", 1);
              resultIdx++;
            }
          }
          slotIdx++;
        }

        var action = isDuplicate ? "DUPLICATE \u2014 discard" : "NEW \u2014 emit";
        phase.textContent = "Row " + (idx + 1) + ": " + pair.cust.name +
          " (rowid=" + rowid + ") \u2192 " + action;
      });
      tl.delay(500);

      // Reset highlight
      tl.call(function() {
        stage.pairLabels[idx].setAttribute("fill", "#1e3a8a");
      });
    })(j);
  }

  tl.delay(500);

  // ---- Phase 3: Done ----
  tl.mark("Done");
  tl.call(function() {
    phase.textContent = "\u2713 Weedout complete \u2014 " + newCount + " unique customers, " +
      dupCount + " duplicates discarded";
    stage.statusLbl.textContent = JOIN_PAIRS.length + " join rows \u2192 " +
      newCount + " unique results. Duplication factor: " +
      (JOIN_PAIRS.length / newCount).toFixed(1) + "\u00d7";
    stage.resultContent.textContent = newCount + " unique customers emitted";
    stage.resultRect.setAttribute("stroke", "#047857");
    stage.resultRect.setAttribute("stroke-width", 2.5);
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

function renderChart(outerRows, innerMatches) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 500000,
    xLabel: "outer rows", yLabel: "Total work (rows processed)",
    curves: [
      { label: "Weedout: outer \u00d7 inner_matches", color: "#ca8a04",
        fn: function(n) { return n * innerMatches; } },
      { label: "Materialization: outer + subquery", color: "#0d9488",
        fn: function(n) { return n + n; } }
    ],
    current: { x: outerRows },
    xSlider: "outer_rows",
    xSliderTransform: function(xVal) { return Math.max(100, Math.round(xVal / 100) * 100); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = weedoutCost(c.outer_rows, c.inner_matches, c.row_size);
  document.getElementById("out-join-rows").textContent = teachRuntime.formatInt(cost.joinRows);
  document.getElementById("out-unique-rows").textContent = teachRuntime.formatInt(cost.uniqueRows);
  document.getElementById("out-discarded").textContent = teachRuntime.formatInt(cost.duplicatesDiscarded);
  document.getElementById("out-temp-inserts").textContent = teachRuntime.formatInt(cost.tempInserts);
  document.getElementById("out-dup-factor").textContent = cost.dupFactor.toFixed(1) + "\u00d7";
  document.getElementById("out-temp-memory").textContent = cost.tempFitsMemory ? "In-memory" : "On-disk";
  document.getElementById("out-temp-memory").className = "value " + (cost.tempFitsMemory ? "ok" : "hot");
  document.getElementById("out-explanation").textContent =
    "The inner join produces " + teachRuntime.formatInt(cost.joinRows) +
    " rows (" + cost.dupFactor.toFixed(1) + "\u00d7 fan-out). " +
    "DuplicateWeedout inserts each row\u2019s outer rowid into a temp table. " +
    teachRuntime.formatInt(cost.duplicatesDiscarded) + " duplicates are rejected (key exists). " +
    teachRuntime.formatInt(cost.uniqueRows) + " unique rows survive. " +
    "Temp table is " + (cost.tempFitsMemory ? "in-memory (fast)." : "on-disk (slower \u2014 consider raising tmp_table_size).");
  buildStage();
  resetStage();
  renderChart(c.outer_rows, c.inner_matches);
}

buildStage();
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
  <h2 id="controls-h">Parameters (Semijoin Duplicate Weedout)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows">Outer table rows (customers): <span class="value-pill" data-pill-for="outer_rows">1000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="500000" step="100" value="1000">
      <div class="hint">Number of rows in the outer (driving) table.</div>
    </div>

    <div class="control">
      <label for="inner_matches">Avg inner matches per outer row: <span class="value-pill" data-pill-for="inner_matches">5</span></label>
      <input type="range" id="inner_matches" name="inner_matches" min="1" max="100" step="1" value="5">
      <div class="hint">Average orders per customer matching the WHERE clause. This creates duplicates.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of each row in the outer table.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- IN subquery \u2192 semijoin \u2192 duplicate weedout\n"
            "SELECT * FROM customers c\n"
            "WHERE  c.id IN (\n"
            "  SELECT o.customer_id\n"
            "  FROM   orders o\n"
            "  WHERE  o.total > 1000\n"
            ");"
        ),
        note=(
            "MySQL rewrites the IN subquery as an inner join, which may "
            "produce duplicate customer rows. DuplicateWeedout uses a temp "
            "table keyed on c.rowid to remove duplicates."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Phase 1 \u2014 inner join: MySQL rewrites the IN subquery as a "
            "normal inner join between customers and orders. Each customer "
            "with multiple matching orders appears multiple times (e.g. Alice "
            "appears twice because she has two orders > $1000).",
            "Phase 2 \u2014 weedout: for each join result row, MySQL tries to "
            "INSERT the outer-table rowid into a temporary table with a "
            "unique key. If the insert succeeds (\u2713 green), the row is new "
            "\u2014 emit it. If it fails (\u2717 red), the rowid was already seen "
            "\u2014 discard the duplicate.",
            "The temp table is keyed on the outer-table rowid (8 bytes). If "
            "unique_rows \u00d7 8 fits in tmp_table_size, the temp table stays "
            "in memory. Otherwise it spills to disk.",
            "The duplicate counter on the right tracks how many rows were "
            "discarded. The deduplicated result set at the bottom shows only "
            "the unique customers that survived.",
            "Total work = join_rows = outer_rows \u00d7 inner_matches. Every "
            "row must be checked against the temp table. Higher fan-out "
            "means more wasted work on duplicates.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %s\n"
        "  %s\n"
        "  %s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="weedout-svg" viewBox="0 0 800 440"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "    %s\n"
        "  </div>\n"
        "</section>"
    ) % (
        query_card_html,
        explainer_html,
        _html.stage_toolbar("Ready \u2014 press Play"),
        _html.phase_nav(),
    )

    ht = _html.help_tip
    readout_html = (
        '<section class="readout">\n'
        "  <h2>Cost readout (Semijoin Duplicate Weedout)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Join rows (before dedup) '
        + ht("Total rows produced by the inner join = outer_rows \u00d7 inner_matches. All of these must be checked against the temp table.")
        + '</p><p class="value" id="out-join-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Unique rows (after dedup) '
        + ht("At most outer_rows survive after weedout. Each unique outer rowid appears exactly once in the result.")
        + '</p><p class="value" id="out-unique-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Duplicates discarded '
        + ht("join_rows \u2212 unique_rows. These rows were produced by the inner join but rejected because their outer rowid was already in the temp table.")
        + '</p><p class="value" id="out-discarded">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Temp table inserts '
        + ht("Every join row triggers an INSERT attempt into the weedout temp table. Successful inserts mean new row; failed inserts mean duplicate.")
        + '</p><p class="value" id="out-temp-inserts">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Duplication factor '
        + ht("inner_matches \u2014 how many times each outer row is duplicated on average. Higher = more wasted work.")
        + '</p><p class="value" id="out-dup-factor">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Temp table location '
        + ht("If unique_rows \u00d7 8 bytes fits in tmp_table_size (16 MiB default), the temp table stays in memory. Otherwise it spills to disk.")
        + '</p><p class="value ok" id="out-temp-memory">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Weedout work vs materialization (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more \u2014 DuplicateWeedout among the four semijoin strategies</summary>
  <div class="body">
    <p>MySQL\u2019s optimizer can rewrite <code>IN (SELECT \u2026)</code> and
    <code>EXISTS (SELECT \u2026)</code> subqueries as semijoins. It then
    chooses among four execution strategies:</p>

    <p><strong>1. FirstMatch</strong> \u2014 as soon as the first inner row
    matches an outer row, stop scanning the inner table for that outer row.
    Works well when the subquery is correlated and selective.</p>

    <p><strong>2. LooseScan</strong> \u2014 scans the inner table\u2019s index
    and skips duplicate key values, feeding only distinct keys to the outer
    join. Requires a suitable index on the inner side.</p>

    <p><strong>3. DuplicateWeedout</strong> (this lesson) \u2014 runs the
    full inner join, then removes duplicates using a temporary table keyed
    on the outer-table rowid. The most general strategy \u2014 works even
    when FirstMatch and LooseScan can\u2019t.</p>

    <p><strong>4. Materialization</strong> \u2014 materializes the subquery
    into a temp table once, then probes it for each outer row. Good when
    the subquery result is small and reusable.</p>

    <p>DuplicateWeedout is controlled by
    <code>optimizer_switch=duplicateweedout=on</code> (enabled by default).
    Its cost depends on the join fan-out (how many inner rows match each
    outer row) and whether the weedout temp table fits in memory.</p>

    <p>When the fan-out is high, DuplicateWeedout does significant wasted
    work processing duplicate rows. In those cases, Materialization or
    FirstMatch may be cheaper \u2014 but DuplicateWeedout is the fallback
    that always works.</p>

    <p>Sources: MySQL 8.4 Reference Manual \u00a78.2.2.1 \u201cOptimizing
    IN and EXISTS Subquery Predicates with Semijoin Transformations\u201d;
    MariaDB Knowledge Base \u201cSemijoin Subquery Optimizations\u201d.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="semijoin_weedout",
        title="Semijoin Duplicate Weedout \u2014 dedup via temp table",
        subtitle=(
            "MySQL rewrites IN/EXISTS subqueries as inner joins, then uses "
            "a temporary table keyed on the outer rowid to remove duplicates. "
            "Watch the weedout process row by row."
        ),
        version_chip="MySQL 8.4 \u2022 MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
