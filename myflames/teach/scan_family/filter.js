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

// Stage geometry: input rows (left), the Filter operator (middle), rows
// passed to the parent (right). The probe pill carries the row's data from
// its input slot into the operator and on to the output (or down and out).
var IN_X = 20, BOX_Y = 48, BOX_W = 260, BOX_H = 260;
var OP_X = 320, OP_Y = 112, OP_W = 160, OP_H = 112;
var OUT_X = 520, OUT_W = 260;
var ROW_H = 30;
var PILL_W = 150, PILL_H = 20;
var OP_CX = OP_X + OP_W / 2, OP_CY = OP_Y + 40;
var DROP_Y = OP_Y + OP_H + 46;

function rowLabel(d) { return "order_id=" + d.id + " total=" + d.total; }
function inRowCenterY(i) { return BOX_Y + 10 + i * ROW_H + 12; }
function outRowY(i) { return BOX_Y + 10 + i * ROW_H; }

function buildStage() {
  var svg = document.getElementById("filter-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var inLbl = anim.svgEl("text", {x: IN_X, y: 28, "font-size": 13, "font-weight": 700, fill: "var(--svg-token-ink)"});
  inLbl.textContent = "child: table scan of orders";
  svg.appendChild(inLbl);
  svg.appendChild(anim.svgEl("rect", {
    x: IN_X, y: BOX_Y, width: BOX_W, height: BOX_H, rx: 8, ry: 8,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token-soft)", "stroke-width": 1.5
  }));

  var opLbl = anim.svgEl("text", {x: OP_CX, y: 28, "text-anchor": "middle", "font-size": 13, "font-weight": 700, fill: "var(--svg-build-ink)"});
  opLbl.textContent = "Filter: total > 500";
  svg.appendChild(opLbl);
  var opRect = anim.svgEl("rect", {
    x: OP_X, y: OP_Y, width: OP_W, height: OP_H, rx: 8, ry: 8,
    fill: "var(--svg-build-soft)", stroke: "var(--svg-build-soft)", "stroke-width": 1.5
  });
  svg.appendChild(opRect);
  var opTxt = anim.svgEl("text", {
    x: OP_CX, y: OP_Y + 18, "text-anchor": "middle",
    "font-size": 11, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  opTxt.textContent = "evaluate WHERE";
  svg.appendChild(opTxt);
  var verdict = anim.svgEl("text", {
    x: OP_CX, y: OP_Y + 70, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "var(--svg-ink)"
  });
  verdict.textContent = "";
  svg.appendChild(verdict);
  // Two short lines so the tally stays inside the operator box.
  var counter = anim.svgEl("text", {
    x: OP_CX, y: OP_Y + 90, "text-anchor": "middle",
    "font-size": 10, "font-weight": 600, fill: "var(--svg-ink-muted)"
  });
  svg.appendChild(counter);
  var counter2 = anim.svgEl("text", {
    x: OP_CX, y: OP_Y + 104, "text-anchor": "middle",
    "font-size": 10, "font-weight": 600, fill: "var(--svg-ink-muted)"
  });
  svg.appendChild(counter2);

  var outLbl = anim.svgEl("text", {x: OUT_X, y: 28, "font-size": 13, "font-weight": 700, fill: "var(--svg-ok)"});
  outLbl.textContent = "rows returned to the parent";
  svg.appendChild(outLbl);
  svg.appendChild(anim.svgEl("rect", {
    x: OUT_X, y: BOX_Y, width: OUT_W, height: BOX_H, rx: 8, ry: 8,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok-soft)", "stroke-width": 1.5
  }));

  svg.appendChild(anim.svgEl("path", {
    d: "M285,168 L318,168 M312,162 L318,168 L312,174",
    stroke: "var(--svg-node-stroke)", "stroke-width": 2, fill: "none"
  }));
  svg.appendChild(anim.svgEl("path", {
    d: "M482,168 L518,168 M512,162 L518,168 L512,174",
    stroke: "var(--svg-node-stroke)", "stroke-width": 2, fill: "none"
  }));
  var dropLbl = anim.svgEl("text", {
    x: OP_CX, y: DROP_Y + 26, "text-anchor": "middle",
    "font-size": 10, "font-weight": 600, fill: "var(--svg-danger)"
  });
  dropLbl.textContent = "dropped rows";
  svg.appendChild(dropLbl);

  var inRows = [];
  var outRows = [];
  for (var i = 0; i < SAMPLE.length; i++) {
    var y = BOX_Y + 10 + i * ROW_H;
    var r = anim.svgEl("rect", {
      x: IN_X + 8, y: y, width: BOX_W - 16, height: 24, rx: 5, ry: 5,
      fill: "var(--svg-token-soft)", stroke: "var(--svg-token-soft)", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: IN_X + 16, y: y + 16, "font-size": 11, "font-weight": 600, fill: "var(--svg-token-ink)"
    });
    t.textContent = rowLabel(SAMPLE[i]);
    svg.appendChild(t);
    inRows.push({bg: r, txt: t, data: SAMPLE[i]});

    var o = anim.svgEl("text", {
      x: OUT_X + 16, y: outRowY(i) + 16, "font-size": 11, "font-weight": 600, fill: "var(--svg-ok)"
    });
    o.textContent = "";
    svg.appendChild(o);
    outRows.push(o);
  }

  // The row in flight: a labelled pill that carries the row's data.
  var pill = anim.svgEl("g", {opacity: 0});
  var pillBg = anim.svgEl("rect", {
    x: -PILL_W / 2, y: -PILL_H / 2, width: PILL_W, height: PILL_H, rx: 10, ry: 10,
    fill: "var(--svg-token)", stroke: "var(--svg-ink)", "stroke-width": 1
  });
  pill.appendChild(pillBg);
  var pillTxt = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle", "font-size": 10, "font-weight": 700, fill: "var(--svg-on-solid)"
  });
  pill.appendChild(pillTxt);
  svg.appendChild(pill);

  var status = anim.svgEl("text", {
    x: W / 2, y: H - 16, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "var(--svg-ink)"
  });
  status.textContent = "";
  svg.appendChild(status);

  stage = {rows: inRows, outRows: outRows, status: status, opRect: opRect,
           verdict: verdict, counter: counter, counter2: counter2, pill: pill, pillBg: pillBg, pillTxt: pillTxt};
}

function movePill(x, y, opacity) {
  stage.pill.setAttribute("transform", "translate(" + x.toFixed(1) + "," + y.toFixed(1) + ")");
  stage.pill.setAttribute("opacity", String(opacity));
}

function setCounter(evaluated, passed) {
  stage.counter.textContent = "tested " + evaluated + " of " + SAMPLE.length;
  stage.counter2.textContent = "passed " + passed + " · dropped " + (evaluated - passed);
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.rows.length; i++) {
    stage.rows[i].bg.setAttribute("fill", "var(--svg-token-soft)");
    stage.rows[i].bg.setAttribute("stroke", "var(--svg-token-soft)");
    stage.outRows[i].textContent = "";
  }
  stage.opRect.setAttribute("stroke", "var(--svg-build-soft)");
  stage.verdict.textContent = "";
  setCounter(0, 0);
  stage.pill.setAttribute("opacity", 0);
  stage.status.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var n = stage.rows.length;

  // Guided steps: starting the child scan, one per row tested, then the
  // summary. Kinds map to the simplified-code lines (GUIDE in filter.py).
  tl.mark("Evaluate predicate");
  tl.beat("start");
  tl.call(function() {
    phase.textContent = "Phase 1/2: the filter asks its child, a table scan of orders, " +
      "for one row at a time and tests total > 500 on each";
    stage.status.textContent = "No index on total, so every row of orders reaches the filter.";
    setCounter(0, 0);
  });
  tl.delay(300);

  var passed = 0;
  for (var i = 0; i < n; i++) {
    (function(idx) {
      var d = stage.rows[idx].data;
      var startX = IN_X + BOX_W / 2, startY = inRowCenterY(idx);
      var passedBefore = passed;
      if (d.pass) passed += 1;
      var passedAfter = passed;
      var slot = passedBefore;  // output position for a passing row
      var endX = OUT_X + 16 + PILL_W / 2, endY = outRowY(slot) + 12;
      var toOp = anim.path(startX, startY, (startX + OP_CX) / 2, Math.min(startY, OP_CY) - 24, OP_CX, OP_CY);
      var toOut = anim.path(OP_CX, OP_CY, (OP_CX + endX) / 2, Math.min(OP_CY, endY) - 24, endX, endY);

      tl.beat(d.pass ? "row-pass" : "row-drop");
      tl.call(function() {
        stage.rows[idx].bg.setAttribute("fill", "var(--svg-highlight)");
        stage.rows[idx].bg.setAttribute("stroke", "var(--svg-build)");
        stage.pillTxt.textContent = rowLabel(d);
        stage.pillBg.setAttribute("fill", "var(--svg-token)");
        stage.opRect.setAttribute("stroke", "var(--svg-build)");
        stage.verdict.textContent = "total > 500 ?";
        stage.verdict.setAttribute("fill", "var(--svg-ink)");
        movePill(startX, startY, 1);
        phase.textContent = "Row " + (idx + 1) + " of " + n + ": the scan returns order_id=" +
          d.id + " (total=" + d.total + ") and the filter tests total > 500";
        stage.status.textContent = "Read row " + (idx + 1) + " of " + n + ": " + rowLabel(d);
      });
      tl.add({from: 0, to: 1, duration: 320, ease: anim.easeInOutCubic,
        onUpdate: function(t) { var p = toOp(t); movePill(p.x, p.y, 1); }});
      tl.call(function() {
        var holds = d.total + (d.pass ? " > 500: true ✓" : " > 500: false ✗");
        stage.verdict.textContent = holds;
        stage.verdict.setAttribute("fill", d.pass ? "var(--svg-ok)" : "var(--svg-danger)");
        stage.opRect.setAttribute("stroke", d.pass ? "var(--svg-ok)" : "var(--svg-danger)");
        stage.pillBg.setAttribute("fill", d.pass ? "var(--svg-ok)" : "var(--svg-danger)");
        setCounter(idx + 1, passedAfter);
        phase.textContent = d.pass
          ? "Row " + (idx + 1) + " of " + n + ": order_id=" + d.id + " has total=" + d.total +
            ", and " + d.total + " > 500 is true, so the filter returns it to its parent"
          : "Row " + (idx + 1) + " of " + n + ": order_id=" + d.id + " has total=" + d.total +
            ", and " + d.total + " > 500 is false, so the filter drops it (the scan already read it)";
      });
      tl.delay(160);
      if (d.pass) {
        tl.add({from: 0, to: 1, duration: 340, ease: anim.easeInOutCubic,
          onUpdate: function(t) { var p = toOut(t); movePill(p.x, p.y, 1); },
          onComplete: function() {
            movePill(endX, endY, 0);
            stage.outRows[slot].textContent = rowLabel(d);
            stage.rows[idx].bg.setAttribute("fill", "var(--svg-ok-soft)");
            stage.rows[idx].bg.setAttribute("stroke", "var(--svg-ok)");
            stage.status.textContent = "Passed: " + rowLabel(d) + " → returned to the parent (" +
              passedAfter + " so far)";
          }});
      } else {
        tl.add({from: 0, to: 1, duration: 300, ease: anim.easeInCubic,
          onUpdate: function(t) { movePill(OP_CX, OP_CY + (DROP_Y - OP_CY) * t, 1 - t); },
          onComplete: function() {
            movePill(OP_CX, DROP_Y, 0);
            stage.rows[idx].bg.setAttribute("fill", "var(--svg-danger-soft)");
            stage.rows[idx].bg.setAttribute("stroke", "var(--svg-danger)");
            stage.status.textContent = "Dropped: " + rowLabel(d) + " (read and tested, then thrown away)";
          }});
      }
      tl.delay(160);
    })(i);
  }

  tl.mark("Summary");
  tl.beat("done");
  tl.call(function() {
    var c = teachRuntime.readControls();
    var cost = filterCost(c.input_rows, c.selectivity);
    stage.opRect.setAttribute("stroke", "var(--svg-build-soft)");
    stage.verdict.textContent = "no rows left";
    stage.verdict.setAttribute("fill", "var(--svg-ink)");
    phase.textContent = "Phase 2/2: done. The filter tested all " + n + " sample rows and kept " +
      passed + "; at your settings it tests " + teachRuntime.formatInt(cost.inputRows) +
      " rows to keep " + teachRuntime.formatInt(cost.outRows) + " (" + (cost.sel * 100).toFixed(1) + "%)";
    stage.status.textContent = "The filter's work grows with the rows it reads, not with the rows it keeps.";
  });
  tl.delay(300);
  return tl;
}

function buildCurrentTimeline() { return buildTimeline(); }
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(selectivityPct, currentRows) {
  var sel = Math.max(0.1, Math.min(100, selectivityPct)) / 100;
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Input rows", yLabel: "Rows",
    curves: [
      {label: "Rows evaluated (filter work)", color: "var(--svg-build)", fn: function(n) { return n; }},
      {label: "Rows output (selectivity × n)", color: "var(--svg-ok)", fn: function(n) { return n * sel; }}
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
