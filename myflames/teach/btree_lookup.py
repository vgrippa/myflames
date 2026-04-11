"""Lesson: InnoDB B+tree lookup.

Animates a query token descending the tree(s), with smooth easing,
pause/resume/speed controls, a real SQL example using the ``users``
table, and a log-log complexity chart showing how page count scales
with row count.
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
      <label for="rows">Rows in <code>users</code> table: <span class="value-pill" data-pill-for="rows">1000000</span></label>
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
        <option value="pk">Clustered PK lookup (WHERE id = 42)</option>
        <option value="secondary_covering">Covering secondary index</option>
        <option value="secondary_noncovering" selected>Non-covering secondary (PK fetch)</option>
      </select>
      <div class="hint">Non-covering is MySQL's single biggest "secret" I/O tax.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Non-covering secondary index lookup\n"
            "SELECT id, first_name, last_name, country_code\n"
            "FROM   users\n"
            "WHERE  email = 'alice@example.com';   -- idx_users_email contains only (email, id)"
        ),
        note="Switch the lookup-type dropdown to see PK-clustered vs covering-index vs non-covering variants of this query."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The grey rectangles are B+tree pages. Top row is the root page, bottom row is the leaf.",
            "A red diamond 'query token' appears above the root and descends level by level — each step is one page read.",
            "When the token arrives at a page, the page pulses yellow. That's the 'page is in the buffer pool now'.",
            "For non-covering secondary lookups, the token fades out at the secondary leaf, then re-appears above the clustered tree root and descends a second time to fetch the row — that's the extra I/O covering indexes avoid.",
        ],
    )

    stage_html = f"""
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Tree descent animation</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Adjust parameters, then press Play")}
  <svg id="btree-svg" viewBox="0 0 800 400" xmlns="http://www.w3.org/2000/svg"></svg>
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
  <div class="complexity-chart">
    <p class="chart-title">Pages touched vs table size (log–log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
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
  return {{fanOut: fanOut, height: height, traversals: traversals, pages: pages, explanation: explanation}};
}}

// ---------- tree rendering ----------
var W = 800, H = 400;
var treeState = {{ height: 3, traversals: 1, nodes: [[],[]], token: null }};

function buildTrees(height, traversals) {{
  var svg = document.getElementById("btree-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  treeState.height = height;
  treeState.traversals = traversals;
  treeState.nodes = [[], []];
  var treeWidth = W / traversals;
  var levels = Math.min(height, 4);
  var levelY = [70, 150, 230, 310];
  var treeNames = (traversals === 2) ? ["Secondary B+tree — idx_users_email", "Clustered B+tree — users (PK)"] : ["Clustered B+tree — users (PK)"];
  for (var t = 0; t < traversals; t++) {{
    var ox = t * treeWidth;
    var heading = anim.svgEl("text", {{
      x: ox + treeWidth/2, y: 32, "text-anchor": "middle",
      "font-size": 13, "font-weight": 700, fill: "#1f2937"
    }});
    heading.textContent = treeNames[t];
    svg.appendChild(heading);

    var treeNodes = [];
    for (var lv = 0; lv < levels; lv++) {{
      var nodesAtLv = Math.min(Math.pow(2, lv), 8);
      var y = levelY[lv];
      var levelNodes = [];
      for (var n = 0; n < nodesAtLv; n++) {{
        var x = ox + ((n + 0.5) * (treeWidth / nodesAtLv)) - 22;
        var isOnPath = (n === Math.floor(nodesAtLv / 2));
        var rect = anim.svgEl("rect", {{
          x: x, y: y, width: 44, height: 22, rx: 4, ry: 4,
          fill: "#f3f4f6", stroke: "#9ca3af", "stroke-width": 1
        }});
        svg.appendChild(rect);
        var lvLbl = anim.svgEl("text", {{
          x: x + 22, y: y + 15, "text-anchor": "middle",
          "font-size": 9, fill: "#6b7280"
        }});
        lvLbl.textContent = (lv === 0) ? "root" : (lv === levels - 1 ? "leaf" : "L" + lv);
        svg.appendChild(lvLbl);
        levelNodes.push({{
          rect: rect, label: lvLbl, cx: x + 22, cy: y + 11, onPath: isOnPath
        }});
      }}
      treeNodes.push(levelNodes);
    }}
    treeState.nodes[t] = treeNodes;
    if (height > 4) {{
      var more = anim.svgEl("text", {{
        x: ox + treeWidth/2, y: 370, "text-anchor": "middle",
        "font-size": 11, fill: "#9ca3af", "font-style": "italic"
      }});
      more.textContent = "(" + height + " levels total — showing top 4)";
      svg.appendChild(more);
    }}
  }}

  // Query token
  var token = anim.svgEl("polygon", {{
    points: "0,-9 9,0 0,9 -9,0",
    fill: "#ff3d3d", stroke: "#991b1b", "stroke-width": 1.5,
    opacity: 0, transform: "translate(0,0)"
  }});
  svg.appendChild(token);
  treeState.token = token;
}}

function resetTreeColors() {{
  for (var t = 0; t < treeState.nodes.length; t++) {{
    for (var lv = 0; lv < treeState.nodes[t].length; lv++) {{
      var lvlNodes = treeState.nodes[t][lv];
      for (var n = 0; n < lvlNodes.length; n++) {{
        lvlNodes[n].rect.setAttribute("fill", "#f3f4f6");
        lvlNodes[n].rect.setAttribute("stroke", "#9ca3af");
        lvlNodes[n].rect.setAttribute("stroke-width", 1);
      }}
    }}
  }}
  if (treeState.token) {{
    treeState.token.setAttribute("opacity", 0);
    treeState.token.setAttribute("transform", "translate(0,0)");
  }}
}}

// ---------- timeline ----------
var currentTimeline = null;

function buildDescentTimeline() {{
  resetTreeColors();
  var tl = anim.timeline();
  var token = treeState.token;
  var levelsShown = Math.min(treeState.height, 4);
  var phaseLabel = document.getElementById("phase-label");

  for (var t = 0; t < treeState.traversals; t++) {{
    (function(treeIdx) {{
      tl.call(function() {{
        var rootNode = treeState.nodes[treeIdx][0].find(function(n) {{ return n.onPath; }});
        if (rootNode) {{
          token.setAttribute("transform", "translate(" + rootNode.cx + "," + (rootNode.cy - 40) + ")");
          token.setAttribute("opacity", 0);
        }}
        phaseLabel.textContent = (treeState.traversals === 2 ? (treeIdx === 0 ? "Walking idx_users_email (secondary)" : "Walking users PK (clustered)") : "Walking users PK (clustered)");
      }});
      tl.add({{
        from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
        onUpdate: function(v) {{ token.setAttribute("opacity", v); }}
      }});
      for (var lv = 0; lv < levelsShown; lv++) {{
        (function(levelIdx) {{
          var activeNode = treeState.nodes[treeIdx][levelIdx].find(function(n) {{ return n.onPath; }});
          if (!activeNode) return;
          tl.call(function() {{
            phaseLabel.textContent = "Tree " + (treeIdx + 1) + "/" + treeState.traversals +
              " — descending to " + (levelIdx === 0 ? "root" : (levelIdx === levelsShown - 1 ? "leaf" : "level " + levelIdx));
          }});
          var currentPos;
          if (levelIdx === 0) {{
            currentPos = {{ x: activeNode.cx, y: activeNode.cy - 40 }};
          }} else {{
            var prev = treeState.nodes[treeIdx][levelIdx - 1].find(function(n) {{ return n.onPath; }});
            currentPos = {{ x: prev.cx, y: prev.cy }};
          }}
          tl.add({{
            from: currentPos,
            to: {{ x: activeNode.cx, y: activeNode.cy }},
            duration: 480, ease: anim.easeInOutCubic,
            onUpdate: function(p) {{
              token.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
            }},
            onComplete: function() {{
              activeNode.rect.setAttribute("fill", "#fde725");
              activeNode.rect.setAttribute("stroke", "#ca8a04");
              anim.pulse(activeNode.rect, 3, 1, 360);
            }}
          }});
          tl.delay(160);
        }})(lv);
      }}
      if (treeIdx < treeState.traversals - 1) {{
        tl.delay(280);
        tl.add({{
          from: 1, to: 0, duration: 220, ease: anim.easeInCubic,
          onUpdate: function(v) {{ token.setAttribute("opacity", v); }}
        }});
        tl.call(function() {{
          phaseLabel.textContent = "Secondary-index leaf holds PK — now fetching row from clustered tree";
        }});
        tl.delay(280);
      }}
    }})(t);
  }}
  tl.call(function() {{
    phaseLabel.textContent = "✓ Lookup complete — press Reset to replay";
    teachRuntime.animationDone();
  }});
  return tl;
}}

function playAnim() {{
  if (currentTimeline) currentTimeline.stop();
  currentTimeline = buildDescentTimeline();
  currentTimeline.play();
}}
function resetAnim() {{
  if (currentTimeline) currentTimeline.stop();
  currentTimeline = null;
  resetTreeColors();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}}

// ---------- complexity chart ----------
function renderChart(keySize, pageSize, keyType, currentRows) {{
  var fanOut = innodbFanout(keySize, pageSize);
  var traversals = (keyType === "secondary_noncovering") ? 2 : 1;
  anim.complexityChart({{
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 10, xMax: 1e10,
    xLabel: "Rows in users table", yLabel: "Pages touched",
    curves: [
      {{ label: "This lesson's choice", color: "#2563eb",
        fn: function(n) {{ return innodbTreeHeight(n, fanOut) * traversals; }} }},
      {{ label: "Hypothetical linear scan", color: "#dc2626",
        fn: function(n) {{ return Math.max(1, n / 400); }} }}
    ],
    current: {{ x: currentRows }}
  }});
}}

// ---------- main recompute ----------
function recompute() {{
  var c = teachRuntime.readControls();
  var rowIdx = Math.max(0, Math.min(ROW_SCALE.length - 1, Math.round(c.rows)));
  var rows = ROW_SCALE[rowIdx];
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
  document.getElementById("out-cold-io").textContent = teachRuntime.formatBytes(cost.pages * pageSize);
  document.getElementById("out-complexity").textContent = (cost.traversals === 2) ? "O(log n) + O(log n)" : "O(log n)";
  document.getElementById("out-explanation").textContent = cost.explanation;

  if (currentTimeline) {{ currentTimeline.stop(); currentTimeline = null; }}
  buildTrees(cost.height, cost.traversals);
  resetTreeColors();
  renderChart(keySize, pageSize, keyType, rows);
}}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar(playAnim, resetAnim);
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
