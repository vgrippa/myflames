// ---- buffer_pool_warmup lesson: cold-start ↦ warm ↦ restart ------------
var POOL_SIZE = 24;   // Pool slots in the animation.
var QUERY_PAGES = [   // Pages this query reads (and will re-read).
  {t: "users",    p: 1001}, {t: "users",    p: 1002}, {t: "users",    p: 1003},
  {t: "users",    p: 1004}, {t: "users",    p: 1005}, {t: "orders",   p: 2001},
  {t: "orders",   p: 2002}, {t: "orders",   p: 2003}, {t: "products", p: 3001},
  {t: "products", p: 3002}
];

// Cost model (visible latencies — rough, verisimilar):
//   A cold read from disk is modelled at 6 ms/page (SSD); a warm read
//   at 0.06 ms/page (pool hit). Ratio ≈ 100×. Real hardware varies
//   wildly but the *contrast* is what the lesson teaches.
var DISK_MS = 6.0;
var RAM_MS  = 0.06;

var COLD_COLOR  = "#fecaca";  // light red — just read from disk
var WARM_COLOR  = "#bbf7d0";  // light green — pool hit
var EMPTY_COLOR = "#e5e7eb";  // grey — empty slot
var DUMP_COLOR  = "#fde68a";  // yellow — being dumped on shutdown

var CELL_W = 56, CELL_H = 22, GAP = 4;

function clearSvg(id) {
  var s = document.getElementById(id);
  while (s.firstChild) s.removeChild(s.firstChild);
  return s;
}

function buildPool(id) {
  var svg = clearSvg(id);
  var cells = [];
  var labels = [];
  var colsPerRow = 8;
  for (var i = 0; i < POOL_SIZE; i++) {
    var row = Math.floor(i / colsPerRow);
    var col = i % colsPerRow;
    var x = 16 + col * (CELL_W + GAP);
    var y = 48 + row * (CELL_H + GAP);
    var r = anim.svgEl("rect", {
      x: x, y: y, width: CELL_W, height: CELL_H,
      rx: 4, ry: 4, fill: EMPTY_COLOR,
      stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: x + CELL_W / 2, y: y + CELL_H / 2 + 3,
      "text-anchor": "middle", "font-size": 9, "font-weight": 600,
      fill: "#374151"
    });
    svg.appendChild(t);
    cells.push(r);
    labels.push(t);
  }
  // Title label above the grid.
  var lbl = anim.svgEl("text", {
    x: 16, y: 28, "font-size": 12, "font-weight": 700, fill: "#1e40af"
  });
  lbl.textContent = "InnoDB buffer pool (" + POOL_SIZE + " pages shown)";
  svg.appendChild(lbl);
  return {svg: svg, cells: cells, labels: labels, nextSlot: 0};
}

function findSlot(pool, pageKey) {
  for (var i = 0; i < pool.labels.length; i++) {
    if (pool.labels[i].textContent === pageKey) return i;
  }
  return -1;
}

function placePage(pool, pageKey, color) {
  var slot = pool.nextSlot % POOL_SIZE;
  pool.nextSlot = (pool.nextSlot + 1) % POOL_SIZE;
  pool.cells[slot].setAttribute("fill", color);
  pool.labels[slot].textContent = pageKey;
  anim.arrival(pool.cells[slot]);
  return slot;
}

function setStat(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

function runAct1(tl, pool) {
  // Cold start: every page is a miss; disk read.
  tl.call(function() {
    setStat("out-phase", "Act 1 — Cold start (empty pool)");
    setStat("out-hits", "0");
    setStat("out-misses", "0");
    setStat("out-total-ms", "0.00");
  });
  var misses = 0, totalMs = 0;
  for (var i = 0; i < QUERY_PAGES.length; i++) {
    (function(i) {
      var p = QUERY_PAGES[i];
      var key = p.t[0].toUpperCase() + ":" + p.p;
      tl.add({
        from: 0, to: 1, duration: 180, ease: anim.easeOutCubic,
        onUpdate: function() {},
        onComplete: function() {
          placePage(pool, key, COLD_COLOR);
          misses += 1;
          totalMs += DISK_MS;
          setStat("out-misses", String(misses));
          setStat("out-total-ms", totalMs.toFixed(2));
        }
      });
    })(i);
    tl.delay(60);
  }
  tl.delay(700);
}

function runAct2(tl, pool) {
  tl.call(function() {
    setStat("out-phase", "Act 2 — Warm (same query repeats)");
    // Reset live counters for this run.
    setStat("out-hits", "0");
    setStat("out-misses", "0");
    setStat("out-total-ms", "0.00");
  });
  var hits = 0, misses = 0, totalMs = 0;
  for (var i = 0; i < QUERY_PAGES.length; i++) {
    (function(i) {
      var p = QUERY_PAGES[i];
      var key = p.t[0].toUpperCase() + ":" + p.p;
      tl.add({
        from: 0, to: 1, duration: 120, ease: anim.easeOutCubic,
        onUpdate: function() {},
        onComplete: function() {
          var slot = findSlot(pool, key);
          if (slot >= 0) {
            // Hit — flash green over the existing cell.
            pool.cells[slot].setAttribute("fill", WARM_COLOR);
            anim.arrival(pool.cells[slot], {peakWidth: 2.0, durationMs: 220});
            hits += 1;
            totalMs += RAM_MS;
          } else {
            placePage(pool, key, COLD_COLOR);
            misses += 1;
            totalMs += DISK_MS;
          }
          setStat("out-hits", String(hits));
          setStat("out-misses", String(misses));
          setStat("out-total-ms", totalMs.toFixed(2));
        }
      });
    })(i);
    tl.delay(40);
  }
  tl.delay(800);
}

function runAct3(tl, pool) {
  // Dump on shutdown → restart → load.
  tl.call(function() {
    setStat("out-phase",
      "Act 3 — Shutdown dumps the hottest 25% to ib_buffer_pool, "
      + "restart loads it back asynchronously");
  });
  // Flash the first 25% of occupied cells as "being dumped".
  var occupied = [];
  for (var i = 0; i < pool.cells.length; i++) {
    if (pool.cells[i].getAttribute("fill") !== EMPTY_COLOR) {
      occupied.push(pool.cells[i]);
    }
  }
  var dumpCount = Math.max(1, Math.ceil(occupied.length * 0.25));
  for (var i = 0; i < dumpCount; i++) {
    (function(i) {
      tl.add({
        from: 0, to: 1, duration: 140, ease: anim.easeOutCubic,
        onUpdate: function() {},
        onComplete: function() {
          occupied[i].setAttribute("fill", DUMP_COLOR);
          anim.arrival(occupied[i], {peakWidth: 2.0, durationMs: 200});
        }
      });
      tl.delay(30);
    })(i);
  }
  tl.delay(500);
  // Clear all non-dumped pages to empty (simulate restart wiping RAM).
  tl.call(function() {
    for (var i = 0; i < pool.cells.length; i++) {
      if (pool.cells[i].getAttribute("fill") !== DUMP_COLOR) {
        pool.cells[i].setAttribute("fill", EMPTY_COLOR);
        pool.labels[i].textContent = "";
      }
    }
    setStat("out-phase",
      "Restart complete — dumped pages reload async "
      + "(innodb_buffer_pool_load_now fires immediately and returns)");
  });
  // Fade the dumped cells back to warm.
  tl.delay(400);
  tl.call(function() {
    for (var i = 0; i < pool.cells.length; i++) {
      if (pool.cells[i].getAttribute("fill") === DUMP_COLOR) {
        pool.cells[i].setAttribute("fill", WARM_COLOR);
        anim.arrival(pool.cells[i]);
      }
    }
  });
}

function buildCurrentTimeline() {
  var pool = buildPool("svg-pool");
  var tl = anim.timeline();
  runAct1(tl, pool);
  runAct2(tl, pool);
  runAct3(tl, pool);
  return tl;
}

function resetAnim() {
  buildPool("svg-pool");
  setStat("out-phase", "Ready — press Play");
  setStat("out-hits", "—");
  setStat("out-misses", "—");
  setStat("out-total-ms", "—");
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

// Build stage on first load so it's not empty, then wire the runtime
// so Play/Pause/Scrub work like every other lesson.
document.addEventListener("DOMContentLoaded", function() {
  buildPool("svg-pool");
});
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
