function lookupCost(rows, selectivityPct, covering) {
  var sel = Math.max(0.001, Math.min(1, selectivityPct / 100));
  var matches = Math.max(1, Math.floor(rows * sel));
  var treeHeight = Math.max(3, Math.ceil(Math.log(Math.max(2, rows)) / Math.log(900)));
  var descentReads = treeHeight;
  var indexReads = descentReads + matches;
  var rowFetches = covering ? 0 : matches;
  var totalReads = indexReads + rowFetches;
  return {
    rows: rows,
    matches: matches,
    treeHeight: treeHeight,
    descentReads: descentReads,
    indexReads: indexReads,
    rowFetches: rowFetches,
    totalReads: totalReads,
    covering: covering
  };
}

var W = 860, H = 470;
var stage = null;

var INDEX_KEYS = [
  {key: "US", rid: 102, name: "Ben", city: "Austin"},
  {key: "US", rid: 118, name: "Diego", city: "Miami"},
  {key: "US", rid: 131, name: "Farah", city: "Seattle"},
  {key: "US", rid: 165, name: "Ivan", city: "Boston"},
  {key: "US", rid: 188, name: "Noah", city: "Dallas"},
  {key: "US", rid: 204, name: "Zara", city: "Denver"}
];

function buildStage() {
  var svg = document.getElementById("lookup-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var idxX = 20, idxY = 42, idxW = 390, idxH = 350;
  var tblX = 440, tblY = 42, tblW = 400, tblH = 350;

  var idxLbl = anim.svgEl("text", {
    x: idxX, y: 24, "font-size": 13, "font-weight": 700, fill: "#1e40af"
  });
  idxLbl.textContent = "secondary B+tree: idx_users_country(country, user_id)";
  svg.appendChild(idxLbl);

  var idxRect = anim.svgEl("rect", {
    x: idxX, y: idxY, width: idxW, height: idxH, rx: 8, ry: 8,
    fill: "#eff6ff", stroke: "#60a5fa", "stroke-width": 1.5
  });
  svg.appendChild(idxRect);

  var tableLbl = anim.svgEl("text", {
    x: tblX, y: 24, "font-size": 13, "font-weight": 700, fill: "#7c2d12"
  });
  tableLbl.textContent = "clustered table (PRIMARY) rows fetched by row-id";
  svg.appendChild(tableLbl);

  var tableRect = anim.svgEl("rect", {
    x: tblX, y: tblY, width: tblW, height: tblH, rx: 8, ry: 8,
    fill: "#fff7ed", stroke: "#fdba74", "stroke-width": 1.5
  });
  svg.appendChild(tableRect);

  var splitX = 420;
  var scanArrow = anim.svgEl("path", {
    d: "M400,220 C412,220 416,220 430,220 M423,214 L430,220 L423,226",
    stroke: "#94a3b8", "stroke-width": 2, fill: "none"
  });
  svg.appendChild(scanArrow);

  var root = anim.svgEl("rect", {
    x: idxX + 145, y: idxY + 16, width: 100, height: 36, rx: 7, ry: 7,
    fill: "#dbeafe", stroke: "#93c5fd", "stroke-width": 1.5
  });
  svg.appendChild(root);
  var rootTxt = anim.svgEl("text", {
    x: idxX + 195, y: idxY + 38, "text-anchor": "middle",
    "font-size": 10.5, "font-weight": 700, fill: "#1e3a8a"
  });
  rootTxt.textContent = "ROOT";
  svg.appendChild(rootTxt);

  var internal = anim.svgEl("rect", {
    x: idxX + 130, y: idxY + 86, width: 130, height: 38, rx: 7, ry: 7,
    fill: "#dbeafe", stroke: "#93c5fd", "stroke-width": 1.5
  });
  svg.appendChild(internal);
  var internalTxt = anim.svgEl("text", {
    x: idxX + 195, y: idxY + 110, "text-anchor": "middle",
    "font-size": 10.5, "font-weight": 700, fill: "#1e3a8a"
  });
  internalTxt.textContent = "INTERNAL: country ranges";
  svg.appendChild(internalTxt);

  var edge1 = anim.svgEl("line", {
    x1: idxX + 195, y1: idxY + 52, x2: idxX + 195, y2: idxY + 86,
    stroke: "#94a3b8", "stroke-width": 2
  });
  svg.appendChild(edge1);

  var leafRect = anim.svgEl("rect", {
    x: idxX + 24, y: idxY + 150, width: idxW - 48, height: 178, rx: 7, ry: 7,
    fill: "#e0ecff", stroke: "#93c5fd", "stroke-width": 1.5
  });
  svg.appendChild(leafRect);
  var leafLbl = anim.svgEl("text", {
    x: idxX + 30, y: idxY + 172, "font-size": 11, "font-weight": 700, fill: "#1e3a8a"
  });
  leafLbl.textContent = "LEAF RANGE: country='US' entries -> row-id pointers";
  svg.appendChild(leafLbl);

  var edge2 = anim.svgEl("line", {
    x1: idxX + 195, y1: idxY + 124, x2: idxX + 195, y2: idxY + 150,
    stroke: "#94a3b8", "stroke-width": 2
  });
  svg.appendChild(edge2);

  var token = anim.svgEl("rect", {
    x: idxX + 34, y: idxY + 16, width: 72, height: 24, rx: 12, ry: 12,
    fill: "#f59e0b", stroke: "#b45309", "stroke-width": 1.5, opacity: 0.96
  });
  svg.appendChild(token);
  var tokenTxt = anim.svgEl("text", {
    x: idxX + 70, y: idxY + 32, "text-anchor": "middle",
    "font-size": 9.5, "font-weight": 700, fill: "#ffffff"
  });
  tokenTxt.textContent = "country='US'";
  svg.appendChild(tokenTxt);

  var rows = [];
  var fetched = [];
  for (var i = 0; i < INDEX_KEYS.length; i++) {
    var y = idxY + 184 + i * 26;
    var r = anim.svgEl("rect", {
      x: idxX + 34, y: y, width: idxW - 72, height: 20, rx: 5, ry: 5,
      fill: "#dbeafe", stroke: "#bfdbfe", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: idxX + 42, y: y + 14, "font-size": 10, "font-weight": 600, fill: "#1e40af"
    });
    t.textContent = "country=US -> row_id=" + INDEX_KEYS[i].rid + " (" + INDEX_KEYS[i].name + ")";
    svg.appendChild(t);
    rows.push({bg: r, txt: t, data: INDEX_KEYS[i], y: y});

    var out = anim.svgEl("text", {
      x: tblX + 14, y: tblY + 70 + i * 34, "font-size": 11, "font-weight": 600, fill: "#7c2d12"
    });
    out.textContent = "";
    svg.appendChild(out);
    fetched.push(out);
  }

  var tableHdr = anim.svgEl("text", {
    x: tblX + 14, y: tblY + 36, "font-size": 11, "font-weight": 700, fill: "#92400e"
  });
  tableHdr.textContent = "Fetched base rows:";
  svg.appendChild(tableHdr);

  var probeDot = anim.svgEl("circle", {
    cx: splitX, cy: idxY + 220, r: 5.5, fill: "#f59e0b", stroke: "#92400e",
    "stroke-width": 1.5, opacity: 0
  });
  svg.appendChild(probeDot);

  var status = anim.svgEl("text", {
    x: W / 2, y: H - 18, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "#1f2937"
  });
  status.textContent = "";
  svg.appendChild(status);

  stage = {
    svg: svg,
    rows: rows,
    fetched: fetched,
    status: status,
    token: token,
    tokenTxt: tokenTxt,
    probeDot: probeDot,
    nodes: {root: root, internal: internal, leaf: leafRect},
    idxX: idxX,
    tblX: tblX,
    tblY: tblY
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.rows.length; i++) {
    stage.rows[i].bg.setAttribute("fill", "#dbeafe");
    stage.rows[i].bg.setAttribute("stroke", "#bfdbfe");
    stage.fetched[i].textContent = "";
  }
  stage.nodes.root.setAttribute("fill", "#dbeafe");
  stage.nodes.internal.setAttribute("fill", "#dbeafe");
  stage.nodes.leaf.setAttribute("fill", "#e0ecff");
  stage.token.setAttribute("x", stage.idxX + 34);
  stage.token.setAttribute("y", 58);
  stage.token.setAttribute("opacity", 0.96);
  stage.tokenTxt.setAttribute("x", stage.idxX + 70);
  stage.tokenTxt.setAttribute("y", 74);
  stage.probeDot.setAttribute("opacity", 0);
  stage.probeDot.setAttribute("cx", 420);
  stage.probeDot.setAttribute("cy", stage.idxY + 220);
  stage.status.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var c = teachRuntime.readControls();
  var covering = !!c.covering;
  var n = Math.max(1, Math.min(stage.rows.length, Math.floor((c.selectivity / 100) * stage.rows.length)));

  tl.mark("Descend B+tree");
  tl.call(function() {
    phase.textContent = "Phase 1/3 — descend B+tree: root -> internal -> matching leaf range";
    stage.status.textContent = "Lookup token starts at the root page.";
  });
  tl.add({
    from: stage.idxX + 34, to: stage.idxX + 158, duration: 360, ease: anim.easeInOutCubic,
    onUpdate: function(x) {
      stage.token.setAttribute("x", x);
      stage.tokenTxt.setAttribute("x", x + 36);
    },
    onComplete: function() {
      stage.nodes.root.setAttribute("fill", "#bfdbfe");
      stage.status.textContent = "Root page chooses child pointer for country='US'.";
    }
  });
  tl.add({
    from: 58, to: 126, duration: 360, ease: anim.easeInOutCubic,
    onUpdate: function(y) {
      stage.token.setAttribute("y", y);
      stage.tokenTxt.setAttribute("y", y + 16);
    },
    onComplete: function() {
      stage.nodes.internal.setAttribute("fill", "#bfdbfe");
      stage.status.textContent = "Internal page narrows to the US key range.";
    }
  });
  tl.add({
    from: 126, to: 206, duration: 380, ease: anim.easeInOutCubic,
    onUpdate: function(y) {
      stage.token.setAttribute("y", y);
      stage.tokenTxt.setAttribute("y", y + 16);
    },
    onComplete: function() {
      stage.nodes.leaf.setAttribute("fill", "#c7d2fe");
      stage.status.textContent = "Reached leaf level: now scan contiguous US entries.";
    }
  });

  tl.mark("Scan leaf range");
  tl.call(function() {
    phase.textContent = "Phase 2/3 — scan matching leaf entries and collect row-id pointers";
  });
  for (var i = 0; i < n; i++) {
    (function(idx) {
      tl.call(function() {
        stage.rows[idx].bg.setAttribute("fill", "#93c5fd");
        stage.rows[idx].bg.setAttribute("stroke", "#3b82f6");
        stage.status.textContent = "Leaf hit " + (idx + 1) + "/" + n + ": row_id=" + stage.rows[idx].data.rid + " collected";
      });
      tl.delay(260);
    })(i);
  }

  tl.mark("Follow row-id to table");
  tl.call(function() {
    phase.textContent = covering
      ? "Phase 3/3 — covering index: skip clustered table fetches"
      : "Phase 3/3 — follow each row-id to clustered table rows";
  });

  if (!covering) {
    for (var j = 0; j < n; j++) {
      (function(ix) {
        var rowY = stage.rows[ix].y + 10;
        var outY = stage.tblY + 64 + ix * 34;
        tl.call(function() {
          stage.probeDot.setAttribute("opacity", 0.95);
          stage.probeDot.setAttribute("cx", 402);
          stage.probeDot.setAttribute("cy", rowY);
        });
        tl.add({
          from: 402, to: 446, duration: 180, ease: anim.easeInOutCubic,
          onUpdate: function(x) {
            stage.probeDot.setAttribute("cx", x);
          }
        });
        tl.add({
          from: rowY, to: outY, duration: 220, ease: anim.easeInOutCubic,
          onUpdate: function(y) {
            stage.probeDot.setAttribute("cy", y);
          },
          onComplete: function() {
            var d = stage.rows[ix].data;
            stage.fetched[ix].textContent = "row_id=" + d.rid + " -> " + d.name + " (" + d.city + ")";
            stage.status.textContent = "Fetched row_id=" + d.rid + " from clustered table";
            stage.probeDot.setAttribute("opacity", 0);
          }
        });
        tl.delay(120);
      })(j);
    }
  } else {
    tl.call(function() {
      stage.status.textContent = "Covering lookup: all needed columns are in leaf entries, so row-id fetches are skipped.";
    });
    tl.delay(520);
  }

  tl.mark("Done");
  tl.call(function() {
    phase.textContent = "Done — one key value can map to many row-id pointers";
    stage.status.textContent = covering
      ? "Final: B+tree descent + leaf scan only (covering path)."
      : "Final: B+tree descent + leaf scan + clustered row fetches.";
  });
  return tl;
}

function buildCurrentTimeline() { return buildTimeline(); }
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

function renderChart(selectivityPct, currentRows) {
  var sel = Math.max(0.001, Math.min(1, selectivityPct / 100));
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Rows in table", yLabel: "Rows/entries touched",
    curves: [
      { label: "Non-unique lookup (non-covering)", color: "#1d4ed8",
        fn: function(n) { return (Math.log(Math.max(2, n)) / Math.log(2)) + (n * sel) + (n * sel); } },
      { label: "Non-unique lookup (covering)", color: "#059669",
        fn: function(n) { return (Math.log(Math.max(2, n)) / Math.log(2)) + (n * sel); } },
      { label: "Full table scan", color: "#b45309",
        fn: function(n) { return n; } }
    ],
    current: { x: currentRows },
    xSlider: "rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(1000, Math.min(10000000, xVal / 1000) * 1000)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = lookupCost(c.rows, c.selectivity, !!c.covering);
  document.getElementById("out-height").textContent = String(cost.treeHeight);
  document.getElementById("out-descent").textContent = teachRuntime.formatInt(cost.descentReads);
  document.getElementById("out-match").textContent = teachRuntime.formatInt(cost.matches);
  document.getElementById("out-index").textContent = teachRuntime.formatInt(cost.indexReads);
  document.getElementById("out-fetch").textContent = teachRuntime.formatInt(cost.rowFetches);
  document.getElementById("out-total").textContent = teachRuntime.formatInt(cost.totalReads);
  document.getElementById("out-explanation").textContent = cost.covering
    ? "Covering non-unique lookup: the engine descends the B+tree and scans matching leaf entries, then returns rows directly from index payload."
    : "Non-unique lookup descends the B+tree, scans " + teachRuntime.formatInt(cost.matches) +
      " matching leaf entries, then follows each row-id pointer to fetch clustered rows (double-touch path).";
  buildStage();
  resetStage();
  renderChart(c.selectivity, c.rows);
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
