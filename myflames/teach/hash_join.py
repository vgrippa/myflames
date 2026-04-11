"""Lesson: Hash join with grace-hash spill (MySQL 8.4)."""
from . import _html
from ._cost_model import JOIN_BUFFER_SIZE_DEFAULT


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Parameters (MySQL 8.4 hash join)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="build_rows">Build-side rows: <span class="value-pill" data-pill-for="build_rows">1000</span></label>
      <input type="range" id="build_rows" name="build_rows" min="100" max="5000000" step="100" value="1000">
      <div class="hint">Smaller input → MySQL picks this side for the hash table.</div>
    </div>

    <div class="control">
      <label for="probe_rows">Probe-side rows: <span class="value-pill" data-pill-for="probe_rows">100000</span></label>
      <input type="range" id="probe_rows" name="probe_rows" min="1000" max="100000000" step="1000" value="100000">
      <div class="hint">Larger input — streamed through the built hash table once.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="8192" max="134217728" step="8192" value="{JOIN_BUFFER_SIZE_DEFAULT}">
      <div class="hint">MySQL 8.4 default: 256 KiB. Bigger → fewer spills.</div>
    </div>

  </div>
</section>
"""

    stage_html = """
<section class="stage">
  <div class="stage-toolbar">
    <button id="btn-play" class="primary">▶ Play</button>
    <button id="btn-reset">Reset</button>
    <span style="margin-left:auto;font-size:12px;color:#6b7280" id="phase-label">Ready</span>
  </div>
  <svg id="hash-svg" viewBox="0 0 800 340" xmlns="http://www.w3.org/2000/svg"></svg>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Cost readout (MySQL 8.4 hash join)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Build-side memory</p><p class="value" id="out-build">—</p></div>
    <div class="item"><p class="label">Fits in join_buffer_size?</p><p class="value" id="out-fits">—</p></div>
    <div class="item"><p class="label">Spilled to disk?</p><p class="value" id="out-spilled">—</p></div>
    <div class="item"><p class="label">Partitions</p><p class="value" id="out-parts">—</p></div>
    <div class="item"><p class="label">Phases</p><p class="value" id="out-phases">—</p></div>
    <div class="item"><p class="label">Complexity</p><p class="value" id="out-complexity">O(n + m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — what happens when the build side doesn't fit?</summary>
  <div class="body">
    <p>MySQL 8.4 allocates the hash table out of <code>join_buffer_size</code>.
    If the whole build side fits, you get a single-pass hash join:
    <strong>phase 1</strong> builds the hash table, <strong>phase 2</strong>
    streams the probe rows through and emits matches. O(build + probe).</p>

    <p>If it doesn't fit, MySQL falls back to <em>grace hash</em>: both
    inputs are partitioned by a hash function onto disk, one file per
    partition. Each partition is then built + probed independently. Total
    I/O roughly doubles (write both inputs, read both inputs again), and
    the complexity grows to O(2·(build + probe)) with a disk-bandwidth
    constant. The lesson's <em>Partitions</em> readout shows how many
    partitions are needed for a given build size.</p>

    <p>Source: MySQL 8.4 Reference Manual §10.2.1.4 "Hash Join
    Optimization" and Doc Library: hash join. Build-side selection heuristic
    (smaller input wins) is in §10.2.1.4.</p>
  </div>
</details>
"""

    lesson_js = f"""
var JOIN_BUFFER_SIZE_DEFAULT = {JOIN_BUFFER_SIZE_DEFAULT};

function hashJoinCost(buildRows, probeRows, rowSize, jbs) {{
  var buildBytes = Math.floor(buildRows * rowSize * 1.4);
  var fits = buildBytes <= jbs;
  var spilled = !fits;
  var partitions = spilled ? Math.max(2, Math.ceil(buildBytes / jbs)) : 1;
  var phases = spilled ? 4 : 2;
  return {{
    buildBytes: buildBytes,
    fits: fits,
    spilled: spilled,
    partitions: partitions,
    phases: phases
  }};
}}

function renderHash(phase, spilled, partitions) {{
  var svg = document.getElementById("hash-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // Build side (left) → hash table (middle) ← probe (right)
  function rect(x, y, w, h, fill, stroke, sw) {{
    var r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", x); r.setAttribute("y", y);
    r.setAttribute("width", w); r.setAttribute("height", h);
    r.setAttribute("rx", 6);
    r.setAttribute("fill", fill); r.setAttribute("stroke", stroke); r.setAttribute("stroke-width", sw || 1);
    svg.appendChild(r);
  }}
  function text(x, y, t, size, weight, fill, anchor) {{
    var el = document.createElementNS("http://www.w3.org/2000/svg", "text");
    el.setAttribute("x", x); el.setAttribute("y", y);
    el.setAttribute("font-size", size || 12);
    el.setAttribute("font-weight", weight || "normal");
    el.setAttribute("fill", fill || "#374151");
    el.setAttribute("text-anchor", anchor || "start");
    el.textContent = t;
    svg.appendChild(el);
  }}

  // Build input
  rect(40, 60, 160, 120, "#fef3c7", "#d97706", 1.5);
  text(120, 52, "Build input (smaller)", 12, 600, "#92400e", "middle");
  text(120, 100, "• row 1", 11, "normal", "#78350f", "middle");
  text(120, 118, "• row 2", 11, "normal", "#78350f", "middle");
  text(120, 136, "• ...", 11, "normal", "#78350f", "middle");
  text(120, 160, "hash(col) →", 11, "normal", "#78350f", "middle");

  // Hash table
  var htActive = (phase >= 1);
  rect(320, 60, 160, 220, htActive ? "#dbeafe" : "#f3f4f6", htActive ? "#2563eb" : "#d1d5db", htActive ? 2 : 1);
  text(400, 52, "Hash table", 12, 600, htActive ? "#1e40af" : "#9ca3af", "middle");
  for (var b = 0; b < 6; b++) {{
    rect(336, 70 + b*32, 128, 24, htActive ? "#ffffff" : "#f9fafb", htActive ? "#93c5fd" : "#e5e7eb");
    text(400, 87 + b*32, "bucket " + (b+1), 10, "normal", htActive ? "#1e40af" : "#9ca3af", "middle");
  }}

  // Probe input
  rect(600, 60, 160, 120, "#ccfbf1", "#0d9488", 1.5);
  text(680, 52, "Probe input (larger)", 12, 600, "#115e59", "middle");
  text(680, 100, "• row 1", 11, "normal", "#134e4a", "middle");
  text(680, 118, "• row 2", 11, "normal", "#134e4a", "middle");
  text(680, 136, "• ...", 11, "normal", "#134e4a", "middle");
  var probeActive = (phase >= 2);
  text(680, 160, probeActive ? "hash(col) → probe" : "(wait for build)", 11, "normal", probeActive ? "#0f766e" : "#9ca3af", "middle");

  // Arrows
  if (phase >= 1) {{
    var a1 = document.createElementNS("http://www.w3.org/2000/svg", "line");
    a1.setAttribute("x1", 200); a1.setAttribute("y1", 120); a1.setAttribute("x2", 318); a1.setAttribute("y2", 120);
    a1.setAttribute("stroke", "#d97706"); a1.setAttribute("stroke-width", 2.5); svg.appendChild(a1);
    text(259, 114, "build", 10, 600, "#d97706", "middle");
  }}
  if (phase >= 2) {{
    var a2 = document.createElementNS("http://www.w3.org/2000/svg", "line");
    a2.setAttribute("x1", 600); a2.setAttribute("y1", 170); a2.setAttribute("x2", 482); a2.setAttribute("y2", 170);
    a2.setAttribute("stroke", "#0d9488"); a2.setAttribute("stroke-width", 2.5); svg.appendChild(a2);
    text(541, 164, "probe", 10, 600, "#0d9488", "middle");
  }}

  if (spilled) {{
    rect(40, 290, 720, 36, "#fef2f2", "#dc2626", 1.5);
    text(400, 313, "⚠ Spilled! Both inputs partitioned into " + partitions + " chunks on disk → 2-pass I/O", 12, 600, "#991b1b", "middle");
  }}
}}

var animHash = {{ phase: 0, spilled: false, parts: 1, timer: null, playing: false }};

function step() {{
  animHash.phase += 1;
  if (animHash.phase > 2) {{
    animHash.phase = 2;
    pause();
    document.getElementById("phase-label").textContent = "Complete";
    return;
  }}
  document.getElementById("phase-label").textContent =
    (animHash.phase === 1 ? "Phase 1: Build hash table" : "Phase 2: Probe + emit matches");
  renderHash(animHash.phase, animHash.spilled, animHash.parts);
}}
function play() {{
  animHash.playing = true;
  document.getElementById("btn-play").textContent = "⏸ Pause";
  animHash.timer = setInterval(step, 1000);
}}
function pause() {{
  animHash.playing = false;
  document.getElementById("btn-play").textContent = "▶ Play";
  if (animHash.timer) {{ clearInterval(animHash.timer); animHash.timer = null; }}
}}
function reset() {{
  pause();
  animHash.phase = 0;
  document.getElementById("phase-label").textContent = "Ready";
  renderHash(0, animHash.spilled, animHash.parts);
}}

function recompute() {{
  var c = teachRuntime.readControls();
  var cost = hashJoinCost(c.build_rows, c.probe_rows, c.row_size, c.jbs);
  document.getElementById("out-build").textContent = teachRuntime.formatBytes(cost.buildBytes);
  document.getElementById("out-fits").textContent = cost.fits ? "Yes" : "No";
  document.getElementById("out-fits").className = "value " + (cost.fits ? "ok" : "warn");
  document.getElementById("out-spilled").textContent = cost.spilled ? "Yes" : "No";
  document.getElementById("out-spilled").className = "value " + (cost.spilled ? "hot" : "ok");
  document.getElementById("out-parts").textContent = cost.partitions;
  document.getElementById("out-phases").textContent = cost.phases;
  document.getElementById("out-complexity").textContent = cost.spilled ? "O(2·(n + m))" : "O(n + m)";
  var expEl = document.getElementById("out-explanation");
  expEl.textContent = cost.spilled
    ? "Build side is " + teachRuntime.formatBytes(cost.buildBytes) + " — bigger than join_buffer_size. MySQL spills: partition both inputs into " + cost.partitions + " chunks on disk, then probe each partition separately. Cost roughly doubles."
    : "Build side is " + teachRuntime.formatBytes(cost.buildBytes) + " — fits in join_buffer_size. Single-pass: build the hash table once, stream the probe rows through. O(build + probe).";
  animHash.spilled = cost.spilled;
  animHash.parts = cost.partitions;
  animHash.phase = 0;
  renderHash(0, cost.spilled, cost.partitions);
}}

document.getElementById("btn-play").addEventListener("click", function() {{
  if (animHash.playing) pause(); else play();
}});
document.getElementById("btn-reset").addEventListener("click", reset);

teachRuntime.wire(recompute);
"""

    return _html.render_page(
        lesson_id="hash",
        title="Hash join — build, probe, and grace-hash spill",
        subtitle=(
            "MySQL 8.4's default for non-indexed equi-joins. See what happens "
            "when the build side doesn't fit in join_buffer_size."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
