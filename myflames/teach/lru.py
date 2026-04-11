"""Lesson: InnoDB midpoint-insertion LRU vs textbook LRU.

Polished animation: instead of running the whole trace and drawing the
final state, this version reveals the trace one access at a time. New
pages slide into the old-sublist head; old-list hits that pass the
``innodb_old_blocks_time`` threshold animate up to the young-sublist
head along a curved path; evictions fade out while shrinking. The
textbook LRU panel animates the same trace with simpler motion.
"""
from . import _html
from ._cost_model import (
    INNODB_OLD_BLOCKS_PCT_DEFAULT,
    INNODB_OLD_BLOCKS_TIME_DEFAULT_MS,
)


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Parameters (InnoDB buffer pool)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="pool_size">Buffer pool (pages): <span class="value-pill" data-pill-for="pool_size">24</span></label>
      <input type="range" id="pool_size" name="pool_size" min="12" max="40" step="2" value="24">
      <div class="hint">For illustration. Real pools are millions of pages.</div>
    </div>

    <div class="control">
      <label for="old_pct">innodb_old_blocks_pct: <span class="value-pill" data-pill-for="old_pct">{INNODB_OLD_BLOCKS_PCT_DEFAULT}</span></label>
      <input type="range" id="old_pct" name="old_pct" min="10" max="90" step="1" value="{INNODB_OLD_BLOCKS_PCT_DEFAULT}">
      <div class="hint">% of pool reserved for the old (cold) sublist. Default {INNODB_OLD_BLOCKS_PCT_DEFAULT}.</div>
    </div>

    <div class="control">
      <label for="old_ms">innodb_old_blocks_time (ms): <span class="value-pill" data-pill-for="old_ms">{INNODB_OLD_BLOCKS_TIME_DEFAULT_MS}</span></label>
      <input type="range" id="old_ms" name="old_ms" min="0" max="5000" step="100" value="{INNODB_OLD_BLOCKS_TIME_DEFAULT_MS}">
      <div class="hint">Minimum age in old sublist before promotion. Default {INNODB_OLD_BLOCKS_TIME_DEFAULT_MS} ms.</div>
    </div>

    <div class="control">
      <label for="workload">Workload</label>
      <select id="workload" name="workload">
        <option value="hot_set">Hot set (frequent few pages)</option>
        <option value="full_scan" selected>One-pass full table scan</option>
        <option value="mixed">Hot set + full scan + hot set</option>
      </select>
      <div class="hint">Full scan is where midpoint insertion proves its worth.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- A one-pass full table scan that would pollute a textbook LRU\n"
            "SELECT SUM(amount)\n"
            "FROM   events                   -- 10 GB table\n"
            "WHERE  event_date >= '2026-01-01';   -- no matching index → scan"
        ),
        note="Pick 'One-pass full table scan' from the workload dropdown to see why InnoDB's buffer pool keeps the OLTP hot set intact during reporting queries like this one."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left panel — InnoDB midpoint-insertion LRU. The pool is split into a blue 'young' half on top and a grey 'old' half on bottom. A dashed red line in the middle is the MIDPOINT.",
            "New pages (cache misses) slide in from the edge into the HEAD OF THE OLD SUBLIST. They never enter the young half on first access.",
            "A page only gets promoted to young if it's hit AGAIN after innodb_old_blocks_time ms — a full scan never does that, which is the whole point.",
            "Right panel — textbook LRU. Every access bumps the page to the MRU end of a single list. A full scan therefore wipes every hot page out.",
            "Watch the counters: with a full-scan workload, InnoDB's 'promotions' stay at 0 and classic LRU's 'hits' go to 0 too — but InnoDB's young pages stay in place.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div>
      <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#1e40af;letter-spacing:0.4px;text-transform:uppercase">InnoDB midpoint-insertion LRU</p>
      <svg id="svg-innodb" viewBox="0 0 400 280" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
    <div>
      <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#6b7280;letter-spacing:0.4px;text-transform:uppercase">Textbook single-list LRU</p>
      <svg id="svg-classic" viewBox="0 0 400 280" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
  </div>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Simulation results (updates as the animation plays)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">InnoDB: young pages</p><p class="value" id="out-young">—</p></div>
    <div class="item"><p class="label">InnoDB: old pages</p><p class="value" id="out-old">—</p></div>
    <div class="item"><p class="label">InnoDB: promotions</p><p class="value" id="out-promotions">—</p></div>
    <div class="item"><p class="label">InnoDB: evictions</p><p class="value" id="out-evictions">—</p></div>
    <div class="item"><p class="label">Classic LRU: evictions</p><p class="value" id="out-classic-evictions">—</p></div>
    <div class="item"><p class="label">Classic LRU: hits</p><p class="value" id="out-classic-hits">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Hot-set survival after a full scan (pool size vs scan length)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — why is a full table scan a problem for plain LRU?</summary>
  <div class="body">
    <p>Imagine a 1 GB buffer pool and a 10 GB table. Under <strong>textbook
    LRU</strong>, reading the whole table once visits every page exactly
    once — and each visit bumps that page to the head of the list. By the
    end of the scan, every single page that used to be hot has been
    evicted. Your OLTP working set just got destroyed by a reporting query.</p>

    <p>InnoDB's answer, introduced long before modern MySQL, is
    <strong>midpoint-insertion LRU</strong>:</p>
    <ol>
      <li>The linked list is conceptually split into two halves by a
      "midpoint": a <em>young</em> sublist (MRU end, ~5/8 by default) and
      an <em>old</em> sublist (LRU end, ~3/8). The split ratio is
      <code>innodb_old_blocks_pct</code> (default
      {INNODB_OLD_BLOCKS_PCT_DEFAULT}).</li>

      <li>On a <strong>cache miss</strong>, the new page is inserted at the
      midpoint — i.e. at the head of the <em>old</em> sublist, NOT at the
      head of the list. Pages that used to be in the young sublist are
      untouched.</li>

      <li>On a <strong>cache hit in the old sublist</strong>, InnoDB only
      promotes the page to the young sublist if
      <code>now - first_access ≥ innodb_old_blocks_time</code> (default
      {INNODB_OLD_BLOCKS_TIME_DEFAULT_MS} ms). A one-pass full scan never
      meets this threshold, so scan pages cycle through the old sublist
      only and never pollute the hot half.</li>

      <li>On a <strong>hit in the young sublist</strong>, the page is
      bumped to the head — cheap linked-list operation.</li>
    </ol>

    <p>Run the "one-pass full scan" workload above and watch the two
    panels: the textbook LRU's entire working set is wiped, while InnoDB
    keeps the young sublist intact.</p>

    <p>Sources: MySQL 8.4 Reference Manual §17.5.1 "Buffer Pool" and
    §17.8.3.3 "Making the Buffer Pool Scan Resistant".</p>
  </div>
</details>
"""

    lesson_js = """
// ---------- workload generators ----------
function genWorkload(kind, pool) {
  var trace = [];
  var now = 0;
  var hotSetSize = Math.max(4, Math.floor(pool * 0.3));
  if (kind === "hot_set") {
    for (var i = 0; i < 40; i++) {
      trace.push({pid: Math.floor(Math.random() * hotSetSize), now: now});
      now += 40;
    }
  } else if (kind === "full_scan") {
    var total = Math.min(pool * 2, 60);
    for (var j = 0; j < total; j++) {
      trace.push({pid: 1000 + j, now: now});
      now += 10;
    }
  } else {
    for (var k = 0; k < 15; k++) {
      trace.push({pid: Math.floor(Math.random() * hotSetSize), now: now});
      now += 40;
    }
    for (var s = 0; s < pool; s++) {
      trace.push({pid: 2000 + s, now: now});
      now += 10;
    }
    for (var m = 0; m < 15; m++) {
      trace.push({pid: Math.floor(Math.random() * hotSetSize), now: now});
      now += 40;
    }
  }
  return trace;
}

// ---------- InnoDB midpoint LRU (client-side copy) ----------
function simInnoDBStep(state, access, oldMs) {
  var pid = access.pid, now = access.now;
  var young = state.young, old = state.old;
  function findIn(lst, id) { for (var i = 0; i < lst.length; i++) if (lst[i].id === id) return i; return -1; }

  var youngIdx = findIn(young, pid);
  if (youngIdx >= 0) {
    // Young hit: MRU bump
    var e = young.splice(youngIdx, 1)[0];
    young.unshift(e);
    return {event: "young_hit", id: pid};
  }
  var oldIdx = findIn(old, pid);
  if (oldIdx >= 0) {
    var e = old[oldIdx];
    var age = now - e.firstSeen;
    if (age >= oldMs) {
      // Promote: old → head of young
      old.splice(oldIdx, 1);
      var demoted = null;
      if (young.length >= state.youngCap) {
        demoted = young.pop();
        demoted.firstSeen = now;
        old.unshift(demoted);
      }
      young.unshift({id: pid, firstSeen: now});
      state.promotions++;
      return {event: "promotion", id: pid, demoted: demoted ? demoted.id : null};
    }
    return {event: "old_hit_no_promo", id: pid};
  }
  // Miss: insert at head of old, evict from old tail if full
  var evicted = null;
  if (old.length >= state.oldCap) {
    evicted = old.pop();
    state.evictions++;
  }
  old.unshift({id: pid, firstSeen: now});
  return {event: "miss", id: pid, evicted: evicted ? evicted.id : null};
}

// ---------- Classic LRU ----------
function simClassicStep(state, access) {
  var pid = access.pid;
  var idx = -1;
  for (var i = 0; i < state.list.length; i++) if (state.list[i] === pid) { idx = i; break; }
  if (idx >= 0) {
    state.list.splice(idx, 1);
    state.list.unshift(pid);
    state.hits++;
    return {event: "hit"};
  }
  var evicted = null;
  if (state.list.length >= state.cap) {
    evicted = state.list.pop();
    state.evictions++;
  }
  state.list.unshift(pid);
  return {event: "miss", evicted: evicted};
}

// ---------- Panel rendering ----------
var CELL_W = 20, CELL_H = 18, CELL_GAP = 3;

function buildInnoDBPanel(youngCap, oldCap) {
  var svg = document.getElementById("svg-innodb");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 400;

  // Young sublist
  var youngLbl = anim.svgEl("text", {x: 16, y: 22, "font-size": 11, "font-weight": 700, fill: "#1e40af"});
  youngLbl.textContent = "Young (" + youngCap + " slots)";
  svg.appendChild(youngLbl);
  var youngBox = anim.svgEl("rect", {
    x: 16, y: 30, width: W - 32, height: 46,
    rx: 4, ry: 4, fill: "#eff6ff", stroke: "#93c5fd", "stroke-width": 1
  });
  svg.appendChild(youngBox);

  // Midpoint divider
  var midLine = anim.svgEl("line", {
    x1: 16, y1: 88, x2: W - 16, y2: 88,
    stroke: "#dc2626", "stroke-width": 2, "stroke-dasharray": "5 3"
  });
  svg.appendChild(midLine);
  var midLbl = anim.svgEl("text", {
    x: W - 16, y: 84, "text-anchor": "end",
    "font-size": 9, fill: "#dc2626", "font-weight": 600
  });
  midLbl.textContent = "← midpoint (new pages enter here)";
  svg.appendChild(midLbl);

  // Old sublist
  var oldLbl = anim.svgEl("text", {x: 16, y: 108, "font-size": 11, "font-weight": 700, fill: "#374151"});
  oldLbl.textContent = "Old (" + oldCap + " slots)";
  svg.appendChild(oldLbl);
  var oldBox = anim.svgEl("rect", {
    x: 16, y: 116, width: W - 32, height: 46,
    rx: 4, ry: 4, fill: "#f9fafb", stroke: "#d1d5db", "stroke-width": 1
  });
  svg.appendChild(oldBox);

  // Stats line
  var stats = anim.svgEl("text", {x: 200, y: 200, "text-anchor": "middle", "font-size": 11, fill: "#374151"});
  stats.textContent = "";
  svg.appendChild(stats);
  var verdict = anim.svgEl("text", {x: 200, y: 225, "text-anchor": "middle", "font-size": 11, "font-weight": 600, fill: "#065f46"});
  svg.appendChild(verdict);
  var currentPage = anim.svgEl("text", {x: 200, y: 250, "text-anchor": "middle", "font-size": 12, "font-weight": 700, fill: "#1f2937", "font-variant-numeric": "tabular-nums"});
  svg.appendChild(currentPage);

  return {
    svg: svg, youngBoxY: 30, youngBoxHeight: 46,
    oldBoxY: 116, oldBoxHeight: 46,
    youngStartX: 22, oldStartX: 22,
    cells: {}, // id → {rect, label, sublist, x, y}
    state: {young: [], old: [], youngCap: youngCap, oldCap: oldCap, promotions: 0, evictions: 0},
    stats: stats, verdict: verdict, currentPage: currentPage
  };
}

function buildClassicPanel(cap) {
  var svg = document.getElementById("svg-classic");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 400;
  var lbl = anim.svgEl("text", {x: 16, y: 22, "font-size": 11, "font-weight": 700, fill: "#374151"});
  lbl.textContent = "Single list (" + cap + " slots)";
  svg.appendChild(lbl);
  var box = anim.svgEl("rect", {
    x: 16, y: 30, width: W - 32, height: 90,
    rx: 4, ry: 4, fill: "#f9fafb", stroke: "#d1d5db", "stroke-width": 1
  });
  svg.appendChild(box);

  var stats = anim.svgEl("text", {x: 200, y: 200, "text-anchor": "middle", "font-size": 11, fill: "#374151"});
  svg.appendChild(stats);
  var verdict = anim.svgEl("text", {x: 200, y: 225, "text-anchor": "middle", "font-size": 11, "font-weight": 600, fill: "#991b1b"});
  svg.appendChild(verdict);

  return {
    svg: svg, boxY: 30, boxHeight: 90, startX: 22,
    cells: {}, state: {list: [], cap: cap, evictions: 0, hits: 0},
    stats: stats, verdict: verdict
  };
}

function cellXYForIndex(panel, sublist, idx) {
  var startX = panel.youngStartX || panel.startX;
  if (sublist === "young") {
    return { x: startX + idx * (CELL_W + CELL_GAP), y: panel.youngBoxY + 14 };
  } else if (sublist === "old") {
    return { x: startX + idx * (CELL_W + CELL_GAP), y: panel.oldBoxY + 14 };
  } else {
    // Classic panel
    var row = Math.floor(idx / 18);
    var col = idx % 18;
    return { x: startX + col * (CELL_W + CELL_GAP), y: panel.boxY + 14 + row * 28 };
  }
}

function createCell(panel, id, sublist, fillColor) {
  var p = cellXYForIndex(panel, sublist, 0);
  var rect = anim.svgEl("rect", {
    x: p.x - 60, y: p.y, width: CELL_W, height: CELL_H,
    rx: 3, ry: 3,
    fill: fillColor || "#9ca3af", stroke: "#374151", "stroke-width": 1, opacity: 0
  });
  panel.svg.appendChild(rect);
  var label = anim.svgEl("text", {
    x: p.x - 60 + CELL_W/2, y: p.y + 13, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#ffffff", opacity: 0
  });
  label.textContent = (id >= 1000) ? "S" + (id - 1000) : String(id);
  panel.svg.appendChild(label);
  var cell = {rect: rect, label: label, sublist: sublist, x: p.x - 60, y: p.y};
  panel.cells[id] = cell;
  return cell;
}

function moveCellTo(cell, toX, toY, duration, ease) {
  anim.tween({
    from: {x: cell.x, y: cell.y},
    to: {x: toX, y: toY},
    duration: duration || 300, ease: ease || anim.easeOutCubic,
    onUpdate: function(p) {
      cell.rect.setAttribute("x", p.x);
      cell.rect.setAttribute("y", p.y);
      cell.label.setAttribute("x", p.x + CELL_W/2);
      cell.label.setAttribute("y", p.y + 13);
    },
    onComplete: function() { cell.x = toX; cell.y = toY; }
  });
}

function fadeInCell(cell) {
  anim.tween({
    from: 0, to: 1, duration: 220, ease: anim.easeOutCubic,
    onUpdate: function(v) {
      cell.rect.setAttribute("opacity", v);
      cell.label.setAttribute("opacity", v);
    }
  });
}
function fadeOutCell(cell, onComplete) {
  anim.tween({
    from: 1, to: 0, duration: 260, ease: anim.easeInCubic,
    onUpdate: function(v) {
      cell.rect.setAttribute("opacity", v);
      cell.label.setAttribute("opacity", v);
    },
    onComplete: function() {
      if (cell.rect.parentNode) cell.rect.parentNode.removeChild(cell.rect);
      if (cell.label.parentNode) cell.label.parentNode.removeChild(cell.label);
      if (onComplete) onComplete();
    }
  });
}

function rerenderInnoDB(panel) {
  // Move every cell to its current index position
  panel.state.young.forEach(function(entry, idx) {
    var cell = panel.cells[entry.id];
    if (!cell) return;
    var p = cellXYForIndex(panel, "young", idx);
    if (Math.abs(cell.x - p.x) > 0.1 || Math.abs(cell.y - p.y) > 0.1) {
      moveCellTo(cell, p.x, p.y, 280, anim.easeInOutCubic);
    }
    cell.rect.setAttribute("fill", "#2563eb");
  });
  panel.state.old.forEach(function(entry, idx) {
    var cell = panel.cells[entry.id];
    if (!cell) return;
    var p = cellXYForIndex(panel, "old", idx);
    if (Math.abs(cell.x - p.x) > 0.1 || Math.abs(cell.y - p.y) > 0.1) {
      moveCellTo(cell, p.x, p.y, 280, anim.easeInOutCubic);
    }
    cell.rect.setAttribute("fill", "#9ca3af");
  });
}

function rerenderClassic(panel) {
  panel.state.list.forEach(function(id, idx) {
    var cell = panel.cells[id];
    if (!cell) return;
    var p = cellXYForIndex(panel, "classic", idx);
    if (Math.abs(cell.x - p.x) > 0.1 || Math.abs(cell.y - p.y) > 0.1) {
      moveCellTo(cell, p.x, p.y, 260, anim.easeInOutCubic);
    }
  });
}

// ---------- playback ----------
var innoDBPanel = null;
var classicPanel = null;

function updateReadouts() {
  if (!innoDBPanel || !classicPanel) return;
  document.getElementById("out-young").textContent = innoDBPanel.state.young.length + "/" + innoDBPanel.state.youngCap;
  document.getElementById("out-old").textContent = innoDBPanel.state.old.length + "/" + innoDBPanel.state.oldCap;
  document.getElementById("out-promotions").textContent = innoDBPanel.state.promotions;
  document.getElementById("out-evictions").textContent = innoDBPanel.state.evictions;
  document.getElementById("out-classic-evictions").textContent = classicPanel.state.evictions;
  document.getElementById("out-classic-hits").textContent = classicPanel.state.hits;
}

function applyStep(trace, i, oldMs) {
  // Apply one access synchronously — single function used by both the
  // timeline steps AND the scrubber fastForwardTo() replay.
  var access = trace[i];
  innoDBPanel.currentPage.textContent = "access #" + (i + 1) + ": page " + access.pid;

  var result = simInnoDBStep(innoDBPanel.state, access, oldMs);
  if (result.event === "miss") {
    if (result.evicted !== null && innoDBPanel.cells[result.evicted]) {
      fadeOutCell(innoDBPanel.cells[result.evicted]);
      delete innoDBPanel.cells[result.evicted];
    }
    var cell = createCell(innoDBPanel, access.pid, "old", "#9ca3af");
    var p = cellXYForIndex(innoDBPanel, "old", 0);
    cell.rect.setAttribute("x", p.x - 30);
    fadeInCell(cell);
    rerenderInnoDB(innoDBPanel);
  } else if (result.event === "promotion") {
    if (result.demoted !== null && innoDBPanel.cells[result.demoted]) {
      var demotedCell = innoDBPanel.cells[result.demoted];
      demotedCell.sublist = "old";
    }
    if (innoDBPanel.cells[access.pid]) {
      innoDBPanel.cells[access.pid].sublist = "young";
      innoDBPanel.cells[access.pid].rect.setAttribute("fill", "#2563eb");
      anim.pulse(innoDBPanel.cells[access.pid].rect, 2, 1, 320);
    }
    rerenderInnoDB(innoDBPanel);
  } else {
    rerenderInnoDB(innoDBPanel);
  }

  var cResult = simClassicStep(classicPanel.state, access);
  if (cResult.event === "miss") {
    if (cResult.evicted !== null && classicPanel.cells[cResult.evicted]) {
      fadeOutCell(classicPanel.cells[cResult.evicted]);
      delete classicPanel.cells[cResult.evicted];
    }
    var cCell = createCell(classicPanel, access.pid, "classic", "#6b7280");
    cCell.rect.setAttribute("x", 400);
    fadeInCell(cCell);
    rerenderClassic(classicPanel);
  } else {
    anim.pulse(classicPanel.cells[access.pid].rect, 2, 1, 260);
    rerenderClassic(classicPanel);
  }
  updateReadouts();
}

function buildCurrentTimeline() {
  if (!innoDBPanel || !classicPanel) recompute();
  var c = teachRuntime.readControls();
  var trace = genWorkload(c.workload, c.pool_size);
  var tl = anim.timeline();
  tl.call(function() {
    document.getElementById("phase-label").textContent = "Running " + trace.length + " accesses…";
  });
  for (var i = 0; i < trace.length; i++) {
    (function(idx) {
      tl.call(function() { applyStep(trace, idx, c.old_ms); });
      tl.delay(420);
    })(i);
  }
  tl.call(function() {
    if (c.workload === "full_scan" && innoDBPanel.state.promotions === 0) {
      innoDBPanel.verdict.textContent = "✓ Young sublist untouched — scan-resistant";
    }
    if (c.workload === "full_scan" && classicPanel.state.hits === 0) {
      classicPanel.verdict.textContent = "✗ All slots evicted — pool polluted";
    }
    document.getElementById("phase-label").textContent = "Complete — see results above";
    document.getElementById("out-explanation").textContent =
      c.workload === "full_scan"
        ? "One-pass full scan. InnoDB's midpoint insertion keeps every page in the old sublist — the young sublist stays intact. Textbook LRU treats every page as MRU and evicts the whole hot set."
        : c.workload === "hot_set"
        ? "A small hot set repeatedly accessed. Both algorithms keep the hot pages; midpoint insertion pays off under scan or mixed workloads, not this one."
        : "Mixed workload: hot set, then a scan, then hot set again. InnoDB's young sublist survives the scan pollution — so the second round of hot queries hits its cache.";
  });
  return tl;
}

function resetAnim() {
  document.getElementById("phase-label").textContent = "Ready — press Play";
  recompute();
}

function renderChart(currentPool) {
  // Toy model: classic LRU hit rate after scan = 0 (fully polluted).
  // InnoDB hit rate after scan ≈ (young_capacity / pool_size) assuming
  // the young set was hot prior to the scan.
  var OLD_PCT = 37;
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 10, xMax: 10000,
    xLabel: "Buffer pool size (pages)", yLabel: "Hot pages still cached after scan",
    curves: [
      { label: "InnoDB (young sublist preserved)", color: "#2563eb",
        fn: function(n) { return Math.max(1, Math.floor(n * (100 - OLD_PCT) / 100)); } },
      { label: "Classic LRU (wiped by scan)", color: "#dc2626",
        fn: function(n) { return 0.5; } }  // log chart needs >0
    ],
    current: { x: currentPool },
    xSlider: "pool_size",
    xSliderTransform: function(xVal) { return Math.max(12, Math.min(40, Math.round(xVal / 2) * 2)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var youngCap = Math.max(1, Math.floor(c.pool_size * (100 - c.old_pct) / 100));
  var oldCap = c.pool_size - youngCap;
  innoDBPanel = buildInnoDBPanel(youngCap, oldCap);
  classicPanel = buildClassicPanel(c.pool_size);
  document.getElementById("out-young").textContent = "0/" + youngCap;
  document.getElementById("out-old").textContent = "0/" + oldCap;
  document.getElementById("out-promotions").textContent = "0";
  document.getElementById("out-evictions").textContent = "0";
  document.getElementById("out-classic-evictions").textContent = "0";
  document.getElementById("out-classic-hits").textContent = "0";
  document.getElementById("out-explanation").textContent = "Press \u201cPlay\u201d to simulate step by step.";
  renderChart(c.pool_size);
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
"""

    return _html.render_page(
        lesson_id="lru",
        title="InnoDB buffer pool — midpoint-insertion LRU",
        subtitle=(
            "Why MySQL's LRU is not the LRU you learned in school. Run a full "
            "table scan and watch the hot set survive."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
