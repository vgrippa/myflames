var JOIN_BUFFER_SIZE_DEFAULT = %d;

function hashJoinCost(buildRows, probeRows, rowSize, jbs) {
  var buildBytes = Math.floor(buildRows * rowSize * 1.4);
  var fits = buildBytes <= jbs;
  var spilled = !fits;
  var partitions = spilled ? Math.max(2, Math.ceil(buildBytes / jbs)) : 1;
  var phases = spilled ? 4 : 2;
  var cmp = buildRows + probeRows + (spilled ? (buildRows + probeRows) : 0);
  return {
    buildBytes: buildBytes, fits: fits, spilled: spilled,
    partitions: partitions, phases: phases, cmp: cmp
  };
}

var W = 800, H = 380;
var NUM_BUCKETS = 6;
var stage = null;

// ---- Sample data the user can follow through the animation ----
var DEPTS = [
  { id: 1, name: "Sales" },
  { id: 3, name: "Eng" },
  { id: 5, name: "Mktg" },
  { id: 7, name: "Ops" },
  { id: 9, name: "HR" },
  { id: 11, name: "Finance" }
];
var EMPLOYEES = [
  { name: "Alice", dept: 3 },
  { name: "Bob", dept: 7 },
  { name: "Carol", dept: 1 },
  { name: "Dave", dept: 3 },
  { name: "Eve", dept: 9 },
  { name: "Frank", dept: 5 },
  { name: "Grace", dept: 11 },
  { name: "Hank", dept: 7 }
];

function deptBucket(deptId) { return deptId %% NUM_BUCKETS; }

function buildStage() {
  var svg = document.getElementById("hash-svg");
  svg.setAttribute("viewBox", "0 0 800 380");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // Build input (left) — actual department rows
  var buildRect = anim.svgEl("rect", {
    x: 12, y: 44, width: 140, height: DEPTS.length * 22 + 10, rx: 8, ry: 8,
    fill: "var(--svg-build-soft)", stroke: "var(--svg-build)", "stroke-width": 1.5
  });
  svg.appendChild(buildRect);
  var buildLbl = anim.svgEl("text", {
    x: 82, y: 36, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  buildLbl.textContent = "departments (build side)";
  svg.appendChild(buildLbl);
  var buildRows = [];
  for (var b = 0; b < DEPTS.length; b++) {
    var by = 54 + b * 22;
    var rowLbl = anim.svgEl("text", {
      x: 82, y: by + 14, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "var(--svg-build-ink)"
    });
    rowLbl.textContent = "id=" + DEPTS[b].id + " " + DEPTS[b].name;
    svg.appendChild(rowLbl);
    buildRows.push({ lbl: rowLbl, cx: 82, cy: by + 8 });
  }

  // Hash table (middle)
  var htX = 280, htY = 24, htW = 210, htH = NUM_BUCKETS * 46 + 20;
  var htRect = anim.svgEl("rect", {
    x: htX, y: htY, width: htW, height: htH, rx: 10, ry: 10,
    fill: "var(--svg-node-muted)", stroke: "var(--svg-node-stroke)", "stroke-width": 1.5
  });
  svg.appendChild(htRect);
  var htLbl = anim.svgEl("text", {
    x: htX + htW/2, y: htY - 8, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "var(--svg-ink)"
  });
  htLbl.textContent = "Hash table — hash(dept.id) %% " + NUM_BUCKETS;
  svg.appendChild(htLbl);

  var bucketRects = [];
  var bucketY0 = htY + 12;
  var bucketH = 36;
  var bucketGap = 10;
  for (var i = 0; i < NUM_BUCKETS; i++) {
    var byB = bucketY0 + i * (bucketH + bucketGap);
    var br = anim.svgEl("rect", {
      x: htX + 10, y: byB, width: htW - 20, height: bucketH, rx: 4, ry: 4,
      fill: "var(--svg-node-bg)", stroke: "var(--svg-node-stroke)", "stroke-width": 1
    });
    svg.appendChild(br);
    var bIdx = anim.svgEl("text", {
      x: htX + 16, y: byB + 14, "font-size": 9, "font-weight": 700, fill: "var(--svg-ink-muted)"
    });
    bIdx.textContent = "[" + i + "]";
    svg.appendChild(bIdx);
    var bContent = anim.svgEl("text", {
      x: htX + htW/2, y: byB + 22, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "var(--svg-ink-muted)"
    });
    bContent.textContent = "(empty)";
    svg.appendChild(bContent);
    var matchLbl = anim.svgEl("text", {
      x: htX + htW - 16, y: byB + 14, "text-anchor": "end",
      "font-size": 10, "font-weight": 700, fill: "var(--svg-ok)", opacity: 0
    });
    matchLbl.textContent = "MATCH \u2713";
    svg.appendChild(matchLbl);
    bucketRects.push({
      rect: br, indexLbl: bIdx, contentLbl: bContent, matchLbl: matchLbl,
      cx: htX + htW/2, cy: byB + bucketH/2,
      contents: []
    });
  }

  // Probe input (right) — named employees
  var probeRect = anim.svgEl("rect", {
    x: W - 170, y: 44, width: 156, height: EMPLOYEES.length * 22 + 10, rx: 8, ry: 8,
    fill: "var(--svg-probe-soft)", stroke: "var(--svg-probe)", "stroke-width": 1.5
  });
  svg.appendChild(probeRect);
  var probeLbl = anim.svgEl("text", {
    x: W - 92, y: 36, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "var(--svg-probe)"
  });
  probeLbl.textContent = "employees (probe side)";
  svg.appendChild(probeLbl);
  var probeRows = [];
  for (var p = 0; p < EMPLOYEES.length; p++) {
    var py = 54 + p * 22;
    var pLbl = anim.svgEl("text", {
      x: W - 92, y: py + 14, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "var(--svg-probe)"
    });
    pLbl.textContent = EMPLOYEES[p].name + " dept=" + EMPLOYEES[p].dept;
    svg.appendChild(pLbl);
    probeRows.push({ lbl: pLbl, cx: W - 92, cy: py + 8 });
  }

  var statusLbl = anim.svgEl("text", {
    x: W/2, y: H - 32, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "var(--svg-ink)"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  var spillLbl = anim.svgEl("text", {
    x: W/2, y: H - 10, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "var(--svg-danger)", opacity: 0
  });
  svg.appendChild(spillLbl);

  stage = {
    svg: svg, buckets: bucketRects,
    buildRows: buildRows, probeRows: probeRows,
    statusLbl: statusLbl, spillLbl: spillLbl, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  stage.buckets.forEach(function(b) {
    b.rect.setAttribute("fill", "var(--svg-node-bg)");
    b.rect.setAttribute("stroke", "var(--svg-node-stroke)");
    b.rect.setAttribute("stroke-width", 1);
    b.contentLbl.textContent = "(empty)";
    b.contentLbl.setAttribute("fill", "var(--svg-ink-muted)");
    b.matchLbl.setAttribute("opacity", 0);
    b.contents = [];
  });
  stage.buildRows.forEach(function(r) { r.lbl.setAttribute("opacity", 1); });
  stage.probeRows.forEach(function(r) { r.lbl.setAttribute("opacity", 1); });
  stage.statusLbl.textContent = "";
  stage.spillLbl.setAttribute("opacity", 0);
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function spawnLabeledTuple(text, color, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var bg = anim.svgEl("rect", {
    x: -42, y: -9, width: 84, height: 18, rx: 9, ry: 9,
    fill: color, stroke: "var(--svg-ink)", "stroke-width": 1
  });
  g.appendChild(bg);
  var lbl = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "var(--svg-on-solid)"
  });
  lbl.textContent = text;
  g.appendChild(lbl);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

// One labelled pill arcs from its input row into a bucket, then fades into
// it. Built from timeline steps (not free-running tweens) so Next step, Back,
// Explore and the scrubber all show the same picture at every step.
function flyPill(tl, text, color, from, bucket, onLand) {
  var g = null;
  var x0 = from.cx, y0 = from.cy, x1 = bucket.cx, y1 = bucket.cy;
  var pathFn = anim.path(x0, y0, (x0 + x1) / 2, Math.min(y0, y1) - 30, x1, y1);
  tl.add({
    from: 0, to: 1, duration: 460, ease: anim.easeInOutQuad,
    onUpdate: function(t) {
      if (!g) g = spawnLabeledTuple(text, color, x0, y0);
      var p = pathFn(t);
      g.setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")");
      g.setAttribute("opacity", Math.min(1, t * 5).toFixed(2));
    },
    onComplete: function() { if (onLand) onLand(); }
  });
  tl.add({
    from: 1, to: 0, duration: 120, ease: anim.easeInCubic,
    onUpdate: function(v) { if (g) g.setAttribute("opacity", v.toFixed(2)); },
    onComplete: function() {
      if (g && g.parentNode) g.parentNode.removeChild(g);
      g = null;
    }
  });
}

// The green MATCH flash of a probe stays up while the step is held, and
// fades back at the start of the next step.
function clearMatch(tl, bucket) {
  tl.add({
    from: 0, to: 1, duration: 140, ease: anim.easeInCubic,
    onUpdate: function(t) {
      bucket.rect.setAttribute("fill", anim.lerpColor("var(--svg-ok-soft)", "var(--svg-build-soft)", t));
      bucket.rect.setAttribute("stroke", anim.lerpColor("var(--svg-ok)", "var(--svg-build)", t));
      bucket.rect.setAttribute("stroke-width", (2.5 - 1.5 * t).toFixed(2));
      bucket.matchLbl.setAttribute("opacity", (1 - t).toFixed(2));
    },
    onComplete: function() {
      bucket.rect.setAttribute("fill", "var(--svg-build-soft)");
      bucket.rect.setAttribute("stroke", "var(--svg-build)");
      bucket.rect.setAttribute("stroke-width", 1);
      bucket.matchLbl.setAttribute("opacity", 0);
    }
  });
}

// Departments in each bucket, in build order (the bucket labels after the
// build). Computed from the data so captions never depend on timing.
function bucketNames() {
  var names = [];
  for (var i = 0; i < NUM_BUCKETS; i++) names.push([]);
  for (var d = 0; d < DEPTS.length; d++) names[deptBucket(DEPTS[d].id)].push(DEPTS[d].name);
  return names;
}

function buildTimeline(spilled, partitions) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var names = bucketNames();
  var used = [];
  for (var u = 0; u < NUM_BUCKETS; u++) if (names[u].length) used.push("[" + u + "]");

  // ---- Phase 1: Build: one step per departments row ----
  tl.mark("Build: hash departments");
  for (var i = 0; i < DEPTS.length; i++) {
    (function(idx) {
      var dept = DEPTS[idx];
      var bucketIdx = deptBucket(dept.id);
      var bucket = stage.buckets[bucketIdx];
      // Guided steps: kinds map to the simplified code (GUIDE in hash.py).
      tl.beat("build-row");
      tl.call(function() {
        phase.textContent = "Build row " + (idx + 1) + " of " + DEPTS.length + ": departments id=" +
          dept.id + " “" + dept.name + "” → hash(" + dept.id + ") %% " + NUM_BUCKETS +
          " = bucket [" + bucketIdx + "]";
        stage.statusLbl.textContent = "Reading departments: id=" + dept.id + " " + dept.name +
          " goes into bucket [" + bucketIdx + "]";
        stage.buildRows[idx].lbl.setAttribute("opacity", 0.45);
      });
      flyPill(tl, "id=" + dept.id + " " + dept.name, "var(--svg-build)",
        stage.buildRows[idx], bucket,
        function() {
          bucket.contents.push(dept.name);
          bucket.contentLbl.textContent = bucket.contents.join(", ");
          bucket.contentLbl.setAttribute("fill", "var(--svg-ink)");
          bucket.rect.setAttribute("fill", "var(--svg-build-soft)");
          bucket.rect.setAttribute("stroke", "var(--svg-build)");
          anim.pulse(bucket.rect, 2.5, 1, 280);
        });
      tl.delay(60);
    })(i);
  }
  // Act boundary: what just happened and what comes next.
  tl.call(function() {
    phase.textContent = "Build done: all " + DEPTS.length + " departments rows are in memory, in buckets " +
      used.join(", ") + ". Next, each employee probes one bucket";
    stage.statusLbl.textContent = "For each employee: hash(dept_id) → one bucket → check the ids in it";
  });
  tl.delay(700);

  // ---- Phase 2: Probe: one step per employees row ----
  tl.mark("Probe: match employees");
  var lastBucket = null;
  for (var j = 0; j < EMPLOYEES.length; j++) {
    (function(jdx, prevBucket) {
      var emp = EMPLOYEES[jdx];
      var bucketIdx = deptBucket(emp.dept);
      var bucket = stage.buckets[bucketIdx];
      var matchDept = null;
      for (var d = 0; d < DEPTS.length; d++) {
        if (DEPTS[d].id === emp.dept) { matchDept = DEPTS[d]; break; }
      }
      var others = [];
      for (var o = 0; o < DEPTS.length; o++) {
        if (deptBucket(DEPTS[o].id) === bucketIdx && DEPTS[o].id !== emp.dept) {
          others.push(DEPTS[o].name + " (id=" + DEPTS[o].id + ")");
        }
      }
      tl.beat("probe-row");
      if (prevBucket) clearMatch(tl, prevBucket);
      tl.call(function() {
        phase.textContent = "Probe row " + (jdx + 1) + " of " + EMPLOYEES.length + ": " + emp.name +
          " (dept_id=" + emp.dept + ") → hash(" + emp.dept + ") %% " + NUM_BUCKETS +
          " = bucket [" + bucketIdx + "], which holds " +
          (names[bucketIdx].length ? names[bucketIdx].join(", ") : "nothing");
        stage.statusLbl.textContent = emp.name + " looks only in bucket [" + bucketIdx + "]";
        stage.probeRows[jdx].lbl.setAttribute("opacity", 0.45);
      });
      flyPill(tl, emp.name + " dept=" + emp.dept, "var(--svg-probe)",
        stage.probeRows[jdx], bucket,
        function() {
          if (!matchDept) {
            stage.statusLbl.textContent = "✗ " + emp.name + ": no department has id=" + emp.dept;
            return;
          }
          bucket.rect.setAttribute("fill", "var(--svg-ok-soft)");
          bucket.rect.setAttribute("stroke", "var(--svg-ok)");
          bucket.rect.setAttribute("stroke-width", 2.5);
          bucket.matchLbl.setAttribute("opacity", 1);
          stage.statusLbl.textContent = "✓ " + emp.name + " matches " + matchDept.name +
            " (id=" + emp.dept + ")" +
            (others.length ? "; " + others.join(", ") + " shares the bucket but not the id" : "");
        });
      tl.delay(280);
    })(j, lastBucket);
    lastBucket = stage.buckets[deptBucket(EMPLOYEES[j].dept)];
  }

  if (spilled) {
    tl.mark("Grace-hash spill");
    tl.beat("spill");
    clearMatch(tl, lastBucket);
    tl.call(function() {
      phase.textContent = "At your settings departments does not fit in join_buffer_size: MySQL joins what fits in " +
        "memory and writes the rest of both inputs to " + partitions + " pairs of chunk files on disk";
      stage.statusLbl.textContent = "Every employee was still probed once; the rows on disk are joined chunk by chunk";
      stage.spillLbl.textContent = "Spilled to " + partitions +
        " chunk files on disk, each pair built and probed again (I/O ≈ 2×)";
    });
    tl.add({
      from: 0, to: 1, duration: 400, ease: anim.easeOutCubic,
      onUpdate: function(v) { stage.spillLbl.setAttribute("opacity", v); }
    });
    tl.delay(600);
  } else {
    tl.mark("Done");
    tl.beat("done");
    clearMatch(tl, lastBucket);
    tl.call(function() {
      phase.textContent = "✓ Done: every employee found its department with one bucket lookup. " +
        "departments was read once and employees once";
      stage.statusLbl.textContent = "Total: " + DEPTS.length + " build + " +
        EMPLOYEES.length + " probe = " + (DEPTS.length + EMPLOYEES.length) +
        " row reads. That is O(n + m).";
    });
    tl.delay(600);
  }

  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var cost = hashJoinCost(c.build_rows, c.probe_rows, c.row_size, c.jbs);
  return buildTimeline(cost.spilled, cost.partitions);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(buildRows, rowSize, jbs, currentProbe) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 1000, xMax: 1e8,
    xLabel: "employees row count", yLabel: "Row comparisons",
    // lowerIsBetter appends "(lower = better)" to the y-label and
    // marks the lower curve at the cursor with a green check + a
    // "Hash is N cheaper" callout. Fixes the "is hash worse?"
    // misread reported by the user (cost charts read inverse to the
    // default "higher = better" eye bias).
    lowerIsBetter: true,
    curves: [
      { label: "Hash: O(depts+emps) = O(n+m)", color: "var(--svg-probe)",
        fn: function(n) { return hashJoinCost(buildRows, n, rowSize, jbs).cmp; } },
      { label: "BNL: O(depts\u00b7emps/buf) = O(n\u00b7m/b)", color: "var(--svg-build)",
        fn: function(n) {
          var rpb = Math.max(1, Math.floor(jbs / rowSize));
          var blocks = Math.max(1, Math.ceil(buildRows / rpb));
          return blocks * n * Math.min(rpb, buildRows);
        } }
    ],
    current: { x: currentProbe },
    xSlider: "probe_rows",
    xSliderTransform: function(xVal) { return Math.max(1000, Math.round(xVal / 1000) * 1000); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = hashJoinCost(c.build_rows, c.probe_rows, c.row_size, c.jbs);
  document.getElementById("out-build").textContent = teachRuntime.formatBytes(cost.buildBytes);
  document.getElementById("out-fits").textContent = cost.fits ? "Yes" : "No";
  document.getElementById("out-fits").className = "value " + (cost.fits ? "ok" : "warn");
  document.getElementById("out-spilled").textContent = cost.spilled ? "Yes" : "No";
  document.getElementById("out-spilled").className = "value " + (cost.spilled ? "hot" : "ok");
  document.getElementById("out-parts").textContent = cost.partitions;
  document.getElementById("out-phases").textContent = cost.phases;
  document.getElementById("out-complexity").textContent = cost.spilled
    ? "O(2\u00b7(depts + emps)) = O(2\u00b7(n + m))"
    : "O(depts + emps) = O(n + m)";
  document.getElementById("out-explanation").textContent = cost.spilled
    ? "departments is " + teachRuntime.formatBytes(cost.buildBytes) + " \u2014 bigger than join_buffer_size. MySQL spills: partition both inputs into " + cost.partitions + " chunks on disk, then probe each separately. Cost roughly doubles."
    : "departments is " + teachRuntime.formatBytes(cost.buildBytes) + " \u2014 fits in join_buffer_size. Single-pass: build the hash table once, stream employees through. O(build + probe).";
  resetStage();
  renderChart(c.build_rows, c.row_size, c.jbs, c.probe_rows);
}

buildStage();
teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
