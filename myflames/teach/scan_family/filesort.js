var SORT_BUFFER_SIZE_DEFAULT = %d;          // MySQL: 256 KiB
var MARIADB_SORT_BUFFER_SIZE_DEFAULT = %d;  // MariaDB: 2 MiB (MAX_SORT_MEMORY)
// ORDER BY order_date sorts on a DATE, a 3-byte sort key in both servers.
// The row size decides how many rows fit in the buffer, never this length.
var SORT_KEY_BYTES = 3;

// Which in-memory sort runs (checked in the server source):
//   MySQL 8.4 / 9.x  Filesort_buffer::sort_buffer (sql/filesort_utils.cc):
//                    std::sort up to 100 rows or for variable-length keys,
//                    std::stable_sort above that. MySQL has no radix sort.
//   MariaDB          Filesort_buffer::sort_buffer: radix sort when the SORT
//                    KEY is at most 20 bytes and the buffer holds 1,000 to
//                    99,999 rows (radixsort_is_applicable(count,
//                    param->sort_length), sql/filesort_utils.cc and
//                    mysys/mf_radix.c) and the keys are not packed (a
//                    fixed-size key such as a DATE never is, filesort.cc
//                    try_to_pack_sortkeys), else its own quicksort
//                    (my_qsort2). Every buffer is checked on its own, so
//                    each spilled run of 1,000 to 99,999 rows is radix sorted.
//   Both             a bounded priority queue for a small ORDER BY ... LIMIT
//                    (MySQL check_if_pq_applicable; MariaDB by cost in
//                    filesort(); both sql/filesort.cc).
function radixApplies(server, bufferRows) {
  return server === "mariadb" && SORT_KEY_BYTES <= 20 &&
         bufferRows >= 1000 && bufferRows < 100000;
}

function filesortCost(rows, rowSize, sbs, limitRows, server) {
  var rpr = Math.max(1, Math.floor(sbs / rowSize));
  var usePQ = (limitRows > 0 && limitRows < rows && limitRows * rowSize * 2 < sbs);
  if (usePQ) {
    // Priority queue: only limitRows kept in memory, no spill possible
    return {rpr: rpr, runs: 1, merges: 0, ioRows: rows, spill: false,
            sortAlg: "priority_queue", pqSize: limitRows, bufferRows: limitRows};
  }
  var runs = Math.max(1, Math.ceil(rows / rpr));
  if (runs <= 1) return {rpr: rpr, runs: 1, merges: 0, ioRows: rows, spill: false,
                         sortAlg: radixApplies(server, rows) ? "radix" : "comparison",
                         pqSize: 0, bufferRows: rows};
  var fanIn = 15;
  var merges = Math.max(1, Math.ceil(Math.log(runs) / Math.log(fanIn)));
  var ioRows = rows + merges * rows * 2;
  // Each full buffer (one run) is sorted before it is written.
  return {rpr: rpr, runs: runs, merges: merges, ioRows: ioRows, spill: true,
          sortAlg: radixApplies(server, rpr) ? "radix" : "comparison",
          pqSize: 0, bufferRows: rpr};
}

function serverName(server) { return server === "mariadb" ? "MariaDB" : "MySQL"; }
// Whole numbers with separators for captions ("10,000 rows", never "10.0K").
function fmtN(n) { return Math.round(n).toLocaleString("en-US"); }

var W = 800, H = 440;
var RUN_ROWS = 3;   // sample rows drawn per sorted run
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

function orderLabel(i) { return ORDERS[i].customer + " " + ORDERS[i].date; }

function translate(g, x, y) {
  g.setAttribute("transform", "translate(" + x.toFixed(1) + "," + y.toFixed(1) + ")");
}

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
      var tmp2 = pos[i]; pos[i] = pos[hi]; pos[hi] = tmp2;
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
// always resolve the correct element at play-time. Swaps are timeline-owned
// tweens, so jumping to a step lands every pill in its final slot.
// In the slow (in-memory) mode each partition is one guided step.
function addQsToTimeline(tl, slotGs, slotXs, orderIndices, fast) {
  var ops = recordQsOps(orderIndices);
  var n = orderIndices.length;  // slotGs is filled while the timeline plays
  var dur  = fast ? 200 : 350;
  var dly  = fast ? 120 : 200;
  var cmpD = fast ? 80  : 140;
  var UNSORTED    = "var(--svg-group)";
  var PIVOT_CLR   = "var(--svg-build)";
  var LESS_CLR    = "var(--svg-ok)";
  var GREATER_CLR = "var(--svg-danger)";
  var PLACED_CLR  = "var(--svg-ok)";
  var phase = document.getElementById("phase-label");
  var partitions = 0;
  for (var pc = 0; pc < ops.length; pc++) if (ops[pc].type === "pivot") partitions++;

  var ptrI = null, ptrJ = null, bracket = null;
  tl.call(function() {
    ptrI = createQsPointer("i", "var(--svg-token)");
    ptrJ = createQsPointer("j", "var(--svg-build)");
    bracket = anim.svgEl("line", {
      x1:0, y1:0, x2:0, y2:0,
      stroke:"var(--svg-node-stroke)", "stroke-width":1, "stroke-dasharray":"3,3", opacity:0
    });
    stage.svg.appendChild(bracket);
    stage.tuples.push(bracket);
  });
  var placed = {};
  var partition = 0;

  for (var oi = 0; oi < ops.length; oi++) {
    (function(op) {
      if (op.type === "pivot") {
        partition += 1;
        var pNum = partition;
        if (!fast) tl.beat("partition");
        tl.call(function() {
          slotGs[op.pos].firstChild.setAttribute("fill", PIVOT_CLR);
          stage.bufContent.textContent = "Pivot: " + ORDERS[op.order].customer +
            " (" + ORDERS[op.order].date + ")";
          bracket.setAttribute("x1", slotXs[op.lo] - 30);
          bracket.setAttribute("y1", 92);
          bracket.setAttribute("x2", slotXs[op.hi] + 30);
          bracket.setAttribute("y2", 92);
          bracket.setAttribute("opacity", 0.5);
          translate(ptrI, slotXs[op.lo], 98);
          ptrI.setAttribute("opacity", 1);
          ptrJ.setAttribute("opacity", 0);
          if (!fast) {
            phase.textContent = "Phase 2/2, partition " + pNum + " of " + partitions + ": pivot " +
              ORDERS[op.order].customer + " (" + ORDERS[op.order].date + "); earlier dates move left of it, " +
              "later dates stay right";
          }
        });
        tl.delay(dly * 2);
      }
      else if (op.type === "cmp") {
        tl.call(function() {
          translate(ptrJ, slotXs[op.j], 98);
          ptrJ.setAttribute("opacity", 1);
          translate(ptrI, slotXs[op.i], 98);
          if (!placed[op.j]) {
            slotGs[op.j].firstChild.setAttribute("fill",
              op.leq ? LESS_CLR : GREATER_CLR);
          }
          var sym = op.leq ? " ≤ " : " > ";
          stage.bufContent.textContent = ORDERS[op.orderJ].date + sym +
            ORDERS[op.orderP].date +
            (op.leq ? " → moves left of the pivot" : "");
        });
        tl.delay(cmpD);
      }
      else if (op.type === "swap") {
        var pair = {ga: null, gb: null};
        var xa = slotXs[op.a], xb = slotXs[op.b];
        tl.call(function() {
          pair.ga = slotGs[op.a];
          pair.gb = slotGs[op.b];
          stage.bufContent.textContent = "Swap: " + ORDERS[op.orderA].customer +
            " ↔ " + ORDERS[op.orderB].customer;
          // Permute tracking array to stay in sync with algorithm
          slotGs[op.a] = pair.gb;
          slotGs[op.b] = pair.ga;
        });
        tl.add({from: 0, to: 1, duration: dur, ease: anim.easeInOutCubic,
          onUpdate: function(t) {
            translate(pair.ga, xa + (xb - xa) * t, 75 - Math.sin(t * Math.PI) * 16);
            translate(pair.gb, xb + (xa - xb) * t, 75 + Math.sin(t * Math.PI) * 16);
          }});
        tl.delay(60);
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
            " (" + ORDERS[op.order].date + ") → final position";
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
      ? "Buffer sorted ✔" : "Sorted by order_date ✔";
  });
  tl.delay(fast ? 150 : 300);
}

// ---- MariaDB radix sort of one spilled run ----
// The full drawing (buckets, passes) is buildRadixTimeline; inside a run the
// rows simply go to their places by key, with no compare or swap steps.
function addRunRadixToTimeline(tl, slotGs, slotXs, orderIndices) {
  var n = orderIndices.length;
  var byKey = [];
  for (var i = 0; i < n; i++) byKey.push({i: i, key: DATE_KEYS[orderIndices[i]]});
  byKey.sort(function(a, b) { return a.key - b.key; });
  var rank = [];
  for (var r = 0; r < byKey.length; r++) rank[byKey[r].i] = r;
  var first = ORDERS[orderIndices[byKey[0].i]];
  tl.call(function() {
    stage.bufContent.textContent = "Radix sort: rows counted into place by key byte, 0 comparisons";
  });
  tl.add({from: 0, to: 1, duration: 450, ease: anim.easeInOutCubic,
    onUpdate: function(t) {
      for (var k = 0; k < n; k++) {
        var g = slotGs[k];
        if (!g) continue;
        var from = slotXs[k], to = slotXs[rank[k]];
        var lift = from === to ? 0 : Math.sin(t * Math.PI) * 16 * (k %% 2 ? 1 : -1);
        translate(g, from + (to - from) * t, 75 - lift);
        g.firstChild.setAttribute("fill", t < 0.5 ? "var(--svg-group)" : "var(--svg-ok)");
      }
    }});
  tl.call(function() {
    stage.bufContent.textContent = "Buffer sorted ✔ (radix, 0 comparisons; " +
      first.customer + " " + first.date + " first)";
  });
  tl.delay(150);
}

// ---- MariaDB radix sort, drawn in the main stage ----
// radixsort_for_str_ptr makes one counting pass per key byte, least
// significant first. The drawing shows two passes: the day, then the month.
var MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May"];
var BUCKET_CLRS = ["var(--svg-token)", "var(--svg-group)", "var(--svg-group-alt)", "var(--svg-build)", "var(--svg-probe)", "var(--svg-ok)"];

function buildRadixTimeline(orderIndices, fast, c) {
  var totalRows = fmtN(c.rows);
  // Drawn detached and attached by the first step, so building the
  // timeline (the guide does it to count steps) leaves the stage as it is.
  var rg = anim.svgEl("g", {});
  stage.tuples.push(rg);

  var n = Math.min(orderIndices.length, 8);
  var RW = 800, RH = 340;
  var dur = fast ? 250 : 420;
  var dly = fast ? 80 : 160;
  var PLACED = "var(--svg-ok)";
  var ROW_Y = 40;       // unsorted row at top
  var BUCKET_Y = 140;   // bucket boxes in middle
  var BUCKET_H = 72;
  var RESULT_Y = 280;   // sorted result row at bottom

  // Pre-compute months, the day order (after pass 1) and the sorted order
  var monthSet = {};
  var items = [];
  for (var mi = 0; mi < n; mi++) {
    var key = DATE_KEYS[orderIndices[mi]];
    monthSet[Math.floor(key / 100)] = true;
    items.push({idx: mi, oi: orderIndices[mi], key: key, day: key %% 100, month: Math.floor(key / 100)});
  }
  var months = [];
  for (var mm in monthSet) months.push(Number(mm));
  months.sort(function(a, b) { return a - b; });
  var numB = months.length;
  var byDay = items.slice().sort(function(a, b) { return a.day - b.day; });
  var dayRank = {};
  for (var dr = 0; dr < byDay.length; dr++) dayRank[byDay[dr].idx] = dr;
  var sorted = items.slice().sort(function(a, b) { return a.key - b.key; });

  // Pill X positions
  // Keep whole pills (about 80 px wide) inside the 20..780 row boxes.
  var pillGap = (RW - 160) / (n - 1 || 1);
  var pillXs = [];
  for (var px = 0; px < n; px++) pillXs.push(80 + px * pillGap);

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
  var titleTop = anim.svgEl("text", {x: 20, y: 24, "font-size": 13, "font-weight": 700, fill: "var(--svg-group)"});
  titleTop.textContent = "① Rows in the sort buffer (MariaDB: 3-byte order_date key → radix sort)";
  rg.appendChild(titleTop);

  var topBg = anim.svgEl("rect", {x: 20, y: ROW_Y - 12, width: RW - 40, height: 36, rx: 8,
    fill: "var(--svg-group-soft)", stroke: "var(--svg-group)", "stroke-width": 1.5});
  rg.appendChild(topBg);

  var arrowD = anim.svgEl("text", {x: RW / 2, y: BUCKET_Y - 18, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "var(--svg-ink-muted)"});
  arrowD.textContent = "↓ count into buckets by one key byte (zero comparisons)";
  rg.appendChild(arrowD);

  var titleMid = anim.svgEl("text", {x: 20, y: BUCKET_Y - 4, "font-size": 13, "font-weight": 700, fill: "var(--svg-token-ink)"});
  titleMid.textContent = "② Month buckets";
  rg.appendChild(titleMid);

  var bucketRects = {};
  var bucketCountLbls = {};
  var bucketGroups = {};
  var arrivals = {};   // month -> rows already in that bucket (build time)
  var bucketTotal = {};
  for (var bt = 0; bt < items.length; bt++) bucketTotal[items[bt].month] = (bucketTotal[items[bt].month] || 0) + 1;
  // Pills sit side by side in their bucket, centred, in arrival order.
  function bucketSlotX(month, slot) {
    var count = bucketTotal[month];
    var step = Math.min(86, (bw - 10) / Math.max(1, count));
    return bucketXs[month].cx + (slot - (count - 1) / 2) * step;
  }
  for (var bk = 0; bk < numB; bk++) {
    var m = months[bk];
    var bp = bucketXs[m];
    var clr = BUCKET_CLRS[bk %% BUCKET_CLRS.length];
    var bgrp = anim.svgEl("g", {opacity: 0});
    var br = anim.svgEl("rect", {x: bp.x, y: BUCKET_Y, width: bw, height: BUCKET_H, rx: 8,
      fill: "var(--svg-token-soft)", stroke: clr, "stroke-width": 2});
    bgrp.appendChild(br);
    var blbl = anim.svgEl("text", {x: bp.cx, y: BUCKET_Y + 20, "text-anchor": "middle",
      "font-size": 13, "font-weight": 700, fill: clr});
    blbl.textContent = MONTH_NAMES[m] || ("M" + m);
    bgrp.appendChild(blbl);
    var bcnt = anim.svgEl("text", {x: bp.cx, y: BUCKET_Y + BUCKET_H - 7, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "var(--svg-ink-muted)"});
    bcnt.textContent = "0 rows";
    bgrp.appendChild(bcnt);
    rg.appendChild(bgrp);
    bucketRects[m] = br;
    bucketCountLbls[m] = bcnt;
    bucketGroups[m] = bgrp;
    arrivals[m] = 0;
  }

  var cmpCounter = anim.svgEl("text", {x: RW - 24, y: BUCKET_Y - 4,
    "text-anchor": "end", "font-size": 12, "font-weight": 700, fill: "var(--svg-ok)", opacity: 0});
  cmpCounter.textContent = "Comparisons: 0";
  rg.appendChild(cmpCounter);

  var arrowD2 = anim.svgEl("text", {x: RW / 2, y: RESULT_Y - 18, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "var(--svg-ink-muted)"});
  arrowD2.textContent = "↓ gather buckets left to right";
  rg.appendChild(arrowD2);

  var titleBot = anim.svgEl("text", {x: 20, y: RESULT_Y - 4, "font-size": 13, "font-weight": 700, fill: "var(--svg-ok)"});
  titleBot.textContent = "③ Sorted result";
  rg.appendChild(titleBot);

  var botBg = anim.svgEl("rect", {x: 20, y: RESULT_Y, width: RW - 40, height: 36, rx: 8,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1.5, opacity: 0.3});
  rg.appendChild(botBg);

  var statusLbl = anim.svgEl("text", {x: RW / 2, y: RH + 40, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "var(--svg-ink)"});
  statusLbl.textContent = "";
  rg.appendChild(statusLbl);

  // ---- Build timeline ----
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var pills = [];

  // Phase 1: the rows in the buffer, in read order
  tl.mark("Load rows");
  tl.beat("fill");
  tl.call(function() {
    stage.svg.appendChild(rg);
    stage.baseGroup.setAttribute("display", "none");
    phase.textContent = "Phase 1/3: all " + totalRows + " orders rows fit in the sort buffer (" + n +
      " drawn), in the order they are read. The sort key is order_date, 3 bytes, so MariaDB radix sorts";
  });
  for (var pi = 0; pi < n; pi++) {
    (function(idx) {
      tl.call(function() {
        var label = orderLabel(orderIndices[idx]);
        var tw = Math.max(70, label.length * 5.5 + 14);
        var g = anim.svgEl("g", {opacity: 0});
        translate(g, pillXs[idx], ROW_Y);
        var bg = anim.svgEl("rect", {x: -tw/2, y: -10, width: tw, height: 20, rx: 10,
          fill: "var(--svg-group)", stroke: "var(--svg-ink)", "stroke-width": 1});
        g.appendChild(bg);
        var txt = anim.svgEl("text", {x: 0, y: 4, "text-anchor": "middle",
          "font-size": 7.5, "font-weight": 700, fill: "var(--svg-on-solid)"});
        txt.textContent = label;
        g.appendChild(txt);
        rg.appendChild(g);
        pills[idx] = g;
        statusLbl.textContent = (idx + 1) + "/" + n + " rows in the buffer";
      });
      tl.add({from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
        onUpdate: function(v) { pills[idx].setAttribute("opacity", v.toFixed(2)); }});
      tl.delay(fast ? 40 : 60);
    })(pi);
  }
  tl.delay(250);

  // Pass 1: the day byte (least significant). Rows come back in day order.
  tl.mark("Pass 1: day");
  tl.beat("radix-pass");
  tl.call(function() {
    phase.textContent = "Radix pass 1 (the least significant part of the key, the day): rows are counted " +
      "into buckets by day and written back in day order, with no comparisons";
    statusLbl.textContent = "Day order: " + byDay.map(function(it) { return ORDERS[it.oi].customer + " (" +
      ORDERS[it.oi].date.slice(4) + ")"; }).join(", ");
  });
  tl.add({from: 0, to: 1, duration: 700, ease: anim.easeInOutCubic,
    onUpdate: function(t) {
      for (var k = 0; k < n; k++) {
        var from = pillXs[k], to = pillXs[dayRank[k]];
        translate(pills[k], from + (to - from) * t, ROW_Y - Math.sin(t * Math.PI) * 14 * (k %% 2 ? 1 : -1));
      }
    }});
  tl.delay(250);

  // Pass 2: the month byte. Buckets appear first (show the container
  // before filling it), then rows drop in, in day order (stable).
  tl.mark("Pass 2: month buckets");
  for (var rv = 0; rv < numB; rv++) {
    (function(rvIdx) {
      var rvM = months[rvIdx];
      tl.add({from: 0, to: 1, duration: 120, ease: anim.easeOutCubic,
        onUpdate: function(v) { bucketGroups[rvM].setAttribute("opacity", v.toFixed(2)); }});
    })(rv);
  }

  for (var di = 0; di < byDay.length; di++) {
    (function(item) {
      var idx = item.idx;
      var oi = item.oi;
      var month = item.month;
      var bp = bucketXs[month];
      var bkIdx = months.indexOf(month);
      var clr = BUCKET_CLRS[bkIdx >= 0 ? bkIdx %% BUCKET_CLRS.length : 0];
      var slotInBucket = arrivals[month];
      arrivals[month] += 1;
      var countAfter = arrivals[month];
      var startX = pillXs[dayRank[idx]];
      var endX = bucketSlotX(month, slotInBucket);
      var endY = BUCKET_Y + 40;

      tl.beat("radix-bucket");
      tl.call(function() {
        cmpCounter.setAttribute("opacity", 1);
        pills[idx].firstChild.setAttribute("fill", "var(--svg-build)");
        phase.textContent = "Radix pass 2 (the month): " + ORDERS[oi].customer + " (" + ORDERS[oi].date +
          ") goes to the " + (MONTH_NAMES[month] || month) + " bucket by its month alone; comparisons so far: 0";
        statusLbl.textContent = ORDERS[oi].customer + ": month byte → " + (MONTH_NAMES[month] || month);
      });
      tl.delay(fast ? 60 : 140);
      tl.add({from: 0, to: 1, duration: dur, ease: anim.easeOutBack,
        onUpdate: function(t) {
          pills[idx].firstChild.setAttribute("fill", clr);
          translate(pills[idx], startX + (endX - startX) * t, ROW_Y + (endY - ROW_Y) * t - Math.sin(t * Math.PI) * 35);
        },
        onComplete: function() {
          anim.arrival(bucketRects[month], {peakWidth: 4.5, baseWidth: 2});
          bucketCountLbls[month].textContent = countAfter + " row" + (countAfter === 1 ? "" : "s");
          statusLbl.textContent = ORDERS[oi].customer + " (" + ORDERS[oi].date +
            ") → " + (MONTH_NAMES[month] || month) + " bucket  •  Comparisons: 0";
        }});
      tl.delay(fast ? dly : dly + 50);
    })(byDay[di]);
  }
  tl.delay(300);

  // Gather from buckets into sorted order, left to right
  tl.mark("Gather sorted");
  tl.beat("radix-gather");
  tl.call(function() {
    phase.textContent = "Radix: gather the buckets left to right. Inside each month the rows are " +
      "already in day order from pass 1, so the result is sorted by order_date";
    botBg.setAttribute("opacity", 1);
  });
  for (var gi = 0; gi < sorted.length; gi++) {
    (function(pos, item) {
      var targetX = pillXs[pos];
      var bp = bucketXs[item.month];
      var slotInBucket = 0;
      for (var q = 0; q < byDay.length && byDay[q].idx !== item.idx; q++) {
        if (byDay[q].month === item.month) slotInBucket++;
      }
      var fromX = bucketSlotX(item.month, slotInBucket);
      var fromY = BUCKET_Y + 40;
      tl.add({from: 0, to: 1, duration: dur, ease: anim.easeOutBack,
        onUpdate: function(t) {
          pills[item.idx].firstChild.setAttribute("fill", PLACED);
          translate(pills[item.idx], fromX + (targetX - fromX) * t,
            fromY + (RESULT_Y + 18 - fromY) * t + Math.sin(t * Math.PI) * 22);
        },
        onComplete: function() {
          statusLbl.textContent = ORDERS[item.oi].customer + " (" + ORDERS[item.oi].date +
            ") → sorted position " + (pos + 1) + "/" + sorted.length;
        }});
      tl.delay(fast ? 40 : 60);
    })(gi, sorted[gi]);
  }
  tl.delay(200);

  tl.mark("Done");
  tl.beat("done-mem");
  tl.call(function() {
    phase.textContent = "✓ " + totalRows + " rows sorted in memory by MariaDB's radix sort: 0 comparisons, " +
      "no tmpdir. MySQL has no radix sort and sorts the same buffer with a comparison sort";
    statusLbl.textContent = "Sorted by order_date ✔ (radix: " + n + " rows, 0 comparisons)";
  });
  tl.delay(300);

  return tl;
}

// ---- Priority queue (bounded heap) with visible seat boxes ----
// k "seat" boxes below the buffer. Each arriving row takes a free seat; once
// the seats are full it is compared with the latest date seated (the heap's
// top): an earlier date evicts it, a later one is turned away. The heap is
// simulated at build time so every move is a timeline tween and a jump to
// any step shows the seats exactly as they are after that row.
function addPQToTimeline(tl, slotGs, slotXs, orderIndices, limitK) {
  // slotGs is filled while the timeline plays, so count from the indices
  // and look pills up inside the steps.
  var n = orderIndices.length;
  var dur = 320;
  var HEAP_CLR    = "var(--svg-token)";
  var EVICT_CLR   = "var(--svg-danger)";
  var KEEP_CLR    = "var(--svg-ok)";
  var INCOMING    = "var(--svg-group)";
  var SEAT_EMPTY  = "var(--svg-node-muted)";
  var SEAT_BORDER = "var(--svg-ink-muted)";
  var phase = document.getElementById("phase-label");

  var seatY = 150;
  var seatH = 32;
  var seatGap = 10;
  var totalSW = Math.min(W - 80, limitK * 110);
  var sw = (totalSW - seatGap * (limitK - 1)) / limitK;
  var seatStartX = (W - totalSW) / 2;
  var seatEls = [];
  var seatCx = [];
  for (var sc = 0; sc < limitK; sc++) seatCx.push(seatStartX + sc * (sw + seatGap) + sw / 2);
  var seatCy = seatY + seatH / 2;

  // Simulate the bounded heap at build time.
  var seats = [];
  for (var s0 = 0; s0 < limitK; s0++) seats.push(null);
  var plan = [];
  for (var p = 0; p < n; p++) {
    var key = DATE_KEYS[orderIndices[p]];
    var free = seats.indexOf(null);
    if (free >= 0) {
      seats[free] = {pos: p, key: key};
      plan.push({type: "seat", seat: free, filled: limitK - seats.filter(function(x) { return x === null; }).length});
      continue;
    }
    var maxSeat = 0;
    for (var h = 1; h < limitK; h++) if (seats[h].key > seats[maxSeat].key) maxSeat = h;
    if (key < seats[maxSeat].key) {
      plan.push({type: "evict", seat: maxSeat, evicted: seats[maxSeat].pos});
      seats[maxSeat] = {pos: p, key: key};
    } else {
      plan.push({type: "reject", seat: maxSeat, maxPos: seats[maxSeat].pos});
    }
  }

  tl.call(function() {
    var hdr = anim.svgEl("text", {
      x: W / 2, y: seatY - 6, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: "var(--svg-ink-muted)"
    });
    hdr.textContent = "Priority queue: " + limitK + " seats, only the earliest dates stay";
    stage.svg.appendChild(hdr);
    stage.tuples.push(hdr);
    for (var si = 0; si < limitK; si++) {
      var sx = seatStartX + si * (sw + seatGap);
      var rect = anim.svgEl("rect", {
        x: sx, y: seatY, width: sw, height: seatH, rx: 6, ry: 6,
        fill: SEAT_EMPTY, stroke: SEAT_BORDER, "stroke-width": 1.5, opacity: 0
      });
      // Under the pills, so a seated row stays visible.
      stage.svg.insertBefore(rect, stage.svg.firstChild);
      stage.tuples.push(rect);
      var lbl = anim.svgEl("text", {
        x: sx + sw / 2, y: seatY + seatH + 12, "text-anchor": "middle",
        "font-size": 8, "font-weight": 600, fill: "var(--svg-ink-muted)", opacity: 0
      });
      lbl.textContent = "empty";
      stage.svg.appendChild(lbl);
      stage.tuples.push(lbl);
      seatEls.push({rect: rect, lbl: lbl});
    }
    stage.bufContent.textContent = "Priority queue: " + limitK + " empty seats waiting";
  });
  tl.add({from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
    onUpdate: function(v) {
      for (var i = 0; i < seatEls.length; i++) {
        seatEls[i].rect.setAttribute("opacity", v.toFixed(2));
        seatEls[i].lbl.setAttribute("opacity", v.toFixed(2));
      }
    }});
  tl.delay(200);

  for (var pi = 0; pi < n; pi++) {
    (function(p, step) {
      var order = ORDERS[orderIndices[p]];
      var g = null;
      var sx = slotXs[p];
      var seatX = seatCx[step.seat];

      tl.beat("pq-row");
      tl.call(function() {
        g = slotGs[p];
        g.firstChild.setAttribute("fill", INCOMING);
        g.setAttribute("opacity", 1);
        stage.bufContent.textContent = order.customer + " (" + order.date + ") arrives…";
      });
      tl.delay(100);

      if (step.type === "seat") {
        tl.call(function() {
          phase.textContent = "Row " + (p + 1) + " of " + n + ": " + order.customer + " (" + order.date +
            ") takes free seat " + (step.seat + 1) + "; " + step.filled + " of " + limitK + " seats filled";
          stage.bufContent.textContent = order.customer + " → seat " + (step.seat + 1) +
            " (" + step.filled + "/" + limitK + " filled)";
        });
        tl.add({from: 0, to: 1, duration: dur, ease: anim.easeOutBack,
          onUpdate: function(t) {
            translate(g, sx + (seatX - sx) * t, 75 + (seatCy - 75) * t - Math.sin(t * Math.PI) * 20);
          },
          onComplete: function() {
            g.firstChild.setAttribute("fill", HEAP_CLR);
            seatEls[step.seat].rect.setAttribute("fill", "var(--svg-token-soft)");
            seatEls[step.seat].lbl.textContent = order.customer;
            seatEls[step.seat].lbl.setAttribute("fill", "var(--svg-token)");
          }});
      } else if (step.type === "evict") {
        var out = ORDERS[orderIndices[step.evicted]];
        var evG = null;
        tl.call(function() {
          evG = slotGs[step.evicted];
          seatEls[step.seat].rect.setAttribute("fill", "var(--svg-highlight)");
          phase.textContent = "Row " + (p + 1) + " of " + n + ": " + order.customer + " (" + order.date +
            ") is earlier than the latest seated date, " + out.date + " (" + out.customer + "), so " +
            out.customer + " is evicted";
          stage.bufContent.textContent = order.customer + " (" + order.date + ") < heap max " + out.date +
            " → evicts " + out.customer;
        });
        tl.add({from: 0, to: 1, duration: 250, ease: anim.easeInCubic,
          onUpdate: function(t) {
            evG.firstChild.setAttribute("fill", EVICT_CLR);
            translate(evG, seatX + t * 60, seatCy);
            evG.setAttribute("opacity", (1 - t).toFixed(2));
          }});
        tl.add({from: 0, to: 1, duration: dur, ease: anim.easeOutBack,
          onUpdate: function(t) {
            translate(g, sx + (seatX - sx) * t, 75 + (seatCy - 75) * t - Math.sin(t * Math.PI) * 18);
          },
          onComplete: function() {
            g.firstChild.setAttribute("fill", KEEP_CLR);
            seatEls[step.seat].rect.setAttribute("fill", "var(--svg-ok-soft)");
            seatEls[step.seat].lbl.textContent = order.customer;
            seatEls[step.seat].lbl.setAttribute("fill", "var(--svg-ok)");
          }});
      } else {
        var top = ORDERS[orderIndices[step.maxPos]];
        tl.call(function() {
          seatEls[step.seat].rect.setAttribute("fill", "var(--svg-highlight)");
          phase.textContent = "Row " + (p + 1) + " of " + n + ": " + order.customer + " (" + order.date +
            ") is not earlier than the latest seated date, " + top.date + " (" + top.customer +
            "), so it is turned away";
          stage.bufContent.textContent = order.customer + " (" + order.date + ") ≥ heap max → turned away";
        });
        tl.add({from: 0, to: 1, duration: 250, ease: anim.easeInCubic,
          onUpdate: function(t) {
            g.firstChild.setAttribute("fill", EVICT_CLR);
            translate(g, sx, 75 - 6 * t);
            g.setAttribute("opacity", (1 - t * 0.8).toFixed(2));
          },
          onComplete: function() {
            seatEls[step.seat].rect.setAttribute("fill", "var(--svg-token-soft)");
          }});
      }
      tl.delay(120);
    })(pi, plan[pi]);
  }

  tl.delay(200);
}

function buildStage(numRuns) {
  var svg = document.getElementById("filesort-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // Everything but the status line sits in baseGroup, so the radix sort can
  // hide it and draw its buckets in the same SVG.
  var baseGroup = anim.svgEl("g", {});
  svg.appendChild(baseGroup);

  // ---- Zone 1: Sort buffer (top) ----
  var bufLbl = anim.svgEl("text", {
    x: 20, y: 28, "font-size": 13, "font-weight": 700, fill: "var(--svg-group)"
  });
  bufLbl.textContent = "① sort buffer (sort_buffer_size)";
  baseGroup.appendChild(bufLbl);

  var bufRect = anim.svgEl("rect", {
    x: 20, y: 40, width: W - 40, height: 70, rx: 8, ry: 8,
    fill: "var(--svg-group-soft)", stroke: "var(--svg-group)", "stroke-width": 1.5
  });
  baseGroup.appendChild(bufRect);

  var bufContent = anim.svgEl("text", {
    x: W - 20, y: 28, "text-anchor": "end",
    "font-size": 11, "font-weight": 600, fill: "var(--svg-group)"
  });
  bufContent.textContent = "(rows fill here, get sorted, then spill to disk)";
  baseGroup.appendChild(bufContent);

  var spillGroup = anim.svgEl("g", {});
  baseGroup.appendChild(spillGroup);
  // The tmpdir part (arrows, title, run boxes): hidden by the priority queue.
  var diskGroup = anim.svgEl("g", {});
  spillGroup.appendChild(diskGroup);

  // ---- Arrow: buffer → tmpdir ----
  var arrow1Y = 118;
  var arrow1 = anim.svgEl("path", {
    d: "M" + (W/2) + "," + arrow1Y + " L" + (W/2) + "," + (arrow1Y + 24) +
       " M" + (W/2 - 6) + "," + (arrow1Y + 18) + " L" + (W/2) + "," + (arrow1Y + 24) +
       " L" + (W/2 + 6) + "," + (arrow1Y + 18),
    stroke: "var(--svg-node-stroke)", "stroke-width": 2, fill: "none"
  });
  diskGroup.appendChild(arrow1);
  var arrow1Lbl = anim.svgEl("text", {
    x: W/2 + 16, y: arrow1Y + 16, "font-size": 10, fill: "var(--svg-ink-muted)", "font-weight": 600
  });
  arrow1Lbl.textContent = "write sorted run";
  diskGroup.appendChild(arrow1Lbl);

  // ---- Zone 2: Sorted runs on disk (middle) ----
  var diskY = 152;
  var diskLbl = anim.svgEl("text", {
    x: 20, y: diskY, "font-size": 13, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  diskLbl.textContent = "② tmpdir: sorted runs on disk";
  diskGroup.appendChild(diskLbl);

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
      fill: "var(--svg-build-soft)", stroke: "var(--svg-node-stroke)", "stroke-width": 1.5, opacity: 0.3
    });
    diskGroup.appendChild(rect);
    var lbl = anim.svgEl("text", {
      x: x + rw / 2, y: diskY + 42, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: "var(--svg-build-ink)", opacity: 0.3
    });
    lbl.textContent = "Run " + (i + 1);
    diskGroup.appendChild(lbl);
    runRects.push({rect: rect, label: lbl});
  }
  if (hasMore) {
    var moreX = startX + cap * (rw + gap) + 8;
    var more = anim.svgEl("text", {
      x: moreX, y: diskY + 32, "font-size": 12, "font-weight": 600,
      fill: "var(--svg-build-ink)", "font-style": "italic"
    });
    more.textContent = "… + " + (numRuns - cap) + " more run" + (numRuns - cap === 1 ? "" : "s");
    diskGroup.appendChild(more);
    var moreSub = anim.svgEl("text", {
      x: moreX, y: diskY + 48, "font-size": 10, fill: "var(--svg-ink-muted)"
    });
    moreSub.textContent = "(each: fill, sort, write)";
    diskGroup.appendChild(moreSub);
  }

  // ---- Arrow: tmpdir → merge ----
  var arrow2Y = diskY + 70;
  var arrow2 = anim.svgEl("path", {
    d: "M" + (W/2) + "," + arrow2Y + " L" + (W/2) + "," + (arrow2Y + 24) +
       " M" + (W/2 - 6) + "," + (arrow2Y + 18) + " L" + (W/2) + "," + (arrow2Y + 24) +
       " L" + (W/2 + 6) + "," + (arrow2Y + 18),
    stroke: "var(--svg-node-stroke)", "stroke-width": 2, fill: "none"
  });
  diskGroup.appendChild(arrow2);
  var arrow2Lbl = anim.svgEl("text", {
    x: W/2 + 16, y: arrow2Y + 16, "font-size": 10, fill: "var(--svg-ink-muted)", "font-weight": 600
  });
  arrow2Lbl.textContent = "merge: smallest head first";
  diskGroup.appendChild(arrow2Lbl);

  // ---- Zone 3: Merge output (bottom) ----
  var mergeY = arrow2Y + 32;
  var mergeLbl = anim.svgEl("text", {
    x: 20, y: mergeY, "font-size": 13, "font-weight": 700, fill: "var(--svg-ok)"
  });
  mergeLbl.textContent = "③ final sorted output";
  spillGroup.appendChild(mergeLbl);

  var mergeRect = anim.svgEl("rect", {
    x: 20, y: mergeY + 12, width: W - 40, height: 50, rx: 8, ry: 8,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1.5, opacity: 0.3
  });
  spillGroup.appendChild(mergeRect);

  var mergeContent = anim.svgEl("text", {
    x: W - 20, y: mergeY, "text-anchor": "end",
    "font-size": 11, "font-weight": 600, fill: "var(--svg-ok)", opacity: 0.3
  });
  mergeContent.textContent = "";
  spillGroup.appendChild(mergeContent);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 16, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "var(--svg-ink)"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, baseGroup: baseGroup, bufRect: bufRect, bufContent: bufContent,
    runRects: runRects, totalRuns: numRuns,
    mergeRect: mergeRect, mergeContent: mergeContent, mergeLbl: mergeLbl,
    statusLbl: statusLbl, spillGroup: spillGroup, diskGroup: diskGroup, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  stage.baseGroup.removeAttribute("display");
  stage.diskGroup.removeAttribute("display");
  stage.bufRect.setAttribute("fill", "var(--svg-group-soft)");
  stage.bufContent.textContent = "(rows fill here, get sorted, then spill to disk)";
  stage.spillGroup.setAttribute("opacity", "1");
  stage.runRects.forEach(function(r) {
    r.rect.setAttribute("opacity", 0.3);
    r.rect.setAttribute("fill", "var(--svg-build-soft)");
    r.rect.setAttribute("stroke", "var(--svg-node-stroke)");
    r.rect.setAttribute("stroke-width", "1.5");
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

// Spawn pills one by one into the buffer (timeline-owned fade-in).
function addFill(tl, gs, xs, indices, stepMs, onEach) {
  for (var k = 0; k < indices.length; k++) {
    (function(idx) {
      tl.call(function() {
        gs[idx] = spawnTuple(orderLabel(indices[idx]), "var(--svg-group)", xs[idx], 75);
        if (onEach) onEach(idx);
      });
      tl.add({from: 0, to: 1, duration: 150, ease: anim.easeOutCubic,
        onUpdate: function(v) { gs[idx].setAttribute("opacity", v.toFixed(2)); }});
      tl.delay(stepMs);
    })(k);
  }
}

function buildTimeline(numRuns, spill, sortAlg, pqSize, c, cost) {
  resetStage();
  var mariadb = c.server === "mariadb";

  // MariaDB's radix sort draws its own buckets in this SVG.
  if (sortAlg === "radix" && !spill) {
    var radixIndices = [];
    for (var ri2 = 0; ri2 < 6; ri2++) radixIndices.push(ri2);
    return buildRadixTimeline(radixIndices, false, c);
  }

  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var shown = stage.runRects.length;

  // Guided steps: in-memory paths step through filling, each partition
  // (or radix pass, or heap row) and the end; the spill path steps through
  // fill, sort and write for each run, then the merge. Kinds map to the
  // simplified-code lines (GUIDE in filesort.py).
  if (sortAlg === "priority_queue") {
    // Priority queue path: ORDER BY … LIMIT k
    var PQ_N = 8;
    var seatsK = Math.min(pqSize, PQ_N - 1);
    var pqXs = [];
    for (var pqi = 0; pqi < PQ_N; pqi++) pqXs.push(60 + pqi * 95);
    var pqGs = [];
    var pqIndices = [];
    for (var pqi2 = 0; pqi2 < PQ_N; pqi2++) pqIndices.push(pqi2);

    tl.mark("Load rows");
    tl.beat("pq-start");
    tl.call(function() {
      stage.diskGroup.setAttribute("display", "none");
      phase.textContent = "ORDER BY order_date LIMIT " + pqSize + ": only the " + pqSize + " earliest dates are " +
        "needed, so the server keeps a priority queue (a bounded heap) instead of sorting every row" +
        (seatsK < pqSize ? " (" + seatsK + " seats drawn)" : "");
      stage.bufRect.setAttribute("fill", "var(--svg-token-soft)");
    });
    addFill(tl, pqGs, pqXs, pqIndices, 60);
    tl.delay(200);

    tl.mark("Heap push/pop");
    addPQToTimeline(tl, pqGs, pqXs, pqIndices, seatsK);

    tl.mark("Done");
    tl.beat("pq-done");
    tl.call(function() {
      phase.textContent = "✓ Priority queue done: every row was seen once, only " + pqSize +
        " were kept, and they are sent in order. Nothing went to tmpdir";
      stage.mergeRect.setAttribute("opacity", 1);
      stage.mergeContent.setAttribute("opacity", 1);
      stage.mergeContent.textContent = "Top " + pqSize + " rows sent from the heap, earliest date first";
      stage.statusLbl.textContent = "Priority queue: O(n · log k), all rows read, only " + pqSize + " kept in memory.";
    });
    tl.delay(300);
    return tl;
  }

  if (!spill) {
    // All rows fit in memory: one comparison sort of the buffer
    var QS_N = 6;
    var qsXs = [];
    for (var qi = 0; qi < QS_N; qi++) qsXs.push(100 + qi * 120);
    var qsGs = [];
    var qsIndices = [];
    for (var qi2 = 0; qi2 < QS_N; qi2++) qsIndices.push(qi2);

    tl.mark("Fill sort buffer");
    tl.beat("fill");
    tl.call(function() {
      phase.textContent = "Phase 1/2: each orders row is copied into the sort buffer with its order_date key; " +
        "all " + fmtN(c.rows) + " rows fit, so nothing is written to tmpdir";
      stage.bufRect.setAttribute("fill", "var(--svg-group-soft)");
    });
    addFill(tl, qsGs, qsXs, qsIndices, 120, function(idx) {
      stage.bufContent.textContent = (idx + 1) + " rows in the buffer";
    });
    tl.delay(300);

    tl.mark("Sort the buffer");
    tl.call(function() {
      stage.bufContent.textContent = "Sorting by order_date…";
    });
    addQsToTimeline(tl, qsGs, qsXs, qsIndices, false);

    tl.mark("Sorted: done");
    tl.beat("done-mem");
    tl.call(function() {
      phase.textContent = mariadb
        ? "✓ Sorted in memory, no disk I/O. MariaDB used its own quicksort: the buffer holds " +
          fmtN(c.rows) + " rows, outside radix sort's 1,000 to 99,999"
        : "✓ Sorted in memory, no disk I/O. The drawing uses quicksort; MySQL sorts the buffer " +
          "with std::sort (100 rows or fewer) or std::stable_sort, both comparison sorts";
      stage.spillGroup.setAttribute("opacity", "1");
      stage.mergeRect.setAttribute("opacity", 1);
      stage.mergeContent.setAttribute("opacity", 1);
      stage.mergeContent.textContent = "Result sent directly from memory";
      stage.statusLbl.textContent = "In-memory sort: 0 sorted runs written, 0 merge passes.";
    });
    tl.delay(300);
    return tl;
  }

  // Spill path: fill buffer, sort, write each run to tmpdir, then merge
  var animRuns = Math.min(shown, numRuns);
  for (var ri = 0; ri < animRuns; ri++) {
    (function(runIdx) {
      // Three rows per drawn run: four runs use all 12 sample rows once.
      var chunkStart = (runIdx * RUN_ROWS) %% ORDERS.length;
      var chunkXs = [140, 400, 660];
      var chunkGs = [];
      var chunkIndices = [];
      for (var ck = 0; ck < RUN_ROWS; ck++) {
        chunkIndices.push((chunkStart + ck) %% ORDERS.length);
      }
      var names = chunkIndices.map(function(o) { return ORDERS[o].customer; }).join(", ");

      tl.mark("Run " + (runIdx + 1) + ": fill");
      tl.beat("fill");
      tl.call(function() {
        phase.textContent = "Run " + (runIdx + 1) + " of " + numRuns + ": the sort buffer fills with the next rows (" +
          names + ", ...) until sort_buffer_size is used up";
        stage.bufRect.setAttribute("fill", "var(--svg-group-soft)");
        stage.bufContent.textContent = "Filling with rows…";
      });
      addFill(tl, chunkGs, chunkXs, chunkIndices, 80);
      tl.delay(150);

      tl.mark("Run " + (runIdx + 1) + ": sort");
      tl.beat("run-sort");
      var bufRows = fmtN(cost.rpr);
      tl.call(function() {
        phase.textContent = "Run " + (runIdx + 1) + " of " + numRuns + ": the buffer is full (" + bufRows +
          " rows), so " + (sortAlg === "radix"
            ? "MariaDB radix sorts them by the 3-byte order_date key, with no comparisons"
            : serverName(c.server) + " sorts them by order_date with a comparison sort");
        stage.bufRect.setAttribute("fill", "var(--svg-group-soft)");
      });
      if (sortAlg === "radix") addRunRadixToTimeline(tl, chunkGs, chunkXs, chunkIndices);
      else addQsToTimeline(tl, chunkGs, chunkXs, chunkIndices, true);

      tl.mark("Run " + (runIdx + 1) + ": write");
      tl.beat("run-write");
      tl.call(function() {
        phase.textContent = "Run " + (runIdx + 1) + " of " + numRuns + ": the sorted buffer is written to tmpdir as " +
          "run " + (runIdx + 1) + ", and the buffer is emptied for the next rows";
        stage.bufContent.textContent = "Writing sorted run " + (runIdx + 1) + " to tmpdir…";
        stage.bufRect.setAttribute("fill", "var(--svg-highlight)");
        stage.runRects[runIdx].rect.setAttribute("opacity", 1);
        stage.runRects[runIdx].label.setAttribute("opacity", 1);
      });
      tl.add({from: 0, to: 1, duration: 400, ease: anim.easeOutCubic,
        onUpdate: function(v) {
          stage.runRects[runIdx].rect.setAttribute("fill",
            anim.lerpColor("var(--svg-highlight)", "var(--svg-build-soft)", v));
          for (var t = 0; t < chunkGs.length; t++) chunkGs[t].setAttribute("opacity", (1 - v).toFixed(2));
        }});
      tl.call(function() {
        stage.bufRect.setAttribute("fill", "var(--svg-group-soft)");
        stage.bufContent.textContent = "(buffer emptied for the next rows)";
        stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
        stage.tuples = [];
      });
      tl.delay(200);
    })(ri);
  }

  // Merge phase: labelled rows flow from the run boxes into the output,
  // always the smallest key among the run heads.
  tl.mark("Merge runs");
  tl.beat("merge");
  tl.call(function() {
    phase.textContent = "Merge: read the " + numRuns + " sorted runs back and repeatedly take the earliest " +
      "order_date among the run heads";
    stage.mergeRect.setAttribute("opacity", 1);
    stage.mergeContent.setAttribute("opacity", 1);
    stage.mergeContent.textContent = "Merging runs…";
  });
  tl.delay(300);

  var runQueues = [];
  for (var rq = 0; rq < animRuns; rq++) {
    var chunk = [];
    for (var ci2 = 0; ci2 < RUN_ROWS; ci2++) {
      chunk.push(((rq * RUN_ROWS) + ci2) %% ORDERS.length);
    }
    chunk.sort(function(a, b) { return DATE_KEYS[a] - DATE_KEYS[b]; });
    runQueues.push(chunk);
  }

  var mergeOrder = [];
  var heads = [];
  for (var h = 0; h < runQueues.length; h++) heads.push(0);
  var totalMerge = animRuns * RUN_ROWS;
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

  // Two rows so each labelled pill has room.
  var perRow = Math.max(1, Math.ceil(mergeOrder.length / 2));
  var mergeSlotW = Math.min(92, (W - 60) / perRow);
  var mergeBaseX = 30;
  var mergeTop = parseFloat(stage.mergeRect.getAttribute("y"));

  for (var mi2 = 0; mi2 < mergeOrder.length; mi2++) {
    (function(pick, slot) {
      var label = orderLabel(pick.orderIdx);
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
      var dstX = mergeBaseX + (slot %% perRow) * mergeSlotW + mergeSlotW / 2;
      var dstY = mergeTop + (slot < perRow ? 14 : 36);
      var ctrlY = (srcY + dstY) / 2 - 10;
      var pathFn = anim.path(srcX, srcY, (srcX + dstX) / 2, ctrlY, dstX, dstY);
      var pill = null;

      tl.call(function() {
        stage.mergeContent.textContent = "Pick: " + label + " (earliest head across runs)";
        if (runRect) {
          runRect.rect.setAttribute("stroke", "var(--svg-build)");
          runRect.rect.setAttribute("stroke-width", "2.5");
        }
        pill = spawnTuple(label, "var(--svg-ok)", srcX, srcY);
        pill.setAttribute("opacity", "1");
      });
      tl.add({
        from: 0, to: 1, duration: 420, ease: anim.easeInOutCubic,
        onUpdate: function(t) {
          if (!pill) return;
          var p = pathFn(t);
          translate(pill, p.x, p.y);
        },
        onComplete: function() {
          if (runRect) {
            runRect.rect.setAttribute("stroke", "var(--svg-node-stroke)");
            runRect.rect.setAttribute("stroke-width", "1.5");
          }
        }
      });
    })(mergeOrder[mi2], mi2);
  }

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
  tl.beat("done-merge");
  tl.call(function() {
    stage.mergeRect.setAttribute("fill", "var(--svg-ok-soft)");
    stage.mergeContent.textContent = "All " + numRuns + " runs merged → final sorted result";
    phase.textContent = "✓ Filesort done: " + numRuns + " sorted runs were written to tmpdir and merged" +
      " (the drawing shows " + animRuns + "). A bigger sort_buffer_size means fewer runs";
    stage.statusLbl.textContent = numRuns + " runs written + read back by the merge = extra disk I/O.";
  });
  tl.delay(300);

  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var limitVal = c.limit_rows || 0;
  var cost = filesortCost(c.rows, c.row_size, c.sbs, limitVal, c.server);
  return buildTimeline(cost.runs, cost.spill, cost.sortAlg, cost.pqSize, c, cost);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(rowSize, sbs, currentRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Row count to sort", yLabel: "Total I/O rows (read+write)",
    curves: [
      { label: "Filesort (this algorithm)", color: "var(--svg-group)",
        fn: function(n) { return filesortCost(n, rowSize, sbs).ioRows; } },
      { label: "Index-ordered scan (no sort)", color: "var(--svg-ok)",
        fn: function(n) { return n; } }
    ],
    current: { x: currentRows },
    xSlider: "rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(100, Math.min(1000000, xVal / 100) * 100)); }
  });
}

function algDisplayName(alg, spill) {
  if (alg === "radix") return spill ? "Radix sort per run (MariaDB)" : "Radix sort (MariaDB)";
  if (alg === "priority_queue") return "Priority queue";
  return "Comparison sort";
}
function algReason(cost, c, limitRows) {
  var rows = fmtN(c.rows);
  var inBuffer = fmtN(cost.bufferRows);
  if (cost.sortAlg === "priority_queue")
    return "LIMIT " + limitRows + " is small and fits in the buffer → a bounded heap of " +
           limitRows + " rows. Every one of the " + rows + " rows is seen, but they are never fully sorted.";
  if (cost.sortAlg === "radix")
    return "The sort key is order_date, " + SORT_KEY_BYTES + " B (20 B or less, and a fixed-size key is never " +
           "packed), and each buffer holds " + inBuffer + " rows (1,000 to 99,999): MariaDB radix sorts " +
           (cost.spill ? "every full run" : "it") + ", one counting pass per key byte with no comparisons. " +
           "The " + c.row_size + " B row size only decides how many rows fit. MySQL has no radix sort.";
  if (c.server === "mariadb")
    return "MariaDB sorts the buffer with its own quicksort, O(n log n) comparisons: radix sort needs " +
           "1,000 to 99,999 rows in the buffer, and this one holds " + inBuffer + ".";
  return "Comparison sort of the buffer: MySQL uses std::sort (up to 100 rows, or variable-length keys) " +
         "or std::stable_sort, O(n log n) comparisons. MySQL has no radix sort; MariaDB would radix sort " +
         "a buffer of 1,000 to 99,999 rows here, since order_date is a " + SORT_KEY_BYTES + "-byte key.";
}

function recompute() {
  var c = teachRuntime.readControls();
  var limitVal = c.limit_rows || 0;
  var cost = filesortCost(c.rows, c.row_size, c.sbs, limitVal, c.server);
  document.getElementById("out-rpr").textContent = teachRuntime.formatInt(cost.rpr);
  document.getElementById("out-runs").textContent = teachRuntime.formatInt(cost.runs);
  document.getElementById("out-merges").textContent = String(cost.merges);
  document.getElementById("out-io").textContent = teachRuntime.formatInt(cost.ioRows);
  document.getElementById("out-spill").textContent = cost.spill ? "Yes — disk I/O" : "No — in-memory";
  document.getElementById("out-spill").className = "value " + (cost.spill ? "hot" : "ok");
  document.getElementById("out-alg").textContent = algDisplayName(cost.sortAlg, cost.spill);
  document.getElementById("out-alg").className = "value " +
    (cost.sortAlg === "radix" ? "ok" : cost.sortAlg === "priority_queue" ? "ok" : "");
  document.getElementById("out-explanation").textContent =
    algReason(cost, c, limitVal) +
    (cost.spill
      ? " The buffer holds " + fmtN(cost.rpr) + " rows → " +
        fmtN(cost.runs) + " sorted runs spilled to tmpdir. K-way merge needs " +
        cost.merges + " pass(es). Bigger sort_buffer_size → fewer runs → less I/O."
      : (cost.sortAlg !== "priority_queue"
         ? " All rows fit in sort_buffer_size — no disk spill."
         : ""));
  buildStage(cost.runs);
  resetStage();
  renderChart(c.row_size, c.sbs, c.rows);
}

// Choosing a server moves sort_buffer_size to that server's default (256 KiB
// on MySQL, 2 MiB on MariaDB) while it is still at the other server's
// default; a value you set yourself stays. Registered before wire(), so the
// lesson and Explore redraw with the new value.
(function() {
  var sel = document.getElementById("server");
  var sbs = document.getElementById("sbs");
  if (!sel || !sbs) return;
  function followDefault() {
    var mine = sel.value === "mariadb" ? MARIADB_SORT_BUFFER_SIZE_DEFAULT : SORT_BUFFER_SIZE_DEFAULT;
    var other = sel.value === "mariadb" ? SORT_BUFFER_SIZE_DEFAULT : MARIADB_SORT_BUFFER_SIZE_DEFAULT;
    if (Number(sbs.value) === other) sbs.value = String(mine);
  }
  sel.addEventListener("input", followDefault);
  sel.addEventListener("change", followDefault);
})();

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
