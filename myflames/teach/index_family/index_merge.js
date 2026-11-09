function indexMergeCost(aRows, bRows, overlapPct, variant) {
  var overlap = Math.floor(Math.min(aRows, bRows) * overlapPct / 100);
  var merged;
  if (variant === "intersection") merged = overlap;
  else merged = aRows + bRows - overlap;
  return {aRows: aRows, bRows: bRows, overlap: overlap, merged: merged, variant: variant};
}

var W = 800, H = 420;
var stage = null;

// Named products so the user can track rows through the merge. Each entry
// carries the product's primary key id: an index merge works on these ids.
// For an equality (category_id = 5) every entry has the same key, and
// InnoDB stores equal keys in id order, so each list comes out sorted by id.
var EQ_A_ROWS = [
  {name: "Laptop", id: 3}, {name: "Keyboard", id: 8}, {name: "Webcam", id: 12},
  {name: "Monitor", id: 15}, {name: "Mouse", id: 21}, {name: "Headset", id: 27},
  {name: "Charger", id: 30}
];
var EQ_B_ROWS = [
  {name: "Tablet", id: 5}, {name: "Webcam", id: 12}, {name: "Monitor", id: 15},
  {name: "Cable", id: 18}, {name: "Stand", id: 24}, {name: "Adapter", id: 33}
];
// For ranges (category_id < 5, supplier_id > 12) entries come in key order,
// so the ids are not sorted: that is why sort-union has to sort them.
var RANGE_A_ROWS = [
  {name: "Mouse", id: 21, key: 1}, {name: "Charger", id: 30, key: 1},
  {name: "Laptop", id: 3, key: 2}, {name: "Webcam", id: 12, key: 2},
  {name: "Keyboard", id: 8, key: 3}, {name: "Monitor", id: 15, key: 4},
  {name: "Headset", id: 27, key: 4}
];
var RANGE_B_ROWS = [
  {name: "Adapter", id: 33, key: 13}, {name: "Tablet", id: 5, key: 14},
  {name: "Webcam", id: 12, key: 14}, {name: "Stand", id: 24, key: 17},
  {name: "Monitor", id: 15, key: 19}, {name: "Cable", id: 18, key: 20}
];
// Products in BOTH scans (same id = same row)
var OVERLAP_IDS = {12: true, 15: true};

// What each merge variant runs. The query card follows the Merge variant.
var VARIANTS = {
  union: {
    label: "Union (OR)",
    a: "category_id = 5", b: "supplier_id = 12",
    sql: "-- Separate indexes on category_id and supplier_id\nSELECT *\nFROM   products\nWHERE  category_id = 5\n   OR  supplier_id = 12;"
  },
  intersection: {
    label: "Intersection (AND)",
    a: "category_id = 5", b: "supplier_id = 12",
    sql: "-- Separate indexes on category_id and supplier_id\nSELECT *\nFROM   products\nWHERE  category_id = 5\n  AND  supplier_id = 12;"
  },
  sort_union: {
    label: "Sort-Union (OR of ranges)",
    a: "category_id < 5", b: "supplier_id > 12",
    sql: "-- Separate indexes on category_id and supplier_id\nSELECT *\nFROM   products\nWHERE  category_id < 5\n   OR  supplier_id > 12;"
  }
};

function rowsFor(variant) {
  return variant === "sort_union"
    ? {a: RANGE_A_ROWS, b: RANGE_B_ROWS}
    : {a: EQ_A_ROWS, b: EQ_B_ROWS};
}

function entryText(variant, row, column) {
  var base = "#" + row.id + " " + row.name;
  return variant === "sort_union" ? column + "=" + row.key + ": " + base : base;
}

// The merged id list for a variant: union and sort-union keep each id
// once; intersection keeps the ids found in both lists. Always in id order.
function mergedIds(variant) {
  var lists = rowsFor(variant);
  var seen = {}, out = [];
  if (variant === "intersection") {
    var inB = {};
    lists.b.forEach(function(r) { inB[r.id] = true; });
    lists.a.forEach(function(r) { if (inB[r.id]) out.push(r.id); });
  } else {
    lists.a.concat(lists.b).forEach(function(r) {
      if (!seen[r.id]) { seen[r.id] = true; out.push(r.id); }
    });
  }
  return out.sort(function(x, y) { return x - y; });
}

function buildStage(variant) {
  var svg = document.getElementById("merge-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var midX = W / 2;
  var v = VARIANTS[variant] || VARIANTS.union;
  var lists = rowsFor(variant);

  // Index A (left)
  var aTitle = anim.svgEl("text", {
    x: midX / 2, y: 22, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  aTitle.textContent = "idx_category scan: " + v.a;
  svg.appendChild(aTitle);

  var aRect = anim.svgEl("rect", {
    x: 20, y: 32, width: midX - 30, height: 120, rx: 8, ry: 8,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token)", "stroke-width": 1.5
  });
  svg.appendChild(aRect);

  var aEntries = [];
  for (var i = 0; i < lists.a.length; i++) {
    var y = 48 + i * 14;
    var lbl = anim.svgEl("text", {
      x: 30, y: y, "font-size": 10, "font-weight": 600, fill: "var(--svg-token-ink)"
    });
    lbl.textContent = entryText(variant, lists.a[i], "category_id");
    svg.appendChild(lbl);
    var dot = anim.svgEl("circle", {
      cx: midX - 40, cy: y - 4, r: 5, fill: "var(--svg-token)", opacity: 0.3
    });
    svg.appendChild(dot);
    aEntries.push({lbl: lbl, dot: dot, id: lists.a[i].id, name: lists.a[i].name});
  }

  // Index B (right)
  var bTitle = anim.svgEl("text", {
    x: midX + midX / 2, y: 22, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  bTitle.textContent = "idx_supplier scan: " + v.b;
  svg.appendChild(bTitle);

  var bRect = anim.svgEl("rect", {
    x: midX + 10, y: 32, width: midX - 30, height: 120, rx: 8, ry: 8,
    fill: "var(--svg-build-soft)", stroke: "var(--svg-build)", "stroke-width": 1.5
  });
  svg.appendChild(bRect);

  var bEntries = [];
  for (var j = 0; j < lists.b.length; j++) {
    var yb = 48 + j * 14;
    var blbl = anim.svgEl("text", {
      x: midX + 20, y: yb, "font-size": 10, "font-weight": 600, fill: "var(--svg-build-ink)"
    });
    blbl.textContent = entryText(variant, lists.b[j], "supplier_id");
    svg.appendChild(blbl);
    var bdot = anim.svgEl("circle", {
      cx: W - 40, cy: yb - 4, r: 5, fill: "var(--svg-build)", opacity: 0.3
    });
    svg.appendChild(bdot);
    bEntries.push({lbl: blbl, dot: bdot, id: lists.b[j].id, name: lists.b[j].name});
  }

  // Merge area (middle)
  var mergeY = 175;
  var mergeLbl = anim.svgEl("text", {
    x: W / 2, y: mergeY, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-group)"
  });
  mergeLbl.textContent = "Merge: " + v.label;
  svg.appendChild(mergeLbl);

  var mergeRect = anim.svgEl("rect", {
    x: 60, y: mergeY + 8, width: W - 120, height: 55, rx: 8, ry: 8,
    fill: "var(--svg-group-soft)", stroke: "var(--svg-group)", "stroke-width": 1.5
  });
  svg.appendChild(mergeRect);

  var mergeContent = anim.svgEl("text", {
    x: W / 2, y: mergeY + 30, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "var(--svg-group)"
  });
  mergeContent.textContent = "(ids merged here)";
  svg.appendChild(mergeContent);
  var mergeIds = anim.svgEl("text", {
    x: W / 2, y: mergeY + 50, "text-anchor": "middle",
    "font-size": 11, "font-weight": 700, fill: "var(--svg-group)"
  });
  mergeIds.textContent = "";
  svg.appendChild(mergeIds);

  // Clustered index fetch area (bottom)
  var fetchY = 270;
  var fetchLbl = anim.svgEl("text", {
    x: W / 2, y: fetchY, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "var(--svg-ok)"
  });
  fetchLbl.textContent = "Clustered index → read each row by id";
  svg.appendChild(fetchLbl);

  var fetchRect = anim.svgEl("rect", {
    x: 60, y: fetchY + 8, width: W - 120, height: 55, rx: 8, ry: 8,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1.5, opacity: 0.3
  });
  svg.appendChild(fetchRect);

  var fetchContent = anim.svgEl("text", {
    x: W / 2, y: fetchY + 40, "text-anchor": "middle",
    "font-size": 11, "font-weight": 600, fill: "var(--svg-ok)", opacity: 0.3
  });
  fetchContent.textContent = "";
  svg.appendChild(fetchContent);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 20, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "var(--svg-ink)"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, variant: variant, aEntries: aEntries, bEntries: bEntries,
    mergeRect: mergeRect, mergeContent: mergeContent, mergeIds: mergeIds,
    fetchRect: fetchRect, fetchContent: fetchContent,
    statusLbl: statusLbl, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  var all = stage.aEntries.concat(stage.bEntries);
  for (var i = 0; i < all.length; i++) {
    all[i].dot.setAttribute("opacity", 0.3);
    all[i].dot.setAttribute("r", 5);
  }
  stage.aEntries.forEach(function(e) { e.dot.setAttribute("fill", "var(--svg-token)"); });
  stage.bEntries.forEach(function(e) { e.dot.setAttribute("fill", "var(--svg-build)"); });
  stage.mergeContent.textContent = "(ids merged here)";
  stage.mergeIds.textContent = "";
  stage.fetchRect.setAttribute("opacity", 0.3);
  stage.fetchContent.setAttribute("opacity", 0.3);
  stage.fetchContent.textContent = "";
  stage.statusLbl.textContent = "";
  stage.tuples.forEach(function(t) { if (t.parentNode) t.parentNode.removeChild(t); });
  stage.tuples = [];
}

function idList(ids) {
  return ids.map(function(id) { return "#" + id; }).join(" ");
}

function buildTimeline(variant) {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var v = VARIANTS[variant] || VARIANTS.union;
  var lists = rowsFor(variant);
  var merged = mergedIds(variant);
  var collected = lists.a.length + lists.b.length;
  var ordered = variant === "sort_union"
    ? "in key order, so their ids are not sorted"
    : "in id order (equal keys are stored by id)";
  // Guided-step kinds for the merge, one per variant (GUIDE in index_merge.py).
  var MERGE_KIND = {
    union: "merge-union",
    intersection: "merge-intersection",
    sort_union: "merge-sort-union"
  };

  function scanIndex(entries, rows, color, name, cond) {
    tl.call(function() {
      phase.textContent = "Scan " + name + " for " + cond + ": " + rows.length + " entries, " +
        ordered + ". Each entry gives a product's id: " + idList(rows.map(function(r) { return r.id; }));
    });
    for (var k = 0; k < entries.length; k++) {
      (function(idx) {
        tl.call(function() {
          entries[idx].dot.setAttribute("opacity", 1);
          entries[idx].dot.setAttribute("fill", color);
        });
        tl.delay(200);
      })(k);
    }
  }

  // Step 1: scan index A
  tl.beat("scan-a");
  tl.mark("Scan idx_category");
  scanIndex(stage.aEntries, lists.a, "var(--svg-token)", "idx_category", v.a);
  tl.call(function() {
    stage.mergeContent.textContent = lists.a.length + " ids from idx_category";
  });
  tl.delay(300);

  // Step 2: scan index B
  tl.beat("scan-b");
  tl.mark("Scan idx_supplier");
  scanIndex(stage.bEntries, lists.b, "var(--svg-build)", "idx_supplier", v.b);
  tl.call(function() {
    stage.mergeContent.textContent = lists.a.length + " + " + lists.b.length + " = " + collected + " ids collected";
  });
  tl.delay(300);

  // Step 3: merge the id lists
  tl.beat(MERGE_KIND[variant] || "merge-union");
  tl.mark("Merge the id lists");
  tl.call(function() {
    var all = stage.aEntries.concat(stage.bEntries);
    for (var ai = 0; ai < all.length; ai++) {
      if (OVERLAP_IDS[all[ai].id]) {
        all[ai].dot.setAttribute("fill", "var(--svg-group)");
        all[ai].dot.setAttribute("r", 7);
      }
    }
    if (variant === "intersection") {
      stage.mergeContent.textContent = "Intersection: keep the ids found in BOTH lists (purple)";
      phase.textContent = "Intersection (AND): only #12 Webcam and #15 Monitor are in both lists, so " +
        merged.length + " ids are left; the other " + (collected - 2 * merged.length) + " ids are dropped and their rows are never read";
    } else if (variant === "sort_union") {
      stage.mergeContent.textContent = "Sort-union: sort all " + collected + " ids, keep each once (#12 and #15 were in both)";
      phase.textContent = "Sort-union (OR of ranges): the ids arrived unsorted, so all " + collected +
        " go into a sort buffer (sort_buffer_size), are sorted, and the 2 repeats (#12, #15) are dropped: " + merged.length + " ids";
    } else {
      stage.mergeContent.textContent = "Union: merge the two sorted lists, keep each id once (#12 and #15 were in both)";
      phase.textContent = "Union (OR): both lists are already in id order, so they merge like a zipper; #12 and #15 come up twice and are kept once: " +
        merged.length + " ids";
    }
    stage.mergeIds.textContent = idList(merged);
  });
  tl.delay(600);

  // Step 4: read the rows from the clustered index
  tl.beat("fetch");
  tl.mark("Read rows from the clustered index");
  tl.call(function() {
    stage.fetchRect.setAttribute("opacity", 1);
    stage.fetchContent.setAttribute("opacity", 1);
    stage.fetchContent.textContent = "Reading " + merged.length + " rows by id, in id order: " + idList(merged);
    phase.textContent = "✓ Index merge done — " + merged.length + " rows read from the clustered index (one lookup per id), " +
      "instead of a full scan of products";
    stage.statusLbl.textContent = variant === "intersection"
      ? "A composite index on (category_id, supplier_id) would find these rows in one range, with no merge."
      : "No single index serves an OR across two columns, which is why the optimizer merges two.";
  });

  return tl;
}

function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  return buildTimeline(c.variant);
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(overlapPct, currentA, variant) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e6,
    xLabel: "Rows per index scan (equal for both)", yLabel: "Rows fetched from clustered index",
    curves: [
      { label: "Index merge union", color: "var(--svg-group)",
        fn: function(n) {
          var ov = Math.floor(n * overlapPct / 100);
          return 2 * n - ov;
        }
      },
      { label: "Index merge intersection", color: "var(--svg-token)",
        fn: function(n) { return Math.floor(n * overlapPct / 100); }
      },
      { label: "Full table scan (no merge)", color: "var(--svg-danger)",
        fn: function(n) { return n * 10; } }
    ],
    current: { x: currentA },
    xSlider: "a_rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(100, Math.min(1000000, xVal / 100) * 100)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = indexMergeCost(c.a_rows, c.b_rows, c.overlap, c.variant);
  document.getElementById("out-a-rows").textContent = teachRuntime.formatInt(cost.aRows);
  document.getElementById("out-b-rows").textContent = teachRuntime.formatInt(cost.bRows);
  document.getElementById("out-overlap").textContent = teachRuntime.formatInt(cost.overlap);
  document.getElementById("out-merged").textContent = teachRuntime.formatInt(cost.merged);
  document.getElementById("out-variant").textContent =
    cost.variant === "intersection" ? "Intersection (AND)" :
    cost.variant === "sort_union" ? "Sort-Union (OR)" : "Union (OR)";
  document.getElementById("out-explanation").textContent =
    cost.variant === "intersection"
      ? "Intersection: only the " + teachRuntime.formatInt(cost.overlap) + " ids present in BOTH lists are read from the clustered index. " +
        "Used for AND conditions on separately indexed columns."
      : (cost.variant === "sort_union" ? "Sort-union: " : "Union: ") + teachRuntime.formatInt(cost.merged) +
        " distinct ids read from the clustered index (" + teachRuntime.formatInt(cost.overlap) +
        " ids found by both scans are read once). Used for OR conditions on separately indexed columns.";
  var sql = document.querySelector(".query-sql");
  if (sql) sql.textContent = (VARIANTS[c.variant] || VARIANTS.union).sql;
  buildStage(c.variant);
  resetStage();
  renderChart(c.overlap, c.a_rows, c.variant);
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
