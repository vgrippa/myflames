// ---- buffer_pool_warmup lesson: cold-start ↦ warm ↦ restart ------------
var POOL_SIZE = 24;   // Pool slots in the animation.
var QUERY_PAGES = [   // Pages this query reads (and will re-read).
  {t: "users",    p: 1001}, {t: "users",    p: 1002}, {t: "users",    p: 1003},
  {t: "users",    p: 1004}, {t: "users",    p: 1005}, {t: "orders",   p: 2001},
  {t: "orders",   p: 2002}, {t: "orders",   p: 2003}, {t: "products", p: 3001},
  {t: "products", p: 3002}
];
// Illustrative tablespace IDs: ib_buffer_pool lists "space,page" per line.
var SPACE_IDS = {users: 12, orders: 13, products: 14};

// Cost model (visible latencies — illustrative, not measured):
//   A cold read from disk is modelled at 6 ms/page (a spinning disk; an
//   SSD read is nearer 0.1 ms); a warm read at 0.06 ms/page (pool hit).
//   Real hardware varies widely but the *contrast* is what the lesson
//   teaches.
var DISK_MS = 6.0;
var RAM_MS  = 0.06;
// innodb_buffer_pool_dump_pct (default 25 in both servers) comes from the
// controls. MySQL dumps that share of the pages in each buffer pool's LRU
// list; MariaDB caps the dump at that share of the pool's size. Both take
// the pages from the most recently used end, and at least one.
function dumpCount(server, pct, cachedPages) {
  if (pct >= 100) return cachedPages;
  var n = server === "mariadb"
    ? Math.min(cachedPages, Math.floor(POOL_SIZE * pct / 100))
    : Math.floor(cachedPages * pct / 100);
  return Math.max(1, n);
}

var COLD_COLOR  = "var(--svg-danger-soft)";  // light red — just read from disk
var WARM_COLOR  = "var(--svg-ok-soft)";  // light green — pool hit
var EMPTY_COLOR = "var(--svg-node-muted)";  // grey — empty slot
var DUMP_COLOR  = "var(--svg-highlight)";  // yellow — listed in ib_buffer_pool at shutdown

var CELL_W = 56, CELL_H = 22, GAP = 4;

// Guided steps: each kind names the simplified-code lines it runs (GUIDE in
// buffer_pool_warmup.py). Kinds come from this table so each is a literal.
var BEAT_KINDS = {
  coldRead: "cold-read",
  warmHit: "warm-hit",
  dump: "dump",
  restart: "restart",
  load: "load"
};

function pageKey(p) {
  return p.t[0].toUpperCase() + ":" + p.p;
}

function clearSvg(id) {
  var s = document.getElementById(id);
  while (s.firstChild) s.removeChild(s.firstChild);
  return s;
}

function svgText(svg, attrs, text) {
  var t = anim.svgEl("text", attrs);
  t.textContent = text || "";
  svg.appendChild(t);
  return t;
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
      stroke: "var(--svg-node-stroke)", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: x + CELL_W / 2, y: y + CELL_H / 2 + 3,
      "text-anchor": "middle", "font-size": 9, "font-weight": 600,
      fill: "var(--svg-ink)"
    });
    svg.appendChild(t);
    cells.push(r);
    labels.push(t);
  }
  // Title and key above the grid.
  svgText(svg, {x: 16, y: 22, "font-size": 12, "font-weight": 700, fill: "var(--svg-token-ink)"},
    "InnoDB buffer pool (" + POOL_SIZE + " pages shown)");
  svgText(svg, {x: 16, y: 38, "font-size": 9, fill: "var(--svg-ink-muted)"},
    "U:1001 = users page 1001, O = orders, P = products · red = read from disk, green = in memory");
  // Captions in the scene: what this step did, and the run so far.
  var note = svgText(svg, {x: 250, y: 146, "text-anchor": "middle", "font-size": 11, "font-weight": 600, fill: "var(--svg-ink)"}, "");
  var tally = svgText(svg, {x: 250, y: 164, "text-anchor": "middle", "font-size": 10, "font-weight": 600, fill: "var(--svg-ink-muted)"}, "");
  // The dump file on disk (shown from the shutdown step on).
  var fileBg = anim.svgEl("rect", {
    x: 16, y: 180, width: 468, height: 56, rx: 6, ry: 6,
    fill: "var(--svg-highlight)", "fill-opacity": 0.18, stroke: "var(--svg-highlight-stroke)",
    "stroke-width": 1.2, "stroke-dasharray": "4 3", opacity: 0
  });
  svg.appendChild(fileBg);
  var fileTitle = svgText(svg, {x: 28, y: 198, "font-size": 10, "font-weight": 700, fill: "var(--svg-ink)", opacity: 0},
    "ib_buffer_pool (a small file in the data directory, kept across the restart)");
  var fileBody = svgText(svg, {x: 28, y: 222, "font-size": 11, "font-weight": 600, fill: "var(--svg-ink)",
    "font-family": "ui-monospace, Menlo, monospace", opacity: 0}, "");
  return {svg: svg, cells: cells, labels: labels, nextSlot: 0, lru: [],
          note: note, tally: tally, file: [fileBg, fileTitle, fileBody], fileBody: fileBody};
}

function findSlot(pool, key) {
  for (var i = 0; i < pool.labels.length; i++) {
    if (pool.labels[i].textContent === key) return i;
  }
  return -1;
}

// A page read from disk takes the next free cell and becomes the most
// recently used page (head of pool.lru).
function placePage(pool, key, color) {
  var slot = pool.nextSlot % POOL_SIZE;
  pool.nextSlot = (pool.nextSlot + 1) % POOL_SIZE;
  pool.cells[slot].setAttribute("fill", color);
  pool.labels[slot].textContent = key;
  pool.lru.unshift(key);
  anim.arrival(pool.cells[slot]);
  return slot;
}

function setStat(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

function showRun(pool, run) {
  setStat("out-hits", String(run.hits));
  setStat("out-misses", String(run.misses));
  setStat("out-total-ms", run.ms.toFixed(2) + " ms");
  pool.tally.textContent = "This run: " + run.misses + " disk read" + (run.misses === 1 ? "" : "s") +
    ", " + run.hits + " hit" + (run.hits === 1 ? "" : "s") + ", " + run.ms.toFixed(2) + " ms of I/O";
}

function runAct1(tl, pool, phase) {
  // Cold start: every page is a miss; disk read.
  tl.mark("Act 1: cold run (empty pool)");
  var run = {hits: 0, misses: 0, ms: 0};
  for (var i = 0; i < QUERY_PAGES.length; i++) {
    (function(i) {
      var key = pageKey(QUERY_PAGES[i]);
      // Step 1 is the first page; step 2 is the other nine.
      if (i <= 1) tl.beat(BEAT_KINDS.coldRead);
      tl.call(function() {
        if (i === 0) {
          setStat("out-phase", "Act 1: cold start (empty pool)");
          showRun(pool, run);
        }
        phase.textContent = "Act 1, cold run: " + key + " (" + (i + 1) + "/" + QUERY_PAGES.length +
          ") is not in the buffer pool, so InnoDB reads it from disk (" + DISK_MS.toFixed(0) + " ms)";
        pool.note.textContent = key + " not in the pool: read from disk, " + DISK_MS.toFixed(0) + " ms";
      });
      tl.add({
        from: 0, to: 1, duration: 180, ease: anim.easeOutCubic,
        onUpdate: function() {},
        onComplete: function() {
          placePage(pool, key, COLD_COLOR);
          run.misses += 1;
          run.ms += DISK_MS;
          showRun(pool, run);
        }
      });
      tl.delay(60);
    })(i);
  }
  tl.call(function() {
    phase.textContent = "Act 1 complete: " + run.misses + " pages, " + run.misses + " disk reads, " +
      run.ms.toFixed(2) + " ms of I/O. Now the same query runs again…";
    pool.note.textContent = "Cold run done: every page came from disk";
  });
  tl.delay(700);
}

function runAct2(tl, pool, phase) {
  tl.mark("Act 2: same query, warm pool");
  var run = {hits: 0, misses: 0, ms: 0};
  for (var i = 0; i < QUERY_PAGES.length; i++) {
    (function(i) {
      var key = pageKey(QUERY_PAGES[i]);
      if (i <= 1) tl.beat(BEAT_KINDS.warmHit);
      tl.call(function() {
        if (i === 0) {
          setStat("out-phase", "Act 2: warm (same query repeats)");
          showRun(pool, run);
        }
        phase.textContent = "Act 2, warm run: " + key + " (" + (i + 1) + "/" + QUERY_PAGES.length +
          ") is already in the buffer pool: a hit, read from memory (" + RAM_MS.toFixed(2) + " ms)";
        pool.note.textContent = key + " found in the pool: no disk read, " + RAM_MS.toFixed(2) + " ms";
      });
      tl.add({
        from: 0, to: 1, duration: 120, ease: anim.easeOutCubic,
        onUpdate: function() {},
        onComplete: function() {
          var slot = findSlot(pool, key);
          if (slot >= 0) {
            // Hit — flash green over the existing cell.
            pool.cells[slot].setAttribute("fill", WARM_COLOR);
            anim.arrival(pool.cells[slot], {peakWidth: 2.0, durationMs: 220});
            run.hits += 1;
            run.ms += RAM_MS;
          } else {
            placePage(pool, key, COLD_COLOR);
            run.misses += 1;
            run.ms += DISK_MS;
          }
          showRun(pool, run);
        }
      });
      tl.delay(40);
    })(i);
  }
  tl.call(function() {
    var cold = QUERY_PAGES.length * DISK_MS;
    phase.textContent = "Act 2 complete: " + run.hits + " hits, " + run.misses + " disk reads: " +
      run.ms.toFixed(2) + " ms of I/O instead of " + cold.toFixed(2) + " ms (" +
      Math.round(cold / Math.max(run.ms, 0.001)) + "× less)";
    pool.note.textContent = "Warm run done: every page came from memory";
  });
  tl.delay(800);
}

// "P:3002 and P:3001", or the first three and a count for longer lists.
function listText(keys) {
  if (keys.length <= 2) return keys.join(" and ");
  if (keys.length <= 3) return keys.slice(0, 2).join(", ") + " and " + keys[2];
  return keys.slice(0, 3).join(", ") + " and " + (keys.length - 3) + " more";
}

function fileLine(key) {
  var p = null;
  for (var i = 0; i < QUERY_PAGES.length; i++) if (pageKey(QUERY_PAGES[i]) === key) p = QUERY_PAGES[i];
  return p ? SPACE_IDS[p.t] + "," + p.p : key;
}

function runAct3(tl, pool, phase, c) {
  // Dump on shutdown → restart → load. The pool holds the query's pages,
  // most recently read first; the dump lists the first n of them.
  tl.mark("Act 3: shutdown, restart, load");
  var pct = c.dump_pct;
  var mariadb = c.server === "mariadb";
  var cached = QUERY_PAGES.map(pageKey).reverse();
  var n = dumpCount(c.server, pct, cached.length);
  var dumped = cached.slice(0, n);
  var otherN = dumpCount(mariadb ? "mysql" : "mariadb", pct, cached.length);
  var dumpedCells = [];

  tl.beat(BEAT_KINDS.dump);
  tl.call(function() {
    setStat("out-phase", "Act 3: shutdown writes ib_buffer_pool");
    dumpedCells = [];
    for (var d = 0; d < dumped.length; d++) {
      var slot = findSlot(pool, dumped[d]);
      if (slot >= 0) dumpedCells.push(pool.cells[slot]);
    }
    var shown = dumped.slice(0, 3).map(fileLine).join("   ") + (dumped.length > 3 ? "   \u2026" : "");
    pool.fileBody.textContent = shown + "   (" + dumped.length + " line" + (dumped.length === 1 ? "" : "s") +
      ": space id, page number)";
    phase.textContent = "Act 3, shutdown: InnoDB writes the IDs of the " + n + " most recently used page" +
      (n === 1 ? "" : "s") + " to ib_buffer_pool (" + (mariadb
        ? "MariaDB: at most " + pct + "% of the pool's " + POOL_SIZE + " pages"
        : "MySQL: " + pct + "% of the " + cached.length + " cached pages") + "): " + listText(dumped) +
      ". " + (mariadb ? "MySQL" : "MariaDB") + " would list " + otherN;
    pool.note.textContent = "Dumped: " + listText(dumped) + " (page IDs only, not the pages)";
    pool.tally.textContent = "innodb_buffer_pool_dump_pct = " + pct + " (" + (mariadb ? "MariaDB" : "MySQL") + " rule)";
  });
  tl.add({
    from: 0, to: 1, duration: 320, ease: anim.easeOutCubic,
    onUpdate: function(t) {
      for (var d = 0; d < dumpedCells.length; d++) {
        dumpedCells[d].setAttribute("fill", anim.lerpColor(WARM_COLOR, DUMP_COLOR, t));
      }
      for (var f = 0; f < pool.file.length; f++) pool.file[f].setAttribute("opacity", t.toFixed(3));
    },
    onComplete: function() {
      for (var d = 0; d < dumpedCells.length; d++) anim.arrival(dumpedCells[d], {peakWidth: 2.0, durationMs: 200});
    }
  });
  tl.delay(600);

  tl.beat(BEAT_KINDS.restart);
  tl.call(function() {
    setStat("out-phase", "Act 3: restart, the pool starts empty");
    setStat("out-hits", "—");
    setStat("out-misses", "—");
    setStat("out-total-ms", "—");
    phase.textContent = "Act 3, restart: memory is wiped, so the buffer pool starts empty; " +
      "only ib_buffer_pool on disk remembers which pages were hot";
    pool.note.textContent = "Restart: the buffer pool is empty again";
    pool.tally.textContent = "";
  });
  tl.add({
    from: 0, to: 1, duration: 260, ease: anim.easeInCubic,
    onUpdate: function(t) {
      for (var i = 0; i < pool.cells.length; i++) {
        if (pool.labels[i].textContent) pool.labels[i].setAttribute("opacity", (1 - t).toFixed(3));
      }
    },
    onComplete: function() {
      for (var i = 0; i < pool.cells.length; i++) {
        pool.cells[i].setAttribute("fill", EMPTY_COLOR);
        pool.labels[i].textContent = "";
        pool.labels[i].setAttribute("opacity", 1);
      }
      pool.nextSlot = 0;
      pool.lru = [];
    }
  });
  tl.delay(500);

  tl.beat(BEAT_KINDS.load);
  tl.call(function() {
    setStat("out-phase", "Act 3: startup loads ib_buffer_pool in the background");
    phase.textContent = "Act 3, startup: innodb_buffer_pool_load_at_startup reads " + listText(dumped) +
      " back into the pool in the background while queries already run";
    pool.note.textContent = "Loading " + listText(dumped) + " from disk in the background";
  });
  for (var k = 0; k < dumped.length; k++) {
    (function(key) {
      tl.add({
        from: 0, to: 1, duration: 220, ease: anim.easeOutCubic,
        onUpdate: function() {},
        onComplete: function() { placePage(pool, key, WARM_COLOR); }
      });
      tl.delay(120);
    })(dumped[k]);
  }
  tl.call(function() {
    var nextMs = dumped.length * RAM_MS + (QUERY_PAGES.length - dumped.length) * DISK_MS;
    var coldMs = QUERY_PAGES.length * DISK_MS;
    phase.textContent = "✓ Warm restart: the next run finds " + dumped.length + " of its " +
      QUERY_PAGES.length + " pages in memory and reads " + (QUERY_PAGES.length - dumped.length) +
      " from disk: " + nextMs.toFixed(2) + " ms of I/O instead of " + coldMs.toFixed(2) + " ms cold";
    pool.note.textContent = listText(dumped) + (dumped.length === 1 ? " is" : " are") +
      " back in memory before the query needs " + (dumped.length === 1 ? "it" : "them");
    pool.tally.textContent = "Next run: " + dumped.length + " hits, " + (QUERY_PAGES.length - dumped.length) +
      " disk reads, " + nextMs.toFixed(2) + " ms of I/O";
    setStat("out-phase", "Act 3: warm restart, " + dumped.length + " of " + QUERY_PAGES.length + " pages preloaded");
  });
  tl.delay(300);
}

function readDumpControls() {
  var c = teachRuntime.readControls();
  return {dump_pct: Math.max(1, Math.min(100, Math.round(c.dump_pct || 25))),
          server: c.server === "mariadb" ? "mariadb" : "mysql"};
}

function buildCurrentTimeline() {
  var c = readDumpControls();
  var pool = buildPool("svg-pool");
  var phase = document.getElementById("phase-label");
  var tl = anim.timeline();
  runAct1(tl, pool, phase);
  runAct2(tl, pool, phase);
  runAct3(tl, pool, phase, c);
  return tl;
}

function resetAnim() {
  buildPool("svg-pool");
  setStat("out-phase", "Ready");
  setStat("out-hits", "—");
  setStat("out-misses", "—");
  setStat("out-total-ms", "—");
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

// Draw the empty pool now and again when an input changes, then wire the
// runtime so Play/Pause/Scrub and the guided steps work like every other
// lesson.
teachRuntime.wire(function() { resetAnim(); });
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
