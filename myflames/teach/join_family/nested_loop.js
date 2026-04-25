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

// A2 (Tier 1 migration) — spawn one match pill at the outer-row's
// right edge, fly it into the probe panel via Motion One's spring()
// instead of a hand-rolled quadratic-Bezier path tween. The spring
// gives a natural over/undershoot on arrival; no separate
// arrival-pulse is needed because the spring's settle IS the arrival
// signal.
//
// What got removed (vs the previous A2 implementation):
//   * anim.path() control-point math (~10 lines) — replaced by direct
//     translate via anim.flip.
//   * The per-frame onUpdate that called pill.setAttribute("transform"),
//     because Motion One drives the CSS transform itself.
//   * The trailing anim.arrival pulse, because spring damping handles
//     the "I just landed" feel.
//   * easeInOutQuad — superseded by physics.
//
// Pills are still capped to 5 visible at once to avoid cluttering the
// panel on wide fan-outs; surplus matches just increment the counter.
var PROBE_SLOT_H = 26;
var PROBE_MAX_SLOTS = 5;

function spawnProbePill(tl, row, orderId, slotIdx, totalThisIter) {
  // Start point (at the outer row's right edge). End point is slot
  // position inside the probe panel.
  var x0 = row.cx + 4;
  var y0 = row.cy;
  var slotClamped = Math.min(slotIdx, PROBE_MAX_SLOTS - 1);
  var x1 = stage.probePanelX + 6;
  var y1 = stage.probePanelY + slotClamped * PROBE_SLOT_H;

  tl.call(function() {
    // Build the pill at the start position. Use the SVG transform
    // attribute for the initial placement; Motion One then animates
    // the CSS transform, which composes on top.
    var pill = anim.svgEl("g", {
      transform: "translate(" + x0.toFixed(1) + "," + y0.toFixed(1) + ")"
    });
    var rect = anim.svgEl("rect", {
      x: -40, y: -11, width: 96, height: 22, rx: 4, ry: 4,
      fill: PILL_FILL, stroke: PILL_STROKE, "stroke-width": 1.2
    });
    var label = anim.svgEl("text", {
      x: 8, y: 4, "text-anchor": "middle",
      "font-size": 10.5, "font-weight": 600, fill: MATCH_TEXT
    });
    label.textContent = "order_id=" + orderId;
    pill.appendChild(rect);
    pill.appendChild(label);
    stage.probeLanding.appendChild(pill);

    // Tier 1: physics-damped flight to the landing slot.
    // damping ≈ 14 gives a small overshoot then settle — visibly more
    // "physical" than the previous easeInOutQuad path traversal.
    if (anim.flip) {
      anim.flip(pill, { x: x1 - x0, y: y1 - y0 }, {
        spring: { stiffness: 160, damping: 14, mass: 1 }
      });
    } else {
      // Tier 1 bundle missing (contributor without npm bundle built)
      // — fall back to instant placement so the lesson still works.
      pill.setAttribute("transform",
        "translate(" + x1.toFixed(1) + "," + y1.toFixed(1) + ")");
    }

    // Overflow indicator (unchanged from previous implementation).
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
