function bnlCost(outer, inner, rowSize, jbs) {
  var rpb = Math.max(1, Math.floor(jbs / rowSize));
  var blocks = Math.max(1, Math.ceil(outer / rpb));
  return {rpb: rpb, blocks: blocks, cmp: blocks * inner * Math.min(rpb, outer)};
}
function hashCost(build, probe, rowSize, jbs) {
  var buildBytes = Math.floor(build * rowSize * 1.4);
  var fits = buildBytes <= jbs;
  var cmp = build + probe + (fits ? 0 : (build + probe));
  return {fits: fits, buildBytes: buildBytes, cmp: cmp, build: build, probe: probe};
}

// ---- Sample data for both panels ----
var BNL_CUSTOMERS = [
  {id: 1, name: "Acme Corp", month: "Jan"},
  {id: 2, name: "Globex", month: "Mar"},
  {id: 3, name: "Initech", month: "Jan"},
  {id: 4, name: "Umbrella", month: "Feb"},
  {id: 5, name: "Stark Ind", month: "Mar"}
];
var BNL_ORDERS = [
  {id: 101, month: "Jan", total: "$2,400"},
  {id: 102, month: "Mar", total: "$1,800"},
  {id: 103, month: "Feb", total: "$3,100"},
  {id: 104, month: "Jan", total: "$950"},
  {id: 105, month: "Apr", total: "$4,200"},
  {id: 106, month: "Mar", total: "$2,700"}
];
// Hash panel: customers hashed by month
var HASH_MONTHS = ["Jan", "Feb", "Mar", "Apr"];
function monthBucket(m) {
  for (var i = 0; i < HASH_MONTHS.length; i++) {
    if (HASH_MONTHS[i] === m) return i;
  }
  return 0;
}

var bnlStage = null;
function buildBnlPanel(numBlocks) {
  var svg = document.getElementById("svg-bnl");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var cap = Math.min(numBlocks, 3);
  var startX = 16, endX = 384;
  var gap = 6;
  var totalW = endX - startX;
  var bw = (totalW - gap * (cap - 1)) / cap;
  var blocks = [];
  var lbl = anim.svgEl("text", {
    x: 16, y: 18, "font-size": 10, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  lbl.textContent = "customers \u2192 blocks (" + numBlocks + " total)";
  svg.appendChild(lbl);

  // Distribute sample customers into blocks
  var rpb = Math.max(1, Math.ceil(BNL_CUSTOMERS.length / cap));
  for (var i = 0; i < cap; i++) {
    var x = startX + i * (bw + gap);
    var blockCusts = BNL_CUSTOMERS.slice(i * rpb, Math.min((i + 1) * rpb, BNL_CUSTOMERS.length));
    var names = [];
    for (var bc = 0; bc < blockCusts.length; bc++) names.push(blockCusts[bc].name);
    var r = anim.svgEl("rect", {
      x: x, y: 26, width: bw, height: 40, rx: 4, ry: 4,
      fill: "var(--svg-build-soft)", stroke: "var(--svg-node-stroke)", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: x + bw/2, y: 42, "text-anchor": "middle",
      "font-size": 9, "font-weight": 700, fill: "var(--svg-build-ink)"
    });
    t.textContent = "B" + (i + 1);
    svg.appendChild(t);
    var cLbl = anim.svgEl("text", {
      x: x + bw/2, y: 56, "text-anchor": "middle",
      "font-size": 7, "font-weight": 600, fill: "var(--svg-build-ink)"
    });
    cLbl.textContent = names.join(", ");
    svg.appendChild(cLbl);
    blocks.push({ rect: r, label: t, cx: x + bw/2, cy: 46, customers: blockCusts });
  }
  if (numBlocks > cap) {
    // On the header line, so it never covers the last block's names.
    var more = anim.svgEl("text", {
      x: endX, y: 18, "text-anchor": "end",
      "font-size": 10, "font-weight": 600, fill: "var(--svg-ink)"
    });
    more.textContent = "+" + (numBlocks - cap) + " more";
    svg.appendChild(more);
  }

  var innerY = 100, innerH = 70, innerX = 24, innerW = 352;
  var innerR = anim.svgEl("rect", {
    x: innerX, y: innerY, width: innerW, height: innerH, rx: 6, ry: 6,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token)", "stroke-width": 1.5
  });
  svg.appendChild(innerR);
  var innerLbl = anim.svgEl("text", {
    x: innerX, y: innerY - 6, "font-size": 10, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  innerLbl.textContent = "orders (re-scanned per block)";
  svg.appendChild(innerLbl);

  // The same orders rows the hash panel probes, one column each.
  var orderLabels = [];
  var colW = innerW / BNL_ORDERS.length;
  for (var oc = 0; oc < BNL_ORDERS.length; oc++) {
    var ol = anim.svgEl("text", {
      x: innerX + colW * (oc + 0.5), y: innerY + 32, "text-anchor": "middle",
      "font-size": 8, "font-weight": 600, fill: "var(--svg-token-ink)"
    });
    ol.textContent = "#" + BNL_ORDERS[oc].id;
    svg.appendChild(ol);
    var om = anim.svgEl("text", {
      x: innerX + colW * (oc + 0.5), y: innerY + 46, "text-anchor": "middle",
      "font-size": 8, "font-weight": 500, fill: "var(--svg-ink-muted)"
    });
    om.textContent = BNL_ORDERS[oc].month;
    svg.appendChild(om);
    orderLabels.push(ol);
  }

  var sweep = anim.svgEl("rect", {
    x: innerX, y: innerY, width: 4, height: innerH,
    fill: "var(--svg-build)", opacity: 0
  });
  svg.appendChild(sweep);

  var status = anim.svgEl("text", {
    x: 200, y: 195, "text-anchor": "middle",
    "font-size": 9, "font-weight": 600, fill: "var(--svg-ink)"
  });
  status.textContent = "";
  svg.appendChild(status);

  var counter = anim.svgEl("text", {
    x: 200, y: 220, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-ink)", "font-variant-numeric": "tabular-nums"
  });
  counter.textContent = "0 row-pair comparisons";
  svg.appendChild(counter);

  bnlStage = {
    svg: svg, blocks: blocks, sweep: sweep, status: status, counter: counter, orderLabels: orderLabels,
    innerX: innerX, innerY: innerY, innerW: innerW, innerH: innerH,
    shown: cap, totalBlocks: numBlocks
  };
}
function resetBnlStage() {
  if (!bnlStage) return;
  bnlStage.blocks.forEach(function(b) {
    b.rect.setAttribute("fill", "var(--svg-build-soft)");
    b.rect.setAttribute("stroke", "var(--svg-node-stroke)");
    b.rect.setAttribute("stroke-width", 1);
  });
  bnlStage.sweep.setAttribute("opacity", 0);
  bnlStage.orderLabels.forEach(function(l) { l.setAttribute("font-weight", 600); });
  bnlStage.status.textContent = "";
  bnlStage.counter.textContent = "0 row-pair comparisons";
}

var hashStage = null;
function buildHashPanel() {
  var svg = document.getElementById("svg-hash");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // Build input (left) - customer rows
  var buildRect = anim.svgEl("rect", {
    x: 10, y: 50, width: 82, height: 90, rx: 6, ry: 6,
    fill: "var(--svg-build-soft)", stroke: "var(--svg-build)", "stroke-width": 1.5
  });
  svg.appendChild(buildRect);
  var buildLbl = anim.svgEl("text", {
    x: 51, y: 44, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  buildLbl.textContent = "customers (build)";
  svg.appendChild(buildLbl);
  // List customer names inside build box
  var buildPos = [];
  for (var ci = 0; ci < BNL_CUSTOMERS.length; ci++) {
    buildPos.push({ cx: 51, cy: 61 + ci * 14 });
    var cTxt = anim.svgEl("text", {
      x: 51, y: 64 + ci * 14, "text-anchor": "middle",
      "font-size": 7, "font-weight": 600, fill: "var(--svg-build-ink)"
    });
    cTxt.textContent = BNL_CUSTOMERS[ci].name + " (" + BNL_CUSTOMERS[ci].month + ")";
    svg.appendChild(cTxt);
  }

  // Hash table (middle) - 4 buckets by month
  var htX = 120, htY = 40, htW = 130, htH = 110;
  var htRect = anim.svgEl("rect", {
    x: htX, y: htY, width: htW, height: htH, rx: 6, ry: 6,
    fill: "var(--svg-node-muted)", stroke: "var(--svg-node-stroke)", "stroke-width": 1
  });
  svg.appendChild(htRect);
  var htLbl = anim.svgEl("text", {
    x: htX + htW/2, y: htY - 6, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "var(--svg-ink)"
  });
  htLbl.textContent = "Hash table (by month)";
  svg.appendChild(htLbl);
  var buckets = [];
  for (var i = 0; i < 4; i++) {
    var by = htY + 8 + i * 25;
    var br = anim.svgEl("rect", {
      x: htX + 8, y: by, width: htW - 16, height: 18, rx: 3, ry: 3,
      fill: "var(--svg-node-bg)", stroke: "var(--svg-node-stroke)", "stroke-width": 1
    });
    svg.appendChild(br);
    var bIdx = anim.svgEl("text", {
      x: htX + 14, y: by + 12, "font-size": 7, "font-weight": 700, fill: "var(--svg-ink-muted)"
    });
    bIdx.textContent = "[" + i + "] " + HASH_MONTHS[i];
    svg.appendChild(bIdx);
    var bContent = anim.svgEl("text", {
      x: htX + 46, y: by + 12, "text-anchor": "start",
      "font-size": 7, "font-weight": 600, fill: "var(--svg-ink-muted)"
    });
    bContent.textContent = "";
    svg.appendChild(bContent);
    buckets.push({ rect: br, cx: htX + htW/2, cy: by + 9, contentLbl: bContent, contents: [] });
  }

  // Probe input (right) - order rows
  var probeRect = anim.svgEl("rect", {
    x: 284, y: 50, width: 100, height: 90, rx: 6, ry: 6,
    fill: "var(--svg-probe-soft)", stroke: "var(--svg-probe)", "stroke-width": 1.5
  });
  svg.appendChild(probeRect);
  var probeLbl = anim.svgEl("text", {
    x: 334, y: 44, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "var(--svg-probe)"
  });
  probeLbl.textContent = "orders (probe)";
  svg.appendChild(probeLbl);
  var probePos = [];
  for (var oi = 0; oi < BNL_ORDERS.length; oi++) {
    probePos.push({ cx: 334, cy: 61 + oi * 12 });
    var oTxt = anim.svgEl("text", {
      x: 334, y: 64 + oi * 12, "text-anchor": "middle",
      "font-size": 7, "font-weight": 600, fill: "var(--svg-probe)"
    });
    oTxt.textContent = "#" + BNL_ORDERS[oi].id + " " + BNL_ORDERS[oi].month;
    svg.appendChild(oTxt);
  }

  var status = anim.svgEl("text", {
    x: 200, y: 175, "text-anchor": "middle",
    "font-size": 9, "font-weight": 600, fill: "var(--svg-ink)"
  });
  status.textContent = "";
  svg.appendChild(status);

  var counter = anim.svgEl("text", {
    x: 200, y: 220, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-ok)", "font-variant-numeric": "tabular-nums"
  });
  counter.textContent = "0 row comparisons";
  svg.appendChild(counter);

  hashStage = {
    svg: svg, buckets: buckets,
    buildCx: 51, buildCy: 95, probeCx: 334, probeCy: 95,
    buildPos: buildPos, probePos: probePos,
    status: status, counter: counter, tuples: []
  };
}
function resetHashStage() {
  if (!hashStage) return;
  hashStage.buckets.forEach(function(b) {
    b.rect.setAttribute("fill", "var(--svg-node-bg)");
    b.rect.setAttribute("stroke", "var(--svg-node-stroke)");
    b.contentLbl.textContent = "";
    b.contents = [];
  });
  hashStage.status.textContent = "";
  hashStage.counter.textContent = "0 row comparisons";
  hashStage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  hashStage.tuples = [];
}

// Small labelled pill for the hash panel ("Acme Corp Jan", "#101 Jan").
function hashPill(text, color) {
  var g = anim.svgEl("g", { opacity: 0 });
  var w = Math.max(40, text.length * 4.2 + 10);
  g.appendChild(anim.svgEl("rect", {
    x: -w / 2, y: -6.5, width: w, height: 13, rx: 6.5, ry: 6.5,
    fill: color, stroke: "var(--svg-ink)", "stroke-width": 0.6
  }));
  var t = anim.svgEl("text", {
    x: 0, y: 2.6, "text-anchor": "middle",
    "font-size": 7, "font-weight": 700, fill: "var(--svg-on-solid)"
  });
  t.textContent = text;
  g.appendChild(t);
  hashStage.svg.appendChild(g);
  hashStage.tuples.push(g);
  return g;
}

// Labelled pills that leave their rows one after another and land in their
// month bucket, all driven by one progress value t (0..1) so stepping and
// scrubbing draw exactly what playing draws. Returns how many have landed.
var HASH_FLY = 0.45;  // share of the slice one pill's flight takes
function flyHashPills(items, t, onLand) {
  var n = items.length;
  var stagger = n > 1 ? (1 - HASH_FLY) / (n - 1) : 0;
  var landed = 0;
  for (var i = 0; i < n; i++) {
    var it = items[i];
    // t = 1 lands every pill (no rounding left over at the end of a slice).
    var local = t >= 1 ? 1 : Math.max(0, Math.min(1, (t - i * stagger) / HASH_FLY));
    if (local <= 0) continue;
    if (!it.pill) it.pill = hashPill(it.text, it.color);
    var p = it.path(anim.easeInOutQuad(local));
    it.pill.setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")");
    it.pill.setAttribute("opacity", local >= 1 ? 0 : Math.min(1, local * 4).toFixed(2));
    if (local >= 1) {
      landed++;
      if (!it.landed) { it.landed = true; if (onLand) onLand(it); }
    }
  }
  return landed;
}

function hashItems(rows, posList, keyOf, textOf, color) {
  var items = [];
  for (var i = 0; i < rows.length; i++) {
    var b = hashStage.buckets[monthBucket(keyOf(rows[i]))];
    var from = posList[i];
    items.push({
      row: rows[i], bucket: b, text: textOf(rows[i]), color: color, pill: null, landed: false,
      path: anim.path(from.cx, from.cy, (from.cx + b.cx) / 2, Math.min(from.cy, b.cy) - 18, b.cx, b.cy)
    });
  }
  return items;
}

// The timeline's own clock for a slice: each moving part eases itself.
function clock(t) { return t; }
var SLICE_MS = 900;

// Guided-step kind for slice s (GUIDE in join_compare.py maps kinds to the
// simplified-code lines): which part of each join runs in that slice.
function sliceKind(s, shown) {
  if (s === 0) return "block-build";
  if (s === 1) return s < shown ? "block-probe" : "probe-only";
  return "block-only";
}

function buildTimeline(bnlC, hashC, innerRows) {
  resetBnlStage();
  resetHashStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var bnlPhase = document.getElementById("bnl-phase");
  var hashPhase = document.getElementById("hash-phase");

  var shown = bnlStage.shown;
  var slices = Math.max(shown, 2);
  // Row-pair comparisons one block costs in the lesson's model, so the
  // running counter adds up to the total shown at the end.
  var perBlock = bnlC.cmp / bnlC.blocks;
  var buildItems = hashItems(BNL_CUSTOMERS, hashStage.buildPos,
    function(c) { return c.month; }, function(c) { return c.name + " " + c.month; }, "var(--svg-build)");
  var probeItems = hashItems(BNL_ORDERS, hashStage.probePos,
    function(o) { return o.month; }, function(o) { return "#" + o.id + " " + o.month; }, "var(--svg-probe)");

  function blockText(idx) {
    var names = [];
    var custs = bnlStage.blocks[idx].customers;
    for (var i = 0; i < custs.length; i++) names.push(custs[i].name);
    return "block " + (idx + 1) + " (" + names.join(", ") + ")";
  }
  function bucketsFrom(items) {
    hashStage.buckets.forEach(function(b) { b.contents = []; });
    items.forEach(function(it) { if (it.landed) it.bucket.contents.push(it.row.name); });
    hashStage.buckets.forEach(function(b) {
      b.contentLbl.textContent = b.contents.join(", ");
      b.contentLbl.setAttribute("fill", "var(--svg-ink)");
      if (b.contents.length) {
        b.rect.setAttribute("fill", "var(--svg-highlight)");
        b.rect.setAttribute("stroke", "var(--svg-build)");
      }
    });
  }

  tl.mark("Both joins, side by side");
  tl.call(function() {
    phase.textContent = "Both engines run the same join on the same rows: watch the two counters";
    bnlPhase.textContent = "Walking through customers blocks";
    hashPhase.textContent = "Building hash table by month";
  });

  for (var s = 0; s < slices; s++) {
    (function(si) {
      var bnlRuns = si < shown;
      var block = bnlRuns ? bnlStage.blocks[si] : null;
      var bnlLast = si === shown - 1;
      // Guided steps: both joins advance together, one slice per step.
      tl.beat(sliceKind(si, shown));
      tl.call(function() {
        var left = bnlRuns
          ? (si === 0 ? "BNL scans orders for " : "BNL scans all of orders again for ") + blockText(si)
          : "BNL is already done after " + shown + " block" + (shown === 1 ? "" : "s");
        var right = si === 0 ? "the hash join puts all " + BNL_CUSTOMERS.length + " customers in month buckets"
          : si === 1 ? "the hash join probes all " + BNL_ORDERS.length + " orders, once each"
          : "the hash join is already done";
        phase.textContent = left + "; " + right;
        if (bnlRuns) {
          block.rect.setAttribute("fill", "var(--svg-highlight)");
          block.rect.setAttribute("stroke", "var(--svg-build)");
          block.rect.setAttribute("stroke-width", 2);
          bnlStage.status.textContent = blockText(si).charAt(0).toUpperCase() + blockText(si).slice(1) +
            " scanning orders…";
          bnlPhase.textContent = "Block " + (si + 1) + " of " + bnlStage.totalBlocks +
            (si === 0 ? ": the first full scan of orders" : ": one more full scan of orders");
        }
        if (si === 0) hashPhase.textContent = "Build: customers into buckets by month";
        if (si === 1) hashPhase.textContent = "Probe: each order looks in one bucket";
      });
      tl.add({
        from: 0, to: 1, duration: SLICE_MS, ease: clock,
        onUpdate: function(t) {
          if (bnlRuns) {
            var e = anim.easeInOutCubic(t);
            bnlStage.sweep.setAttribute("x", bnlStage.innerX + bnlStage.innerW * e);
            bnlStage.sweep.setAttribute("opacity", 0.85);
            bnlStage.counter.textContent =
              teachRuntime.formatInt(perBlock * (si + e)) + " row-pair comparisons";
            var cur = Math.min(BNL_ORDERS.length - 1, Math.floor(e * BNL_ORDERS.length));
            bnlStage.orderLabels.forEach(function(l, li) { l.setAttribute("font-weight", li === cur ? 800 : 600); });
          }
          if (si === 0) {
            var built = flyHashPills(buildItems, t, null);
            bucketsFrom(buildItems);
            hashStage.counter.textContent = teachRuntime.formatInt(hashC.build * t) + " row comparisons";
            if (built) {
              var c = buildItems[built - 1].row;
              hashStage.status.textContent = "Build: " + c.name + " (" + c.month + ") → bucket [" +
                monthBucket(c.month) + "]";
            }
          } else if (si === 1) {
            var probed = flyHashPills(probeItems, t, function(it) { anim.pulse(it.bucket.rect, 2.5, 1, 220); });
            hashStage.counter.textContent =
              teachRuntime.formatInt(hashC.build + hashC.probe * t) + " row comparisons";
            if (probed) {
              var o = probeItems[probed - 1];
              var hits = o.bucket.contents.length ? o.bucket.contents.join(", ") + " match" : "no customer, no match";
              hashStage.status.textContent = "Probe: order #" + o.row.id + " (" + o.row.month + ") → bucket [" +
                monthBucket(o.row.month) + "]: " + hits;
            }
          }
        },
        onComplete: function() {
          if (bnlRuns) {
            bnlStage.sweep.setAttribute("opacity", 0);
            bnlStage.orderLabels.forEach(function(l) { l.setAttribute("font-weight", 600); });
            block.rect.setAttribute("fill", "var(--svg-node-muted)");
            block.rect.setAttribute("stroke", "var(--svg-node-stroke)");
            block.rect.setAttribute("stroke-width", 1);
            bnlStage.counter.textContent = teachRuntime.formatInt(perBlock * (si + 1)) + " row-pair comparisons";
            bnlStage.status.textContent = blockText(si).charAt(0).toUpperCase() + blockText(si).slice(1) +
              " done: one full scan of orders";
          }
          if (bnlLast) {
            bnlStage.counter.textContent = teachRuntime.formatInt(bnlC.cmp) + " row-pair comparisons (total)";
            if (bnlStage.totalBlocks > shown) {
              bnlStage.status.textContent = "…and " + (bnlStage.totalBlocks - shown) +
                " more blocks, each one another full scan of orders";
            }
            bnlPhase.textContent = "✓ Done: " + bnlStage.totalBlocks + " full scan" +
              (bnlStage.totalBlocks === 1 ? "" : "s") + " of orders";
          }
          if (si === 1) {
            hashStage.counter.textContent = teachRuntime.formatInt(hashC.cmp) + " row comparisons (total)";
            hashPhase.textContent = "✓ Done: one pass each" + (hashC.fits ? "" : ", plus a spill pass") +
              ", " + teachRuntime.formatInt(hashC.cmp) + " row comparisons";
          }
        }
      });
      tl.delay(250);
    })(s);
  }

  tl.mark("Compare results");
  tl.beat("compare");
  tl.call(function() {
    var speedup = hashC.cmp > 0 ? bnlC.cmp / hashC.cmp : 1;
    if (speedup >= 10) {
      phase.textContent = "Hash did " + speedup.toFixed(0) + "× less work than BNL: this is why MySQL replaced BNL with hash join.";
    } else if (speedup >= 2) {
      phase.textContent = "Hash was " + speedup.toFixed(1) + "× less work; try raising customers for a bigger gap.";
    } else {
      phase.textContent = "At tiny sizes both algorithms are comparable; raise customers/orders.";
    }
  });
  tl.delay(600);
  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var bnlC = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  var hashC = hashCost(Math.min(c.outer_rows, c.inner_rows), Math.max(c.outer_rows, c.inner_rows), c.row_size, c.jbs);
  buildBnlPanel(bnlC.blocks);
  resetHashStage();
  return buildTimeline(bnlC, hashC, c.inner_rows);
}
function resetAnim() {
  resetBnlStage();
  resetHashStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
  document.getElementById("bnl-phase").textContent = "Idle";
  document.getElementById("hash-phase").textContent = "Idle";
}

function renderChart(innerRows, rowSize, jbs, currentOuter) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "customers row count", yLabel: "Row-pair comparisons",
    curves: [
      { label: "BNL: O(cust\u00b7ord/buf) = O(n\u00b7m/b)", color: "var(--svg-build)",
        fn: function(n) { return bnlCost(n, innerRows, rowSize, jbs).cmp; } },
      { label: "Hash: O(cust+ord) = O(n+m)", color: "var(--svg-probe)",
        fn: function(n) { return hashCost(Math.min(n, innerRows), Math.max(n, innerRows), rowSize, jbs).cmp; } }
    ],
    current: { x: currentOuter },
    xSlider: "outer_rows",
    xSliderTransform: function(xVal) { return Math.max(1000, Math.round(xVal / 1000) * 1000); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var bnlC = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  var build = Math.min(c.outer_rows, c.inner_rows);
  var probe = Math.max(c.outer_rows, c.inner_rows);
  var hashC = hashCost(build, probe, c.row_size, c.jbs);

  document.getElementById("bnl-cmp").textContent = teachRuntime.formatInt(bnlC.cmp);
  document.getElementById("hash-cmp").textContent = teachRuntime.formatInt(hashC.cmp);
  var speedup = hashC.cmp > 0 ? bnlC.cmp / hashC.cmp : 0;
  document.getElementById("speedup").textContent =
    isFinite(speedup) ? (speedup >= 2 ? speedup.toFixed(0) + "\u00d7" : speedup.toFixed(2) + "\u00d7") : "\u2014";

  var exp = "With these parameters: MariaDB BNL performs ~" + teachRuntime.formatInt(bnlC.cmp) +
    " row-pair comparisons (" + bnlC.blocks + " block(s) \u00d7 inner scans). " +
    "MySQL hash join performs " + teachRuntime.formatInt(hashC.cmp) +
    " row comparisons (one build + one probe" +
    (hashC.fits ? "" : ", plus a spill pass") + "). ";
  if (speedup >= 10) {
    exp += "At this scale hash join is ~" + speedup.toFixed(0) + "\u00d7 fewer comparisons \u2014 this is why MySQL 8.4 removed BNL.";
  } else if (speedup >= 2) {
    exp += "Hash is " + speedup.toFixed(1) + "\u00d7 less work even at this modest size.";
  } else {
    exp += "At tiny sizes the algorithms are comparable; at scale hash wins by orders of magnitude.";
  }
  document.getElementById("out-explanation").textContent = exp;

  buildBnlPanel(bnlC.blocks);
  buildHashPanel();
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
