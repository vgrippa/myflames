// Constants pinned to myflames/teach/_cost_model.py.
var INNODB_PAGE_OVERHEAD_BYTES = %d;
var INNODB_CHILD_POINTER_BYTES = %d;
var INNODB_PAGE_SIZE_DEFAULT = %d;
// Log-scale row counts aligned with the btree lesson's ROW_SCALE.
var ROW_SCALE = [1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000];

// ---- cost model (mirrors btree.js, specialised to a single-row lookup) ----
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
  // Key size fixed at 8 B, page size at the InnoDB default 16 KiB: this
  // lesson is about the unique lookup, not the fan-out knob btree teaches.
  // Both trees get the same height here, as in the btree lesson.
  var fanOut = innodbFanout(8, INNODB_PAGE_SIZE_DEFAULT);
  var height = innodbTreeHeight(rows, fanOut);
  var indexReads = height;
  // A non-covering lookup walks the clustered index again from its root
  // page with the primary key the unique record holds.
  var clusteredReads = covering ? 0 : height;
  var total = indexReads + clusteredReads;
  var explanation = covering
    ? "Covering unique lookup: " + height + " page reads in uq_users_email, root page to leaf page. " +
      "The leaf record holds email and id, every column SELECT id, email asks for, so the clustered index is never read."
    : "Non-covering unique lookup: " + height + " page reads in uq_users_email to find the one record, then " +
      height + " more in the clustered index, from its root page to the leaf page with id 42, to get first_name. " +
      total + " page reads in total.";
  return {
    fanOut: fanOut,
    height: height,
    indexReads: indexReads,
    clusteredReads: clusteredReads,
    total: total,
    explanation: explanation
  };
}

// The query the stage runs, for each choice of the Covering input.
var QUERY_SQL = {
  noncovering:
    "-- Single-row lookup on a UNIQUE secondary index\n" +
    "SELECT id, email, first_name\n" +
    "FROM   users\n" +
    "WHERE  email = 'alice@example.com';   -- uq_users_email: UNIQUE (email)",
  covering:
    "-- Single-row lookup on a UNIQUE secondary index\n" +
    "SELECT id, email\n" +
    "FROM   users\n" +
    "WHERE  email = 'alice@example.com';   -- uq_users_email holds (email, id)"
};

// ---- concrete labels on the lookup path ----
// uq_users_email is UNIQUE (email); its leaf records hold (email, id). The
// token looks for alice@example.com, which at most ONE record can hold.
var PATH_LABELS_UNIQ = [
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

function getPathLabel(treeIdx, levelIdx, levelsShown, covering) {
  var isLeaf = (levelIdx === levelsShown - 1);
  if (treeIdx === 0) {
    if (isLeaf) return covering ? "alice@ex #42" : "PK = 42";
    return PATH_LABELS_UNIQ[Math.min(levelIdx, PATH_LABELS_UNIQ.length - 1)];
  }
  if (isLeaf) return "#42 alice@ex";
  return PATH_LABELS_CLUS[Math.min(levelIdx, PATH_LABELS_CLUS.length - 1)];
}

// ---- tree rendering (same geometry as btree.js) -------------------------
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
  treeArrow.appendChild(anim.svgEl("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "var(--svg-node-stroke)"}));
  globalDefs.appendChild(treeArrow);
  var treeArrowHi = anim.svgEl("marker", {
    id: "uqTreeArrowHi", viewBox: "0 0 10 10",
    refX: "9", refY: "5",
    markerWidth: "5", markerHeight: "5",
    orient: "auto"
  });
  treeArrowHi.appendChild(anim.svgEl("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "var(--svg-token)"}));
  globalDefs.appendChild(treeArrowHi);
  svg.appendChild(globalDefs);

  var treeNames = (traversals === 2)
    ? ["Unique B+tree — uq_users_email", "Clustered B+tree — users (PK)"]
    : ["Unique B+tree — uq_users_email (covering)"];

  for (var t = 0; t < traversals; t++) {
    var ox = t * treeWidth;

    var heading = anim.svgEl("text", {
      x: ox + treeWidth/2, y: 32, "text-anchor": "middle",
      "font-size": 13, "font-weight": 700, fill: "var(--svg-ink)"
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
            stroke: edgeOnPath ? "var(--svg-node-stroke)" : "var(--svg-node-muted)",
            "stroke-width": edgeOnPath ? 1.5 : 0.8,
            "marker-end": edgeOnPath ? "url(#uqTreeArrow)" : ""
          });
          svg.appendChild(line);
          treeEdges.push({ line: line, onPath: edgeOnPath, from: lv2, to: lv2 + 1 });
        }
      }
    }
    treeState.edges[t] = treeEdges;

    // Pass 3: page rectangles and labels on top.
    for (var lv3 = 0; lv3 < levels; lv3++) {
      for (var n3 = 0; n3 < treeNodes[lv3].length; n3++) {
        var nd = treeNodes[lv3][n3];
        var rect = anim.svgEl("rect", {
          x: nd.x, y: nd.y, width: NODE_W, height: NODE_H, rx: 4, ry: 4,
          fill: "var(--svg-node-bg)", stroke: "var(--svg-node-stroke)", "stroke-width": 1
        });
        svg.appendChild(rect);
        var lvLbl = anim.svgEl("text", {
          x: nd.cx, y: nd.cy - 2, "text-anchor": "middle",
          "font-size": 9, "font-weight": 600, fill: "var(--svg-ink-muted)"
        });
        lvLbl.textContent = (lv3 === 0) ? "root" : (lv3 === levels - 1 ? "leaf" : "internal");
        svg.appendChild(lvLbl);
        var dataLbl = anim.svgEl("text", {
          x: nd.cx, y: nd.cy + 10, "text-anchor": "middle",
          "font-size": 7, "font-weight": 700, fill: "var(--svg-ink)", opacity: 0
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
        "font-size": 11, fill: "var(--svg-ink-muted)", "font-style": "italic"
      });
      more.textContent = "(" + height + " levels total — showing top 4)";
      svg.appendChild(more);
    }
  }

  // ---- PK link: dashed arrow from the unique leaf to the clustered leaf ----
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
      pkMarker.appendChild(anim.svgEl("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "var(--svg-hop)"}));
      pkArrowDefs.appendChild(pkMarker);
      svg.appendChild(pkArrowDefs);
      var pkLink = anim.svgEl("path", {
        d: pathD,
        fill: "none",
        stroke: "var(--svg-hop)",
        "stroke-width": 2,
        "stroke-dasharray": "5 3",
        "marker-end": "url(#uqPkArrow)",
        opacity: 0
      });
      svg.appendChild(pkLink);
      treeState.pkLink = pkLink;

      var pkLabel = anim.svgEl("text", {
        x: cx, y: cy - 4, "text-anchor": "middle",
        "font-size": 11, "font-weight": 700, fill: "var(--svg-hop)",
        opacity: 0
      });
      pkLabel.textContent = "PK=42 → clustered leaf";
      svg.appendChild(pkLabel);
      treeState.pkLinkLabel = pkLabel;
    }
  }

  // ---- UNIQUE chip under the unique-index leaf ----
  var uniqLeaf = treeState.nodes[0][levels - 1].find(function(n) { return n.onPath; });
  if (uniqLeaf) {
    var chipW = 150, chipH = 22;
    var chipX = uniqLeaf.cx - chipW / 2;
    var chipY = uniqLeaf.y + NODE_H + 10;
    // Keep the chip inside this tree's column.
    var treeLeft = 6;
    var treeRight = (traversals === 2) ? (W / 2) - 6 : W - 6;
    if (chipX < treeLeft) chipX = treeLeft;
    if (chipX + chipW > treeRight) chipX = Math.max(treeLeft, treeRight - chipW);
    var chipGroup = anim.svgEl("g", {opacity: 0});
    chipGroup.appendChild(anim.svgEl("rect", {
      x: chipX, y: chipY, width: chipW, height: chipH, rx: 11, ry: 11,
      fill: "var(--svg-probe-soft)", stroke: "var(--svg-probe)", "stroke-width": 1.2
    }));
    var chipText = anim.svgEl("text", {
      x: chipX + chipW / 2, y: chipY + 15, "text-anchor": "middle",
      "font-size": 10, "font-weight": 700, fill: "var(--svg-probe)"
    });
    chipText.textContent = "UNIQUE — at most 1 row";
    chipGroup.appendChild(chipText);
    svg.appendChild(chipGroup);
    treeState.uniqueChip = chipGroup;
  }

  // ---- Index-only badge above the unique leaf (covering only) ----
  if (traversals === 1 && uniqLeaf) {
    var badgeW = 78, badgeH = 16;
    // Beside the leaf, clear of the edge that comes down into it.
    var badgeX = uniqLeaf.x + NODE_W + 8;
    var badgeY = uniqLeaf.cy - badgeH / 2;
    var badgeGroup = anim.svgEl("g", {opacity: 0});
    badgeGroup.appendChild(anim.svgEl("rect", {
      x: badgeX, y: badgeY, width: badgeW, height: badgeH, rx: 8, ry: 8,
      fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok)", "stroke-width": 1.2
    }));
    var badgeText = anim.svgEl("text", {
      x: badgeX + badgeW / 2, y: badgeY + 11, "text-anchor": "middle",
      "font-size": 9, "font-weight": 700, fill: "var(--svg-ok)"
    });
    badgeText.textContent = "Index-only ✓";
    badgeGroup.appendChild(badgeText);
    svg.appendChild(badgeGroup);
    treeState.coveringBadge = badgeGroup;
  }

  // ---- Query token (red diamond) with its trailing label ----
  var token = anim.svgEl("polygon", {
    points: "0,-9 9,0 0,9 -9,0",
    fill: "var(--svg-danger)", stroke: "var(--svg-danger)", "stroke-width": 1.5,
    opacity: 0, transform: "translate(0,0)"
  });
  svg.appendChild(token);
  treeState.token = token;

  var tokenLabel = anim.svgEl("text", {
    x: 0, y: 16, "text-anchor": "middle",
    "font-size": 8, "font-weight": 700, fill: "var(--svg-ink)",
    opacity: 0
  });
  tokenLabel.textContent = "looking for email=alice@…";
  svg.appendChild(tokenLabel);
  treeState.tokenLabel = tokenLabel;
}

function resetTreeColors() {
  for (var t = 0; t < treeState.nodes.length; t++) {
    for (var lv = 0; lv < treeState.nodes[t].length; lv++) {
      var lvlNodes = treeState.nodes[t][lv];
      for (var n = 0; n < lvlNodes.length; n++) {
        lvlNodes[n].rect.setAttribute("fill", "var(--svg-node-bg)");
        lvlNodes[n].rect.setAttribute("stroke", "var(--svg-node-stroke)");
        lvlNodes[n].rect.setAttribute("stroke-width", 1);
        var orig = (lv === 0) ? "root" : (lv === treeState.nodes[t].length - 1 ? "leaf" : "internal");
        lvlNodes[n].label.textContent = orig;
        lvlNodes[n].label.setAttribute("fill", "var(--svg-ink-muted)");
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
      edge.line.setAttribute("stroke", edge.onPath ? "var(--svg-node-stroke)" : "var(--svg-node-muted)");
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
  node.label.setAttribute("fill", "var(--svg-ink)");
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
  var totalReads = levelsShown * treeState.traversals;

  // Guided steps: one per page read, plus the UNIQUE stop, the PK hop and
  // the end. Kinds name the simplified-code lines (GUIDE in unique_lookup.py).
  var BEAT_KINDS = {
    unique: ["root", "internal", "leaf"],
    clustered: ["clus-root", "clus-internal", "clus-leaf"]
  };
  function beatKind(treeIdx, levelIdx, isLeaf) {
    var kinds = treeIdx === 0 ? BEAT_KINDS.unique : BEAT_KINDS.clustered;
    return kinds[levelIdx === 0 ? 0 : (isLeaf ? 2 : 1)];
  }
  function treeName(treeIdx) {
    if (treeIdx === 0) return covering ? "uq_users_email (unique, covering)" : "uq_users_email (unique)";
    return "users PK (clustered)";
  }

  // Walk one tree root page -> leaf page, pulsing each page on arrival.
  // The clustered walk starts its first step before the token moves there.
  function addTreeDescent(treeIdx, fadeInFromAbove, rootStepStarted) {
    if (!rootStepStarted) tl.beat(beatKind(treeIdx, 0, false));
    tl.call(function() {
      var rootNode = treeState.nodes[treeIdx][0].find(function(n) { return n.onPath; });
      if (fadeInFromAbove && rootNode) {
        token.setAttribute("transform", "translate(" + rootNode.cx + "," + (rootNode.cy - 40) + ")");
        token.setAttribute("opacity", 0);
      }
      tokenLabel.textContent = (treeIdx === 0) ? "looking for email=alice@…" : "looking for id=42";
      phaseLabel.textContent = (treeIdx === 0)
        ? "Walking uq_users_email (unique) for WHERE email = 'alice@example.com'"
        : "Walking users PK (clustered) using PK=42 from the unique leaf";
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
        var nodeDataLabel = getPathLabel(treeIdx, levelIdx, levelsShown, covering);
        var captionData = (treeIdx === 0 && isLeaf)
          ? "the record (alice@example.com, id 42)"
          : nodeDataLabel;
        if (levelIdx > 0) tl.beat(beatKind(treeIdx, levelIdx, isLeaf));
        tl.call(function() {
          // Number each page read while the whole tree is drawn.
          var pageName = levelIdx === 0 ? "root page" : (isLeaf ? "leaf page" : "internal page");
          var read = treeState.height <= levelsShown
            ? "page read " + (treeIdx * levelsShown + levelIdx + 1) + " of " + totalReads + ": "
            : "";
          phaseLabel.textContent = treeName(treeIdx) + ", " + read + pageName + ", " + captionData;
        });

        var currentPos;
        var prev = null;
        if (levelIdx === 0) {
          currentPos = { x: activeNode.cx, y: activeNode.cy - 40 };
        } else {
          prev = treeState.nodes[treeIdx][levelIdx - 1].find(function(n) { return n.onPath; });
          currentPos = { x: prev.cx, y: prev.cy };
        }
        // Entering the clustered root over the PK link: the token is
        // already there, so skip the jump from above.
        var skipStart = (levelIdx === 0 && !fadeInFromAbove);
        tl.add({
          from: skipStart ? { x: activeNode.cx, y: activeNode.cy } : currentPos,
          to: { x: activeNode.cx, y: activeNode.cy },
          duration: skipStart ? 160 : 480,
          ease: anim.easeInOutCubic,
          onUpdate: function(p) {
            if (prev) {
              prev.rect.setAttribute("fill", "var(--svg-token-soft)");
              prev.rect.setAttribute("stroke", "var(--svg-token)");
            }
            token.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
            tokenLabel.setAttribute("x", p.x);
            tokenLabel.setAttribute("y", p.y + 16);
          },
          onComplete: function() {
            activeNode.rect.setAttribute("fill", "var(--svg-highlight)");
            activeNode.rect.setAttribute("stroke", "var(--svg-highlight-stroke)");
            anim.pulse(activeNode.rect, 3, 1, 360);
            showNodeData(activeNode, nodeDataLabel);
            if (levelIdx > 0) {
              var edges = treeState.edges[treeIdx] || [];
              for (var ei = 0; ei < edges.length; ei++) {
                var ed = edges[ei];
                if (ed.onPath && ed.from === levelIdx - 1 && ed.to === levelIdx) {
                  ed.line.setAttribute("stroke", "var(--svg-token)");
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

  // ---------- Walk the unique index ----------
  tl.mark("Descend uq_users_email");
  addTreeDescent(0, true);

  // ---------- UNIQUE: one record, no next-record check ----------
  tl.beat("unique");
  tl.mark("UNIQUE: stop at one record");
  tl.call(function() {
    phaseLabel.textContent = "UNIQUE: no other record can hold alice@example.com, so the lookup stops at this one (no range scan, no next-record check)";
  });
  tl.add({
    from: 0, to: 1, duration: 280, ease: anim.easeOutCubic,
    onUpdate: function(v) {
      if (treeState.uniqueChip) treeState.uniqueChip.setAttribute("opacity", v);
    }
  });
  tl.delay(260);

  if (covering) {
    // ---------- Covering: the record has every column ----------
    tl.beat("covered");
    tl.mark("Covering: stop at the leaf");
    tl.call(function() {
      phaseLabel.textContent = "uq_users_email holds (email, id), every column SELECT id, email asks for, so the clustered index is never read";
    });
    // The Index-only badge arrives with a gentle overshoot: the one
    // follow-through moment of this lesson.
    tl.add({
      from: 0, to: 1, duration: 420, ease: anim.easeOutBack,
      onUpdate: function(v) {
        if (treeState.coveringBadge) treeState.coveringBadge.setAttribute("opacity", Math.min(1, v));
      }
    });
    tl.add({
      from: 1, to: 0, duration: 280, ease: anim.easeInCubic,
      onUpdate: function(v) {
        token.setAttribute("opacity", v);
        tokenLabel.setAttribute("opacity", v);
      }
    });
    tl.mark("Lookup complete");
    tl.call(function() {
      var uqLeaf = treeState.nodes[0][levelsShown - 1].find(function(n) { return n.onPath; });
      if (uqLeaf) {
        uqLeaf.rect.setAttribute("fill", "var(--svg-ok-soft)");
        uqLeaf.rect.setAttribute("stroke", "var(--svg-ok)");
      }
      phaseLabel.textContent = "✓ The record (alice@example.com, id 42) has every column SELECT id, email asks for: 1 row in " +
        totalReads + " page reads, no clustered index read";
    });
    return tl;
  }

  // ---------- Non-covering: follow the primary key ----------
  var uqLeaf = treeState.nodes[0][levelsShown - 1].find(function(n) { return n.onPath; });
  var clusLeaf = treeState.nodes[1][levelsShown - 1].find(function(n) { return n.onPath; });
  if (uqLeaf && clusLeaf) {
    tl.beat("pk-hop");
    tl.mark("Follow PK to clustered index");
    tl.call(function() {
      uqLeaf.rect.setAttribute("fill", "var(--svg-hop-soft)");
      uqLeaf.rect.setAttribute("stroke", "var(--svg-hop)");
      phaseLabel.textContent = "first_name is not in uq_users_email: the record holds PK=42, the key that finds the row in the clustered tree";
    });
    tl.add({
      from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
      onUpdate: function(v) {
        if (treeState.pkLink) treeState.pkLink.setAttribute("opacity", v);
        if (treeState.pkLinkLabel) treeState.pkLinkLabel.setAttribute("opacity", v);
      }
    });
    // The token rides the same curve as the PK link, as in btree.
    var sx = uqLeaf.cx + 22;
    var sy = uqLeaf.cy;
    var ex = clusLeaf.cx;
    var ey = clusLeaf.cy;
    var pathFn = anim.path(sx, sy, (sx + ex) / 2, Math.min(sy, ey) - 50, ex, ey);
    tl.add({
      from: 0, to: 1, duration: 820, ease: anim.easeInOutCubic,
      onUpdate: function(t) {
        var p = pathFn(t);
        token.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
        tokenLabel.setAttribute("x", p.x);
        tokenLabel.setAttribute("y", p.y + 16);
      },
      onComplete: function() {
        clusLeaf.rect.setAttribute("fill", "var(--svg-highlight)");
        clusLeaf.rect.setAttribute("stroke", "var(--svg-highlight-stroke)");
        anim.pulse(clusLeaf.rect, 3, 1, 360);
        showNodeData(clusLeaf, "#42 alice@ex");
      }
    });
    tl.delay(220);
  }

  // The key, not a page address, is the link: walk the clustered tree
  // from its root page.
  tl.beat("clus-root");
  tl.mark("Descend clustered B+tree");
  tl.call(function() {
    phaseLabel.textContent = "InnoDB walks the clustered B+tree top-down using PK=42";
    tokenLabel.textContent = "looking for id=42";
  });
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
  addTreeDescent(1, false, true);

  tl.beat("done");
  tl.mark("Lookup complete");
  tl.call(function() {
    tokenLabel.setAttribute("opacity", 0);
    if (clusLeaf) {
      clusLeaf.rect.setAttribute("fill", "var(--svg-ok-soft)");
      clusLeaf.rect.setAttribute("stroke", "var(--svg-ok)");
    }
    phaseLabel.textContent = "✓ Lookup complete — 1 row (id 42, alice@example.com, first_name) in " +
      totalReads + " page reads: " + levelsShown + " in uq_users_email, " + levelsShown + " in the clustered index";
  });
  tl.add({
    from: 1, to: 0, duration: 240, ease: anim.easeInCubic,
    onUpdate: function(v) { token.setAttribute("opacity", v); }
  });
  return tl;
}

function resetAnim() {
  resetTreeColors();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
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
        color: "var(--svg-ok)",
        fn: function(n) { return innodbTreeHeight(n, fanOut); } },
      { label: "Non-covering unique lookup: O(2 log n)",
        color: "var(--svg-token)",
        fn: function(n) { return 2 * innodbTreeHeight(n, fanOut); } },
      { label: "Full table scan: O(n)",
        color: "var(--svg-danger)",
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
  document.getElementById("out-index").textContent = cost.indexReads + " pages";
  document.getElementById("out-fetch").textContent = cost.clusteredReads + " pages";
  document.getElementById("out-total").textContent = cost.total + " pages";
  document.getElementById("out-exp").textContent = cost.explanation;
  var sql = document.querySelector(".query-sql");
  if (sql) sql.textContent = covering ? QUERY_SQL.covering : QUERY_SQL.noncovering;

  buildTrees(cost.height, covering ? 1 : 2);
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
