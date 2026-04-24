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

  var chart = document.getElementById("chart-panel");
  var ctaBtn = document.getElementById("open-teach-btn");
  var ctaHint = document.getElementById("teach-cta-hint");
  var dialog = document.getElementById("teach-dialog");
  var frame = document.getElementById("teach-dialog-frame");
  var closeBtn = document.getElementById("teach-dialog-close");
  var subtitle = document.getElementById("teach-dialog-subtitle");
  var complexityPanel = document.getElementById("teach-complexity-panel");
  var complexityBadge = document.getElementById("teach-complexity-badge");
  var complexityRationale = document.getElementById("teach-complexity-rationale");
  var complexityConfidence = document.getElementById("teach-complexity-confidence");
  var selectedIndex = -1;

  function populateComplexityFor(hook) {
    if (!complexityPanel) return;
    var folded = (hook && hook.match && hook.match.folded_label) || "";
    var info = folded ? complexityMap[folded] : null;
    if (!info || !info.big_o) {
      complexityPanel.hidden = true;
      complexityPanel.removeAttribute("data-kind");
      return;
    }
    complexityPanel.hidden = false;
    complexityPanel.setAttribute("data-severity", info.severity || "medium");
    complexityPanel.setAttribute("data-kind", info.kind || "");
    if (complexityBadge) {
      complexityBadge.textContent = info.big_o;
      complexityBadge.setAttribute("data-severity", info.severity || "medium");
    }
    if (complexityRationale) {
      complexityRationale.textContent = info.rationale || "";
    }
    if (complexityConfidence) {
      var conf = info.confidence || "exact";
      if (conf === "exact") {
        complexityConfidence.hidden = true;
        complexityConfidence.textContent = "";
      } else {
        complexityConfidence.hidden = false;
        complexityConfidence.textContent =
          conf === "typical" ? "Confidence: typical (EXPLAIN does not expose every parameter)."
          : "Confidence: worst-case upper bound.";
      }
    }
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
    var inject = "<script>(function(){"
      + "var ctx=" + JSON.stringify(payload) + ";"
      + "function run(){"
      + "if(window.teachRuntime&&typeof window.teachRuntime.bootstrapFromObject==='function'){"
      + "window.teachRuntime.bootstrapFromObject(ctx);"
      + "}"
      + "}"
      + "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',run,{once:true});}else{run();}"
      + "})();<\/script>";
    if (/<\/body>/i.test(html)) {
      return html.replace(/<\/body>/i, inject + "</body>");
    }
    return html + inject;
  }
  function openForIndex(idx) {
    var hook = hookAt(idx);
    if (!hook || !dialog || !frame) return;
    selectedIndex = Number(idx);
    var lesson = hook.lesson || "lesson";
    if (subtitle) {
      subtitle.textContent = (hook.note || "").slice(0, 180) || ("Lesson: " + lesson);
    }
    populateComplexityFor(hook);
    frame.setAttribute("srcdoc", srcDocForHook(hook));
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  }
  function closeTeachDialog() {
    if (!dialog || !dialog.open) return;
    dialog.close();
    if (frame) frame.removeAttribute("srcdoc");
    if (complexityPanel) {
      complexityPanel.hidden = true;
      complexityPanel.removeAttribute("data-kind");
    }
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
