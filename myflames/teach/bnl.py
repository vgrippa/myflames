"""Lesson: Block Nested Loop join (MariaDB's default for non-indexed joins).

Prominently warns that MySQL 8.0.20+ removed BNL — this lesson applies to
MariaDB 11.x. The animation shows outer rows filling the join buffer in
blocks, with the inner table re-scanned once per block.
"""
from . import _html
from ._cost_model import JOIN_BUFFER_SIZE_DEFAULT, MYSQL_BNL_REMOVED_IN


def render() -> str:
    banner_html = f"""
<div class="banner">
  <strong>Heads up:</strong> BNL is <strong>not used by MySQL 8.4</strong> —
  MySQL {MYSQL_BNL_REMOVED_IN} removed it in favour of hash join for non-indexed
  equi-joins. This lesson shows <strong>MariaDB 11.x</strong>, where BNL is
  still the default (<code>join_cache_level = 2</code>). Compare it with hash
  join in the <a href="join.html">BNL vs hash</a> lesson.
</div>
"""

    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (MariaDB 11.x Block Nested Loop)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows">Outer rows: <span class="value-pill" data-pill-for="outer_rows">10000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Rows from the outer table (driving side of the join).</div>
    </div>

    <div class="control">
      <label for="inner_rows">Inner rows: <span class="value-pill" data-pill-for="inner_rows">50000</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="100" max="10000000" step="100" value="50000">
      <div class="hint">Rows in the inner table, rescanned once per block.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of one outer row in the join buffer.</div>
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="8192" max="16777216" step="8192" value="{JOIN_BUFFER_SIZE_DEFAULT}">
      <div class="hint">MariaDB 11.4 default is {JOIN_BUFFER_SIZE_DEFAULT} B (256 KiB).</div>
    </div>

  </div>
</section>
"""

    stage_html = """
<section class="stage">
  <div class="stage-toolbar">
    <button id="btn-play" class="primary">▶ Play</button>
    <button id="btn-step">Step</button>
    <button id="btn-reset">Reset</button>
    <span style="margin-left:auto;font-size:12px;color:#6b7280" id="phase-label">Ready</span>
  </div>
  <svg id="bnl-svg" viewBox="0 0 800 340" xmlns="http://www.w3.org/2000/svg"></svg>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Cost readout (MariaDB 11.x BNL)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Rows per block</p><p class="value" id="out-rpb">—</p></div>
    <div class="item"><p class="label">Blocks</p><p class="value" id="out-blocks">—</p></div>
    <div class="item"><p class="label">Inner re-scans</p><p class="value" id="out-scans">—</p></div>
    <div class="item"><p class="label">Row comparisons</p><p class="value" id="out-cmp">—</p></div>
    <div class="item"><p class="label">Complexity</p><p class="value" id="out-complexity">O(n·m / b)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — why does MariaDB still use BNL?</summary>
  <div class="body">
    <p>MariaDB controls block-based join algorithms with
    <code>join_cache_level</code> (0–8), not <code>optimizer_switch</code>.
    The default is <strong>2</strong> — "BNL without hashing". Levels 3
    and 4 enable <em>incremental</em> and <em>hashed</em> BNL respectively.</p>

    <p>MariaDB's "hashed BNL" (level 4) is <strong>not</strong> the same
    algorithm as MySQL 8.4's hash join. It's still BNL structurally — each
    outer block builds a tiny hash table, then the inner is scanned once
    per block and probed into that hash table. It's faster than plain BNL
    but still O(outer_blocks × inner_rows), not O(outer + inner). See the
    <a href="join.html">BNL vs hash</a> lesson for the visual.</p>

    <p>MySQL 8.0.20 removed BNL entirely — <code>optimizer_switch=block_nested_loop</code>
    is a no-op in 8.4. For non-indexed equi-joins MySQL now always uses a
    two-phase hash join.</p>

    <p>Sources: MariaDB Knowledge Base "Block-based Join Algorithms";
    "What's New in MySQL 8.0.20" release notes.</p>
  </div>
</details>
"""

    lesson_js = f"""
var JOIN_BUFFER_SIZE_DEFAULT = {JOIN_BUFFER_SIZE_DEFAULT};

function bnlCost(outer, inner, rowSize, jbs) {{
  var rpb = Math.max(1, Math.floor(jbs / rowSize));
  var blocks = Math.max(1, Math.ceil(outer / rpb));
  var innerScans = blocks;
  var cmp = blocks * inner * Math.min(rpb, outer);
  return {{rpb: rpb, blocks: blocks, innerScans: innerScans, cmp: cmp}};
}}

function renderBnl(blocks, activeBlock) {{
  var svg = document.getElementById("bnl-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 800;

  // Outer blocks (top row)
  var capBlocks = Math.min(blocks, 12);
  var bw = Math.max(40, (W - 60) / capBlocks);
  for (var i = 0; i < capBlocks; i++) {{
    var isActive = (i === activeBlock);
    var r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", 30 + i * bw);
    r.setAttribute("y", 60);
    r.setAttribute("width", bw - 6);
    r.setAttribute("height", 50);
    r.setAttribute("rx", 4);
    r.setAttribute("fill", isActive ? "#fde725" : (i < activeBlock ? "#e5e7eb" : "#f9fafb"));
    r.setAttribute("stroke", isActive ? "#ca8a04" : "#d1d5db");
    r.setAttribute("stroke-width", isActive ? 3 : 1);
    svg.appendChild(r);
    var lbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
    lbl.setAttribute("x", 30 + i * bw + (bw-6)/2);
    lbl.setAttribute("y", 89);
    lbl.setAttribute("text-anchor", "middle");
    lbl.setAttribute("font-size", "11");
    lbl.setAttribute("font-weight", "600");
    lbl.setAttribute("fill", isActive ? "#78350f" : "#6b7280");
    lbl.textContent = "B" + (i + 1);
    svg.appendChild(lbl);
  }}
  if (blocks > 12) {{
    var more = document.createElementNS("http://www.w3.org/2000/svg", "text");
    more.setAttribute("x", W - 30); more.setAttribute("y", 89);
    more.setAttribute("text-anchor", "end");
    more.setAttribute("font-size", "11"); more.setAttribute("fill", "#9ca3af");
    more.textContent = "+" + (blocks - 12) + " more";
    svg.appendChild(more);
  }}
  var hdr = document.createElementNS("http://www.w3.org/2000/svg", "text");
  hdr.setAttribute("x", 30); hdr.setAttribute("y", 50);
  hdr.setAttribute("font-size", "12"); hdr.setAttribute("font-weight", "600"); hdr.setAttribute("fill", "#374151");
  hdr.textContent = "Outer table → join buffer, packed into blocks";
  svg.appendChild(hdr);

  // Inner table (big block bottom)
  var innerY = 180;
  var innerR = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  innerR.setAttribute("x", 30); innerR.setAttribute("y", innerY);
  innerR.setAttribute("width", W - 60); innerR.setAttribute("height", 80);
  innerR.setAttribute("rx", 6); innerR.setAttribute("fill", "#f0f9ff");
  innerR.setAttribute("stroke", "#0284c7"); innerR.setAttribute("stroke-width", 1.5);
  svg.appendChild(innerR);

  var innerLbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
  innerLbl.setAttribute("x", 30); innerLbl.setAttribute("y", innerY - 8);
  innerLbl.setAttribute("font-size", "12"); innerLbl.setAttribute("font-weight", "600"); innerLbl.setAttribute("fill", "#0c4a6e");
  innerLbl.textContent = "Inner table (re-scanned once per block)";
  svg.appendChild(innerLbl);

  var innerBig = document.createElementNS("http://www.w3.org/2000/svg", "text");
  innerBig.setAttribute("x", W/2); innerBig.setAttribute("y", innerY + 50);
  innerBig.setAttribute("text-anchor", "middle");
  innerBig.setAttribute("font-size", "22"); innerBig.setAttribute("font-weight", "700"); innerBig.setAttribute("fill", "#0c4a6e");
  innerBig.textContent = "full scan " + (activeBlock >= 0 ? ("#" + (activeBlock + 1) + " of " + blocks) : "");
  svg.appendChild(innerBig);

  // Arrow from active block into inner
  if (activeBlock >= 0 && activeBlock < capBlocks) {{
    var arrow = document.createElementNS("http://www.w3.org/2000/svg", "line");
    arrow.setAttribute("x1", 30 + activeBlock * bw + (bw-6)/2);
    arrow.setAttribute("y1", 115);
    arrow.setAttribute("x2", 30 + activeBlock * bw + (bw-6)/2);
    arrow.setAttribute("y2", innerY - 3);
    arrow.setAttribute("stroke", "#ca8a04");
    arrow.setAttribute("stroke-width", 2.5);
    arrow.setAttribute("marker-end", "url(#arrow)");
    svg.appendChild(arrow);
    var defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    var marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
    marker.setAttribute("id", "arrow"); marker.setAttribute("markerWidth", "10"); marker.setAttribute("markerHeight", "7");
    marker.setAttribute("refX", "9"); marker.setAttribute("refY", "3.5"); marker.setAttribute("orient", "auto");
    var poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
    poly.setAttribute("points", "0 0, 10 3.5, 0 7"); poly.setAttribute("fill", "#ca8a04");
    marker.appendChild(poly); defs.appendChild(marker); svg.appendChild(defs);
  }}
}}

var animBNL = {{ block: 0, blocks: 1, playing: false, timer: null }};

function step() {{
  animBNL.block += 1;
  if (animBNL.block >= animBNL.blocks) {{
    animBNL.block = animBNL.blocks - 1;
    pause();
    document.getElementById("phase-label").textContent = "Complete — all " + animBNL.blocks + " block(s) scanned";
    return;
  }}
  renderBnl(animBNL.blocks, animBNL.block);
  document.getElementById("phase-label").textContent = "Scanning inner table for block " + (animBNL.block + 1) + "/" + animBNL.blocks;
}}
function play() {{
  animBNL.playing = true;
  document.getElementById("btn-play").textContent = "⏸ Pause";
  animBNL.timer = setInterval(step, 800);
}}
function pause() {{
  animBNL.playing = false;
  document.getElementById("btn-play").textContent = "▶ Play";
  if (animBNL.timer) {{ clearInterval(animBNL.timer); animBNL.timer = null; }}
}}
function reset() {{
  pause();
  animBNL.block = 0;
  document.getElementById("phase-label").textContent = "Ready";
  renderBnl(animBNL.blocks, 0);
}}

function recompute() {{
  var c = teachRuntime.readControls();
  var cost = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  document.getElementById("out-rpb").textContent = teachRuntime.formatInt(cost.rpb);
  document.getElementById("out-blocks").textContent = teachRuntime.formatInt(cost.blocks);
  document.getElementById("out-scans").textContent = teachRuntime.formatInt(cost.innerScans);
  document.getElementById("out-cmp").textContent = teachRuntime.formatInt(cost.cmp);
  var expEl = document.getElementById("out-explanation");
  expEl.textContent =
    "Outer rows pack into " + cost.blocks + " block(s) of up to " + cost.rpb +
    " rows. The inner table is fully re-scanned once per block — " +
    cost.innerScans + " scan(s). Raise join_buffer_size → fewer blocks → fewer rescans.";
  animBNL.blocks = cost.blocks;
  animBNL.block = 0;
  renderBnl(cost.blocks, 0);
}}

document.getElementById("btn-play").addEventListener("click", function() {{
  if (animBNL.playing) pause(); else play();
}});
document.getElementById("btn-step").addEventListener("click", function() {{ pause(); step(); }});
document.getElementById("btn-reset").addEventListener("click", reset);

teachRuntime.wire(recompute);
"""

    return _html.render_page(
        lesson_id="bnl",
        title="Block Nested Loop join — MariaDB's default",
        subtitle=(
            "Watch join_buffer_size decide how many times the inner table is "
            "re-scanned. Bigger buffer, fewer blocks, less I/O."
        ),
        version_chip="MariaDB 11.4",
        banner_html=banner_html,
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
