var SORT_BUFFER_SIZE_DEFAULT = %d;

function filesortCost(rows, rowSize, sbs, limitRows) {
  var rpr = Math.max(1, Math.floor(sbs / rowSize));
  var usePQ = (limitRows > 0 && limitRows < rows && limitRows * rowSize * 2 < sbs);
  if (usePQ) {
    // Priority queue: only limitRows kept in memory, no spill possible
    return {rpr: rpr, runs: 1, merges: 0, ioRows: rows, spill: false,
            sortAlg: "priority_queue", pqSize: limitRows};
  }
  var runs = Math.max(1, Math.ceil(rows / rpr));
  // Radix sort: fixed-length key ≤ 16 B → O(n·k) no comparisons
  var useRadix = (rowSize <= 16);
  var alg = useRadix ? "radix" : "introsort";
  if (runs <= 1) return {rpr: rpr, runs: 1, merges: 0, ioRows: rows, spill: false,
                         sortAlg: alg, pqSize: 0};
  var fanIn = 15;
  var merges = Math.max(1, Math.ceil(Math.log(runs) / Math.log(fanIn)));
  var ioRows = rows + merges * rows * 2;
  return {rpr: rpr, runs: runs, merges: merges, ioRows: ioRows, spill: true,
          sortAlg: alg, pqSize: 0};
}

var W = 800, H = 440;
var stage = null;

// Named orders so the user can track rows through the sort
var ORDERS = [
  {customer: "Alice",  date: "Jan 15", total: "$2,400"},
  {customer: "Bob",    date: "Mar 22", total: "$1,800"},
  {customer: "Carol",  date: "Feb 10", total: "$3,100"},
  {customer: "Dave",   date: "Jan 03", total: "$950"},
  {customer: "Eve",    date: "Apr 18", total: "$4,200"},
  {customer: "Frank",  date: "Mar 01", total: "$2,700"},
  {customer: "Grace",  date: "Jan 28", total: "$1,100"},
  {customer: "Hank",   date: "Feb 14", total: "$890"},
  {customer: "Iris",   date: "May 05", total: "$3,400"},
  {customer: "Jake",   date: "Apr 11", total: "$1,650"},
  {customer: "Kate",   date: "Feb 28", total: "$2,200"},
  {customer: "Leo",    date: "Mar 17", total: "$1,500"}
];

// Numeric sort keys for ORDER BY order_date (month*100 + day)
var DATE_KEYS = [115, 322, 210, 103, 418, 301, 128, 214, 505, 411, 228, 317];

// ---- Quicksort step recorder (runs at timeline-build time) ----
// Returns an array of ops: {type:'pivot'|'cmp'|'swap'|'placed'|'done', ...}
function recordQsOps(orderIndices) {
  var n = orderIndices.length;
  var pos = orderIndices.slice();
  var ops = [];
  function qs(lo, hi) {
    if (lo >= hi) {
      if (lo === hi) ops.push({type:"done", pos:lo, order:pos[lo]});
      return;
    }
    ops.push({type:"pivot", pos:hi, order:pos[hi], lo:lo, hi:hi});
    var i = lo;
    for (var j = lo; j < hi; j++) {
      var leq = DATE_KEYS[pos[j]] <= DATE_KEYS[pos[hi]];
      ops.push({type:"cmp", j:j, i:i, pivotPos:hi,
                orderJ:pos[j], orderP:pos[hi], leq:leq});
      if (leq) {
        if (i !== j) {
          ops.push({type:"swap", a:i, b:j, orderA:pos[i], orderB:pos[j]});
          var tmp = pos[i]; pos[i] = pos[j]; pos[j] = tmp;
        }
        i++;
      }
    }
    if (i !== hi) {
      ops.push({type:"swap", a:i, b:hi, orderA:pos[i], orderB:pos[hi]});
      var tmp = pos[i]; pos[i] = pos[hi]; pos[hi] = tmp;
    }
    ops.push({type:"placed", pos:i, order:pos[i]});
    qs(lo, i - 1);
    qs(i + 1, hi);
  }
  qs(0, n - 1);
  return ops;
}

// Small pointer triangle below the pills (i = partition boundary, j = scan)
function createQsPointer(label, color) {
  var g = anim.svgEl("g", {opacity: 0});
  var tri = anim.svgEl("polygon", {points: "0,-5 -4,3 4,3", fill: color});
  g.appendChild(tri);
  var txt = anim.svgEl("text", {
    x: 0, y: 13, "text-anchor": "middle",
    "font-size": 7, "font-weight": 700, fill: color
  });
  txt.textContent = label;
  g.appendChild(txt);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

// ---- Animate recorded quicksort ops on the timeline ----
// slotGs/slotXs: arrays indexed by position. slotGs[i] is the SVG <g> at
// position i; swaps permute slotGs in sync with the algorithm so closures
// always resolve the correct element at play-time.
function addQsToTimeline(tl, slotGs, slotXs, orderIndices, fast) {
  var ops = recordQsOps(orderIndices);
  var n = slotGs.length;
  var dur  = fast ? 200 : 350;
  var dly  = fast ? 120 : 200;
  var cmpD = fast ? 80  : 140;
  var UNSORTED    = "#7c3aed";
  var PIVOT_CLR   = "#ea580c";
  var LESS_CLR    = "#16a34a";
  var GREATER_CLR = "#dc2626";
  var PLACED_CLR  = "#059669";

  var ptrI = createQsPointer("i", "#2563eb");
  var ptrJ = createQsPointer("j", "#d97706");
  var bracket = anim.svgEl("line", {
    x1:0, y1:0, x2:0, y2:0,
    stroke:"#9ca3af", "stroke-width":1, "stroke-dasharray":"3,3", opacity:0
  });
  stage.svg.appendChild(bracket);
  stage.tuples.push(bracket);
  var placed = {};

  for (var oi = 0; oi < ops.length; oi++) {
    (function(op) {
      if (op.type === "pivot") {
        tl.call(function() {
          slotGs[op.pos].firstChild.setAttribute("fill", PIVOT_CLR);
          stage.bufContent.textContent = "Pivot: " + ORDERS[op.order].customer +
            " (" + ORDERS[op.order].date + ")";
          bracket.setAttribute("x1", slotXs[op.lo] - 30);
          bracket.setAttribute("y1", 92);
          bracket.setAttribute("x2", slotXs[op.hi] + 30);
          bracket.setAttribute("y2", 92);
          bracket.setAttribute("opacity", 0.5);
          ptrI.setAttribute("transform", "translate(" + slotXs[op.lo] + ",98)");
          ptrI.setAttribute("opacity", 1);
          ptrJ.setAttribute("opacity", 0);
        });
        tl.delay(dly * 2);
      }
      else if (op.type === "cmp") {
        tl.call(function() {
          ptrJ.setAttribute("transform", "translate(" + slotXs[op.j] + ",98)");
          ptrJ.setAttribute("opacity", 1);
          ptrI.setAttribute("transform", "translate(" + slotXs[op.i] + ",98)");
          if (!placed[op.j]) {
            slotGs[op.j].firstChild.setAttribute("fill",
              op.leq ? LESS_CLR : GREATER_CLR);
          }
          var sym = op.leq ? " \u2264 " : " > ";
          stage.bufContent.textContent = ORDERS[op.orderJ].date + sym +
            ORDERS[op.orderP].date +
            (op.leq ? " \u2192 swap zone grows" : "");
        });
        tl.delay(cmpD);
      }
      else if (op.type === "swap") {
        tl.call(function() {
          var ga = slotGs[op.a], gb = slotGs[op.b];
          var xa = slotXs[op.a],  xb = slotXs[op.b];
          anim.tween({from: 0, to: 1, duration: dur,
            ease: anim.easeInOutCubic,
            onUpdate: function(t) {
              var ax = xa + (xb - xa) * t;
              var ay = 75 - Math.sin(t * Math.PI) * 16;
              ga.setAttribute("transform", "translate(" + ax + "," + ay + ")");
              var bx = xb + (xa - xb) * t;
              var by = 75 + Math.sin(t * Math.PI) * 16;
              gb.setAttribute("transform", "translate(" + bx + "," + by + ")");
            }
          });
          stage.bufContent.textContent = "Swap: " + ORDERS[op.orderA].customer +
            " \u2194 " + ORDERS[op.orderB].customer;
          // Permute tracking array to stay in sync with algorithm
          var tmpG = slotGs[op.a];
          slotGs[op.a] = slotGs[op.b];
          slotGs[op.b] = tmpG;
        });
        tl.delay(dur + 60);
      }
      else if (op.type === "placed") {
        tl.call(function() {
          slotGs[op.pos].firstChild.setAttribute("fill", PLACED_CLR);
          placed[op.pos] = true;
          // Reset non-placed elements to unsorted purple
          for (var k = 0; k < n; k++) {
            if (!placed[k]) slotGs[k].firstChild.setAttribute("fill", UNSORTED);
          }
          stage.bufContent.textContent = ORDERS[op.order].customer +
            " (" + ORDERS[op.order].date + ") \u2192 final position";
        });
        tl.delay(dly);
      }
      else if (op.type === "done") {
        tl.call(function() {
          slotGs[op.pos].firstChild.setAttribute("fill", PLACED_CLR);
          placed[op.pos] = true;
        });
      }
    })(ops[oi]);
  }

  // Clean up pointers
  tl.call(function() {
    ptrI.setAttribute("opacity", 0);
    ptrJ.setAttribute("opacity", 0);
    bracket.setAttribute("opacity", 0);
    stage.bufContent.textContent = fast
      ? "Chunk sorted \u2714" : "Sorted by order_date \u2714";
  });
  tl.delay(fast ? 150 : 300);
}

// ---- Radix sort animation ----
// Simple and reliable: color each pill by its month group (pass 1),
// then slide all pills to their sorted positions (pass 2).
// No external bucket DOM — everything stays in the buffer zone.
var MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May"];
var BUCKET_CLRS = ["#2563eb", "#7c3aed", "#db2777", "#ea580c", "#ca8a04", "#059669"];

// Radix sort uses its own dedicated SVG (radix-svg) so it doesn't
// fight with the filesort stage's zones/arrows/z-order.
var radixPills = [];  // tracks pill elements inside radix-svg

function buildRadixTimeline(orderIndices, fast) {
  var rsvg = document.getElementById("radix-svg");
  var fsvg = document.getElementById("filesort-svg");
  // Show radix SVG, hide filesort SVG
  fsvg.style.display = "none";
  rsvg.style.display = "";
  while (rsvg.firstChild) rsvg.removeChild(rsvg.firstChild);
  radixPills = [];

  var n = Math.min(orderIndices.length, 8);
  var RW = 800, RH = 340;
  var dur = fast ? 250 : 420;
  var dly = fast ? 80 : 160;
  var PLACED = "#059669";
  var ROW_Y = 40;       // unsorted row at top
  var BUCKET_Y = 140;   // bucket boxes in middle
  var BUCKET_H = 54;
  var RESULT_Y = 260;   // sorted result row at bottom

  // Pre-compute months and sorted order
  var monthSet = {};
  for (var mi = 0; mi < n; mi++) {
    var mk = Math.floor(DATE_KEYS[orderIndices[mi]] / 100);
    monthSet[mk] = true;
  }
  var months = [];
  for (var mm in monthSet) months.push(Number(mm));
  months.sort(function(a, b) { return a - b; });
  var numB = months.length;

  var sorted = [];
  for (var si = 0; si < n; si++) {
    sorted.push({idx: si, oi: orderIndices[si], key: DATE_KEYS[orderIndices[si]],
                 month: Math.floor(DATE_KEYS[orderIndices[si]] / 100)});
  }
  sorted.sort(function(a, b) { return a.key - b.key; });

  // Pill X positions
  var pillGap = (RW - 80) / (n - 1 || 1);
  var pillXs = [];
  for (var px = 0; px < n; px++) pillXs.push(40 + px * pillGap);

  // Bucket positions
  var bGap = 10;
  var bTotalW = RW - 80;
  var bw = (bTotalW - bGap * (numB - 1)) / numB;
  var bucketXs = {};  // month -> {x, cx}
  for (var bi = 0; bi < numB; bi++) {
    var bx = 40 + bi * (bw + bGap);
    bucketXs[months[bi]] = {x: bx, cx: bx + bw / 2};
  }

  // ---- Draw static layout ----
  // Title: unsorted array
  var titleTop = anim.svgEl("text", {x: 20, y: 24, "font-size": 13, "font-weight": 700, fill: "#7c3aed"});
  titleTop.textContent = "\u2460 Unsorted rows (sort key \u2264 16 B \u2192 radix sort)";
  rsvg.appendChild(titleTop);

  // Unsorted row background
  var topBg = anim.svgEl("rect", {x: 20, y: ROW_Y - 12, width: RW - 40, height: 36, rx: 8,
    fill: "#f5f3ff", stroke: "#8b5cf6", "stroke-width": 1.5});
  rsvg.appendChild(topBg);

  // Arrow down
  var arrowD = anim.svgEl("text", {x: RW / 2, y: BUCKET_Y - 18, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#9ca3af"});
  arrowD.textContent = "\u2193 distribute by month digit (zero comparisons)";
  rsvg.appendChild(arrowD);

  // Title: buckets
  var titleMid = anim.svgEl("text", {x: 20, y: BUCKET_Y - 4, "font-size": 13, "font-weight": 700, fill: "#0369a1"});
  titleMid.textContent = "\u2461 Month buckets";
  rsvg.appendChild(titleMid);

  // Draw bucket boxes — initially at opacity 0 for staggered reveal
  var bucketRects = {};
  var bucketCounts = {};
  var bucketCountLbls = {};
  var bucketGroups = {};
  for (var bk = 0; bk < numB; bk++) {
    var m = months[bk];
    var bp = bucketXs[m];
    var clr = BUCKET_CLRS[bk %% BUCKET_CLRS.length];
    var bgrp = anim.svgEl("g", {opacity: 0});
    var br = anim.svgEl("rect", {x: bp.x, y: BUCKET_Y, width: bw, height: BUCKET_H, rx: 8,
      fill: "#f0f9ff", stroke: clr, "stroke-width": 2});
    bgrp.appendChild(br);
    var blbl = anim.svgEl("text", {x: bp.cx, y: BUCKET_Y + 20, "text-anchor": "middle",
      "font-size": 13, "font-weight": 700, fill: clr});
    blbl.textContent = MONTH_NAMES[m] || ("M" + m);
    bgrp.appendChild(blbl);
    var bcnt = anim.svgEl("text", {x: bp.cx, y: BUCKET_Y + BUCKET_H - 8, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "#64748b"});
    bcnt.textContent = "0 rows";
    bgrp.appendChild(bcnt);
    rsvg.appendChild(bgrp);
    bucketRects[m] = br;
    bucketCounts[m] = 0;
    bucketCountLbls[m] = bcnt;
    bucketGroups[m] = bgrp;
  }

  // Comparison counter (always shows 0 — the visual punchline of radix)
  var cmpCounter = anim.svgEl("text", {x: RW - 24, y: BUCKET_Y + BUCKET_H / 2 + 4,
    "text-anchor": "end", "font-size": 12, "font-weight": 700, fill: "#059669", opacity: 0});
  cmpCounter.textContent = "Comparisons: 0";
  rsvg.appendChild(cmpCounter);

  // Arrow down
  var arrowD2 = anim.svgEl("text", {x: RW / 2, y: RESULT_Y - 18, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#9ca3af"});
  arrowD2.textContent = "\u2193 gather left-to-right into sorted order";
  rsvg.appendChild(arrowD2);

  // Title: sorted result
  var titleBot = anim.svgEl("text", {x: 20, y: RESULT_Y - 4, "font-size": 13, "font-weight": 700, fill: "#047857"});
  titleBot.textContent = "\u2462 Sorted result";
  rsvg.appendChild(titleBot);

  // Sorted row background
  var botBg = anim.svgEl("rect", {x: 20, y: RESULT_Y, width: RW - 40, height: 36, rx: 8,
    fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.5, opacity: 0.3});
  rsvg.appendChild(botBg);

  // Status label
  var statusLbl = anim.svgEl("text", {x: RW / 2, y: RH - 8, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "#1f2937"});
  statusLbl.textContent = "";
  rsvg.appendChild(statusLbl);

  // ---- Build timeline ----
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");

  // Phase 1: spawn pills in unsorted row (show customer + date for tracking)
  tl.mark("Load rows");
  tl.call(function() { phase.textContent = "Phase 1/3 \u2014 loading unsorted rows"; });
  var pills = [];
  for (var pi = 0; pi < n; pi++) {
    (function(idx) {
      tl.call(function() {
        var o = ORDERS[orderIndices[idx]];
        var label = o.customer + " " + o.date;
        var tw = Math.max(70, label.length * 5.5 + 14);
        var g = anim.svgEl("g", {opacity: 0, transform: "translate(" + pillXs[idx] + "," + ROW_Y + ")"});
        var bg = anim.svgEl("rect", {x: -tw/2, y: -10, width: tw, height: 20, rx: 10,
          fill: "#7c3aed", stroke: "#1f2937", "stroke-width": 1});
        g.appendChild(bg);
        var txt = anim.svgEl("text", {x: 0, y: 4, "text-anchor": "middle",
          "font-size": 7.5, "font-weight": 700, fill: "#fff"});
        txt.textContent = label;
        g.appendChild(txt);
        rsvg.appendChild(g);
        pills[idx] = g;
        radixPills.push(g);
        // Staggered fade-in with slight scale feel via easeOutBack
        anim.tween({from: 0, to: 1, duration: 220, ease: anim.easeOutCubic,
          onUpdate: function(v) { g.setAttribute("opacity", v); }});
        statusLbl.textContent = (idx + 1) + "/" + n + " rows loaded";
      });
      tl.delay(fast ? 60 : 100);
    })(pi);
  }
  tl.delay(250);

  // Phase 2a: reveal bucket containers with stagger (Bostock: show container before filling)
  tl.mark("Distribute to buckets");
  tl.call(function() {
    phase.textContent = "Phase 2/3 \u2014 distribute by month digit (no comparisons)";
    anim.tween({from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
      onUpdate: function(v) { cmpCounter.setAttribute("opacity", v); }});
  });
  for (var rv = 0; rv < numB; rv++) {
    (function(rvIdx) {
      var rvM = months[rvIdx];
      tl.call(function() {
        anim.tween({from: 0, to: 1, duration: 280, ease: anim.easeOutCubic,
          onUpdate: function(v) { bucketGroups[rvM].setAttribute("opacity", v); }});
      });
      tl.delay(fast ? 50 : 80);
    })(rv);
  }
  tl.delay(fast ? 150 : 250);

  for (var di = 0; di < n; di++) {
    (function(idx) {
      var oi = orderIndices[idx];
      var month = Math.floor(DATE_KEYS[oi] / 100);
      var bp = bucketXs[month];
      var bkIdx = months.indexOf(month);
      var clr = BUCKET_CLRS[bkIdx >= 0 ? bkIdx %% BUCKET_CLRS.length : 0];

      // Staging: highlight pill before it moves (anticipation)
      tl.call(function() {
        var g = pills[idx];
        g.firstChild.setAttribute("fill", "#fbbf24");  // flash yellow = "I'm being classified"
        statusLbl.textContent = ORDERS[oi].customer + ": extracting month digit \u2192 " +
          (MONTH_NAMES[month] || month);
      });
      tl.delay(fast ? 60 : 140);

      // Arc into bucket with easeOutBack (overshoot landing)
      tl.call(function() {
        var g = pills[idx];
        g.firstChild.setAttribute("fill", clr);  // take bucket's color
        var startX = pillXs[idx];
        var endX = bp.cx + bucketCounts[month] * 14 - 7;
        var endY = BUCKET_Y + 32;
        anim.tween({from: 0, to: 1, duration: dur, ease: anim.easeOutBack,
          onUpdate: function(t) {
            var x = startX + (endX - startX) * t;
            var y = ROW_Y + (endY - ROW_Y) * t - Math.sin(t * Math.PI) * 35;
            g.setAttribute("transform", "translate(" + x + "," + y + ")");
          }
        });
        // Pulse bucket border + tint fill on receive
        var br = bucketRects[month];
        anim.tween({from: 2, to: 4.5, duration: 120, ease: anim.easeOutCubic,
          onUpdate: function(v) { br.setAttribute("stroke-width", v); },
          onComplete: function() {
            anim.tween({from: 4.5, to: 2, duration: 200, ease: anim.easeInCubic,
              onUpdate: function(v) { br.setAttribute("stroke-width", v); }});
          }
        });
        // Tint bucket background slightly darker as it fills
        var fillIntensity = Math.min(0.3, bucketCounts[month] * 0.08);
        br.setAttribute("fill", anim.lerpColor("#f0f9ff", "#dbeafe", fillIntensity));
        bucketCounts[month]++;
        bucketCountLbls[month].textContent = bucketCounts[month] +
          " row" + (bucketCounts[month] === 1 ? "" : "s");
        statusLbl.textContent = ORDERS[oi].customer + " (" + ORDERS[oi].date +
          ") \u2192 " + (MONTH_NAMES[month] || month) + " bucket  \u2022  Comparisons: 0";
      });
      tl.delay(fast ? dly : dly + 50);
    })(di);
  }
  tl.delay(300);

  // Phase 3: gather from buckets into sorted result — sweep left to right
  tl.mark("Gather sorted");
  tl.call(function() {
    phase.textContent = "Phase 3/3 \u2014 gather from buckets left\u2192right into sorted order";
    // Reveal result row
    anim.tween({from: 0.3, to: 1, duration: 300, ease: anim.easeOutCubic,
      onUpdate: function(v) { botBg.setAttribute("opacity", v); }});
  });
  tl.delay(fast ? dly : 250);

  for (var gi = 0; gi < sorted.length; gi++) {
    (function(pos, item) {
      var targetX = pillXs[pos];
      tl.call(function() {
        var g = pills[item.idx];
        var curTransform = g.getAttribute("transform") || "";
        var match = curTransform.match(/translate\(([^,]+),([^)]+)\)/);
        var fromX = match ? parseFloat(match[1]) : pillXs[item.idx];
        var fromY = match ? parseFloat(match[2]) : BUCKET_Y + 32;
        // Arc up slightly then down to result row with easeOutBack (settle)
        anim.tween({from: 0, to: 1, duration: dur, ease: anim.easeOutBack,
          onUpdate: function(t) {
            var x = fromX + (targetX - fromX) * t;
            var y = fromY + (RESULT_Y + 10 - fromY) * t + Math.sin(t * Math.PI) * 22;
            g.setAttribute("transform", "translate(" + x + "," + y + ")");
          }
        });
        g.firstChild.setAttribute("fill", PLACED);
        statusLbl.textContent = ORDERS[item.oi].customer + " (" + ORDERS[item.oi].date +
          ") \u2192 sorted position " + (pos + 1) + "/" + sorted.length;
      });
      tl.delay(fast ? dly : dly + 30);
    })(gi, sorted[gi]);
  }
  tl.delay(200);

  tl.mark("Done");
  tl.call(function() {
    phase.textContent = "\u2713 Radix sort complete \u2014 zero comparisons, pure digit bucketing";
    statusLbl.textContent = "Sorted by order_date \u2714 (radix: " + n + " rows, 0 comparisons)";
  });

  return tl;
}

function cleanupRadix() {
  var rsvg = document.getElementById("radix-svg");
  var fsvg = document.getElementById("filesort-svg");
  if (rsvg) { rsvg.style.display = "none"; while (rsvg.firstChild) rsvg.removeChild(rsvg.firstChild); }
  if (fsvg) fsvg.style.display = "";
  radixPills = [];
}


// ---- Priority queue (bounded heap) with visible seat boxes ----
// Visual: k "seat" boxes rendered below the array. Elements try to enter.
// If heap not full → element arcs into an empty seat.
// If heap full → compare to max (flashes). Smaller wins the seat, max
// is evicted and slides out. Larger is turned away (fades right).
function addPQToTimeline(tl, slotGs, slotXs, orderIndices, limitK) {
  var n = slotGs.length;
  var dur  = 320;
  var HEAP_CLR    = "#2563eb";
  var EVICT_CLR   = "#dc2626";
  var KEEP_CLR    = "#059669";
  var INCOMING    = "#7c3aed";
  var MAX_FLASH   = "#f59e0b";
  var SEAT_EMPTY  = "#f1f5f9";
  var SEAT_BORDER = "#94a3b8";

  // ---- Draw k seat boxes below the buffer ----
  var seatY = 115;
  var seatH = 32;
  var seatGap = 10;
  var totalSW = Math.min(W - 80, limitK * 110);
  var sw = (totalSW - seatGap * (limitK - 1)) / limitK;
  var seatStartX = (W - totalSW) / 2;
  var seatEls = []; // {rect, lbl, cx, cy, occupant: null}

  tl.call(function() {
    // Header for the heap section
    var hdr = anim.svgEl("text", {
      x: W / 2, y: seatY - 6, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: "#475569"
    });
    hdr.textContent = "Max-heap seats (" + limitK + " slots) \u2014 only smallest rows survive";
    stage.svg.appendChild(hdr);
    stage.tuples.push(hdr);

    // Render empty seats
    for (var si = 0; si < limitK; si++) {
      var sx = seatStartX + si * (sw + seatGap);
      var rect = anim.svgEl("rect", {
        x: sx, y: seatY, width: sw, height: seatH, rx: 6, ry: 6,
        fill: SEAT_EMPTY, stroke: SEAT_BORDER, "stroke-width": 1.5, opacity: 0
      });
      stage.svg.appendChild(rect);
      stage.tuples.push(rect);
      var lbl = anim.svgEl("text", {
        x: sx + sw / 2, y: seatY + seatH / 2 + 4, "text-anchor": "middle",
        "font-size": 8, "font-weight": 600, fill: "#94a3b8", opacity: 0
      });
      lbl.textContent = "empty";
      stage.svg.appendChild(lbl);
      stage.tuples.push(lbl);
      seatEls.push({rect: rect, lbl: lbl, cx: sx + sw / 2, cy: seatY + seatH / 2,
                     occupant: null, key: Infinity});
    }
    stage.bufContent.textContent = "Priority queue: " + limitK + " empty seats waiting";
  });

  // Staggered fade-in of seats
  for (var fi2 = 0; fi2 < limitK; fi2++) {
    (function(idx) {
      tl.call(function() {
        anim.tween({from: 0, to: 1, duration: 150, ease: anim.easeOutCubic,
          onUpdate: function(v) {
            seatEls[idx].rect.setAttribute("opacity", v);
            seatEls[idx].lbl.setAttribute("opacity", v);
          }
        });
      });
      tl.delay(40);
    })(fi2);
  }
  tl.delay(200);

  // Track heap state
  var heapCount = 0;

  for (var pi = 0; pi < n; pi++) {
    (function(p) {
      var key = DATE_KEYS[orderIndices[p]];
      var order = ORDERS[orderIndices[p]];

      tl.call(function() {
        slotGs[p].firstChild.setAttribute("fill", INCOMING);
        anim.tween({from: 0, to: 1, duration: 180, ease: anim.easeOutCubic,
          onUpdate: function(v) { slotGs[p].setAttribute("opacity", v); }});
        stage.bufContent.textContent = order.customer + " (" + order.date +
          ") arrives\u2026";
      });
      tl.delay(100);

      tl.call(function() {
        if (heapCount < limitK) {
          // Heap not full — find first empty seat and arc into it
          var seatIdx = -1;
          for (var si2 = 0; si2 < limitK; si2++) {
            if (seatEls[si2].occupant === null) { seatIdx = si2; break; }
          }
          if (seatIdx < 0) seatIdx = 0;
          var seat = seatEls[seatIdx];

          // Arc-tween element to seat
          var startX = slotXs[p], startY = 75;
          var endX = seat.cx, endY = seat.cy;
          var g = slotGs[p];
          anim.tween({from: 0, to: 1, duration: dur, ease: anim.easeOutBack,
            onUpdate: function(t) {
              var x = startX + (endX - startX) * t;
              var y = startY + (endY - startY) * t - Math.sin(t * Math.PI) * 20;
              g.setAttribute("transform", "translate(" + x + "," + y + ")");
            }
          });
          g.firstChild.setAttribute("fill", HEAP_CLR);
          seat.rect.setAttribute("fill", "#dbeafe");
          seat.lbl.textContent = order.customer.substring(0, 5);
          seat.lbl.setAttribute("fill", "#1e40af");
          seat.occupant = {pos: p, key: key, oi: orderIndices[p]};
          seat.key = key;
          heapCount++;
          stage.bufContent.textContent = order.customer + " \u2192 seat " +
            (seatIdx + 1) + " (" + heapCount + "/" + limitK + " filled)";
        } else {
          // Find max in heap (the one to potentially evict)
          var maxSeatIdx = 0;
          for (var hi2 = 1; hi2 < limitK; hi2++) {
            if (seatEls[hi2].key > seatEls[maxSeatIdx].key) maxSeatIdx = hi2;
          }
          var maxSeat = seatEls[maxSeatIdx];
          // Flash the max seat to show the comparison target
          anim.tween({from: 0, to: 1, duration: 150, ease: anim.easeOutCubic,
            onUpdate: function(t) {
              maxSeat.rect.setAttribute("fill", anim.lerpColor("#dbeafe", MAX_FLASH, t));
            },
            onComplete: function() {
              anim.tween({from: 0, to: 1, duration: 200, ease: anim.easeInCubic,
                onUpdate: function(t) {
                  maxSeat.rect.setAttribute("fill", anim.lerpColor(MAX_FLASH, "#dbeafe", t));
                }
              });
            }
          });

          if (key < maxSeat.key) {
            // New element wins — evict the max, take its seat
            var evicted = maxSeat.occupant;
            // Slide evicted element out to the right and fade
            var evG = slotGs[evicted.pos];
            anim.tween({from: 0, to: 1, duration: 250, ease: anim.easeInCubic,
              onUpdate: function(t) {
                var cx = maxSeat.cx + t * 60;
                evG.setAttribute("transform", "translate(" + cx + "," + maxSeat.cy + ")");
                evG.setAttribute("opacity", String(1 - t * 0.8));
              }
            });
            evG.firstChild.setAttribute("fill", EVICT_CLR);

            // Arc new element into the freed seat
            var g2 = slotGs[p];
            var sx2 = slotXs[p], sy2 = 75;
            anim.tween({from: 0, to: 1, duration: dur, ease: anim.easeOutBack, delay: 100,
              onUpdate: function(t) {
                var x = sx2 + (maxSeat.cx - sx2) * t;
                var y = sy2 + (maxSeat.cy - sy2) * t - Math.sin(t * Math.PI) * 18;
                g2.setAttribute("transform", "translate(" + x + "," + y + ")");
              }
            });
            g2.firstChild.setAttribute("fill", KEEP_CLR);
            maxSeat.rect.setAttribute("fill", "#dcfce7");
            maxSeat.lbl.textContent = order.customer.substring(0, 5);
            maxSeat.lbl.setAttribute("fill", "#166534");
            maxSeat.occupant = {pos: p, key: key, oi: orderIndices[p]};
            maxSeat.key = key;
            stage.bufContent.textContent = order.customer + " (" + order.date +
              ") < heap max " + ORDERS[evicted.oi].date +
              " \u2192 evicts " + ORDERS[evicted.oi].customer;
          } else {
            // New element is larger — turned away
            var gReject = slotGs[p];
            anim.tween({from: 0, to: 1, duration: 250, ease: anim.easeInCubic,
              onUpdate: function(t) {
                var cx = slotXs[p] + t * 50;
                gReject.setAttribute("transform", "translate(" + cx + ",75)");
                gReject.setAttribute("opacity", String(1 - t * 0.8));
              }
            });
            gReject.firstChild.setAttribute("fill", EVICT_CLR);
            stage.bufContent.textContent = order.customer + " (" + order.date +
              ") \u2265 heap max \u2192 turned away";
          }
        }
      });
      tl.delay(dur + 60);
    })(pi);
  }

  tl.delay(200);
  // Final: highlight all surviving seats green
  tl.call(function() {
    for (var fsi = 0; fsi < limitK; fsi++) {
      if (seatEls[fsi].occupant) {
        seatEls[fsi].rect.setAttribute("fill", "#bbf7d0");
        seatEls[fsi].rect.setAttribute("stroke", KEEP_CLR);
        var surv = slotGs[seatEls[fsi].occupant.pos];
        surv.firstChild.setAttribute("fill", KEEP_CLR);
        surv.setAttribute("opacity", "1");
      }
    }
    stage.bufContent.textContent = "Heap complete: " + limitK +
      " smallest rows survive, rest discarded \u2714";
  });
  tl.delay(300);
}

function buildStage(numRuns) {
  var svg = document.getElementById("filesort-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // ---- Zone 1: Sort buffer (top) ----
  var bufLbl = anim.svgEl("text", {
    x: 20, y: 28, "font-size": 13, "font-weight": 700, fill: "#7c3aed"
  });
  bufLbl.textContent = "\u2460 sort_buffer_size \u2014 in-memory sort area";
  svg.appendChild(bufLbl);

  var bufRect = anim.svgEl("rect", {
    x: 20, y: 40, width: W - 40, height: 70, rx: 8, ry: 8,
    fill: "#f5f3ff", stroke: "#8b5cf6", "stroke-width": 1.5
  });
  svg.appendChild(bufRect);

  var bufContent = anim.svgEl("text", {
    x: W / 2, y: 80, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#5b21b6"
  });
  bufContent.textContent = "(rows fill here, get sorted, then spill to disk)";
  svg.appendChild(bufContent);

  // Wrap all spill-path visuals in a group so radix sort can hide them
  var spillGroup = anim.svgEl("g", {});
  svg.appendChild(spillGroup);

  // ---- Arrow: buffer \u2192 tmpdir ----
  var arrow1Y = 118;
  var arrow1 = anim.svgEl("path", {
    d: "M" + (W/2) + "," + arrow1Y + " L" + (W/2) + "," + (arrow1Y + 24) +
       " M" + (W/2 - 6) + "," + (arrow1Y + 18) + " L" + (W/2) + "," + (arrow1Y + 24) +
       " L" + (W/2 + 6) + "," + (arrow1Y + 18),
    stroke: "#9ca3af", "stroke-width": 2, fill: "none"
  });
  spillGroup.appendChild(arrow1);
  var arrow1Lbl = anim.svgEl("text", {
    x: W/2 + 16, y: arrow1Y + 16, "font-size": 10, fill: "#9ca3af", "font-weight": 600
  });
  arrow1Lbl.textContent = "spill sorted run";
  spillGroup.appendChild(arrow1Lbl);

  // ---- Zone 2: Sorted runs on disk (middle) ----
  var diskY = 152;
  var diskLbl = anim.svgEl("text", {
    x: 20, y: diskY, "font-size": 13, "font-weight": 700, fill: "#b45309"
  });
  diskLbl.textContent = "\u2461 tmpdir \u2014 sorted runs on disk";
  spillGroup.appendChild(diskLbl);

  // Show max 4 run boxes so there is always room for the overflow label
  var cap = Math.min(numRuns, 4);
  var hasMore = numRuns > cap;
  var gap = 12;
  var startX = 20;
  // Reserve 180px on the right for the "+N more" label when needed
  var avail = hasMore ? W - 40 - 180 : W - 40;
  var rw = cap > 0 ? (avail - gap * (cap - 1)) / cap : 0;
  var runRects = [];

  for (var i = 0; i < cap; i++) {
    var x = startX + i * (rw + gap);
    var rect = anim.svgEl("rect", {
      x: x, y: diskY + 12, width: rw, height: 50, rx: 6, ry: 6,
      fill: "#fffbeb", stroke: "#d1d5db", "stroke-width": 1.5, opacity: 0.3
    });
    spillGroup.appendChild(rect);
    var lbl = anim.svgEl("text", {
      x: x + rw / 2, y: diskY + 42, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: "#92400e", opacity: 0.3
    });
    lbl.textContent = "Run " + (i + 1);
    spillGroup.appendChild(lbl);
    runRects.push({rect: rect, label: lbl});
  }
  if (hasMore) {
    var moreX = startX + cap * (rw + gap) + 8;
    var more = anim.svgEl("text", {
      x: moreX, y: diskY + 32, "font-size": 12, "font-weight": 600,
      fill: "#92400e", "font-style": "italic"
    });
    more.textContent = "\u2026 + " + (numRuns - cap) + " more run" + (numRuns - cap === 1 ? "" : "s");
    spillGroup.appendChild(more);
    var moreSub = anim.svgEl("text", {
      x: moreX, y: diskY + 48, "font-size": 10, fill: "#9ca3af"
    });
    moreSub.textContent = "(each is another buffer-fill \u2192 sort \u2192 flush cycle)";
    spillGroup.appendChild(moreSub);
  }

  // ---- Arrow: tmpdir \u2192 merge ----
  var arrow2Y = diskY + 70;
  var arrow2 = anim.svgEl("path", {
    d: "M" + (W/2) + "," + arrow2Y + " L" + (W/2) + "," + (arrow2Y + 24) +
       " M" + (W/2 - 6) + "," + (arrow2Y + 18) + " L" + (W/2) + "," + (arrow2Y + 24) +
       " L" + (W/2 + 6) + "," + (arrow2Y + 18),
    stroke: "#9ca3af", "stroke-width": 2, fill: "none"
  });
  spillGroup.appendChild(arrow2);
  var arrow2Lbl = anim.svgEl("text", {
    x: W/2 + 16, y: arrow2Y + 16, "font-size": 10, fill: "#9ca3af", "font-weight": 600
  });
  arrow2Lbl.textContent = "k-way merge";
  spillGroup.appendChild(arrow2Lbl);

  // ---- Zone 3: Merge output (bottom) ----
  var mergeY = arrow2Y + 32;
  var mergeLbl = anim.svgEl("text", {
    x: 20, y: mergeY, "font-size": 13, "font-weight": 700, fill: "#047857"
  });
  mergeLbl.textContent = "\u2462 final sorted output";
  spillGroup.appendChild(mergeLbl);

  var mergeRect = anim.svgEl("rect", {
    x: 20, y: mergeY + 12, width: W - 40, height: 50, rx: 8, ry: 8,
    fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.5, opacity: 0.3
  });
  spillGroup.appendChild(mergeRect);

  var mergeContent = anim.svgEl("text", {
    x: W / 2, y: mergeY + 42, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "#065f46", opacity: 0.3
  });
  mergeContent.textContent = "";
  spillGroup.appendChild(mergeContent);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 16, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, bufRect: bufRect, bufContent: bufContent,
    runRects: runRects, totalRuns: numRuns,
    mergeRect: mergeRect, mergeContent: mergeContent, mergeLbl: mergeLbl,
    statusLbl: statusLbl, spillGroup: spillGroup, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  stage.bufRect.setAttribute("fill", "#f5f3ff");
  stage.bufContent.textContent = "(rows fill here, get sorted, then spill to disk)";
  stage.spillGroup.setAttribute("opacity", "1");
  stage.runRects.forEach(function(r) {
    r.rect.setAttribute("opacity", 0.3);
    r.rect.setAttribute("fill", "#fffbeb");
    r.label.setAttribute("opacity", 0.3);
  });
  stage.mergeRect.setAttribute("opacity", 0.3);
  stage.mergeContent.setAttribute("opacity", 0.3);
  stage.mergeContent.textContent = "";
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function spawnTuple(text, color, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var tw = Math.max(70, text.length * 5.5 + 14);
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

function buildTimeline(numRuns, spill, sortAlg, pqSize) {
  cleanupRadix();  // ensure radix SVG is hidden if switching from radix
  resetStage();

  // Radix sort gets its own dedicated SVG — completely separate rendering
  if (sortAlg === "radix" && !spill) {
    var radixIndices = [];
    for (var ri2 = 0; ri2 < 6; ri2++) radixIndices.push(ri2);
    return buildRadixTimeline(radixIndices, false);
  }

  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var shown = stage.runRects.length;

  if (sortAlg === "priority_queue") {
    // Priority queue path: ORDER BY … LIMIT k
    var PQ_N = 8;
    var pqXs = [];
    for (var pqi = 0; pqi < PQ_N; pqi++) pqXs.push(60 + pqi * 95);
    var pqGs = [];
    var pqIndices = [];
    for (var pqi2 = 0; pqi2 < PQ_N; pqi2++) pqIndices.push(pqi2);

    tl.mark("Load rows");
    tl.call(function() {
      phase.textContent = "Priority queue \u2014 ORDER BY \u2026 LIMIT " + pqSize + " (bounded max-heap)";
      stage.bufRect.setAttribute("fill", "#dbeafe");
    });
    for (var pr = 0; pr < PQ_N; pr++) {
      (function(idx) {
        tl.call(function() {
          var o = ORDERS[idx];
          var label = o.customer + " " + o.date;
          var tuple = spawnTuple(label, "#7c3aed", pqXs[idx], 75);
          pqGs[idx] = tuple;
          anim.tween({from: 0, to: 1, duration: 150, ease: anim.easeOutCubic,
            onUpdate: function(v) { tuple.setAttribute("opacity", v); }});
        });
        tl.delay(100);
      })(pr);
    }
    tl.delay(300);

    tl.mark("Heap push/pop");
    tl.call(function() {
      phase.textContent = "Push each row into max-heap of size " + pqSize + " \u2014 evict largest if full";
      stage.bufRect.setAttribute("fill", "#bfdbfe");
    });
    addPQToTimeline(tl, pqGs, pqXs, pqIndices, Math.min(pqSize, PQ_N - 1));

    tl.mark("Done");
    tl.call(function() {
      phase.textContent = "\u2713 Priority queue done \u2014 only " + pqSize + " rows kept, no full sort needed";
      stage.mergeRect.setAttribute("opacity", 1);
      stage.mergeContent.setAttribute("opacity", 1);
      stage.mergeContent.textContent = "Top " + pqSize + " rows streamed from heap";
      stage.statusLbl.textContent = "Priority queue: O(n \u00b7 log k) — scanned all rows but only kept " + pqSize + " in memory.";
    });
    return tl;
  }

  if (!spill) {
    // All rows fit in memory — choose radix or introsort
    var QS_N = 6;
    var qsXs = [];
    for (var qi = 0; qi < QS_N; qi++) qsXs.push(100 + qi * 120);
    var qsGs = [];
    var qsIndices = [];
    for (var qi2 = 0; qi2 < QS_N; qi2++) qsIndices.push(qi2);

    tl.mark("Fill sort buffer");
    tl.call(function() {
      phase.textContent = "Phase 1/2 \u2014 loading rows into sort_buffer";
      stage.bufRect.setAttribute("fill", "#ddd6fe");
    });
    for (var r = 0; r < QS_N; r++) {
      (function(idx) {
        tl.call(function() {
          var o = ORDERS[idx];
          var label = o.customer + " " + o.date;
          var tuple = spawnTuple(label, "#7c3aed", qsXs[idx], 75);
          qsGs[idx] = tuple;
          anim.tween({from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
            onUpdate: function(v) { tuple.setAttribute("opacity", v); }});
          stage.bufContent.textContent = (idx + 1) + " rows loaded";
        });
        tl.delay(150);
      })(r);
    }
    tl.delay(400);

    tl.mark("Introsort: partitioning");
    tl.call(function() {
      phase.textContent = "Phase 2/2 \u2014 introsort (quicksort + heapsort fallback after O(log n) depth)";
      stage.bufRect.setAttribute("fill", "#c4b5fd");
    });
    tl.delay(200);
    addQsToTimeline(tl, qsGs, qsXs, qsIndices, false);

    tl.mark("Sorted \u2014 done");
    tl.call(function() {
      phase.textContent = "\u2713 Sorted in memory via introsort \u2014 no disk I/O!";
      // Restore spill group to show merge result
      stage.spillGroup.setAttribute("opacity", "1");
      stage.mergeRect.setAttribute("opacity", 1);
      stage.mergeContent.setAttribute("opacity", 1);
      stage.mergeContent.textContent = "Result streamed directly from memory";
      stage.statusLbl.textContent = "In-memory " + algName + ": 0 disk spills, 0 merge passes.";
    });
    return tl;
  }

  // Spill path: fill buffer, quicksort, flush to disk runs, then merge
  var animRuns = Math.min(shown, numRuns);
  for (var ri = 0; ri < animRuns; ri++) {
    (function(runIdx) {
      var chunkStart = (runIdx * 4) %% ORDERS.length;
      var chunkXs = [80, 260, 440, 620];
      var chunkGs = [];
      var chunkIndices = [];
      for (var ck = 0; ck < 4; ck++) {
        chunkIndices.push((chunkStart + ck) %% ORDERS.length);
      }

      tl.mark("Run " + (runIdx + 1) + ": fill");
      tl.call(function() {
        phase.textContent = "Phase 1/" + (numRuns > 1 ? "2" : "1") +
          " \u2014 filling sort_buffer, run " + (runIdx + 1) + " of " + numRuns;
        stage.bufRect.setAttribute("fill", "#ddd6fe");
        stage.bufContent.textContent = "Filling with rows\u2026";
      });
      for (var ci = 0; ci < 4; ci++) {
        (function(c) {
          tl.call(function() {
            var order = ORDERS[chunkIndices[c]];
            var label = order.customer + " " + order.date;
            var tuple = spawnTuple(label, "#7c3aed", chunkXs[c], 75);
            chunkGs[c] = tuple;
            anim.tween({from: 0, to: 1, duration: 150, ease: anim.easeOutCubic,
              onUpdate: function(v) { tuple.setAttribute("opacity", v); }});
          });
          tl.delay(100);
        })(ci);
      }
      tl.delay(200);

      // Sort animation (fast mode for spill path — always introsort here,
      // radix in-memory path is handled by buildRadixTimeline above)
      tl.mark("Run " + (runIdx + 1) + ": introsort");
      tl.call(function() {
        stage.bufRect.setAttribute("fill", "#c4b5fd");
      });
      addQsToTimeline(tl, chunkGs, chunkXs, chunkIndices, true);

      // Flush sorted run to disk
      tl.mark("Run " + (runIdx + 1) + ": flush");
      tl.call(function() {
        stage.bufContent.textContent = "Flushing sorted run to tmpdir\u2026";
        stage.bufRect.setAttribute("fill", "#fde68a");
        if (runIdx < shown) {
          stage.runRects[runIdx].rect.setAttribute("opacity", 1);
          stage.runRects[runIdx].rect.setAttribute("fill", "#fde68a");
          stage.runRects[runIdx].label.setAttribute("opacity", 1);
          anim.tween({
            from: 0, to: 1, duration: 400, ease: anim.easeOutCubic,
            onUpdate: function(v) {
              stage.runRects[runIdx].rect.setAttribute("fill",
                anim.lerpColor("#fde68a", "#fbbf24", v));
            }
          });
        }
        // Fade out buffer tuples + QS pointers
        stage.tuples.forEach(function(t) {
          anim.tween({from: 1, to: 0, duration: 300, ease: anim.easeInCubic,
            onUpdate: function(v) { t.setAttribute("opacity", v); }});
        });
      });
      tl.delay(500);
      tl.call(function() {
        stage.bufRect.setAttribute("fill", "#f5f3ff");
        stage.bufContent.textContent = "(buffer cleared for next chunk)";
        stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
        stage.tuples = [];
      });
      tl.delay(200);
    })(ri);
  }

  // Merge phase — animate labeled tuples flowing from run boxes into the
  // merge output area, showing the k-way merge picking the smallest key
  // from each run head in sorted order.
  tl.mark("K-way merge");
  tl.call(function() {
    phase.textContent = "Phase 2/2 \u2014 k-way merge: pick the smallest key across " + numRuns + " run heads";
    stage.mergeRect.setAttribute("opacity", 1);
    stage.mergeContent.setAttribute("opacity", 1);
    stage.mergeContent.textContent = "Merging runs\u2026";
  });
  tl.delay(300);

  // Build a per-run queue of sorted order indices so we know which rows
  // belong to each run (same chunking logic used during the spill phase).
  var runQueues = [];
  for (var rq = 0; rq < animRuns; rq++) {
    var chunk = [];
    for (var ci2 = 0; ci2 < 4; ci2++) {
      chunk.push(((rq * 4) + ci2) %% ORDERS.length);
    }
    chunk.sort(function(a, b) { return DATE_KEYS[a] - DATE_KEYS[b]; });
    runQueues.push(chunk);
  }

  // Flatten via simulated k-way merge: always pick the run whose head
  // has the smallest DATE_KEY.
  var mergeOrder = [];
  var heads = [];
  for (var h = 0; h < runQueues.length; h++) heads.push(0);
  var totalMerge = animRuns * 4;
  for (var mp = 0; mp < totalMerge; mp++) {
    var bestRun = -1, bestKey = Infinity;
    for (var r2 = 0; r2 < runQueues.length; r2++) {
      if (heads[r2] < runQueues[r2].length) {
        var k = DATE_KEYS[runQueues[r2][heads[r2]]];
        if (k < bestKey) { bestKey = k; bestRun = r2; }
      }
    }
    if (bestRun < 0) break;
    mergeOrder.push({run: bestRun, orderIdx: runQueues[bestRun][heads[bestRun]]});
    heads[bestRun]++;
  }

  // Animate each merge pick: spawn a pill at the source run, arc it down
  // to its landing position in the merge output area.
  var mergeSlotW = Math.min(90, (W - 60) / Math.max(1, mergeOrder.length));
  var mergeBaseX = 30;
  var mergeBaseY = parseFloat(stage.mergeRect.getAttribute("y")) + 28;
  var placed = 0;

  // Pre-compute pill data at build time so tl.add() closures can reference
  // stable values.  Each pick creates: call (spawn pill + narrate) → add
  // (arc tween owned by timeline) → call (de-highlight).
  for (var mi2 = 0; mi2 < mergeOrder.length; mi2++) {
    (function(pick, slot) {
      var order = ORDERS[pick.orderIdx];
      var label = order.customer + " " + order.date;
      var runRect = (pick.run < shown) ? stage.runRects[pick.run] : null;
      var srcX, srcY;
      if (runRect) {
        srcX = parseFloat(runRect.rect.getAttribute("x")) +
               parseFloat(runRect.rect.getAttribute("width")) / 2;
        srcY = parseFloat(runRect.rect.getAttribute("y")) + 25;
      } else {
        srcX = W / 2;
        srcY = parseFloat(stage.mergeRect.getAttribute("y")) - 20;
      }
      var dstX = mergeBaseX + slot * mergeSlotW + mergeSlotW / 2;
      var dstY = mergeBaseY;
      var ctrlY = (srcY + dstY) / 2 - 10;
      var pathFn = anim.path(srcX, srcY, (srcX + dstX) / 2, ctrlY, dstX, dstY);

      // Pill reference — created in the call, animated by the tl.add tween
      var pill = null;

      tl.call(function() {
        stage.mergeContent.textContent = "Pick: " + label + " (smallest head across runs)";
        if (runRect) {
          runRect.rect.setAttribute("stroke", "#f59e0b");
          runRect.rect.setAttribute("stroke-width", "2.5");
        }
        pill = spawnTuple(label, "#059669", srcX, srcY);
        pill.setAttribute("opacity", "1");
      });
      // Timeline-owned tween — scrubbable, no standalone RAF
      tl.add({
        from: 0, to: 1, duration: 420, ease: anim.easeInOutCubic,
        onUpdate: function(t) {
          if (!pill) return;
          var p = pathFn(t);
          pill.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
        },
        onComplete: function() {
          if (runRect) {
            runRect.rect.setAttribute("stroke", "#d1d5db");
            runRect.rect.setAttribute("stroke-width", "1.5");
          }
        }
      });
    })(mergeOrder[mi2], placed);
    placed++;
  }

  // Fade out run boxes — also timeline-owned
  tl.add({
    from: 1, to: 0.25, duration: 400, ease: anim.easeInCubic,
    onUpdate: function(v) {
      for (var fi = 0; fi < shown; fi++) {
        stage.runRects[fi].rect.setAttribute("opacity", v);
        stage.runRects[fi].label.setAttribute("opacity", v);
      }
    }
  });

  tl.mark("Done");
  tl.call(function() {
    stage.mergeRect.setAttribute("fill", "#a7f3d0");
    stage.mergeContent.textContent = "All " + numRuns + " runs merged \u2192 final sorted result";
    phase.textContent = "\u2713 Filesort complete \u2014 " + numRuns + " sorted runs merged via k-way merge";
    stage.statusLbl.textContent = numRuns + " disk spills + merge = significant I/O. Bigger sort_buffer_size would help.";
  });

  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var limitVal = c.limit_rows || 0;
  var cost = filesortCost(c.rows, c.row_size, c.sbs, limitVal);
  return buildTimeline(cost.runs, cost.spill, cost.sortAlg, cost.pqSize);
}
function resetAnim() {
  cleanupRadix();
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function renderChart(rowSize, sbs, currentRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Row count to sort", yLabel: "Total I/O rows (read+write)",
    curves: [
      { label: "Filesort (this algorithm)", color: "#7c3aed",
        fn: function(n) { return filesortCost(n, rowSize, sbs).ioRows; } },
      { label: "Index-ordered scan (0 I/O)", color: "#059669",
        fn: function(n) { return n; } }
    ],
    current: { x: currentRows },
    xSlider: "rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(100, Math.min(1000000, xVal / 100) * 100)); }
  });
}

function algDisplayName(alg) {
  if (alg === "radix") return "Radix sort";
  if (alg === "priority_queue") return "Priority queue";
  return "Introsort (std::sort)";
}
function algReason(alg, rowSize, limitRows, rows) {
  if (alg === "priority_queue")
    return "LIMIT " + limitRows + " is small and fits in buffer \u2192 bounded max-heap of " +
           limitRows + " elements. Scans all " + rows + " rows but never fully sorts them.";
  if (alg === "radix")
    return "Sort key is " + rowSize + " B (\u2264 16 B fixed-length) \u2192 radix sort. " +
           "O(n\u00b7k) with zero comparisons \u2014 distributes into digit buckets.";
  return "Sort key is " + rowSize + " B (> 16 B or variable-length) \u2192 introsort " +
         "(quicksort + heapsort fallback after O(log n) depth). " +
         "Guaranteed O(n log n) worst case.";
}

function recompute() {
  var c = teachRuntime.readControls();
  var limitVal = c.limit_rows || 0;
  var cost = filesortCost(c.rows, c.row_size, c.sbs, limitVal);
  document.getElementById("out-rpr").textContent = teachRuntime.formatInt(cost.rpr);
  document.getElementById("out-runs").textContent = teachRuntime.formatInt(cost.runs);
  document.getElementById("out-merges").textContent = String(cost.merges);
  document.getElementById("out-io").textContent = teachRuntime.formatInt(cost.ioRows);
  document.getElementById("out-spill").textContent = cost.spill ? "Yes \u2014 disk I/O" : "No \u2014 in-memory";
  document.getElementById("out-spill").className = "value " + (cost.spill ? "hot" : "ok");
  document.getElementById("out-alg").textContent = algDisplayName(cost.sortAlg);
  document.getElementById("out-alg").className = "value " +
    (cost.sortAlg === "radix" ? "ok" : cost.sortAlg === "priority_queue" ? "ok" : "");
  document.getElementById("out-explanation").textContent =
    algReason(cost.sortAlg, c.row_size, limitVal, c.rows) +
    (cost.spill
      ? " Buffer holds " + cost.rpr + " rows \u2192 " + cost.runs +
        " sorted runs spilled to tmpdir. K-way merge needs " + cost.merges +
        " pass(es). Bigger sort_buffer_size \u2192 fewer runs \u2192 less I/O."
      : (cost.sortAlg !== "priority_queue"
         ? " All rows fit in sort_buffer_size \u2014 no disk spill."
         : ""));
  buildStage(cost.runs);
  resetStage();
  renderChart(c.row_size, c.sbs, c.rows);
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
