"""Lesson: Unique Key Lookup (single-row index lookup).

Shares the visual vocabulary of the flagship ``btree`` lesson: a real
B+tree (root / internal / leaf pages) with a red diamond query token
descending level by level, yellow page-pulses on arrival, and a dashed
orange PK-hop arrow when the lookup is non-covering. The UNIQUE
special-case is called out by a small chip that makes it obvious that
exactly one leaf entry can match — not a range.
"""
from .. import _html
from .._cost_model import (
    INNODB_PAGE_OVERHEAD_BYTES,
    INNODB_CHILD_POINTER_BYTES,
    INNODB_PAGE_SIZE_DEFAULT,
)


_LESSON_JS_TEMPLATE = r"""
// Constants pinned to myflames/teach/_cost_model.py.
var INNODB_PAGE_OVERHEAD_BYTES = %d;
var INNODB_CHILD_POINTER_BYTES = %d;
var INNODB_PAGE_SIZE_DEFAULT = %d;
// Log-scale row counts aligned with the btree lesson's ROW_SCALE.
var ROW_SCALE = [1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000];

// ---- cost model (mirrors btree.py, specialised to single-row lookup) ----
function innodbFanout(keySize, pageSize) {
  var usable = pageSize - INNODB_PAGE_OVERHEAD_BYTES;
  var entry = keySize + INNODB_CHILD_POINTER_BYTES;
  return Math.max(2, Math.floor(usable / entry));
}
function innodbTreeHeight(rows, fanOut) {
  if (rows <= 0) return 2;
  if (rows <= fanOut) return 2;
  return Math.max(2, Math.ceil(Math.log(rows) / Math.log(fanOut)));
}
function uniqueLookupCost(rows, covering) {
  // Key size fixed at 8 B (BIGINT PK / UNIQUE INT) — this lesson is about
  // the unique-lookup flow, not the fan-out knob that btree.py already
  // teaches. Page size is InnoDB default 16 KiB.
  var fanOut = innodbFanout(8, INNODB_PAGE_SIZE_DEFAULT);
  var height = innodbTreeHeight(rows, fanOut);
  var indexReads = height;
  var rowFetches = covering ? 0 : 1;
  var total = indexReads + rowFetches;
  var explanation = covering
    ? "Covering unique lookup: one descent of " + height + " levels. Every column the query asked for is already in the unique-index leaf — no clustered-tree visit."
    : "Non-covering unique lookup: " + height + " levels on the unique index, then one clustered-row fetch using the PK stored in the unique leaf. " + (height + 1) + " pages total.";
  return {
    fanOut: fanOut,
    height: height,
    indexReads: indexReads,
    rowFetches: rowFetches,
    total: total,
    explanation: explanation
  };
}

// ---- concrete labels on the lookup path ----
// The unique index for this lesson is uq_users_id; the token is looking
// for id=42, which matches at most ONE leaf entry.
var PATH_LABELS_UNIQ = [
  "keys 1..1M",
  "keys 1..100K",
  "keys 30..50",
  "id = 42"
];
var PATH_LABELS_CLUS = [
  "keys 1..1M",
  "keys 1..100K",
  "keys 30..50",
  "#42 alice@ex"
];

function getUniqLabel(levelIdx, levelsShown) {
  var isLeaf = (levelIdx === levelsShown - 1);
  if (isLeaf) return "id = 42";
  return PATH_LABELS_UNIQ[Math.min(levelIdx, PATH_LABELS_UNIQ.length - 1)];
}
function getClusLabel(levelIdx, levelsShown) {
  var isLeaf = (levelIdx === levelsShown - 1);
  if (isLeaf) return "#42 alice@ex";
  return PATH_LABELS_CLUS[Math.min(levelIdx, PATH_LABELS_CLUS.length - 1)];
}

// ---- tree rendering (adapted from btree.py) -------------------------------
var W = 800, H = 400;
var treeState = {
  height: 3, traversals: 1, nodes: [[],[]], token: null,
  tokenLabel: null,
  pkLink: null, pkLinkLabel: null,
  uniqueChip: null, coveringBadge: null,
  edges: [[],[]]
};

function buildTrees(height, traversals) {
  var svg = document.getElementById("unique-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  treeState.height = height;
  treeState.traversals = traversals;
  treeState.nodes = [[], []];
  treeState.edges = [[], []];
  treeState.uniqueChip = null;
  treeState.coveringBadge = null;

  var treeWidth = W / traversals;
  var levels = Math.min(height, 4);
  var levelY = [70, 150, 230, 310];
  var NODE_W = 52, NODE_H = 30;

  // Arrow marker defs (shared across both trees).
  var globalDefs = anim.svgEl("defs");
  var treeArrow = anim.svgEl("marker", {
    id: "uqTreeArrow", viewBox: "0 0 10 10",
    refX: "9", refY: "5",
    markerWidth: "5", markerHeight: "5",
    orient: "auto"
  });
  treeArrow.appendChild(anim.svgEl("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "#d1d5db"}));
  globalDefs.appendChild(treeArrow);
  var treeArrowHi = anim.svgEl("marker", {
    id: "uqTreeArrowHi", viewBox: "0 0 10 10",
    refX: "9", refY: "5",
    markerWidth: "5", markerHeight: "5",
    orient: "auto"
  });
  treeArrowHi.appendChild(anim.svgEl("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "#f97316"}));
  globalDefs.appendChild(treeArrowHi);
  svg.appendChild(globalDefs);

  var treeNames = (traversals === 2)
    ? ["Unique B+tree — uq_users_id", "Clustered B+tree — users (PK)"]
    : ["Unique B+tree — uq_users_id (covering)"];

  for (var t = 0; t < traversals; t++) {
    var ox = t * treeWidth;

    var heading = anim.svgEl("text", {
      x: ox + treeWidth/2, y: 32, "text-anchor": "middle",
      "font-size": 13, "font-weight": 700, fill: "#1f2937"
    });
    heading.textContent = treeNames[t];
    svg.appendChild(heading);

    // Pass 1: lay out nodes.
    var treeNodes = [];
    for (var lv = 0; lv < levels; lv++) {
      var nodesAtLv = Math.min(Math.pow(2, lv), 8);
      var y = levelY[lv];
      var levelNodes = [];
      for (var n = 0; n < nodesAtLv; n++) {
        var x = ox + ((n + 0.5) * (treeWidth / nodesAtLv)) - NODE_W / 2;
        var isOnPath = (n === Math.floor(nodesAtLv / 2));
        levelNodes.push({
          x: x, y: y, cx: x + NODE_W / 2, cy: y + NODE_H / 2,
          onPath: isOnPath, rect: null, label: null
        });
      }
      treeNodes.push(levelNodes);
    }

    // Pass 2: edges (parent -> child).
    var treeEdges = [];
    for (var lv2 = 0; lv2 < levels - 1; lv2++) {
      var parents = treeNodes[lv2];
      var children = treeNodes[lv2 + 1];
      for (var p = 0; p < parents.length; p++) {
        var parentNode = parents[p];
        var cStart = p * 2;
        var cEnd = Math.min(cStart + 2, children.length);
        for (var c = cStart; c < cEnd; c++) {
          var childNode = children[c];
          var edgeOnPath = parentNode.onPath && childNode.onPath;
          var line = anim.svgEl("line", {
            x1: parentNode.cx, y1: parentNode.cy + NODE_H / 2,
            x2: childNode.cx,  y2: childNode.cy - NODE_H / 2,
            stroke: edgeOnPath ? "#d1d5db" : "#e5e7eb",
            "stroke-width": edgeOnPath ? 1.5 : 0.8,
            "marker-end": edgeOnPath ? "url(#uqTreeArrow)" : ""
          });
          svg.appendChild(line);
          treeEdges.push({ line: line, onPath: edgeOnPath, from: lv2, to: lv2 + 1 });
        }
      }
    }
    treeState.edges[t] = treeEdges;

    // Pass 3: node rectangles and labels on top.
    for (var lv3 = 0; lv3 < levels; lv3++) {
      for (var n3 = 0; n3 < treeNodes[lv3].length; n3++) {
        var nd = treeNodes[lv3][n3];
        var rect = anim.svgEl("rect", {
          x: nd.x, y: nd.y, width: NODE_W, height: NODE_H, rx: 4, ry: 4,
          fill: "#f3f4f6", stroke: "#9ca3af", "stroke-width": 1
        });
        svg.appendChild(rect);
        var lvLbl = anim.svgEl("text", {
          x: nd.cx, y: nd.cy - 2, "text-anchor": "middle",
          "font-size": 9, "font-weight": 600, fill: "#6b7280"
        });
        lvLbl.textContent = (lv3 === 0) ? "root" : (lv3 === levels - 1 ? "leaf" : "L" + lv3);
        svg.appendChild(lvLbl);
        var dataLbl = anim.svgEl("text", {
          x: nd.cx, y: nd.cy + 10, "text-anchor": "middle",
          "font-size": 7, "font-weight": 700, fill: "#0f172a", opacity: 0
        });
        svg.appendChild(dataLbl);
        nd.rect = rect;
        nd.label = lvLbl;
        nd.dataLabel = dataLbl;
      }
    }
    treeState.nodes[t] = treeNodes;

    if (height > 4) {
      var more = anim.svgEl("text", {
        x: ox + treeWidth/2, y: 370, "text-anchor": "middle",
        "font-size": 11, fill: "#9ca3af", "font-style": "italic"
      });
      more.textContent = "(" + height + " levels total — showing top 4)";
      svg.appendChild(more);
    }
  }

  // ---- PK-hop dashed arrow (non-covering only) ----
  treeState.pkLink = null;
  treeState.pkLinkLabel = null;
  if (traversals === 2) {
    var uqLeaf = treeState.nodes[0][levels - 1].find(function(n) { return n.onPath; });
    var clusLeaf = treeState.nodes[1][levels - 1].find(function(n) { return n.onPath; });
    if (uqLeaf && clusLeaf) {
      var sx = uqLeaf.cx + 22;
      var sy = uqLeaf.cy;
      var ex = clusLeaf.cx;
      var ey = clusLeaf.cy;
      var cx = (sx + ex) / 2;
      var cy = Math.min(sy, ey) - 50;
      var pathD = "M " + sx + "," + sy +
                  " Q " + cx + "," + cy + " " + ex + "," + ey;
      var pkArrowDefs = anim.svgEl("defs");
      var pkMarker = anim.svgEl("marker", {
        id: "uqPkArrow", viewBox: "0 0 10 10",
        refX: "9", refY: "5",
        markerWidth: "6", markerHeight: "6",
        orient: "auto"
      });
      pkMarker.appendChild(anim.svgEl("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "#f97316"}));
      pkArrowDefs.appendChild(pkMarker);
      svg.appendChild(pkArrowDefs);
      var pkLink = anim.svgEl("path", {
        d: pathD,
        fill: "none",
        stroke: "#f97316",
        "stroke-width": 2,
        "stroke-dasharray": "5 3",
        "marker-end": "url(#uqPkArrow)",
        opacity: 0
      });
      svg.appendChild(pkLink);
      treeState.pkLink = pkLink;

      var pkLabel = anim.svgEl("text", {
        x: cx, y: cy - 4, "text-anchor": "middle",
        "font-size": 11, "font-weight": 700, fill: "#c2410c",
        opacity: 0
      });
      pkLabel.textContent = "PK=42 → clustered row";
      svg.appendChild(pkLabel);
      treeState.pkLinkLabel = pkLabel;
    }
  }

  // ---- UNIQUE chip near the unique-index leaf ----
  var uniqLeaf = treeState.nodes[0][levels - 1].find(function(n) { return n.onPath; });
  if (uniqLeaf) {
    var chipX = uniqLeaf.x - 6;
    var chipY = uniqLeaf.y + NODE_H + 8;
    var chipW = 150, chipH = 22;
    // Keep the chip on-screen: if it would overflow the left edge, shift right.
    if (chipX < 6) chipX = 6;
    // If it would overflow the right edge of this tree's column, shift left.
    var treeRight = (traversals === 2) ? (W / 2) - 6 : W - 6;
    if (chipX + chipW > treeRight) chipX = Math.max(6, treeRight - chipW);
    var chipGroup = anim.svgEl("g", {opacity: 0});
    var chipRect = anim.svgEl("rect", {
      x: chipX, y: chipY, width: chipW, height: chipH, rx: 11, ry: 11,
      fill: "#ecfeff", stroke: "#0e7490", "stroke-width": 1.2
    });
    chipGroup.appendChild(chipRect);
    var chipText = anim.svgEl("text", {
      x: chipX + chipW / 2, y: chipY + 15, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: "#155e75"
    });
    chipText.textContent = "UNIQUE — at most 1 row";
    chipGroup.appendChild(chipText);
    svg.appendChild(chipGroup);
    treeState.uniqueChip = chipGroup;
  }

  // ---- Covering badge on the unique-index leaf (only when traversals===1) ----
  if (traversals === 1 && uniqLeaf) {
    var badgeX = uniqLeaf.x + 6;
    var badgeY = uniqLeaf.y - 22;
    var badgeW = 78, badgeH = 16;
    var badgeGroup = anim.svgEl("g", {opacity: 0});
    var badgeRect = anim.svgEl("rect", {
      x: badgeX, y: badgeY, width: badgeW, height: badgeH, rx: 8, ry: 8,
      fill: "#ecfdf5", stroke: "#059669", "stroke-width": 1.2
    });
    badgeGroup.appendChild(badgeRect);
    var badgeText = anim.svgEl("text", {
      x: badgeX + badgeW / 2, y: badgeY + 11, "text-anchor": "middle",
      "font-size": 9, "font-weight": 700, fill: "#047857"
    });
    badgeText.textContent = "Index-only ✓";
    badgeGroup.appendChild(badgeText);
    svg.appendChild(badgeGroup);
    treeState.coveringBadge = badgeGroup;
  }

  // ---- Query token (red diamond) ----
  var token = anim.svgEl("polygon", {
    points: "0,-9 9,0 0,9 -9,0",
    fill: "#ff3d3d", stroke: "#991b1b", "stroke-width": 1.5,
    opacity: 0, transform: "translate(0,0)"
  });
  svg.appendChild(token);
  treeState.token = token;

  var tokenLabel = anim.svgEl("text", {
    x: 0, y: 16, "text-anchor": "middle",
    "font-size": 8, "font-weight": 700, fill: "#991b1b",
    opacity: 0
  });
  tokenLabel.textContent = "looking for id=42";
  svg.appendChild(tokenLabel);
  treeState.tokenLabel = tokenLabel;
}

function resetTreeColors() {
  for (var t = 0; t < treeState.nodes.length; t++) {
    for (var lv = 0; lv < treeState.nodes[t].length; lv++) {
      var lvlNodes = treeState.nodes[t][lv];
      for (var n = 0; n < lvlNodes.length; n++) {
        lvlNodes[n].rect.setAttribute("fill", "#f3f4f6");
        lvlNodes[n].rect.setAttribute("stroke", "#9ca3af");
        lvlNodes[n].rect.setAttribute("stroke-width", 1);
        var orig = (lv === 0) ? "root" : (lv === treeState.nodes[t].length - 1 ? "leaf" : "L" + lv);
        lvlNodes[n].label.textContent = orig;
        lvlNodes[n].label.setAttribute("fill", "#6b7280");
        lvlNodes[n].label.setAttribute("font-weight", 600);
        if (lvlNodes[n].dataLabel) {
          lvlNodes[n].dataLabel.textContent = "";
          lvlNodes[n].dataLabel.setAttribute("opacity", 0);
        }
      }
    }
    var edges = treeState.edges[t] || [];
    for (var e = 0; e < edges.length; e++) {
      var edge = edges[e];
      edge.line.setAttribute("stroke", edge.onPath ? "#d1d5db" : "#e5e7eb");
      edge.line.setAttribute("stroke-width", edge.onPath ? 1.5 : 0.8);
      edge.line.setAttribute("marker-end", edge.onPath ? "url(#uqTreeArrow)" : "");
    }
  }
  if (treeState.token) {
    treeState.token.setAttribute("opacity", 0);
    treeState.token.setAttribute("transform", "translate(0,0)");
  }
  if (treeState.tokenLabel) treeState.tokenLabel.setAttribute("opacity", 0);
  if (treeState.pkLink) treeState.pkLink.setAttribute("opacity", 0);
  if (treeState.pkLinkLabel) treeState.pkLinkLabel.setAttribute("opacity", 0);
  if (treeState.uniqueChip) treeState.uniqueChip.setAttribute("opacity", 0);
  if (treeState.coveringBadge) treeState.coveringBadge.setAttribute("opacity", 0);
}

function showNodeData(node, dataText) {
  if (!node || !node.dataLabel) return;
  node.dataLabel.textContent = dataText;
  node.dataLabel.setAttribute("opacity", 1);
  node.label.setAttribute("fill", "#0f172a");
  node.label.setAttribute("font-weight", 700);
}

// ---- timeline -----------------------------------------------------------
function buildDescentTimeline() {
  resetTreeColors();
  var tl = anim.timeline();
  var token = treeState.token;
  var tokenLabel = treeState.tokenLabel;
  var levelsShown = Math.min(treeState.height, 4);
  var phaseLabel = document.getElementById("phase-label");
  var covering = (treeState.traversals === 1);

  function addTreeDescent(treeIdx, fadeInFromAbove) {
    tl.call(function() {
      var rootNode = treeState.nodes[treeIdx][0].find(function(n) { return n.onPath; });
      if (fadeInFromAbove && rootNode) {
        token.setAttribute("transform", "translate(" + rootNode.cx + "," + (rootNode.cy - 40) + ")");
        token.setAttribute("opacity", 0);
      }
      tokenLabel.textContent = (treeIdx === 0) ? "looking for id=42" : "fetching row for id=42";
      if (treeIdx === 0) {
        phaseLabel.textContent = "Phase 1/2 — descending uq_users_id for WHERE id = 42 (UNIQUE — at most 1 row)";
      } else {
        phaseLabel.textContent = "Phase 2/2 — fetching clustered row #42 by PK";
      }
    });
    if (fadeInFromAbove) {
      tl.add({
        from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
        onUpdate: function(v) {
          token.setAttribute("opacity", v);
          tokenLabel.setAttribute("opacity", v);
        }
      });
    }

    for (var lv = 0; lv < levelsShown; lv++) {
      (function(levelIdx) {
        var activeNode = treeState.nodes[treeIdx][levelIdx].find(function(n) { return n.onPath; });
        if (!activeNode) return;
        var isLeaf = (levelIdx === levelsShown - 1);
        var labelFn = (treeIdx === 0) ? getUniqLabel : getClusLabel;
        var nodeDataLabel = labelFn(levelIdx, levelsShown);
        tl.call(function() {
          var which = covering
            ? "uq_users_id (covering)"
            : (treeIdx === 0 ? "uq_users_id" : "users PK (clustered)");
          phaseLabel.textContent = which + " — " +
            (levelIdx === 0 ? "root" : (isLeaf ? "leaf" : "level " + levelIdx)) +
            " — " + nodeDataLabel;
        });

        var currentPos;
        if (levelIdx === 0) {
          currentPos = { x: activeNode.cx, y: activeNode.cy - 40 };
        } else {
          var prev = treeState.nodes[treeIdx][levelIdx - 1].find(function(n) { return n.onPath; });
          currentPos = { x: prev.cx, y: prev.cy };
        }
        var skipStart = (levelIdx === 0 && !fadeInFromAbove);
        tl.add({
          from: skipStart ? { x: activeNode.cx, y: activeNode.cy } : currentPos,
          to: { x: activeNode.cx, y: activeNode.cy },
          duration: skipStart ? 160 : 480,
          ease: anim.easeInOutCubic,
          onUpdate: function(p) {
            token.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
            tokenLabel.setAttribute("x", p.x);
            tokenLabel.setAttribute("y", p.y + 16);
          },
          onComplete: function() {
            activeNode.rect.setAttribute("fill", "#fde725");
            activeNode.rect.setAttribute("stroke", "#ca8a04");
            anim.pulse(activeNode.rect, 3, 1, 360);
            showNodeData(activeNode, nodeDataLabel);
            if (levelIdx > 0) {
              var edges = treeState.edges[treeIdx] || [];
              for (var ei = 0; ei < edges.length; ei++) {
                var ed = edges[ei];
                if (ed.onPath && ed.from === levelIdx - 1 && ed.to === levelIdx) {
                  ed.line.setAttribute("stroke", "#f97316");
                  ed.line.setAttribute("stroke-width", 2.5);
                  ed.line.setAttribute("marker-end", "url(#uqTreeArrowHi)");
                }
              }
            }
          }
        });
        tl.delay(160);
      })(lv);
    }
  }

  // ---------- Phase 1: traverse index ----------
  tl.mark("Traverse index");
  addTreeDescent(0, true);

  // Reveal the UNIQUE chip on arrival at the unique leaf.
  tl.add({
    from: 0, to: 1, duration: 280, ease: anim.easeOutCubic,
    onUpdate: function(v) {
      if (treeState.uniqueChip) treeState.uniqueChip.setAttribute("opacity", v);
    }
  });
  tl.call(function() {
    phaseLabel.textContent = "Unique leaf reached — at most 1 row can match id=42 (no range scan)";
  });
  tl.delay(260);

  if (covering) {
    // ---------- Phase 2 (covering): Index-only stop ----------
    tl.mark("Covering — stop at leaf");
    tl.call(function() {
      phaseLabel.textContent = "Phase 2/2 — covering index holds every column the query needs — NO clustered fetch";
    });
    // Reveal the Index-only badge with a gentle overshoot so it feels
    // like a piece of evidence arriving.
    tl.add({
      from: 0, to: 1, duration: 420, ease: anim.easeOutBack,
      onUpdate: function(v) {
        if (treeState.coveringBadge) treeState.coveringBadge.setAttribute("opacity", v);
      }
    });
    // Token fades gracefully (follow-through): lookup is done at the leaf.
    tl.add({
      from: 1, to: 0, duration: 280, ease: anim.easeInCubic,
      onUpdate: function(v) {
        token.setAttribute("opacity", v);
        tokenLabel.setAttribute("opacity", v);
      }
    });
    tl.delay(200);
    tl.mark("Lookup complete");
    tl.call(function() {
      phaseLabel.textContent = "✓ Lookup complete — 1 row returned from the unique index (no table fetch)";
    });
  } else {
    // ---------- Phase 2 (non-covering): PK hop + clustered fetch ----------
    tl.mark("Fetch row");
    var uqLeaf = treeState.nodes[0][levelsShown - 1].find(function(n) { return n.onPath; });
    var clusLeaf = treeState.nodes[1][levelsShown - 1].find(function(n) { return n.onPath; });
    if (uqLeaf && clusLeaf) {
      tl.call(function() {
        phaseLabel.textContent = "Phase 2/2 — the unique leaf stored PK=42; follow the pointer to the clustered row";
      });
      tl.add({
        from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
        onUpdate: function(v) {
          if (treeState.pkLink) treeState.pkLink.setAttribute("opacity", v);
          if (treeState.pkLinkLabel) treeState.pkLinkLabel.setAttribute("opacity", v);
        }
      });
      var sx = uqLeaf.cx + 22;
      var sy = uqLeaf.cy;
      var ex = clusLeaf.cx;
      var ey = clusLeaf.cy;
      var cx = (sx + ex) / 2;
      var cy = Math.min(sy, ey) - 50;
      var pathFn = anim.path(sx, sy, cx, cy, ex, ey);
      tl.add({
        from: 0, to: 1, duration: 820, ease: anim.easeInOutCubic,
        onUpdate: function(t) {
          var p = pathFn(t);
          token.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
          tokenLabel.setAttribute("x", p.x);
          tokenLabel.setAttribute("y", p.y + 16);
        },
        onComplete: function() {
          clusLeaf.rect.setAttribute("fill", "#fde725");
          clusLeaf.rect.setAttribute("stroke", "#ca8a04");
          anim.pulse(clusLeaf.rect, 3, 1, 360);
          showNodeData(clusLeaf, "#42 alice@ex");
        }
      });
      tl.delay(240);
    }
    tl.mark("Lookup complete");
    tl.call(function() {
      phaseLabel.textContent = "✓ Lookup complete — 1 row fetched from the clustered tree (index + row-fetch)";
      if (tokenLabel) tokenLabel.setAttribute("opacity", 0);
    });
  }

  return tl;
}

function resetAnim() {
  resetTreeColors();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

// ---- complexity chart ---------------------------------------------------
function renderChart(covering, currentRows) {
  var fanOut = innodbFanout(8, INNODB_PAGE_SIZE_DEFAULT);
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 1000, xMax: 1e9,
    xLabel: "Rows in users table", yLabel: "Pages touched",
    curves: [
      { label: "Covering unique lookup: O(log n)",
        color: "#059669",
        fn: function(n) { return innodbTreeHeight(n, fanOut); } },
      { label: "Non-covering unique lookup: O(log n + 1)",
        color: "#2563eb",
        fn: function(n) { return innodbTreeHeight(n, fanOut) + 1; } },
      { label: "Full table scan: O(n)",
        color: "#dc2626",
        fn: function(n) { return Math.max(1, n / fanOut); } }
    ],
    current: { x: currentRows },
    xSlider: "rows",
    xSliderTransform: function(xVal) {
      var logV = Math.log10(Math.max(1, xVal));
      return Math.max(0, Math.min(ROW_SCALE.length - 1, Math.round(logV - 3)));
    }
  });
}

// ---- main recompute -----------------------------------------------------
function recompute() {
  var c = teachRuntime.readControls();
  var rowIdx = Math.max(0, Math.min(ROW_SCALE.length - 1, Math.round(c.rows)));
  var rows = ROW_SCALE[rowIdx];
  var pill = document.querySelector('[data-pill-for="rows"]');
  if (pill) pill.textContent = teachRuntime.formatInt(rows);

  var covering = (String(c.covering) === "true");
  var cost = uniqueLookupCost(rows, covering);

  document.getElementById("out-height").textContent = cost.height + " levels";
  document.getElementById("out-index").textContent = String(cost.indexReads);
  document.getElementById("out-fetch").textContent = String(cost.rowFetches);
  document.getElementById("out-total").textContent = String(cost.total);
  document.getElementById("out-exp").textContent = cost.explanation;

  var traversals = covering ? 1 : 2;
  buildTrees(cost.height, traversals);
  resetTreeColors();
  renderChart(covering, rows);
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: buildDescentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildDescentTimeline,
  reset: resetAnim
});
"""


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (single-row unique lookup)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows in <code>users</code> table: <span class="value-pill" data-pill-for="rows">1000000</span></label>
      <input type="range" id="rows" name="rows" min="0" max="6" step="1" value="3">
      <div class="hint">Logarithmic: 1K, 10K, 100K, 1M, 10M, 100M, 1B</div>
    </div>

    <div class="control">
      <label for="covering">Covering unique index?</label>
      <select id="covering" name="covering">
        <option value="false" selected>No — one clustered-row fetch (PK hop)</option>
        <option value="true">Yes — index-only, no table fetch</option>
      </select>
      <div class="hint">Covering removes the final table-row read — watch the PK-hop arrow disappear.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Single-row lookup on a UNIQUE key\n"
            "SELECT id, email\n"
            "FROM   users\n"
            "WHERE  id = 42;   -- uq_users_id guarantees at most 1 row"
        ),
        note=(
            "In EXPLAIN this appears as Single-row index lookup (eq_ref / const): "
            "descend the B+tree to exactly one leaf entry; non-covering plans "
            "then do one clustered-row fetch."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The grey rectangles are B+tree pages of the UNIQUE index (uq_users_id). Top row is the root page, bottom row is the leaf.",
            "A red diamond 'query token' appears above the root and descends level by level — each step is one page read.",
            "When the token arrives at a page, the page pulses yellow. That's 'page is in the buffer pool now'.",
            "Because the index is UNIQUE, the leaf stores at most ONE matching entry for id=42 — a small chip next to the leaf says so. No range scan, no sibling-page walk.",
            "Covering mode: the leaf already has every column the query asked for, an 'Index-only' badge glows on the leaf, and the lookup stops there.",
            "Non-covering mode: the leaf stored the PK. A dashed orange PK-hop arrow appears to the clustered tree and the token rides that arrow across to fetch the full row.",
        ],
    )

    stage_html = f"""
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Unique key lookup animation</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Adjust parameters, then press Play")}
  <div class="stage-with-phases">
    <svg id="unique-svg" viewBox="0 0 800 400" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (single-row unique lookup)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Tree height {ht("Number of B+tree levels from root to leaf. InnoDB's high fan-out keeps this tiny: a billion-row table is only 4 levels deep.")}</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Index reads {ht("Pages touched while traversing the UNIQUE index to the matching entry. Equals tree height.")}</p><p class="value" id="out-index">—</p></div>
    <div class="item"><p class="label">Row fetches {ht("Clustered table-row fetches after the index hit. 0 if covering, 1 if non-covering.")}</p><p class="value" id="out-fetch">—</p></div>
    <div class="item"><p class="label">Total work (pages) {ht("Index reads + row fetches. O(log n) covering, O(log n + 1) non-covering.")}</p><p class="value" id="out-total">—</p></div>
  </div>
  <div class="explanation" id="out-exp"></div>
  <div class="complexity-chart">
    <p class="chart-title">Unique lookup vs full scan (log–log, InnoDB fan-out ≈ 800)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — unique vs non-unique lookup</summary>
  <div class="body">
    <p><strong>Unique lookup</strong> returns at most one row for a key value.
    MySQL's optimizer labels this access path <code>const</code> or
    <code>eq_ref</code>, and EXPLAIN ANALYZE prints
    <em>Single-row index lookup</em>. Because the upper bound is 1, the planner
    can skip a bunch of bookkeeping that range scans need — there's no
    sibling-leaf walk, no "stop when the next key changes" check, no MRR.</p>

    <p><strong>Non-unique lookup</strong> (see the sibling lesson) can match
    many rows for one value. That means a range of leaf entries on the
    secondary tree, and potentially many clustered-row fetches when
    non-covering. The I/O scales with the number of matches — not just with
    <code>log n</code>.</p>

    <p><strong>Covering unique index</strong> — when every column in the
    SELECT list is stored in the unique index's leaf, the lookup never
    touches the clustered B+tree. This is the fastest shape of point read
    that InnoDB can do: a single descent of <code>O(log n)</code> pages.</p>

    <p>Sources: MySQL 8.4 Reference Manual §8.2.1.1 (WHERE clause optimization),
    §17.6.2 (InnoDB Indexes).</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (
        INNODB_PAGE_OVERHEAD_BYTES,
        INNODB_CHILD_POINTER_BYTES,
        INNODB_PAGE_SIZE_DEFAULT,
    )

    return _html.render_page(
        lesson_id="unique_lookup",
        title="Unique Key Lookup — single-row index lookup",
        subtitle=(
            "Descend a UNIQUE B+tree to exactly one leaf entry; if the "
            "index is non-covering, take one PK-hop to the clustered row."
        ),
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
