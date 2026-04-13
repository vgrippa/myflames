"""Lesson: BNL vs hash join side-by-side with shared sliders.

Same query run by both algorithms -- customers x orders. The confusing
'10.10B total' label has been replaced with 'row-pair comparisons' and
the '...and N more blocks' footer now explicitly says what the additional
blocks would do.

Concrete sample data: the BNL panel shows named customers packed into
blocks scanning named orders. The hash panel shows customers hashed
into buckets by month, then orders probed through -- following the same
pattern as hash_join.py.
"""
from .. import _html
from .._cost_model import JOIN_BUFFER_SIZE_DEFAULT, MYSQL_BNL_REMOVED_IN


_LESSON_JS_TEMPLATE = r"""
function bnlCost(outer, inner, rowSize, jbs) {
  var rpb = Math.max(1, Math.floor(jbs / rowSize));
  var blocks = Math.max(1, Math.ceil(outer / rpb));
  return {rpb: rpb, blocks: blocks, cmp: blocks * inner * Math.min(rpb, outer)};
}
function hashCost(build, probe, rowSize, jbs) {
  var buildBytes = Math.floor(build * rowSize * 1.4);
  var fits = buildBytes <= jbs;
  var cmp = build + probe + (fits ? 0 : (build + probe));
  return {fits: fits, buildBytes: buildBytes, cmp: cmp};
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
    x: 16, y: 18, "font-size": 10, "font-weight": 700, fill: "#92400e"
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
      fill: "#fffbeb", stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: x + bw/2, y: 42, "text-anchor": "middle",
      "font-size": 9, "font-weight": 700, fill: "#92400e"
    });
    t.textContent = "B" + (i + 1);
    svg.appendChild(t);
    var cLbl = anim.svgEl("text", {
      x: x + bw/2, y: 56, "text-anchor": "middle",
      "font-size": 7, "font-weight": 600, fill: "#78350f"
    });
    cLbl.textContent = names.join(", ");
    svg.appendChild(cLbl);
    blocks.push({ rect: r, label: t, cx: x + bw/2, cy: 46, customers: blockCusts });
  }
  if (numBlocks > cap) {
    var more = anim.svgEl("text", {
      x: endX, y: 51, "text-anchor": "end",
      "font-size": 10, "font-weight": 600, fill: "#9ca3af"
    });
    more.textContent = "+" + (numBlocks - cap) + " more";
    svg.appendChild(more);
  }

  var innerY = 100, innerH = 70, innerX = 24, innerW = 352;
  var innerR = anim.svgEl("rect", {
    x: innerX, y: innerY, width: innerW, height: innerH, rx: 6, ry: 6,
    fill: "#f0f9ff", stroke: "#0284c7", "stroke-width": 1.5
  });
  svg.appendChild(innerR);
  var innerLbl = anim.svgEl("text", {
    x: innerX, y: innerY - 6, "font-size": 10, "font-weight": 700, fill: "#0c4a6e"
  });
  innerLbl.textContent = "orders (re-scanned per block)";
  svg.appendChild(innerLbl);

  var sweep = anim.svgEl("rect", {
    x: innerX, y: innerY, width: 4, height: innerH,
    fill: "#ca8a04", opacity: 0
  });
  svg.appendChild(sweep);

  var status = anim.svgEl("text", {
    x: 200, y: 195, "text-anchor": "middle",
    "font-size": 9, "font-weight": 600, fill: "#374151"
  });
  status.textContent = "";
  svg.appendChild(status);

  var counter = anim.svgEl("text", {
    x: 200, y: 220, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#1f2937", "font-variant-numeric": "tabular-nums"
  });
  counter.textContent = "0 row-pair comparisons";
  svg.appendChild(counter);

  bnlStage = {
    svg: svg, blocks: blocks, sweep: sweep, status: status, counter: counter,
    innerX: innerX, innerY: innerY, innerW: innerW, innerH: innerH,
    shown: cap, totalBlocks: numBlocks
  };
}
function resetBnlStage() {
  if (!bnlStage) return;
  bnlStage.blocks.forEach(function(b) {
    b.rect.setAttribute("fill", "#fffbeb");
    b.rect.setAttribute("stroke", "#d1d5db");
    b.rect.setAttribute("stroke-width", 1);
  });
  bnlStage.sweep.setAttribute("opacity", 0);
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
    fill: "#fef3c7", stroke: "#d97706", "stroke-width": 1.5
  });
  svg.appendChild(buildRect);
  var buildLbl = anim.svgEl("text", {
    x: 51, y: 44, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#92400e"
  });
  buildLbl.textContent = "customers (build)";
  svg.appendChild(buildLbl);
  // List customer names inside build box
  for (var ci = 0; ci < BNL_CUSTOMERS.length; ci++) {
    var cTxt = anim.svgEl("text", {
      x: 51, y: 64 + ci * 14, "text-anchor": "middle",
      "font-size": 7, "font-weight": 600, fill: "#78350f"
    });
    cTxt.textContent = BNL_CUSTOMERS[ci].name + " (" + BNL_CUSTOMERS[ci].month + ")";
    svg.appendChild(cTxt);
  }

  // Hash table (middle) - 4 buckets by month
  var htX = 120, htY = 40, htW = 130, htH = 110;
  var htRect = anim.svgEl("rect", {
    x: htX, y: htY, width: htW, height: htH, rx: 6, ry: 6,
    fill: "#f3f4f6", stroke: "#6b7280", "stroke-width": 1
  });
  svg.appendChild(htRect);
  var htLbl = anim.svgEl("text", {
    x: htX + htW/2, y: htY - 6, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#1f2937"
  });
  htLbl.textContent = "Hash table (by month)";
  svg.appendChild(htLbl);
  var buckets = [];
  for (var i = 0; i < 4; i++) {
    var by = htY + 8 + i * 25;
    var br = anim.svgEl("rect", {
      x: htX + 8, y: by, width: htW - 16, height: 18, rx: 3, ry: 3,
      fill: "#ffffff", stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(br);
    var bIdx = anim.svgEl("text", {
      x: htX + 14, y: by + 12, "font-size": 7, "font-weight": 700, fill: "#9ca3af"
    });
    bIdx.textContent = "[" + i + "] " + HASH_MONTHS[i];
    svg.appendChild(bIdx);
    var bContent = anim.svgEl("text", {
      x: htX + htW/2 + 10, y: by + 12, "text-anchor": "middle",
      "font-size": 7, "font-weight": 600, fill: "#6b7280"
    });
    bContent.textContent = "";
    svg.appendChild(bContent);
    buckets.push({ rect: br, cx: htX + htW/2, cy: by + 9, contentLbl: bContent, contents: [] });
  }

  // Probe input (right) - order rows
  var probeRect = anim.svgEl("rect", {
    x: 284, y: 50, width: 100, height: 90, rx: 6, ry: 6,
    fill: "#ccfbf1", stroke: "#0d9488", "stroke-width": 1.5
  });
  svg.appendChild(probeRect);
  var probeLbl = anim.svgEl("text", {
    x: 334, y: 44, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#115e59"
  });
  probeLbl.textContent = "orders (probe)";
  svg.appendChild(probeLbl);
  for (var oi = 0; oi < BNL_ORDERS.length; oi++) {
    var oTxt = anim.svgEl("text", {
      x: 334, y: 64 + oi * 12, "text-anchor": "middle",
      "font-size": 7, "font-weight": 600, fill: "#134e4a"
    });
    oTxt.textContent = "#" + BNL_ORDERS[oi].id + " " + BNL_ORDERS[oi].month;
    svg.appendChild(oTxt);
  }

  var status = anim.svgEl("text", {
    x: 200, y: 175, "text-anchor": "middle",
    "font-size": 9, "font-weight": 600, fill: "#374151"
  });
  status.textContent = "";
  svg.appendChild(status);

  var counter = anim.svgEl("text", {
    x: 200, y: 220, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#065f46", "font-variant-numeric": "tabular-nums"
  });
  counter.textContent = "0 row comparisons";
  svg.appendChild(counter);

  hashStage = {
    svg: svg, buckets: buckets,
    buildCx: 51, buildCy: 95, probeCx: 334, probeCy: 95,
    status: status, counter: counter, tuples: []
  };
}
function resetHashStage() {
  if (!hashStage) return;
  hashStage.buckets.forEach(function(b) {
    b.rect.setAttribute("fill", "#ffffff");
    b.rect.setAttribute("stroke", "#d1d5db");
    b.contentLbl.textContent = "";
    b.contents = [];
  });
  hashStage.status.textContent = "";
  hashStage.counter.textContent = "0 row comparisons";
  hashStage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  hashStage.tuples = [];
}

function buildTimeline(bnlC, hashC, innerRows) {
  resetBnlStage();
  resetHashStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var bnlPhase = document.getElementById("bnl-phase");
  var hashPhase = document.getElementById("hash-phase");

  tl.mark("BNL: scan blocks");
  tl.call(function() {
    phase.textContent = "Running both algorithms on the same query\u2026";
    bnlPhase.textContent = "Walking through customers blocks";
    hashPhase.textContent = "Building hash table by month";
  });

  var shown = bnlStage.shown;
  var perBlockDur = 420;
  var bnlRunning = 0;

  // BNL side - sweep with block names
  for (var i = 0; i < shown; i++) {
    (function(idx) {
      var block = bnlStage.blocks[idx];
      var custNames = [];
      for (var cn = 0; cn < block.customers.length; cn++) {
        custNames.push(block.customers[cn].name);
      }
      tl.call(function() {
        block.rect.setAttribute("fill", "#fde725");
        block.rect.setAttribute("stroke", "#ca8a04");
        block.rect.setAttribute("stroke-width", 2);
        bnlStage.status.textContent = "Block " + (idx + 1) + " (" + custNames.join(", ") + ") scanning orders\u2026";
      });
      tl.add({
        from: bnlStage.innerX, to: bnlStage.innerX + bnlStage.innerW,
        duration: perBlockDur, ease: anim.easeInOutCubic,
        onUpdate: function(x) {
          bnlStage.sweep.setAttribute("x", x);
          bnlStage.sweep.setAttribute("opacity", 0.85);
          var progressThroughScan = (x - bnlStage.innerX) / bnlStage.innerW;
          bnlStage.counter.textContent =
            teachRuntime.formatInt(bnlRunning + innerRows * progressThroughScan) + " row-pair comparisons";
        },
        onComplete: function() {
          bnlStage.sweep.setAttribute("opacity", 0);
          block.rect.setAttribute("fill", "#e5e7eb");
          block.rect.setAttribute("stroke", "#9ca3af");
          block.rect.setAttribute("stroke-width", 1);
          bnlRunning += innerRows;
          bnlStage.counter.textContent = teachRuntime.formatInt(bnlRunning) + " row-pair comparisons";
        }
      });
    })(i);
  }
  tl.call(function() {
    bnlStage.counter.textContent = teachRuntime.formatInt(bnlC.cmp) + " row-pair comparisons (total)";
    if (bnlStage.totalBlocks > shown) {
      bnlStage.status.textContent = "\u2026and " + (bnlStage.totalBlocks - shown) + " more blocks \u2014 each one is another full scan of orders";
    }
    bnlPhase.textContent = "\u2713 Done \u2014 " + bnlStage.totalBlocks + " inner rescans";
  });

  var totalBnlDur = shown * perBlockDur + 700;
  var buildDur = totalBnlDur * 0.35;
  var probeDur = totalBnlDur * 0.55;

  // Hash side - build phase: hash each customer by month into a bucket
  tl.call(function() {
    for (var b = 0; b < BNL_CUSTOMERS.length; b++) {
      (function(bi) {
        var cust = BNL_CUSTOMERS[bi];
        var bucketIdx = monthBucket(cust.month);
        setTimeout(function() {
          var bucket = hashStage.buckets[bucketIdx];
          hashPhase.textContent = "Build: " + cust.name + " (" + cust.month + ") \u2192 bucket [" + bucketIdx + "]";
          var tuple = anim.svgEl("circle", {
            cx: hashStage.buildCx, cy: hashStage.buildCy, r: 4,
            fill: "#d97706", stroke: "#78350f", "stroke-width": 1, opacity: 0
          });
          hashStage.svg.appendChild(tuple);
          hashStage.tuples.push(tuple);
          var pathFn = anim.path(
            hashStage.buildCx, hashStage.buildCy,
            (hashStage.buildCx + bucket.cx) / 2, bucket.cy - 20,
            bucket.cx, bucket.cy
          );
          anim.tween({
            from: 0, to: 1, duration: 160, ease: anim.easeOutCubic,
            onUpdate: function(v) { tuple.setAttribute("opacity", v); }
          });
          anim.tween({
            from: 0, to: 1, duration: buildDur / 5, ease: anim.easeInOutQuad,
            onUpdate: function(t) {
              var p = pathFn(t);
              tuple.setAttribute("cx", p.x);
              tuple.setAttribute("cy", p.y);
            },
            onComplete: function() {
              bucket.rect.setAttribute("fill", "#fde725");
              bucket.rect.setAttribute("stroke", "#ca8a04");
              bucket.contents.push(cust.name);
              bucket.contentLbl.textContent = bucket.contents.join(", ");
              bucket.contentLbl.setAttribute("fill", "#1f2937");
              anim.tween({
                from: 1, to: 0, duration: 140, ease: anim.easeInCubic,
                onUpdate: function(v) { tuple.setAttribute("opacity", v); }
              });
            }
          });
        }, bi * (buildDur / 6));
      })(b);
    }
    // Probe phase after build completes
    setTimeout(function() {
      hashPhase.textContent = "Probing orders through hash table";
      for (var j = 0; j < BNL_ORDERS.length; j++) {
        (function(jj) {
          var order = BNL_ORDERS[jj];
          var bucketIdx = monthBucket(order.month);
          setTimeout(function() {
            var bucket = hashStage.buckets[bucketIdx];
            hashStage.status.textContent = "Probing: order #" + order.id + " (" + order.month + ") \u2192 bucket [" + bucketIdx + "]";
            var tuple = anim.svgEl("circle", {
              cx: hashStage.probeCx, cy: hashStage.probeCy, r: 4,
              fill: "#0d9488", stroke: "#134e4a", "stroke-width": 1, opacity: 0
            });
            hashStage.svg.appendChild(tuple);
            hashStage.tuples.push(tuple);
            var pathFn = anim.path(
              hashStage.probeCx, hashStage.probeCy,
              (hashStage.probeCx + bucket.cx) / 2, bucket.cy - 20,
              bucket.cx, bucket.cy
            );
            anim.tween({
              from: 0, to: 1, duration: 140, ease: anim.easeOutCubic,
              onUpdate: function(v) { tuple.setAttribute("opacity", v); }
            });
            anim.tween({
              from: 0, to: 1, duration: probeDur / 6, ease: anim.easeInOutQuad,
              onUpdate: function(t) {
                var p = pathFn(t);
                tuple.setAttribute("cx", p.x);
                tuple.setAttribute("cy", p.y);
                var progress = (jj + t) / BNL_ORDERS.length;
                hashStage.counter.textContent =
                  teachRuntime.formatInt(hashC.cmp * progress) + " row comparisons";
              },
              onComplete: function() {
                anim.pulse(bucket.rect, 2.5, 1, 220);
                anim.tween({
                  from: 1, to: 0, duration: 140, ease: anim.easeInCubic,
                  onUpdate: function(v) { tuple.setAttribute("opacity", v); }
                });
              }
            });
          }, jj * (probeDur / 8));
        })(j);
      }
      setTimeout(function() {
        hashStage.counter.textContent = teachRuntime.formatInt(hashC.cmp) + " row comparisons (total)";
        hashPhase.textContent = "\u2713 Done \u2014 single pass, " + teachRuntime.formatInt(hashC.cmp) + " row comparisons";
      }, probeDur + 300);
    }, buildDur + 200);
  });
  tl.delay(totalBnlDur + 300);
  tl.mark("Compare results");
  tl.call(function() {
    var speedup = hashC.cmp > 0 ? bnlC.cmp / hashC.cmp : 1;
    if (speedup >= 10) {
      phase.textContent = "Hash beat BNL by " + speedup.toFixed(0) + "\u00d7 \u2014 this is why MySQL 8.4 removed BNL.";
    } else if (speedup >= 2) {
      phase.textContent = "Hash was " + speedup.toFixed(1) + "\u00d7 less work \u2014 try raising customers for a bigger gap.";
    } else {
      phase.textContent = "At tiny sizes both algorithms are comparable; raise customers/orders.";
    }
  });
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
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
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
      { label: "BNL: O(cust\u00b7ord/buf) = O(n\u00b7m/b)", color: "#ca8a04",
        fn: function(n) { return bnlCost(n, innerRows, rowSize, jbs).cmp; } },
      { label: "Hash: O(cust+ord) = O(n+m)", color: "#0d9488",
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
"""


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Shared parameters — both panels update together</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows"><code>customers</code> rows: <span class="value-pill" data-pill-for="outer_rows">50000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="1000" max="5000000" step="1000" value="50000">
    </div>

    <div class="control">
      <label for="inner_rows"><code>orders</code> rows: <span class="value-pill" data-pill-for="inner_rows">200000</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="1000" max="10000000" step="1000" value="200000">
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="8192" max="16777216" step="8192" value="{JOIN_BUFFER_SIZE_DEFAULT}">
      <div class="hint">Default is 256 KiB in both engines.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- The same non-indexed join run by both engines\n"
            "SELECT c.country, SUM(o.total) AS revenue\n"
            "FROM   customers c\n"
            "JOIN   orders    o  ON  c.signup_month = o.signup_month\n"
            "GROUP  BY c.country;   -- no index on signup_month"
        ),
        note="MariaDB 11.x runs this with BNL (join_cache_level=2). MySQL 8.4 runs it with a two-phase hash join. Same SQL, very different cost."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left panel — MariaDB BNL: customers rows pack into blocks; a yellow sweep bar crosses the orders table once per block. One sweep = one full scan of orders.",
            "Right panel — MySQL 8.4 hash join: orange build-tuples fly into hash buckets (phase 1), then teal probe-tuples fly into those buckets (phase 2). Only one pass of each side.",
            "Both panels run the same input in parallel so you can feel the asymptotic difference — BNL grows quadratically with customers, hash grows linearly.",
            "Each panel has its own row-pair comparison counter. The ratio is shown above as 'Speedup (hash vs BNL)'. At small sizes it is close to 1×; crank the sliders and watch it grow.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start">
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#92400e;letter-spacing:0.4px;text-transform:uppercase">MariaDB 11.x BNL</p>
        <svg id="svg-bnl" viewBox="0 0 400 240" xmlns="http://www.w3.org/2000/svg"></svg>
        <p style="margin:4px 0 0;font-size:11px;color:#6b7280" id="bnl-phase">Idle</p>
      </div>
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#1e40af;letter-spacing:0.4px;text-transform:uppercase">MySQL 8.4 hash join</p>
        <svg id="svg-hash" viewBox="0 0 400 240" xmlns="http://www.w3.org/2000/svg"></svg>
        <p style="margin:4px 0 0;font-size:11px;color:#6b7280" id="hash-phase">Idle</p>
      </div>
    </div>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost comparison — row-pair comparisons</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">BNL row-pair comparisons {ht("Total (outer row, inner row) pairs checked by the BNL algorithm. Grows with blocks × inner_rows. This is the number that makes BNL expensive at scale.")}</p><p class="value" id="bnl-cmp">—</p></div>
    <div class="item"><p class="label">Hash row comparisons {ht("Total rows processed by the hash join: one read of the build side + one read of the probe side. Grows linearly — much flatter than BNL.")}</p><p class="value ok" id="hash-cmp">—</p></div>
    <div class="item"><p class="label">Speedup (hash vs BNL) {ht("How many times fewer row-pair comparisons the hash join needs compared to BNL. At large scale this is 100× to 10,000× — the whole reason MySQL removed BNL.")}</p><p class="value" id="speedup">—</p></div>
    <div class="item"><p class="label">BNL complexity {ht("BNL re-scans orders once per block of customers. The more blocks, the more rescans. Quadratic-ish growth.")}</p><p class="value">O(customers · orders / buffer) = O(n·m/b)</p></div>
    <div class="item"><p class="label">Hash complexity {ht("One pass through customers (build) + one pass through orders (probe). Linear growth no matter the size.")}</p><p class="value">O(customers + orders) = O(n + m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Row-pair comparisons vs customers rows (log–log, orders fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — isn't MariaDB's "hash join" the same thing?</summary>
  <div class="body">
    <p><strong>No.</strong> MariaDB 11.x can use <code>join_cache_level = 4</code>
    for "hashed BNL" — which is still structurally a Block Nested Loop.
    Each outer block builds a small hash table and the inner is scanned
    once per block. It's faster than plain BNL but still
    <code>O(outer_blocks · inner_rows)</code>.</p>

    <p>MySQL 8.4's hash join (and PostgreSQL's, and most analytics
    engines') is a two-phase algorithm: <em>build</em> a single in-memory
    hash table from the smaller input, then stream the larger input
    through once. That's <code>O(build + probe)</code>. Hash join has
    existed in MariaDB in a limited form since 10.4 but is not the
    default, and its heuristics are different from MySQL's.</p>

    <p>Takeaway: when you see "hash join" in a MariaDB EXPLAIN, check
    which <code>join_cache_level</code> is active. In MySQL 8.4 there's
    only one kind — <strong>BNL is gone</strong> (removed in
    {MYSQL_BNL_REMOVED_IN}).</p>

    <p>Sources: MariaDB Knowledge Base "Block-based Join Algorithms",
    "Hash Join Support". MySQL 8.4 Reference Manual §10.2.1.4.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="join",
        title="BNL vs hash join — side by side",
        subtitle=(
            "Move the sliders and feel the asymptotic difference between "
            "MariaDB's Block Nested Loop and MySQL 8.4's hash join."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
