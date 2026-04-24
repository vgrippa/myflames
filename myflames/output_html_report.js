function exportSVG() {
  var svg = document.querySelector('#chart-panel svg');
  if (!svg) { alert('No SVG found'); return; }
  var data = new XMLSerializer().serializeToString(svg);
  var blob = new Blob(['<?xml version="1.0" standalone="no"?>\n', data], {type: 'image/svg+xml'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'query-plan.svg';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}

function exportJSON() {
  // Export the embedded sidecar (same data the JSON-LD script carries).
  var script = document.querySelector('script[type="application/ld+json"]');
  if (!script) { alert('No sidecar found'); return; }
  var blob = new Blob([script.textContent], {type: 'application/json'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'query-analysis.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}

(function wireTeachBridge() {
  var hooks = window.__MYFLAMES_TEACH_HOOKS || [];
  if (!hooks.length) return;
  var complexityMap = window.__MYFLAMES_COMPLEXITY || {};
  var complexityCharts = window.__MYFLAMES_COMPLEXITY_CHARTS || {};

  var chart = document.getElementById("chart-panel");
  var ctaBtn = document.getElementById("open-teach-btn");
  var ctaHint = document.getElementById("teach-cta-hint");
  var dialog = document.getElementById("teach-dialog");
  var frame = document.getElementById("teach-dialog-frame");
  var closeBtn = document.getElementById("teach-dialog-close");
  var subtitle = document.getElementById("teach-dialog-subtitle");
  var selectedIndex = -1;

  function escapeHTML(s) {
    return String(s || "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Build an HTML fragment that renders the Big O complexity for a given
  // operator — inserted inline at the bottom of each teach lesson so it's
  // just part of the lesson's regular content flow (no modal panel, no
  // floating overlay). Returns "" if we have no complexity for the op.
  function complexityFragmentFor(hook) {
    var folded = (hook && hook.match && hook.match.folded_label) || "";
    var info = folded ? complexityMap[folded] : null;
    if (!info || !info.big_o) return "";
    var chartSvg = complexityCharts[info.kind] || complexityCharts[""] || "";
    var conf = info.confidence || "exact";
    var confLine = "";
    if (conf !== "exact") {
      var confText = conf === "typical"
        ? "Confidence: typical (EXPLAIN does not expose every parameter)."
        : "Confidence: worst-case upper bound.";
      confLine = '<p class="mf-complexity-confidence">' + escapeHTML(confText) + "</p>";
    }
    var sevClass = "mf-sev-" + (info.severity || "medium");
    return ""
      + '<style>'
      + '  .mf-complexity-section { margin: 28px auto 16px; max-width: 900px; '
      +       'padding: 18px 22px 22px; background: #f8fafc; '
      +       'border: 1px solid #e2e8f0; border-radius: 10px; '
      +       'font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; }'
      + '  .mf-complexity-header { display: flex; justify-content: space-between; '
      +       'align-items: center; gap: 14px; margin-bottom: 10px; flex-wrap: wrap; }'
      + '  .mf-complexity-header h3 { margin: 0; font-size: 15px; font-weight: 700; '
      +       'color: #0f172a; letter-spacing: 0.1px; }'
      + '  .mf-complexity-badge { display: inline-block; padding: 4px 12px; '
      +       'border-radius: 999px; font-family: ui-monospace, Menlo, monospace; '
      +       'font-size: 13px; font-weight: 700; color: #0f172a; '
      +       'border: 1px solid rgba(0,0,0,0.12); }'
      + '  .mf-complexity-badge.mf-sev-good { background: rgb(100,180,180); }'
      + '  .mf-complexity-badge.mf-sev-medium { background: rgb(255,200,50); }'
      + '  .mf-complexity-badge.mf-sev-bad { background: rgb(255,90,90); color: #1a0000; }'
      + '  .mf-complexity-rationale { margin: 0 0 10px; color: #1f2937; '
      +       'font-size: 13.5px; line-height: 1.5; }'
      + '  .mf-complexity-confidence { margin: 0 0 10px; color: #64748b; '
      +       'font-size: 12px; font-style: italic; }'
      + '  .mf-complexity-chart { overflow-x: auto; }'
      + '  .mf-complexity-chart svg { max-width: 100%; height: auto; display: block; margin: 0 auto; }'
      + '</style>'
      + '<section class="mf-complexity-section" aria-labelledby="mf-complexity-heading">'
      +   '<header class="mf-complexity-header">'
      +     '<h3 id="mf-complexity-heading">Big O complexity</h3>'
      +     '<span class="mf-complexity-badge ' + sevClass + '">' + escapeHTML(info.big_o) + '</span>'
      +   '</header>'
      +   '<p class="mf-complexity-rationale">' + escapeHTML(info.rationale || "") + '</p>'
      +   confLine
      +   '<div class="mf-complexity-chart">' + chartSvg + '</div>'
      + '</section>';
  }

  function hookAt(idx) {
    var i = Number(idx);
    if (!isFinite(i) || i < 0 || i >= hooks.length) return null;
    return hooks[i];
  }
  function findTeachTarget(start) {
    var el = start;
    while (el && el !== document.body) {
      if (el.getAttribute && el.getAttribute("data-teach-index")) return el;
      el = el.parentNode;
    }
    return null;
  }
  function srcDocForHook(hook) {
    var lesson = hook.lesson;
    var tpl = document.getElementById("teach-tpl-" + lesson);
    if (!tpl) return "";
    var html = tpl.innerHTML || "";
    var payload = {
      controls: hook.controls || {},
      query_sql: hook.query_sql || "",
      note: hook.note || ""
    };
    // Bootstrap the lesson's teachRuntime controls.
    var bootstrap = "<script>(function(){"
      + "var ctx=" + JSON.stringify(payload) + ";"
      + "function run(){"
      + "if(window.teachRuntime&&typeof window.teachRuntime.bootstrapFromObject==='function'){"
      + "window.teachRuntime.bootstrapFromObject(ctx);"
      + "}"
      + "}"
      + "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',run,{once:true});}else{run();}"
      + "})();<\/script>";
    // Append the Big O complexity section as normal in-page content —
    // after the lesson's main content but BEFORE the lesson's <footer>
    // ("Generated by myflames teach — …") so that footer stays as the
    // last visible element on the page.
    var complexity = complexityFragmentFor(hook);
    if (complexity && /<footer\b/i.test(html)) {
      html = html.replace(/<footer\b/i, complexity + "<footer");
    } else if (complexity && /<\/main>/i.test(html)) {
      html = html.replace(/<\/main>/i, "</main>" + complexity);
    } else {
      // Last resort — fall back to just before </body>.
      html = /<\/body>/i.test(html)
        ? html.replace(/<\/body>/i, complexity + "</body>")
        : html + complexity;
    }
    // Bootstrap script goes before </body> as before.
    if (/<\/body>/i.test(html)) {
      return html.replace(/<\/body>/i, bootstrap + "</body>");
    }
    return html + bootstrap;
  }
  function resizeFrameToContent() {
    // Iframe content scrolling inside the dialog made the Big O panel at
    // the bottom feel "pinned" — the outer dialog didn't scroll, the iframe
    // did. We auto-size the iframe to its content's scrollHeight so the
    // whole dialog scrolls as one unit and the panel reads as a page-end
    // footer. Runs after the lesson's DOM has settled.
    if (!frame) return;
    try {
      var doc = frame.contentDocument;
      if (!doc || !doc.documentElement) return;
      var h = Math.max(
        doc.documentElement.scrollHeight || 0,
        doc.body ? (doc.body.scrollHeight || 0) : 0,
        640
      );
      frame.style.height = h + "px";
    } catch (_e) { /* cross-origin; fall back to CSS height */ }
  }
  function openForIndex(idx) {
    var hook = hookAt(idx);
    if (!hook || !dialog || !frame) return;
    selectedIndex = Number(idx);
    var lesson = hook.lesson || "lesson";
    if (subtitle) {
      subtitle.textContent = (hook.note || "").slice(0, 180) || ("Lesson: " + lesson);
    }
    // Attach the load handler BEFORE setting srcdoc so we can't race the
    // load event. Size the iframe once on load, and again a beat later to
    // catch post-load async content growth (some lessons mount widgets
    // after DOMContentLoaded).
    frame.addEventListener("load", function onload() {
      resizeFrameToContent();
      setTimeout(resizeFrameToContent, 300);
    }, { once: true });
    frame.setAttribute("srcdoc", srcDocForHook(hook));
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  }
  function closeTeachDialog() {
    if (!dialog || !dialog.open) return;
    dialog.close();
    if (frame) frame.removeAttribute("srcdoc");
  }
  if (chart) {
    // Use capture phase so we see the click before SVG-internal handlers
    // call stopPropagation (diagram, flamegraph, tree all do this).
    chart.addEventListener("click", function(e) {
      var t = findTeachTarget(e.target);
      if (!t) return;
      var idx = t.getAttribute("data-teach-index");
      var hook = hookAt(idx);
      if (!hook) return;
      selectedIndex = Number(idx);
      if (ctaBtn) ctaBtn.hidden = false;
      if (ctaHint) {
        ctaHint.textContent = "Selected: " + (hook.note || hook.lesson || "operator");
      }
    }, true);
  }
  if (ctaBtn) {
    ctaBtn.addEventListener("click", function() {
      if (selectedIndex >= 0) openForIndex(selectedIndex);
    });
  }
  if (closeBtn) closeBtn.addEventListener("click", closeTeachDialog);
  if (dialog) {
    dialog.addEventListener("click", function(evt) {
      var rect = dialog.getBoundingClientRect();
      var inside = (
        evt.clientX >= rect.left &&
        evt.clientX <= rect.right &&
        evt.clientY >= rect.top &&
        evt.clientY <= rect.bottom
      );
      if (!inside) closeTeachDialog();
    });
    dialog.addEventListener("close", function() {
      if (frame) frame.removeAttribute("srcdoc");
    });
  }
  window.addEventListener("keydown", function(e) {
    if (e.key === "Escape") closeTeachDialog();
  });
})();
