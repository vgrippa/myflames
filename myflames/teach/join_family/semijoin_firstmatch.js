function firstMatchCost(outerRows, innerMatches) {
  var outer = Math.max(1, outerRows);
  var matches = Math.max(1, innerMatches);
  // Naive inner join reads every matching inner row for every outer row.
  var naiveInnerReads = outer * matches;
  // FirstMatch stops on the first match: ~1 inner read per outer row.
  var firstMatchInnerReads = outer * 1;
  return {
    outerRows: outer,
    innerMatches: matches,
    naiveInnerReads: naiveInnerReads,
    firstMatchInnerReads: firstMatchInnerReads,
    innerReadsSaved: naiveInnerReads - firstMatchInnerReads
  };
}

var W = 800, H = 420;
var stage = null;

// Sample data: customers (outer) and, for each, their orders over $1000
// (the inner rows the IN subquery scans). Alice/Initech have many; that
// fan-out is where FirstMatch pays off.
var CUSTOMERS = [
  {id: 1, name: "Alice"},
  {id: 2, name: "Bob"},
  {id: 3, name: "Carol"},
  {id: 4, name: "Dave"},
  {id: 5, name: "Eve"}
];

// order_id + total for each customer's qualifying orders (total > 1000).
var ORDERS_BY_CUSTOMER = {
  1: [{oid: 104, total: 1500}, {oid: 118, total: 2200}, {oid: 131, total: 3100}],
  2: [{oid: 102, total: 1200}],
  3: [{oid: 107, total: 1100}, {oid: 109, total: 4800}, {oid: 121, total: 1900}, {oid: 140, total: 2500}],
  4: [],
  5: [{oid: 106, total: 5000}, {oid: 133, total: 1300}]
};

// --- palette (unified with nested_loop / hash lessons) ----------------
var OUTER_BG_REST   = "#ffedd5";
var OUTER_BG_DRIVE  = "#fde68a";
var OUTER_BG_DONE   = "#dcfce7";   // emitted (matched)
var OUTER_BG_NONE   = "#f3f4f6";   // checked, no match
var OUTER_STROKE    = "#fdba74";
var OUTER_STROKE_DR = "#f59e0b";
var OUTER_STROKE_OK = "#16a34a";
var PROBE_BG        = "#eff6ff";
var PROBE_STROKE    = "#7dd3fc";
var PILL_FILL       = "#bae6fd";
var PILL_STROKE     = "#0284c7";
var PILL_MATCH_FILL = "#bbf7d0";
var PILL_MATCH_STK  = "#16a34a";
var PILL_SKIP_FILL  = "#e5e7eb";   // greyed: never read
var PILL_SKIP_STK   = "#9ca3af";
var MATCH_TEXT      = "#0c4a6e";
var SKIP_TEXT       = "#9ca3af";

function buildStage() {
  var svg = document.getElementById("fm-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var leftX = 24, topY = 52, colW = 300;
  var rightX = 470, rightW = 300;
  var rowH = 46;

  var outerLbl = anim.svgEl("text", {
    x: leftX, y: 26, "font-size": 13, "font-weight": 700, fill: "#7c2d12"
  });
  outerLbl.textContent = "Outer (driving): customers checked by IN (…)";
  svg.appendChild(outerLbl);

  var innerLbl = anim.svgEl("text", {
    x: rightX, y: 26, "font-size": 13, "font-weight": 700, fill: "#0c4a6e"
  });
  innerLbl.textContent = "Inner scan: this customer's orders > $1000";
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
      x: leftX + 12, y: y + 23, "font-size": 11, "font-weight": 600,
      fill: "#7c2d12"
    });
    txt.textContent = "customer_id=" + CUSTOMERS[i].id + " " + CUSTOMERS[i].name;
    svg.appendChild(txt);
    // Per-row verdict badge (right edge of the outer box).
    var badge = anim.svgEl("text", {
      x: leftX + colW - 10, y: y + 23, "text-anchor": "end",
      "font-size": 10.5, "font-weight": 700, fill: "#065f46", opacity: 0
    });
    svg.appendChild(badge);
    outerRows.push({
      bg: bg, txt: txt, badge: badge, data: CUSTOMERS[i],
      y: y, cx: leftX + colW, cy: y + (rowH - 8) / 2
    });
  }

  // Inner probe panel.
  var innerPanel = anim.svgEl("rect", {
    x: rightX, y: topY, width: rightW, height: 210, rx: 8, ry: 8,
    fill: PROBE_BG, stroke: PROBE_STROKE, "stroke-width": 1.5
  });
  svg.appendChild(innerPanel);

  var probeTitle = anim.svgEl("text", {
    x: rightX + 12, y: topY + 22, "font-size": 11.5, "font-weight": 700,
    fill: "#0c4a6e"
  });
  probeTitle.textContent = "Inner rows read for the current customer";
  svg.appendChild(probeTitle);

  var probeLanding = anim.svgEl("g", {id: "probe-landing"});
  svg.appendChild(probeLanding);

  var verdict = anim.svgEl("text", {
    x: rightX + 12, y: topY + 192, "font-size": 12, "font-weight": 700,
    fill: MATCH_TEXT
  });
  verdict.textContent = "";
  svg.appendChild(verdict);

  // Two diverging counters (the story).
  var counterY = topY + 232;
  var naiveCounter = anim.svgEl("text", {
    x: rightX, y: counterY, "font-size": 12, "font-weight": 700, fill: "#b91c1c"
  });
  naiveCounter.textContent = "Naive join inner rows read: 0";
  svg.appendChild(naiveCounter);

  var fmCounter = anim.svgEl("text", {
    x: rightX, y: counterY + 20, "font-size": 12, "font-weight": 700, fill: "#15803d"
  });
  fmCounter.textContent = "FirstMatch inner rows read: 0";
  svg.appendChild(fmCounter);

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
    probePanelY: topY + 44,
    verdict: verdict,
    naiveCounter: naiveCounter,
    fmCounter: fmCounter,
    statusLbl: statusLbl
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.outerRows.length; i++) {
    var r = stage.outerRows[i];
    r.bg.setAttribute("fill", OUTER_BG_REST);
    r.bg.setAttribute("stroke", OUTER_STROKE);
    r.badge.setAttribute("opacity", 0);
    r.badge.textContent = "";
  }
  while (stage.probeLanding.firstChild) {
    stage.probeLanding.removeChild(stage.probeLanding.firstChild);
  }
  stage.verdict.textContent = "";
  stage.naiveCounter.textContent = "Naive join inner rows read: 0";
  stage.fmCounter.textContent = "FirstMatch inner rows read: 0";
  stage.statusLbl.textContent = "";
}

// Tweened "this is the current driver" transition + arrival pulse.
function driveRow(tl, row) {
  tl.add({
    from: 0, to: 1, duration: 240, ease: anim.easeOutCubic,
    onUpdate: function(t) {
      row.bg.setAttribute("fill", anim.lerpColor(OUTER_BG_REST, OUTER_BG_DRIVE, t));
      row.bg.setAttribute("stroke", anim.lerpColor(OUTER_STROKE, OUTER_STROKE_DR, t));
    },
    onComplete: function() { anim.arrival(row.bg); }
  });
}

function settleRow(tl, row, matched) {
  var toFill = matched ? OUTER_BG_DONE : OUTER_BG_NONE;
  var toStroke = matched ? OUTER_STROKE_OK : OUTER_STROKE;
  tl.add({
    from: 0, to: 1, duration: 220, ease: anim.easeInCubic,
    onUpdate: function(t) {
      row.bg.setAttribute("fill", anim.lerpColor(OUTER_BG_DRIVE, toFill, t));
      row.bg.setAttribute("stroke", anim.lerpColor(OUTER_STROKE_DR, toStroke, t));
    }
  });
}

var PROBE_SLOT_H = 30;
var PROBE_MAX_SLOTS = 4;

// Spawn one inner-row pill and arc it into the probe panel.
//   read  = true  -> the pill is actually read (blue, or green if it is
//                    the matching row that triggers the early-out).
//   read  = false -> FirstMatch skips it: greyed-out, arrives faded, no
//                    arrival pulse. Communicates "never read".
function spawnInnerPill(tl, row, order, slotIdx, opts) {
  opts = opts || {};
  var read = opts.read !== false;
  var isMatch = !!opts.isMatch;
  var built = false;
  var pill = null;
  var rect = null;
  var label = null;

  var x0 = row.cx + 4;
  var y0 = row.cy;
  var slotClamped = Math.min(slotIdx, PROBE_MAX_SLOTS - 1);
  var x1 = stage.probePanelX + 6;
  var y1 = stage.probePanelY + slotClamped * PROBE_SLOT_H;
  var cx = (x0 + x1) / 2;
  var cy = Math.min(y0, y1) - 34 - slotIdx * 4;
  var pathFn = anim.path(x0, y0, cx, cy, x1, y1);

  var fill = read ? (isMatch ? PILL_MATCH_FILL : PILL_FILL) : PILL_SKIP_FILL;
  var stroke = read ? (isMatch ? PILL_MATCH_STK : PILL_STROKE) : PILL_SKIP_STK;
  var txtColor = read ? MATCH_TEXT : SKIP_TEXT;
  var endOpacity = read ? 1 : 0.55;

  tl.add({
    from: 0, to: 1, duration: read ? 380 : 300, ease: anim.easeInOutQuad,
    onUpdate: function(t) {
      if (!built) {
        built = true;
        pill = anim.svgEl("g", {opacity: read ? 1 : 0.55});
        rect = anim.svgEl("rect", {
          x: -46, y: -12, width: 172, height: 24, rx: 4, ry: 4,
          fill: fill, stroke: stroke, "stroke-width": 1.2,
          "stroke-dasharray": read ? "0" : "4 3"
        });
        label = anim.svgEl("text", {
          x: 40, y: 4, "text-anchor": "middle",
          "font-size": 10, "font-weight": 600, fill: txtColor
        });
        var tag = isMatch ? " ✓ match" : (read ? "" : " — skipped");
        label.textContent =
          "order #" + order.oid + " $" + order.total + tag;
        pill.appendChild(rect);
        pill.appendChild(label);
        stage.probeLanding.appendChild(pill);
      }
      var pt = pathFn(t);
      pill.setAttribute("transform",
        "translate(" + pt.x.toFixed(1) + "," + pt.y.toFixed(1) + ")");
      if (!read) pill.setAttribute("opacity", (0.55 * t).toFixed(2));
    },
    onComplete: function() {
      if (pill) pill.setAttribute("opacity", endOpacity);
      if (rect && read) {
        anim.arrival(rect, {peakWidth: isMatch ? 2.8 : 2.0, durationMs: 260});
      }
    }
  });
}

function clearProbe(tl, headline) {
  tl.call(function() {
    while (stage.probeLanding.firstChild) {
      stage.probeLanding.removeChild(stage.probeLanding.firstChild);
    }
    stage.verdict.textContent = "";
    if (headline) document.getElementById("phase-label").textContent = headline;
  });
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var naiveRead = 0;
  var fmRead = 0;

  function setCounters() {
    stage.naiveCounter.textContent = "Naive join inner rows read: " + naiveRead;
    stage.fmCounter.textContent = "FirstMatch inner rows read: " + fmRead;
  }

  // ================= ACT 1 — naive inner join =================
  tl.mark("Act 1 — naive join (reads every match)");
  tl.call(function() {
    phase.textContent =
      "Act 1/2 — a plain inner join reads EVERY matching order per customer (wasted work for IN)";
  });
  tl.delay(500);

  for (var i = 0; i < stage.outerRows.length; i++) {
    (function(idx) {
      var row = stage.outerRows[idx];
      var c = row.data;
      var orders = ORDERS_BY_CUSTOMER[c.id] || [];

      clearProbe(tl,
        "Act 1 — checking " + c.name + " (id=" + c.id + "): reading ALL matching orders");
      driveRow(tl, row);

      for (var j = 0; j < orders.length; j++) {
        (function(jj) {
          spawnInnerPill(tl, row, orders[jj], jj, {read: true, isMatch: false});
          tl.call(function() {
            naiveRead += 1;
            setCounters();
            phase.textContent = "Act 1 — " + c.name + " (id=" + c.id +
              ") reads order #" + orders[jj].oid + " $" + orders[jj].total +
              " (" + (jj + 1) + "/" + orders.length + ")";
          });
          tl.delay(90);
        })(j);
      }

      tl.call(function() {
        var n = orders.length;
        if (n === 0) {
          stage.verdict.textContent = c.name + " (id=" + c.id + ") → no orders > $1000";
          row.badge.textContent = "no match";
          row.badge.setAttribute("fill", "#6b7280");
        } else {
          stage.verdict.textContent = c.name + " (id=" + c.id + ") → read all " +
            n + (n === 1 ? " order" : " orders");
          row.badge.textContent = "read " + n;
          row.badge.setAttribute("fill", "#b91c1c");
        }
        row.badge.setAttribute("opacity", 1);
      });
      settleRow(tl, row, orders.length > 0);
      tl.delay(650);
    })(i);
  }

  tl.call(function() {
    phase.textContent =
      "Act 1 done — naive join read " + naiveRead +
      " inner rows. But IN only needs to know a match EXISTS. Watch FirstMatch…";
    stage.statusLbl.textContent =
      "Every order past the first was wasted work for the IN check.";
  });
  tl.delay(1100);

  // ================= ACT 2 — FirstMatch early-out =================
  tl.mark("Act 2 — FirstMatch (stops on first)");
  tl.call(function() {
    // Reset outer rows to rest so the second pass reads clean.
    for (var k = 0; k < stage.outerRows.length; k++) {
      var rr = stage.outerRows[k];
      rr.bg.setAttribute("fill", OUTER_BG_REST);
      rr.bg.setAttribute("stroke", OUTER_STROKE);
      rr.badge.setAttribute("opacity", 0);
    }
    phase.textContent =
      "Act 2/2 — same query as a semijoin: stop the inner scan on the FIRST match";
  });
  tl.delay(500);

  for (var m = 0; m < stage.outerRows.length; m++) {
    (function(idx) {
      var row = stage.outerRows[idx];
      var c = row.data;
      var orders = ORDERS_BY_CUSTOMER[c.id] || [];

      clearProbe(tl,
        "Act 2 — checking " + c.name + " (id=" + c.id + "): scan until the first match");
      driveRow(tl, row);

      if (orders.length === 0) {
        // No match: the inner side is scanned but finds nothing.
        tl.call(function() {
          fmRead += 1;               // one probe to discover "no rows"
          setCounters();
          stage.verdict.textContent = c.name + " (id=" + c.id + ") → no match — not in result";
          row.badge.textContent = "no match";
          row.badge.setAttribute("fill", "#6b7280");
          row.badge.setAttribute("opacity", 1);
          phase.textContent = "Act 2 — " + c.name + " (id=" + c.id +
            ") has no order > $1000 → excluded from the IN result";
        });
        settleRow(tl, row, false);
        tl.delay(650);
        return;
      }

      // First order: read it, it matches -> emit + STOP.
      spawnInnerPill(tl, row, orders[0], 0, {read: true, isMatch: true});
      tl.call(function() {
        fmRead += 1;
        setCounters();
        phase.textContent = "Act 2 — " + c.name + " (id=" + c.id +
          ") → order #" + orders[0].oid + " $" + orders[0].total +
          " matches → emit " + c.name + ", STOP scanning";
        stage.verdict.textContent = c.name + " (id=" + c.id +
          ") → first match on order #" + orders[0].oid + " — emit, short-circuit";
        row.badge.textContent = "✓ emit (read 1)";
        row.badge.setAttribute("fill", "#15803d");
        row.badge.setAttribute("opacity", 1);
      });
      tl.delay(140);

      // Remaining orders: greyed, never read.
      for (var j = 1; j < orders.length; j++) {
        (function(jj) {
          spawnInnerPill(tl, row, orders[jj], jj, {read: false, isMatch: false});
          tl.delay(55);
        })(j);
      }
      tl.call(function() {
        if (orders.length > 1) {
          phase.textContent = "Act 2 — " + c.name + "'s other " +
            (orders.length - 1) + " matching order" +
            (orders.length - 1 === 1 ? "" : "s") +
            " are SKIPPED (greyed) — MySQL never reads them";
        }
      });
      settleRow(tl, row, true);
      tl.delay(650);
    })(m);
  }

  tl.mark("Consequence");
  tl.call(function() {
    phase.textContent =
      "FirstMatch read " + fmRead + " inner rows vs " + naiveRead +
      " for the naive join — IN asks does-it-exist, so the inner scan short-circuits";
    stage.statusLbl.textContent =
      "Saved " + (naiveRead - fmRead) + " inner-row reads on 5 customers. " +
      "At scale the gap is outer_rows × (matches − 1).";
  });
  tl.delay(700);
  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}

function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

function renderChart(innerMatches, currentOuterRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 1000, xMax: 5e6,
    xLabel: "Outer (customer) rows", yLabel: "Inner rows read",
    curves: [
      { label: "Naive join: O(n · matches)", color: "#b91c1c",
        fn: function(n) { return n * innerMatches; } },
      { label: "FirstMatch: O(n · 1)", color: "#15803d",
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
  var cost = firstMatchCost(c.outer_rows, c.inner_matches);
  document.getElementById("out-outer").textContent =
    teachRuntime.formatInt(cost.outerRows);
  document.getElementById("out-inner").textContent =
    teachRuntime.formatInt(cost.innerMatches);
  document.getElementById("out-naive").textContent =
    teachRuntime.formatInt(cost.naiveInnerReads);
  document.getElementById("out-firstmatch").textContent =
    teachRuntime.formatInt(cost.firstMatchInnerReads);
  document.getElementById("out-saved").textContent =
    teachRuntime.formatInt(cost.innerReadsSaved);
  document.getElementById("out-explanation").textContent =
    "A plain inner join would read every matching order for each customer: " +
    teachRuntime.formatInt(cost.outerRows) + " customers × " +
    teachRuntime.formatInt(cost.innerMatches) + " matches = " +
    teachRuntime.formatInt(cost.naiveInnerReads) + " inner rows read. " +
    "But IN (subquery) only asks whether a match EXISTS, not how many. " +
    "FirstMatch stops the inner scan on the first match for each customer, " +
    "so it reads about " + teachRuntime.formatInt(cost.firstMatchInnerReads) +
    " inner rows — saving " +
    teachRuntime.formatInt(cost.innerReadsSaved) +
    " reads. The saving is outer_rows × (matches − 1): the more " +
    "orders each customer has, the more FirstMatch skips.";
  buildStage();
  resetStage();
  renderChart(cost.innerMatches, cost.outerRows);
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
