"""Lesson: Block Nested Loop join (MariaDB 11.x default).

Real-world join: `customers × orders`. Polished animation with curved
tuple paths, shared toolbar, query card, explainer, and complexity
chart. Labels clearly say "row-pair comparisons", not the confusing
"rows examined" bare number.
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
      <label for="outer_rows"><code>customers</code> rows: <span class="value-pill" data-pill-for="outer_rows">10000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Rows from the outer (driving) table.</div>
    </div>

    <div class="control">
      <label for="inner_rows"><code>orders</code> rows: <span class="value-pill" data-pill-for="inner_rows">50000</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="100" max="10000000" step="100" value="50000">
      <div class="hint">Rows in the inner table. Re-scanned once per outer block.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of one <code>customers</code> row in the join buffer.</div>
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="8192" max="16777216" step="8192" value="{JOIN_BUFFER_SIZE_DEFAULT}">
      <div class="hint">MariaDB 11.4 default is {JOIN_BUFFER_SIZE_DEFAULT} B (256 KiB).</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Non-indexed join executed by MariaDB 11.x BNL\n"
            "SELECT c.country, SUM(o.total) AS revenue\n"
            "FROM   customers c\n"
            "JOIN   orders    o  ON  c.signup_month = o.signup_month\n"
            "GROUP  BY c.country;   -- no index on signup_month → BNL"
        ),
        note="BNL kicks in whenever the ON clause has no usable index on the inner side."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Yellow blocks on top = chunks of customers rows packed into the join buffer (one block holds as many rows as join_buffer_size allows).",
            "Blue table below = the orders table. It is a full table — every row lives here.",
            "Small yellow circles flow from the active block along a curved path down to the orders table. They represent the outer rows being compared against the inner.",
            "A yellow sweep bar moves left-to-right across the orders table. That sweep is one full scan of orders.",
            "Each block triggers exactly one full scan of orders. 10 blocks = 10 full scans. That is why bigger join_buffer_size → fewer blocks → less work.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <svg id="bnl-svg" viewBox="0 0 800 360" xmlns="http://www.w3.org/2000/svg"></svg>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Cost readout (MariaDB 11.x BNL)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">customers per block</p><p class="value" id="out-rpb">—</p></div>
    <div class="item"><p class="label">Blocks</p><p class="value" id="out-blocks">—</p></div>
    <div class="item"><p class="label">Inner re-scans of orders</p><p class="value" id="out-scans">—</p></div>
    <div class="item"><p class="label">Row-pair comparisons</p><p class="value" id="out-cmp">—</p></div>
    <div class="item"><p class="label">Complexity</p><p class="value" id="out-complexity">O(customers · orders / buffer)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Row-pair comparisons vs customer rows (log–log, orders fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
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

var W = 800, H = 360;
var stage = null;

function buildStage(numBlocks) {{
  var svg = document.getElementById("bnl-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var headerOuter = anim.svgEl("text", {{
    x: 20, y: 28, "font-size": 13, "font-weight": 700, fill: "#92400e"
  }});
  headerOuter.textContent = "customers → join_buffer (" + numBlocks + " block" + (numBlocks === 1 ? "" : "s") + ")";
  svg.appendChild(headerOuter);

  var cap = Math.min(numBlocks, 10);
  var gap = 8;
  var startX = 20;
  var avail = W - 40;
  var bw = (avail - gap * (cap - 1)) / cap;
  var blockRects = [];
  for (var i = 0; i < cap; i++) {{
    var x = startX + i * (bw + gap);
    var rect = anim.svgEl("rect", {{
      x: x, y: 42, width: bw, height: 48, rx: 6, ry: 6,
      fill: "#fffbeb", stroke: "#d1d5db", "stroke-width": 1.5
    }});
    svg.appendChild(rect);
    var lbl = anim.svgEl("text", {{
      x: x + bw/2, y: 72, "text-anchor": "middle",
      "font-size": 12, "font-weight": 600, fill: "#92400e"
    }});
    lbl.textContent = "Block " + (i + 1);
    svg.appendChild(lbl);
    blockRects.push({{ rect: rect, label: lbl, cx: x + bw/2, cy: 66 }});
  }}
  if (numBlocks > cap) {{
    var more = anim.svgEl("text", {{
      x: W - 20, y: 72, "text-anchor": "end",
      "font-size": 11, "font-weight": 600, fill: "#9ca3af", "font-style": "italic"
    }});
    more.textContent = "… and " + (numBlocks - cap) + " more blocks (each one is another full scan of orders)";
    svg.appendChild(more);
  }}

  var innerY = 200;
  var innerH = 90;
  var innerX = 60;
  var innerW = W - 120;
  var innerLbl = anim.svgEl("text", {{
    x: innerX, y: innerY - 8, "font-size": 13, "font-weight": 700, fill: "#0c4a6e"
  }});
  innerLbl.textContent = "orders — re-scanned once per customers block";
  svg.appendChild(innerLbl);

  var innerRect = anim.svgEl("rect", {{
    x: innerX, y: innerY, width: innerW, height: innerH, rx: 8, ry: 8,
    fill: "#f0f9ff", stroke: "#0284c7", "stroke-width": 1.5
  }});
  svg.appendChild(innerRect);

  for (var j = 0; j < 8; j++) {{
    var sy = innerY + 10 + j * 10;
    var stripe = anim.svgEl("line", {{
      x1: innerX + 12, y1: sy, x2: innerX + innerW - 12, y2: sy,
      stroke: "#dbeafe", "stroke-width": 1
    }});
    svg.appendChild(stripe);
  }}

  var sweep = anim.svgEl("rect", {{
    x: innerX, y: innerY, width: 4, height: innerH,
    fill: "#ca8a04", opacity: 0
  }});
  svg.appendChild(sweep);

  var statusLbl = anim.svgEl("text", {{
    x: W/2, y: innerY + innerH + 30, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  }});
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {{
    svg: svg, blocks: blockRects, totalBlocks: numBlocks, sweep: sweep,
    innerX: innerX, innerY: innerY, innerW: innerW, innerH: innerH,
    statusLbl: statusLbl, tuples: []
  }};
}}

function resetStage() {{
  if (!stage) return;
  stage.blocks.forEach(function(b) {{
    b.rect.setAttribute("fill", "#fffbeb");
    b.rect.setAttribute("stroke", "#d1d5db");
    b.rect.setAttribute("stroke-width", 1.5);
  }});
  stage.sweep.setAttribute("opacity", 0);
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) {{ if (t.parentNode) t.parentNode.removeChild(t); }});
  stage.tuples = [];
}}

var currentTimeline = null;

function buildTimeline(totalBlocksReal) {{
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var shown = stage.blocks.length;
  var TUPLES_PER_BLOCK = 5;

  for (var i = 0; i < shown; i++) {{
    (function(idx) {{
      var block = stage.blocks[idx];
      tl.call(function() {{
        block.rect.setAttribute("fill", "#fde725");
        block.rect.setAttribute("stroke", "#ca8a04");
        block.rect.setAttribute("stroke-width", 2.5);
        phase.textContent = "Scanning orders for customers block " + (idx + 1) + "/" + totalBlocksReal;
      }});
      tl.call(function() {{
        for (var k = 0; k < TUPLES_PER_BLOCK; k++) {{
          (function(kk) {{
            var tuple = anim.svgEl("circle", {{
              cx: block.cx, cy: block.cy + 4, r: 5,
              fill: "#ca8a04", stroke: "#78350f", "stroke-width": 1, opacity: 0
            }});
            stage.svg.appendChild(tuple);
            stage.tuples.push(tuple);
            var targetX = stage.innerX + 40 + (kk * (stage.innerW - 80) / TUPLES_PER_BLOCK);
            var targetY = stage.innerY + 20;
            var pathFn = anim.path(
              block.cx, block.cy + 4,
              (block.cx + targetX) / 2, (block.cy + targetY) / 2 - 40,
              targetX, targetY
            );
            setTimeout(function() {{
              anim.tween({{
                from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
                onUpdate: function(v) {{ tuple.setAttribute("opacity", v); }}
              }});
              anim.tween({{
                from: 0, to: 1, duration: 640, ease: anim.easeInOutQuad,
                onUpdate: function(t) {{
                  var p = pathFn(t);
                  tuple.setAttribute("cx", p.x);
                  tuple.setAttribute("cy", p.y);
                }},
                onComplete: function() {{
                  anim.tween({{
                    from: 1, to: 0, duration: 240, ease: anim.easeInCubic,
                    onUpdate: function(v) {{ tuple.setAttribute("opacity", v); }}
                  }});
                }}
              }});
            }}, kk * 80);
          }})(k);
        }}
      }});
      tl.add({{
        from: stage.innerX, to: stage.innerX + stage.innerW,
        duration: 900, ease: anim.easeInOutCubic,
        onUpdate: function(x) {{
          stage.sweep.setAttribute("x", x);
          stage.sweep.setAttribute("opacity", 0.85);
        }},
        onComplete: function() {{
          stage.sweep.setAttribute("opacity", 0);
          block.rect.setAttribute("fill", "#e5e7eb");
          block.rect.setAttribute("stroke", "#9ca3af");
          block.rect.setAttribute("stroke-width", 1);
        }}
      }});
      tl.delay(160);
    }})(i);
  }}
  tl.call(function() {{
    phase.textContent = "✓ All " + totalBlocksReal + " block(s) done — " + totalBlocksReal + " full scans of orders";
    if (totalBlocksReal > shown) {{
      stage.statusLbl.textContent = "…plus " + (totalBlocksReal - shown) + " more blocks not drawn above, same pattern.";
    }}
    teachRuntime.animationDone();
  }});
  return tl;
}}

function playAnim() {{
  if (currentTimeline) currentTimeline.stop();
  var c = teachRuntime.readControls();
  var cost = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  currentTimeline = buildTimeline(cost.blocks);
  currentTimeline.play();
}}
function resetAnim() {{
  if (currentTimeline) currentTimeline.stop();
  currentTimeline = null;
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}}

function renderChart(innerRows, rowSize, jbs, currentOuter) {{
  anim.complexityChart({{
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "customers row count", yLabel: "Row-pair comparisons",
    curves: [
      {{ label: "BNL (this algorithm)", color: "#ca8a04",
        fn: function(n) {{ return bnlCost(n, innerRows, rowSize, jbs).cmp; }} }},
      {{ label: "Ideal indexed join (O(n+m))", color: "#0d9488",
        fn: function(n) {{ return n + innerRows; }} }}
    ],
    current: {{ x: currentOuter }}
  }});
}}

function recompute() {{
  var c = teachRuntime.readControls();
  var cost = bnlCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs);
  document.getElementById("out-rpb").textContent = teachRuntime.formatInt(cost.rpb);
  document.getElementById("out-blocks").textContent = teachRuntime.formatInt(cost.blocks);
  document.getElementById("out-scans").textContent = teachRuntime.formatInt(cost.innerScans);
  document.getElementById("out-cmp").textContent = teachRuntime.formatInt(cost.cmp);
  document.getElementById("out-explanation").textContent =
    "customers rows pack into " + cost.blocks + " block(s) of up to " + cost.rpb +
    " rows. orders is fully re-scanned once per block — " +
    cost.innerScans + " scan(s). Raise join_buffer_size → fewer blocks → fewer rescans.";
  if (currentTimeline) {{ currentTimeline.stop(); currentTimeline = null; }}
  buildStage(cost.blocks);
  resetStage();
  renderChart(c.inner_rows, c.row_size, c.jbs, c.outer_rows);
}}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar(playAnim, resetAnim);
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
