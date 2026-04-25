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
    fill: "#fef3c7", stroke: "#d97706", "stroke-width": 1.5
  });
  svg.appendChild(buildRect);
  var buildLbl = anim.svgEl("text", {
    x: 82, y: 36, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "#92400e"
  });
  buildLbl.textContent = "departments (build side)";
  svg.appendChild(buildLbl);
  var buildRows = [];
  for (var b = 0; b < DEPTS.length; b++) {
    var by = 54 + b * 22;
    var rowLbl = anim.svgEl("text", {
      x: 82, y: by + 14, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "#78350f"
    });
    rowLbl.textContent = "id=" + DEPTS[b].id + " " + DEPTS[b].name;
    svg.appendChild(rowLbl);
    buildRows.push({ lbl: rowLbl, cx: 82, cy: by + 8 });
  }

  // Hash table (middle)
  var htX = 280, htY = 24, htW = 210, htH = NUM_BUCKETS * 46 + 20;
  var htRect = anim.svgEl("rect", {
    x: htX, y: htY, width: htW, height: htH, rx: 10, ry: 10,
    fill: "#f3f4f6", stroke: "#6b7280", "stroke-width": 1.5
  });
  svg.appendChild(htRect);
  var htLbl = anim.svgEl("text", {
    x: htX + htW/2, y: htY - 8, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "#1f2937"
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
      fill: "#ffffff", stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(br);
    var bIdx = anim.svgEl("text", {
      x: htX + 16, y: byB + 14, "font-size": 9, "font-weight": 700, fill: "#9ca3af"
    });
    bIdx.textContent = "[" + i + "]";
    svg.appendChild(bIdx);
    var bContent = anim.svgEl("text", {
      x: htX + htW/2, y: byB + 22, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "#6b7280"
    });
    bContent.textContent = "(empty)";
    svg.appendChild(bContent);
    var matchLbl = anim.svgEl("text", {
      x: htX + htW - 16, y: byB + 14, "text-anchor": "end",
      "font-size": 10, "font-weight": 700, fill: "#059669", opacity: 0
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
    fill: "#ccfbf1", stroke: "#0d9488", "stroke-width": 1.5
  });
  svg.appendChild(probeRect);
  var probeLbl = anim.svgEl("text", {
    x: W - 92, y: 36, "text-anchor": "middle",
    "font-size": 12, "font-weight": 700, fill: "#115e59"
  });
  probeLbl.textContent = "employees (probe side)";
  svg.appendChild(probeLbl);
  var probeRows = [];
  for (var p = 0; p < EMPLOYEES.length; p++) {
    var py = 54 + p * 22;
    var pLbl = anim.svgEl("text", {
      x: W - 92, y: py + 14, "text-anchor": "middle",
      "font-size": 10, "font-weight": 600, fill: "#134e4a"
    });
    pLbl.textContent = EMPLOYEES[p].name + " dept=" + EMPLOYEES[p].dept;
    svg.appendChild(pLbl);
    probeRows.push({ lbl: pLbl, cx: W - 92, cy: py + 8 });
  }

  var statusLbl = anim.svgEl("text", {
    x: W/2, y: H - 32, "text-anchor": "middle",
    "font-size": 13, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  var spillLbl = anim.svgEl("text", {
    x: W/2, y: H - 10, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "#991b1b", opacity: 0
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
    b.rect.setAttribute("fill", "#ffffff");
    b.rect.setAttribute("stroke", "#d1d5db");
    b.rect.setAttribute("stroke-width", 1);
    b.contentLbl.textContent = "(empty)";
    b.contentLbl.setAttribute("fill", "#6b7280");
    b.matchLbl.setAttribute("opacity", 0);
    b.contents = [];
  });
  stage.statusLbl.textContent = "";
  stage.spillLbl.setAttribute("opacity", 0);
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function spawnLabeledTuple(text, color, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var bg = anim.svgEl("rect", {
    x: -42, y: -9, width: 84, height: 18, rx: 9, ry: 9,
    fill: color, stroke: "#1f2937", "stroke-width": 1
  });
  g.appendChild(bg);
  var lbl = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "#ffffff"
  });
  lbl.textContent = text;
  g.appendChild(lbl);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

function flyGroupToBucket(group, fromCx, fromCy, targetBucket, onArrive) {
  var toX = targetBucket.cx;
  var toY = targetBucket.cy;
  var midX = (fromCx + toX) / 2;
  var midY = Math.min(fromCy, toY) - 30;
  var pathFn = anim.path(fromCx, fromCy, midX, midY, toX, toY);

  anim.tween({
    from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
    onUpdate: function(v) { group.setAttribute("opacity", v); }
  });
  anim.tween({
    from: 0, to: 1, duration: 720, ease: anim.easeInOutQuad,
    onUpdate: function(t) {
      var p = pathFn(t);
      group.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
    },
    onComplete: function() {
      anim.tween({
        from: 1, to: 0, duration: 260, ease: anim.easeInCubic,
        onUpdate: function(v) { group.setAttribute("opacity", v); }
      });
      if (onArrive) onArrive();
    }
  });
}

function buildTimeline(spilled, partitions) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");

  // ---- Phase 1: Build — hash each dept row into a bucket ----
  tl.mark("Build: hash departments");
  tl.call(function() {
    phase.textContent = "Phase 1/2 — hashing each department row into a bucket";
    stage.statusLbl.textContent = "hash(dept.id) %% " + NUM_BUCKETS + " decides which bucket";
  });
  for (var i = 0; i < DEPTS.length; i++) {
    (function(idx) {
      var dept = DEPTS[idx];
      var bucketIdx = deptBucket(dept.id);
      var bucket = stage.buckets[bucketIdx];
      tl.call(function() {
        phase.textContent = "Build: dept id=" + dept.id + " \u201c" + dept.name +
          "\u201d \u2192 hash(" + dept.id + ") %% " + NUM_BUCKETS + " = bucket [" + bucketIdx + "]";
        var label = "id=" + dept.id + " " + dept.name;
        var tuple = spawnLabeledTuple(label, "#d97706",
          stage.buildRows[idx].cx, stage.buildRows[idx].cy);
        flyGroupToBucket(tuple,
          stage.buildRows[idx].cx, stage.buildRows[idx].cy, bucket,
          function() {
            bucket.contents.push(dept.name);
            bucket.contentLbl.textContent = bucket.contents.join(", ");
            bucket.contentLbl.setAttribute("fill", "#1f2937");
            bucket.rect.setAttribute("fill", "#fef3c7");
            bucket.rect.setAttribute("stroke", "#d97706");
            anim.pulse(bucket.rect, 2.5, 1, 280);
          });
      });
      tl.delay(320);
    })(i);
  }
  tl.delay(800);
  tl.call(function() {
    phase.textContent = "Build complete \u2014 each bucket shows the dept rows it holds. Now probing employees\u2026";
    stage.statusLbl.textContent = "For each employee: hash(dept_id) \u2192 bucket \u2192 check if dept.id matches";
  });
  tl.delay(1000);

  // ---- Phase 2: Probe — each employee finds its dept in one bucket ----
  tl.mark("Probe: match employees");
  for (var j = 0; j < EMPLOYEES.length; j++) {
    (function(jdx) {
      var emp = EMPLOYEES[jdx];
      var bucketIdx = deptBucket(emp.dept);
      var bucket = stage.buckets[bucketIdx];
      var matchDept = null;
      for (var d = 0; d < DEPTS.length; d++) {
        if (DEPTS[d].id === emp.dept) { matchDept = DEPTS[d].name; break; }
      }
      tl.call(function() {
        phase.textContent = "Probe: " + emp.name + " (dept_id=" + emp.dept +
          ") \u2192 hash(" + emp.dept + ") %% " + NUM_BUCKETS + " = bucket [" + bucketIdx +
          "] \u2014 bucket holds: " + (bucket.contents.length ? bucket.contents.join(", ") : "empty");
        var label = emp.name + " dept=" + emp.dept;
        var tuple = spawnLabeledTuple(label, "#0d9488",
          stage.probeRows[jdx].cx, stage.probeRows[jdx].cy);
        flyGroupToBucket(tuple,
          stage.probeRows[jdx].cx, stage.probeRows[jdx].cy, bucket,
          function() {
            if (matchDept && bucket.contents.indexOf(matchDept) >= 0) {
              bucket.rect.setAttribute("fill", "#d1fae5");
              bucket.rect.setAttribute("stroke", "#059669");
              bucket.rect.setAttribute("stroke-width", 2.5);
              bucket.matchLbl.setAttribute("opacity", 1);
              stage.statusLbl.textContent = "\u2713 " + emp.name + " matched \u201c" +
                matchDept + "\u201d (both have dept_id=" + emp.dept +
                " \u2192 same bucket [" + bucketIdx + "])";
              setTimeout(function() {
                bucket.rect.setAttribute("fill", "#fef3c7");
                bucket.rect.setAttribute("stroke", "#d97706");
                bucket.rect.setAttribute("stroke-width", 1);
                bucket.matchLbl.setAttribute("opacity", 0);
              }, 700);
            }
          });
      });
      tl.delay(400);
    })(j);
  }
  tl.delay(600);

  if (spilled) {
    tl.mark("Grace-hash spill");
    tl.call(function() {
      phase.textContent = "\u26a0 Spill! departments was too big for join_buffer_size";
      stage.spillLbl.textContent = "Partitioned into " + partitions +
        " chunks on disk \u2014 each re-built and re-probed (I/O \u2248 2\u00d7)";
    });
    tl.add({
      from: 0, to: 1, duration: 400, ease: anim.easeOutCubic,
      onUpdate: function(v) { stage.spillLbl.setAttribute("opacity", v); }
    });
  } else {
    tl.mark("Done");
    tl.call(function() {
      phase.textContent = "\u2713 Complete \u2014 every employee found its department in one bucket lookup";
      stage.statusLbl.textContent = "Total: " + DEPTS.length + " build + " +
        EMPLOYEES.length + " probe = " + (DEPTS.length + EMPLOYEES.length) +
        " row reads. That is O(n + m).";
    });
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
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
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
      { label: "Hash: O(depts+emps) = O(n+m)", color: "#0d9488",
        fn: function(n) { return hashJoinCost(buildRows, n, rowSize, jbs).cmp; } },
      { label: "BNL: O(depts\u00b7emps/buf) = O(n\u00b7m/b)", color: "#ca8a04",
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
