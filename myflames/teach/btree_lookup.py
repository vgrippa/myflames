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
            "For non-covering secondary lookups, the secondary-tree LEAF stores the primary key (labelled 'leaf: PK = 42'). A dashed orange PK-hop arrow then links that leaf directly to the clustered-tree leaf which holds the actual row (labelled 'leaf: full row'). The token rides that arrow across — no re-descent magic, the link is a real index pointer.",
            "That dashed arrow is the extra I/O a covering index would eliminate.",
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
var treeState = {{
  height: 3, traversals: 1, nodes: [[],[]], token: null,
  pkLink: null, pkLinkLabel: null, leafLabels: []
}};

function buildTrees(height, traversals) {{
  var svg = document.getElementById("btree-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  treeState.height = height;
  treeState.traversals = traversals;
  treeState.nodes = [[], []];
  treeState.leafLabels = [];
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

  // PK link path — dashed curve from secondary leaf to clustered leaf.
  // Only meaningful when traversals === 2. Drawn first so the token
  // renders on top. Hidden until the secondary leaf is reached.
  treeState.pkLink = null;
  treeState.pkLinkLabel = null;
  if (traversals === 2) {{
    var secLeaf = treeState.nodes[0][levels - 1].find(function(n) {{ return n.onPath; }});
    var clusRoot = treeState.nodes[1][0].find(function(n) {{ return n.onPath; }});
    var clusLeaf = treeState.nodes[1][levels - 1].find(function(n) {{ return n.onPath; }});
    if (secLeaf && clusRoot && clusLeaf) {{
      // Quadratic Bezier arching upward: sec leaf → above clus root → clus leaf
      var sx = secLeaf.cx + 22;  // right edge of secondary leaf
      var sy = secLeaf.cy;
      var ex = clusLeaf.cx;
      var ey = clusLeaf.cy;
      var cx = (sx + ex) / 2;
      var cy = Math.min(sy, ey) - 50;
      var pathD = "M " + sx + "," + sy +
                  " Q " + cx + "," + cy + " " + ex + "," + ey;
      var pkLink = anim.svgEl("path", {{
        d: pathD,
        fill: "none",
        stroke: "#f97316",
        "stroke-width": 2,
        "stroke-dasharray": "5 3",
        "marker-end": "url(#pkArrow)",
        opacity: 0
      }});
      // Arrow marker defs
      var defs = anim.svgEl("defs");
      var marker = anim.svgEl("marker", {{
        id: "pkArrow", viewBox: "0 0 10 10",
        refX: "9", refY: "5",
        markerWidth: "6", markerHeight: "6",
        orient: "auto"
      }});
      var arrowPath = anim.svgEl("path", {{
        d: "M 0 0 L 10 5 L 0 10 z", fill: "#f97316"
      }});
      marker.appendChild(arrowPath);
      defs.appendChild(marker);
      svg.appendChild(defs);
      svg.appendChild(pkLink);
      treeState.pkLink = pkLink;

      var pkLabel = anim.svgEl("text", {{
        x: cx, y: cy - 4, "text-anchor": "middle",
        "font-size": 11, "font-weight": 700, fill: "#c2410c",
        opacity: 0
      }});
      pkLabel.textContent = "uses PK → clustered leaf";
      svg.appendChild(pkLabel);
      treeState.pkLinkLabel = pkLabel;
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
        // Restore the original "root / L1 / leaf" label text
        var orig = (lv === 0) ? "root" : (lv === treeState.nodes[t].length - 1 ? "leaf" : "L" + lv);
        lvlNodes[n].label.textContent = orig;
        lvlNodes[n].label.setAttribute("font-size", 9);
        lvlNodes[n].label.setAttribute("font-weight", "normal");
        lvlNodes[n].label.setAttribute("fill", "#6b7280");
      }}
    }}
  }}
  if (treeState.token) {{
    treeState.token.setAttribute("opacity", 0);
    treeState.token.setAttribute("transform", "translate(0,0)");
  }}
  if (treeState.pkLink) {{
    treeState.pkLink.setAttribute("opacity", 0);
  }}
  if (treeState.pkLinkLabel) {{
    treeState.pkLinkLabel.setAttribute("opacity", 0);
  }}
}}

// Label a node that's on the current lookup path. ``descriptor`` is a
// short text like "leaf: PK = 42" that replaces the generic "leaf" label
// while the animation is highlighting that node.
function labelLeafAs(node, descriptor) {{
  if (!node || !node.label) return;
  node.label.textContent = descriptor;
  node.label.setAttribute("font-size", 8);
  node.label.setAttribute("font-weight", 700);
  node.label.setAttribute("fill", "#0f172a");
}}

// ---------- timeline ----------
function buildDescentTimeline() {{
  resetTreeColors();
  var tl = anim.timeline();
  var token = treeState.token;
  var levelsShown = Math.min(treeState.height, 4);
  var phaseLabel = document.getElementById("phase-label");

  // Walk one tree (descend root → leaf, pulsing each node). Step offset
  // allows the token to start at the root position without a fade-in
  // when it's arriving over the PK link.
  function addTreeDescent(treeIdx, fadeInFromAbove) {{
    tl.call(function() {{
      var rootNode = treeState.nodes[treeIdx][0].find(function(n) {{ return n.onPath; }});
      if (fadeInFromAbove && rootNode) {{
        token.setAttribute("transform", "translate(" + rootNode.cx + "," + (rootNode.cy - 40) + ")");
        token.setAttribute("opacity", 0);
      }}
      phaseLabel.textContent = (treeState.traversals === 2
        ? (treeIdx === 0 ? "Walking idx_users_email (secondary) for WHERE email = …"
                         : "Walking users PK (clustered) using the PK from the secondary leaf")
        : "Walking users PK (clustered)");
    }});
    if (fadeInFromAbove) {{
      tl.add({{
        from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
        onUpdate: function(v) {{ token.setAttribute("opacity", v); }}
      }});
    }}
    for (var lv = 0; lv < levelsShown; lv++) {{
      (function(levelIdx) {{
        var activeNode = treeState.nodes[treeIdx][levelIdx].find(function(n) {{ return n.onPath; }});
        if (!activeNode) return;
        var isLeaf = (levelIdx === levelsShown - 1);
        tl.call(function() {{
          phaseLabel.textContent = "Tree " + (treeIdx + 1) + "/" + treeState.traversals +
            " — descending to " + (levelIdx === 0 ? "root" : (isLeaf ? "leaf" : "level " + levelIdx));
        }});
        var currentPos;
        if (levelIdx === 0) {{
          currentPos = {{ x: activeNode.cx, y: activeNode.cy - 40 }};
        }} else {{
          var prev = treeState.nodes[treeIdx][levelIdx - 1].find(function(n) {{ return n.onPath; }});
          currentPos = {{ x: prev.cx, y: prev.cy }};
        }}
        // Starting-position fudge: if the first level is being entered
        // over the PK link, the token's current position may have been
        // set by the link-traversal step, so skip the re-jump.
        var skipStart = (levelIdx === 0 && !fadeInFromAbove);
        tl.add({{
          from: skipStart ? {{ x: activeNode.cx, y: activeNode.cy }} : currentPos,
          to: {{ x: activeNode.cx, y: activeNode.cy }},
          duration: skipStart ? 160 : 480,
          ease: anim.easeInOutCubic,
          onUpdate: function(p) {{
            token.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
          }},
          onComplete: function() {{
            activeNode.rect.setAttribute("fill", "#fde725");
            activeNode.rect.setAttribute("stroke", "#ca8a04");
            anim.pulse(activeNode.rect, 3, 1, 360);
            // Relabel the leaf node on arrival so the user sees what
            // the leaf actually contains.
            if (isLeaf && treeState.traversals === 2) {{
              if (treeIdx === 0) {{
                labelLeafAs(activeNode, "leaf: PK = 42");
              }} else {{
                labelLeafAs(activeNode, "leaf: full row");
              }}
            }} else if (isLeaf && treeState.traversals === 1) {{
              labelLeafAs(activeNode, "leaf: full row");
            }}
          }}
        }});
        tl.delay(160);
      }})(lv);
    }}
  }}

  if (treeState.traversals === 1) {{
    addTreeDescent(0, true);
  }} else {{
    // Secondary descent
    addTreeDescent(0, true);
    // PK-hop: reveal the dashed arrow, then animate the token along it
    // from the secondary leaf to the clustered leaf.
    var secLeaf = treeState.nodes[0][levelsShown - 1].find(function(n) {{ return n.onPath; }});
    var clusLeaf = treeState.nodes[1][levelsShown - 1].find(function(n) {{ return n.onPath; }});
    if (secLeaf && clusLeaf) {{
      tl.delay(240);
      tl.call(function() {{
        phaseLabel.textContent = "Secondary leaf has the PK — following the pointer to the clustered leaf";
      }});
      tl.add({{
        from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
        onUpdate: function(v) {{
          if (treeState.pkLink) treeState.pkLink.setAttribute("opacity", v);
          if (treeState.pkLinkLabel) treeState.pkLinkLabel.setAttribute("opacity", v);
        }}
      }});
      // Animate the token along the same quadratic-Bezier path used by
      // the PK link so the motion visibly rides the arrow.
      var sx = secLeaf.cx + 22;
      var sy = secLeaf.cy;
      var ex = clusLeaf.cx;
      var ey = clusLeaf.cy;
      var cx = (sx + ex) / 2;
      var cy = Math.min(sy, ey) - 50;
      var pathFn = anim.path(sx, sy, cx, cy, ex, ey);
      tl.add({{
        from: 0, to: 1, duration: 820, ease: anim.easeInOutCubic,
        onUpdate: function(t) {{
          var p = pathFn(t);
          token.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
        }},
        onComplete: function() {{
          // Light up the clustered leaf and label it, because the
          // PK pointer lands directly on it.
          clusLeaf.rect.setAttribute("fill", "#fde725");
          clusLeaf.rect.setAttribute("stroke", "#ca8a04");
          anim.pulse(clusLeaf.rect, 3, 1, 360);
          labelLeafAs(clusLeaf, "leaf: full row");
        }}
      }});
      tl.delay(220);
    }}
    // Clustered descent — but now we highlight the remaining path from
    // root → ... → leaf for completeness even though the PK pointer took
    // us straight there. This shows that InnoDB actually walks the
    // clustered tree from the top (via the PK's B+tree index).
    tl.call(function() {{
      phaseLabel.textContent = "InnoDB still walks the clustered B+tree top-down using that PK";
    }});
    // Move the token back up to above the clustered root, then descend.
    var clusRoot = treeState.nodes[1][0].find(function(n) {{ return n.onPath; }});
    if (clusRoot) {{
      tl.add({{
        from: 1, to: 0, duration: 200, ease: anim.easeInCubic,
        onUpdate: function(v) {{ token.setAttribute("opacity", v); }}
      }});
      tl.call(function() {{
        token.setAttribute("transform", "translate(" + clusRoot.cx + "," + (clusRoot.cy - 40) + ")");
      }});
      tl.add({{
        from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
        onUpdate: function(v) {{ token.setAttribute("opacity", v); }}
      }});
    }}
    addTreeDescent(1, false);
  }}

  tl.call(function() {{
    phaseLabel.textContent = "✓ Lookup complete — press Reset to replay";
  }});
  return tl;
}}

function resetAnim() {{
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
    current: {{ x: currentRows }},
    xSlider: "rows",
    xSliderTransform: function(xVal) {{
      // ROW_SCALE = [10, 100, 1k, 10k, 100k, 1M, 10M, 100M, 1B]
      // Find the closest log index
      var logV = Math.log10(Math.max(1, xVal));
      return Math.max(0, Math.min(ROW_SCALE.length - 1, Math.round(logV - 1)));
    }}
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

  buildTrees(cost.height, cost.traversals);
  resetTreeColors();
  renderChart(keySize, pageSize, keyType, rows);
}}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({{
  build: buildDescentTimeline,
  reset: resetAnim
}});
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
