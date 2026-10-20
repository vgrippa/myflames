var TMP_TABLE_SIZE_DEFAULT = %d;
var MAX_HEAP_TABLE_SIZE_DEFAULT = %d;

// Mirrors tmp_table_cost() in myflames/teach/_cost_model.py, which cites the
// MySQL 8.4 / MariaDB 11.4 source for each rule:
//   temptable  tmp_table_size per table; max_heap_table_size does not apply
//   memory     smaller of tmp_table_size and max_heap_table_size
//   mariadb    smaller of the two; spills to Aria instead of InnoDB
var TMP_ENGINES = {
  temptable: {label: "TempTable", long: "TempTable", disk: "InnoDB", server: "MySQL"},
  memory: {label: "MEMORY", long: "MEMORY", disk: "InnoDB", server: "MySQL"},
  mariadb: {label: "MariaDB HEAP", long: "MariaDB in-memory (HEAP)", disk: "Aria", server: "MariaDB"}
};

function tmpTableCost(rows, rowSize, tmpSize, maxHeap, engine) {
  var key = Object.prototype.hasOwnProperty.call(TMP_ENGINES, engine) ? engine : "temptable";
  var limit, source;
  if (key === "temptable") {
    limit = tmpSize;
    source = "tmp_table_size";
  } else {
    limit = Math.min(tmpSize, maxHeap);
    source = tmpSize === maxHeap ? "both"
      : (tmpSize < maxHeap ? "tmp_table_size" : "max_heap_table_size");
  }
  var diskEngine = TMP_ENGINES[key].disk;
  if (rows <= 0 || rowSize <= 0) {
    return {engine: key, limit: limit, source: source, cap: 0, fits: true,
            memRows: 0, diskRows: 0, diskEngine: diskEngine};
  }
  var cap = Math.max(1, Math.floor(limit / rowSize));
  var fits = rows <= cap;
  return {engine: key, limit: limit, source: source, cap: cap, fits: fits,
          memRows: Math.min(rows, cap), diskRows: fits ? 0 : rows - cap,
          diskEngine: diskEngine};
}

function fmtN(n) { return Math.round(n).toLocaleString("en-US"); }
function fmtB(b) { return teachRuntime.formatBytes(b); }
function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
// Progress of v inside the sub-range [a, b], clamped to 0..1.
function sub(v, a, b) { return clamp01((v - a) / (b - a)); }

function ruleText(cost) {
  return cost.engine === "temptable"
    ? "tmp_table_size alone; max_heap_table_size does not apply to TempTable"
    : "the smaller of tmp_table_size and max_heap_table_size";
}
// Short form printed on the stage, right above the in-memory box.
function limitLabel(cost) {
  return cost.engine === "temptable"
    ? "limit: tmp_table_size = " + fmtB(cost.limit) + " (max_heap_table_size ignored)"
    : "limit: smaller of tmp_table_size, max_heap_table_size = " + fmtB(cost.limit);
}
function limitPhrase(cost) {
  return cost.engine === "temptable"
    ? "its limit is tmp_table_size, " + fmtB(cost.limit)
    : "its limit is the smaller of tmp_table_size and max_heap_table_size, " + fmtB(cost.limit);
}
function pctText(frac) {
  var p = frac * 100;
  return (p > 0 && p < 1 ? "<1" : String(Math.round(p))) + "%%";
}

var W = 800, H = 380;
var stage = null;

// Stage geometry. Boxes and bars are created once per recompute and only
// mutated afterwards (animation-expert: move, don't redraw).
var SRC_Y = 61;
var MEM_Y = 124, MEM_H = 72;
var DISK_Y = 246, DISK_H = 66;
var BAR_X = 30, BAR_W = W - 60, BAR_H = 14;
var MEM_BAR_Y = MEM_Y + 12;
var DISK_BAR_Y = DISK_Y + 12;

var EMPLOYEES = [
  {name: "Alice", dept: "Engineering"},
  {name: "Bob", dept: "Sales"},
  {name: "Carol", dept: "Engineering"},
  {name: "Dave", dept: "Marketing"},
  {name: "Eve", dept: "Engineering"},
  {name: "Frank", dept: "Sales"},
  {name: "Grace", dept: "HR"},
  {name: "Hank", dept: "Marketing"},
  {name: "Iris", dept: "Engineering"},
  {name: "Jake", dept: "Sales"},
  {name: "Kate", dept: "HR"},
  {name: "Leo", dept: "Engineering"}
];
var N_SAMPLE = EMPLOYEES.length;

var MEM_IDLE_TEXT = "Rows land here first: fast, no disk I/O";
var DISK_IDLE_TEXT = "Used only if the table outgrows its limit";

function svgText(svg, attrs, text) {
  var t = anim.svgEl("text", attrs);
  t.textContent = text;
  svg.appendChild(t);
  return t;
}

function buildStage(cost) {
  var svg = document.getElementById("tmp-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var eng = TMP_ENGINES[cost.engine];

  // Source rows (top)
  svgText(svg, {x: 20, y: 24, "font-size": 13, "font-weight": 700, fill: "var(--svg-token)"},
    "employees table → rows streaming into GROUP BY department");
  var srcRect = anim.svgEl("rect", {
    x: 20, y: 36, width: W - 40, height: 50, rx: 8, ry: 8,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token)", "stroke-width": 1.5
  });
  svg.appendChild(srcRect);

  // In-memory temp table (middle): engine name on the left, the limit
  // rule with its value on the right.
  var memLbl = svgText(svg, {x: 20, y: MEM_Y - 10, "font-size": 13, "font-weight": 700, fill: "var(--svg-ok)"},
    "In memory: " + eng.label + " temp table");
  var limitLbl = svgText(svg, {
    x: W - 20, y: MEM_Y - 10, "text-anchor": "end",
    "font-size": 11.5, "font-weight": 700, fill: "var(--svg-ok)"
  }, limitLabel(cost));
  var memRect = anim.svgEl("rect", {
    x: 20, y: MEM_Y, width: W - 40, height: MEM_H, rx: 8, ry: 8,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1.5
  });
  svg.appendChild(memRect);
  var barBg = anim.svgEl("rect", {
    x: BAR_X, y: MEM_BAR_Y, width: BAR_W, height: BAR_H, rx: 3, ry: 3,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok-soft)", "stroke-width": 1
  });
  svg.appendChild(barBg);
  var barFill = anim.svgEl("rect", {
    x: BAR_X, y: MEM_BAR_Y, width: 0, height: BAR_H, rx: 3, ry: 3, fill: "var(--svg-ok-soft)"
  });
  svg.appendChild(barFill);
  var barPct = svgText(svg, {
    x: BAR_X + BAR_W / 2, y: MEM_BAR_Y + 11, "text-anchor": "middle",
    "font-size": 9, "font-weight": 700, fill: "var(--svg-ok)"
  }, "0%% of limit");
  var memContent = svgText(svg, {
    x: W / 2, y: MEM_Y + 52, "text-anchor": "middle",
    "font-size": 11.5, "font-weight": 600, fill: "var(--svg-ok)"
  }, MEM_IDLE_TEXT);

  // On-disk temp table (bottom). Everything but the title sits in one
  // group so a single opacity tween brings it to life at conversion.
  svgText(svg, {x: 20, y: DISK_Y - 10, "font-size": 13, "font-weight": 700, fill: "var(--svg-danger)"},
    "On disk: " + eng.disk + " temp table (after conversion)");
  var diskGroup = anim.svgEl("g", {opacity: 0.3});
  svg.appendChild(diskGroup);
  var diskRect = anim.svgEl("rect", {
    x: 20, y: DISK_Y, width: W - 40, height: DISK_H, rx: 8, ry: 8,
    fill: "var(--svg-danger-soft)", stroke: "var(--svg-danger)", "stroke-width": 1.5
  });
  diskGroup.appendChild(diskRect);
  diskGroup.appendChild(anim.svgEl("rect", {
    x: BAR_X, y: DISK_BAR_Y, width: BAR_W, height: BAR_H, rx: 3, ry: 3,
    fill: "var(--svg-danger-soft)", stroke: "var(--svg-danger-soft)", "stroke-width": 1
  }));
  var diskBarFill = anim.svgEl("rect", {
    x: BAR_X, y: DISK_BAR_Y, width: 0, height: BAR_H, rx: 3, ry: 3, fill: "var(--svg-danger)"
  });
  diskGroup.appendChild(diskBarFill);
  var diskContent = anim.svgEl("text", {
    x: W / 2, y: DISK_Y + 48, "text-anchor": "middle",
    "font-size": 11.5, "font-weight": 600, fill: "var(--svg-danger)"
  });
  diskContent.textContent = DISK_IDLE_TEXT;
  diskGroup.appendChild(diskContent);

  var statusLbl = svgText(svg, {
    x: W / 2, y: H - 14, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "var(--svg-ink)"
  }, "");

  stage = {
    svg: svg, memLbl: memLbl, limitLbl: limitLbl,
    memRect: memRect, memContent: memContent,
    barFill: barFill, barPct: barPct,
    diskGroup: diskGroup, diskRect: diskRect, diskBarFill: diskBarFill,
    diskContent: diskContent, statusLbl: statusLbl, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  stage.memRect.setAttribute("fill", "var(--svg-ok-soft)");
  stage.memRect.setAttribute("stroke", "var(--svg-ok)");
  stage.memContent.textContent = MEM_IDLE_TEXT;
  setMemBar(0);
  stage.diskGroup.setAttribute("opacity", 0.3);
  stage.diskBarFill.setAttribute("width", 0);
  stage.diskContent.textContent = DISK_IDLE_TEXT;
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

// Bar colour eases green -> amber -> red as the table nears its limit.
function barColor(frac) {
  if (frac <= 0.6) return "var(--svg-ok-soft)";
  if (frac <= 0.8) return anim.lerpColor("var(--svg-ok-soft)", "var(--svg-build)", (frac - 0.6) / 0.2);
  return anim.lerpColor("var(--svg-build)", "var(--svg-danger)", clamp01((frac - 0.8) / 0.2));
}
function setMemBar(frac) {
  var f = clamp01(frac);
  stage.barFill.setAttribute("width", (BAR_W * f).toFixed(1));
  stage.barFill.setAttribute("fill", barColor(f));
  stage.barPct.textContent = f >= 1 ? "FULL" : pctText(f) + " of limit";
}
function setDiskBar(frac) {
  stage.diskBarFill.setAttribute("width", (BAR_W * clamp01(frac)).toFixed(1));
}

function spawnTuple(text, color, cx, cy) {
  var g = anim.svgEl("g", { opacity: 0, transform: "translate(" + cx + "," + cy + ")" });
  var tw = Math.max(70, text.length * 5.5 + 14);
  g.appendChild(anim.svgEl("rect", {
    x: -tw / 2, y: -9, width: tw, height: 18, rx: 9, ry: 9,
    fill: color, stroke: "var(--svg-ink)", "stroke-width": 1
  }));
  var lbl = anim.svgEl("text", {
    x: 0, y: 4, "text-anchor": "middle",
    "font-size": 8.5, "font-weight": 700, fill: "var(--svg-on-solid)"
  });
  lbl.textContent = text;
  g.appendChild(lbl);
  stage.svg.appendChild(g);
  stage.tuples.push(g);
  return g;
}

// Where a pill lands: the growing edge of a bar, kept clear of the box ends.
function barEdgeX(frac) {
  return Math.max(BAR_X + 50, Math.min(BAR_X + BAR_W - 50, BAR_X + BAR_W * clamp01(frac)));
}

// Move a pill along a slight arc from (x1, y1) to (x2, y2) at eased
// progress v. opts.fadeIn fades it in at the start; opts.fadeOut fades it
// out as it lands (the row has joined the bar).
function placePill(slot, x1, y1, x2, y2, v, opts) {
  var g = slot.g;
  if (!g) return;
  var p = anim.path(x1, y1, (x1 + x2) / 2, Math.min(y1, y2) - 18, x2, y2)(v);
  var op = 1;
  if (opts.fadeIn) op = Math.min(op, v / 0.15);
  if (opts.fadeOut) op = Math.min(op, 1 - sub(v, 0.82, 1));
  g.setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")");
  g.setAttribute("opacity", clamp01(op).toFixed(3));
}

function buildTimeline(cost, c) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var eng = TMP_ENGINES[cost.engine];
  var rows = c.rows;
  var phases = cost.fits ? 2 : 3;

  // Each sample pill stands for one twelfth of the real rows.
  function realAfter(i) { return Math.min(rows, Math.round((i + 1) * rows / N_SAMPLE)); }
  var convIdx = -1;
  if (!cost.fits) {
    for (var k = 0; k < N_SAMPLE; k++) {
      if (realAfter(k) > cost.cap) { convIdx = k; break; }
    }
  }
  var copiedFrac = cost.cap / rows;
  var memBarMid = MEM_BAR_Y + BAR_H / 2;
  var diskBarMid = DISK_BAR_Y + BAR_H / 2;

  tl.mark("Fill in memory");
  tl.call(function() {
    phase.textContent = "Phase 1/" + phases + ": GROUP BY streams " + fmtN(rows) +
      " employees rows into an in-memory temp table (" + eng.label + "); " + limitPhrase(cost);
  });

  for (var i = 0; i < N_SAMPLE; i++) {
    (function(idx) {
      var emp = EMPLOYEES[idx];
      var label = emp.name + " (" + emp.dept + ")";
      var after = realAfter(idx);
      var before = idx === 0 ? 0 : realAfter(idx - 1);
      var col = 60 + (idx %% 8) * 90;
      var slot = {g: null, x: 0, y: 0};

      if (convIdx === -1 || idx < convIdx) {
        // Row batch fits: the pill drops onto the bar's growing edge.
        var fromFrac = before / cost.cap;
        var toFrac = after / cost.cap;
        tl.call(function() {
          slot.g = spawnTuple(label, "var(--svg-ok)", col, SRC_Y);
          stage.statusLbl.textContent = "Insert " + label + ": " + fmtN(after) + " of " +
            fmtN(rows) + " rows in memory, " + pctText(toFrac) + " of the " + fmtB(cost.limit) + " limit";
        });
        tl.add({from: 0, to: 1, duration: 360, ease: anim.easeInOutQuad, onUpdate: function(v) {
          placePill(slot, col, SRC_Y, barEdgeX(toFrac), memBarMid, v, {fadeIn: true, fadeOut: true});
          setMemBar(fromFrac + (toFrac - fromFrac) * sub(v, 0.55, 1));
        }});
        tl.call(function() {
          anim.arrival(stage.memRect);
          stage.memContent.textContent = fmtN(after) + " of " + fmtN(rows) + " rows in memory (" +
            fmtB(after * c.row_size) + " of " + fmtB(cost.limit) + ")";
        });
        tl.delay(60);
        return;
      }

      if (idx === convIdx) {
        // The batch that does not fit: amber pill fills the bar and waits.
        var startFrac = before / cost.cap;
        slot.x = barEdgeX(1);
        slot.y = memBarMid;
        tl.call(function() {
          slot.g = spawnTuple(label, "var(--svg-build)", col, SRC_Y);
          stage.statusLbl.textContent = "Insert " + label + ": row " + fmtN(cost.cap + 1) + " of " +
            fmtN(rows) + " does not fit under the " + fmtB(cost.limit) + " limit";
        });
        tl.add({from: 0, to: 1, duration: 360, ease: anim.easeInOutQuad, onUpdate: function(v) {
          placePill(slot, col, SRC_Y, slot.x, slot.y, v, {fadeIn: true});
          setMemBar(startFrac + (1 - startFrac) * sub(v, 0.55, 1));
        }});
        tl.call(function() {
          stage.memContent.textContent = fmtN(cost.cap) + " rows fill the " + fmtB(cost.limit) + " limit";
        });
        tl.delay(300);

        // The one anticipation moment: the box flushes red, disk wakes up.
        tl.mark("Convert to disk");
        tl.call(function() {
          phase.textContent = "Phase 2/3: ⚠ the " + eng.label + " table hit its " +
            fmtB(cost.limit) + " limit, so " + eng.server + " converts it to an on-disk " +
            cost.diskEngine + " temp table";
          stage.statusLbl.textContent = "Conversion: create an on-disk " + cost.diskEngine +
            " table, then copy every row already in memory into it";
        });
        tl.add({from: 0, to: 1, duration: 420, ease: anim.easeOutCubic, onUpdate: function(v) {
          stage.memRect.setAttribute("fill", anim.lerpColor("var(--svg-ok-soft)", "var(--svg-danger-soft)", v));
          stage.memRect.setAttribute("stroke", anim.lerpColor("var(--svg-ok)", "var(--svg-danger)", v));
          stage.diskGroup.setAttribute("opacity", (0.3 + 0.7 * v).toFixed(3));
        }});
        tl.call(function() {
          anim.arrival(stage.memRect, {peakWidth: 3});
          stage.barPct.textContent = "copying…";
          stage.memContent.textContent = "Copying " + fmtN(cost.cap) + " rows to the on-disk " +
            cost.diskEngine + " table…";
          stage.diskContent.textContent = "Receiving " + fmtN(cost.cap) + " rows from memory…";
        });
        // Copy: the memory bar drains while the disk bar fills.
        tl.add({from: 0, to: 1, duration: 800, ease: anim.easeInOutCubic, onUpdate: function(v) {
          stage.barFill.setAttribute("width", (BAR_W * (1 - v)).toFixed(1));
          setDiskBar(copiedFrac * v);
        }});
        tl.call(function() {
          stage.barPct.textContent = "freed";
          stage.memContent.textContent = "Memory freed: the whole table now lives on disk";
          stage.diskContent.textContent = fmtN(cost.cap) + " rows copied from memory";
          anim.arrival(stage.diskRect);
        });
        tl.delay(300);

        tl.mark("Insert on disk");
        tl.call(function() {
          phase.textContent = "Phase 3/3: the remaining " + fmtN(cost.diskRows) +
            " rows go straight to the on-disk " + cost.diskEngine + " table (slower)";
        });
        var diskTo = after / rows;
        tl.add({from: 0, to: 1, duration: 400, ease: anim.easeInOutQuad, onUpdate: function(v) {
          placePill(slot, slot.x, slot.y, barEdgeX(diskTo), diskBarMid, v, {fadeOut: true});
          setDiskBar(copiedFrac + (diskTo - copiedFrac) * sub(v, 0.55, 1));
        }});
        tl.call(function() {
          anim.arrival(stage.diskRect);
          stage.diskContent.textContent = fmtN(cost.cap) + " rows copied + " +
            fmtN(after - cost.cap) + " rows inserted on disk";
        });
        tl.delay(60);
        return;
      }

      // After the conversion: red pills skip memory and land on disk.
      var dFrom = before / rows;
      var dTo = after / rows;
      tl.call(function() {
        slot.g = spawnTuple(label, "var(--svg-danger)", col, SRC_Y);
        stage.statusLbl.textContent = "Insert " + label + ": straight to disk, " +
          fmtN(after - cost.cap) + " rows inserted on disk so far";
      });
      tl.add({from: 0, to: 1, duration: 400, ease: anim.easeInOutQuad, onUpdate: function(v) {
        placePill(slot, col, SRC_Y, barEdgeX(dTo), diskBarMid, v, {fadeIn: true, fadeOut: true});
        setDiskBar(dFrom + (dTo - dFrom) * sub(v, 0.55, 1));
      }});
      tl.call(function() {
        anim.arrival(stage.diskRect);
        stage.diskContent.textContent = fmtN(cost.cap) + " rows copied + " +
          fmtN(after - cost.cap) + " rows inserted on disk";
      });
      tl.delay(60);
    })(i);
  }

  tl.delay(300);
  tl.mark("Done");
  tl.call(function() {
    var need = fmtB(rows * c.row_size);
    if (cost.fits) {
      phase.textContent = "Phase 2/2: ✓ done. All " + fmtN(rows) + " rows (" + need +
        ") stayed in the " + eng.label + " table, under its " + fmtB(cost.limit) + " limit";
      stage.statusLbl.textContent = cost.engine === "temptable"
        ? "0 rows on disk. TempTable also needs room in the shared temptable_max_ram pool."
        : "0 rows on disk: the whole GROUP BY ran in memory.";
    } else {
      phase.textContent = "✓ Done: " + fmtN(cost.cap) + " rows fit in memory, then the table moved to disk and " +
        fmtN(cost.diskRows) + " more rows were inserted into on-disk " + cost.diskEngine;
      stage.statusLbl.textContent = cost.engine === "temptable"
        ? "To stay in memory: tmp_table_size ≥ ~" + need + ". max_heap_table_size does not matter here."
        : "To stay in memory: tmp_table_size and max_heap_table_size both ≥ ~" + need + ".";
    }
  });
  tl.delay(350);
  return tl;
}

function readCost() {
  var c = teachRuntime.readControls();
  return {c: c, cost: tmpTableCost(c.rows, c.row_size, c.tmp_size, c.max_heap, c.engine)};
}

function buildCurrentTimeline() {
  var rc = readCost();
  return buildTimeline(rc.cost, rc.c);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

// TempTable ignores max_heap_table_size: dim that control and say so, but
// keep it draggable so the learner can see the limit stay put.
function syncMaxHeapControl(engine) {
  var ctl = document.getElementById("max-heap-control");
  var hint = document.getElementById("max-heap-hint");
  if (!ctl || !hint) return;
  if (engine === "temptable") {
    ctl.style.opacity = "0.55";
    hint.textContent = "TempTable ignores this setting. Drag it: the limit stays put.";
  } else {
    ctl.style.opacity = "1";
    hint.textContent = "Default: " + (MAX_HEAP_TABLE_SIZE_DEFAULT / 1048576) + " MiB. " +
      TMP_ENGINES[engine].server + (engine === "memory" ? " MEMORY" : "") +
      " uses the smaller of this and tmp_table_size.";
  }
}

function sourceText(cost) {
  if (cost.engine === "temptable") return "tmp_table_size";
  if (cost.source === "both") return "both (equal)";
  return cost.source + " (smaller)";
}

function explanationText(cost, c) {
  var label = TMP_ENGINES[cost.engine].long;
  if (cost.fits) {
    var s = "All " + fmtN(c.rows) + " rows (" + fmtB(c.rows * c.row_size) + ") fit in the " + label +
      " temp table. Its limit is " + fmtB(cost.limit) + " (" + ruleText(cost) + "). No on-disk conversion.";
    if (cost.engine === "temptable") {
      s += " TempTable tables also share the global temptable_max_ram pool; if it runs out, " +
        "a table converts to on-disk InnoDB even below tmp_table_size.";
    }
    return s;
  }
  var out = "The " + label + " temp table holds " + fmtN(cost.cap) + " rows (" + fmtB(cost.limit) +
    ": " + ruleText(cost) + "). Row " + fmtN(cost.cap + 1) + " triggers conversion to an on-disk " +
    cost.diskEngine + " temp table, and the remaining " + fmtN(cost.diskRows) + " rows are inserted there. ";
  if (cost.engine === "temptable") {
    out += "Raise tmp_table_size to avoid the conversion; raising max_heap_table_size changes nothing here.";
  } else {
    out += "Raise tmp_table_size and max_heap_table_size together; raising only one leaves the smaller limit in force.";
  }
  return out;
}

function recompute() {
  var rc = readCost();
  var c = rc.c, cost = rc.cost;
  syncMaxHeapControl(cost.engine);
  document.getElementById("out-limit").textContent = fmtB(cost.limit);
  document.getElementById("out-source").textContent = sourceText(cost);
  document.getElementById("out-cap").textContent = teachRuntime.formatInt(cost.cap);
  document.getElementById("out-mem-rows").textContent = teachRuntime.formatInt(cost.memRows);
  document.getElementById("out-disk-rows").textContent = teachRuntime.formatInt(cost.diskRows);
  document.getElementById("out-conversion").textContent = cost.fits
    ? "No: all in memory"
    : "Yes: moved to on-disk " + cost.diskEngine;
  document.getElementById("out-conversion").className = "value " + (cost.fits ? "ok" : "hot");
  document.getElementById("out-explanation").textContent = explanationText(cost, c);
  buildStage(cost);
  resetStage();
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
