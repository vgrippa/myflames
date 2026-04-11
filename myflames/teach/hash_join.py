"""Lesson: Hash join with grace-hash spill (MySQL 8.4).

Real-world join: `employees × departments`. Build side is the small
table (departments), probe side is the big table (employees). Shared
toolbar, explainer, query card, and complexity chart included.
"""
from . import _html
from ._cost_model import JOIN_BUFFER_SIZE_DEFAULT


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Parameters (MySQL 8.4 hash join: employees ⋈ departments)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="build_rows"><code>departments</code> rows (build side): <span class="value-pill" data-pill-for="build_rows">1000</span></label>
      <input type="range" id="build_rows" name="build_rows" min="100" max="5000000" step="100" value="1000">
      <div class="hint">Smaller input → MySQL picks this side for the hash table.</div>
    </div>

    <div class="control">
      <label for="probe_rows"><code>employees</code> rows (probe side): <span class="value-pill" data-pill-for="probe_rows">100000</span></label>
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

    query_card_html = _html.query_card(
        sql=(
            "-- Non-indexed equi-join executed by MySQL 8.4 hash join\n"
            "SELECT e.name, d.name AS department\n"
            "FROM   employees  e\n"
            "JOIN   departments d  ON  e.dept_id = d.id\n"
            "WHERE  e.active = 1;   -- d.id has no usable index → hash join"
        ),
        note="MySQL picks the smaller input (departments) as the build side. The larger input (employees) is streamed through in one pass."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Phase 1 — build: orange circles fly from departments (left) into specific hash buckets in the middle. Each row's join-key is hashed, and the row drops into the bucket at index hash(key) % num_buckets.",
            "The bucket fills up (colour turns yellow) when it receives a row. After 6 rows the whole hash table is built.",
            "Phase 2 — probe: teal circles fly from employees (right) toward their matching bucket. Each probe row hashes into exactly one bucket and checks for matches.",
            "When a probe row arrives, the bucket it landed in pulses — that's the join match being emitted to the client.",
            "Total work: one pass through the build side + one pass through the probe side = O(build + probe). If the hash table doesn't fit in join_buffer_size, a red 'spill' banner appears and MySQL falls back to grace-hash partitioning (both inputs written to disk, then re-read).",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <svg id="hash-svg" viewBox="0 0 800 360" xmlns="http://www.w3.org/2000/svg"></svg>
</section>
"""

    readout_html = """
<section class="readout">
  <h2>Cost readout (MySQL 8.4 hash join)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Build-side memory (departments)</p><p class="value" id="out-build">—</p></div>
    <div class="item"><p class="label">Fits in join_buffer_size?</p><p class="value" id="out-fits">—</p></div>
    <div class="item"><p class="label">Spilled to disk?</p><p class="value" id="out-spilled">—</p></div>
    <div class="item"><p class="label">Partitions</p><p class="value" id="out-parts">—</p></div>
    <div class="item"><p class="label">Phases</p><p class="value" id="out-phases">—</p></div>
    <div class="item"><p class="label">Complexity</p><p class="value" id="out-complexity">O(n + m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Row comparisons vs probe size (log–log, build side fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
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
    constant.</p>

    <p>Source: MySQL 8.4 Reference Manual §10.2.1.4 "Hash Join
    Optimization". Build-side selection heuristic (smaller input wins) is
    in the same section.</p>
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
  var cmp = buildRows + probeRows + (spilled ? (buildRows + probeRows) : 0);
  return {{
    buildBytes: buildBytes, fits: fits, spilled: spilled,
    partitions: partitions, phases: phases, cmp: cmp
  }};
}}

var W = 800, H = 360;
var NUM_BUCKETS = 6;
var stage = null;

function buildStage() {{
  var svg = document.getElementById("hash-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var buildRect = anim.svgEl("rect", {{
    x: 20, y: 90, width: 130, height: 140, rx: 8, ry: 8,
    fill: "#fef3c7", stroke: "#d97706", "stroke-width": 1.5
  }});
  svg.appendChild(buildRect);
  var buildLbl = anim.svgEl("text", {{
    x: 85, y: 82, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#92400e"
  }});
  buildLbl.textContent = "departments (build, small)";
  svg.appendChild(buildLbl);
  for (var b = 0; b < 6; b++) {{
    var dot = anim.svgEl("circle", {{
      cx: 85, cy: 110 + b * 18, r: 4, fill: "#d97706"
    }});
    svg.appendChild(dot);
  }}

  var htX = 320, htY = 60, htW = 160, htH = 200;
  var htRect = anim.svgEl("rect", {{
    x: htX, y: htY, width: htW, height: htH, rx: 10, ry: 10,
    fill: "#f3f4f6", stroke: "#6b7280", "stroke-width": 1.5
  }});
  svg.appendChild(htRect);
  var htLbl = anim.svgEl("text", {{
    x: htX + htW/2, y: htY - 10, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#1f2937"
  }});
  htLbl.textContent = "Hash table (" + NUM_BUCKETS + " buckets)";
  svg.appendChild(htLbl);

  var bucketRects = [];
  var bucketY0 = htY + 14;
  var bucketSpacing = (htH - 28) / NUM_BUCKETS;
  for (var i = 0; i < NUM_BUCKETS; i++) {{
    var by = bucketY0 + i * bucketSpacing;
    var br = anim.svgEl("rect", {{
      x: htX + 14, y: by, width: htW - 28, height: bucketSpacing - 8, rx: 4, ry: 4,
      fill: "#ffffff", stroke: "#d1d5db", "stroke-width": 1
    }});
    svg.appendChild(br);
    var bl = anim.svgEl("text", {{
      x: htX + htW/2, y: by + (bucketSpacing - 8)/2 + 4, "text-anchor": "middle",
      "font-size": 10, fill: "#6b7280"
    }});
    bl.textContent = "bucket " + (i + 1);
    svg.appendChild(bl);
    bucketRects.push({{
      rect: br, label: bl,
      cx: htX + htW/2, cy: by + (bucketSpacing - 8)/2
    }});
  }}

  var probeRect = anim.svgEl("rect", {{
    x: W - 150, y: 90, width: 130, height: 140, rx: 8, ry: 8,
    fill: "#ccfbf1", stroke: "#0d9488", "stroke-width": 1.5
  }});
  svg.appendChild(probeRect);
  var probeLbl = anim.svgEl("text", {{
    x: W - 85, y: 82, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#115e59"
  }});
  probeLbl.textContent = "employees (probe, large)";
  svg.appendChild(probeLbl);
  for (var p = 0; p < 6; p++) {{
    var pdot = anim.svgEl("circle", {{
      cx: W - 85, cy: 110 + p * 18, r: 4, fill: "#0d9488"
    }});
    svg.appendChild(pdot);
  }}

  var statusLbl = anim.svgEl("text", {{
    x: W/2, y: 300, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  }});
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  var spillLbl = anim.svgEl("text", {{
    x: W/2, y: 325, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "#991b1b", opacity: 0
  }});
  svg.appendChild(spillLbl);

  stage = {{
    svg: svg, buckets: bucketRects,
    buildCx: 85, buildCy: 160, probeCx: W - 85, probeCy: 160,
    statusLbl: statusLbl, spillLbl: spillLbl, tuples: []
  }};
}}

function resetStage() {{
  if (!stage) return;
  stage.buckets.forEach(function(b) {{
    b.rect.setAttribute("fill", "#ffffff");
    b.rect.setAttribute("stroke", "#d1d5db");
    b.rect.setAttribute("stroke-width", 1);
  }});
  stage.statusLbl.textContent = "";
  stage.spillLbl.setAttribute("opacity", 0);
  stage.tuples.forEach(function(t) {{ if (t.parentNode) t.parentNode.removeChild(t); }});
  stage.tuples = [];
}}

function spawnTuple(color, cx, cy) {{
  var t = anim.svgEl("circle", {{
    cx: cx, cy: cy, r: 6, fill: color, stroke: "#1f2937",
    "stroke-width": 1, opacity: 0
  }});
  stage.svg.appendChild(t);
  stage.tuples.push(t);
  return t;
}}

function flyToBucket(tuple, targetBucket, color) {{
  var fromX = parseFloat(tuple.getAttribute("cx"));
  var fromY = parseFloat(tuple.getAttribute("cy"));
  var toX = targetBucket.cx;
  var toY = targetBucket.cy;
  var cx = (fromX + toX) / 2;
  var cy = Math.min(fromY, toY) - 30;
  var pathFn = anim.path(fromX, fromY, cx, cy, toX, toY);

  anim.tween({{
    from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
    onUpdate: function(v) {{ tuple.setAttribute("opacity", v); }}
  }});
  anim.tween({{
    from: 0, to: 1, duration: 680, ease: anim.easeInOutQuad,
    onUpdate: function(t) {{
      var p = pathFn(t);
      tuple.setAttribute("cx", p.x);
      tuple.setAttribute("cy", p.y);
    }},
    onComplete: function() {{
      targetBucket.rect.setAttribute("fill", color);
      targetBucket.rect.setAttribute("stroke", "#1f2937");
      anim.pulse(targetBucket.rect, 2.5, 1, 320);
      anim.tween({{
        from: 1, to: 0, duration: 220, ease: anim.easeInCubic,
        onUpdate: function(v) {{ tuple.setAttribute("opacity", v); }}
      }});
    }}
  }});
}}

function buildTimeline(spilled, partitions) {{
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var BUILD_TUPLES = 6;
  var PROBE_TUPLES = 8;

  tl.call(function() {{
    phase.textContent = "Phase 1/2 — building hash table from departments";
    stage.statusLbl.textContent = "Hashing " + BUILD_TUPLES + " rows into " + NUM_BUCKETS + " buckets";
  }});
  for (var i = 0; i < BUILD_TUPLES; i++) {{
    (function(idx) {{
      tl.call(function() {{
        var bucket = stage.buckets[idx % NUM_BUCKETS];
        var tuple = spawnTuple("#d97706", stage.buildCx, stage.buildCy);
        flyToBucket(tuple, bucket, "#fde725");
      }});
      tl.delay(140);
    }})(i);
  }}
  tl.delay(600);

  tl.call(function() {{
    phase.textContent = "Phase 2/2 — streaming employees through the hash table";
    stage.statusLbl.textContent = "Probing: each row hashes into exactly one bucket";
  }});
  for (var j = 0; j < PROBE_TUPLES; j++) {{
    (function(jdx) {{
      tl.call(function() {{
        var bucket = stage.buckets[jdx % NUM_BUCKETS];
        var tuple = spawnTuple("#0d9488", stage.probeCx, stage.probeCy);
        flyToBucket(tuple, bucket, "#10b981");
      }});
      tl.delay(130);
    }})(j);
  }}
  tl.delay(500);

  if (spilled) {{
    tl.call(function() {{
      phase.textContent = "⚠ Spill! departments was too big for join_buffer_size";
      stage.spillLbl.textContent = "Partitioned into " + partitions + " chunks on disk — each partition re-built and re-probed (total I/O ≈ 2×)";
    }});
    tl.add({{
      from: 0, to: 1, duration: 400, ease: anim.easeOutCubic,
      onUpdate: function(v) {{ stage.spillLbl.setAttribute("opacity", v); }}
    }});
  }} else {{
    tl.call(function() {{
      phase.textContent = "✓ Complete — single in-memory pass, O(build + probe)";
    }});
  }}
  return tl;
}}

function buildCurrentTimeline() {{
  var c = teachRuntime.readControls();
  var cost = hashJoinCost(c.build_rows, c.probe_rows, c.row_size, c.jbs);
  return buildTimeline(cost.spilled, cost.partitions);
}}
function resetAnim() {{
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}}

function renderChart(buildRows, rowSize, jbs, currentProbe) {{
  anim.complexityChart({{
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 1000, xMax: 1e8,
    xLabel: "employees row count", yLabel: "Row comparisons",
    curves: [
      {{ label: "Hash join (this algorithm)", color: "#0d9488",
        fn: function(n) {{ return hashJoinCost(buildRows, n, rowSize, jbs).cmp; }} }},
      {{ label: "BNL baseline (for contrast)", color: "#ca8a04",
        fn: function(n) {{
          var rpb = Math.max(1, Math.floor(jbs / rowSize));
          var blocks = Math.max(1, Math.ceil(buildRows / rpb));
          return blocks * n * Math.min(rpb, buildRows);
        }} }}
    ],
    current: {{ x: currentProbe }},
    xSlider: "probe_rows",
    xSliderTransform: function(xVal) {{ return Math.max(1000, Math.round(xVal / 1000) * 1000); }}
  }});
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
  document.getElementById("out-explanation").textContent = cost.spilled
    ? "departments is " + teachRuntime.formatBytes(cost.buildBytes) + " — bigger than join_buffer_size. MySQL spills: partition both inputs into " + cost.partitions + " chunks on disk, then probe each partition separately. Cost roughly doubles."
    : "departments is " + teachRuntime.formatBytes(cost.buildBytes) + " — fits in join_buffer_size. Single-pass: build the hash table once, stream employees through. O(build + probe).";
  resetStage();
  renderChart(c.build_rows, c.row_size, c.jbs, c.probe_rows);
}}

buildStage();
teachRuntime.wire(recompute);
teachRuntime.wireToolbar({{
  build: buildCurrentTimeline,
  reset: resetAnim
}});
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
