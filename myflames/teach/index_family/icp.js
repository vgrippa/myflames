function icpCost(indexRows, selectivity) {
  var without = indexRows;
  var withIcp = Math.max(1, Math.floor(indexRows * selectivity));
  return {without: without, withIcp: withIcp, saved: without - withIcp};
}

var W = 800, H = 480;
var stage = null;

// Sample index entries the user can follow.
// The index is on (last_name, first_name). The range scan uses last_name LIKE 'S%',
// and the ICP condition is first_name LIKE 'J%'.
// "match" means first_name starts with J.
var INDEX_ENTRIES = [
  {last: "Sanders", first: "Amy",    match: false},
  {last: "Santos",  first: "Jorge",  match: true},
  {last: "Schmidt", first: "Julia",  match: true},
  {last: "Scott",   first: "Derek",  match: false},
  {last: "Shaw",    first: "Janet",  match: true},
  {last: "Silva",   first: "Marco",  match: false},
  {last: "Smith",   first: "James",  match: true},
  {last: "Snyder",  first: "Paul",   match: false},
  {last: "Stone",   first: "Jill",   match: true},
  {last: "Sullivan",first: "Mike",   match: false}
];

function buildStage() {
  var svg = document.getElementById("icp-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var midX = W / 2;

  // Divider
  var divider = anim.svgEl("line", {
    x1: midX, y1: 0, x2: midX, y2: H,
    stroke: "#e5e7eb", "stroke-width": 2, "stroke-dasharray": "6,4"
  });
  svg.appendChild(divider);

  // Panel titles + subtitles explaining each side
  var leftTitle = anim.svgEl("text", {
    x: midX / 2, y: 20, "text-anchor": "middle",
    "font-size": 14, "font-weight": 700, fill: "#dc2626"
  });
  leftTitle.textContent = "\u2718 Without ICP";
  svg.appendChild(leftTitle);
  var leftSub = anim.svgEl("text", {
    x: midX / 2, y: 36, "text-anchor": "middle",
    "font-size": 10, fill: "#6b7280"
  });
  leftSub.textContent = "Always fetch row, THEN check first_name";
  svg.appendChild(leftSub);

  var rightTitle = anim.svgEl("text", {
    x: midX + midX / 2, y: 20, "text-anchor": "middle",
    "font-size": 14, "font-weight": 700, fill: "#059669"
  });
  rightTitle.textContent = "\u2714 With ICP";
  svg.appendChild(rightTitle);
  var rightSub = anim.svgEl("text", {
    x: midX + midX / 2, y: 36, "text-anchor": "middle",
    "font-size": 10, fill: "#6b7280"
  });
  rightSub.textContent = "Check first_name on INDEX first, skip if no J";
  svg.appendChild(rightSub);

  // Column headers for the table-like layout
  var headerY = 52;
  // Left headers
  var lhName = anim.svgEl("text", {
    x: 14, y: headerY, "font-size": 9, "font-weight": 700, fill: "#6b7280",
    "text-transform": "uppercase"
  });
  lhName.textContent = "INDEX ENTRY";
  svg.appendChild(lhName);
  var lhFirst = anim.svgEl("text", {
    x: 155, y: headerY, "font-size": 9, "font-weight": 700, fill: "#6b7280"
  });
  lhFirst.textContent = "FIRST_NAME";
  svg.appendChild(lhFirst);
  var lhResult = anim.svgEl("text", {
    x: midX - 14, y: headerY, "text-anchor": "end",
    "font-size": 9, "font-weight": 700, fill: "#6b7280"
  });
  lhResult.textContent = "RESULT";
  svg.appendChild(lhResult);
  // Right headers
  var rhName = anim.svgEl("text", {
    x: midX + 14, y: headerY, "font-size": 9, "font-weight": 700, fill: "#6b7280"
  });
  rhName.textContent = "INDEX ENTRY";
  svg.appendChild(rhName);
  var rhFirst = anim.svgEl("text", {
    x: midX + 155, y: headerY, "font-size": 9, "font-weight": 700, fill: "#6b7280"
  });
  rhFirst.textContent = "FIRST_NAME";
  svg.appendChild(rhFirst);
  var rhResult = anim.svgEl("text", {
    x: W - 14, y: headerY, "text-anchor": "end",
    "font-size": 9, "font-weight": 700, fill: "#6b7280"
  });
  rhResult.textContent = "RESULT";
  svg.appendChild(rhResult);

  // Index entries (both sides) — table-like rows
  var entryH = 32;
  var startY = 62;
  var leftEntries = [];
  var rightEntries = [];

  for (var i = 0; i < INDEX_ENTRIES.length; i++) {
    var e = INDEX_ENTRIES[i];
    var y = startY + i * entryH;
    // Highlight the first letter of first_name in the label
    var nameLabel = e.last + ", ";
    var firstLetter = e.first.charAt(0);
    var firstRest = e.first.substring(1);

    // ---- Left panel entry ----
    var lbg = anim.svgEl("rect", {
      x: 8, y: y, width: midX - 16, height: entryH - 4, rx: 5, ry: 5,
      fill: "#fafafa", stroke: "#e5e7eb", "stroke-width": 1
    });
    svg.appendChild(lbg);
    // Name part (last_name + comma)
    var ltext = anim.svgEl("text", {
      x: 16, y: y + 19, "font-size": 11, "font-weight": 600, fill: "#374151"
    });
    ltext.textContent = nameLabel;
    svg.appendChild(ltext);
    // First name — bold first letter to show J/not-J
    var lfirst = anim.svgEl("text", {
      x: 155, y: y + 19, "font-size": 11, fill: "#374151"
    });
    svg.appendChild(lfirst);
    var lfBold = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
    lfBold.setAttribute("font-weight", "800");
    lfBold.setAttribute("font-size", "13");
    lfBold.setAttribute("fill", e.match ? "#16a34a" : "#dc2626");
    lfBold.textContent = firstLetter;
    lfirst.appendChild(lfBold);
    var lfRest = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
    lfRest.setAttribute("font-weight", "400");
    lfRest.textContent = firstRest;
    lfirst.appendChild(lfRest);
    // Status icon — right-aligned
    var licon = anim.svgEl("text", {
      x: midX - 20, y: y + 19, "text-anchor": "end",
      "font-size": 10, "font-weight": 700, fill: "#9ca3af"
    });
    licon.textContent = "";
    svg.appendChild(licon);
    leftEntries.push({bg: lbg, icon: licon, match: e.match, first: e.first});

    // ---- Right panel entry ----
    var rbg = anim.svgEl("rect", {
      x: midX + 8, y: y, width: midX - 16, height: entryH - 4, rx: 5, ry: 5,
      fill: "#fafafa", stroke: "#e5e7eb", "stroke-width": 1
    });
    svg.appendChild(rbg);
    var rtext = anim.svgEl("text", {
      x: midX + 16, y: y + 19, "font-size": 11, "font-weight": 600, fill: "#374151"
    });
    rtext.textContent = nameLabel;
    svg.appendChild(rtext);
    var rfirst = anim.svgEl("text", {
      x: midX + 155, y: y + 19, "font-size": 11, fill: "#374151"
    });
    svg.appendChild(rfirst);
    var rfBold = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
    rfBold.setAttribute("font-weight", "800");
    rfBold.setAttribute("font-size", "13");
    rfBold.setAttribute("fill", e.match ? "#16a34a" : "#dc2626");
    rfBold.textContent = firstLetter;
    rfirst.appendChild(rfBold);
    var rfRest = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
    rfRest.setAttribute("font-weight", "400");
    rfRest.textContent = firstRest;
    rfirst.appendChild(rfRest);
    var ricon = anim.svgEl("text", {
      x: W - 20, y: y + 19, "text-anchor": "end",
      "font-size": 10, "font-weight": 700, fill: "#9ca3af"
    });
    ricon.textContent = "";
    svg.appendChild(ricon);
    rightEntries.push({bg: rbg, icon: ricon, match: e.match, first: e.first});
  }

  // Summary boxes at bottom
  var boxY = startY + INDEX_ENTRIES.length * entryH + 12;

  // Left summary box
  var lsBox = anim.svgEl("rect", {
    x: 8, y: boxY, width: midX - 16, height: 50, rx: 6, ry: 6,
    fill: "#fef2f2", stroke: "#fca5a5", "stroke-width": 1
  });
  svg.appendChild(lsBox);
  var leftCounter = anim.svgEl("text", {
    x: midX / 2, y: boxY + 22, "text-anchor": "middle",
    "font-size": 15, "font-weight": 700, fill: "#dc2626"
  });
  leftCounter.textContent = "Row fetches: 0 of 10";
  svg.appendChild(leftCounter);
  var leftSummary = anim.svgEl("text", {
    x: midX / 2, y: boxY + 40, "text-anchor": "middle",
    "font-size": 10, fill: "#991b1b"
  });
  leftSummary.textContent = "Every row fetched before checking first_name";
  svg.appendChild(leftSummary);

  // Right summary box
  var rsBox = anim.svgEl("rect", {
    x: midX + 8, y: boxY, width: midX - 16, height: 50, rx: 6, ry: 6,
    fill: "#ecfdf5", stroke: "#86efac", "stroke-width": 1
  });
  svg.appendChild(rsBox);
  var rightCounter = anim.svgEl("text", {
    x: midX + midX / 2, y: boxY + 22, "text-anchor": "middle",
    "font-size": 15, "font-weight": 700, fill: "#059669"
  });
  rightCounter.textContent = "Row fetches: 0 of 10";
  svg.appendChild(rightCounter);
  var rightSummary = anim.svgEl("text", {
    x: midX + midX / 2, y: boxY + 40, "text-anchor": "middle",
    "font-size": 10, fill: "#065f46"
  });
  rightSummary.textContent = "Only rows where first_name starts with J are fetched";
  svg.appendChild(rightSummary);

  var statusLbl = anim.svgEl("text", {
    x: W / 2, y: H - 10, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "#1f2937"
  });
  statusLbl.textContent = "";
  svg.appendChild(statusLbl);

  stage = {
    svg: svg, leftEntries: leftEntries, rightEntries: rightEntries,
    leftCounter: leftCounter, rightCounter: rightCounter,
    leftSummary: leftSummary, rightSummary: rightSummary,
    statusLbl: statusLbl, tuples: []
  };
}

function resetStage() {
  if (!stage) return;
  for (var i = 0; i < stage.leftEntries.length; i++) {
    stage.leftEntries[i].bg.setAttribute("fill", "#fafafa");
    stage.leftEntries[i].bg.setAttribute("stroke", "#e5e7eb");
    stage.leftEntries[i].icon.textContent = "";
    stage.rightEntries[i].bg.setAttribute("fill", "#fafafa");
    stage.rightEntries[i].bg.setAttribute("stroke", "#e5e7eb");
    stage.rightEntries[i].icon.textContent = "";
  }
  stage.leftCounter.textContent = "Row fetches: 0 of " + INDEX_ENTRIES.length;
  stage.rightCounter.textContent = "Row fetches: 0 of " + INDEX_ENTRIES.length;
  stage.leftSummary.textContent = "Every row fetched before checking first_name";
  stage.rightSummary.textContent = "Only rows where first_name starts with J are fetched";
  stage.statusLbl.textContent = "";
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var leftFetches = 0;
  var rightFetches = 0;
  var total = INDEX_ENTRIES.length;

  tl.mark("Range scan on last_name");
  tl.call(function() {
    phase.textContent = "Range scan: last_name LIKE 'S%' \u2014 found " + total + " entries in the secondary index";
  });
  tl.delay(600);

  for (var i = 0; i < INDEX_ENTRIES.length; i++) {
    (function(idx) {
      var entry = INDEX_ENTRIES[idx];
      var name = entry.last + ", " + entry.first;
      var startsWithJ = entry.first.charAt(0) === "J";

      // Add a phase mark every 3 entries
      if (idx % 3 === 0) {
        tl.mark("Check " + entry.last + ", " + entry.first);
      }

      // ---- Left panel: fetch first, check later ----
      tl.call(function() {
        var le = stage.leftEntries[idx];
        // Step 1: always fetch the full row from clustered index
        le.bg.setAttribute("fill", "#fef2f2");
        le.bg.setAttribute("stroke", "#f87171");
        le.icon.textContent = "\u2192 fetching row\u2026";
        le.icon.setAttribute("fill", "#dc2626");
        leftFetches++;
        stage.leftCounter.textContent = "Row fetches: " + leftFetches + " of " + total;
        phase.textContent = "Without ICP: " + name + " \u2192 fetch full row from clustered index (always, regardless of first_name)";
      });
      tl.delay(250);
      tl.call(function() {
        var le = stage.leftEntries[idx];
        if (startsWithJ) {
          // Step 2: server checks first_name — match!
          le.bg.setAttribute("fill", "#dcfce7");
          le.bg.setAttribute("stroke", "#22c55e");
          le.icon.textContent = entry.first + " starts with J \u2714";
          le.icon.setAttribute("fill", "#16a34a");
        } else {
          // Step 2: server checks first_name — fail! Wasted I/O.
          le.bg.setAttribute("fill", "#fee2e2");
          le.bg.setAttribute("stroke", "#fca5a5");
          le.icon.textContent = entry.first + " \u2260 J% \u2718 wasted!";
          le.icon.setAttribute("fill", "#dc2626");
        }
      });

      // ---- Right panel: check ICP condition on the index first ----
      tl.call(function() {
        var re = stage.rightEntries[idx];
        // Step 1: InnoDB checks first_name on the index entry
        re.bg.setAttribute("fill", "#fffbeb");
        re.bg.setAttribute("stroke", "#f59e0b");
        re.icon.textContent = entry.first + " starts with J?";
        re.icon.setAttribute("fill", "#b45309");
        phase.textContent = "With ICP: " + name + " \u2014 InnoDB checks: does \"" + entry.first + "\" start with J?";
      });
      tl.delay(350);
      tl.call(function() {
        var re = stage.rightEntries[idx];
        if (startsWithJ) {
          // Yes \u2192 fetch the row
          re.bg.setAttribute("fill", "#dcfce7");
          re.bg.setAttribute("stroke", "#22c55e");
          re.icon.textContent = entry.first + " = J\u2026 \u2192 fetch \u2714";
          re.icon.setAttribute("fill", "#16a34a");
          rightFetches++;
          stage.rightCounter.textContent = "Row fetches: " + rightFetches + " of " + total;
        } else {
          // No \u2192 skip entirely, no row fetch
          re.bg.setAttribute("fill", "#f0fdf4");
          re.bg.setAttribute("stroke", "#bbf7d0");
          re.icon.textContent = entry.first + " \u2260 J\u2026 \u2192 SKIP";
          re.icon.setAttribute("fill", "#059669");
        }
      });
      tl.delay(280);
    })(i);
  }

  tl.delay(400);
  tl.mark("Summary");
  tl.call(function() {
    var matches = 0;
    for (var j = 0; j < INDEX_ENTRIES.length; j++) if (INDEX_ENTRIES[j].match) matches++;
    var saved = total - matches;
    phase.textContent = "\u2713 Done \u2014 Without ICP: " + total + " row fetches. With ICP: " + matches + ". Saved " + saved + " wasted fetches!";
    stage.leftSummary.textContent = "Fetched ALL " + total + " rows, then filtered \u2014 " + saved + " were wasted";
    stage.rightSummary.textContent = "Only fetched " + matches + " rows where first_name starts with J";
    stage.statusLbl.textContent = "ICP saved " + saved + " unnecessary clustered-index lookups by checking the index first.";
  });

  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}
function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
}

function renderChart(currentRows) {
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 100, xMax: 1e6,
    xLabel: "Index rows matching range scan", yLabel: "Clustered-index row fetches",
    curves: [
      { label: "Without ICP (all rows fetched)", color: "#dc2626",
        fn: function(n) { return n; } },
      { label: "With ICP (20% selectivity)", color: "#059669",
        fn: function(n) { return Math.max(1, Math.floor(n * 0.2)); } },
      { label: "With ICP (5% selectivity)", color: "#0284c7",
        fn: function(n) { return Math.max(1, Math.floor(n * 0.05)); } }
    ],
    current: { x: currentRows },
    xSlider: "index_rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(100, Math.min(1000000, xVal / 100) * 100)); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = icpCost(c.index_rows, c.selectivity / 100);
  document.getElementById("out-scanned").textContent = teachRuntime.formatInt(cost.without);
  document.getElementById("out-without").textContent = teachRuntime.formatInt(cost.without);
  document.getElementById("out-with").textContent = teachRuntime.formatInt(cost.withIcp);
  document.getElementById("out-saved").textContent = teachRuntime.formatInt(cost.saved);
  document.getElementById("out-saved").className = "value " + (cost.saved > 0 ? "ok" : "");
  document.getElementById("out-explanation").textContent =
    "Without ICP: all " + teachRuntime.formatInt(cost.without) + " index rows trigger a " +
    "clustered-index row fetch. With ICP: only " + teachRuntime.formatInt(cost.withIcp) +
    " rows (" + c.selectivity + "%) pass the pushed condition \u2014 " +
    teachRuntime.formatInt(cost.saved) + " row fetches saved.";
  renderChart(c.index_rows);
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
buildStage();
