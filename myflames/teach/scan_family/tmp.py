"""Lesson: Temporary tables — MEMORY to on-disk InnoDB conversion.

Animates how GROUP BY / DISTINCT / UNION materialize rows into a
MEMORY temp table, and what happens when the data exceeds
``min(tmp_table_size, max_heap_table_size)`` — the dramatic conversion
to an on-disk InnoDB table.

Concrete sample data: employees grouped by department, counting
headcount per department.
"""
from .. import _html
from .._cost_model import TMP_TABLE_SIZE_DEFAULT, MAX_HEAP_TABLE_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = r"""
var TMP_TABLE_SIZE_DEFAULT = %d;
var MAX_HEAP_TABLE_SIZE_DEFAULT = %d;

function tmpTableCost(rows, rowSize, tmpSize, maxHeap) {
  var limit = Math.min(tmpSize, maxHeap);
  var cap = Math.max(1, Math.floor(limit / rowSize));
  var fits = rows <= cap;
  var memRows = Math.min(rows, cap);
  var diskRows = fits ? 0 : rows - cap;
  return {limit: limit, cap: cap, fits: fits, memRows: memRows, diskRows: diskRows};
}

var W = 800, H = 360;
var stage = null;

var EMPLOYEES = [
  {name: "Alice", dept: "Engineering"},
  {name: "Bob", dept: "Sales"},
  {name: "Carol", dept: "Engineering"},
  {name: "Dave", dept: "Marketing"},
  {name: "Eve", dept: "Engineering"},
  {name: "Frank", dept: "Sales"},
  {name: "Grace", dept: "HR"},
  {name: "Hank", dept: "Marketing"},
  {name: "Iris", dept: "Engineering"},
  {name: "Jake", dept: "Sales"},
  {name: "Kate", dept: "HR"},
  {name: "Leo", dept: "Engineering"}
];

function buildStage(fits) {
  var svg = document.getElementById("tmp-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // Source rows (top)
  var srcLbl = anim.svgEl("text", {
    x: 20, y: 28, "font-size": 13, "font-weight": 700, fill: "#0c4a6e"
  });
  srcLbl.textContent = "employees table \u2192 streaming rows";
  svg.appendChild(srcLbl);

  var srcRect = anim.svgEl("rect", {
    x: 20, y: 40, width: W - 40, height: 50, rx: 8, ry: 8,
    fill: "#f0f9ff", stroke: "#0284c7", "stroke-width": 1.5
  });
  svg.appendChild(srcRect);

  // MEMORY temp table (middle)
  var memY = 120;
  var memLbl = anim.svgEl("text", {
    x: 20, y: memY, "font-size": 13, "font-weight": 700, fill: "#047857"
  });
  memLbl.textContent = "MEMORY temp table (GROUP BY department)";
  svg.appendChild(memLbl);

  var memRect = anim.svgEl("rect", {
    x: 20, y: memY + 12, width: W - 40, height: 65, rx: 8, ry: 8,
    fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.5
  });
  svg.appendChild(memRect);

  var memContent = anim.svgEl("text", {
    x: W / 2, y: memY + 50, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#065f46"
  });
  memContent.textContent = "(rows accumulate here — fast, in-memory)";
  svg.appendChild(memContent);

  // Capacity bar inside mem table
  var barX = 30, barY = memY + 22, barW = W - 60, barH = 14;
  var barBg = anim.svgEl("rect", {
    x: barX, y: barY, width: barW, height: barH, rx: 3, ry: 3,
    fill: "#d1fae5", stroke: "#a7f3d0", "stroke-width": 1
  });
  svg.appendChild(barBg);
  var barFill = anim.svgEl("rect", {
    x: barX, y: barY, width: 0, height: barH, rx: 3, ry: 3,
    fill: "#34d399"
  });
  svg.appendChild(barFill);
  var barPct = anim.svgEl("text", {
    x: barX + barW / 2, y: barY + 11, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#065f46"
  });
  barPct.textContent = "0%%";
  svg.appendChild(barPct);

  // On-disk area (bottom)
  var diskY = 225;
  var diskLbl = anim.svgEl("text", {
    x: 20, y: diskY, "font-size": 13, "font-weight": 700, fill: "#991b1b"
  });
  diskLbl.textContent = "on-disk InnoDB temp table (after conversion)";
  svg.appendChild(diskLbl);

  var diskRect = anim.svgEl("rect", {
    x: 20, y: diskY + 12, width: W - 40, height: 55, rx: 8, ry: 8,
    fill: "#fef2f2", stroke: "#dc2626", "stroke-width": 1.5, opacity: 0.3
  });
  svg.appendChild(diskRect);

  var diskContent = anim.svgEl("text", {
    x: W / 2, y: diskY + 44, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#991b1b", opacity: 0.3
  });
  diskContent.textContent = "(slow — random I/O on disk)";
  svg.appendChild(diskContent);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 20, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, srcRect: srcRect,
    memRect: memRect, memContent: memContent, memLbl: memLbl,
    barFill: barFill, barBg: barBg, barPct: barPct,
    barX: barX, barW: barW,
    diskRect: diskRect, diskContent: diskContent, diskLbl: diskLbl,
    statusLbl: statusLbl, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  stage.memRect.setAttribute("fill", "#ecfdf5");
  stage.memRect.setAttribute("stroke", "#059669");
  stage.memContent.textContent = "(rows accumulate here \u2014 fast, in-memory)";
  stage.barFill.setAttribute("width", 0);
  stage.barFill.setAttribute("fill", "#34d399");
  stage.barPct.textContent = "0%%";
  stage.diskRect.setAttribute("opacity", 0.3);
  stage.diskContent.setAttribute("opacity", 0.3);
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

function buildTimeline(cap, fits) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var total = EMPLOYEES.length;
  var conversionRow = Math.min(cap, total);

  tl.mark("Stream into MEMORY");
  tl.call(function() {
    phase.textContent = "Phase 1 \u2014 streaming rows into MEMORY temp table";
  });

  for (var i = 0; i < total; i++) {
    (function(idx) {
      var emp = EMPLOYEES[idx];
      var label = emp.name + " (" + emp.dept + ")";
      var isConversion = !fits && idx === conversionRow;

      tl.call(function() {
        // Update capacity bar
        var pct = Math.min(100, Math.round(((idx + 1) / cap) * 100));
        var fillW = Math.min(stage.barW, stage.barW * (idx + 1) / cap);
        stage.barFill.setAttribute("width", fillW);
        stage.barPct.textContent = pct + "%%";

        if (pct >= 90 && !fits) {
          stage.barFill.setAttribute("fill", "#f87171");
          stage.memRect.setAttribute("stroke", "#dc2626");
        } else if (pct >= 70 && !fits) {
          stage.barFill.setAttribute("fill", "#fbbf24");
          stage.memRect.setAttribute("stroke", "#f59e0b");
        }

        var cx = 60 + (idx %% 8) * 90;
        var tuple = spawnTuple(label, idx < conversionRow ? "#059669" : "#dc2626", cx, 65);
        anim.tween({from: 0, to: 1, duration: 180, ease: anim.easeOutCubic,
          onUpdate: function(v) { tuple.setAttribute("opacity", v); }});

        if (idx < conversionRow) {
          stage.memContent.textContent = (idx + 1) + " rows in MEMORY (" + pct + "%% full)";
        }
      });

      if (isConversion) {
        tl.delay(300);
        tl.mark("MEMORY \u2192 disk conversion");
        tl.call(function() {
          phase.textContent = "\u26a0 MEMORY limit exceeded \u2014 converting to on-disk InnoDB!";
          stage.memRect.setAttribute("fill", "#fef2f2");
          stage.memRect.setAttribute("stroke", "#dc2626");
          stage.memContent.textContent = "CONVERTING TO DISK\u2026";
          stage.barFill.setAttribute("fill", "#ef4444");
          stage.barPct.textContent = "FULL";
          stage.diskRect.setAttribute("opacity", 1);
          stage.diskContent.setAttribute("opacity", 1);
          stage.diskContent.textContent = "Conversion in progress\u2026 copying all rows to InnoDB";
        });
        tl.delay(600);
        tl.call(function() {
          stage.diskContent.textContent = "On-disk InnoDB temp table active \u2014 remaining rows go here";
          phase.textContent = "Phase 2 \u2014 inserting remaining rows on disk (slow)";
        });
        tl.mark("Insert on disk (slow)");
      }

      if (!fits && idx >= conversionRow) {
        tl.call(function() {
          stage.diskContent.textContent = (idx - conversionRow + 1) + " rows inserted on disk";
        });
      }

      tl.delay(200);

      // Fade out tuple
      tl.call(function() {
        stage.tuples.forEach(function(t) {
          if (parseFloat(t.getAttribute("opacity") || 1) > 0.3) {
            anim.tween({from: 1, to: 0, duration: 150, ease: anim.easeInCubic,
              onUpdate: function(v) { t.setAttribute("opacity", v); }});
          }
        });
      });
    })(i);
  }

  tl.delay(300);
  tl.mark("Done");
  tl.call(function() {
    if (fits) {
      phase.textContent = "\u2713 All rows fit in MEMORY \u2014 no disk conversion needed!";
      stage.statusLbl.textContent = "Fast path: pure MEMORY temp table, 0 disk I/O.";
    } else {
      phase.textContent = "\u2713 GROUP BY complete \u2014 but conversion to disk was costly";
      stage.statusLbl.textContent = "The MEMORY \u2192 disk conversion adds significant latency. Raise tmp_table_size or add an index.";
    }
  });

  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var cost = tmpTableCost(c.rows, c.row_size, c.tmp_size, c.max_heap);
  return buildTimeline(cost.cap, cost.fits);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = tmpTableCost(c.rows, c.row_size, c.tmp_size, c.max_heap);
  document.getElementById("out-limit").textContent = teachRuntime.formatBytes(cost.limit);
  document.getElementById("out-cap").textContent = teachRuntime.formatInt(cost.cap);
  document.getElementById("out-mem-rows").textContent = teachRuntime.formatInt(cost.memRows);
  document.getElementById("out-disk-rows").textContent = teachRuntime.formatInt(cost.diskRows);
  document.getElementById("out-conversion").textContent = cost.fits ? "No \u2014 all in MEMORY" : "Yes \u2014 converted to disk";
  document.getElementById("out-conversion").className = "value " + (cost.fits ? "ok" : "hot");
  document.getElementById("out-explanation").textContent =
    cost.fits
      ? "All " + cost.memRows + " rows fit in the MEMORY temp table (limit " +
        teachRuntime.formatBytes(cost.limit) + "). No on-disk conversion needed."
      : "MEMORY temp table holds " + cost.cap + " rows (" +
        teachRuntime.formatBytes(cost.limit) + "). Row " + (cost.cap + 1) +
        " triggers conversion to on-disk InnoDB \u2014 " + cost.diskRows +
        " remaining rows inserted on disk. Raise tmp_table_size or max_heap_table_size.";
  buildStage(cost.fits);
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


def render() -> str:
    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (MySQL 8.4 internal temporary tables)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows to materialize: <span class="value-pill" data-pill-for="rows">10000</span></label>
      <input type="range" id="rows" name="rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Total rows inserted into the internal temp table.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of each materialized row in the temp table.</div>
    </div>

    <div class="control">
      <label for="tmp_size">tmp_table_size (bytes): <span class="value-pill" data-pill-for="tmp_size">{TMP_TABLE_SIZE_DEFAULT}</span></label>
      <input type="range" id="tmp_size" name="tmp_size" min="65536" max="134217728" step="65536" value="{TMP_TABLE_SIZE_DEFAULT}">
      <div class="hint">Default: 16 MiB. MySQL uses min(tmp_table_size, max_heap_table_size).</div>
    </div>

    <div class="control">
      <label for="max_heap">max_heap_table_size (bytes): <span class="value-pill" data-pill-for="max_heap">{MAX_HEAP_TABLE_SIZE_DEFAULT}</span></label>
      <input type="range" id="max_heap" name="max_heap" min="65536" max="134217728" step="65536" value="{MAX_HEAP_TABLE_SIZE_DEFAULT}">
      <div class="hint">Default: 16 MiB. The effective limit is min(tmp_table_size, max_heap_table_size).</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- GROUP BY without index \u2192 internal temp table\n"
            "SELECT   department, COUNT(*) AS headcount\n"
            "FROM     employees\n"
            "GROUP BY department\n"
            "HAVING   COUNT(*) > 5;"
        ),
        note="Internal temp-table operator flow: materialize rows in MEMORY first, then convert to on-disk InnoDB when the effective limit is exceeded."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Rows from child operators are inserted into internal MEMORY temp table first.",
            "Capacity is bounded by min(tmp_table_size, max_heap_table_size).",
            "Once limit is crossed, MySQL converts to on-disk InnoDB temp table.",
            "Remaining inserts hit disk-backed structure, increasing latency.",
            "Higher effective limit delays conversion and reduces disk I/O risk.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="tmp-svg" viewBox="0 0 800 360" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (internal temporary tables)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Effective limit {ht("MySQL uses min(tmp_table_size, max_heap_table_size) as the effective MEMORY temp table limit.")}</p><p class="value" id="out-limit">\u2014</p></div>
    <div class="item"><p class="label">MEMORY capacity (rows) {ht("How many rows fit in the MEMORY temp table before the on-disk conversion is triggered.")}</p><p class="value" id="out-cap">\u2014</p></div>
    <div class="item"><p class="label">Rows in MEMORY {ht("Rows inserted while the temp table is still in-memory. These are fast, no disk I/O.")}</p><p class="value" id="out-mem-rows">\u2014</p></div>
    <div class="item"><p class="label">Rows on disk {ht("Rows inserted after the MEMORY limit was exceeded and the table was converted to on-disk InnoDB.")}</p><p class="value" id="out-disk-rows">\u2014</p></div>
    <div class="item"><p class="label">Disk conversion? {ht("If Yes, the MEMORY temp table exceeded the limit and was converted to on-disk InnoDB — a costly operation.")}</p><p class="value ok" id="out-conversion">\u2014</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when does MySQL use internal temp tables?</summary>
  <div class="body">
    <p>MySQL creates internal temporary tables for several query patterns:</p>
    <p><strong>GROUP BY / DISTINCT / ORDER BY with GROUP BY</strong> — when
    there's no covering index that produces rows in the grouped/sorted order.
    This is the most common trigger.</p>
    <p><strong>UNION / UNION DISTINCT</strong> — the de-duplication step
    materializes into a temp table. <code>UNION ALL</code> does not need one.</p>
    <p><strong>Derived tables and CTEs</strong> — subqueries in FROM clauses
    and non-recursive CTEs are often materialized.</p>
    <p><strong>Semijoin materialization</strong> — the optimizer may
    materialize the inner side of <code>IN (SELECT ...)</code> into a temp
    table and probe it for each outer row.</p>
    <p>The conversion to on-disk happens when <strong>any</strong> of these
    are true: (1) data exceeds <code>min(tmp_table_size, max_heap_table_size)</code>,
    (2) the table contains BLOB/TEXT columns (can't be stored in MEMORY engine),
    (3) the total row length exceeds MEMORY engine limits.</p>
    <p>In MySQL 8.4, on-disk temp tables use the <strong>TempTable</strong>
    storage engine by default (<code>internal_tmp_mem_storage_engine=TempTable</code>),
    which is more efficient than the old InnoDB-backed path. The TempTable engine
    also has its own memory limit: <code>temptable_max_ram</code> (default 1 GiB).</p>
    <p>Sources: MySQL 8.4 reference manual §10.4.4 "Internal Temporary Table Use";
    MySQL 8.4 reference manual §7.1.8 "Server System Variables".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (TMP_TABLE_SIZE_DEFAULT, MAX_HEAP_TABLE_SIZE_DEFAULT)

    return _html.render_page(
        lesson_id="tmp",
        title="Temporary tables — MEMORY to on-disk conversion",
        subtitle=(
            "Watch a GROUP BY fill a MEMORY temp table, hit the limit, "
            "and convert to on-disk InnoDB. That cliff is why your query suddenly slows down."
        ),
        version_chip="MySQL 8.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
