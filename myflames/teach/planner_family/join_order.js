// =============================================================================
// Constants & cost model
// =============================================================================

// Real table aliases from the 7-table example query. Extra synthetic
// names cover N > 7.
var TABLE_NAMES = ["u", "o", "li", "p", "s", "c", "w", "t8", "t9", "ta"];
var TABLE_FULL = {
  u: "users", o: "orders", li: "line_items",
  p: "products", s: "suppliers", c: "categories", w: "warehouses",
  t8: "t8", t9: "t9", ta: "t10"
};

// Join edges from the example query (which table joins to which).
// The query graph is a tree: u — o — li — p — s, with p — c and li — w.
// For N != 7 we truncate or extend (chain) below.
var BASE_JOIN_EDGES = [
  ["u", "o"], ["o", "li"], ["li", "p"], ["p", "s"], ["p", "c"], ["li", "w"]
];

function joinEdgesFor(N) {
  var names = TABLE_NAMES.slice(0, N);
  var set = {};
  for (var i = 0; i < names.length; i++) set[names[i]] = true;
  var edges = [];
  // Keep BASE edges whose both endpoints are in the first N tables.
  for (var j = 0; j < BASE_JOIN_EDGES.length; j++) {
    var e = BASE_JOIN_EDGES[j];
    if (set[e[0]] && set[e[1]]) edges.push(e);
  }
  // For N > 7, chain extra synthetic tables off the last real one.
  var prev = "w";
  for (var k = 7; k < N; k++) {
    edges.push([prev, names[k]]);
    prev = names[k];
  }
  // For N < 4, ensure we still have edges to draw (chain the first N tables).
  if (edges.length === 0 && N >= 2) {
    for (var m = 0; m < N - 1; m++) edges.push([names[m], names[m + 1]]);
  }
  return edges;
}

// Stage geometry.
var W = 820, H = 460;
var TABLE_ROW_Y = 56;
var FINAL_PLAN_Y = 408;

// ---- Factorial / permutation helpers (greedy_search cost) ----
function fact(n) {
  if (n <= 1) return 1;
  var r = 1;
  for (var i = 2; i <= n; i++) {
    r *= i;
    if (r > 1e15) return 1e15;
  }
  return r;
}
function perm(a, b) {
  if (b <= 0) return 1;
  if (b > a) b = a;
  var r = 1;
  for (var i = 0; i < b; i++) {
    r *= (a - i);
    if (r > 1e15) return 1e15;
  }
  return r;
}

// greedy_search plan count, summed across its N commit steps.
function plansEvaluated(N, d) {
  var total = 0;
  for (var i = 0; i < N; i++) {
    var remaining = N - i;
    total += perm(remaining, Math.min(d, remaining));
    if (total > 1e15) return 1e15;
  }
  return total;
}

function applyPruning(plans, pruneLevel) {
  if (pruneLevel === 0) return { plans: plans, ratio: 0 };
  var ratio = 0.75;
  return { plans: Math.max(1, Math.round(plans * (1 - ratio))), ratio: ratio };
}

function bigOLabel(N, d) {
  if (d >= N) return "O(N!) — exhaustive enumeration";
  if (d === 1) return "O(N²) — pure greedy, no lookahead";
  return "O(N · N^" + d + " / " + d + ") — depth-" + d + " lookahead";
}

// =============================================================================
// Hypergraph (DPhyp) cost model
// =============================================================================
// DPhyp enumerates connected subgraphs (CSGs) of the query graph plus
// complement-pairs. Worst case is O(3^N) for clique queries; for
// tree-shaped queries (our example) it's O(N^3). Capped at
// optimizer_max_subgraph_pairs (default 100,000).
var OPTIMIZER_MAX_SUBGRAPH_PAIRS = 100000;

function dphypTreePairs(N) {
  // Empirical: for a tree-shaped query graph, DPhyp examines roughly
  // C(N, 2) + (N-2)*(N-1)/2 + ... ≈ N^3 / 6 pairs. Use N^3 / 6 as the
  // teaching approximation — same order of magnitude as the real algo,
  // and the lesson is about regimes not exact counts.
  return Math.max(N, Math.round((N * N * N) / 6));
}

function dphypClique(N) {
  // Worst case: complete graph → 3^N pairs.
  return Math.min(Math.pow(3, N), 1e15);
}

function bigOLabelDPhyp() {
  return "O(3^N) worst case, O(N³) on tree query (current example)";
}

// =============================================================================
// Tweened color transitions (animation-craft: never instant property swaps)
// =============================================================================
// Fires a short, independent rAF tween that interpolates a single color
// attribute from its current value to `toColor`. Used in place of bare
// setAttribute("fill"/"stroke", …) inside timeline steps so state changes
// (costing → yellow, commit → green, prune → grey) ease in rather than
// snapping. Honors reduced-motion by jumping straight to the end state.
function animColor(el, attr, toColor, dur, ease) {
  if (!el) return;
  var fromColor = el.getAttribute(attr) || toColor;
  if (fromColor === toColor) return;
  if (anim.reducedMotion && anim.reducedMotion()) {
    el.setAttribute(attr, toColor);
    return;
  }
  anim.tween({
    from: 0, to: 1, duration: dur || 220, ease: ease || anim.easeOutCubic,
    onUpdate: function(t) {
      el.setAttribute(attr, anim.lerpColor(fromColor, toColor, t));
    },
    onComplete: function() { el.setAttribute(attr, toColor); }
  });
}

// =============================================================================
// Stage rendering — shared chrome
// =============================================================================

var stageState = {
  svg: null, plansCounter: null, finalPlanText: null,
  // Greedy mode
  tree: null,
  // Hypergraph mode
  graph: null,
  // Shared
  tableCards: [],
  algo: "greedy"
};

function clearStage() {
  var svg = document.getElementById("planner-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  stageState.svg = svg;
  stageState.tableCards = [];
  stageState.tree = null;
  stageState.graph = null;
  return svg;
}

function drawTablesStrip(svg, N) {
  var stripBg = anim.svgEl("rect", {
    x: 20, y: TABLE_ROW_Y - 22, width: W - 40, height: 40,
    rx: 8, fill: "#eef4ff", stroke: "#c7d8f0"
  });
  svg.appendChild(stripBg);
  var stripLabel = anim.svgEl("text", {
    x: 28, y: TABLE_ROW_Y - 5, "font-size": 10, "font-weight": 700,
    fill: "#1e3a8a", "letter-spacing": "0.4"
  });
  stripLabel.textContent = "TABLES IN QUERY (FROM-list)";
  svg.appendChild(stripLabel);

  var tables = TABLE_NAMES.slice(0, N);
  stageState.tableCards = [];
  var cardW = Math.min(56, (W - 80) / N);
  var totalW = cardW * N + (N - 1) * 6;
  var startX = (W - totalW) / 2;
  for (var i = 0; i < N; i++) {
    var x = startX + i * (cardW + 6);
    var rect = anim.svgEl("rect", {
      x: x, y: TABLE_ROW_Y, width: cardW, height: 22, rx: 5,
      fill: "#ffffff", stroke: "#3b82f6", "stroke-width": 1.5
    });
    svg.appendChild(rect);
    var t = anim.svgEl("text", {
      x: x + cardW / 2, y: TABLE_ROW_Y + 15,
      "text-anchor": "middle", "font-size": 11,
      "font-weight": 700, fill: "#1e3a8a",
      "font-family": "ui-monospace, Menlo, monospace"
    });
    t.textContent = tables[i];
    svg.appendChild(t);
    stageState.tableCards.push({ rect: rect, label: t, name: tables[i] });
  }
}

function drawCounter(svg, title) {
  var counterBg = anim.svgEl("rect", {
    x: W - 220, y: 12, width: 200, height: 32, rx: 8,
    fill: "#0f172a", stroke: "#1e293b"
  });
  svg.appendChild(counterBg);
  var counterLbl = anim.svgEl("text", {
    x: W - 212, y: 26, "font-size": 9, fill: "#94a3b8",
    "font-weight": 700, "letter-spacing": "0.5"
  });
  counterLbl.textContent = title;
  svg.appendChild(counterLbl);
  var counterVal = anim.svgEl("text", {
    x: W - 212, y: 39, "font-size": 14, fill: "#fde725",
    "font-weight": 700,
    "font-family": "ui-monospace, Menlo, monospace"
  });
  counterVal.textContent = "0";
  svg.appendChild(counterVal);
  stageState.plansCounter = counterVal;
}

function drawFinalPlanStrip(svg, label) {
  var finalBg = anim.svgEl("rect", {
    x: 20, y: FINAL_PLAN_Y - 18, width: W - 40, height: 36, rx: 8,
    fill: "#f0fdf4", stroke: "#86efac"
  });
  svg.appendChild(finalBg);
  var finalLbl = anim.svgEl("text", {
    x: 28, y: FINAL_PLAN_Y - 3, "font-size": 10, "font-weight": 700,
    fill: "#065f46", "letter-spacing": "0.4"
  });
  finalLbl.textContent = label;
  svg.appendChild(finalLbl);
  var finalText = anim.svgEl("text", {
    x: W / 2, y: FINAL_PLAN_Y + 13, "text-anchor": "middle",
    "font-size": 13, "font-weight": 700, fill: "#065f46",
    "font-family": "ui-monospace, Menlo, monospace", opacity: 0.35
  });
  finalText.textContent = "— (run search to see) —";
  svg.appendChild(finalText);
  stageState.finalPlanText = finalText;
}

// =============================================================================
// Greedy mode — search tree of partial orderings (left-deep)
// =============================================================================

var SAMPLE_BREADTH = 4;
var SAMPLE_DEPTH = 3;
var TREE_TOP_Y = 110;
var TREE_BOT_Y = 360;

function buildSampleTreeNodes(N, depth) {
  var tables = TABLE_NAMES.slice(0, N);
  var levels = [];
  var maxLevels = Math.min(SAMPLE_DEPTH, depth, N);
  var allNodes = [];
  var idCounter = 0;
  var lvl0 = [];
  for (var i = 0; i < Math.min(SAMPLE_BREADTH, tables.length); i++) {
    var node = {
      id: idCounter++, parentId: null,
      label: tables[i], used: [tables[i]],
      onPath: (i === 0)
    };
    lvl0.push(node);
    allNodes.push(node);
  }
  levels.push(lvl0);
  for (var lv = 1; lv < maxLevels; lv++) {
    var prevLevel = levels[lv - 1];
    var thisLevel = [];
    for (var p = 0; p < prevLevel.length; p++) {
      var parent = prevLevel[p];
      var remainingTables = tables.filter(function(t) { return parent.used.indexOf(t) < 0; });
      var fan = parent.onPath ? Math.min(SAMPLE_BREADTH, remainingTables.length)
                              : Math.min(2, remainingTables.length);
      for (var c = 0; c < fan; c++) {
        var nextTable = remainingTables[c];
        var child = {
          id: idCounter++, parentId: parent.id,
          label: nextTable,
          used: parent.used.concat([nextTable]),
          onPath: parent.onPath && (c === 0)
        };
        thisLevel.push(child);
        allNodes.push(child);
      }
    }
    levels.push(thisLevel);
  }

  var levelGap = (TREE_BOT_Y - TREE_TOP_Y) / Math.max(1, levels.length - 1 || 1);
  for (var lv2 = 0; lv2 < levels.length; lv2++) {
    var lvlNodes = levels[lv2];
    var yPos = TREE_TOP_Y + lv2 * levelGap;
    for (var n = 0; n < lvlNodes.length; n++) {
      lvlNodes[n].x = ((n + 1) / (lvlNodes.length + 1)) * (W - 80) + 40;
      lvlNodes[n].y = yPos;
    }
  }
  return { levels: levels, allNodes: allNodes };
}

function drawTreeSkeleton(svg, tree) {
  var g = anim.svgEl("g", { id: "tree-g" });
  svg.appendChild(g);
  for (var lv = 1; lv < tree.levels.length; lv++) {
    var lvl = tree.levels[lv];
    for (var i = 0; i < lvl.length; i++) {
      var node = lvl[i];
      var parent = tree.allNodes.find(function(n) { return n.id === node.parentId; });
      if (!parent) continue;
      var line = anim.svgEl("line", {
        x1: parent.x, y1: parent.y + 8,
        x2: node.x, y2: node.y - 8,
        stroke: "#cbd5e1", "stroke-width": 1, opacity: 0.6
      });
      g.appendChild(line);
      node.edge = line;
    }
  }
  for (var n = 0; n < tree.allNodes.length; n++) {
    var nd = tree.allNodes[n];
    var circle = anim.svgEl("circle", {
      cx: nd.x, cy: nd.y, r: 11,
      fill: "#f1f5f9", stroke: "#94a3b8", "stroke-width": 1.5
    });
    g.appendChild(circle);
    var lbl = anim.svgEl("text", {
      x: nd.x, y: nd.y + 3, "text-anchor": "middle",
      "font-size": 9, "font-weight": 700, fill: "#334155",
      "font-family": "ui-monospace, Menlo, monospace"
    });
    lbl.textContent = nd.label;
    g.appendChild(lbl);
    nd.circle = circle;
    nd.labelEl = lbl;
  }
}

function buildGreedyStage(N, depth) {
  var svg = clearStage();
  drawTablesStrip(svg, N);
  drawCounter(svg, "PLANS EVALUATED (greedy_search)");

  var treeTitle = anim.svgEl("text", {
    x: 28, y: TREE_TOP_Y - 16,
    "font-size": 10, "font-weight": 700, fill: "#6b7280",
    "letter-spacing": "0.4"
  });
  treeTitle.textContent = "SEARCH TREE OF PARTIAL PLANS (left-deep order)";
  svg.appendChild(treeTitle);

  stageState.tree = buildSampleTreeNodes(N, Math.max(2, Math.min(SAMPLE_DEPTH, depth)));
  drawTreeSkeleton(svg, stageState.tree);
  drawFinalPlanStrip(svg, "CHOSEN PLAN (linear left-deep order)");
}

function resetGreedyColors() {
  if (!stageState.tree) return;
  for (var i = 0; i < stageState.tree.allNodes.length; i++) {
    var nd = stageState.tree.allNodes[i];
    nd.circle.setAttribute("fill", "#f1f5f9");
    nd.circle.setAttribute("stroke", "#94a3b8");
    nd.circle.setAttribute("stroke-width", 1.5);
    nd.labelEl.setAttribute("fill", "#334155");
    if (nd.edge) {
      nd.edge.setAttribute("stroke", "#cbd5e1");
      nd.edge.setAttribute("stroke-width", 1);
      nd.edge.setAttribute("opacity", 0.6);
    }
  }
}

function buildGreedyTimeline(controls) {
  var tl = anim.timeline();
  var phaseLabel = document.getElementById("phase-label");
  var N = controls.tables;
  var depth = controls.search_depth;
  var prune = controls.prune_level;
  var tree = stageState.tree;
  if (!tree) return tl;

  var effDepth = Math.min(depth, N);
  var totalPlans = plansEvaluated(N, effDepth);
  var pruned = applyPruning(totalPlans, prune);
  var finalPlanCount = pruned.plans;
  var evaluatedSoFar = 0;

  function bumpCounter(amount) {
    evaluatedSoFar += amount;
    if (evaluatedSoFar > finalPlanCount) evaluatedSoFar = finalPlanCount;
    if (stageState.plansCounter) {
      stageState.plansCounter.textContent = teachRuntime.formatInt(evaluatedSoFar);
    }
  }

  tl.mark("Problem: pick an order for " + N + " tables");
  tl.call(function() {
    phaseLabel.textContent = "Problem: " + N + " tables, " + teachRuntime.formatInt(fact(N)) + " possible left-deep orderings.";
  });
  for (var i = 0; i < stageState.tableCards.length; i++) {
    (function(idx) {
      tl.call(function() {
        var c = stageState.tableCards[idx];
        animColor(c.rect, "fill", "#dbeafe", 200);
        anim.pulse(c.rect, 3, 1, 200);
      });
      tl.delay(60);
    })(i);
  }
  tl.delay(200);

  tl.mark("Step 1: evaluate each candidate first table");
  tl.call(function() {
    phaseLabel.textContent = "Step 1 — best_extension_by_limited_search costs each candidate root";
  });
  var lvl0 = tree.levels[0];
  for (var k = 0; k < lvl0.length; k++) {
    (function(idx) {
      var nd = lvl0[idx];
      tl.call(function() {
        animColor(nd.circle, "fill", "#fef3c7", 200);
        animColor(nd.circle, "stroke", "#d97706", 200);
        anim.pulse(nd.circle, 4, 1, 240);
        bumpCounter(Math.max(1, Math.round(finalPlanCount / (lvl0.length * (tree.levels.length + 1)))));
      });
      tl.delay(220);
    })(k);
  }

  tl.delay(150);
  tl.mark("Pick lowest-cost partial plan");
  tl.call(function() {
    phaseLabel.textContent = "Greedy commit — lowest-cost candidate becomes the outer loop";
    var winner = lvl0.find(function(n) { return n.onPath; });
    if (winner) {
      animColor(winner.circle, "fill", "#34d399", 300, anim.easeOutBack);
      animColor(winner.circle, "stroke", "#047857", 300);
      winner.circle.setAttribute("stroke-width", 2.5);
      animColor(winner.labelEl, "fill", "#064e3b", 300);
      anim.arrival(winner.circle);
    }
  });
  tl.delay(280);

  // Helper: a node is "reachable for evaluation" depending on prune_level.
  //   prune=1 (default): only nodes whose every ancestor was on-path get
  //     visited — pruned subtrees are never opened by
  //     best_extension_by_limited_search.
  //   prune=0 (exhaustive): every node at every level is costed.
  function isReachable(node) {
    if (prune === 0) return true;
    var cur = node;
    // Walk to root; any off-path ancestor means this subtree was pruned.
    while (cur && cur.parentId !== null) {
      var parent = null;
      for (var pi = 0; pi < tree.allNodes.length; pi++) {
        if (tree.allNodes[pi].id === cur.parentId) { parent = tree.allNodes[pi]; break; }
      }
      if (!parent) break;
      if (!parent.onPath) return false;
      cur = parent;
    }
    return true;
  }

  // When prune=1 we proactively grey out every never-visited node up-front
  // so the viewer sees the "pruned forest" before the animation walks
  // only the surviving path.
  if (prune === 1) {
    tl.delay(120);
    tl.mark("optimizer_prune_level=1 — prune unreachable subtrees up-front");
    tl.call(function() {
      phaseLabel.textContent =
        "optimizer_prune_level=1 — partial plans whose ancestor was already beaten are dropped without exploring their subtree";
      for (var ai = 0; ai < tree.allNodes.length; ai++) {
        var nd = tree.allNodes[ai];
        if (nd.parentId === null) continue;
        if (!isReachable(nd)) {
          animColor(nd.circle, "fill", "#e2e8f0", 260, anim.easeInCubic);
          animColor(nd.circle, "stroke", "#cbd5e1", 260, anim.easeInCubic);
          nd.circle.setAttribute("stroke-width", 1);
          animColor(nd.labelEl, "fill", "#94a3b8", 260, anim.easeInCubic);
          if (nd.edge) {
            animColor(nd.edge, "stroke", "#e2e8f0", 260, anim.easeInCubic);
            nd.edge.setAttribute("opacity", 0.25);
            nd.edge.setAttribute("stroke-dasharray", "3 3");
          }
        }
      }
    });
    tl.delay(200);
  }

  for (var lv = 1; lv < tree.levels.length; lv++) {
    (function(levelIdx) {
      var levelNodes = tree.levels[levelIdx];
      // The set we actually animate depends on prune_level.
      var visited = levelNodes.filter(isReachable);
      var onPath  = levelNodes.filter(function(n) { return n.onPath; });

      tl.mark("Step " + (levelIdx + 1) +
              (prune === 0
                ? ": exhaustive — every partial plan costed"
                : ": cost only the surviving prefix's extensions"));
      tl.call(function() {
        phaseLabel.textContent = (prune === 0
          ? "Step " + (levelIdx + 1) + " (prune_level=0) — costing all " + levelNodes.length + " partial plans at this level"
          : "Step " + (levelIdx + 1) + " (prune_level=1) — only the " + visited.length + " of " + levelNodes.length + " plans whose prefix survived are extended");
      });

      // Walk the visited set. With prune=0 this is the full level (every
      // sibling, every subtree). With prune=1 it's only the on-path
      // descendants — siblings stayed grey from the up-front prune step.
      for (var s = 0; s < visited.length; s++) {
        (function(nd) {
          tl.call(function() {
            animColor(nd.circle, "fill", "#fef3c7", 180);
            animColor(nd.circle, "stroke", "#d97706", 180);
            if (nd.edge) {
              animColor(nd.edge, "stroke", "#fbbf24", 180);
              nd.edge.setAttribute("stroke-width", 1.5);
              nd.edge.setAttribute("opacity", 1);
            }
            anim.pulse(nd.circle, 3, 1, 160);
            bumpCounter(Math.max(1, Math.round(finalPlanCount / Math.max(1, tree.allNodes.length))));
          });
          tl.delay(prune === 0 ? 120 : 160);
        })(visited[s]);
      }
      tl.delay(120);

      // Commit the winning extension at this level.
      tl.call(function() {
        for (var w = 0; w < onPath.length; w++) {
          animColor(onPath[w].circle, "fill", "#34d399", 300, anim.easeOutBack);
          animColor(onPath[w].circle, "stroke", "#047857", 300);
          onPath[w].circle.setAttribute("stroke-width", 2.5);
          animColor(onPath[w].labelEl, "fill", "#064e3b", 300);
          if (onPath[w].edge) {
            animColor(onPath[w].edge, "stroke", "#10b981", 300);
            onPath[w].edge.setAttribute("stroke-width", 2);
          }
          anim.arrival(onPath[w].circle);
        }
      });
      tl.delay(220);
    })(lv);
  }

  tl.mark("Final plan locked");
  tl.call(function() {
    phaseLabel.textContent = "✓ greedy_search complete — " +
      teachRuntime.formatInt(finalPlanCount) + " partial plans evaluated";
    if (stageState.plansCounter) {
      stageState.plansCounter.textContent = teachRuntime.formatInt(finalPlanCount);
    }
    var tables = TABLE_NAMES.slice(0, N);
    var chosenPrefix = [];
    for (var lv3 = 0; lv3 < tree.levels.length; lv3++) {
      var onPath = tree.levels[lv3].find(function(n) { return n.onPath; });
      if (onPath) chosenPrefix.push(onPath.label);
    }
    var rest = tables.filter(function(t) { return chosenPrefix.indexOf(t) < 0; });
    var finalOrder = chosenPrefix.concat(rest);
    if (stageState.finalPlanText) {
      stageState.finalPlanText.textContent = finalOrder.join(" ▶ ");
      stageState.finalPlanText.setAttribute("opacity", 1);
    }
  });
  return tl;
}

// =============================================================================
// Hypergraph mode — query graph + connected subgraph enumeration
// =============================================================================
// DPhyp (Moerkotte & Neumann 2006) works on the query *graph*, not on
// orderings. Vertices are tables, edges are join predicates. The
// algorithm enumerates connected subgraphs (CSGs) of sizes 1..N and
// pairs of disjoint CSGs that share a connecting edge — each such pair
// is a candidate join. Final plan can be a bushy tree, not just
// left-deep. Far fewer subgraph pairs than N! orderings: for our
// 7-table tree query, ~57 pairs vs 5,040 orderings.

var GRAPH_CENTER_X = W / 2;
var GRAPH_CENTER_Y = 230;
var GRAPH_RADIUS = 120;

function layoutGraph(N) {
  var names = TABLE_NAMES.slice(0, N);
  var positions = {};
  for (var i = 0; i < N; i++) {
    var theta = (i / N) * 2 * Math.PI - Math.PI / 2;
    positions[names[i]] = {
      x: GRAPH_CENTER_X + Math.cos(theta) * GRAPH_RADIUS,
      y: GRAPH_CENTER_Y + Math.sin(theta) * GRAPH_RADIUS
    };
  }
  return positions;
}

function buildHypergraphStage(N) {
  var svg = clearStage();
  drawTablesStrip(svg, N);
  drawCounter(svg, "SUBGRAPH PAIRS (DPhyp)");

  var graphTitle = anim.svgEl("text", {
    x: 28, y: 95,
    "font-size": 10, "font-weight": 700, fill: "#6b7280",
    "letter-spacing": "0.4"
  });
  graphTitle.textContent = "QUERY GRAPH — vertices = tables, edges = join predicates";
  svg.appendChild(graphTitle);

  // Layout
  var edges = joinEdgesFor(N);
  var positions = layoutGraph(N);

  // Edges first (so vertices draw on top)
  var edgeEls = [];
  var g = anim.svgEl("g", { id: "graph-g" });
  svg.appendChild(g);
  for (var e = 0; e < edges.length; e++) {
    var a = positions[edges[e][0]];
    var b = positions[edges[e][1]];
    if (!a || !b) continue;
    var line = anim.svgEl("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      stroke: "#cbd5e1", "stroke-width": 2, opacity: 0.6
    });
    g.appendChild(line);
    edgeEls.push({ from: edges[e][0], to: edges[e][1], line: line });
  }

  // Vertices
  var vertexEls = {};
  var tables = TABLE_NAMES.slice(0, N);
  for (var i = 0; i < tables.length; i++) {
    var name = tables[i];
    var pos = positions[name];
    if (!pos) continue;
    var circle = anim.svgEl("circle", {
      cx: pos.x, cy: pos.y, r: 18,
      fill: "#f1f5f9", stroke: "#94a3b8", "stroke-width": 2
    });
    g.appendChild(circle);
    var lbl = anim.svgEl("text", {
      x: pos.x, y: pos.y + 4, "text-anchor": "middle",
      "font-size": 11, "font-weight": 700, fill: "#1e293b",
      "font-family": "ui-monospace, Menlo, monospace"
    });
    lbl.textContent = name;
    g.appendChild(lbl);
    vertexEls[name] = { circle: circle, label: lbl, x: pos.x, y: pos.y };
  }

  // CSG box (right side panel for showing current subgraph)
  var csgBoxBg = anim.svgEl("rect", {
    x: W - 200, y: 100, width: 180, height: 90, rx: 8,
    fill: "#fef3c7", stroke: "#f59e0b", "stroke-width": 1.5, opacity: 0.4
  });
  svg.appendChild(csgBoxBg);
  var csgLbl = anim.svgEl("text", {
    x: W - 192, y: 116, "font-size": 9, "font-weight": 700,
    fill: "#92400e", "letter-spacing": "0.4"
  });
  csgLbl.textContent = "CURRENT SUBGRAPH (CSG)";
  svg.appendChild(csgLbl);
  var csgContent = anim.svgEl("text", {
    x: W - 192, y: 140, "font-size": 13, "font-weight": 700,
    fill: "#7c2d12",
    "font-family": "ui-monospace, Menlo, monospace"
  });
  csgContent.textContent = "—";
  svg.appendChild(csgContent);
  var csgSize = anim.svgEl("text", {
    x: W - 192, y: 168, "font-size": 10, fill: "#a16207"
  });
  csgSize.textContent = "size: 0";
  svg.appendChild(csgSize);
  var csgPair = anim.svgEl("text", {
    x: W - 192, y: 182, "font-size": 9, fill: "#a16207"
  });
  csgPair.textContent = "";
  svg.appendChild(csgPair);

  stageState.graph = {
    vertices: vertexEls,
    edges: edgeEls,
    csgBoxBg: csgBoxBg,
    csgContent: csgContent,
    csgSize: csgSize,
    csgPair: csgPair
  };

  drawFinalPlanStrip(svg, "CHOSEN PLAN (DPhyp can produce a bushy tree)");
}

function resetHypergraphColors() {
  if (!stageState.graph) return;
  var v = stageState.graph.vertices;
  for (var k in v) {
    if (Object.prototype.hasOwnProperty.call(v, k)) {
      v[k].circle.setAttribute("fill", "#f1f5f9");
      v[k].circle.setAttribute("stroke", "#94a3b8");
      v[k].circle.setAttribute("stroke-width", 2);
      v[k].label.setAttribute("fill", "#1e293b");
    }
  }
  for (var i = 0; i < stageState.graph.edges.length; i++) {
    stageState.graph.edges[i].line.setAttribute("stroke", "#cbd5e1");
    stageState.graph.edges[i].line.setAttribute("stroke-width", 2);
    stageState.graph.edges[i].line.setAttribute("opacity", 0.6);
  }
  if (stageState.graph.csgContent) stageState.graph.csgContent.textContent = "—";
  if (stageState.graph.csgSize) stageState.graph.csgSize.textContent = "size: 0";
  if (stageState.graph.csgPair) stageState.graph.csgPair.textContent = "";
}

function highlightVertex(name, color, strokeColor) {
  var v = stageState.graph && stageState.graph.vertices[name];
  if (!v) return;
  animColor(v.circle, "fill", color, 240);
  animColor(v.circle, "stroke", strokeColor, 240);
  v.circle.setAttribute("stroke-width", 2.5);
  anim.pulse(v.circle, 4, 1, 240);
}

function highlightEdge(fromTo, color, width) {
  if (!stageState.graph) return;
  for (var i = 0; i < stageState.graph.edges.length; i++) {
    var e = stageState.graph.edges[i];
    if ((e.from === fromTo[0] && e.to === fromTo[1]) ||
        (e.from === fromTo[1] && e.to === fromTo[0])) {
      animColor(e.line, "stroke", color, 240);
      e.line.setAttribute("stroke-width", width);
      e.line.setAttribute("opacity", 1);
      return;
    }
  }
}

function setCsgLabel(members, sizeLabel, pairLabel) {
  if (!stageState.graph) return;
  stageState.graph.csgContent.textContent = "{ " + members.join(", ") + " }";
  stageState.graph.csgSize.textContent = "size: " + members.length + " of " + (TABLE_NAMES.slice(0, members.length).length);
  if (sizeLabel) stageState.graph.csgSize.textContent = sizeLabel;
  stageState.graph.csgPair.textContent = pairLabel || "";
}

function buildHypergraphTimeline(controls) {
  var tl = anim.timeline();
  var phaseLabel = document.getElementById("phase-label");
  var N = controls.tables;
  if (!stageState.graph) return tl;

  var totalPairs = Math.min(dphypTreePairs(N), OPTIMIZER_MAX_SUBGRAPH_PAIRS);
  var counterVal = 0;
  function bumpCounter(amount) {
    counterVal += amount;
    if (counterVal > totalPairs) counterVal = totalPairs;
    if (stageState.plansCounter) {
      stageState.plansCounter.textContent = teachRuntime.formatInt(counterVal);
    }
  }

  // ---- Phase 1: vertex enumeration (size-1 CSGs) ----
  tl.mark("Phase 1: enumerate vertices (size-1 CSGs)");
  tl.call(function() {
    phaseLabel.textContent = "Phase 1 — each table is a size-1 connected subgraph (CSG)";
  });
  var tables = TABLE_NAMES.slice(0, N);
  for (var i = 0; i < tables.length; i++) {
    (function(name, idx) {
      tl.call(function() {
        highlightVertex(name, "#fef3c7", "#d97706");
        setCsgLabel([name], "size: 1 of " + N, "DPhyp builds best plan for { " + name + " } (= just scan)");
        bumpCounter(1);
      });
      tl.delay(180);
    })(tables[i], i);
  }
  tl.delay(120);

  // ---- Phase 2: enumerate pairs along edges (CSG + CMP pairs of size 2) ----
  tl.mark("Phase 2: enumerate edge-induced pairs (CSG-CMP, size 2)");
  tl.call(function() {
    phaseLabel.textContent = "Phase 2 — each join edge is a candidate 2-table CSG-CMP pair";
  });
  var edges = stageState.graph.edges;
  for (var e = 0; e < edges.length; e++) {
    (function(edgeRec) {
      tl.call(function() {
        highlightEdge([edgeRec.from, edgeRec.to], "#fbbf24", 4);
        highlightVertex(edgeRec.from, "#fcd34d", "#d97706");
        highlightVertex(edgeRec.to,   "#fcd34d", "#d97706");
        setCsgLabel(
          [edgeRec.from, edgeRec.to],
          "pair: { " + edgeRec.from + " } ⋈ { " + edgeRec.to + " }",
          "best plan for this 2-set kept in DP table"
        );
        bumpCounter(2);
      });
      tl.delay(220);
    })(edges[e]);
  }
  tl.delay(160);

  // ---- Phase 3: grow CSGs by adjacent vertex (size 3, 4, ...) ----
  tl.mark("Phase 3: grow CSGs by adjacent vertex");
  tl.call(function() {
    phaseLabel.textContent = "Phase 3 — extend each CSG with adjacent vertices via EnumerateCsgRec";
  });
  // Demonstrate growing from one seed (u) outward.
  var seed = tables[0];
  var grown = [seed];
  var adjacency = {};
  for (var ei = 0; ei < edges.length; ei++) {
    adjacency[edges[ei].from] = adjacency[edges[ei].from] || [];
    adjacency[edges[ei].to]   = adjacency[edges[ei].to]   || [];
    adjacency[edges[ei].from].push(edges[ei].to);
    adjacency[edges[ei].to].push(edges[ei].from);
  }
  // BFS-style growth, capped at min(N, 5) steps for readability.
  var growthSteps = Math.min(N, 5);
  for (var step = 1; step < growthSteps; step++) {
    (function(stepIdx) {
      tl.call(function() {
        // Find a neighbor not yet in CSG.
        var candidates = [];
        for (var g = 0; g < grown.length; g++) {
          var adj = adjacency[grown[g]] || [];
          for (var j = 0; j < adj.length; j++) {
            if (grown.indexOf(adj[j]) < 0 && candidates.indexOf(adj[j]) < 0) {
              candidates.push(adj[j]);
            }
          }
        }
        if (candidates.length === 0) return;
        var next = candidates[0];
        grown.push(next);
        for (var v = 0; v < grown.length; v++) {
          highlightVertex(grown[v], "#a7f3d0", "#059669");
        }
        // Highlight all edges within the CSG.
        for (var x = 0; x < grown.length; x++) {
          for (var y = x + 1; y < grown.length; y++) {
            highlightEdge([grown[x], grown[y]], "#34d399", 3);
          }
        }
        setCsgLabel(
          grown.slice(),
          "size: " + grown.length + " of " + N,
          "CSG grew via connecting edge"
        );
        bumpCounter(Math.max(1, Math.round(totalPairs / (growthSteps + edges.length))));
      });
      tl.delay(360);
    })(step);
  }
  tl.delay(160);

  // ---- Phase 4: final plan (bushy possible) ----
  tl.mark("Phase 4: lock in plan (DPhyp may emit a bushy tree)");
  tl.call(function() {
    phaseLabel.textContent = "✓ DPhyp complete — " + teachRuntime.formatInt(totalPairs) + " subgraph pairs evaluated; plan can be bushy";
    if (stageState.plansCounter) {
      stageState.plansCounter.textContent = teachRuntime.formatInt(totalPairs);
    }
    // Color every vertex green to indicate it's in the final plan.
    for (var t = 0; t < tables.length; t++) {
      highlightVertex(tables[t], "#bbf7d0", "#047857");
    }
    for (var ei2 = 0; ei2 < edges.length; ei2++) {
      highlightEdge([edges[ei2].from, edges[ei2].to], "#10b981", 3);
    }
    setCsgLabel(tables.slice(), "size: " + N + " of " + N + " (final)", "DP table now has best plan for { all tables }");
    if (stageState.finalPlanText) {
      // Render a bushy-style notation: ((u⋈o)⋈(li⋈p)) ⋈ ((s)⋈(c⋈w))
      // Just show the table set; the bushy structure is implied.
      stageState.finalPlanText.textContent =
        "(bushy) " + tables.slice(0, Math.ceil(N / 2)).join("⋈") +
        " ⋈ " + tables.slice(Math.ceil(N / 2)).join("⋈");
      stageState.finalPlanText.setAttribute("opacity", 1);
    }
  });
  return tl;
}

// =============================================================================
// Dispatchers
// =============================================================================

function buildStage(N, depth, algo) {
  stageState.algo = algo;
  if (algo === "hypergraph") {
    buildHypergraphStage(N);
  } else {
    buildGreedyStage(N, depth);
  }
}

function resetStageColors() {
  if (stageState.algo === "hypergraph") {
    resetHypergraphColors();
  } else {
    resetGreedyColors();
  }
  if (stageState.plansCounter) stageState.plansCounter.textContent = "0";
  if (stageState.finalPlanText) {
    stageState.finalPlanText.textContent = "— (run search to see) —";
    stageState.finalPlanText.setAttribute("opacity", 0.35);
  }
  for (var j = 0; j < stageState.tableCards.length; j++) {
    stageState.tableCards[j].rect.setAttribute("fill", "#ffffff");
    stageState.tableCards[j].rect.setAttribute("stroke", "#3b82f6");
  }
}

function buildSearchTimeline(controls) {
  resetStageColors();
  if (controls.algo === "hypergraph") {
    return buildHypergraphTimeline(controls);
  }
  return buildGreedyTimeline(controls);
}

// =============================================================================
// Complexity chart
// =============================================================================

function renderChart(currentN, depth, prune, algo) {
  function greedyCurve(d) {
    return function(n) {
      var p = plansEvaluated(n, Math.min(d, n));
      var pr = applyPruning(p, prune).plans;
      return Math.max(1, pr);
    };
  }
  var curves = [
    {
      label: "Greedy d=1: O(N²)",
      color: "#10b981",
      fn: greedyCurve(1)
    },
    {
      label: "Greedy d=" + Math.min(depth, currentN) + " (current setting)",
      color: "#3b82f6",
      fn: greedyCurve(depth)
    },
    {
      label: "Greedy exhaustive: O(N!)",
      color: "#dc2626",
      fn: function(n) {
        var p = plansEvaluated(n, n);
        var pr = applyPruning(p, prune).plans;
        return Math.max(1, pr);
      }
    },
    {
      label: "Hypergraph DPhyp (tree query): O(N³)",
      color: "#ec4899",
      fn: function(n) {
        return Math.min(dphypTreePairs(n), OPTIMIZER_MAX_SUBGRAPH_PAIRS);
      }
    }
  ];
  if (algo === "hypergraph") {
    // Reorder so the hypergraph curve is most prominent.
    curves.unshift(curves.splice(3, 1)[0]);
  }
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 2, xMax: 14,
    xLabel: "Tables in query (N)", yLabel: "Plans / subgraph pairs evaluated",
    curves: curves,
    current: { x: currentN },
    xSlider: "tables",
    xSliderTransform: function(xVal) {
      return xVal;
    }
  });
}

// =============================================================================
// Recompute
// =============================================================================

function recompute() {
  var c = teachRuntime.readControls();
  var N = Math.round(c.tables);
  var rawDepth = Math.round(c.search_depth);
  var pillDepth = document.querySelector('[data-pill-for="search_depth"]');
  if (pillDepth) pillDepth.textContent = (rawDepth >= 10) ? "62 (max)" : String(rawDepth);
  var depth = (rawDepth >= 10) ? 62 : rawDepth;
  var prune = Number(c.prune_level);
  var algo = c.algo;

  var pillN = document.querySelector('[data-pill-for="tables"]');
  if (pillN) pillN.textContent = String(N);

  document.getElementById("out-n").textContent = String(N);

  if (algo === "hypergraph") {
    var pairs = Math.min(dphypTreePairs(N), OPTIMIZER_MAX_SUBGRAPH_PAIRS);
    document.getElementById("out-depth").textContent = "n/a (set-based DP)";
    document.getElementById("out-plans").textContent = teachRuntime.formatInt(pairs);
    document.getElementById("out-complexity").textContent = bigOLabelDPhyp();
    document.getElementById("out-prune").textContent = "n/a (DP, no prune_level)";
    document.getElementById("out-time").textContent = teachRuntime.formatMs(pairs * 0.001);
    document.getElementById("out-explanation").textContent =
      "Hypergraph optimizer (DPhyp, Moerkotte & Neumann 2006). Treats the query as a graph: vertices = tables, edges = join predicates. Enumerates connected subgraphs bottom-up via the DP recurrence: for each CSG, find a non-overlapping CMP whose connecting edge merges them. Capped by optimizer_max_subgraph_pairs = " +
      teachRuntime.formatInt(OPTIMIZER_MAX_SUBGRAPH_PAIRS) +
      ". For our 7-table tree query, ~57 pairs vs greedy's worst-case 5,040 orderings — and DPhyp can emit a bushy plan tree.";
  } else {
    var effDepth = Math.min(depth, N);
    var raw = plansEvaluated(N, effDepth);
    var pruned = applyPruning(raw, prune);
    var plans = pruned.plans;
    document.getElementById("out-depth").textContent = String(effDepth);
    document.getElementById("out-plans").textContent = teachRuntime.formatInt(plans);
    document.getElementById("out-complexity").textContent = bigOLabel(N, effDepth);
    document.getElementById("out-prune").textContent =
      (prune === 1) ? (Math.round(pruned.ratio * 100) + "%") : "off";
    document.getElementById("out-time").textContent = teachRuntime.formatMs(plans * 0.001);

    var explanation;
    if (effDepth >= N && prune === 0) {
      explanation =
        "Exhaustive enumeration: every one of N! left-deep orders is costed. At N=" + N + " that's " + teachRuntime.formatInt(fact(N)) + " plans. Beyond N≈12 this is what tanks query compilation time on giant analytical queries.";
    } else if (effDepth >= N && prune === 1) {
      explanation =
        "Exhaustive but pruned: optimizer_prune_level=1 cuts branches whose partial cost already exceeds the best complete plan found so far. The shape is still O(N!), the constant is ~4-10× smaller.";
    } else if (effDepth === 1) {
      explanation =
        "Pure greedy: at each step pick the locally-cheapest next table. O(N²) plans. Fast but can miss the globally-optimal order — that is why MySQL's default lookahead is much larger.";
    } else {
      explanation =
        "Bounded lookahead: at each commit point the planner enumerates the next " + effDepth + " tables exhaustively, then commits one. Cost O(N · N^" + effDepth + " / " + effDepth + "). Reduce optimizer_search_depth on session queries that re-plan repeatedly.";
    }
    document.getElementById("out-explanation").textContent = explanation;
  }

  buildStage(N, (rawDepth >= 10) ? 62 : Math.min(rawDepth, N), algo);
  resetStageColors();
  renderChart(N, depth, prune, algo);
}

function resetAnim() {
  resetStageColors();
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: function() {
    return buildSearchTimeline(teachRuntime.readControls());
  },
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: function() {
    return buildSearchTimeline(teachRuntime.readControls());
  },
  reset: resetAnim
});
