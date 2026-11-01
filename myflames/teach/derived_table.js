function derivedCost(subqueryRows, rowSize, outerRows, hasIndex) {
  var materializeWrites = subqueryRows;
  var tmpSize = subqueryRows * rowSize;
  var spillsToDisk = tmpSize > 16 * 1024 * 1024;  // 16 MiB
  var totalReads;
  var readsPerProbe = 0;
  var height = 0;
  if (hasIndex) {
    height = Math.max(1, Math.ceil(Math.log(Math.max(2, subqueryRows)) / Math.log(800)));
    readsPerProbe = height;
    totalReads = outerRows * readsPerProbe;
  } else {
    totalReads = subqueryRows;  // full scan once
  }
  var totalIO = materializeWrites + totalReads;
  return {
    materializeWrites: materializeWrites,
    tmpSize: tmpSize,
    spillsToDisk: spillsToDisk,
    hasIndex: hasIndex,
    height: height,
    readsPerProbe: readsPerProbe,
    totalReads: totalReads,
    totalIO: totalIO
  };
}

var W = 800, H = 440;
var TMP_LIMIT = 16 * 1024 * 1024;  // tmp_table_size default
var stage = null;

var SUBQUERY_ROWS = [
  {dept: 1, avg: 62000},
  {dept: 2, avg: 71000},
  {dept: 3, avg: 58000},
  {dept: 4, avg: 85000},
  {dept: 5, avg: 69000},
  {dept: 6, avg: 77000},
  {dept: 7, avg: 54000},
  {dept: 8, avg: 93000}
];

var OUTER_ROWS = [
  {id: 1, name: "Sales"},
  {id: 3, name: "Eng"},
  {id: 5, name: "Mktg"},
  {id: 7, name: "Ops"},
  {id: 2, name: "HR"},
  {id: 4, name: "Finance"}
];

// Stage geometry: stats (the temp table) across the top, departments on
// the lower left, result rows on the lower right. A probe pill carries
// "Eng id=3" up to its stats row; a result pill carries the joined row down.
var CELL_W = 182, CELL_H = 26, CELL_GAP = 10, CELL_Y0 = 36;
var BAR_X = 20, BAR_Y = 104, BAR_W = W - 40, BAR_H = 12;
var DEPT_X = 20, DEPT_W = 250, RES_X = 530, RES_W = 250;
var LIST_Y0 = 186, LIST_STEP = 30, LIST_H = 24;

function money(v) { return "$" + (v / 1000).toFixed(0) + "K"; }
function cellX(i) { return 20 + (i % 4) * (CELL_W + CELL_GAP); }
function cellY(i) { return CELL_Y0 + Math.floor(i / 4) * (CELL_H + 6); }
function listY(i) { return LIST_Y0 + i * LIST_STEP; }
function statsIndexOf(dept) {
  for (var i = 0; i < SUBQUERY_ROWS.length; i++) if (SUBQUERY_ROWS[i].dept === dept) return i;
  return -1;
}

function label(svg, attrs, value) {
  var t = anim.svgEl("text", attrs);
  t.textContent = value;
  svg.appendChild(t);
  return t;
}

function makePill(svg, width, fill) {
  var g = anim.svgEl("g", {opacity: 0});
  g.appendChild(anim.svgEl("rect", {
    x: -width / 2, y: -10, width: width, height: 20, rx: 10, ry: 10,
    fill: fill, stroke: "var(--svg-ink)", "stroke-width": 1
  }));
  var t = label(g, {x: 0, y: 4, "text-anchor": "middle", "font-size": 10, "font-weight": 700,
    fill: "var(--svg-on-solid)"}, "");
  svg.appendChild(g);
  return {g: g, text: t};
}

function buildStage() {
  var svg = document.getElementById("derived-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  label(svg, {x: 20, y: 24, "font-size": 13, "font-weight": 700, fill: "var(--svg-build-ink)"},
    "① Temp table stats: the subquery's result rows");
  var keyBadge = label(svg, {x: W - 20, y: 24, "text-anchor": "end", "font-size": 11,
    "font-weight": 700, fill: "var(--svg-token-ink)", opacity: 0}, "");

  var cells = [];
  for (var i = 0; i < SUBQUERY_ROWS.length; i++) {
    var r = SUBQUERY_ROWS[i];
    var bg = anim.svgEl("rect", {
      x: cellX(i), y: cellY(i), width: CELL_W, height: CELL_H, rx: 5, ry: 5,
      fill: "none", stroke: "var(--svg-node-stroke)", "stroke-width": 1, "stroke-dasharray": "4,3"
    });
    svg.appendChild(bg);
    var t = label(svg, {x: cellX(i) + 12, y: cellY(i) + 17, "font-size": 11, "font-weight": 600,
      fill: "var(--svg-build-ink)", opacity: 0}, "dept_id=" + r.dept + "   avg_salary=" + money(r.avg));
    cells.push({bg: bg, text: t, row: r});
  }

  svg.appendChild(anim.svgEl("rect", {
    x: BAR_X, y: BAR_Y, width: BAR_W, height: BAR_H, rx: 3, ry: 3,
    fill: "var(--svg-build-soft)", stroke: "var(--svg-node-stroke)", "stroke-width": 1
  }));
  var barFill = anim.svgEl("rect", {
    x: BAR_X, y: BAR_Y, width: 0, height: BAR_H, rx: 3, ry: 3, fill: "var(--svg-highlight)"
  });
  svg.appendChild(barFill);
  var barText = label(svg, {x: BAR_X, y: BAR_Y + 30, "font-size": 11, "font-weight": 600,
    fill: "var(--svg-build-ink)"}, "");
  var spillLbl = label(svg, {x: W - 20, y: BAR_Y + 30, "text-anchor": "end", "font-size": 11,
    "font-weight": 700, fill: "var(--svg-danger)", opacity: 0}, "⚠ over tmp_table_size: moved to an on-disk table");

  label(svg, {x: DEPT_X, y: 170, "font-size": 13, "font-weight": 700, fill: "var(--svg-group)"},
    "② departments: each row finds its stats row");
  label(svg, {x: RES_X, y: 170, "font-size": 13, "font-weight": 700, fill: "var(--svg-ok)"},
    "③ Result rows");

  var depts = [];
  var results = [];
  for (var j = 0; j < OUTER_ROWS.length; j++) {
    var d = OUTER_ROWS[j];
    var dbg = anim.svgEl("rect", {
      x: DEPT_X, y: listY(j), width: DEPT_W, height: LIST_H, rx: 5, ry: 5,
      fill: "var(--svg-group-soft)", stroke: "var(--svg-group)", "stroke-width": 1
    });
    svg.appendChild(dbg);
    label(svg, {x: DEPT_X + 12, y: listY(j) + 16, "font-size": 11, "font-weight": 600,
      fill: "var(--svg-group)"}, "id=" + d.id + "   dept_name=" + d.name);
    depts.push({bg: dbg, row: d});
    var rbg = anim.svgEl("rect", {
      x: RES_X, y: listY(j), width: RES_W, height: LIST_H, rx: 5, ry: 5,
      fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok-soft)", "stroke-width": 1
    });
    svg.appendChild(rbg);
    var rt = label(svg, {x: RES_X + 12, y: listY(j) + 16, "font-size": 11, "font-weight": 600,
      fill: "var(--svg-ok)"}, "");
    results.push({bg: rbg, text: rt});
  }

  var probe = makePill(svg, 110, "var(--svg-group)");
  var result = makePill(svg, 150, "var(--svg-ok)");

  var statusLbl = label(svg, {x: W / 2, y: H - 16, "text-anchor": "middle", "font-size": 12.5,
    "font-weight": 600, fill: "var(--svg-ink)"}, "");

  stage = {svg: svg, keyBadge: keyBadge, cells: cells, barFill: barFill, barText: barText,
           spillLbl: spillLbl, depts: depts, results: results, probe: probe, result: result,
           statusLbl: statusLbl};
}

function setBar(frac, spilled) {
  var f = Math.max(0, Math.min(1, frac));
  stage.barFill.setAttribute("width", (BAR_W * f).toFixed(1));
  stage.barFill.setAttribute("fill", spilled ? "var(--svg-danger)" : "var(--svg-highlight)");
}

function resetStage() {
  if (!stage) return;
  stage.keyBadge.setAttribute("opacity", 0);
  for (var i = 0; i < stage.cells.length; i++) {
    var c = stage.cells[i];
    c.bg.setAttribute("fill", "none");
    c.bg.setAttribute("stroke", "var(--svg-node-stroke)");
    c.bg.setAttribute("stroke-dasharray", "4,3");
    c.bg.setAttribute("stroke-width", 1);
    c.text.setAttribute("opacity", 0);
    c.text.setAttribute("transform", "");
  }
  setBar(0, false);
  stage.barText.textContent = "stats is empty";
  stage.spillLbl.setAttribute("opacity", 0);
  for (var j = 0; j < stage.depts.length; j++) {
    stage.depts[j].bg.setAttribute("fill", "var(--svg-group-soft)");
    stage.results[j].text.textContent = "";
    stage.results[j].bg.setAttribute("stroke", "var(--svg-ok-soft)");
  }
  stage.probe.g.setAttribute("opacity", 0);
  stage.result.g.setAttribute("opacity", 0);
  stage.statusLbl.textContent = "";
}

function placePill(pill, p, opacity) {
  pill.g.setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")");
  pill.g.setAttribute("opacity", String(opacity));
}

function buildTimeline(cost, c) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var hasIndex = cost.hasIndex;
  var total = SUBQUERY_ROWS.length;
  var perSample = cost.tmpSize / total;     // bytes each sample row stands for
  var spillAt = -1;                         // sample row whose write crosses the limit
  if (cost.spillsToDisk) {
    for (var s = 0; s < total; s++) {
      if (perSample * (s + 1) > TMP_LIMIT) { spillAt = s; break; }
    }
  }

  // Guided steps: creating stats, one per subquery row written, the spill
  // (when it happens), the scan of stats without a key, one per departments
  // row, then the summary. Kinds map to the simplified-code lines (GUIDE in
  // derived_table.py).
  tl.mark("Create the temp table");
  tl.beat("create");
  tl.call(function() {
    stage.keyBadge.textContent = hasIndex ? "key on dept_id (<auto_key0> in MySQL, key0 in MariaDB)"
                                          : "no key on stats";
    phase.textContent = hasIndex
      ? "The subquery cannot be merged (GROUP BY, AVG), so it is materialized. The optimizer already " +
        "planned a key on stats.dept_id, so stats is created with that index before any row is written"
      : "The subquery cannot be merged (GROUP BY, AVG), so it is materialized into stats, " +
        "created here without an index (MariaDB: optimizer_switch derived_with_keys=off)";
    stage.statusLbl.textContent = "stats will hold one row per dept_id: " + teachRuntime.formatInt(c.subquery_rows) +
      " rows at your settings, 8 in this drawing.";
  });
  tl.add({from: 0, to: 1, duration: 320, ease: anim.easeOutCubic,
    onUpdate: function(v) { stage.keyBadge.setAttribute("opacity", v.toFixed(2)); }});
  tl.delay(200);

  tl.mark("Materialize subquery");
  for (var i = 0; i < total; i++) {
    (function(idx) {
      var r = SUBQUERY_ROWS[idx];
      var cell = stage.cells[idx];
      var spilledNow = spillAt >= 0 && idx >= spillAt;
      var fracBefore = Math.min(1, perSample * idx / TMP_LIMIT);
      var fracAfter = Math.min(1, perSample * (idx + 1) / TMP_LIMIT);
      tl.beat("write");
      tl.call(function() {
        cell.bg.setAttribute("stroke-dasharray", "");
        cell.bg.setAttribute("stroke", "var(--svg-build)");
        phase.textContent = "Subquery row " + (idx + 1) + " of " + total + ": dept_id=" + r.dept +
          ", AVG(salary)=" + money(r.avg) + " is written into stats" +
          (hasIndex ? " and indexed by dept_id as it goes" : "");
      });
      tl.add({from: 0, to: 1, duration: 300, ease: anim.easeOutBack,
        onUpdate: function(v) {
          cell.text.setAttribute("opacity", Math.min(1, v * 1.4).toFixed(2));
          cell.text.setAttribute("transform", "translate(0," + ((1 - v) * -10).toFixed(1) + ")");
          setBar(fracBefore + (fracAfter - fracBefore) * Math.min(1, v), spilledNow);
        },
        onComplete: function() {
          cell.text.setAttribute("transform", "");
          cell.bg.setAttribute("fill", "var(--svg-build-soft)");
          anim.arrival(cell.bg);
          var bytes = perSample * (idx + 1);
          stage.barText.textContent = "stats: " + (idx + 1) + " of " + total + " rows written (" +
            teachRuntime.formatBytes(Math.round(bytes)) +
            (spillAt >= 0 && idx > spillAt
              ? ", in the on-disk table now)"
              : " of the " + teachRuntime.formatBytes(TMP_LIMIT) + " tmp_table_size at your settings)");
          stage.statusLbl.textContent = "Written: dept_id=" + r.dept + " avg_salary=" + money(r.avg);
        }});
      tl.delay(140);
      if (idx === spillAt) {
        tl.beat("spill");
        tl.call(function() {
          stage.spillLbl.setAttribute("opacity", 1);
          setBar(1, true);
          phase.textContent = "stats just passed tmp_table_size (" + teachRuntime.formatBytes(TMP_LIMIT) +
            "), so it is converted to an on-disk table (InnoDB in MySQL, Aria in MariaDB) and filling continues there";
          stage.statusLbl.textContent = "Every row written so far is copied to disk; later rows go straight there.";
        });
        tl.delay(500);
      }
    })(i);
  }

  if (!hasIndex) {
    tl.mark("Read stats (no key)");
    tl.beat("scan");
    tl.call(function() {
      phase.textContent = "No key on stats, so there is nothing to look dept_id up in: the join reads all " +
        total + " stats rows once (a hash join in MySQL, a join buffer in MariaDB)";
    });
    for (var k = 0; k < total; k++) {
      (function(idx) {
        tl.add({from: 0, to: 1, duration: 90, ease: anim.easeOutCubic, onUpdate: function() {},
          onComplete: function() {
            stage.cells[idx].bg.setAttribute("stroke", "var(--svg-group)");
            stage.cells[idx].bg.setAttribute("stroke-width", 2);
            stage.statusLbl.textContent = "Read stats row " + (idx + 1) + " of " + total;
          }});
      })(k);
    }
    tl.delay(250);
  }

  tl.mark("Outer query probing");
  for (var j = 0; j < OUTER_ROWS.length; j++) {
    (function(idx) {
      var d = OUTER_ROWS[idx];
      var si = statsIndexOf(d.id);
      var sr = SUBQUERY_ROWS[si];
      var cell = stage.cells[si];
      var from = {x: DEPT_X + DEPT_W + 60, y: listY(idx) + LIST_H / 2};
      var cellMid = {x: cellX(si) + CELL_W / 2, y: cellY(si) + CELL_H / 2};
      var resMid = {x: RES_X + 80, y: listY(idx) + LIST_H / 2};
      var up = anim.path(from.x, from.y, (from.x + cellMid.x) / 2, Math.min(from.y, cellMid.y) - 10, cellMid.x, cellMid.y + 18);
      var down = anim.path(cellMid.x, cellMid.y + 18, (cellMid.x + resMid.x) / 2, cellMid.y + 60, resMid.x, resMid.y);
      var joined = d.name + ": avg_salary " + money(sr.avg);
      tl.beat("probe");
      tl.call(function() {
        stage.depts[idx].bg.setAttribute("fill", "var(--svg-highlight)");
        stage.probe.text.textContent = d.name + " id=" + d.id;
        placePill(stage.probe, from, 1);
        phase.textContent = hasIndex
          ? "departments row " + (idx + 1) + " of " + OUTER_ROWS.length + ": " + d.name + " (id=" + d.id +
            ") looks up dept_id=" + d.id + " in stats through the key: one index lookup"
          : "departments row " + (idx + 1) + " of " + OUTER_ROWS.length + ": " + d.name + " (id=" + d.id +
            ") is matched with dept_id=" + d.id + " among the stats rows already read";
        stage.statusLbl.textContent = "Probe: " + d.name + " (id=" + d.id + ")";
      });
      tl.add({from: 0, to: 1, duration: 420, ease: anim.easeInOutCubic,
        onUpdate: function(t) { placePill(stage.probe, up(t), 1); },
        onComplete: function() {
          stage.probe.g.setAttribute("opacity", 0);
          cell.bg.setAttribute("stroke", "var(--svg-group)");
          cell.bg.setAttribute("stroke-width", 3);
          anim.arrival(cell.bg);
          stage.result.text.textContent = joined;
        }});
      tl.add({from: 0, to: 1, duration: 380, ease: anim.easeInOutCubic,
        onUpdate: function(t) { placePill(stage.result, down(t), 1); },
        onComplete: function() {
          stage.result.g.setAttribute("opacity", 0);
          stage.results[idx].text.textContent = joined;
          stage.results[idx].bg.setAttribute("stroke", "var(--svg-ok)");
          anim.arrival(stage.results[idx].bg);
          phase.textContent = d.name + " (id=" + d.id + ") matched stats row dept_id=" + d.id +
            ": avg_salary " + money(sr.avg) + " → result row " + (idx + 1) + " of " + OUTER_ROWS.length;
          stage.statusLbl.textContent = "Result: " + joined;
        }});
      tl.delay(160);
      tl.call(function() {
        stage.depts[idx].bg.setAttribute("fill", "var(--svg-ok-soft)");
        cell.bg.setAttribute("stroke", hasIndex ? "var(--svg-build)" : "var(--svg-group)");
        cell.bg.setAttribute("stroke-width", hasIndex ? 1 : 2);
      });
    })(j);
  }

  tl.delay(250);
  tl.mark("Done");
  tl.beat("done");
  tl.call(function() {
    phase.textContent = hasIndex
      ? "Done: stats was filled once (" + teachRuntime.formatInt(cost.materializeWrites) + " rows at your settings) and probed " +
        teachRuntime.formatInt(c.outer_rows) + " times through its key, about " + cost.readsPerProbe + " page reads each"
      : "Done: stats was filled once (" + teachRuntime.formatInt(cost.materializeWrites) + " rows at your settings) and read " +
        "in full once to match every departments row, with no key to look up";
    stage.statusLbl.textContent = hasIndex
      ? "The key turns each probe into one lookup instead of a read of all of stats."
      : "Without a key, the join reads every stats row; with a key, each probe reads only its own row.";
  });
  tl.delay(300);
  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var cost = derivedCost(c.subquery_rows, c.row_size, c.outer_rows, c.has_index);
  return buildTimeline(cost, c);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(currentSubqueryRows, hasIndex) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e6,
    xLabel: "Subquery rows", yLabel: "Total I/O operations",
    curves: [
      {label: "Materialized + auto-index", color: "var(--svg-token)", fn: function(n) {
        var h = Math.max(1, Math.ceil(Math.log(Math.max(2, n)) / Math.log(800)));
        return n + 1000 * h;
      }},
      {label: "Materialized + full scan", color: "var(--svg-danger)", fn: function(n) {
        return n + n;
      }},
      {label: "Merged (no temp table)", color: "var(--svg-ok)", fn: function(n) {
        return 1000;  // just outer_rows, no materialization overhead
      }}
    ],
    current: { x: currentSubqueryRows }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = derivedCost(c.subquery_rows, c.row_size, c.outer_rows, c.has_index);

  document.getElementById("out-subquery-rows").textContent = teachRuntime.formatInt(cost.materializeWrites);
  document.getElementById("out-mat-writes").textContent = teachRuntime.formatInt(cost.materializeWrites);
  document.getElementById("out-tmp-size").textContent = teachRuntime.formatBytes(cost.tmpSize);
  document.getElementById("out-spills").textContent = cost.spillsToDisk ? "Yes — exceeds 16 MiB" : "No — fits in memory";
  document.getElementById("out-spills").className = "value " + (cost.spillsToDisk ? "hot" : "ok");
  document.getElementById("out-auto-index").textContent = cost.hasIndex ? "Yes: key on dept_id" : "No: full read";
  document.getElementById("out-auto-index").className = "value " + (cost.hasIndex ? "ok" : "hot");
  document.getElementById("out-probe-reads").textContent = teachRuntime.formatInt(cost.totalReads);
  document.getElementById("out-total-io").textContent = teachRuntime.formatInt(cost.totalIO);

  var explanation;
  if (cost.hasIndex) {
    explanation = "Subquery materializes " + teachRuntime.formatInt(cost.materializeWrites) +
      " rows (" + teachRuntime.formatBytes(cost.tmpSize) + ") into temp table." +
      " Auto-index (B+tree height " + cost.height + ") gives O(log n) probe." +
      " Each of " + teachRuntime.formatInt(c.outer_rows) + " outer rows needs " +
      cost.readsPerProbe + " page reads = " + teachRuntime.formatInt(cost.totalReads) + " total probe reads.";
  } else {
    explanation = "Subquery materializes " + teachRuntime.formatInt(cost.materializeWrites) +
      " rows (" + teachRuntime.formatBytes(cost.tmpSize) + ") into temp table." +
      " No auto-index: full scan of " + teachRuntime.formatInt(cost.totalReads) +
      " rows to answer outer query.";
  }
  if (cost.spillsToDisk) {
    explanation += " Temp table exceeds 16 MiB — spills to disk, adding I/O latency.";
  }
  document.getElementById("out-explanation").textContent = explanation;

  renderChart(c.subquery_rows, c.has_index);
  buildStage();
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
