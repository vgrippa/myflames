"""Lesson: InnoDB B+tree lookup.

Shows what happens when InnoDB resolves a point lookup:

* Clustered-PK lookup — one tree descent, leaf holds the row.
* Covering secondary index — one tree descent on the secondary tree.
* Non-covering secondary index — two descents: secondary → PK → clustered.

All parameters are in-page sliders. The cost model in JS mirrors
``_cost_model.innodb_fanout`` / ``innodb_tree_height`` exactly — tests
assert the JS constants match the Python constants.
"""
from . import _html
from ._cost_model import (
    INNODB_PAGE_OVERHEAD_BYTES,
    INNODB_CHILD_POINTER_BYTES,
    INNODB_PAGE_SIZE_DEFAULT,
)


def render() -> str:
    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows in table: <span class="value-pill" data-pill-for="rows">1000000</span></label>
      <input type="range" id="rows" name="rows" min="1" max="9" step="1" value="6">
      <div class="hint">Logarithmic: 10, 100, 1K, 10K, 100K, 1M, 10M, 100M, 1B</div>
    </div>

    <div class="control">
      <label for="key_size">Key size (bytes): <span class="value-pill" data-pill-for="key_size">8</span></label>
      <input type="range" id="key_size" name="key_size" min="4" max="64" step="2" value="8">
      <div class="hint">BIGINT PK = 8 B. INT = 4 B. 64-char VARCHAR prefix ≈ 64 B.</div>
    </div>

    <div class="control">
      <label for="page_size">InnoDB page size</label>
      <select id="page_size" name="page_size">
        <option value="4096">4 KiB</option>
        <option value="8192">8 KiB</option>
        <option value="16384" selected>16 KiB (default)</option>
        <option value="32768">32 KiB</option>
        <option value="65536">64 KiB</option>
      </select>
      <div class="hint">Set at <code>innodb_page_size</code>; default 16 KiB.</div>
    </div>

    <div class="control">
      <label for="key_type">Lookup type</label>
      <select id="key_type" name="key_type">
        <option value="pk">Clustered PK lookup</option>
        <option value="secondary_covering">Covering secondary index</option>
        <option value="secondary_noncovering" selected>Non-covering secondary (PK fetch)</option>
      </select>
      <div class="hint">Non-covering is MySQL's single biggest "secret" I/O tax.</div>
    </div>

  </div>
</section>
"""

    stage_html = """
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Tree descent animation</h2>
  <div class="stage-toolbar">
    <button id="btn-play" class="primary">▶ Play</button>
    <button id="btn-step">Step</button>
    <button id="btn-reset">Reset</button>
    <span style="margin-left:auto;font-size:12px;color:#6b7280" id="phase-label">Ready</span>
  </div>
  <svg id="btree-svg" viewBox="0 0 800 360" xmlns="http://www.w3.org/2000/svg"></svg>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Cost readout (updates live)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Fan-out (non-leaf)</p><p class="value" id="out-fanout">—</p></div>
    <div class="item"><p class="label">Tree height</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Tree traversals</p><p class="value" id="out-traversals">—</p></div>
    <div class="item"><p class="label">Pages touched</p><p class="value" id="out-pages">—</p></div>
    <div class="item"><p class="label">Cold-cache I/O (est.)</p><p class="value" id="out-cold-io">—</p></div>
    <div class="item"><p class="label">Complexity</p><p class="value" id="out-complexity">O(log n)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — why is a non-covering secondary index slower?</summary>
  <div class="body">
    <p>InnoDB stores every table as a <strong>clustered B+tree on the primary key</strong>.
    The leaf pages of that tree hold the full row. A secondary index is a
    <em>separate</em> B+tree whose leaves hold <code>(secondary_cols, primary_key)</code>,
    not the row itself.</p>

    <p>So a lookup by a non-covering secondary index has to:</p>
    <ol>
      <li>Walk the secondary B+tree down to a leaf, yielding <code>PK</code>.</li>
      <li>Walk the <em>clustered</em> B+tree using that <code>PK</code> to fetch the row.</li>
    </ol>
    <p>Two traversals, each the full height of its tree. That is why
    <strong>covering indexes</strong> — where the leaf already has every
    column the query asked for — are such an important tuning tool.</p>

    <p>The fan-out formula here is
    <code>(page_size − {INNODB_PAGE_OVERHEAD_BYTES}) / (key_size + {INNODB_CHILD_POINTER_BYTES})</code>.
    It's an approximation: real InnoDB records have variable-length headers and
    leaf pages split at a 15/16 fill factor, so real fan-out is a bit lower.
    The <em>height</em> is stable under that noise because it's a logarithm —
    halving the real fan-out only adds one level of the tree for every
    factor of the original fan-out.</p>

    <p>Sources: MySQL 8.4 Reference Manual §17.6.2 (InnoDB Indexes),
    §17.11.2 (File Space Management). Default
    <code>innodb_page_size</code> = 16 KiB.</p>
  </div>
</details>
"""

    lesson_js = f"""
// Constants must match myflames/teach/_cost_model.py — enforced by tests.
var INNODB_PAGE_OVERHEAD_BYTES = {INNODB_PAGE_OVERHEAD_BYTES};
var INNODB_CHILD_POINTER_BYTES = {INNODB_CHILD_POINTER_BYTES};
var INNODB_PAGE_SIZE_DEFAULT = {INNODB_PAGE_SIZE_DEFAULT};
var ROW_SCALE = [10, 100, 1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000];

function innodbFanout(keySize, pageSize) {{
  var usable = pageSize - INNODB_PAGE_OVERHEAD_BYTES;
  var entry = keySize + INNODB_CHILD_POINTER_BYTES;
  return Math.max(2, Math.floor(usable / entry));
}}

function innodbTreeHeight(rows, fanOut) {{
  if (rows <= 0) return 2;
  if (rows <= fanOut) return 2;
  return Math.max(2, Math.ceil(Math.log(rows) / Math.log(fanOut)));
}}

function btreeCost(rows, keySize, pageSize, keyType) {{
  var fanOut = innodbFanout(keySize, pageSize);
  var height = innodbTreeHeight(rows, fanOut);
  var traversals = (keyType === "secondary_noncovering") ? 2 : 1;
  var pages = height * traversals;
  var explanation;
  if (keyType === "pk") {{
    explanation = "Clustered PK lookup: one descent of " + height + " levels. The leaf page holds the full row — no extra I/O.";
  }} else if (keyType === "secondary_covering") {{
    explanation = "Covering secondary index: one descent of " + height + " levels. Every column the query asked for is already in the secondary leaf — no clustered-tree visit.";
  }} else {{
    explanation = "Non-covering secondary: " + height + " levels on the secondary tree, then " + height + " more on the clustered tree to fetch the row. Two traversals.";
  }}
  return {{
    fanOut: fanOut,
    height: height,
    traversals: traversals,
    pages: pages,
    explanation: explanation
  }};
}}

// --------------- render the tree as SVG ---------------
function renderTree(height, traversals, phase) {{
  var svg = document.getElementById("btree-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var W = 800, H = 360;
  var treeCount = traversals;
  var treeWidth = W / treeCount;
  var treeLabels = (traversals === 2) ? ["Secondary index", "Clustered PK tree"] : [null];
  var defaultLabel = (treeCount === 1) ? "Clustered PK tree" : "Secondary index";

  for (var t = 0; t < treeCount; t++) {{
    var ox = t * treeWidth;
    var label = (treeLabels[t] !== null) ? treeLabels[t] : defaultLabel;

    // Tree title
    var titleEl = document.createElementNS("http://www.w3.org/2000/svg", "text");
    titleEl.setAttribute("x", ox + treeWidth/2);
    titleEl.setAttribute("y", 22);
    titleEl.setAttribute("text-anchor", "middle");
    titleEl.setAttribute("font-size", "12");
    titleEl.setAttribute("font-weight", "600");
    titleEl.setAttribute("fill", "#374151");
    titleEl.textContent = label;
    svg.appendChild(titleEl);

    // Nodes per level
    var levelY = [50, 130, 210, 290];
    var levels = Math.min(height, 4);
    for (var lv = 0; lv < levels; lv++) {{
      var nodesAtLv = Math.min(Math.pow(2, lv), 8);
      var isActive = (t === phase.tree && lv === phase.level);
      var isVisited = (t < phase.tree) || (t === phase.tree && lv < phase.level);
      for (var n = 0; n < nodesAtLv; n++) {{
        var x = ox + ((n + 0.5) * (treeWidth / nodesAtLv)) - 18;
        var y = levelY[lv];
        var isOnPath = (n === Math.floor(nodesAtLv / 2));
        var color = "#f3f4f6";
        var stroke = "#d1d5db";
        var sw = 1;
        if (isOnPath && isVisited) {{ color = "#e0e7ff"; stroke = "#6366f1"; sw = 1.5; }}
        if (isOnPath && isActive) {{ color = "#fde725"; stroke = "#ca8a04"; sw = 3; }}
        var r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        r.setAttribute("x", x);
        r.setAttribute("y", y);
        r.setAttribute("width", 36);
        r.setAttribute("height", 18);
        r.setAttribute("rx", 3);
        r.setAttribute("fill", color);
        r.setAttribute("stroke", stroke);
        r.setAttribute("stroke-width", sw);
        svg.appendChild(r);
      }}
      // Level label
      var lvLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      lvLabel.setAttribute("x", ox + 6);
      lvLabel.setAttribute("y", y + 13);
      lvLabel.setAttribute("font-size", "9");
      lvLabel.setAttribute("fill", "#6b7280");
      lvLabel.textContent = (lv === 0) ? "root" : (lv === levels - 1 ? "leaf" : "L" + lv);
      svg.appendChild(lvLabel);
    }}
    if (height > 4) {{
      var moreLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      moreLabel.setAttribute("x", ox + treeWidth/2);
      moreLabel.setAttribute("y", 334);
      moreLabel.setAttribute("text-anchor", "middle");
      moreLabel.setAttribute("font-size", "10");
      moreLabel.setAttribute("fill", "#6b7280");
      moreLabel.textContent = "(" + height + " total levels)";
      svg.appendChild(moreLabel);
    }}
  }}
}}

// --------------- animation state ---------------
var animState = {{ tree: 0, level: 0, height: 4, traversals: 1, playing: false, timer: null }};

function stepAnim() {{
  animState.level += 1;
  if (animState.level >= Math.min(animState.height, 4)) {{
    animState.level = 0;
    animState.tree += 1;
  }}
  if (animState.tree >= animState.traversals) {{
    animState.tree = 0;
    animState.level = 0;
    pauseAnim();
    document.getElementById("phase-label").textContent = "Complete — reset to replay";
  }}
  renderTree(animState.height, animState.traversals, animState);
  if (animState.playing) {{
    document.getElementById("phase-label").textContent =
      "Walking tree " + (animState.tree + 1) + "/" + animState.traversals +
      " — level " + animState.level;
  }}
}}

function playAnim() {{
  animState.playing = true;
  document.getElementById("btn-play").textContent = "⏸ Pause";
  animState.timer = setInterval(stepAnim, 700);
}}
function pauseAnim() {{
  animState.playing = false;
  document.getElementById("btn-play").textContent = "▶ Play";
  if (animState.timer) {{ clearInterval(animState.timer); animState.timer = null; }}
}}
function resetAnim() {{
  pauseAnim();
  animState.tree = 0;
  animState.level = 0;
  document.getElementById("phase-label").textContent = "Ready";
  renderTree(animState.height, animState.traversals, animState);
}}

// --------------- main recompute ---------------
function recompute() {{
  var c = teachRuntime.readControls();
  var rowIdx = Math.max(0, Math.min(ROW_SCALE.length - 1, Math.round(c.rows)));
  var rows = ROW_SCALE[rowIdx];
  // Update rows pill to show real number
  var pill = document.querySelector('[data-pill-for="rows"]');
  if (pill) pill.textContent = teachRuntime.formatInt(rows);

  var keySize = Math.round(c.key_size);
  var pageSize = Math.round(c.page_size);
  var keyType = c.key_type;
  var cost = btreeCost(rows, keySize, pageSize, keyType);

  document.getElementById("out-fanout").textContent = teachRuntime.formatInt(cost.fanOut);
  document.getElementById("out-height").textContent = cost.height + " levels";
  document.getElementById("out-traversals").textContent = cost.traversals;
  document.getElementById("out-pages").textContent = cost.pages + " pages";
  document.getElementById("out-cold-io").textContent =
    teachRuntime.formatBytes(cost.pages * pageSize);
  document.getElementById("out-complexity").textContent =
    (cost.traversals === 2) ? "O(log n) + O(log n)" : "O(log n)";
  var expEl = document.getElementById("out-explanation");
  expEl.textContent = cost.explanation;

  animState.height = cost.height;
  animState.traversals = cost.traversals;
  renderTree(cost.height, cost.traversals, animState);
}}

document.getElementById("btn-play").addEventListener("click", function() {{
  if (animState.playing) pauseAnim(); else playAnim();
}});
document.getElementById("btn-step").addEventListener("click", function() {{
  pauseAnim();
  stepAnim();
}});
document.getElementById("btn-reset").addEventListener("click", resetAnim);

teachRuntime.wire(recompute);
"""

    return _html.render_page(
        lesson_id="btree",
        title="B+tree lookup — how InnoDB finds a row",
        subtitle=(
            "Clustered primary key, covering vs non-covering secondary "
            "index, and why one extra tree walk doubles your I/O."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
