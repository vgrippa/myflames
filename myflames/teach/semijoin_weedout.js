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
