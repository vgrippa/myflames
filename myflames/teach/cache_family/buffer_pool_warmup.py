"""Lesson: Why cold queries are slow — the InnoDB buffer pool warmup story.

Three acts:

Act 1 — **Cold start**. The buffer pool is empty. Every page the query
        asks for has to be read from disk. Bars are long (disk latency).

Act 2 — **Hit ratio climbs**. The same query runs again. Pages are now
        in the pool, so reads are in-memory and ~50–100× faster.

Act 3 — **Warm restart** (dump/load). On shutdown, InnoDB writes the
        hottest N% of the pool to ``ib_buffer_pool``. On startup it
        loads that file back asynchronously — so a restart doesn't
        leave you cold.

MySQL internals facts referenced here are verified against the server
source tree (storage/innobase/buf/buf0dump.cc and
storage/innobase/handler/ha_innodb.cc around line 22688):

  * ``innodb_buffer_pool_dump_pct`` default is **25** — only the top
    25% of hot pages get serialized. This keeps dump time bounded.
  * Dump file name is **``ib_buffer_pool``** (constant
    ``SRV_BUF_DUMP_FILENAME_DEFAULT`` in storage/innobase/include/srv0srv.h).
  * ``SET GLOBAL innodb_buffer_pool_load_now=ON`` wakes the background
    dump/load thread and returns immediately — the load runs async.
  * ``innodb_buffer_pool_load_at_startup`` (default ON) kicks off the
    same load once InnoDB is up.
"""
from .. import _html


_LESSON_JS_TEMPLATE = r"""
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
"""


def render():
    query_card_html = _html.query_card(
        "SELECT u.id, u.email, o.total "
        "FROM users u JOIN orders o ON o.user_id = u.id "
        "WHERE o.created_at > NOW() - INTERVAL 7 DAY",
        note=(
            "Same query, two runs back-to-back. Act 1 reads from disk; "
            "Act 2 reads from RAM. The third act shows what happens "
            "when the server restarts — warm, not cold."
        ),
    )
    explainer_html = _html.explainer(
        "What you'll see",
        [
            "Cells = buffer-pool slots. Red-ish = just read from disk (slow). "
            "Green = pool hit (fast). Yellow = dumped to <code>ib_buffer_pool</code> "
            "on shutdown.",
            "Latency counter on the right updates live as each page is touched.",
            "The hit/miss ratio is what decides whether a repeat run is "
            "milliseconds (warm) or seconds (cold).",
        ],
    )
    controls_html = f"""
<section class="controls" aria-labelledby="controls-heading">
  <h2 id="controls-heading" class="visually-hidden">Controls</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play to warm the buffer pool")}
</section>
"""
    stage_html = f"""
<section class="stage">
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0">
      <svg id="svg-pool" viewBox="0 0 500 260" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Live cache stats</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Phase</p><p class="value" id="out-phase">—</p></div>
    <div class="item"><p class="label">Pool hits {ht("Pages found in the buffer pool — no disk I/O.")}</p><p class="value ok" id="out-hits">—</p></div>
    <div class="item"><p class="label">Pool misses {ht("Pages not in the pool — read from storage and inserted.")}</p><p class="value hot" id="out-misses">—</p></div>
    <div class="item"><p class="label">Cumulative I/O time {ht("Sum of per-page latencies at ~6 ms for a cold read, ~0.06 ms for a warm one. Real SSD ratios vary but the orders of magnitude are correct.")}</p><p class="value" id="out-total-ms">—</p></div>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — the dump &amp; load mechanism</summary>
  <div class="body">
    <p>A cold-start disaster is the first thing a DBA learns to avoid:
    restart the server, and the first hour of queries all go to disk
    instead of RAM because the buffer pool is empty. InnoDB ships with
    two defaults that make this a solved problem — but only if you
    know they exist.</p>

    <h3>Dump on shutdown</h3>
    <ul>
      <li><code>innodb_buffer_pool_dump_at_shutdown</code> (ON by
      default) writes the identities of the hottest
      <code>innodb_buffer_pool_dump_pct</code> = <strong>25</strong>
      percent of pages to a small file named
      <code>ib_buffer_pool</code> in the data directory.</li>
      <li>Only <em>page IDs</em> are dumped, not page data — the file
      is tiny (a few MB for a 10 GB pool).</li>
    </ul>

    <h3>Load on startup</h3>
    <ul>
      <li><code>innodb_buffer_pool_load_at_startup</code> (ON) reads
      <code>ib_buffer_pool</code> and issues reads for those pages.
      The load runs in a <em>background thread</em>; queries don't
      block on it.</li>
      <li><code>SET GLOBAL innodb_buffer_pool_load_now = ON</code>
      triggers the same load immediately at runtime. The statement
      returns fast — the actual reads run async (verified in
      <code>storage/innobase/buf/buf0dump.cc</code>'s
      <code>buf_load_start()</code>).</li>
    </ul>

    <h3>Common sizing mistake</h3>
    <p>If <code>innodb_buffer_pool_size</code> is small relative to
    your working set, no amount of warmup helps — pages you loaded at
    startup get evicted before real queries reach them. Size the pool
    to the working set first; <em>then</em> worry about warmup.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="buffer_pool_warmup",
        title="InnoDB buffer pool — the cold-start problem &amp; dump/load cure",
        subtitle=(
            "Same query, run twice: why the first run hits disk and "
            "the second runs from RAM — plus the restart story."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS_TEMPLATE,
    )
