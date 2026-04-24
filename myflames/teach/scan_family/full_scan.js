function fullScanCost(rows, rowSize, selectivityPct) {
  var sel = Math.max(0.001, Math.min(1, selectivityPct / 100));
  var matched = Math.max(1, Math.floor(rows * sel));
  var pageSize = 16 * 1024;
  var fullBytes = rows * rowSize;
  var pages = Math.max(1, Math.ceil(fullBytes / pageSize));
  var btreeHeight = Math.max(2, Math.ceil(Math.log(Math.max(2, rows)) / Math.log(800)));
  var indexRowsRead = btreeHeight + matched;
  var amplification = rows / Math.max(1, indexRowsRead);
  return {
    fullRows: rows,
    matched: matched,
    fullBytes: fullBytes,
    pages: pages,
    btreeHeight: btreeHeight,
    indexRowsRead: indexRowsRead,
    amplification: amplification
  };
}

var W = 800, H = 460;
var stage = null;

var USERS = [
  {id: 1, name: "Ana",    country: "BR", match: false},
  {id: 2, name: "Ben",    country: "US", match: true},
  {id: 3, name: "Cora",   country: "DE", match: false},
  {id: 4, name: "Diego",  country: "US", match: true},
  {id: 5, name: "Elena",  country: "ES", match: false},
  {id: 6, name: "Farah",  country: "US", match: true},
  {id: 7, name: "Gus",    country: "AR", match: false},
  {id: 8, name: "Hana",   country: "JP", match: false},
  {id: 9, name: "Ivan",   country: "US", match: true},
  {id: 10, name: "Jules", country: "FR", match: false}
];

function buildStage() {
  var svg = document.getElementById("scan-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var leftX = 20;
  var topY = 42;
  var rowH = 28;
  var tableW = 500;
  var tableH = USERS.length * rowH + 10;

  var tableLbl = anim.svgEl("text", {
    x: leftX, y: 24, "font-size": 13, "font-weight": 700, fill: "#7c2d12"
  });
  tableLbl.textContent = "users table — full scan reads every row";
  svg.appendChild(tableLbl);

  var tableRect = anim.svgEl("rect", {
    x: leftX, y: topY, width: tableW, height: tableH, rx: 8, ry: 8,
    fill: "#fff7ed", stroke: "#fdba74", "stroke-width": 1.5
  });
  svg.appendChild(tableRect);

  var rows = [];
  for (var i = 0; i < USERS.length; i++) {
    var u = USERS[i];
    var y = topY + 6 + i * rowH;
    var r = anim.svgEl("rect", {
      x: leftX + 8, y: y, width: tableW - 16, height: rowH - 4, rx: 5, ry: 5,
      fill: "#ffedd5", stroke: "#fed7aa", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: leftX + 18, y: y + 17, "font-size": 11, "font-weight": 600, fill: "#7c2d12"
    });
    t.textContent = "id=" + u.id + " " + u.name + " country=" + u.country;
    svg.appendChild(t);
    rows.push({bg: r, txt: t, data: u});
  }

  var scanner = anim.svgEl("rect", {
    x: leftX + 10, y: topY + 8, width: tableW - 20, height: rowH - 8, rx: 6, ry: 6,
    fill: "#fef3c7", stroke: "#f59e0b", "stroke-width": 2, opacity: 0.95
  });
  svg.appendChild(scanner);

  var resultX = 560;
  var resultY = 42;
  var resultW = 220;
  var resultH = 230;

  var outLbl = anim.svgEl("text", {
    x: resultX, y: 24, "font-size": 13, "font-weight": 700, fill: "#065f46"
  });
  outLbl.textContent = "predicate matches (country='US')";
  svg.appendChild(outLbl);

  var outRect = anim.svgEl("rect", {
    x: resultX, y: resultY, width: resultW, height: resultH, rx: 8, ry: 8,
    fill: "#ecfdf5", stroke: "#34d399", "stroke-width": 1.5
  });
  svg.appendChild(outRect);

  var resultRows = [];
  for (var j = 0; j < 5; j++) {
    var rr = anim.svgEl("text", {
      x: resultX + 12, y: resultY + 24 + j * 20, "font-size": 11, "font-weight": 600, fill: "#065f46"
    });
    rr.textContent = "";
    svg.appendChild(rr);
    resultRows.push(rr);
  }

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 18, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  // --- Live cost ticker (right panel, below result box) ---
  var tickerX = resultX;
  var tickerY = resultY + resultH + 18;
  var tickerW = resultW;

  var tickerBg = anim.svgEl("rect", {
    x: tickerX, y: tickerY, width: tickerW, height: 160, rx: 8, ry: 8,
    fill: "#fefce8", stroke: "#fbbf24", "stroke-width": 1.2, opacity: 0.0
  });
  svg.appendChild(tickerBg);

  var tickerTitle = anim.svgEl("text", {
    x: tickerX + tickerW / 2, y: tickerY + 16, "text-anchor": "middle",
    "font-size": 10, "font-weight": 700, fill: "#92400e",
    "text-transform": "uppercase", "letter-spacing": "0.5", opacity: 0.0
  });
  tickerTitle.textContent = "LIVE COST";
  svg.appendChild(tickerTitle);

  var tickerMetrics = [];
  var metricDefs = [
    {label: "Rows read"},
    {label: "Rows returned"},
    {label: "Bytes read"},
    {label: "Pages touched"},
    {label: "Indexed path"},
    {label: "Amplification"}
  ];
  var tickerH = 30 + metricDefs.length * 22 + 6;
  tickerBg.setAttribute("height", tickerH);
  for (var m = 0; m < metricDefs.length; m++) {
    var my = tickerY + 26 + m * 22;
    var lbl = anim.svgEl("text", {
      x: tickerX + 10, y: my + 12, "font-size": 10, fill: "#78716c",
      "font-weight": 600, opacity: 0.0
    });
    lbl.textContent = metricDefs[m].label;
    svg.appendChild(lbl);
    var val = anim.svgEl("text", {
      x: tickerX + tickerW - 10, y: my + 12, "text-anchor": "end",
      "font-size": 12, "font-weight": 700, fill: "#1c1917",
      "font-variant-numeric": "tabular-nums", opacity: 0.0
    });
    val.textContent = "—";
    svg.appendChild(val);
    tickerMetrics.push({lbl: lbl, val: val, def: metricDefs[m]});
  }

  stage = {
    svg: svg,
    rows: rows,
    scanner: scanner,
    resultRows: resultRows,
    statusLbl: statusLbl,
    matches: [],
    ticker: {
      bg: tickerBg,
      title: tickerTitle,
      metrics: tickerMetrics
    }
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.rows.length; i++) {
    stage.rows[i].bg.setAttribute("fill", "#ffedd5");
    stage.rows[i].bg.setAttribute("stroke", "#fed7aa");
  }
  stage.scanner.setAttribute("y", 50);
  for (var j = 0; j < stage.resultRows.length; j++) stage.resultRows[j].textContent = "";
  stage.matches = [];
  stage.statusLbl.textContent = "";
  // Reset ticker
  if (stage.ticker) {
    stage.ticker.bg.setAttribute("opacity", "0.0");
    stage.ticker.title.setAttribute("opacity", "0.0");
    for (var t = 0; t < stage.ticker.metrics.length; t++) {
      stage.ticker.metrics[t].lbl.setAttribute("opacity", "0.0");
      stage.ticker.metrics[t].val.setAttribute("opacity", "0.0");
      stage.ticker.metrics[t].val.textContent = "—";
    }
  }
}

// Update the live ticker using the real cost-model numbers scaled by progress.
// progress: 0..1 representing how far through the full scan we are.
function updateTicker(progress) {
  if (!stage || !stage.ticker) return;
  var c = teachRuntime.readControls();
  var cost = fullScanCost(c.rows, c.row_size, c.selectivity);
  var tk = stage.ticker;

  var rowsRead = Math.round(cost.fullRows * progress);
  var rowsReturned = Math.round(cost.matched * progress);
  var bytesRead = Math.round(cost.fullBytes * progress);
  var pagesNow = Math.max(0, Math.ceil(bytesRead / (16 * 1024)));
  var ampNow = rowsReturned > 0 ? (rowsRead / rowsReturned).toFixed(1) + "\u00d7" : "\u2014";

  tk.metrics[0].val.textContent = teachRuntime.formatInt(rowsRead);
  tk.metrics[1].val.textContent = teachRuntime.formatInt(rowsReturned);
  tk.metrics[2].val.textContent = teachRuntime.formatBytes(bytesRead);
  tk.metrics[3].val.textContent = teachRuntime.formatInt(pagesNow) + " pg";
  tk.metrics[4].val.textContent = teachRuntime.formatInt(cost.indexRowsRead);
  tk.metrics[5].val.textContent = ampNow;

  // Color: amplification red when high
  if (rowsReturned > 0 && rowsRead / rowsReturned > 2) {
    tk.metrics[5].val.setAttribute("fill", "#dc2626");
  } else {
    tk.metrics[5].val.setAttribute("fill", "#1c1917");
  }
  // Indexed path is always green (the comparison baseline)
  tk.metrics[4].val.setAttribute("fill", "#059669");
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var scanned = 0;
  var matched = 0;

  tl.mark("Scan every row");
  tl.call(function() {
    phase.textContent = "Phase 1/2 — no usable index, so the engine reads each row one by one";
    // Fade in the live cost ticker
    if (stage.ticker) {
      var els = [stage.ticker.bg, stage.ticker.title];
      for (var ti = 0; ti < stage.ticker.metrics.length; ti++) {
        els.push(stage.ticker.metrics[ti].lbl);
        els.push(stage.ticker.metrics[ti].val);
      }
      for (var ei = 0; ei < els.length; ei++) {
        (function(el, delay) {
          anim.tween({from: 0, to: 1, duration: 300, delay: delay, ease: anim.easeOutCubic,
            onUpdate: function(v) { el.setAttribute("opacity", v); }
          });
        })(els[ei], ei * 40);
      }
      updateTicker(0);
    }
  });

  var totalSample = stage.rows.length;
  for (var i = 0; i < totalSample; i++) {
    (function(idx) {
      tl.call(function() {
        var y = 50 + idx * 28;
        stage.scanner.setAttribute("y", y);
        for (var k = 0; k < stage.rows.length; k++) {
          stage.rows[k].bg.setAttribute("fill", "#ffedd5");
          stage.rows[k].bg.setAttribute("stroke", "#fed7aa");
        }
        stage.rows[idx].bg.setAttribute("fill", "#fde68a");
        stage.rows[idx].bg.setAttribute("stroke", "#f59e0b");
        scanned += 1;

        var u = stage.rows[idx].data;
        var progress = scanned / totalSample;
        if (u.match) {
          stage.rows[idx].bg.setAttribute("fill", "#bbf7d0");
          stage.rows[idx].bg.setAttribute("stroke", "#22c55e");
          matched += 1;
          var label = "id=" + u.id + " " + u.name;
          stage.resultRows[Math.min(matched - 1, stage.resultRows.length - 1)].textContent = label;
          stage.statusLbl.textContent = "Read row " + scanned + "/" + totalSample +
            " — match \u2713 (" + label + ")";
          // Pulse the ticker match count green briefly
          if (stage.ticker) {
            var matchVal = stage.ticker.metrics[1].val;
            anim.tween({from: 0, to: 1, duration: 400, ease: anim.easeOutCubic,
              onUpdate: function(t) {
                matchVal.setAttribute("fill", anim.lerpColor("#16a34a", "#1c1917", t));
              }
            });
          }
        } else {
          stage.statusLbl.textContent = "Read row " + scanned + "/" + totalSample +
            " — no match (filtered out)";
        }
        updateTicker(progress);
      });
      tl.delay(260);
    })(i);
  }

  tl.mark("Summary");
  tl.call(function() {
    var c = teachRuntime.readControls();
    var cost = fullScanCost(c.rows, c.row_size, c.selectivity);
    phase.textContent = "Phase 2/2 — scan complete: all " +
      teachRuntime.formatInt(cost.fullRows) + " rows touched, only " +
      teachRuntime.formatInt(cost.matched) + " matched (" +
      cost.amplification.toFixed(1) + "\u00d7 amplification)";
    stage.statusLbl.textContent = "Full scan: " + teachRuntime.formatInt(cost.fullRows) +
      " rows \u2192 " + teachRuntime.formatInt(cost.matched) + " results. " +
      teachRuntime.formatBytes(cost.fullBytes) + " read, " +
      cost.amplification.toFixed(1) + "\u00d7 more work than an indexed path.";
    updateTicker(1);
  });
  tl.delay(350);
  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}

function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

function renderChart(rowSize, selectivityPct, currentRows) {
  var sel = Math.max(0.001, Math.min(1, selectivityPct / 100));
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Rows in table", yLabel: "Rows touched by access path",
    curves: [
      { label: "Full table scan O(n)", color: "#b45309",
        fn: function(n) { return n; } },
      { label: "Indexed range O(log n + k)", color: "#059669",
        fn: function(n) { return Math.max(1, Math.log(Math.max(2, n)) / Math.log(2)) + (n * sel); } }
    ],
    current: { x: currentRows },
    xSlider: "rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(1000, Math.min(10000000, xVal / 1000) * 1000)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = fullScanCost(c.rows, c.row_size, c.selectivity);
  document.getElementById("out-read").textContent = teachRuntime.formatInt(cost.fullRows);
  document.getElementById("out-match").textContent = teachRuntime.formatInt(cost.matched);
  document.getElementById("out-bytes").textContent = teachRuntime.formatBytes(cost.fullBytes);
  document.getElementById("out-pages").textContent = teachRuntime.formatInt(cost.pages) + " pages";
  document.getElementById("out-index").textContent = teachRuntime.formatInt(cost.indexRowsRead);
  document.getElementById("out-amp").textContent = cost.amplification.toFixed(1) + "×";
  document.getElementById("out-explanation").textContent =
    "With no usable index, the engine touches all " + teachRuntime.formatInt(cost.fullRows) +
    " rows (" + teachRuntime.formatBytes(cost.fullBytes) + "). If an index could isolate the same predicate, it would read about " +
    teachRuntime.formatInt(cost.indexRowsRead) + " rows/entries (" + cost.amplification.toFixed(1) + "× less work).";
  buildStage();
  resetStage();
  renderChart(c.row_size, c.selectivity, c.rows);
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
