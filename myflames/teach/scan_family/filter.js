function filterCost(inputRows, selectivityPct) {
  var sel = Math.max(0.1, Math.min(100, selectivityPct)) / 100;
  var outRows = Math.max(1, Math.floor(inputRows * sel));
  var dropped = Math.max(0, inputRows - outRows);
  return {inputRows: inputRows, outRows: outRows, dropped: dropped, sel: sel};
}

var W = 800, H = 390;
var stage = null;

var SAMPLE = [
  {id: 1001, total: 120, pass: false},
  {id: 1002, total: 510, pass: true},
  {id: 1003, total: 390, pass: false},
  {id: 1004, total: 920, pass: true},
  {id: 1005, total: 470, pass: false},
  {id: 1006, total: 760, pass: true},
  {id: 1007, total: 180, pass: false},
  {id: 1008, total: 640, pass: true}
];

function buildStage() {
  var svg = document.getElementById("filter-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var inX = 20, boxY = 48, boxW = 260, boxH = 260;
  var opX = 320, opW = 150;
  var outX = 520, outW = 260;

  var inLbl = anim.svgEl("text", {x: inX, y: 28, "font-size": 13, "font-weight": 700, fill: "#1e40af"});
  inLbl.textContent = "rows from child operator";
  svg.appendChild(inLbl);
  var inRect = anim.svgEl("rect", {
    x: inX, y: boxY, width: boxW, height: boxH, rx: 8, ry: 8,
    fill: "#eff6ff", stroke: "#93c5fd", "stroke-width": 1.5
  });
  svg.appendChild(inRect);

  var opLbl = anim.svgEl("text", {x: opX + 10, y: 28, "font-size": 13, "font-weight": 700, fill: "#7c2d12"});
  opLbl.textContent = "Filter: total > 500";
  svg.appendChild(opLbl);
  var opRect = anim.svgEl("rect", {
    x: opX, y: 120, width: opW, height: 80, rx: 8, ry: 8,
    fill: "#fff7ed", stroke: "#fdba74", "stroke-width": 1.5
  });
  svg.appendChild(opRect);
  var opTxt = anim.svgEl("text", {
    x: opX + opW/2, y: 166, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "#7c2d12"
  });
  opTxt.textContent = "evaluate WHERE";
  svg.appendChild(opTxt);

  var outLbl = anim.svgEl("text", {x: outX, y: 28, "font-size": 13, "font-weight": 700, fill: "#065f46"});
  outLbl.textContent = "rows passed to parent";
  svg.appendChild(outLbl);
  var outRect = anim.svgEl("rect", {
    x: outX, y: boxY, width: outW, height: boxH, rx: 8, ry: 8,
    fill: "#ecfdf5", stroke: "#86efac", "stroke-width": 1.5
  });
  svg.appendChild(outRect);

  var a1 = anim.svgEl("path", {
    d: "M285,164 C300,164 305,164 318,164 M312,158 L318,164 L312,170",
    stroke: "#9ca3af", "stroke-width": 2, fill: "none"
  });
  svg.appendChild(a1);
  var a2 = anim.svgEl("path", {
    d: "M472,164 C488,164 500,164 518,164 M512,158 L518,164 L512,170",
    stroke: "#9ca3af", "stroke-width": 2, fill: "none"
  });
  svg.appendChild(a2);

  var inRows = [];
  var outRows = [];
  for (var i = 0; i < SAMPLE.length; i++) {
    var y = boxY + 10 + i * 30;
    var r = anim.svgEl("rect", {
      x: inX + 8, y: y, width: boxW - 16, height: 24, rx: 5, ry: 5,
      fill: "#dbeafe", stroke: "#bfdbfe", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: inX + 16, y: y + 16, "font-size": 11, "font-weight": 600, fill: "#1e40af"
    });
    t.textContent = "order_id=" + SAMPLE[i].id + " total=" + SAMPLE[i].total;
    svg.appendChild(t);
    inRows.push({bg: r, txt: t, data: SAMPLE[i]});

    var o = anim.svgEl("text", {
      x: outX + 12, y: boxY + 24 + i * 26, "font-size": 11, "font-weight": 600, fill: "#065f46"
    });
    o.textContent = "";
    svg.appendChild(o);
    outRows.push(o);
  }

  var status = anim.svgEl("text", {
    x: W / 2, y: H - 16, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "#1f2937"
  });
  status.textContent = "";
  svg.appendChild(status);

  stage = {rows: inRows, outRows: outRows, status: status};
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.rows.length; i++) {
    stage.rows[i].bg.setAttribute("fill", "#dbeafe");
    stage.rows[i].bg.setAttribute("stroke", "#bfdbfe");
    stage.outRows[i].textContent = "";
  }
  stage.status.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var outIx = 0;

  tl.mark("Evaluate predicate");
  tl.call(function() {
    phase.textContent = "Phase 1/2 — evaluate WHERE predicate for each incoming row";
  });
  for (var i = 0; i < stage.rows.length; i++) {
    (function(idx) {
      tl.call(function() {
        var d = stage.rows[idx].data;
        stage.rows[idx].bg.setAttribute("fill", "#fde68a");
        stage.rows[idx].bg.setAttribute("stroke", "#f59e0b");
        if (d.pass) {
          stage.rows[idx].bg.setAttribute("fill", "#bbf7d0");
          stage.rows[idx].bg.setAttribute("stroke", "#22c55e");
          stage.outRows[outIx].textContent = "order_id=" + d.id + " total=" + d.total;
          outIx += 1;
          stage.status.textContent = "Row passes filter ✓";
        } else {
          stage.rows[idx].bg.setAttribute("fill", "#fecaca");
          stage.rows[idx].bg.setAttribute("stroke", "#ef4444");
          stage.status.textContent = "Row dropped ✗";
        }
      });
      tl.delay(220);
    })(i);
  }

  tl.mark("Summary");
  tl.call(function() {
    phase.textContent = "Phase 2/2 — filter complete: only matching rows continue";
  });
  return tl;
}

function buildCurrentTimeline() { return buildTimeline(); }
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

function renderChart(selectivityPct, currentRows) {
  var sel = Math.max(0.1, Math.min(100, selectivityPct)) / 100;
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Input rows", yLabel: "Rows",
    curves: [
      {label: "Rows evaluated (filter work)", color: "#b45309", fn: function(n) { return n; }},
      {label: "Rows output (selectivity × n)", color: "#059669", fn: function(n) { return n * sel; }}
    ],
    current: {x: currentRows},
    xSlider: "input_rows",
    xSliderTransform: function(x) { return Math.round(Math.max(1000, Math.min(10000000, x / 1000) * 1000)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = filterCost(c.input_rows, c.selectivity);
  document.getElementById("out-in").textContent = teachRuntime.formatInt(cost.inputRows);
  document.getElementById("out-out").textContent = teachRuntime.formatInt(cost.outRows);
  document.getElementById("out-drop").textContent = teachRuntime.formatInt(cost.dropped);
  document.getElementById("out-sel").textContent = (cost.sel * 100).toFixed(1) + "%";
  document.getElementById("out-exp").textContent =
    "Filter evaluates " + teachRuntime.formatInt(cost.inputRows) +
    " rows and keeps " + teachRuntime.formatInt(cost.outRows) +
    ". Predicate cost scales with input rows, not output rows.";
  buildStage();
  resetStage();
  renderChart(c.selectivity, c.input_rows);
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({build: buildCurrentTimeline, reset: resetAnim});
teachRuntime.wirePhaseNav("phase-nav", {build: buildCurrentTimeline, reset: resetAnim});
