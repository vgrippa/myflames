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
    x: 20, y: 28, "font-size": 13, "font-weight": 700, fill: "var(--svg-build-ink)"
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
      fill: "var(--svg-build-soft)", stroke: "var(--svg-node-stroke)", "stroke-width": 1.5
    });
    svg.appendChild(rect);
    var lbl = anim.svgEl("text", {
      x: x + bw/2, y: 62, "text-anchor": "middle",
      "font-size": 11, "font-weight": 700, fill: "var(--svg-build-ink)"
    });
    lbl.textContent = "Block " + (i + 1);
    svg.appendChild(lbl);
    var contentLbl = anim.svgEl("text", {
      x: x + bw/2, y: 80, "text-anchor": "middle",
      "font-size": 9, "font-weight": 600, fill: "var(--svg-build-ink)"
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
    // Under the blocks, so it never covers a block's customer names.
    var more = anim.svgEl("text", {
      x: W - 20, y: 116, "text-anchor": "end",
      "font-size": 11, "font-weight": 600, fill: "var(--svg-ink-muted)", "font-style": "italic"
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
    x: innerX, y: innerY - 8, "font-size": 13, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  innerLbl.textContent = "orders \u2014 re-scanned once per customers block";
  svg.appendChild(innerLbl);

  var innerRect = anim.svgEl("rect", {
    x: innerX, y: innerY, width: innerW, height: innerH, rx: 8, ry: 8,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token)", "stroke-width": 1.5
  });
  svg.appendChild(innerRect);

  // Draw order rows inside the inner table, one column per order, upright
  // so they stay readable (they used to be rotated and cross the border).
  var orderLabels = [];
  var colW = (innerW - 32) / ORDERS.length;
  for (var j = 0; j < ORDERS.length; j++) {
    var ox = innerX + 16 + j * colW;
    var oLbl = anim.svgEl("text", {
      x: ox + colW / 2, y: innerY + 38, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "var(--svg-token-ink)"
    });
    oLbl.textContent = "#" + ORDERS[j].id + " " + ORDERS[j].month;
    svg.appendChild(oLbl);
    orderLabels.push(oLbl);
    var oTot = anim.svgEl("text", {
      x: ox + colW / 2, y: innerY + 56, "text-anchor": "middle",
      "font-size": 9, "font-weight": 500, fill: "var(--svg-ink-muted)"
    });
    oTot.textContent = ORDERS[j].total;
    svg.appendChild(oTot);

    // Faint stripe between orders
    if (j > 0) {
      var stripe = anim.svgEl("line", {
        x1: ox, y1: innerY + 18, x2: ox, y2: innerY + innerH - 18,
        stroke: "var(--svg-token)", "stroke-width": 0.5, opacity: 0.35
      });
      svg.appendChild(stripe);
    }
  }

  var sweep = anim.svgEl("rect", {
    x: innerX, y: innerY, width: 4, height: innerH,
    fill: "var(--svg-build)", opacity: 0
  });
  svg.appendChild(sweep);

  // Where the buffered customers wait while orders is scanned.
  var bufLbl = anim.svgEl("text", {
    x: innerX, y: 132, "font-size": 11, "font-weight": 600,
    fill: "var(--svg-ink-muted)", opacity: 0
  });
  bufLbl.textContent = "In the join buffer now (each is compared with every order):";
  svg.appendChild(bufLbl);

  var statusLbl = anim.svgEl("text", {
    x: W/2, y: innerY + innerH + 30, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "var(--svg-ink)"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, blocks: blockRects, totalBlocks: numBlocks, sweep: sweep,
    innerX: innerX, innerY: innerY, innerW: innerW, innerH: innerH,
    statusLbl: statusLbl, bufLbl: bufLbl, tuples: [], orderLabels: orderLabels
  };
}

function resetStage() {
  if (!stage) return;
  stage.blocks.forEach(function(b) {
    b.rect.setAttribute("fill", "var(--svg-build-soft)");
    b.rect.setAttribute("stroke", "var(--svg-node-stroke)");
    b.rect.setAttribute("stroke-width", 1.5);
  });
  stage.orderLabels.forEach(function(l) {
    l.setAttribute("fill", "var(--svg-token-ink)");
    l.setAttribute("font-weight", 600);
  });
  stage.sweep.setAttribute("opacity", 0);
  stage.bufLbl.setAttribute("opacity", 0);
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function spawnLabeledTuple(text, color, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var tw = Math.max(84, text.length * 6 + 16);
  var bg = anim.svgEl("rect", {
    x: -tw/2, y: -9, width: tw, height: 18, rx: 9, ry: 9,
    fill: color, stroke: "var(--svg-ink)", "stroke-width": 1
  });
  g.appendChild(bg);
  var lbl = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle",
    "font-size": 8, "font-weight": 700, fill: "var(--svg-on-solid)"
  });
  lbl.textContent = text;
  g.appendChild(lbl);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

// The timeline's own clock for staggered moves: each pill eases itself.
function clock(t) { return t; }
var BUFFER_Y = 160;
var PILL_FLY = 480, PILL_STAGGER = 80;

// The block's customers leave the block one after another (100 ms apart) and
// line up above orders, where they stay while orders is scanned. One
// timeline tween drives every pill, so stepping and scrubbing show exactly
// the same picture as playing.
function flyBlockPills(tl, block, held) {
  var n = block.customers.length;
  var paths = [];
  for (var k = 0; k < n; k++) {
    var tx = stage.innerX + stage.innerW * (k + 0.5) / n;
    paths.push(anim.path(block.cx, block.cy + 4,
      (block.cx + tx) / 2, (block.cy + BUFFER_Y) / 2 - 30, tx, BUFFER_Y));
  }
  var total = PILL_FLY + (n - 1) * PILL_STAGGER;
  tl.add({
    from: 0, to: 1, duration: total, ease: clock,
    onUpdate: function(t) {
      var ms = t * total;
      for (var i = 0; i < n; i++) {
        var local = Math.max(0, Math.min(1, (ms - i * PILL_STAGGER) / PILL_FLY));
        if (local <= 0) continue;
        if (!held.pills[i]) {
          var c = block.customers[i];
          held.pills[i] = spawnLabeledTuple(c.name + " (" + c.month + ")", "var(--svg-build)",
            block.cx, block.cy + 4);
        }
        var p = paths[i](anim.easeInOutQuad(local));
        held.pills[i].setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")");
        held.pills[i].setAttribute("opacity", Math.min(1, local * 4).toFixed(2));
      }
    }
  });
}

function matchesFor(customers, order) {
  var names = [];
  for (var i = 0; i < customers.length; i++) {
    if (customers[i].month === order.month) names.push(customers[i].name);
  }
  return names;
}

function buildTimeline(totalBlocksReal) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var shown = stage.blocks.length;

  for (var i = 0; i < shown; i++) {
    (function(idx) {
      var block = stage.blocks[idx];
      var custNames = [], custLabels = [];
      for (var cn = 0; cn < block.customers.length; cn++) {
        custNames.push(block.customers[cn].name);
        custLabels.push(block.customers[cn].name + " (" + block.customers[cn].month + ")");
      }
      var pairs = 0;
      for (var o = 0; o < ORDERS.length; o++) pairs += matchesFor(block.customers, ORDERS[o]).length;
      var held = { pills: [] };

      tl.mark("Block " + (idx + 1) + ": scan orders");
      // Guided steps: filling the buffer, then one scan of orders per block.
      // Kinds map to the simplified code (GUIDE in bnl.py).
      tl.beat("fill");
      tl.call(function() {
        block.rect.setAttribute("fill", "var(--svg-highlight)");
        block.rect.setAttribute("stroke", "var(--svg-build)");
        block.rect.setAttribute("stroke-width", 2.5);
        stage.bufLbl.setAttribute("opacity", 1);
        phase.textContent = "Block " + (idx + 1) + " of " + totalBlocksReal +
          " fills the join buffer: " + custLabels.join(", ");
        stage.statusLbl.textContent = "join buffer = Block " + (idx + 1) + ": " + custNames.join(", ");
      });
      flyBlockPills(tl, block, held);
      tl.delay(150);

      tl.beat("scan");
      tl.call(function() {
        phase.textContent = "Block " + (idx + 1) + ": one full scan of orders compares each of the " +
          ORDERS.length + " orders with " + custNames.join(" and ");
      });
      // Sweep across the inner table with narration.
      tl.add({
        from: stage.innerX + 16, to: stage.innerX + stage.innerW - 16,
        duration: 1200, ease: anim.easeInOutCubic,
        onUpdate: function(x) {
          stage.sweep.setAttribute("x", x - 2);
          stage.sweep.setAttribute("opacity", 0.85);
          var progress = (x - stage.innerX - 16) / (stage.innerW - 32);
          var orderIdx = Math.min(ORDERS.length - 1, Math.floor(progress * ORDERS.length));
          var order = ORDERS[orderIdx];
          for (var l = 0; l < stage.orderLabels.length; l++) {
            var on = l === orderIdx;
            stage.orderLabels[l].setAttribute("fill", on ? "var(--svg-build-ink)" : "var(--svg-token-ink)");
            stage.orderLabels[l].setAttribute("font-weight", on ? 800 : 600);
          }
          var matches = matchesFor(block.customers, order);
          stage.statusLbl.textContent = custNames.join("+") + " vs order #" + order.id +
            " (" + order.month + " " + order.total + "): " +
            (matches.length ? matches.join(" & ") + " match ✓" : "no match");
        },
        onComplete: function() {
          stage.sweep.setAttribute("opacity", 0);
          for (var l = 0; l < stage.orderLabels.length; l++) {
            stage.orderLabels[l].setAttribute("fill", "var(--svg-token-ink)");
            stage.orderLabels[l].setAttribute("font-weight", 600);
          }
          block.rect.setAttribute("fill", "var(--svg-node-muted)");
          block.rect.setAttribute("stroke", "var(--svg-node-stroke)");
          block.rect.setAttribute("stroke-width", 1);
          stage.statusLbl.textContent = "Block " + (idx + 1) + " done: orders read once (" +
            ORDERS.length + " rows), " + pairs + " matching pair" + (pairs === 1 ? "" : "s");
        }
      });
      // The buffer empties for the next block.
      tl.add({
        from: 1, to: 0, duration: 180, ease: anim.easeInCubic,
        onUpdate: function(v) {
          held.pills.forEach(function(p) { if (p) p.setAttribute("opacity", v.toFixed(2)); });
          stage.bufLbl.setAttribute("opacity", v.toFixed(2));
        },
        onComplete: function() {
          held.pills.forEach(function(p) { if (p && p.parentNode) p.parentNode.removeChild(p); });
          held.pills = [];
        }
      });
      tl.delay(120);
    })(i);
  }
  tl.mark("Done");
  tl.beat("done");
  tl.call(function() {
    phase.textContent = "✓ All " + totalBlocksReal + " block" + (totalBlocksReal === 1 ? "" : "s") +
      " done: orders was scanned " + totalBlocksReal + " time" + (totalBlocksReal === 1 ? "" : "s") +
      ", once per block";
    if (totalBlocksReal > shown) {
      stage.statusLbl.textContent = "…plus " + (totalBlocksReal - shown) + " more blocks not drawn above, same pattern.";
    } else {
      stage.statusLbl.textContent = "Each block triggered one full scan of the orders table.";
    }
  });
  tl.delay(400);
  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var cost = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  return buildTimeline(cost.blocks);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(innerRows, rowSize, jbs, currentOuter) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "customers row count", yLabel: "Row-pair comparisons",
    curves: [
      { label: "BNL: O(customers\u00b7orders/buf) = O(n\u00b7m/b)", color: "var(--svg-build)",
        fn: function(n) { return bnlCost(n, innerRows, rowSize, jbs).cmp; } },
      { label: "Indexed join: O(customers+orders) = O(n+m)", color: "var(--svg-probe)",
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
