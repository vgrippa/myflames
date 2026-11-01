var JOIN_BUFFER_SIZE_DEFAULT = %d;

function bkaCost(outerRows, innerRows, rowSize, jbs, keySize) {
  var rpb = Math.max(1, Math.floor(jbs / rowSize));
  var batches = Math.max(1, Math.ceil(outerRows / rpb));
  var fanout = Math.max(2, Math.floor((16384 - 120) / (keySize + 9)));
  var height = Math.max(2, Math.ceil(Math.log(Math.max(1, innerRows)) / Math.log(fanout)));
  var randomIosWithout = outerRows * height;
  var seqIosWith = batches * (height + rpb);
  var speedup = randomIosWithout > 0 ? randomIosWithout / Math.max(1, seqIosWith) : 1;
  return {
    rpb: rpb,
    batches: batches,
    fanout: fanout,
    height: height,
    randomIosWithout: randomIosWithout,
    seqIosWith: seqIosWith,
    speedup: speedup
  };
}

var W = 800, H = 372;
var stage = null;

// ---- Sample data ----
// orders (outer) carry a dept_no; departments (inner) has primary key id
// (InnoDB's row id) and a secondary index on dept_no. The two orders differ
// on purpose: sorting by id is not the same as sorting by dept_no.
var OUTER_ROWS = [
  {id: 1001, deptNo: 30},
  {id: 1002, deptNo: 70},
  {id: 1003, deptNo: 10},
  {id: 1004, deptNo: 50},
  {id: 1005, deptNo: 30},
  {id: 1006, deptNo: 90}
];
var DEPT_BY_NO = {
  10: {id: 5, name: "Sales"},
  30: {id: 2, name: "Eng"},
  50: {id: 4, name: "Mktg"},
  70: {id: 1, name: "Ops"},
  90: {id: 3, name: "HR"}
};

var PILL_W = 110, PILL_H = 26, PILL_GAP = 8, PILL_X0 = 32;
function slotX(i) { return PILL_X0 + i * (PILL_W + PILL_GAP); }

// Lookups in buffer order, and the same entries sorted by primary key.
function lookups() {
  var out = [];
  for (var i = 0; i < OUTER_ROWS.length; i++) {
    var d = DEPT_BY_NO[OUTER_ROWS[i].deptNo];
    out.push({order: OUTER_ROWS[i], dept: d, from: i});
  }
  return out;
}
function sortedLookups() {
  var list = lookups();
  list.sort(function(a, b) { return (a.dept.id - b.dept.id) || (a.from - b.from); });
  return list;
}

function zone(svg, y, h, fill, stroke, title, ink) {
  svg.appendChild(anim.svgEl("rect", {
    x: 20, y: y, width: W - 40, height: h, rx: 10, ry: 10,
    fill: fill, stroke: stroke, "stroke-width": 1.5
  }));
  var t = anim.svgEl("text", {
    x: 32, y: y + 20, "font-size": 12, "font-weight": 700, fill: ink
  });
  t.textContent = title;
  svg.appendChild(t);
}

function pillEl(svg, x, y, fill, stroke, ink) {
  var g = anim.svgEl("g", {opacity: 0, transform: "translate(" + x + "," + y + ")"});
  g.appendChild(anim.svgEl("rect", {
    x: 0, y: 0, width: PILL_W, height: PILL_H, rx: 13, ry: 13,
    fill: fill, stroke: stroke, "stroke-width": 1
  }));
  var t = anim.svgEl("text", {
    x: PILL_W / 2, y: 17, "text-anchor": "middle",
    "font-size": 9.5, "font-weight": 600, fill: ink
  });
  g.appendChild(t);
  svg.appendChild(g);
  return {g: g, rect: g.firstChild, txt: t};
}

function buildStage() {
  var svg = document.getElementById("bka-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // --- Zone 1: Join buffer (top, purple) ---
  var bufY = 30, bufH = 82;
  zone(svg, bufY, bufH, "var(--svg-group-soft)", "var(--svg-group)",
    "Join buffer: a batch of orders rows (the outer input)", "var(--svg-group)");
  var pills = [];
  for (var i = 0; i < OUTER_ROWS.length; i++) {
    var p = pillEl(svg, slotX(i), bufY + 38, "var(--svg-group-soft)", "var(--svg-group)", "var(--svg-group)");
    p.txt.textContent = "#" + OUTER_ROWS[i].id + " dept_no=" + OUTER_ROWS[i].deptNo;
    pills.push(p);
  }

  // --- Zone 2: Multi-Range Read: index lookups, then sort (middle, blue) ---
  var sortY = bufY + bufH + 16, sortH = 82;
  zone(svg, sortY, sortH, "var(--svg-token-soft)", "var(--svg-token)",
    "Multi-Range Read: the dept_no index gives each key's primary key (id), then sort by id",
    "var(--svg-token-ink)");
  var keyPills = [];
  for (var s = 0; s < OUTER_ROWS.length; s++) {
    var kp = pillEl(svg, slotX(s), sortY + 38, "var(--svg-token-soft)", "var(--svg-token)", "var(--svg-token-ink)");
    keyPills.push(kp);
  }

  // --- Zone 3: Read in primary key order (bottom, green) ---
  var readY = sortY + sortH + 16, readH = 96;
  zone(svg, readY, readH, "var(--svg-ok-soft)", "var(--svg-ok)",
    "Read departments rows in primary key order, join each with its order", "var(--svg-ok)");
  var readSlots = [];
  var slotY = readY + 36;
  for (var r = 0; r < OUTER_ROWS.length; r++) {
    var rx = slotX(r);
    var slot = anim.svgEl("rect", {
      x: rx, y: slotY, width: PILL_W, height: 40, rx: 8, ry: 8,
      fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1, opacity: 0
    });
    svg.appendChild(slot);
    var rlbl = anim.svgEl("text", {
      x: rx + PILL_W / 2, y: slotY + 16, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: "var(--svg-ok)", opacity: 0
    });
    svg.appendChild(rlbl);
    var rlbl2 = anim.svgEl("text", {
      x: rx + PILL_W / 2, y: slotY + 31, "text-anchor": "middle",
      "font-size": 9, "font-weight": 600, fill: "var(--svg-ok)", opacity: 0
    });
    svg.appendChild(rlbl2);
    readSlots.push({rect: slot, lbl: rlbl, lbl2: rlbl2});
  }

  // Sweep bar: the read position, moving forward through the primary key.
  var sweep = anim.svgEl("rect", {
    x: 24, y: readY + 30, width: 4, height: 52,
    fill: "var(--svg-ok)", opacity: 0, rx: 2, ry: 2
  });
  svg.appendChild(sweep);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: readY + readH + 34, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "var(--svg-ink)"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, pills: pills, keyPills: keyPills, readSlots: readSlots,
    sweep: sweep, statusLbl: statusLbl, sortY: sortY, bufY: bufY
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.pills.length; i++) stage.pills[i].g.setAttribute("opacity", 0);
  for (var j = 0; j < stage.keyPills.length; j++) {
    var kp = stage.keyPills[j];
    kp.g.setAttribute("opacity", 0);
    kp.g.setAttribute("transform", "translate(" + slotX(j) + "," + (stage.sortY + 38) + ")");
    kp.txt.textContent = "";
  }
  for (var k = 0; k < stage.readSlots.length; k++) {
    var rs = stage.readSlots[k];
    rs.rect.setAttribute("opacity", 0);
    rs.lbl.setAttribute("opacity", 0);
    rs.lbl.textContent = "";
    rs.lbl2.setAttribute("opacity", 0);
    rs.lbl2.textContent = "";
  }
  stage.sweep.setAttribute("opacity", 0);
  stage.sweep.setAttribute("x", 24);
  stage.statusLbl.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var found = lookups();
  var sorted = sortedLookups();
  var n = OUTER_ROWS.length;
  var foundIds = found.map(function(f) { return f.dept.id; }).join(", ");
  var sortedIds = sorted.map(function(f) { return f.dept.id; }).join(", ");

  // --- Phase 1: fill the join buffer (one guided step) ---
  tl.mark("Collect outer keys");
  // Guided steps: kinds map to the simplified code (GUIDE in bka_join.py).
  tl.beat("fill");
  tl.call(function() {
    phase.textContent = "Phase 1 of 4: orders rows fill the join buffer, forming one batch";
  });
  for (var i = 0; i < n; i++) {
    (function(idx) {
      var o = OUTER_ROWS[idx];
      tl.call(function() {
        stage.statusLbl.textContent = "Buffered order #" + o.id + " (dept_no=" + o.deptNo + "), " +
          (idx + 1) + " of " + n;
      });
      tl.add({
        from: 0, to: 1, duration: 160, ease: anim.easeOutCubic,
        onUpdate: function(v) { stage.pills[idx].g.setAttribute("opacity", v.toFixed(2)); }
      });
      tl.delay(100);
    })(i);
  }
  tl.call(function() {
    phase.textContent = "The batch holds " + n + " orders rows. Next, one Multi-Range Read for all their dept_no keys";
  });
  tl.delay(400);

  // --- Phase 2: one Multi-Range Read: index lookups (one guided step) ---
  tl.mark("Index lookups (MRR)");
  tl.beat("lookup");
  tl.call(function() {
    phase.textContent = "Phase 2 of 4: the " + n + " dept_no keys go to InnoDB as one Multi-Range Read; " +
      "the dept_no index gives each department's primary key";
  });
  for (var j = 0; j < n; j++) {
    (function(idx) {
      var f = found[idx];
      var kp = stage.keyPills[idx];
      tl.call(function() {
        kp.txt.textContent = "dept_no=" + f.order.deptNo + " → id=" + f.dept.id;
        stage.statusLbl.textContent = "Index lookup " + (idx + 1) + " of " + n + ": dept_no=" +
          f.order.deptNo + " → primary key id=" + f.dept.id;
      });
      // The found key drops in under its order.
      tl.add({
        from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
        onUpdate: function(v) {
          kp.g.setAttribute("opacity", v.toFixed(2));
          kp.g.setAttribute("transform", "translate(" + slotX(idx) + "," +
            (stage.sortY + 38 - 14 * (1 - v)).toFixed(1) + ")");
        }
      });
      tl.delay(90);
    })(j);
  }
  tl.call(function() {
    phase.textContent = "Primary keys found, in buffer order: " + foundIds + ". Reading them now would jump around";
  });
  tl.delay(400);

  // --- Phase 3: sort by primary key (one guided step) ---
  tl.mark("Sort by rowid");
  tl.beat("sort");
  tl.call(function() {
    phase.textContent = "Phase 3 of 4: sort the primary keys (InnoDB's row ids): " + foundIds + " → " + sortedIds;
    stage.statusLbl.textContent = "Sorting " + n + " row ids so the reads move forward through departments";
  });
  // Each key pill slides to its sorted slot; alternate pills arc over and
  // under so they never cover each other.
  tl.add({
    from: 0, to: 1, duration: 700, ease: anim.easeInOutCubic,
    onUpdate: function(t) {
      for (var k = 0; k < sorted.length; k++) {
        var from = sorted[k].from;
        var x = slotX(from) + (slotX(k) - slotX(from)) * t;
        var lift = (k %% 2 ? 1 : -1) * 12 * Math.sin(Math.PI * t) * (from === k ? 0 : 1);
        stage.keyPills[from].g.setAttribute("transform",
          "translate(" + x.toFixed(1) + "," + (stage.sortY + 38 + lift).toFixed(1) + ")");
      }
    }
  });
  tl.call(function() {
    stage.statusLbl.textContent = "Sorted: " + sortedIds + ". Ready to read in primary key order";
  });
  tl.delay(400);

  // --- Phase 4: read rows in primary key order (one guided step per row) ---
  tl.mark("Sequential read");
  for (var r = 0; r < sorted.length; r++) {
    (function(idx) {
      var f = sorted[idx];
      var again = idx > 0 && sorted[idx - 1].dept.id === f.dept.id;
      var rs = stage.readSlots[idx];
      tl.beat("read-row");
      tl.call(function() {
        phase.textContent = "Phase 4 of 4, read " + (idx + 1) + " of " + n + ": departments id=" + f.dept.id +
          " (" + f.dept.name + ") → joined with order #" + f.order.id +
          (again ? " (the same row again: its page was just read)" : "");
        stage.statusLbl.textContent = "Reading id=" + f.dept.id + " " + f.dept.name +
          " for order #" + f.order.id;
      });
      tl.add({
        from: 0, to: 1, duration: 260, ease: anim.easeInOutCubic,
        onUpdate: function(v) {
          var x0 = idx === 0 ? 24 : slotX(idx - 1) + PILL_W + PILL_GAP / 2 - 2;
          var x1 = slotX(idx) + PILL_W + PILL_GAP / 2 - 2;
          stage.sweep.setAttribute("x", (x0 + (x1 - x0) * v).toFixed(1));
          stage.sweep.setAttribute("opacity", 0.8);
          rs.lbl.textContent = f.dept.name + " (id=" + f.dept.id + ")";
          rs.lbl2.textContent = "\u2192 order #" + f.order.id;
          rs.rect.setAttribute("opacity", v.toFixed(2));
          rs.lbl.setAttribute("opacity", v.toFixed(2));
          rs.lbl2.setAttribute("opacity", v.toFixed(2));
        }
      });
      tl.delay(200);
    })(r);
  }

  tl.mark("Done");
  tl.beat("done");
  tl.call(function() {
    stage.sweep.setAttribute("opacity", 0);
    phase.textContent = "✓ Batch complete: " + n + " lookups, one sorted pass over departments. " +
      "The next batch refills the buffer";
    stage.statusLbl.textContent = "BKA turned " + n + " scattered lookups into one sorted Multi-Range Read.";
  });
  tl.delay(400);
  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}

function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(outerRows, innerRows, keySize) {
  var fanout = Math.max(2, Math.floor((16384 - 120) / (keySize + 9)));
  var height = Math.max(2, Math.ceil(Math.log(Math.max(1, innerRows)) / Math.log(fanout)));
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e6,
    xLabel: "Outer (driving) rows", yLabel: "I/O operations",
    curves: [
      { label: "Simple NLJ: outer \u00d7 height (random)", color: "var(--svg-danger)",
        fn: function(n) { return n * height; } },
      { label: "BKA + MRR: batches \u00d7 (height + rpb) (seq)", color: "var(--svg-ok)",
        fn: function(n) {
          var c = teachRuntime.readControls();
          var cost = bkaCost(n, c.inner_rows, c.row_size, c.jbs, c.key_size);
          return cost.seqIosWith;
        } },
      { label: "Full scan: inner_rows", color: "var(--svg-ink-muted)",
        fn: function(n) { return innerRows; } }
    ],
    current: { x: outerRows },
    xSlider: "outer_rows",
    xSliderTransform: function(xVal) { return Math.max(100, Math.round(xVal / 100) * 100); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = bkaCost(c.outer_rows, c.inner_rows, c.row_size, c.jbs, c.key_size);
  document.getElementById("out-rpb").textContent = teachRuntime.formatInt(cost.rpb);
  document.getElementById("out-batches").textContent = teachRuntime.formatInt(cost.batches);
  document.getElementById("out-height").textContent = teachRuntime.formatInt(cost.height);
  document.getElementById("out-random").textContent = teachRuntime.formatInt(cost.randomIosWithout);
  document.getElementById("out-seq").textContent = teachRuntime.formatInt(cost.seqIosWith);
  document.getElementById("out-speedup").textContent = cost.speedup.toFixed(1) + "\u00d7";
  document.getElementById("out-explanation").textContent =
    "Join buffer holds " + teachRuntime.formatInt(cost.rpb) + " outer rows per batch \u2192 " +
    teachRuntime.formatInt(cost.batches) + " batch(es). " +
    "Without BKA: " + teachRuntime.formatInt(cost.randomIosWithout) + " random I/Os " +
    "(each outer row traverses " + cost.height + " B+tree levels). " +
    "With BKA + MRR: " + teachRuntime.formatInt(cost.seqIosWith) + " sequential I/Os. " +
    "Speedup: " + cost.speedup.toFixed(1) + "\u00d7. " +
    "Raise join_buffer_size \u2192 more rows per batch \u2192 fewer batches \u2192 less overhead.";
  buildStage();
  resetStage();
  renderChart(c.outer_rows, c.inner_rows, c.key_size);
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
