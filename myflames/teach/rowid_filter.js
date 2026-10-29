function rowidFilterCost(mainRows, filterSelectivity, rowSize) {
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

// Sample idx_date entries for the animation, in created_date order. The
// entry holds (created_date, id); status is stored only in the row.
// "pass" = the id IS in the rowid filter (status = 'shipped').
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
// The filter holds only the shipped orders' primary keys, sorted.
var FILTER_IDS = SAMPLE_ROWS.filter(function(r) { return r.pass; })
  .map(function(r) { return r.id; });

function fmtFull(n) {
  return Math.round(n).toLocaleString("en-US");
}

function buildStage() {
  var svg = document.getElementById("rowid-filter-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // --- Zone 1: filter index scan -> rowid filter (top) ---
  var z1y = 10;
  var z1Title = anim.svgEl("text", {
    x: W / 2, y: z1y + 14, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  z1Title.textContent = "Step 1: scan idx_status for status = 'shipped' → rowid filter (sorted primary keys)";
  svg.appendChild(z1Title);

  svg.appendChild(anim.svgEl("rect", {
    x: 40, y: z1y + 24, width: W - 80, height: 36, rx: 6, ry: 6,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token)", "stroke-width": 1.5
  }));

  // One slot per key the filter will hold.
  var slotW = (W - 100) / FILTER_IDS.length;
  var slots = [];
  for (var i = 0; i < FILTER_IDS.length; i++) {
    var sx = 50 + i * slotW;
    var cell = anim.svgEl("rect", {
      x: sx, y: z1y + 30, width: slotW - 6, height: 24, rx: 3, ry: 3,
      fill: "var(--svg-node-muted)", stroke: "var(--svg-node-stroke)", "stroke-width": 1
    });
    svg.appendChild(cell);
    var idLbl = anim.svgEl("text", {
      x: sx + (slotW - 6) / 2, y: z1y + 46,
      "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "var(--svg-ink-muted)"
    });
    idLbl.textContent = "–";
    svg.appendChild(idLbl);
    slots.push({cell: cell, lbl: idLbl, id: FILTER_IDS[i]});
  }

  // --- Zone 2: main index scan + filter check (middle) ---
  var z2y = z1y + 80;
  var z2Title = anim.svgEl("text", {
    x: W / 2, y: z2y + 14, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-group)"
  });
  z2Title.textContent = "Step 2: scan idx_date → look each id up in the filter before reading the row";
  svg.appendChild(z2Title);

  var rowEntries = [];
  var rowH = 21;
  var rowStartY = z2y + 30;
  var statusHdr = anim.svgEl("text", {
    x: 380, y: rowStartY - 1, "font-size": 9, "font-weight": 600, fill: "var(--svg-ink-muted)"
  });
  statusHdr.textContent = "status is in the row, not in idx_date:";
  svg.appendChild(statusHdr);
  for (var j = 0; j < SAMPLE_ROWS.length; j++) {
    var r = SAMPLE_ROWS[j];
    var ry2 = rowStartY + j * rowH + 2;
    var bg = anim.svgEl("rect", {
      x: 40, y: ry2, width: W - 80, height: rowH - 3, rx: 4, ry: 4,
      fill: "var(--svg-node-bg)", stroke: "var(--svg-edge)", "stroke-width": 1
    });
    svg.appendChild(bg);
    var idLbl2 = anim.svgEl("text", {
      x: 52, y: ry2 + 14, "font-size": 10, "font-weight": 600, fill: "var(--svg-ink)"
    });
    idLbl2.textContent = "idx_date entry: date=" + r.date + ", id=" + r.id;
    svg.appendChild(idLbl2);
    var statusLbl = anim.svgEl("text", {
      x: 380, y: ry2 + 14, "font-size": 10, "font-weight": 600, fill: "var(--svg-ink-muted)"
    });
    statusLbl.textContent = "status=" + r.status;
    svg.appendChild(statusLbl);
    var icon = anim.svgEl("text", {
      x: W - 52, y: ry2 + 14, "text-anchor": "end",
      "font-size": 10, "font-weight": 700, fill: "var(--svg-ink-muted)"
    });
    icon.textContent = "";
    svg.appendChild(icon);
    rowEntries.push({bg: bg, icon: icon, pass: r.pass, id: r.id});
  }

  // --- Zone 3: result counter (bottom) ---
  var z3y = rowStartY + SAMPLE_ROWS.length * rowH + 12;
  svg.appendChild(anim.svgEl("rect", {
    x: 40, y: z3y, width: W - 80, height: 48, rx: 6, ry: 6,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1.5
  }));
  var resultLbl = anim.svgEl("text", {
    x: W / 2, y: z3y + 22, "text-anchor": "middle",
    "font-size": 14, "font-weight": 700, fill: "var(--svg-ok)"
  });
  resultLbl.textContent = "Rows read: 0 | Clustered index reads skipped: 0";
  svg.appendChild(resultLbl);
  var resultSub = anim.svgEl("text", {
    x: W / 2, y: z3y + 40, "text-anchor": "middle",
    "font-size": 10, fill: "var(--svg-ok)"
  });
  resultSub.textContent = "Only ids found in the filter go on to a clustered index read";
  svg.appendChild(resultSub);

  stage = {
    svg: svg, slots: slots, rowEntries: rowEntries,
    resultLbl: resultLbl, resultSub: resultSub
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.slots.length; i++) {
    stage.slots[i].cell.setAttribute("fill", "var(--svg-node-muted)");
    stage.slots[i].cell.setAttribute("stroke", "var(--svg-node-stroke)");
    stage.slots[i].cell.setAttribute("stroke-width", "1");
    stage.slots[i].lbl.textContent = "–";
    stage.slots[i].lbl.setAttribute("fill", "var(--svg-ink-muted)");
  }
  for (var j = 0; j < stage.rowEntries.length; j++) {
    stage.rowEntries[j].bg.setAttribute("fill", "var(--svg-node-bg)");
    stage.rowEntries[j].bg.setAttribute("stroke", "var(--svg-edge)");
    stage.rowEntries[j].icon.textContent = "";
  }
  stage.resultLbl.textContent = "Rows read: 0 | Clustered index reads skipped: 0";
  stage.resultSub.textContent = "Only ids found in the filter go on to a clustered index read";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");

  // Step 1: build the filter from idx_status. Only the shipped entries are
  // read: a range scan of idx_status touches nothing else.
  tl.mark("Build the rowid filter");
  tl.beat("build");
  tl.call(function() {
    phase.textContent = "Range scan of idx_status for status = 'shipped', reading the index only: " +
      FILTER_IDS.length + " entries, each one adds its primary key to the filter: " + FILTER_IDS.join(", ");
  });
  for (var i = 0; i < stage.slots.length; i++) {
    (function(idx) {
      tl.call(function() {
        var slot = stage.slots[idx];
        slot.cell.setAttribute("fill", "var(--svg-token-soft)");
        slot.cell.setAttribute("stroke", "var(--svg-token)");
        slot.lbl.textContent = "id " + slot.id;
        slot.lbl.setAttribute("fill", "var(--svg-token-ink)");
        anim.pulse(slot.cell, 3, 1, 320);
      });
      tl.delay(220);
    })(i);
  }

  tl.beat("sort");
  tl.call(function() {
    phase.textContent = "MariaDB sorts the filter's keys so each check is a binary search. " +
      "Entries with one status are stored in id order, so these were already sorted.";
    stage.resultSub.textContent = "Filter: " + FILTER_IDS.length + " sorted primary keys of shipped orders";
  });
  tl.delay(600);

  // Step 2: scan idx_date; each entry's id is looked up in the filter
  // before InnoDB reads the row from the clustered index.
  tl.mark("Scan idx_date, check the filter");
  for (var j = 0; j < SAMPLE_ROWS.length; j++) {
    (function(idx) {
      var row = SAMPLE_ROWS[idx];
      var readSoFar = 0;
      for (var k = 0; k <= idx; k++) if (SAMPLE_ROWS[k].pass) readSoFar++;
      var skippedSoFar = idx + 1 - readSoFar;
      var slotIdx = FILTER_IDS.indexOf(row.id);

      if (idx > 0 && idx % 3 === 0) tl.mark("Check id=" + row.id);
      tl.beat(row.pass ? "row-pass" : "row-skip");
      tl.call(function() {
        var re = stage.rowEntries[idx];
        re.bg.setAttribute("fill", "var(--svg-group-soft)");
        re.bg.setAttribute("stroke", "var(--svg-group)");
        if (slotIdx >= 0) stage.slots[slotIdx].cell.setAttribute("stroke-width", "3");
        phase.textContent = "idx_date entry (" + row.date + ", id " + row.id + "): binary search for " +
          row.id + " in the filter…";
      });
      tl.delay(300);
      tl.call(function() {
        var re = stage.rowEntries[idx];
        if (slotIdx >= 0) stage.slots[slotIdx].cell.setAttribute("stroke-width", "1");
        if (row.pass) {
          re.bg.setAttribute("fill", "var(--svg-ok-soft)");
          re.bg.setAttribute("stroke", "var(--svg-ok)");
          re.icon.textContent = "✔ in filter: row read, sent";
          re.icon.setAttribute("fill", "var(--svg-ok)");
          phase.textContent = "idx_date entry (" + row.date + ", id " + row.id + "): " + row.id +
            " is in the filter, so InnoDB reads the row from the clustered index (status = shipped) and sends it";
        } else {
          re.bg.setAttribute("fill", "var(--svg-danger-soft)");
          re.bg.setAttribute("stroke", "var(--svg-danger-soft)");
          re.icon.textContent = "✘ not in filter: row not read";
          re.icon.setAttribute("fill", "var(--svg-danger)");
          phase.textContent = "idx_date entry (" + row.date + ", id " + row.id + "): " + row.id +
            " is not in the filter, so InnoDB skips it without reading the row";
        }
        stage.resultLbl.textContent = "Rows read: " + readSoFar +
          " | Clustered index reads skipped: " + skippedSoFar;
      });
      tl.delay(250);
    })(j);
  }

  tl.delay(400);
  tl.beat("done");
  tl.mark("Summary");
  tl.call(function() {
    var c = teachRuntime.readControls();
    var cost = rowidFilterCost(c.main_rows, c.filter_selectivity, c.row_size);
    var read = FILTER_IDS.length;
    var skipped = SAMPLE_ROWS.length - read;
    phase.textContent = "✓ Done — " + SAMPLE_ROWS.length + " idx_date entries checked: " + read +
      " rows read, " + skipped + " clustered index reads skipped. At " + fmtFull(cost.mainRows) +
      " idx_date entries and " + cost.filterSelectivity + "% shipped: " + fmtFull(cost.withFilterFetches) +
      " rows read, " + fmtFull(cost.rowsSkipped) + " skipped.";
    stage.resultSub.textContent = "Without the filter: " + SAMPLE_ROWS.length +
      " clustered index reads. With it: " + read + ", so " + skipped + " are saved.";
  });

  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(mainRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 500000,
    xLabel: "Main index rows scanned", yLabel: "Table-row random I/Os",
    curves: [
      { label: "Without rowid filter (all rows fetched)", color: "var(--svg-danger)",
        fn: function(n) { return n; } },
      { label: "With rowid filter (20% selectivity)", color: "var(--svg-ok)",
        fn: function(n) { return Math.max(1, Math.ceil(n * 0.20)); } },
      { label: "With rowid filter (5% selectivity)", color: "var(--svg-token)",
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
    "Without a rowid filter: all " + teachRuntime.formatInt(cost.withoutFilterFetches) +
    " idx_date entries cost a clustered index read. With the filter: " +
    cost.filterSelectivity + "% of the ids are found in it \u2014 only " +
    teachRuntime.formatInt(cost.withFilterFetches) + " rows read, saving " +
    teachRuntime.formatInt(cost.savings) + " clustered index reads.";
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
