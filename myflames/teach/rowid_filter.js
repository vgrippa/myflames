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

      if (idx % 3 === 0) {
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
      { label: "With rowid filter (20% selectivity)", color: "#059669",
        fn: function(n) { return Math.max(1, Math.ceil(n * 0.20)); } },
      { label: "With rowid filter (5% selectivity)", color: "#0284c7",
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
  document.getElementById("out-selectivity").textContent = cost.filterSelectivity + "%";
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
    cost.filterSelectivity + "% of rows \u2014 only " +
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
