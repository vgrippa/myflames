"""Lesson: Nested Loop Join — flagship-quality rewrite.

EXPLAIN shows ``Nested loop`` whenever MySQL's classical join iterator
runs: pick one outer row, probe the inner side, emit matches, repeat.
Verified in ``sql/iterators/composite_iterators.h:318`` —
``NestedLoopIterator`` "may scan the inner iterator many times" by design.

Upgrade from the prior three-phase state-swap animation to flagship
btree vocabulary (Slice 3 / A1 + A2 + T2):

* **A1 — tween-based arrivals**: outer-row highlight transitions via
  ``anim.tween`` + ``easeOutCubic`` + ``anim.arrival`` pulse instead
  of a bare ``setAttribute`` swap.
* **A2 — arc'd probe pills**: each matching ``order_id`` spawns a
  labelled pill ``<g>`` at the outer row, arcs to the inner panel via
  ``anim.path`` with ~80 ms stagger, then lands with a pulse. The
  pedagogical point (driver → probe tuple flow) is now *visible*
  frame-to-frame.
* **T2 — match verdict pills**: each outer row's status line names
  the match count ("Acme id=1 → 2 orders ✓" / "Globex id=2 → 1 order ✓").
  The consequence line ties the observed cost back to "inner side
  indexed or not" so the takeaway lands.

Uses the shared A5 ``anim.arrival`` primitive so pulses look identical
to btree / hash / unique_lookup. Reduced-motion respected via the
helper's built-in ``reducedMotion()`` gate.
"""
from .. import _html


_LESSON_JS = r"""
function nestedLoopCost(outerRows, innerProbeRows) {
  var outer = Math.max(1, outerRows);
  var probe = Math.max(1, innerProbeRows);
  return {
    outerRows: outer,
    innerProbeRows: probe,
    comparedPairs: outer * probe
  };
}

var W = 800, H = 380;
var stage = null;

var CUSTOMERS = [
  {id: 1, name: "Acme"},
  {id: 2, name: "Globex"},
  {id: 3, name: "Initech"},
  {id: 4, name: "Umbrella"},
  {id: 5, name: "Stark"}
];

var ORDERS_BY_CUSTOMER = {
  1: [101, 104],
  2: [102],
  3: [107, 109, 110],
  4: [103],
  5: [106, 108]
};

// --- palette (unified across lessons) ---------------------------------
var OUTER_BG_REST   = "#ffedd5";   // cool orange when idle
var OUTER_BG_DRIVE  = "#fde68a";   // warmer when this row is the driver
var OUTER_STROKE    = "#fdba74";
var OUTER_STROKE_DR = "#f59e0b";
var PROBE_BG        = "#eff6ff";
var PROBE_STROKE    = "#7dd3fc";
var PILL_FILL       = "#bae6fd";
var PILL_STROKE     = "#0284c7";
var MATCH_TEXT      = "#0c4a6e";

function buildStage() {
  var svg = document.getElementById("nlj-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var leftX = 24, topY = 46, colW = 300;
  var rightX = 470, rightW = 300;
  var rowH = 48;

  var outerLbl = anim.svgEl("text", {
    x: leftX, y: 26, "font-size": 13, "font-weight": 700, fill: "#7c2d12"
  });
  outerLbl.textContent = "Outer input (driving): customers";
  svg.appendChild(outerLbl);

  var innerLbl = anim.svgEl("text", {
    x: rightX, y: 26, "font-size": 13, "font-weight": 700, fill: "#0c4a6e"
  });
  innerLbl.textContent = "Inner probe: orders matching current customer";
  svg.appendChild(innerLbl);

  // Outer-row boxes.
  var outerRows = [];
  for (var i = 0; i < CUSTOMERS.length; i++) {
    var y = topY + i * rowH;
    var bg = anim.svgEl("rect", {
      x: leftX, y: y, width: colW, height: rowH - 8, rx: 7, ry: 7,
      fill: OUTER_BG_REST, stroke: OUTER_STROKE, "stroke-width": 1.5
    });
    svg.appendChild(bg);
    var txt = anim.svgEl("text", {
      x: leftX + 12, y: y + 24, "font-size": 11, "font-weight": 600,
      fill: "#7c2d12"
    });
    txt.textContent = "customer_id=" + CUSTOMERS[i].id + " " + CUSTOMERS[i].name;
    svg.appendChild(txt);
    outerRows.push({
      bg: bg, txt: txt, data: CUSTOMERS[i],
      y: y, cx: leftX + colW, cy: y + (rowH - 8) / 2
    });
  }

  // Inner panel (bigger so match-pills have room to land).
  var innerPanel = anim.svgEl("rect", {
    x: rightX, y: topY, width: rightW, height: 240, rx: 8, ry: 8,
    fill: PROBE_BG, stroke: PROBE_STROKE, "stroke-width": 1.5
  });
  svg.appendChild(innerPanel);

  var probeTitle = anim.svgEl("text", {
    x: rightX + 12, y: topY + 22, "font-size": 11.5, "font-weight": 700,
    fill: "#0c4a6e"
  });
  probeTitle.textContent = "Probe panel";
  svg.appendChild(probeTitle);

  // Container <g> where match-pills land. Cleared per iteration.
  var probeLanding = anim.svgEl("g", {id: "probe-landing"});
  svg.appendChild(probeLanding);

  // Per-outer-row verdict line (bottom of the panel).
  var verdict = anim.svgEl("text", {
    x: rightX + 12, y: topY + 220, "font-size": 12, "font-weight": 700,
    fill: MATCH_TEXT
  });
  verdict.textContent = "";
  svg.appendChild(verdict);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 14, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "#111827"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg,
    outerRows: outerRows,
    probeLanding: probeLanding,
    probePanelX: rightX + 16,
    probePanelY: topY + 48,
    probePanelRight: rightX + rightW - 16,
    verdict: verdict,
    statusLbl: statusLbl
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.outerRows.length; i++) {
    stage.outerRows[i].bg.setAttribute("fill", OUTER_BG_REST);
    stage.outerRows[i].bg.setAttribute("stroke", OUTER_STROKE);
  }
  while (stage.probeLanding.firstChild) {
    stage.probeLanding.removeChild(stage.probeLanding.firstChild);
  }
  stage.verdict.textContent = "";
  stage.statusLbl.textContent = "";
}

// A1 — tweened "this is the current driver" transition, with arrival pulse.
function driveRow(tl, row) {
  tl.add({
    from: 0, to: 1, duration: 240, ease: anim.easeOutCubic,
    onUpdate: function(t) {
      var fill = anim.lerpColor(OUTER_BG_REST, OUTER_BG_DRIVE, t);
      var stroke = anim.lerpColor(OUTER_STROKE, OUTER_STROKE_DR, t);
      row.bg.setAttribute("fill", fill);
      row.bg.setAttribute("stroke", stroke);
    },
    onComplete: function() { anim.arrival(row.bg); }
  });
}

function releaseRow(tl, row) {
  tl.add({
    from: 0, to: 1, duration: 200, ease: anim.easeInCubic,
    onUpdate: function(t) {
      var fill = anim.lerpColor(OUTER_BG_DRIVE, OUTER_BG_REST, t);
      var stroke = anim.lerpColor(OUTER_STROKE_DR, OUTER_STROKE, t);
      row.bg.setAttribute("fill", fill);
      row.bg.setAttribute("stroke", stroke);
    }
  });
}

// A2 — spawn one match pill at the outer-row's right edge, arc it into
// the probe panel via anim.path(), then pulse on arrival. Pills are
// capped to 5 visible at once to avoid cluttering the panel on wide
// fan-outs; surplus matches just increment the counter.
var PROBE_SLOT_H = 26;
var PROBE_MAX_SLOTS = 5;

function spawnProbePill(tl, row, orderId, slotIdx, totalThisIter) {
  var cleared = false;
  var pill = null;
  var label = null;

  // Start point (at the outer row's right edge). End point is slot
  // position inside the probe panel.
  var x0 = row.cx + 4;
  var y0 = row.cy;
  var slotClamped = Math.min(slotIdx, PROBE_MAX_SLOTS - 1);
  var x1 = stage.probePanelX + 6;
  var y1 = stage.probePanelY + slotClamped * PROBE_SLOT_H;
  // Mid-control-point above the line so the arc has a clear curve.
  var cx = (x0 + x1) / 2;
  var cy = Math.min(y0, y1) - 40 - slotIdx * 4;

  var pathFn = anim.path(x0, y0, cx, cy, x1, y1);

  tl.add({
    from: 0, to: 1, duration: 360, ease: anim.easeInOutQuad,
    onUpdate: function(t) {
      if (!cleared) {
        cleared = true;
        // Build the pill on first frame so it doesn't flash before moving.
        pill = anim.svgEl("g", {});
        var rect = anim.svgEl("rect", {
          x: -40, y: -11, width: 96, height: 22, rx: 4, ry: 4,
          fill: PILL_FILL, stroke: PILL_STROKE, "stroke-width": 1.2
        });
        label = anim.svgEl("text", {
          x: 8, y: 4, "text-anchor": "middle",
          "font-size": 10.5, "font-weight": 600, fill: MATCH_TEXT
        });
        label.textContent = "order_id=" + orderId;
        pill.appendChild(rect);
        pill.appendChild(label);
        stage.probeLanding.appendChild(pill);
      }
      var pt = pathFn(t);
      pill.setAttribute("transform",
        "translate(" + pt.x.toFixed(1) + "," + pt.y.toFixed(1) + ")");
    },
    onComplete: function() {
      if (pill) {
        // A5 arrival pulse on the pill's rect.
        anim.arrival(pill.firstChild, {peakWidth: 2.2, durationMs: 260});
      }
      // If more matches than visible slots, collapse surplus into a
      // numeric counter in the last slot.
      if (slotIdx >= PROBE_MAX_SLOTS && slotIdx === totalThisIter - 1) {
        var overflow = anim.svgEl("text", {
          x: stage.probePanelX + 120,
          y: stage.probePanelY + (PROBE_MAX_SLOTS - 1) * PROBE_SLOT_H + 4,
          "font-size": 11, "font-weight": 700, fill: MATCH_TEXT
        });
        overflow.textContent =
          "… +" + (totalThisIter - PROBE_MAX_SLOTS) + " more";
        stage.probeLanding.appendChild(overflow);
      }
    }
  });
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var compared = 0;
  var totalMatches = 0;

  tl.mark("Drive outer rows");
  tl.call(function() {
    phase.textContent =
      "Phase 1/2 — the join picks one driving (outer) customer at a time";
  });

  for (var i = 0; i < stage.outerRows.length; i++) {
    (function(idx) {
      var row = stage.outerRows[idx];
      var c = row.data;
      var orderIds = ORDERS_BY_CUSTOMER[c.id] || [];

      // Release any previously-driving row first (skip on i=0).
      if (idx > 0) releaseRow(tl, stage.outerRows[idx - 1]);

      // Clear probe-panel contents for the new iteration.
      tl.call(function() {
        while (stage.probeLanding.firstChild) {
          stage.probeLanding.removeChild(stage.probeLanding.firstChild);
        }
        stage.verdict.textContent = "";
        phase.textContent =
          "Phase 2/2 — probing orders for " + c.name + " (id=" + c.id + ")";
      });

      // Tween this row to the driver state.
      driveRow(tl, row);

      // Stagger arc'd match pills outer→inner, ~80 ms apart.
      for (var j = 0; j < orderIds.length; j++) {
        spawnProbePill(tl, row, orderIds[j], j, orderIds.length);
        tl.delay(80);
      }

      compared += Math.max(1, orderIds.length);
      totalMatches += orderIds.length;

      tl.call(function() {
        var n = orderIds.length;
        var verb = n === 0 ? "→ no matching orders" :
                   n === 1 ? "→ 1 order ✓" : "→ " + n + " orders ✓";
        stage.verdict.textContent = c.name + " (id=" + c.id + ") " + verb;
        stage.statusLbl.textContent =
          "Driver " + (idx + 1) + "/" + stage.outerRows.length +
          " — " + n + " inner matches (running total: " + compared + ")";
      });

      // Deliberate pause between iterations so the eye can track the
      // tuple flow (teaching skill: "act boundaries need a stage").
      tl.delay(900);
    })(i);
  }

  tl.mark("Consequence");
  tl.call(function() {
    phase.textContent =
      "Nested loop cost = outer_rows × inner_rows_per_probe";
    stage.statusLbl.textContent =
      "Inner side had ~" + (totalMatches / stage.outerRows.length).toFixed(1)
      + " matches per driver. Indexed inner probe = small; full scan = explodes.";
  });
  tl.delay(600);
  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}

function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

function renderChart(innerProbeRows, currentOuterRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Outer (driving) rows", yLabel: "Row-pair comparisons",
    curves: [
      { label: "Nested loop: O(n · m)", color: "#b45309",
        fn: function(n) { return n * innerProbeRows; } },
      { label: "Linear baseline: O(n)", color: "#0284c7",
        fn: function(n) { return n; } }
    ],
    current: { x: currentOuterRows },
    xSlider: "outer_rows",
    xSliderTransform: function(xVal) {
      return Math.max(1000, Math.round(xVal / 1000) * 1000);
    }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = nestedLoopCost(c.outer_rows, c.inner_rows);
  document.getElementById("out-outer").textContent =
    teachRuntime.formatInt(cost.outerRows);
  document.getElementById("out-inner").textContent =
    teachRuntime.formatInt(cost.innerProbeRows);
  document.getElementById("out-cmp").textContent =
    teachRuntime.formatInt(cost.comparedPairs);
  document.getElementById("out-explanation").textContent =
    "Nested loop probes the inner side for every outer row. With " +
    teachRuntime.formatInt(cost.outerRows) + " outer rows and ~" +
    teachRuntime.formatInt(cost.innerProbeRows) +
    " inner rows per probe, that is about " +
    teachRuntime.formatInt(cost.comparedPairs) +
    " row-pair comparisons. If the inner side is an indexed lookup, "
    + "`inner_rows_per_probe` stays at 1 and this operator is fast; "
    + "without a usable index the inner side becomes a full scan per "
    + "outer row and cost grows as n·m.";
  buildStage();
  resetStage();
  renderChart(cost.innerProbeRows, cost.outerRows);
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
<section class="controls">
  <h2>Join shape controls</h2>
  <div class="control-grid">
    <div class="control">
      <label for="outer_rows">Outer rows (customers): <span class="value-pill" data-pill-for="outer_rows">50000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="1000" max="5000000" step="1000" value="50000">
      <div class="hint">The driving side: each of these rows triggers an inner probe.</div>
    </div>
    <div class="control">
      <label for="inner_rows">Inner rows per probe (orders): <span class="value-pill" data-pill-for="inner_rows">8</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="1" max="200" step="1" value="8">
      <div class="hint">Average orders rows checked for each customer row.</div>
    </div>
  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "SELECT c.customer_id, o.order_id\n"
            "FROM   customers c\n"
            "JOIN   orders o ON o.customer_id = c.customer_id\n"
            "WHERE  c.country = 'US';"
        ),
        note=(
            "This lesson isolates the Nested loop operator: one outer "
            "row becomes the driver, match pills arc from the outer row "
            "into the probe panel, then the next driver takes over. "
            "It's exactly what EXPLAIN's Nested loop node does at runtime."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left side = outer (driving) customers. Orange = currently "
            "driving the join.",
            "For each driver, <strong>match pills</strong> spawn at the "
            "outer row and arc into the probe panel — one per matching "
            "inner row. That arc IS the tuple flow — the point of the "
            "operator.",
            "Bottom-right verdict names the driver and its match count "
            "('Acme id=1 → 2 orders ✓').",
            "Cost scales with outer_rows × inner_rows_per_probe. "
            "Indexed inner = tiny per-probe cost; un-indexed inner = "
            "full-scan per driver and cost explodes.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="nlj-svg" viewBox="0 0 800 380" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Nested loop cost model</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Outer rows {ht("Rows from the driving side of the join. The nested loop runs once per outer row.")}</p><p class="value" id="out-outer">—</p></div>
    <div class="item"><p class="label">Inner rows per probe {ht("Average rows checked on the inner side for each outer row.")}</p><p class="value" id="out-inner">—</p></div>
    <div class="item"><p class="label">Row-pair comparisons {ht("Approximate work for this operator: outer_rows × inner_rows_per_probe.")}</p><p class="value" id="out-cmp">—</p></div>
    <div class="item"><p class="label">Complexity {ht("Nested loop cost grows multiplicatively with both inputs.")}</p><p class="value">O(n · m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Nested loop growth curve (log-log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when Nested loop is fast and when it isn't</summary>
  <div class="body">
    <p>The iterator itself is simple — one outer row, one inner probe,
    repeat. The difference between fast and catastrophic is <em>what
    inner_rows_per_probe looks like</em>:</p>

    <ul>
      <li><strong>Indexed inner side</strong> (eq_ref / ref access):
      inner_rows_per_probe ≈ 1. Total work is ~linear in outer rows —
      this is the fast path and exactly what "add an index on the join
      column" achieves.</li>

      <li><strong>Un-indexed inner side</strong> (type=ALL): the inner
      side is <em>re-scanned</em> per outer row, so
      inner_rows_per_probe = total inner rows. Work grows as
      outer × inner — the curve on the chart above is the pain.</li>
    </ul>

    <p>MySQL 8.0.20+ rewrites this at <em>execution time</em> into a
    hash join when no usable index exists (see
    <code>sql/sql_executor.cc:~2891</code>
    <code>replace_with_hash_join</code>). Which is why you'll see
    plans labelled "Nested loop" that are actually running as hash
    join under the hood — the EXPLAIN string is descriptive of the
    optimizer's decision, not always the executor's behaviour.</p>

    <p>Source: <code>sql/iterators/composite_iterators.h:318</code>
    — <code>NestedLoopIterator</code>. "Currently the only form of
    join we have" (at the logical level).</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="nested_loop",
        title="Nested Loop Join — outer row drives inner probe",
        subtitle=(
            "Dedicated operator view for EXPLAIN's Nested loop nodes. "
            "Watch each driver fire match pills into the probe panel — "
            "the flow that makes the operator's cost visible."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
    )
