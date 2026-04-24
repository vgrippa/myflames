var JOIN_BUFFER_SIZE_DEFAULT = %d;

function bnlCost(outer, inner, rowSize, jbs) {
  var rpb = Math.max(1, Math.floor(jbs / rowSize));
  var blocks = Math.max(1, Math.ceil(outer / rpb));
  var innerScans = blocks;
  var cmp = blocks * inner * Math.min(rpb, outer);
  return {rpb: rpb, blocks: blocks, innerScans: innerScans, cmp: cmp};
}

var W = 800, H = 360;
var stage = null;

// ---- Sample data the user can follow through the animation ----
var CUSTOMERS = [
  {id: 1, name: "Acme Corp", month: "Jan"},
  {id: 2, name: "Globex", month: "Mar"},
  {id: 3, name: "Initech", month: "Jan"},
  {id: 4, name: "Umbrella", month: "Feb"},
  {id: 5, name: "Stark Ind", month: "Mar"}
];
var ORDERS = [
  {id: 101, month: "Jan", total: "$2,400"},
  {id: 102, month: "Mar", total: "$1,800"},
  {id: 103, month: "Feb", total: "$3,100"},
  {id: 104, month: "Jan", total: "$950"},
  {id: 105, month: "Apr", total: "$4,200"},
  {id: 106, month: "Mar", total: "$2,700"},
  {id: 107, month: "Jan", total: "$1,100"},
  {id: 108, month: "Feb", total: "$890"}
];

function buildStage(numBlocks) {
  var svg = document.getElementById("bnl-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var headerOuter = anim.svgEl("text", {
    x: 20, y: 28, "font-size": 13, "font-weight": 700, fill: "#92400e"
  });
  headerOuter.textContent = "customers \u2192 join_buffer (" + numBlocks + " block" + (numBlocks === 1 ? "" : "s") + ")";
  svg.appendChild(headerOuter);

  // We show up to 3 blocks in the animation, each with customer names
  var cap = Math.min(numBlocks, 3);
  var gap = 10;
  var startX = 20;
  var avail = W - 40;
  var bw = (avail - gap * (cap - 1)) / cap;
  var blockRects = [];

  // Distribute CUSTOMERS into blocks (round-robin for the animation)
  var rpb = Math.max(1, Math.ceil(CUSTOMERS.length / cap));
  for (var i = 0; i < cap; i++) {
    var x = startX + i * (bw + gap);
    var blockCustomers = CUSTOMERS.slice(i * rpb, Math.min((i + 1) * rpb, CUSTOMERS.length));
    var names = [];
    for (var bc = 0; bc < blockCustomers.length; bc++) names.push(blockCustomers[bc].name);
    var rect = anim.svgEl("rect", {
      x: x, y: 42, width: bw, height: 56, rx: 6, ry: 6,
      fill: "#fffbeb", stroke: "#d1d5db", "stroke-width": 1.5
    });
    svg.appendChild(rect);
    var lbl = anim.svgEl("text", {
      x: x + bw/2, y: 62, "text-anchor": "middle",
      "font-size": 11, "font-weight": 700, fill: "#92400e"
    });
    lbl.textContent = "Block " + (i + 1);
    svg.appendChild(lbl);
    var contentLbl = anim.svgEl("text", {
      x: x + bw/2, y: 80, "text-anchor": "middle",
      "font-size": 9, "font-weight": 600, fill: "#78350f"
    });
    contentLbl.textContent = names.join(", ");
    svg.appendChild(contentLbl);
    blockRects.push({
      rect: rect, label: lbl, contentLbl: contentLbl,
      cx: x + bw/2, cy: 66,
      customers: blockCustomers
    });
  }
  if (numBlocks > cap) {
    var more = anim.svgEl("text", {
      x: W - 20, y: 80, "text-anchor": "end",
      "font-size": 11, "font-weight": 600, fill: "#9ca3af", "font-style": "italic"
    });
    more.textContent = "\u2026 and " + (numBlocks - cap) + " more blocks (each one is another full scan of orders)";
    svg.appendChild(more);
  }

  var innerY = 200;
  var innerH = 90;
  var innerX = 60;
  var innerW = W - 120;

  // Inner table header
  var innerLbl = anim.svgEl("text", {
    x: innerX, y: innerY - 8, "font-size": 13, "font-weight": 700, fill: "#0c4a6e"
  });
  innerLbl.textContent = "orders \u2014 re-scanned once per customers block";
  svg.appendChild(innerLbl);

  var innerRect = anim.svgEl("rect", {
    x: innerX, y: innerY, width: innerW, height: innerH, rx: 8, ry: 8,
    fill: "#f0f9ff", stroke: "#0284c7", "stroke-width": 1.5
  });
  svg.appendChild(innerRect);

  // Draw order rows inside the inner table
  var orderLabels = [];
  for (var j = 0; j < ORDERS.length; j++) {
    var ox = innerX + 16 + j * ((innerW - 32) / ORDERS.length);
    var oy = innerY + 12;
    var oLbl = anim.svgEl("text", {
      x: ox, y: oy + 12, "font-size": 8, "font-weight": 600, fill: "#0c4a6e",
      transform: "rotate(-35," + ox + "," + (oy + 12) + ")"
    });
    oLbl.textContent = "#" + ORDERS[j].id + " " + ORDERS[j].month;
    svg.appendChild(oLbl);
    orderLabels.push(oLbl);

    // Faint stripe
    var stripe = anim.svgEl("line", {
      x1: ox, y1: innerY + 28, x2: ox, y2: innerY + innerH - 8,
      stroke: "#dbeafe", "stroke-width": 1
    });
    svg.appendChild(stripe);
  }

  var sweep = anim.svgEl("rect", {
    x: innerX, y: innerY, width: 4, height: innerH,
    fill: "#ca8a04", opacity: 0
  });
  svg.appendChild(sweep);

  var statusLbl = anim.svgEl("text", {
    x: W/2, y: innerY + innerH + 30, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, blocks: blockRects, totalBlocks: numBlocks, sweep: sweep,
    innerX: innerX, innerY: innerY, innerW: innerW, innerH: innerH,
    statusLbl: statusLbl, tuples: [], orderLabels: orderLabels
  };
}

function resetStage() {
  if (!stage) return;
  stage.blocks.forEach(function(b) {
    b.rect.setAttribute("fill", "#fffbeb");
    b.rect.setAttribute("stroke", "#d1d5db");
    b.rect.setAttribute("stroke-width", 1.5);
  });
  stage.sweep.setAttribute("opacity", 0);
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function spawnLabeledTuple(text, color, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var tw = Math.max(84, text.length * 6 + 16);
  var bg = anim.svgEl("rect", {
    x: -tw/2, y: -9, width: tw, height: 18, rx: 9, ry: 9,
    fill: color, stroke: "#1f2937", "stroke-width": 1
  });
  g.appendChild(bg);
  var lbl = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle",
    "font-size": 8, "font-weight": 700, fill: "#ffffff"
  });
  lbl.textContent = text;
  g.appendChild(lbl);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

function buildTimeline(totalBlocksReal) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var shown = stage.blocks.length;

  for (var i = 0; i < shown; i++) {
    (function(idx) {
      var block = stage.blocks[idx];
      var custNames = [];
      for (var cn = 0; cn < block.customers.length; cn++) {
        custNames.push(block.customers[cn].name);
      }
      var blockLabel = "Block " + (idx + 1) + " (" + custNames.join(", ") + ")";

      tl.mark("Block " + (idx + 1) + ": scan orders");
      tl.call(function() {
        block.rect.setAttribute("fill", "#fde725");
        block.rect.setAttribute("stroke", "#ca8a04");
        block.rect.setAttribute("stroke-width", 2.5);
        phase.textContent = "Scanning orders for " + blockLabel;
      });

      // Spawn labeled tuples flying from the block to the inner table
      tl.call(function() {
        for (var k = 0; k < block.customers.length; k++) {
          (function(kk, cust) {
            var label = cust.name + " (" + cust.month + ")";
            var tuple = spawnLabeledTuple(label, "#ca8a04", block.cx, block.cy + 4);
            var targetX = stage.innerX + 40 + (kk * (stage.innerW - 80) / Math.max(1, block.customers.length));
            var targetY = stage.innerY + 20;
            var pathFn = anim.path(
              block.cx, block.cy + 4,
              (block.cx + targetX) / 2, (block.cy + targetY) / 2 - 40,
              targetX, targetY
            );
            setTimeout(function() {
              anim.tween({
                from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
                onUpdate: function(v) { tuple.setAttribute("opacity", v); }
              });
              anim.tween({
                from: 0, to: 1, duration: 640, ease: anim.easeInOutQuad,
                onUpdate: function(t) {
                  var p = pathFn(t);
                  tuple.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
                },
                onComplete: function() {
                  anim.tween({
                    from: 1, to: 0, duration: 240, ease: anim.easeInCubic,
                    onUpdate: function(v) { tuple.setAttribute("opacity", v); }
                  });
                }
              });
            }, kk * 100);
          })(k, block.customers[k]);
        }
      });

      // Sweep across the inner table with narration
      tl.add({
        from: stage.innerX, to: stage.innerX + stage.innerW,
        duration: 1200, ease: anim.easeInOutCubic,
        onUpdate: function(x) {
          stage.sweep.setAttribute("x", x);
          stage.sweep.setAttribute("opacity", 0.85);
          // Narrate which order is being compared
          var progress = (x - stage.innerX) / stage.innerW;
          var orderIdx = Math.min(ORDERS.length - 1, Math.floor(progress * ORDERS.length));
          var order = ORDERS[orderIdx];
          var matches = [];
          for (var mc = 0; mc < block.customers.length; mc++) {
            if (block.customers[mc].month === order.month) {
              matches.push(block.customers[mc].name);
            }
          }
          var matchStr = matches.length > 0
            ? " \u2014 " + matches.join(" & ") + " match!"
            : " \u2014 no match";
          stage.statusLbl.textContent = custNames.join("+") + " vs order #" + order.id +
            " (" + order.month + " " + order.total + ")" + matchStr;
        },
        onComplete: function() {
          stage.sweep.setAttribute("opacity", 0);
          block.rect.setAttribute("fill", "#e5e7eb");
          block.rect.setAttribute("stroke", "#9ca3af");
          block.rect.setAttribute("stroke-width", 1);
        }
      });
      tl.delay(200);
    })(i);
  }
  tl.mark("Done");
  tl.call(function() {
    phase.textContent = "\u2713 All " + totalBlocksReal + " block(s) done \u2014 " + totalBlocksReal + " full scans of orders";
    if (totalBlocksReal > shown) {
      stage.statusLbl.textContent = "\u2026plus " + (totalBlocksReal - shown) + " more blocks not drawn above, same pattern.";
    } else {
      stage.statusLbl.textContent = "Each block triggered one full scan of the orders table.";
    }
  });
  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var cost = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  return buildTimeline(cost.blocks);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function renderChart(innerRows, rowSize, jbs, currentOuter) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "customers row count", yLabel: "Row-pair comparisons",
    curves: [
      { label: "BNL: O(customers\u00b7orders/buf) = O(n\u00b7m/b)", color: "#ca8a04",
        fn: function(n) { return bnlCost(n, innerRows, rowSize, jbs).cmp; } },
      { label: "Indexed join: O(customers+orders) = O(n+m)", color: "#0d9488",
        fn: function(n) { return n + innerRows; } }
    ],
    current: { x: currentOuter },
    xSlider: "outer_rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(100, Math.min(1000000, xVal / 100) * 100)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  document.getElementById("out-rpb").textContent = teachRuntime.formatInt(cost.rpb);
  document.getElementById("out-blocks").textContent = teachRuntime.formatInt(cost.blocks);
  document.getElementById("out-scans").textContent = teachRuntime.formatInt(cost.innerScans);
  document.getElementById("out-cmp").textContent = teachRuntime.formatInt(cost.cmp);
  document.getElementById("out-explanation").textContent =
    "customers rows pack into " + cost.blocks + " block(s) of up to " + cost.rpb +
    " rows. orders is fully re-scanned once per block \u2014 " +
    cost.innerScans + " scan(s). Raise join_buffer_size \u2192 fewer blocks \u2192 fewer rescans.";
  buildStage(cost.blocks);
  resetStage();
  renderChart(c.inner_rows, c.row_size, c.jbs, c.outer_rows);
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
