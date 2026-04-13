"""Lesson: Nested Loop Join operator (single algorithm view).

Explains the classic nested-loop join shape shown in EXPLAIN plans:
an outer (driving) input and an inner probe repeated for each outer row.
"""
from .. import _html


_LESSON_JS = r"""
function nestedLoopCost(outerRows, innerProbeRows) {
  var outer = Math.max(1, outerRows);
  var probe = Math.max(1, innerProbeRows);
  var pairs = outer * probe;
  return {
    outerRows: outer,
    innerProbeRows: probe,
    comparedPairs: pairs
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
  innerLbl.textContent = "Inner probe: orders rows checked per customer";
  svg.appendChild(innerLbl);

  var outerRows = [];
  for (var i = 0; i < CUSTOMERS.length; i++) {
    var y = topY + i * rowH;
    var bg = anim.svgEl("rect", {
      x: leftX, y: y, width: colW, height: rowH - 8, rx: 7, ry: 7,
      fill: "#ffedd5", stroke: "#fdba74", "stroke-width": 1.5
    });
    svg.appendChild(bg);
    var txt = anim.svgEl("text", {
      x: leftX + 12, y: y + 24, "font-size": 11, "font-weight": 600, fill: "#7c2d12"
    });
    txt.textContent = "customer_id=" + CUSTOMERS[i].id + " " + CUSTOMERS[i].name;
    svg.appendChild(txt);
    outerRows.push({bg: bg, txt: txt, data: CUSTOMERS[i], y: y});
  }

  var innerPanel = anim.svgEl("rect", {
    x: rightX, y: topY, width: rightW, height: 240, rx: 8, ry: 8,
    fill: "#eff6ff", stroke: "#7dd3fc", "stroke-width": 1.5
  });
  svg.appendChild(innerPanel);

  var probeTitle = anim.svgEl("text", {
    x: rightX + 12, y: topY + 24, "font-size": 11.5, "font-weight": 700, fill: "#0c4a6e"
  });
  probeTitle.textContent = "Current probe set";
  svg.appendChild(probeTitle);

  var probeRows = [];
  for (var p = 0; p < 5; p++) {
    var pr = anim.svgEl("text", {
      x: rightX + 14, y: topY + 52 + p * 28, "font-size": 11, "font-weight": 600, fill: "#075985"
    });
    pr.textContent = "";
    svg.appendChild(pr);
    probeRows.push(pr);
  }

  var arrow = anim.svgEl("line", {
    x1: leftX + colW + 12, y1: topY + 18, x2: rightX - 12, y2: topY + 18,
    stroke: "#f59e0b", "stroke-width": 3, "stroke-linecap": "round", opacity: 0
  });
  svg.appendChild(arrow);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 18, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "#111827"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg,
    outerRows: outerRows,
    probeRows: probeRows,
    arrow: arrow,
    statusLbl: statusLbl
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.outerRows.length; i++) {
    stage.outerRows[i].bg.setAttribute("fill", "#ffedd5");
    stage.outerRows[i].bg.setAttribute("stroke", "#fdba74");
  }
  for (var p = 0; p < stage.probeRows.length; p++) stage.probeRows[p].textContent = "";
  stage.arrow.setAttribute("opacity", 0);
  stage.statusLbl.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var compared = 0;

  tl.mark("Drive outer rows");
  tl.call(function() {
    phase.textContent = "Phase 1/2 — the join drives one customer row at a time";
  });

  for (var i = 0; i < stage.outerRows.length; i++) {
    (function(idx) {
      tl.call(function() {
        for (var r = 0; r < stage.outerRows.length; r++) {
          stage.outerRows[r].bg.setAttribute("fill", "#ffedd5");
          stage.outerRows[r].bg.setAttribute("stroke", "#fdba74");
        }
        var row = stage.outerRows[idx];
        row.bg.setAttribute("fill", "#fde68a");
        row.bg.setAttribute("stroke", "#f59e0b");
        stage.arrow.setAttribute("opacity", 0.95);
        stage.arrow.setAttribute("y1", row.y + 20);
        stage.arrow.setAttribute("y2", row.y + 20);
        var c = row.data;
        phase.textContent = "Phase 2/2 — probing orders for customer " + c.name + " (id=" + c.id + ")";
        var orderIds = ORDERS_BY_CUSTOMER[c.id] || [];
        for (var p = 0; p < stage.probeRows.length; p++) stage.probeRows[p].textContent = "";
        for (var j = 0; j < Math.min(orderIds.length, stage.probeRows.length); j++) {
          stage.probeRows[j].textContent = "order_id=" + orderIds[j] + " (customer_id=" + c.id + ")";
        }
        compared += Math.max(1, orderIds.length);
        stage.statusLbl.textContent = "Outer row " + (idx + 1) + "/" + stage.outerRows.length +
          " drove " + Math.max(1, orderIds.length) + " inner comparisons (running total: " + compared + ")";
      });
      tl.delay(340);
    })(i);
  }

  tl.mark("Summary");
  tl.call(function() {
    phase.textContent = "Nested loop repeats inner probes for each outer row";
    stage.statusLbl.textContent = "This shape scales as outer_rows × inner_rows_per_probe";
  });
  tl.delay(360);
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
    xSliderTransform: function(xVal) { return Math.max(1000, Math.round(xVal / 1000) * 1000); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = nestedLoopCost(c.outer_rows, c.inner_rows);
  document.getElementById("out-outer").textContent = teachRuntime.formatInt(cost.outerRows);
  document.getElementById("out-inner").textContent = teachRuntime.formatInt(cost.innerProbeRows);
  document.getElementById("out-cmp").textContent = teachRuntime.formatInt(cost.comparedPairs);
  document.getElementById("out-explanation").textContent =
    "Nested loop join executes the inner probe for every outer row. With " +
    teachRuntime.formatInt(cost.outerRows) + " outer rows and ~" +
    teachRuntime.formatInt(cost.innerProbeRows) + " inner rows per probe, that is about " +
    teachRuntime.formatInt(cost.comparedPairs) + " row-pair comparisons. If the inner side is indexed and selective, "
    + "inner rows per probe stay small; without a good index, this operator can degrade quickly.";
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
"""


def render() -> str:
    controls_html = """
<section class="controls">
  <h2>Join shape controls</h2>
  <div class="control-grid">
    <div class="control">
      <label for="outer_rows">Outer rows (customers): <span class="value-pill" data-pill-for="outer_rows">50000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="1000" max="5000000" step="1000" value="50000">
      <div class="hint">The driving side: each of these rows triggers an inner probe.</div>
    </div>
    <div class="control">
      <label for="inner_rows">Inner rows per probe (orders): <span class="value-pill" data-pill-for="inner_rows">8</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="1" max="200" step="1" value="8">
      <div class="hint">Average orders rows checked for each customer row.</div>
    </div>
  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "SELECT c.customer_id, o.order_id\n"
            "FROM   customers c\n"
            "JOIN   orders o ON o.customer_id = c.customer_id\n"
            "WHERE  c.country = 'US';"
        ),
        note=(
            "This lesson isolates the Nested loop operator itself: one outer row is chosen, "
            "then the inner side is probed, and repeated. This is the exact background behavior behind EXPLAIN's Nested loop node."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left side is the outer (driving) customers input. One highlighted customer means one loop iteration.",
            "For each highlighted customer, the arrow moves to the orders side and shows the concrete rows checked in that probe.",
            "The operator repeats this pattern until all outer rows are consumed.",
            "Top-line cost is row-pair comparisons, which grows with outer_rows × inner_rows_per_probe (the operator's real background work).",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="nlj-svg" viewBox="0 0 800 380" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Nested loop cost model</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Outer rows {ht("Rows from the driving side of the join. The nested loop runs once per outer row.")}</p><p class="value" id="out-outer">—</p></div>
    <div class="item"><p class="label">Inner rows per probe {ht("Average rows checked on the inner side each time one outer row is processed.")}</p><p class="value" id="out-inner">—</p></div>
    <div class="item"><p class="label">Row-pair comparisons {ht("Approximate work for this operator: outer_rows × inner_rows_per_probe.")}</p><p class="value" id="out-cmp">—</p></div>
    <div class="item"><p class="label">Complexity {ht("Nested loop cost grows multiplicatively with both inputs.")}</p><p class="value">O(n · m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Nested loop growth curve (log-log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — why this operator can get expensive</summary>
  <div class="body">
    <p>The Nested loop operator itself is simple: pick one outer row and run an inner probe.
    The expensive part is repetition. If either side grows, total row-pair checks grow quickly.</p>
    <p>If the inner probe is a selective index lookup, <code>inner_rows_per_probe</code> stays small and
    Nested loop can be very fast. If the inner side scans many rows per outer row, work explodes.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="nested_loop",
        title="Nested Loop Join — outer row drives inner probe",
        subtitle=(
            "Dedicated operator view for EXPLAIN's Nested loop nodes. "
            "Learn the driver/probe shape without mixing algorithms."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
    )
