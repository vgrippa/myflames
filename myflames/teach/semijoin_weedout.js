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
    x: 20, y: 24, "font-size": 12, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  joinLbl.textContent = "Inner join result (customers \u00d7 orders) \u2014 contains duplicates";
  svg.appendChild(joinLbl);

  var joinRect = anim.svgEl("rect", {
    x: 20, y: 34, width: W - 40, height: 90, rx: 8, ry: 8,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token)", "stroke-width": 1.5
  });
  svg.appendChild(joinRect);

  // Pre-render join pair labels
  var pairLabels = [];
  for (var p = 0; p < JOIN_PAIRS.length; p++) {
    var px = 50 + (p % 4) * 185;
    var py = 55 + Math.floor(p / 4) * 30;
    var pl = anim.svgEl("text", {
      x: px, y: py, "font-size": 10, "font-weight": 600, fill: "var(--svg-token-ink)", opacity: 0
    });
    pl.textContent = JOIN_PAIRS[p].cust.name + " \u00d7 " + JOIN_PAIRS[p].order.desc;
    svg.appendChild(pl);
    pl.cx = px + 50;
    pl.cy = py - 4;
    pairLabels.push(pl);
  }

  // ---- Zone 2: Temp table (middle, yellow) ----
  var tempY = 155;
  var tempLbl = anim.svgEl("text", {
    x: 20, y: tempY, "font-size": 12, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  tempLbl.textContent = "Inserts into the weedout temp table (unique key = customer rowid)";
  svg.appendChild(tempLbl);

  var tempRect = anim.svgEl("rect", {
    x: 20, y: tempY + 10, width: W - 40, height: 110, rx: 8, ry: 8,
    fill: "var(--svg-build-soft)", stroke: "var(--svg-build)", "stroke-width": 1.5
  });
  svg.appendChild(tempRect);

  // Column headers for temp table
  var colHdrRowid = anim.svgEl("text", {
    x: 60, y: tempY + 30, "font-size": 10, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  colHdrRowid.textContent = "rowid";
  svg.appendChild(colHdrRowid);
  var colHdrName = anim.svgEl("text", {
    x: 140, y: tempY + 30, "font-size": 10, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  colHdrName.textContent = "customer";
  svg.appendChild(colHdrName);
  var colHdrResult = anim.svgEl("text", {
    x: 280, y: tempY + 30, "font-size": 10, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  colHdrResult.textContent = "insert result";
  svg.appendChild(colHdrResult);

  // Slots for temp table inserts (up to 7 visible rows)
  var tempSlots = [];
  for (var s = 0; s < 7; s++) {
    var sy = tempY + 42 + s * 11;
    var sRowid = anim.svgEl("text", {
      x: 60, y: sy, "font-size": 9, "font-weight": 600, fill: "var(--svg-build-ink)", opacity: 0
    });
    svg.appendChild(sRowid);
    var sName = anim.svgEl("text", {
      x: 140, y: sy, "font-size": 9, "font-weight": 600, fill: "var(--svg-build-ink)", opacity: 0
    });
    svg.appendChild(sName);
    var sResult = anim.svgEl("text", {
      x: 280, y: sy, "font-size": 9, "font-weight": 700, fill: "var(--svg-build-ink)", opacity: 0
    });
    svg.appendChild(sResult);
    tempSlots.push({ rowid: sRowid, name: sName, result: sResult, cx: 92, cy: sy - 3 });
  }

  // Duplicate counter on the right side of temp table
  var dupCounterLbl = anim.svgEl("text", {
    x: W - 60, y: tempY + 50, "text-anchor": "middle",
    "font-size": 11, "font-weight": 700, fill: "var(--svg-danger)"
  });
  dupCounterLbl.textContent = "Duplicates: 0";
  svg.appendChild(dupCounterLbl);

  var newCounterLbl = anim.svgEl("text", {
    x: W - 60, y: tempY + 70, "text-anchor": "middle",
    "font-size": 11, "font-weight": 700, fill: "var(--svg-ok)"
  });
  newCounterLbl.textContent = "New: 0";
  svg.appendChild(newCounterLbl);

  // ---- Zone 3: Deduplicated result (bottom, green) ----
  var resultY = 310;
  var resultLbl = anim.svgEl("text", {
    x: 20, y: resultY, "font-size": 12, "font-weight": 700, fill: "var(--svg-ok)"
  });
  resultLbl.textContent = "Deduplicated result set";
  svg.appendChild(resultLbl);

  var resultRect = anim.svgEl("rect", {
    x: 20, y: resultY + 10, width: W - 40, height: 60, rx: 8, ry: 8,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1.5
  });
  svg.appendChild(resultRect);

  // Placeholder until the first customer arrives, then the summary line
  // under the customer names (it used to sit on top of them).
  var resultContent = anim.svgEl("text", {
    x: W / 2, y: resultY + 56, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "var(--svg-ok)"
  });
  resultContent.textContent = "(unique customers appear here)";
  svg.appendChild(resultContent);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 16, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "var(--svg-ink)"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  // Result row labels (up to 5 customers)
  var resultLabels = [];
  for (var r = 0; r < 5; r++) {
    var rx = 80 + r * 150;
    var rl = anim.svgEl("text", {
      x: rx, y: resultY + 32, "font-size": 10, "font-weight": 700, fill: "var(--svg-ok)", opacity: 0
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
  stage.pairLabels.forEach(function(l) {
    l.setAttribute("opacity", 0);
    l.setAttribute("fill", "var(--svg-token-ink)");
    l.setAttribute("font-weight", 600);
    l.removeAttribute("text-decoration");
  });
  stage.tempSlots.forEach(function(s) {
    s.rowid.setAttribute("opacity", 0);
    s.name.setAttribute("opacity", 0);
    s.result.setAttribute("opacity", 0);
  });
  stage.dupCounterLbl.textContent = "Duplicates: 0";
  stage.newCounterLbl.textContent = "New: 0";
  stage.resultContent.textContent = "(unique customers appear here)";
  stage.resultLabels.forEach(function(l) { l.setAttribute("opacity", 0); l.textContent = ""; });
  stage.resultRect.setAttribute("stroke-width", 1.5);
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function spawnRowidPill(text, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var tw = Math.max(80, text.length * 6 + 14);
  g.appendChild(anim.svgEl("rect", {
    x: -tw / 2, y: -9, width: tw, height: 18, rx: 9, ry: 9,
    fill: "var(--svg-build)", stroke: "var(--svg-ink)", "stroke-width": 1
  }));
  var lbl = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "var(--svg-on-solid)"
  });
  lbl.textContent = text;
  g.appendChild(lbl);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

// Every outcome is decided from the data when the timeline is built, so a
// step shows the same counts whether it was played, stepped to or scrubbed.
function weedoutPlan() {
  var seen = {}, dups = 0, news = 0, plan = [];
  for (var i = 0; i < JOIN_PAIRS.length; i++) {
    var rowid = JOIN_PAIRS[i].cust.id;
    var dup = !!seen[rowid];
    seen[rowid] = true;
    if (dup) dups++; else news++;
    plan.push({ pair: JOIN_PAIRS[i], rowid: rowid, dup: dup, dups: dups, news: news, resultIdx: news - 1 });
  }
  return plan;
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var plan = weedoutPlan();
  var total = plan.length;
  var uniques = plan[total - 1].news, discarded = plan[total - 1].dups;

  // ---- Phase 1: the inner join produces rows (one guided step) ----
  tl.mark("Inner join (with duplicates)");
  // Guided steps: kinds map to the simplified code (GUIDE in semijoin_weedout.py).
  tl.beat("join");
  tl.call(function() {
    phase.textContent = "Phase 1: the IN subquery runs as an inner join of customers and orders, " +
      "so a customer appears once per matching order";
  });
  for (var i = 0; i < total; i++) {
    (function(idx) {
      var pair = plan[idx].pair;
      tl.call(function() {
        phase.textContent = "Join row " + (idx + 1) + " of " + total + ": " + pair.cust.name +
          " × " + pair.order.desc;
      });
      tl.add({
        from: 0, to: 1, duration: 160, ease: anim.easeOutCubic,
        onUpdate: function(v) { stage.pairLabels[idx].setAttribute("opacity", v.toFixed(2)); }
      });
      tl.delay(190);
    })(i);
  }
  tl.delay(400);
  tl.call(function() {
    phase.textContent = "The inner join produced " + total + " rows for " + uniques +
      " customers: some customers appear more than once. Next, the weedout";
    stage.statusLbl.textContent = "Alice and Carol each appear more than once";
  });
  tl.delay(700);

  // ---- Phase 2: weedout, one guided step per join row ----
  tl.mark("Weedout (dedup via temp table)");
  for (var j = 0; j < total; j++) {
    (function(idx) {
      var step = plan[idx];
      var pair = step.pair;
      var label = stage.pairLabels[idx];
      var slot = stage.tempSlots[idx];
      var pill = null;
      var path = anim.path(label.cx, label.cy, (label.cx + slot.cx) / 2, Math.min(label.cy, slot.cy) - 20,
        slot.cx, slot.cy);

      tl.beat(step.dup ? "weed-dup" : "weed-new");
      tl.call(function() {
        label.setAttribute("fill", "var(--svg-group)");
        label.setAttribute("font-weight", 800);
        phase.textContent = "Weedout, join row " + (idx + 1) + " of " + total + ": " + pair.cust.name +
          " × " + pair.order.desc + " → INSERT rowid=" + step.rowid + " into the temp table";
        stage.statusLbl.textContent = "Trying to insert rowid=" + step.rowid + " (" + pair.cust.name + ")";
      });
      // The rowid travels from the join row to the temp table.
      tl.add({
        from: 0, to: 1, duration: 420, ease: anim.easeInOutQuad,
        onUpdate: function(t) {
          if (!pill) pill = spawnRowidPill("rowid=" + step.rowid + " " + pair.cust.name, label.cx, label.cy);
          var p = path(t);
          pill.setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")");
          pill.setAttribute("opacity", Math.min(1, t * 5).toFixed(2));
        }
      });
      tl.call(function() {
        slot.rowid.textContent = "rowid=" + step.rowid;
        slot.name.textContent = pair.cust.name;
        slot.rowid.setAttribute("opacity", 1);
        slot.name.setAttribute("opacity", 1);
        slot.result.setAttribute("opacity", 1);
        var color = step.dup ? "var(--svg-danger)" : "var(--svg-ok)";
        slot.result.textContent = step.dup ? "✗ duplicate key: discard" : "✓ inserted: new, emit";
        slot.result.setAttribute("fill", color);
        slot.rowid.setAttribute("fill", color);
        slot.name.setAttribute("fill", color);
        stage.dupCounterLbl.textContent = "Duplicates: " + step.dups;
        stage.newCounterLbl.textContent = "New: " + step.news;
        label.setAttribute("fill", color);
        label.setAttribute("font-weight", 600);
        if (step.dup) {
          label.setAttribute("text-decoration", "line-through");
          phase.textContent = "Join row " + (idx + 1) + " of " + total + ": rowid=" + step.rowid +
            " is already in the temp table (duplicate key), so this " +
            pair.cust.name + " row is discarded";
          stage.statusLbl.textContent = "✗ " + pair.cust.name + " was already returned: discard";
        } else {
          if (step.resultIdx < stage.resultLabels.length) {
            stage.resultLabels[step.resultIdx].textContent = pair.cust.name;
            stage.resultLabels[step.resultIdx].setAttribute("opacity", 1);
          }
          stage.resultContent.textContent = "";
          phase.textContent = "Join row " + (idx + 1) + " of " + total + ": rowid=" + step.rowid +
            " is new, the insert succeeds and " +
            pair.cust.name + " is returned";
          stage.statusLbl.textContent = "✓ " + pair.cust.name + " is returned (first time seen)";
        }
      });
      tl.add({
        from: 1, to: 0, duration: 160, ease: anim.easeInCubic,
        onUpdate: function(v) { if (pill) pill.setAttribute("opacity", v.toFixed(2)); },
        onComplete: function() {
          if (pill && pill.parentNode) pill.parentNode.removeChild(pill);
          pill = null;
        }
      });
      tl.delay(380);
    })(j);
  }

  // ---- Phase 3: Done ----
  tl.mark("Done");
  tl.beat("done");
  tl.call(function() {
    phase.textContent = "✓ Weedout complete: " + uniques + " unique customers returned, " +
      discarded + " duplicates discarded";
    stage.statusLbl.textContent = total + " join rows → " + uniques +
      " unique results. Duplication factor: " + (total / uniques).toFixed(1) + "×";
    stage.resultContent.textContent = uniques + " unique customers emitted";
    stage.resultRect.setAttribute("stroke", "var(--svg-ok)");
    stage.resultRect.setAttribute("stroke-width", 2.5);
  });
  tl.delay(500);

  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(outerRows, innerMatches) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 500000,
    xLabel: "outer rows", yLabel: "Total work (rows processed)",
    curves: [
      { label: "Weedout: outer \u00d7 inner_matches", color: "var(--svg-build)",
        fn: function(n) { return n * innerMatches; } },
      { label: "Materialization: outer + subquery", color: "var(--svg-probe)",
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
