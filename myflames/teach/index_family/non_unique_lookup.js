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

// The query the stage runs, for each choice of the Covering input.
var QUERY_SQL = {
  noncovering: "SELECT id, name, country\nFROM users\nWHERE country = 'US';",
  covering: "SELECT id, country\nFROM users\nWHERE country = 'US';"
};

// A deck of idx_users_country leaf entries: the 'US' entries in (country, id)
// order, then the first entry of the next country, where the lookup stops.
// The entry holds only country and id; name and city live in the row.
var INDEX_KEYS = [
  {key: "US", id: 102, name: "Ben", city: "Austin"},
  {key: "US", id: 118, name: "Diego", city: "Miami"},
  {key: "US", id: 131, name: "Farah", city: "Seattle"},
  {key: "US", id: 165, name: "Ivan", city: "Boston"},
  {key: "US", id: 188, name: "Noah", city: "Dallas"},
  {key: "US", id: 204, name: "Zara", city: "Denver"}
];
var NEXT_KEY = {key: "UY", id: 15};

// Whole numbers with thousands separators, so a caption never shows "20.0K".
function fmtFull(n) {
  return Math.round(n).toLocaleString("en-US");
}

function isCovering() {
  return String(teachRuntime.readControls().covering) === "true";
}

function buildStage() {
  var svg = document.getElementById("lookup-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var idxX = 20, idxY = 42, idxW = 390, idxH = 350;
  var tblX = 440, tblY = 42, tblW = 400, tblH = 350;

  var idxLbl = anim.svgEl("text", {
    x: idxX, y: 24, "font-size": 13, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  idxLbl.textContent = "secondary B+tree: idx_users_country (country, id)";
  svg.appendChild(idxLbl);

  svg.appendChild(anim.svgEl("rect", {
    x: idxX, y: idxY, width: idxW, height: idxH, rx: 8, ry: 8,
    fill: "var(--svg-token-soft)", stroke: "var(--svg-token)", "stroke-width": 1.5
  }));

  var tableLbl = anim.svgEl("text", {
    x: tblX, y: 24, "font-size": 13, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  tableLbl.textContent = "clustered index (PRIMARY): rows found by id";
  svg.appendChild(tableLbl);

  var tableRect = anim.svgEl("rect", {
    x: tblX, y: tblY, width: tblW, height: tblH, rx: 8, ry: 8,
    fill: "var(--svg-build-soft)", stroke: "var(--svg-build-soft)", "stroke-width": 1.5
  });
  svg.appendChild(tableRect);

  var scanArrow = anim.svgEl("path", {
    d: "M400,220 C412,220 416,220 430,220 M423,214 L430,220 L423,226",
    stroke: "var(--svg-node-stroke)", "stroke-width": 2, fill: "none"
  });
  svg.appendChild(scanArrow);

  var root = anim.svgEl("rect", {
    x: idxX + 145, y: idxY + 16, width: 100, height: 36, rx: 7, ry: 7,
    fill: "var(--svg-node-bg)", stroke: "var(--svg-node-stroke)", "stroke-width": 1.5
  });
  svg.appendChild(root);
  var rootTxt = anim.svgEl("text", {
    x: idxX + 195, y: idxY + 38, "text-anchor": "middle",
    "font-size": 10.5, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  rootTxt.textContent = "root page";
  svg.appendChild(rootTxt);

  var internal = anim.svgEl("rect", {
    x: idxX + 115, y: idxY + 86, width: 160, height: 38, rx: 7, ry: 7,
    fill: "var(--svg-node-bg)", stroke: "var(--svg-node-stroke)", "stroke-width": 1.5
  });
  svg.appendChild(internal);
  var internalTxt = anim.svgEl("text", {
    x: idxX + 195, y: idxY + 110, "text-anchor": "middle",
    "font-size": 10.5, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  internalTxt.textContent = "internal page: country ranges";
  svg.appendChild(internalTxt);

  svg.appendChild(anim.svgEl("line", {
    x1: idxX + 195, y1: idxY + 52, x2: idxX + 195, y2: idxY + 86,
    stroke: "var(--svg-node-stroke)", "stroke-width": 2
  }));

  var leafRect = anim.svgEl("rect", {
    x: idxX + 24, y: idxY + 150, width: idxW - 48, height: 192, rx: 7, ry: 7,
    fill: "var(--svg-node-bg)", stroke: "var(--svg-node-stroke)", "stroke-width": 1.5
  });
  svg.appendChild(leafRect);
  var leafLbl = anim.svgEl("text", {
    x: idxX + 30, y: idxY + 170, "font-size": 11, "font-weight": 700, fill: "var(--svg-token-ink)"
  });
  leafLbl.textContent = "leaf pages: entries (country, id) in key order";
  svg.appendChild(leafLbl);

  svg.appendChild(anim.svgEl("line", {
    x1: idxX + 195, y1: idxY + 124, x2: idxX + 195, y2: idxY + 150,
    stroke: "var(--svg-node-stroke)", "stroke-width": 2
  }));

  var token = anim.svgEl("rect", {
    x: idxX + 34, y: idxY + 16, width: 72, height: 24, rx: 12, ry: 12,
    fill: "var(--svg-build)", stroke: "var(--svg-build)", "stroke-width": 1.5, opacity: 0.96
  });
  svg.appendChild(token);
  var tokenTxt = anim.svgEl("text", {
    x: idxX + 70, y: idxY + 32, "text-anchor": "middle",
    "font-size": 9.5, "font-weight": 700, fill: "var(--svg-on-solid)"
  });
  tokenTxt.textContent = "country='US'";
  svg.appendChild(tokenTxt);

  var rows = [];
  var fetched = [];
  var entryStep = 22;
  for (var i = 0; i <= INDEX_KEYS.length; i++) {
    var isNext = (i === INDEX_KEYS.length);
    var data = isNext ? NEXT_KEY : INDEX_KEYS[i];
    var y = idxY + 180 + i * entryStep;
    var r = anim.svgEl("rect", {
      x: idxX + 34, y: y, width: idxW - 72, height: 18, rx: 5, ry: 5,
      fill: "var(--svg-token-soft)", stroke: "var(--svg-token-soft)", "stroke-width": 1
    });
    svg.appendChild(r);
    var t = anim.svgEl("text", {
      x: idxX + 42, y: y + 13, "font-size": 10, "font-weight": 600,
      fill: isNext ? "var(--svg-ink-muted)" : "var(--svg-token-ink)"
    });
    t.textContent = "country=" + data.key + ", id=" + data.id +
      (isNext ? "   (the next country)" : "");
    svg.appendChild(t);
    var mark = anim.svgEl("text", {
      x: idxX + idxW - 46, y: y + 13, "text-anchor": "end",
      "font-size": 10, "font-weight": 700, fill: "var(--svg-ok)"
    });
    mark.textContent = "";
    svg.appendChild(mark);
    rows.push({bg: r, txt: t, mark: mark, data: data, y: y, isNext: isNext});

    if (!isNext) {
      var out = anim.svgEl("text", {
        x: tblX + 14, y: tblY + 70 + i * 34, "font-size": 11, "font-weight": 600, fill: "var(--svg-build-ink)"
      });
      out.textContent = "";
      svg.appendChild(out);
      fetched.push(out);
    }
  }

  var tableHdr = anim.svgEl("text", {
    x: tblX + 14, y: tblY + 36, "font-size": 11, "font-weight": 700, fill: "var(--svg-build-ink)"
  });
  tableHdr.textContent = "Each id is a lookup from the root page to a leaf page:";
  svg.appendChild(tableHdr);

  var probeDot = anim.svgEl("circle", {
    cx: 420, cy: idxY + 220, r: 5.5, fill: "var(--svg-build)", stroke: "var(--svg-build)",
    "stroke-width": 1.5, opacity: 0
  });
  svg.appendChild(probeDot);

  var status = anim.svgEl("text", {
    x: W / 2, y: H - 18, "text-anchor": "middle",
    "font-size": 12.5, "font-weight": 600, fill: "var(--svg-ink)"
  });
  status.textContent = "";
  svg.appendChild(status);

  stage = {
    svg: svg,
    rows: rows,
    fetched: fetched,
    tableHdr: tableHdr,
    status: status,
    token: token,
    tokenTxt: tokenTxt,
    probeDot: probeDot,
    nodes: {root: root, internal: internal, leaf: leafRect},
    idxX: idxX,
    idxY: idxY,   // 2026-04-25: was missing — caused cy=NaN when
                  // resetStage() did setAttribute("cy", idxY + 220).
                  // Found by the headless animation harness.
    tblX: tblX,
    tblY: tblY
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.rows.length; i++) {
    stage.rows[i].bg.setAttribute("fill", "var(--svg-token-soft)");
    stage.rows[i].bg.setAttribute("stroke", "var(--svg-token-soft)");
    stage.rows[i].mark.textContent = "";
  }
  for (var j = 0; j < stage.fetched.length; j++) stage.fetched[j].textContent = "";
  var nodes = [stage.nodes.root, stage.nodes.internal, stage.nodes.leaf];
  for (var k = 0; k < nodes.length; k++) {
    nodes[k].setAttribute("fill", "var(--svg-node-bg)");
    nodes[k].setAttribute("stroke", "var(--svg-node-stroke)");
  }
  stage.tableHdr.textContent = isCovering()
    ? "Covering: id and country come from the index, no lookups here"
    : "Each id is a lookup from the root page to a leaf page:";
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

// The page the token just reached turns yellow; the one it left keeps the
// visited orange, as in the btree lesson.
function arrive(node, prevNode) {
  if (prevNode) {
    prevNode.setAttribute("fill", "var(--svg-token-soft)");
    prevNode.setAttribute("stroke", "var(--svg-token)");
  }
  node.setAttribute("fill", "var(--svg-highlight)");
  node.setAttribute("stroke", "var(--svg-highlight-stroke)");
  anim.pulse(node, 3, 1.5, 360);
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var c = teachRuntime.readControls();
  var covering = isCovering();
  var cost = lookupCost(c.rows, c.selectivity, covering);
  var drawn = INDEX_KEYS.length;
  var scale = fmtFull(cost.matches) + " of " + fmtFull(cost.rows) +
    " users match at " + c.selectivity + "%";

  // ---------- Descend idx_users_country: one step per page read ----------
  tl.mark("Descend B+tree");
  tl.beat("root");
  tl.call(function() {
    phase.textContent = "idx_users_country, page read 1: root page, picks the child whose key range holds 'US'";
    stage.status.textContent = "Looking for country = 'US' in idx_users_country.";
  });
  tl.add({
    from: stage.idxX + 34, to: stage.idxX + 158, duration: 360, ease: anim.easeInOutCubic,
    onUpdate: function(x) {
      stage.token.setAttribute("x", x);
      stage.tokenTxt.setAttribute("x", x + 36);
    },
    onComplete: function() { arrive(stage.nodes.root, null); }
  });
  tl.delay(160);

  tl.beat("internal");
  tl.call(function() {
    phase.textContent = "idx_users_country, page read 2: internal page, narrows to the key range that holds 'US'";
  });
  tl.add({
    from: 58, to: 126, duration: 360, ease: anim.easeInOutCubic,
    onUpdate: function(y) {
      stage.token.setAttribute("y", y);
      stage.tokenTxt.setAttribute("y", y + 16);
    },
    onComplete: function() { arrive(stage.nodes.internal, stage.nodes.root); }
  });
  tl.delay(160);

  tl.beat("leaf");
  tl.call(function() {
    phase.textContent = "idx_users_country, page read 3: leaf page, first entry with country = 'US' is (US, id 102)";
  });
  tl.add({
    from: 126, to: 170, duration: 380, ease: anim.easeInOutCubic,
    onUpdate: function(y) {
      stage.token.setAttribute("y", y);
      stage.tokenTxt.setAttribute("y", y + 16);
    },
    onComplete: function() {
      arrive(stage.nodes.leaf, stage.nodes.internal);
      stage.status.textContent = "The 'US' entries sit next to each other, ordered by id.";
    }
  });
  tl.delay(200);

  // ---------- Read each 'US' entry; non-covering looks the row up ----------
  tl.mark("Read matching entries");
  for (var i = 0; i < drawn; i++) {
    (function(idx) {
      var row = stage.rows[idx];
      var d = row.data;
      tl.beat(covering ? "row-covered" : "row");
      tl.call(function() {
        if (idx === 0) {
          stage.nodes.leaf.setAttribute("fill", "var(--svg-token-soft)");
          stage.nodes.leaf.setAttribute("stroke", "var(--svg-token)");
        } else {
          var before = stage.rows[idx - 1];
          before.bg.setAttribute("fill", "var(--svg-ok-soft)");
          before.bg.setAttribute("stroke", "var(--svg-ok)");
        }
        row.bg.setAttribute("fill", "var(--svg-highlight)");
        row.bg.setAttribute("stroke", "var(--svg-token)");
        phase.textContent = covering
          ? "Entry " + (idx + 1) + " of " + drawn + " drawn: (US, id " + d.id +
            ") already holds id and country, so the row is sent from the index"
          : "Entry " + (idx + 1) + " of " + drawn + " drawn: (US, id " + d.id +
            ") lacks name, so InnoDB looks id " + d.id + " up in the clustered index";
      });
      if (!covering) {
        var rowY = row.y + 9;
        var outY = stage.tblY + 64 + idx * 34;
        tl.call(function() {
          stage.probeDot.setAttribute("opacity", 0.95);
          stage.probeDot.setAttribute("cx", 402);
          stage.probeDot.setAttribute("cy", rowY);
        });
        tl.add({
          from: 402, to: 446, duration: 200, ease: anim.easeInOutCubic,
          onUpdate: function(x) { stage.probeDot.setAttribute("cx", x); }
        });
        tl.add({
          from: rowY, to: outY, duration: 260, ease: anim.easeInOutCubic,
          onUpdate: function(y) { stage.probeDot.setAttribute("cy", y); },
          onComplete: function() {
            stage.fetched[idx].textContent = "id " + d.id + " → " + d.name + ", " + d.city;
            stage.probeDot.setAttribute("opacity", 0);
          }
        });
      } else {
        tl.delay(260);
      }
      tl.call(function() {
        row.mark.textContent = "sent ✓";
        stage.status.textContent = covering
          ? (idx + 1) + " of " + drawn + " drawn entries sent from the index, 0 clustered index lookups"
          : (idx + 1) + " of " + drawn + " drawn entries read, " + (idx + 1) + " clustered index lookups";
      });
      tl.delay(200);
    })(i);
  }

  // ---------- The next entry has another key: stop ----------
  tl.beat("stop");
  tl.mark("Stop at the next key");
  tl.call(function() {
    var last = stage.rows[drawn - 1];
    last.bg.setAttribute("fill", "var(--svg-ok-soft)");
    last.bg.setAttribute("stroke", "var(--svg-ok)");
    var next = stage.rows[drawn];
    next.bg.setAttribute("fill", "var(--svg-node-muted)");
    next.bg.setAttribute("stroke", "var(--svg-node-stroke)");
    next.mark.textContent = "stop";
    next.mark.setAttribute("fill", "var(--svg-ink-muted)");
    phase.textContent = "✓ The next entry is (UY, id 15): country is no longer 'US', so the lookup ends. " +
      "At this table's size " + scale + ": " +
      (covering ? "0 clustered index lookups."
                : fmtFull(cost.matches) + " clustered index lookups, one per entry.");
    stage.status.textContent = covering
      ? "Done: descent + leaf entries only (covering)."
      : "Done: descent + leaf entries + one clustered index lookup per entry.";
  });
  tl.delay(300);
  return tl;
}

function buildCurrentTimeline() { return buildTimeline(); }
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(selectivityPct, currentRows) {
  var sel = Math.max(0.001, Math.min(1, selectivityPct / 100));
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e7,
    xLabel: "Rows in table", yLabel: "Rows/entries touched",
    curves: [
      { label: "Non-unique lookup (non-covering)", color: "var(--svg-token)",
        fn: function(n) { return (Math.log(Math.max(2, n)) / Math.log(2)) + (n * sel) + (n * sel); } },
      { label: "Non-unique lookup (covering)", color: "var(--svg-ok)",
        fn: function(n) { return (Math.log(Math.max(2, n)) / Math.log(2)) + (n * sel); } },
      { label: "Full table scan", color: "var(--svg-build)",
        fn: function(n) { return n; } }
    ],
    current: { x: currentRows },
    xSlider: "rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(1000, Math.min(10000000, xVal / 1000) * 1000)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  // The select's value is the text "true" or "false"; "false" is a
  // non-empty string, so it must be compared, not cast to a boolean.
  var covering = isCovering();
  var cost = lookupCost(c.rows, c.selectivity, covering);
  document.getElementById("out-height").textContent = cost.treeHeight + " levels";
  document.getElementById("out-descent").textContent = teachRuntime.formatInt(cost.descentReads) + " pages";
  document.getElementById("out-match").textContent = teachRuntime.formatInt(cost.matches) + " entries";
  document.getElementById("out-index").textContent = teachRuntime.formatInt(cost.indexReads);
  document.getElementById("out-fetch").textContent = teachRuntime.formatInt(cost.rowFetches);
  document.getElementById("out-total").textContent = teachRuntime.formatInt(cost.totalReads);
  document.getElementById("out-explanation").textContent = cost.covering
    ? "Covering non-unique lookup: InnoDB descends idx_users_country, reads the " +
      teachRuntime.formatInt(cost.matches) + " matching entries and returns id and country straight from them."
    : "Non-unique lookup: InnoDB descends idx_users_country, reads the " + teachRuntime.formatInt(cost.matches) +
      " matching entries and, for each one, looks its id up in the clustered index to get name: " +
      teachRuntime.formatInt(cost.matches) + " lookups, each one from the root page to a leaf page.";
  var sql = document.querySelector(".query-sql");
  if (sql) sql.textContent = covering ? QUERY_SQL.covering : QUERY_SQL.noncovering;
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
