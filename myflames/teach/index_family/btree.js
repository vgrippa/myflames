// Constants must match myflames/teach/_cost_model.py — enforced by tests.
var INNODB_PAGE_OVERHEAD_BYTES = %d;
var INNODB_CHILD_POINTER_BYTES = %d;
var INNODB_PAGE_SIZE_DEFAULT = %d;
var ROW_SCALE = [10, 100, 1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000];

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
function btreeCost(rows, keySize, pageSize, keyType) {
  var fanOut = innodbFanout(keySize, pageSize);
  var height = innodbTreeHeight(rows, fanOut);
  var traversals = (keyType === "secondary_noncovering") ? 2 : 1;
  var pages = height * traversals;
  var explanation;
  if (keyType === "pk") {
    explanation = "Clustered PK lookup: one descent of " + height + " levels. The leaf page holds the full row \u2014 no extra I/O.";
  } else if (keyType === "secondary_covering") {
    explanation = "Covering secondary index: one descent of " + height + " levels. Every column the query asked for is already in the secondary leaf \u2014 no clustered-tree visit.";
  } else {
    explanation = "Non-covering secondary: " + height + " levels on the secondary tree, then " + height + " more on the clustered tree to fetch the row. Two traversals.";
  }
  return {fanOut: fanOut, height: height, traversals: traversals, pages: pages, explanation: explanation};
}

// ---- Concrete data labels for nodes on the lookup path ----
// These label arrays are indexed by [treeIdx][levelIdx].
// treeIdx 0 = secondary (or clustered if single tree), treeIdx 1 = clustered.
// We provide labels for up to 4 levels shown.
var PATH_LABELS_SEC = [
  "keys a..z",
  "keys a..f",
  "keys al..an",
  "PK = 42"
];
var PATH_LABELS_CLUS = [
  "keys 1..500K",
  "keys 1..100K",
  "keys 30..50",
  "#42 alice@ex"
];
var PATH_LABELS_PK = [
  "keys 1..500K",
  "keys 1..100K",
  "keys 30..50",
  "#42 alice@ex"
];

function getPathLabel(treeIdx, levelIdx, levelsShown, traversals) {
  var isLeaf = (levelIdx === levelsShown - 1);
  if (traversals === 1) {
    if (isLeaf) return "#42 alice@ex";
    return PATH_LABELS_PK[Math.min(levelIdx, PATH_LABELS_PK.length - 1)];
  }
  if (treeIdx === 0) {
    if (isLeaf) return "PK = 42";
    return PATH_LABELS_SEC[Math.min(levelIdx, PATH_LABELS_SEC.length - 1)];
  }
  if (isLeaf) return "#42 alice@ex";
  return PATH_LABELS_CLUS[Math.min(levelIdx, PATH_LABELS_CLUS.length - 1)];
}

function getTokenLabel(traversals, treeIdx) {
  if (traversals === 1) return "looking for id=42";
  if (treeIdx === 0) return "looking for email=alice@\u2026";
  return "looking for id=42";
}

// ---------- tree rendering ----------
var W = 800, H = 400;
var treeState = {
  height: 3, traversals: 1, nodes: [[],[]], token: null,
  tokenLabel: null,
  pkLink: null, pkLinkLabel: null, leafLabels: [],
  edges: [[],[]]   // per-tree array of { line, onPath } for parent->child edges
};

function buildTrees(height, traversals) {
  var svg = document.getElementById("btree-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  treeState.height = height;
  treeState.traversals = traversals;
  treeState.nodes = [[], []];
  treeState.edges = [[], []];
  treeState.leafLabels = [];
  var treeWidth = W / traversals;
  var levels = Math.min(height, 4);
  var levelY = [70, 150, 230, 310];
  var NODE_W = 52, NODE_H = 30;
  // Arrow marker for tree edges (small, subtle)
  var globalDefs = anim.svgEl("defs");
  var treeArrow = anim.svgEl("marker", {
    id: "treeArrow", viewBox: "0 0 10 10",
    refX: "9", refY: "5",
    markerWidth: "5", markerHeight: "5",
    orient: "auto"
  });
  var treeArrowPath = anim.svgEl("path", {
    d: "M 0 0 L 10 5 L 0 10 z", fill: "#d1d5db"
  });
  treeArrow.appendChild(treeArrowPath);
  globalDefs.appendChild(treeArrow);
  // Highlighted version - orange
  var treeArrowHi = anim.svgEl("marker", {
    id: "treeArrowHi", viewBox: "0 0 10 10",
    refX: "9", refY: "5",
    markerWidth: "5", markerHeight: "5",
    orient: "auto"
  });
  var treeArrowHiPath = anim.svgEl("path", {
    d: "M 0 0 L 10 5 L 0 10 z", fill: "#f97316"
  });
  treeArrowHi.appendChild(treeArrowHiPath);
  globalDefs.appendChild(treeArrowHi);
  svg.appendChild(globalDefs);

  var treeNames = (traversals === 2) ? ["Secondary B+tree \u2014 idx_users_email", "Clustered B+tree \u2014 users (PK)"] : ["Clustered B+tree \u2014 users (PK)"];
  for (var t = 0; t < traversals; t++) {
    var ox = t * treeWidth;
    var heading = anim.svgEl("text", {
      x: ox + treeWidth/2, y: 32, "text-anchor": "middle",
      "font-size": 13, "font-weight": 700, fill: "#1f2937"
    });
    heading.textContent = treeNames[t];
    svg.appendChild(heading);

    // First pass: create all nodes (so we know their positions).
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

    // Second pass: draw edges from every parent to its children. Edges
    // on the lookup path start light grey and will be highlighted orange
    // when the token traverses them. Other edges are a faint background.
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
            "marker-end": edgeOnPath ? "url(#treeArrow)" : ""
          });
          svg.appendChild(line);
          treeEdges.push({ line: line, onPath: edgeOnPath, from: lv2, to: lv2 + 1 });
        }
      }
    }
    treeState.edges[t] = treeEdges;

    // Third pass: draw the node rectangles + labels on top of the edges.
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
        // Data label: second line inside the node, shows key ranges when visited
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
      more.textContent = "(" + height + " levels total \u2014 showing top 4)";
      svg.appendChild(more);
    }
  }

  // PK link path - dashed curve from secondary leaf to clustered leaf.
  // Only meaningful when traversals === 2. Drawn first so the token
  // renders on top. Hidden until the secondary leaf is reached.
  treeState.pkLink = null;
  treeState.pkLinkLabel = null;
  if (traversals === 2) {
    var secLeaf = treeState.nodes[0][levels - 1].find(function(n) { return n.onPath; });
    var clusRoot = treeState.nodes[1][0].find(function(n) { return n.onPath; });
    var clusLeaf = treeState.nodes[1][levels - 1].find(function(n) { return n.onPath; });
    if (secLeaf && clusRoot && clusLeaf) {
      // Quadratic Bezier arching upward: sec leaf -> above clus root -> clus leaf
      var sx = secLeaf.cx + 22;  // right edge of secondary leaf
      var sy = secLeaf.cy;
      var ex = clusLeaf.cx;
      var ey = clusLeaf.cy;
      var cx = (sx + ex) / 2;
      var cy = Math.min(sy, ey) - 50;
      var pathD = "M " + sx + "," + sy +
                  " Q " + cx + "," + cy + " " + ex + "," + ey;
      var pkLink = anim.svgEl("path", {
        d: pathD,
        fill: "none",
        stroke: "#f97316",
        "stroke-width": 2,
        "stroke-dasharray": "5 3",
        "marker-end": "url(#pkArrow)",
        opacity: 0
      });
      // Arrow marker defs
      var defs = anim.svgEl("defs");
      var marker = anim.svgEl("marker", {
        id: "pkArrow", viewBox: "0 0 10 10",
        refX: "9", refY: "5",
        markerWidth: "6", markerHeight: "6",
        orient: "auto"
      });
      var arrowPath = anim.svgEl("path", {
        d: "M 0 0 L 10 5 L 0 10 z", fill: "#f97316"
      });
      marker.appendChild(arrowPath);
      defs.appendChild(marker);
      svg.appendChild(defs);
      svg.appendChild(pkLink);
      treeState.pkLink = pkLink;

      var pkLabel = anim.svgEl("text", {
        x: cx, y: cy - 4, "text-anchor": "middle",
        "font-size": 11, "font-weight": 700, fill: "#c2410c",
        opacity: 0
      });
      pkLabel.textContent = "PK=42 \u2192 clustered leaf";
      svg.appendChild(pkLabel);
      treeState.pkLinkLabel = pkLabel;
    }
  }

  // Query token
  var token = anim.svgEl("polygon", {
    points: "0,-9 9,0 0,9 -9,0",
    fill: "#ff3d3d", stroke: "#991b1b", "stroke-width": 1.5,
    opacity: 0, transform: "translate(0,0)"
  });
  svg.appendChild(token);
  treeState.token = token;

  // Token label - trails just below the diamond
  var tokenLabel = anim.svgEl("text", {
    x: 0, y: 16, "text-anchor": "middle",
    "font-size": 8, "font-weight": 700, fill: "#991b1b",
    opacity: 0
  });
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
        lvlNodes[n].label.setAttribute("font-size", 9);
        lvlNodes[n].label.setAttribute("font-weight", 600);
        lvlNodes[n].label.setAttribute("fill", "#6b7280");
        if (lvlNodes[n].dataLabel) {
          lvlNodes[n].dataLabel.textContent = "";
          lvlNodes[n].dataLabel.setAttribute("opacity", 0);
        }
      }
    }
    // Reset edges to their default grey state
    var edges = treeState.edges[t] || [];
    for (var e = 0; e < edges.length; e++) {
      var edge = edges[e];
      edge.line.setAttribute("stroke", edge.onPath ? "#d1d5db" : "#e5e7eb");
      edge.line.setAttribute("stroke-width", edge.onPath ? 1.5 : 0.8);
      edge.line.setAttribute("marker-end", edge.onPath ? "url(#treeArrow)" : "");
    }
  }
  if (treeState.token) {
    treeState.token.setAttribute("opacity", 0);
    treeState.token.setAttribute("transform", "translate(0,0)");
  }
  if (treeState.tokenLabel) {
    treeState.tokenLabel.setAttribute("opacity", 0);
  }
  if (treeState.pkLink) {
    treeState.pkLink.setAttribute("opacity", 0);
  }
  if (treeState.pkLinkLabel) {
    treeState.pkLinkLabel.setAttribute("opacity", 0);
  }
}

// Show data content on a visited node. The level name ("root", "L1",
// "leaf") stays in node.label; the data (key range or row) appears on
// the second line (node.dataLabel). Both are visible simultaneously.
function showNodeData(node, dataText) {
  if (!node || !node.dataLabel) return;
  node.dataLabel.textContent = dataText;
  node.dataLabel.setAttribute("opacity", 1);
  // Also emphasise the level label so it's clear both belong together
  node.label.setAttribute("fill", "#0f172a");
  node.label.setAttribute("font-weight", 700);
}

// ---------- timeline ----------
function buildDescentTimeline() {
  resetTreeColors();
  var tl = anim.timeline();
  var token = treeState.token;
  var tokenLabel = treeState.tokenLabel;
  var levelsShown = Math.min(treeState.height, 4);
  var phaseLabel = document.getElementById("phase-label");

  // Walk one tree (descend root -> leaf, pulsing each node). Step offset
  // allows the token to start at the root position without a fade-in
  // when it's arriving over the PK link.
  function addTreeDescent(treeIdx, fadeInFromAbove) {
    tl.call(function() {
      var rootNode = treeState.nodes[treeIdx][0].find(function(n) { return n.onPath; });
      if (fadeInFromAbove && rootNode) {
        token.setAttribute("transform", "translate(" + rootNode.cx + "," + (rootNode.cy - 40) + ")");
        token.setAttribute("opacity", 0);
      }
      // Update token label text for the current tree
      var tLabel = getTokenLabel(treeState.traversals, treeIdx);
      tokenLabel.textContent = tLabel;

      phaseLabel.textContent = (treeState.traversals === 2
        ? (treeIdx === 0 ? "Walking idx_users_email (secondary) for WHERE email = 'alice@example.com'"
                         : "Walking users PK (clustered) using PK=42 from the secondary leaf")
        : "Walking users PK (clustered) for WHERE id = 42");
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
        var nodeDataLabel = getPathLabel(treeIdx, levelIdx, levelsShown, treeState.traversals);
        tl.call(function() {
          phaseLabel.textContent = "Tree " + (treeIdx + 1) + "/" + treeState.traversals +
            " \u2014 descending to " + (levelIdx === 0 ? "root" : (isLeaf ? "leaf" : "level " + levelIdx)) +
            " \u2014 " + nodeDataLabel;
        });
        var currentPos;
        if (levelIdx === 0) {
          currentPos = { x: activeNode.cx, y: activeNode.cy - 40 };
        } else {
          var prev = treeState.nodes[treeIdx][levelIdx - 1].find(function(n) { return n.onPath; });
          currentPos = { x: prev.cx, y: prev.cy };
        }
        // Starting-position fudge: if the first level is being entered
        // over the PK link, the token's current position may have been
        // set by the link-traversal step, so skip the re-jump.
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
            // Label the on-path node with concrete data
            showNodeData(activeNode, nodeDataLabel);
            // Highlight the edge from the previous level to this one.
            if (levelIdx > 0) {
              var edges = treeState.edges[treeIdx] || [];
              for (var ei = 0; ei < edges.length; ei++) {
                var ed = edges[ei];
                if (ed.onPath && ed.from === levelIdx - 1 && ed.to === levelIdx) {
                  ed.line.setAttribute("stroke", "#f97316");
                  ed.line.setAttribute("stroke-width", 2.5);
                  ed.line.setAttribute("marker-end", "url(#treeArrowHi)");
                }
              }
            }
          }
        });
        tl.delay(160);
      })(lv);
    }
  }

  if (treeState.traversals === 1) {
    tl.mark("Descend clustered B+tree");
    addTreeDescent(0, true);
  } else {
    // Secondary descent
    tl.mark("Descend secondary index");
    addTreeDescent(0, true);
    // PK-hop: reveal the dashed arrow, then animate the token along it
    // from the secondary leaf to the clustered leaf.
    var secLeaf = treeState.nodes[0][levelsShown - 1].find(function(n) { return n.onPath; });
    var clusLeaf = treeState.nodes[1][levelsShown - 1].find(function(n) { return n.onPath; });
    if (secLeaf && clusLeaf) {
      tl.delay(240);
      tl.mark("Follow PK pointer");
      tl.call(function() {
        phaseLabel.textContent = "Secondary leaf has PK=42 \u2014 following the pointer to the clustered leaf";
      });
      tl.add({
        from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
        onUpdate: function(v) {
          if (treeState.pkLink) treeState.pkLink.setAttribute("opacity", v);
          if (treeState.pkLinkLabel) treeState.pkLinkLabel.setAttribute("opacity", v);
        }
      });
      // Animate the token along the same quadratic-Bezier path used by
      // the PK link so the motion visibly rides the arrow.
      var sx = secLeaf.cx + 22;
      var sy = secLeaf.cy;
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
          // Light up the clustered leaf and label it
          clusLeaf.rect.setAttribute("fill", "#fde725");
          clusLeaf.rect.setAttribute("stroke", "#ca8a04");
          anim.pulse(clusLeaf.rect, 3, 1, 360);
          showNodeData(clusLeaf, "#42 alice@ex");
        }
      });
      tl.delay(220);
    }
    // Clustered descent - highlight the remaining path from
    // root -> ... -> leaf for completeness.
    tl.mark("Descend clustered B+tree");
    tl.call(function() {
      phaseLabel.textContent = "InnoDB walks the clustered B+tree top-down using PK=42";
      tokenLabel.textContent = "looking for id=42";
    });
    // Move the token back up to above the clustered root, then descend.
    var clusRoot = treeState.nodes[1][0].find(function(n) { return n.onPath; });
    if (clusRoot) {
      tl.add({
        from: 1, to: 0, duration: 200, ease: anim.easeInCubic,
        onUpdate: function(v) {
          token.setAttribute("opacity", v);
          tokenLabel.setAttribute("opacity", v);
        }
      });
      tl.call(function() {
        token.setAttribute("transform", "translate(" + clusRoot.cx + "," + (clusRoot.cy - 40) + ")");
        tokenLabel.setAttribute("x", clusRoot.cx);
        tokenLabel.setAttribute("y", clusRoot.cy - 40 + 16);
      });
      tl.add({
        from: 0, to: 1, duration: 200, ease: anim.easeOutCubic,
        onUpdate: function(v) {
          token.setAttribute("opacity", v);
          tokenLabel.setAttribute("opacity", v);
        }
      });
    }
    addTreeDescent(1, false);
  }

  tl.mark("Lookup complete");
  tl.call(function() {
    phaseLabel.textContent = "\u2713 Lookup complete \u2014 found user #42 (alice@example.com)";
    tokenLabel.setAttribute("opacity", 0);
  });
  return tl;
}

function resetAnim() {
  resetTreeColors();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

// ---------- complexity chart ----------
function renderChart(keySize, pageSize, keyType, currentRows) {
  var fanOut = innodbFanout(keySize, pageSize);
  var traversals = (keyType === "secondary_noncovering") ? 2 : 1;
  var lookupLabel = (keyType === "pk") ? "PK: O(log users) = O(log n)"
    : (keyType === "secondary_covering") ? "Covering: O(log users) = O(log n)"
    : "Non-covering: O(2 log users) = O(2 log n)";
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 10, xMax: 1e10,
    xLabel: "Rows in users table", yLabel: "Pages touched",
    curves: [
      { label: lookupLabel, color: "#2563eb",
        fn: function(n) { return innodbTreeHeight(n, fanOut) * traversals; } },
      { label: "Full scan: O(users) = O(n)", color: "#dc2626",
        fn: function(n) { return Math.max(1, n / 400); } }
    ],
    current: { x: currentRows },
    xSlider: "rows",
    xSliderTransform: function(xVal) {
      // ROW_SCALE = [10, 100, 1k, 10k, 100k, 1M, 10M, 100M, 1B]
      // Find the closest log index
      var logV = Math.log10(Math.max(1, xVal));
      return Math.max(0, Math.min(ROW_SCALE.length - 1, Math.round(logV - 1)));
    }
  });
}

// ---------- main recompute ----------
function recompute() {
  var c = teachRuntime.readControls();
  var rowIdx = Math.max(0, Math.min(ROW_SCALE.length - 1, Math.round(c.rows)));
  var rows = ROW_SCALE[rowIdx];
  var pill = document.querySelector('[data-pill-for="rows"]');
  if (pill) pill.textContent = teachRuntime.formatInt(rows);

  var keySize = Math.round(c.key_size);
  var pageSize = Math.round(c.page_size);
  var keyType = c.key_type;
  var cost = btreeCost(rows, keySize, pageSize, keyType);

  document.getElementById("out-fanout").textContent = teachRuntime.formatInt(cost.fanOut);
  document.getElementById("out-height").textContent = cost.height + " levels";
  document.getElementById("out-traversals").textContent = cost.traversals;
  document.getElementById("out-pages").textContent = cost.pages + " pages";
  document.getElementById("out-cold-io").textContent = teachRuntime.formatBytes(cost.pages * pageSize);
  document.getElementById("out-complexity").textContent = (cost.traversals === 2)
    ? "O(log users) + O(log users) = O(2 log n)"
    : "O(log users) = O(log n)";
  document.getElementById("out-explanation").textContent = cost.explanation;

  buildTrees(cost.height, cost.traversals);
  resetTreeColors();
  renderChart(keySize, pageSize, keyType, rows);
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
