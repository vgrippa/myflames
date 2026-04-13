"""Lesson: Batched Key Access (BKA) join.

BKA collects a batch of outer-row join keys into the join buffer, sorts
them by rowid for disk-order access, then sends the batch to the storage
engine as a Multi-Range Read (MRR). This converts random I/O into
sequential I/O — dramatically faster on spinning disks and still
beneficial on SSDs due to read-ahead and reduced syscall overhead.
"""
from . import _html
from ._cost_model import JOIN_BUFFER_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = r"""
var JOIN_BUFFER_SIZE_DEFAULT = %d;

function bkaCost(outerRows, innerRows, rowSize, jbs, keySize) {
  var rpb = Math.max(1, Math.floor(jbs / rowSize));
  var batches = Math.max(1, Math.ceil(outerRows / rpb));
  var fanout = Math.max(2, Math.floor((16384 - 120) / (keySize + 9)));
  var height = Math.max(2, Math.ceil(Math.log(Math.max(1, innerRows)) / Math.log(fanout)));
  var randomIosWithout = outerRows * height;
  var seqIosWith = batches * (height + rpb);
  var speedup = randomIosWithout > 0 ? randomIosWithout / Math.max(1, seqIosWith) : 1;
  return {
    rpb: rpb,
    batches: batches,
    fanout: fanout,
    height: height,
    randomIosWithout: randomIosWithout,
    seqIosWith: seqIosWith,
    speedup: speedup
  };
}

var W = 800, H = 440;
var stage = null;

// ---- Sample data ----
var OUTER_ROWS = [
  {id: 1001, dept: 3, label: "order #1001 dept=3"},
  {id: 1002, dept: 7, label: "order #1002 dept=7"},
  {id: 1003, dept: 1, label: "order #1003 dept=1"},
  {id: 1004, dept: 5, label: "order #1004 dept=5"},
  {id: 1005, dept: 3, label: "order #1005 dept=3"},
  {id: 1006, dept: 9, label: "order #1006 dept=9"}
];

var DEPT_NAMES = {1: "Sales", 3: "Eng", 5: "Mktg", 7: "Ops", 9: "HR"};

function buildStage() {
  var svg = document.getElementById("bka-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // --- Zone 1: Join buffer (top, purple) ---
  var bufY = 30, bufH = 90;
  var bufBg = anim.svgEl("rect", {
    x: 20, y: bufY, width: W - 40, height: bufH, rx: 10, ry: 10,
    fill: "#f3e8ff", stroke: "#a855f7", "stroke-width": 1.5
  });
  svg.appendChild(bufBg);

  var bufTitle = anim.svgEl("text", {
    x: 32, y: bufY + 20, "font-size": 12, "font-weight": 700, fill: "#6b21a8"
  });
  bufTitle.textContent = "Join buffer — collecting outer-row keys";
  svg.appendChild(bufTitle);

  var pills = [];
  var pillW = 105, pillH = 26, pillGap = 8;
  var pillStartX = 32;
  var pillY = bufY + 36;
  for (var i = 0; i < OUTER_ROWS.length; i++) {
    var px = pillStartX + i * (pillW + pillGap);
    var pill = anim.svgEl("rect", {
      x: px, y: pillY, width: pillW, height: pillH, rx: 13, ry: 13,
      fill: "#e9d5ff", stroke: "#c084fc", "stroke-width": 1, opacity: 0
    });
    svg.appendChild(pill);
    var ptxt = anim.svgEl("text", {
      x: px + pillW / 2, y: pillY + 16, "text-anchor": "middle",
      "font-size": 9, "font-weight": 600, fill: "#581c87", opacity: 0
    });
    ptxt.textContent = "dept=" + OUTER_ROWS[i].dept + " (#" + OUTER_ROWS[i].id + ")";
    svg.appendChild(ptxt);
    pills.push({rect: pill, txt: ptxt, data: OUTER_ROWS[i]});
  }

  // --- Zone 2: Sort + MRR dispatch (middle, blue) ---
  var sortY = bufY + bufH + 20, sortH = 80;
  var sortBg = anim.svgEl("rect", {
    x: 20, y: sortY, width: W - 40, height: sortH, rx: 10, ry: 10,
    fill: "#eff6ff", stroke: "#3b82f6", "stroke-width": 1.5
  });
  svg.appendChild(sortBg);

  var sortTitle = anim.svgEl("text", {
    x: 32, y: sortY + 20, "font-size": 12, "font-weight": 700, fill: "#1e40af"
  });
  sortTitle.textContent = "Sort by rowid \u2192 MRR batch sent to inner B+tree";
  svg.appendChild(sortTitle);

  var sortedPills = [];
  var spY = sortY + 36;
  for (var s = 0; s < OUTER_ROWS.length; s++) {
    var spx = pillStartX + s * (pillW + pillGap);
    var sp = anim.svgEl("rect", {
      x: spx, y: spY, width: pillW, height: pillH, rx: 13, ry: 13,
      fill: "#dbeafe", stroke: "#60a5fa", "stroke-width": 1, opacity: 0
    });
    svg.appendChild(sp);
    var stxt = anim.svgEl("text", {
      x: spx + pillW / 2, y: spY + 16, "text-anchor": "middle",
      "font-size": 9, "font-weight": 600, fill: "#1e3a8a", opacity: 0
    });
    stxt.textContent = "";
    svg.appendChild(stxt);
    sortedPills.push({rect: sp, txt: stxt});
  }

  // Arrow from sort zone to read zone
  var arrowMid = anim.svgEl("line", {
    x1: W / 2, y1: sortY + sortH, x2: W / 2, y2: sortY + sortH + 18,
    stroke: "#3b82f6", "stroke-width": 2, "stroke-dasharray": "4,3",
    "marker-end": "", opacity: 0
  });
  svg.appendChild(arrowMid);

  // --- Zone 3: Sequential read (bottom, green) ---
  var readY = sortY + sortH + 20, readH = 100;
  var readBg = anim.svgEl("rect", {
    x: 20, y: readY, width: W - 40, height: readH, rx: 10, ry: 10,
    fill: "#ecfdf5", stroke: "#10b981", "stroke-width": 1.5
  });
  svg.appendChild(readBg);

  var readTitle = anim.svgEl("text", {
    x: 32, y: readY + 20, "font-size": 12, "font-weight": 700, fill: "#065f46"
  });
  readTitle.textContent = "Sequential disk read — matching rows in rowid order";
  svg.appendChild(readTitle);

  var readSlots = [];
  var slotW = 105, slotH = 36, slotGap = 8;
  var slotY = readY + 36;
  for (var r = 0; r < OUTER_ROWS.length; r++) {
    var rx = pillStartX + r * (slotW + slotGap);
    var slot = anim.svgEl("rect", {
      x: rx, y: slotY, width: slotW, height: slotH, rx: 8, ry: 8,
      fill: "#d1fae5", stroke: "#6ee7b7", "stroke-width": 1, opacity: 0
    });
    svg.appendChild(slot);
    var rlbl = anim.svgEl("text", {
      x: rx + slotW / 2, y: slotY + 14, "text-anchor": "middle",
      "font-size": 9, "font-weight": 600, fill: "#064e3b", opacity: 0
    });
    rlbl.textContent = "";
    svg.appendChild(rlbl);
    var rlbl2 = anim.svgEl("text", {
      x: rx + slotW / 2, y: slotY + 28, "text-anchor": "middle",
      "font-size": 8, "font-weight": 600, fill: "#047857", opacity: 0
    });
    rlbl2.textContent = "";
    svg.appendChild(rlbl2);
    readSlots.push({rect: slot, lbl: rlbl, lbl2: rlbl2});
  }

  // Sweep bar for sequential read
  var sweep = anim.svgEl("rect", {
    x: 20, y: readY, width: 4, height: readH,
    fill: "#059669", opacity: 0, rx: 2, ry: 2
  });
  svg.appendChild(sweep);

  // Status label
  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 10, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "#111827"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg,
    pills: pills,
    sortedPills: sortedPills,
    readSlots: readSlots,
    sweep: sweep,
    arrowMid: arrowMid,
    statusLbl: statusLbl,
    bufBg: bufBg,
    sortBg: sortBg,
    readBg: readBg,
    readY: readY,
    readH: readH
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.pills.length; i++) {
    stage.pills[i].rect.setAttribute("opacity", 0);
    stage.pills[i].txt.setAttribute("opacity", 0);
  }
  for (var j = 0; j < stage.sortedPills.length; j++) {
    stage.sortedPills[j].rect.setAttribute("opacity", 0);
    stage.sortedPills[j].txt.setAttribute("opacity", 0);
    stage.sortedPills[j].txt.textContent = "";
  }
  for (var k = 0; k < stage.readSlots.length; k++) {
    stage.readSlots[k].rect.setAttribute("opacity", 0);
    stage.readSlots[k].lbl.setAttribute("opacity", 0);
    stage.readSlots[k].lbl.textContent = "";
    stage.readSlots[k].lbl2.setAttribute("opacity", 0);
    stage.readSlots[k].lbl2.textContent = "";
  }
  stage.sweep.setAttribute("opacity", 0);
  stage.arrowMid.setAttribute("opacity", 0);
  stage.statusLbl.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");

  // --- Phase 1: Collect outer keys into join buffer ---
  tl.mark("Collect outer keys");
  tl.call(function() {
    phase.textContent = "Phase 1/4 \u2014 collecting outer-row keys into join buffer";
  });

  for (var i = 0; i < stage.pills.length; i++) {
    (function(idx) {
      tl.call(function() {
        stage.pills[idx].rect.setAttribute("opacity", 1);
        stage.pills[idx].txt.setAttribute("opacity", 1);
        stage.statusLbl.textContent = "Buffered key " + (idx + 1) + "/" + stage.pills.length +
          ": dept=" + OUTER_ROWS[idx].dept + " from order #" + OUTER_ROWS[idx].id;
      });
      tl.delay(280);
    })(i);
  }

  // --- Phase 2: Sort by rowid ---
  tl.mark("Sort by rowid");
  tl.call(function() {
    phase.textContent = "Phase 2/4 \u2014 sorting keys by rowid for disk-order access";
    stage.statusLbl.textContent = "Sorting " + stage.pills.length + " keys by dept rowid\u2026";
  });
  tl.delay(300);

  // Create a sorted copy by dept id
  var sorted = [];
  for (var si = 0; si < OUTER_ROWS.length; si++) sorted.push(OUTER_ROWS[si]);
  sorted.sort(function(a, b) { return a.dept - b.dept; });

  tl.call(function() {
    for (var s = 0; s < sorted.length; s++) {
      stage.sortedPills[s].rect.setAttribute("opacity", 1);
      stage.sortedPills[s].txt.setAttribute("opacity", 1);
      stage.sortedPills[s].txt.textContent = "dept=" + sorted[s].dept + " (#" + sorted[s].id + ")";
    }
    stage.statusLbl.textContent = "Keys sorted by dept rowid \u2192 ready for MRR";
  });
  tl.delay(500);

  // --- Phase 3: MRR batch to inner B+tree ---
  tl.mark("MRR batch lookup");
  tl.call(function() {
    phase.textContent = "Phase 3/4 \u2014 MRR sends sorted batch to inner B+tree index";
    stage.arrowMid.setAttribute("opacity", 0.7);
    stage.statusLbl.textContent = "Multi-Range Read: sending " + sorted.length + " keys in disk order to storage engine\u2026";
  });
  tl.delay(400);

  // --- Phase 4: Sequential read of matching rows ---
  tl.mark("Sequential read");
  tl.call(function() {
    phase.textContent = "Phase 4/4 \u2014 storage engine reads matching rows in disk order (sequential I/O)";
  });

  // Show sweep + read slots appearing one by one
  for (var ri = 0; ri < sorted.length; ri++) {
    (function(idx) {
      tl.call(function() {
        var deptId = sorted[idx].dept;
        var deptName = DEPT_NAMES[deptId] || ("dept " + deptId);
        stage.readSlots[idx].rect.setAttribute("opacity", 1);
        stage.readSlots[idx].lbl.setAttribute("opacity", 1);
        stage.readSlots[idx].lbl.textContent = deptName + " (id=" + deptId + ")";
        stage.readSlots[idx].lbl2.setAttribute("opacity", 1);
        stage.readSlots[idx].lbl2.textContent = "\u2192 order #" + sorted[idx].id;

        // Move sweep bar
        var progress = (idx + 1) / sorted.length;
        var sweepX = 20 + progress * (W - 44);
        stage.sweep.setAttribute("x", sweepX);
        stage.sweep.setAttribute("opacity", 0.8);

        stage.statusLbl.textContent = "Sequential read " + (idx + 1) + "/" + sorted.length +
          ": " + deptName + " \u2192 order #" + sorted[idx].id + " (disk-order access)";
      });
      tl.delay(350);
    })(ri);
  }

  tl.mark("Done");
  tl.call(function() {
    stage.sweep.setAttribute("opacity", 0);
    phase.textContent = "\u2713 Batch complete \u2014 all " + sorted.length + " keys resolved via sequential I/O";
    stage.statusLbl.textContent = "BKA converted " + sorted.length + " random lookups into 1 sequential MRR sweep.";
  });
  tl.delay(400);
  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}

function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function renderChart(outerRows, innerRows, keySize) {
  var fanout = Math.max(2, Math.floor((16384 - 120) / (keySize + 9)));
  var height = Math.max(2, Math.ceil(Math.log(Math.max(1, innerRows)) / Math.log(fanout)));
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e6,
    xLabel: "Outer (driving) rows", yLabel: "I/O operations",
    curves: [
      { label: "Simple NLJ: outer \u00d7 height (random)", color: "#dc2626",
        fn: function(n) { return n * height; } },
      { label: "BKA + MRR: batches \u00d7 (height + rpb) (seq)", color: "#059669",
        fn: function(n) {
          var c = teachRuntime.readControls();
          var cost = bkaCost(n, c.inner_rows, c.row_size, c.jbs, c.key_size);
          return cost.seqIosWith;
        } },
      { label: "Full scan: inner_rows", color: "#9ca3af",
        fn: function(n) { return innerRows; } }
    ],
    current: { x: outerRows },
    xSlider: "outer_rows",
    xSliderTransform: function(xVal) { return Math.max(100, Math.round(xVal / 100) * 100); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = bkaCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs, c.key_size);
  document.getElementById("out-rpb").textContent = teachRuntime.formatInt(cost.rpb);
  document.getElementById("out-batches").textContent = teachRuntime.formatInt(cost.batches);
  document.getElementById("out-height").textContent = teachRuntime.formatInt(cost.height);
  document.getElementById("out-random").textContent = teachRuntime.formatInt(cost.randomIosWithout);
  document.getElementById("out-seq").textContent = teachRuntime.formatInt(cost.seqIosWith);
  document.getElementById("out-speedup").textContent = cost.speedup.toFixed(1) + "\u00d7";
  document.getElementById("out-explanation").textContent =
    "Join buffer holds " + teachRuntime.formatInt(cost.rpb) + " outer rows per batch \u2192 " +
    teachRuntime.formatInt(cost.batches) + " batch(es). " +
    "Without BKA: " + teachRuntime.formatInt(cost.randomIosWithout) + " random I/Os " +
    "(each outer row traverses " + cost.height + " B+tree levels). " +
    "With BKA + MRR: " + teachRuntime.formatInt(cost.seqIosWith) + " sequential I/Os. " +
    "Speedup: " + cost.speedup.toFixed(1) + "\u00d7. " +
    "Raise join_buffer_size \u2192 more rows per batch \u2192 fewer batches \u2192 less overhead.";
  buildStage();
  resetStage();
  renderChart(c.outer_rows, c.inner_rows, c.key_size);
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
  <h2 id="controls-h">Parameters (Batched Key Access join)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows"><code>orders</code> rows (outer): <span class="value-pill" data-pill-for="outer_rows">10000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="500000" step="100" value="10000">
      <div class="hint">Rows from the outer (driving) table. Keys are collected into the join buffer.</div>
    </div>

    <div class="control">
      <label for="inner_rows"><code>departments</code> rows (inner): <span class="value-pill" data-pill-for="inner_rows">100000</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="100" max="5000000" step="100" value="100000">
      <div class="hint">Rows in the inner table. Looked up via B+tree index.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of one outer row in the join buffer.</div>
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="32768" max="16777216" step="8192" value="262144">
      <div class="hint">Buffer for collecting outer keys. Default 256 KiB.</div>
    </div>

    <div class="control">
      <label for="key_size">Index key size (bytes): <span class="value-pill" data-pill-for="key_size">8</span></label>
      <input type="range" id="key_size" name="key_size" min="4" max="128" step="4" value="8">
      <div class="hint">Size of the join key in the inner B+tree index. Affects fan-out and tree height.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- BKA: batch outer keys \u2192 MRR on inner\n"
            "SELECT o.*, d.name\n"
            "FROM   orders o\n"
            "JOIN   departments d ON d.id = o.dept_id\n"
            "ORDER  BY o.order_date;"
        ),
        note=(
            "BKA collects outer keys into join_buffer, sorts by rowid, "
            "then does a Multi-Range Read on the inner index \u2014 converting "
            "random I/O into sequential."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Purple zone (top): outer-row join keys flow into the join buffer one by one, forming a batch.",
            "Blue zone (middle): the batch is sorted by rowid so the storage engine can read in disk order. The sorted keys are dispatched as an MRR request.",
            "Green zone (bottom): the storage engine reads matching inner rows sequentially in rowid order \u2014 no random seeks.",
            "A sweep bar moves across the green zone to show the sequential nature of the disk reads.",
            "The readout below compares random I/Os (without BKA) vs sequential I/Os (with BKA) and shows the speedup factor.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %(query_card)s\n"
        "  %(explainer)s\n"
        "  %(toolbar)s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="bka-svg" viewBox="0 0 800 440" xmlns="http://www.w3.org/2000/svg"></svg>\n'
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
        "  <h2>BKA cost model</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Rows per batch '
        + ht("How many outer rows fit in one join_buffer_size chunk. More rows per batch = fewer batches = fewer MRR round-trips.")
        + '</p><p class="value" id="out-rpb">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Number of batches '
        + ht("ceil(outer_rows / rows_per_batch). Each batch triggers one MRR request to the storage engine.")
        + '</p><p class="value" id="out-batches">\u2014</p></div>\n'
        '    <div class="item"><p class="label">B+tree height '
        + ht("Levels in the inner index B+tree. Each batch traverses this many levels before reaching leaf pages.")
        + '</p><p class="value" id="out-height">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Random I/Os (without BKA) '
        + ht("Without BKA, each outer row does an independent random index lookup: outer_rows x height page reads.")
        + '</p><p class="value" id="out-random">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Sequential I/Os (with BKA) '
        + ht("With BKA + MRR, keys are sorted by rowid so the engine reads pages in disk order: batches x (height + rows_per_batch).")
        + '</p><p class="value" id="out-seq">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Speedup factor '
        + ht("random_ios / sequential_ios. Higher = more benefit from BKA. Biggest gains on HDD; still helps on SSD.")
        + '</p><p class="value" id="out-speedup">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">I/O operations vs outer rows (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — BKA vs simple Nested Loop, and when to enable it</summary>
  <div class="body">
    <p><strong>Simple Nested Loop</strong> does one random index lookup per outer row.
    If the inner table is large and the index is deep, each lookup may touch 3-4 random
    pages. With 100,000 outer rows that is 300,000-400,000 random I/Os — catastrophic
    on spinning disks and still expensive on SSDs.</p>

    <p><strong>BKA (Batched Key Access)</strong> changes the pattern: it collects a
    batch of outer keys into <code>join_buffer_size</code>, sorts them by the inner
    table's rowid (primary key order), and hands the sorted batch to the storage engine
    as a <em>Multi-Range Read</em> (MRR). The engine reads the matching rows in disk
    order — sequential I/O instead of random.</p>

    <p><strong>When to enable it:</strong> BKA is not on by default. You need:</p>
    <pre><code>SET optimizer_switch = 'batched_key_access=on,mrr=on,mrr_cost_based=off';</code></pre>
    <p><code>mrr_cost_based=off</code> forces MRR even when the optimizer's cost model
    thinks random I/O is cheap (which it often misjudges on HDD). On MySQL 8.4 you can
    also set these in <code>my.cnf</code> globally.</p>

    <p><strong>Why disk-order reads are faster:</strong> HDDs have ~10 ms seek time per
    random read. Sequential reads bypass seeks entirely — the head just streams. On SSDs,
    sequential reads still win because of read-ahead, fewer syscalls, and better NAND
    page utilization. A 10-50x speedup is common on HDD; 2-5x on SSD.</p>

    <p>MariaDB 11.4 also supports BKA (called BKA in join_cache_level 5-6).
    The principle is identical: batch, sort, MRR.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % JOIN_BUFFER_SIZE_DEFAULT

    return _html.render_page(
        lesson_id="bka",
        title="Batched Key Access (BKA) join \u2014 batch, sort, MRR",
        subtitle=(
            "Watch outer keys collect into a batch, get sorted by rowid, "
            "and sweep the inner index sequentially via Multi-Range Read."
        ),
        version_chip="MySQL 8.4 \u2022 MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
