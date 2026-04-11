"""Lesson: BNL vs hash join side-by-side with shared sliders."""
from . import _html
from ._cost_model import JOIN_BUFFER_SIZE_DEFAULT, MYSQL_BNL_REMOVED_IN


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Shared parameters — both panels update together</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows">Outer rows: <span class="value-pill" data-pill-for="outer_rows">50000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="1000" max="5000000" step="1000" value="50000">
    </div>

    <div class="control">
      <label for="inner_rows">Inner rows: <span class="value-pill" data-pill-for="inner_rows">200000</span></label>
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

    stage_html = """
<section class="stage">
  <div class="stage-toolbar">
    <span style="font-size:13px;color:#374151;font-weight:600">Left: MariaDB BNL (default) · Right: MySQL 8.4 hash join</span>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div>
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#92400e">MariaDB BNL</p>
      <svg id="svg-bnl" viewBox="0 0 400 220" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
    <div>
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#1e40af">MySQL 8.4 hash join</p>
      <svg id="svg-hash" viewBox="0 0 400 220" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
  </div>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Cost comparison</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">BNL rows examined</p><p class="value" id="bnl-cmp">—</p></div>
    <div class="item"><p class="label">Hash rows examined</p><p class="value ok" id="hash-cmp">—</p></div>
    <div class="item"><p class="label">Speedup (hash/BNL)</p><p class="value" id="speedup">—</p></div>
    <div class="item"><p class="label">BNL complexity</p><p class="value">O(outer · inner / buffer)</p></div>
    <div class="item"><p class="label">Hash complexity</p><p class="value">O(outer + inner)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
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

    lesson_js = """
function bnlCost(outer, inner, rowSize, jbs) {
  var rpb = Math.max(1, Math.floor(jbs / rowSize));
  var blocks = Math.max(1, Math.ceil(outer / rpb));
  return {rpb: rpb, blocks: blocks, cmp: blocks * inner * Math.min(rpb, outer)};
}
function hashCost(build, probe, rowSize, jbs) {
  var buildBytes = Math.floor(build * rowSize * 1.4);
  var fits = buildBytes <= jbs;
  var cmp = build + probe + (fits ? 0 : (build + probe)); // +one more pass on spill
  return {fits: fits, buildBytes: buildBytes, cmp: cmp};
}

function box(svg, x, y, w, h, fill, stroke, sw) {
  var r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  r.setAttribute("x", x); r.setAttribute("y", y);
  r.setAttribute("width", w); r.setAttribute("height", h); r.setAttribute("rx", 4);
  r.setAttribute("fill", fill); r.setAttribute("stroke", stroke); r.setAttribute("stroke-width", sw || 1);
  svg.appendChild(r);
}
function label(svg, x, y, t, size, weight, fill, anchor) {
  var el = document.createElementNS("http://www.w3.org/2000/svg", "text");
  el.setAttribute("x", x); el.setAttribute("y", y);
  el.setAttribute("font-size", size || 11);
  el.setAttribute("font-weight", weight || "normal");
  el.setAttribute("fill", fill || "#374151");
  el.setAttribute("text-anchor", anchor || "start");
  el.textContent = t;
  svg.appendChild(el);
}

function renderBNLPanel(blocks) {
  var svg = document.getElementById("svg-bnl");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var cap = Math.min(blocks, 6);
  var bw = 50;
  for (var i = 0; i < cap; i++) {
    box(svg, 20 + i * bw, 40, bw - 6, 40, "#fde725", "#ca8a04", 1.5);
    label(svg, 20 + i * bw + (bw-6)/2, 65, "B" + (i+1), 11, 600, "#78350f", "middle");
  }
  if (blocks > 6) {
    label(svg, 20 + 6 * bw, 65, "+" + (blocks - 6), 11, 600, "#9ca3af");
  }
  label(svg, 20, 32, "Outer blocks: " + blocks, 11, 600, "#92400e");
  box(svg, 20, 110, 360, 60, "#f0f9ff", "#0284c7", 1.5);
  label(svg, 200, 144, "Inner × " + blocks + " re-scans", 13, 700, "#0c4a6e", "middle");
  label(svg, 20, 104, "Inner table", 11, 600, "#0c4a6e");

  // Arrows: every block to inner
  for (var j = 0; j < cap; j++) {
    var ln = document.createElementNS("http://www.w3.org/2000/svg", "line");
    ln.setAttribute("x1", 20 + j * bw + (bw-6)/2);
    ln.setAttribute("y1", 80);
    ln.setAttribute("x2", 20 + j * bw + (bw-6)/2);
    ln.setAttribute("y2", 108);
    ln.setAttribute("stroke", "#ca8a04"); ln.setAttribute("stroke-width", 1.5);
    svg.appendChild(ln);
  }
  label(svg, 200, 200, "Cost: O(outer · inner / buffer)", 11, 600, "#374151", "middle");
}

function renderHashPanel(fits) {
  var svg = document.getElementById("svg-hash");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  // Build
  box(svg, 20, 40, 100, 60, "#fef3c7", "#d97706", 1.5);
  label(svg, 70, 32, "Build (small)", 11, 600, "#92400e", "middle");
  label(svg, 70, 72, "hash(col)", 11, "normal", "#78350f", "middle");
  // Hash table
  box(svg, 150, 40, 100, 130, fits ? "#dbeafe" : "#fee2e2", fits ? "#2563eb" : "#dc2626", 2);
  label(svg, 200, 32, "Hash table", 11, 600, fits ? "#1e40af" : "#991b1b", "middle");
  for (var i = 0; i < 4; i++) {
    box(svg, 160, 50 + i * 28, 80, 20, "#ffffff", fits ? "#93c5fd" : "#fca5a5");
    label(svg, 200, 63 + i * 28, "bucket " + (i+1), 9, "normal", fits ? "#1e40af" : "#991b1b", "middle");
  }
  // Probe
  box(svg, 280, 40, 100, 60, "#ccfbf1", "#0d9488", 1.5);
  label(svg, 330, 32, "Probe (large)", 11, 600, "#115e59", "middle");
  label(svg, 330, 72, "stream once", 11, "normal", "#134e4a", "middle");
  // Arrows
  var a1 = document.createElementNS("http://www.w3.org/2000/svg", "line");
  a1.setAttribute("x1", 120); a1.setAttribute("y1", 70); a1.setAttribute("x2", 148); a1.setAttribute("y2", 70);
  a1.setAttribute("stroke", "#d97706"); a1.setAttribute("stroke-width", 2); svg.appendChild(a1);
  var a2 = document.createElementNS("http://www.w3.org/2000/svg", "line");
  a2.setAttribute("x1", 280); a2.setAttribute("y1", 70); a2.setAttribute("x2", 252); a2.setAttribute("y2", 70);
  a2.setAttribute("stroke", "#0d9488"); a2.setAttribute("stroke-width", 2); svg.appendChild(a2);

  if (!fits) {
    label(svg, 200, 200, "⚠ Spilled — grace-hash partition to disk", 11, 600, "#991b1b", "middle");
  } else {
    label(svg, 200, 200, "Cost: O(outer + inner)", 11, 600, "#115e59", "middle");
  }
}

function recompute() {
  var c = teachRuntime.readControls();
  var bnl = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  // For hash join we use the smaller side as build; inner is usually larger
  var build = Math.min(c.outer_rows, c.inner_rows);
  var probe = Math.max(c.outer_rows, c.inner_rows);
  var hash = hashCost(build, probe, c.row_size, c.jbs);

  document.getElementById("bnl-cmp").textContent = teachRuntime.formatInt(bnl.cmp);
  document.getElementById("hash-cmp").textContent = teachRuntime.formatInt(hash.cmp);
  var speedup = hash.cmp > 0 ? bnl.cmp / hash.cmp : 0;
  document.getElementById("speedup").textContent =
    isFinite(speedup) ? (speedup >= 2 ? speedup.toFixed(0) + "×" : speedup.toFixed(2) + "×") : "—";

  var exp = "With these parameters: MariaDB BNL examines ~" + teachRuntime.formatInt(bnl.cmp) +
    " row pairs (" + bnl.blocks + " block(s) × inner scans). MySQL hash join examines " +
    teachRuntime.formatInt(hash.cmp) + " rows (one build + one probe" +
    (hash.fits ? "" : ", plus a spill pass") + "). ";
  if (speedup >= 10) {
    exp += "At this scale hash join is ~" + speedup.toFixed(0) + "× faster — this is why MySQL 8.4 removed BNL.";
  } else if (speedup >= 2) {
    exp += "Hash is " + speedup.toFixed(1) + "× less work even at this modest size.";
  } else {
    exp += "At tiny sizes the algorithms are comparable; at scale hash wins by orders of magnitude.";
  }
  document.getElementById("out-explanation").textContent = exp;

  renderBNLPanel(bnl.blocks);
  renderHashPanel(hash.fits);
}

teachRuntime.wire(recompute);
"""

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
