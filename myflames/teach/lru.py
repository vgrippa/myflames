"""Lesson: InnoDB midpoint-insertion LRU vs textbook LRU.

Teaches the single most common "you think you know LRU but MySQL's is
different" moment: InnoDB splits the list into young (MRU) and old (LRU)
halves, new pages enter at the **midpoint** (not the head), and a second
access only promotes to young if it happened more than
``innodb_old_blocks_time`` after the first access. That's how InnoDB
survives one-pass full-scan pollution.

This lesson lets the user run three canned workloads against both
algorithms and see the hit/eviction counts diverge.
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
      <label for="pool_size">Buffer pool (pages): <span class="value-pill" data-pill-for="pool_size">100</span></label>
      <input type="range" id="pool_size" name="pool_size" min="10" max="400" step="10" value="100">
      <div class="hint">For illustration. Real pools are millions of pages.</div>
    </div>

    <div class="control">
      <label for="old_pct">innodb_old_blocks_pct: <span class="value-pill" data-pill-for="old_pct">{INNODB_OLD_BLOCKS_PCT_DEFAULT}</span></label>
      <input type="range" id="old_pct" name="old_pct" min="5" max="95" step="1" value="{INNODB_OLD_BLOCKS_PCT_DEFAULT}">
      <div class="hint">% of the pool reserved for the old (cold) sublist. Default {INNODB_OLD_BLOCKS_PCT_DEFAULT}.</div>
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
        <option value="mixed">Hot set + full scan</option>
      </select>
      <div class="hint">Full scan is where midpoint insertion proves its worth.</div>
    </div>

  </div>
</section>
"""

    stage_html = """
<section class="stage">
  <div class="stage-toolbar">
    <button id="btn-run" class="primary">▶ Run workload</button>
    <button id="btn-reset">Reset</button>
    <span style="margin-left:auto;font-size:12px;color:#6b7280" id="phase-label">Ready</span>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div>
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#1e40af">InnoDB midpoint-insertion LRU</p>
      <svg id="svg-innodb" viewBox="0 0 400 260" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
    <div>
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#6b7280">Textbook single-list LRU (for contrast)</p>
      <svg id="svg-classic" viewBox="0 0 400 260" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
  </div>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Simulation results</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">InnoDB: young pages</p><p class="value" id="out-young">—</p></div>
    <div class="item"><p class="label">InnoDB: old pages</p><p class="value" id="out-old">—</p></div>
    <div class="item"><p class="label">InnoDB: promotions</p><p class="value" id="out-promotions">—</p></div>
    <div class="item"><p class="label">InnoDB: evictions</p><p class="value" id="out-evictions">—</p></div>
    <div class="item"><p class="label">Classic LRU: evictions</p><p class="value" id="out-classic-evictions">—</p></div>
    <div class="item"><p class="label">Classic LRU: hits</p><p class="value" id="out-classic-hits">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
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

    <p>Run the "one-pass full scan" workload above and compare the two
    panels: the textbook LRU's entire working set is wiped, while InnoDB
    keeps the young sublist intact.</p>

    <p>Sources: MySQL 8.4 Reference Manual §17.5.1 "Buffer Pool" and
    §17.8.3.3 "Making the Buffer Pool Scan Resistant".</p>
  </div>
</details>
"""

    lesson_js = f"""
var INNODB_OLD_BLOCKS_PCT_DEFAULT = {INNODB_OLD_BLOCKS_PCT_DEFAULT};
var INNODB_OLD_BLOCKS_TIME_DEFAULT_MS = {INNODB_OLD_BLOCKS_TIME_DEFAULT_MS};

// --------- workload generators ---------
function genWorkload(kind, pool) {{
  var trace = [];
  var now = 0;
  var hotSetSize = Math.max(5, Math.floor(pool * 0.3));
  if (kind === "hot_set") {{
    for (var i = 0; i < 400; i++) {{
      trace.push([Math.floor(Math.random() * hotSetSize), now]);
      now += 20;
    }}
  }} else if (kind === "full_scan") {{
    // Scan 3x the pool — clearly bigger than fits.
    var total = pool * 3;
    for (var j = 0; j < total; j++) {{
      trace.push([1000 + j, now]); // unique ids, disjoint from hot set
      now += 5;
    }}
  }} else {{
    // Mixed: hot set queries interleaved with a full scan
    for (var k = 0; k < 200; k++) {{
      trace.push([Math.floor(Math.random() * hotSetSize), now]);
      now += 20;
    }}
    for (var s = 0; s < pool * 3; s++) {{
      trace.push([2000 + s, now]);
      now += 5;
    }}
    for (var m = 0; m < 200; m++) {{
      trace.push([Math.floor(Math.random() * hotSetSize), now]);
      now += 20;
    }}
  }}
  return trace;
}}

// --------- InnoDB midpoint LRU simulator (mirrors _cost_model.simulate_midpoint_lru) ---------
function simInnoDB(pool, trace, oldPct, oldMs) {{
  var youngCap = Math.max(1, Math.floor(pool * (100 - oldPct) / 100));
  var oldCap = pool - youngCap;
  var young = []; // head = MRU
  var old = [];
  var evictions = 0, promotions = 0;

  function find(pid) {{
    for (var i = 0; i < young.length; i++) if (young[i].id === pid) return {{list: "young", idx: i, e: young[i]}};
    for (var j = 0; j < old.length; j++) if (old[j].id === pid) return {{list: "old", idx: j, e: old[j]}};
    return null;
  }}

  for (var k = 0; k < trace.length; k++) {{
    var pid = trace[k][0], now = trace[k][1];
    var f = find(pid);
    if (f === null) {{
      if (old.length >= oldCap) {{ old.pop(); evictions++; }}
      old.unshift({{id: pid, firstSeen: now}});
    }} else if (f.list === "old") {{
      var age = now - f.e.firstSeen;
      if (age >= oldMs) {{
        old.splice(f.idx, 1);
        if (young.length >= youngCap) {{
          var demoted = young.pop();
          demoted.firstSeen = now;
          old.unshift(demoted);
        }}
        young.unshift({{id: pid, firstSeen: now}});
        promotions++;
      }}
    }} else {{
      young.splice(f.idx, 1);
      young.unshift(f.e);
    }}
  }}
  return {{
    youngCap: youngCap, oldCap: oldCap,
    youngPages: young.length, oldPages: old.length,
    evictions: evictions, promotions: promotions,
    young: young, old: old
  }};
}}

function simClassic(pool, trace) {{
  var list = [];
  var evictions = 0, hits = 0;
  for (var i = 0; i < trace.length; i++) {{
    var pid = trace[i][0];
    var idx = list.indexOf(pid);
    if (idx >= 0) {{
      list.splice(idx, 1);
      list.unshift(pid);
      hits++;
    }} else {{
      if (list.length >= pool) {{ list.pop(); evictions++; }}
      list.unshift(pid);
    }}
  }}
  return {{evictions: evictions, hits: hits, population: list.length, list: list}};
}}

// --------- rendering ---------
function renderInnoDB(r) {{
  var svg = document.getElementById("svg-innodb");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 400, H = 260;
  // Young row (top)
  var youngCapShown = Math.min(r.youngCap, 18);
  var cellW = (W - 40) / 18;
  for (var i = 0; i < youngCapShown; i++) {{
    var filled = i < r.youngPages;
    var r1 = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r1.setAttribute("x", 20 + i * cellW); r1.setAttribute("y", 60);
    r1.setAttribute("width", cellW - 2); r1.setAttribute("height", 22);
    r1.setAttribute("rx", 2);
    r1.setAttribute("fill", filled ? "#2563eb" : "#f3f4f6");
    r1.setAttribute("stroke", "#93c5fd");
    svg.appendChild(r1);
  }}
  var lbly = document.createElementNS("http://www.w3.org/2000/svg", "text");
  lbly.setAttribute("x", 20); lbly.setAttribute("y", 52);
  lbly.setAttribute("font-size", "11"); lbly.setAttribute("font-weight", "600"); lbly.setAttribute("fill", "#1e40af");
  lbly.textContent = "Young sublist — " + r.youngPages + "/" + r.youngCap + " pages";
  svg.appendChild(lbly);

  // Midpoint line
  var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  line.setAttribute("x1", 20); line.setAttribute("y1", 100);
  line.setAttribute("x2", W - 20); line.setAttribute("y2", 100);
  line.setAttribute("stroke", "#dc2626"); line.setAttribute("stroke-width", 2);
  line.setAttribute("stroke-dasharray", "5 3");
  svg.appendChild(line);
  var midLbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
  midLbl.setAttribute("x", W - 20); midLbl.setAttribute("y", 95);
  midLbl.setAttribute("text-anchor", "end");
  midLbl.setAttribute("font-size", "10"); midLbl.setAttribute("fill", "#dc2626"); midLbl.setAttribute("font-weight", "600");
  midLbl.textContent = "← midpoint: new pages enter here";
  svg.appendChild(midLbl);

  // Old row (bottom)
  var oldCapShown = Math.min(r.oldCap, 18);
  for (var k = 0; k < oldCapShown; k++) {{
    var filled2 = k < r.oldPages;
    var r2 = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r2.setAttribute("x", 20 + k * cellW); r2.setAttribute("y", 120);
    r2.setAttribute("width", cellW - 2); r2.setAttribute("height", 22);
    r2.setAttribute("rx", 2);
    r2.setAttribute("fill", filled2 ? "#9ca3af" : "#f3f4f6");
    r2.setAttribute("stroke", "#d1d5db");
    svg.appendChild(r2);
  }}
  var lblo = document.createElementNS("http://www.w3.org/2000/svg", "text");
  lblo.setAttribute("x", 20); lblo.setAttribute("y", 112);
  lblo.setAttribute("font-size", "11"); lblo.setAttribute("font-weight", "600"); lblo.setAttribute("fill", "#374151");
  lblo.textContent = "Old sublist — " + r.oldPages + "/" + r.oldCap + " pages";
  svg.appendChild(lblo);

  // Stats
  var stats = document.createElementNS("http://www.w3.org/2000/svg", "text");
  stats.setAttribute("x", 20); stats.setAttribute("y", 200);
  stats.setAttribute("font-size", "11"); stats.setAttribute("fill", "#374151");
  stats.textContent = "Evictions: " + r.evictions + "  ·  Promotions: " + r.promotions;
  svg.appendChild(stats);

  var verdict = document.createElementNS("http://www.w3.org/2000/svg", "text");
  verdict.setAttribute("x", 20); verdict.setAttribute("y", 230);
  verdict.setAttribute("font-size", "11"); verdict.setAttribute("font-weight", "600"); verdict.setAttribute("fill", "#065f46");
  if (r.promotions === 0 && r.evictions > 0) {{
    verdict.textContent = "✓ Young sublist untouched — scan-resistant";
  }} else if (r.promotions > 0) {{
    verdict.textContent = r.promotions + " pages promoted from old → young";
  }} else {{
    verdict.textContent = "Pool not yet full";
  }}
  svg.appendChild(verdict);
}}

function renderClassic(r, pool) {{
  var svg = document.getElementById("svg-classic");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 400;
  var capShown = Math.min(pool, 36);
  var cellW = (W - 40) / 18;
  // Two rows of 18
  for (var i = 0; i < capShown; i++) {{
    var row = Math.floor(i / 18);
    var col = i % 18;
    var rE = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rE.setAttribute("x", 20 + col * cellW); rE.setAttribute("y", 60 + row * 26);
    rE.setAttribute("width", cellW - 2); rE.setAttribute("height", 22);
    rE.setAttribute("rx", 2);
    rE.setAttribute("fill", i < r.population ? "#6b7280" : "#f3f4f6");
    rE.setAttribute("stroke", "#d1d5db");
    svg.appendChild(rE);
  }}
  var lbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
  lbl.setAttribute("x", 20); lbl.setAttribute("y", 52);
  lbl.setAttribute("font-size", "11"); lbl.setAttribute("font-weight", "600"); lbl.setAttribute("fill", "#374151");
  lbl.textContent = "Single list — " + r.population + "/" + pool + " pages";
  svg.appendChild(lbl);

  var stats = document.createElementNS("http://www.w3.org/2000/svg", "text");
  stats.setAttribute("x", 20); stats.setAttribute("y", 200);
  stats.setAttribute("font-size", "11"); stats.setAttribute("fill", "#374151");
  stats.textContent = "Evictions: " + r.evictions + "  ·  Hits: " + r.hits;
  svg.appendChild(stats);

  var verdict = document.createElementNS("http://www.w3.org/2000/svg", "text");
  verdict.setAttribute("x", 20); verdict.setAttribute("y", 230);
  verdict.setAttribute("font-size", "11"); verdict.setAttribute("font-weight", "600"); verdict.setAttribute("fill", "#991b1b");
  if (r.hits === 0 && r.evictions >= pool) {{
    verdict.textContent = "✗ Full scan polluted the pool — all hot pages evicted";
  }} else {{
    verdict.textContent = r.hits + " hits across " + pool + " slots";
  }}
  svg.appendChild(verdict);
}}

var cachedTrace = null, cachedCtl = null;

function run() {{
  var c = teachRuntime.readControls();
  cachedCtl = c;
  cachedTrace = genWorkload(c.workload, c.pool_size);
  var inno = simInnoDB(c.pool_size, cachedTrace, c.old_pct, c.old_ms);
  var classic = simClassic(c.pool_size, cachedTrace);
  renderInnoDB(inno);
  renderClassic(classic, c.pool_size);

  document.getElementById("out-young").textContent = inno.youngPages + "/" + inno.youngCap;
  document.getElementById("out-old").textContent = inno.oldPages + "/" + inno.oldCap;
  document.getElementById("out-promotions").textContent = inno.promotions;
  document.getElementById("out-evictions").textContent = inno.evictions;
  document.getElementById("out-classic-evictions").textContent = classic.evictions;
  document.getElementById("out-classic-hits").textContent = classic.hits;

  var exp;
  if (c.workload === "full_scan") {{
    exp = "One-pass full scan. InnoDB's midpoint insertion keeps every page in the old sublist — the young sublist stays intact. Textbook LRU treats every page as MRU and evicts the whole hot set. Promotions: " + inno.promotions + " (should be 0 because no page is hit twice within " + c.old_ms + " ms).";
  }} else if (c.workload === "hot_set") {{
    exp = "A small hot set repeatedly accessed. Both algorithms keep the hot pages; with a hot set that fits in the pool the classic LRU is actually simpler. Midpoint insertion pays its value back under mixed or scan workloads.";
  }} else {{
    exp = "Mixed: hot queries, then a full scan, then hot queries again. InnoDB's young sublist survives the scan pollution — so the second round of hot queries hits its cache. The textbook LRU evicted the hot set during the scan and has to re-read everything.";
  }}
  document.getElementById("out-explanation").textContent = exp;
  document.getElementById("phase-label").textContent = "Workload complete (" + cachedTrace.length + " accesses)";
}}

function reset() {{
  document.getElementById("phase-label").textContent = "Ready";
  var c = teachRuntime.readControls();
  // Empty render
  renderInnoDB({{youngCap: Math.floor(c.pool_size * (100 - c.old_pct) / 100), oldCap: c.pool_size - Math.floor(c.pool_size * (100 - c.old_pct) / 100), youngPages: 0, oldPages: 0, evictions: 0, promotions: 0, young: [], old: []}});
  renderClassic({{evictions: 0, hits: 0, population: 0, list: []}}, c.pool_size);
  document.getElementById("out-young").textContent = "—";
  document.getElementById("out-old").textContent = "—";
  document.getElementById("out-promotions").textContent = "—";
  document.getElementById("out-evictions").textContent = "—";
  document.getElementById("out-classic-evictions").textContent = "—";
  document.getElementById("out-classic-hits").textContent = "—";
  document.getElementById("out-explanation").textContent = "Press \u201cRun workload\u201d to simulate.";
}}

document.getElementById("btn-run").addEventListener("click", run);
document.getElementById("btn-reset").addEventListener("click", reset);

teachRuntime.wire(reset);
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
