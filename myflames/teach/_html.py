"""Shared HTML chrome for `myflames teach` lessons.

Emits a complete offline HTML5 document with:

* system font stack (matches output_diagram.py)
* controls section (sliders / number inputs / dropdowns)
* stage section (SVG + optional tuple overlay)
* cost-readout section (live numeric block)
* learn-more collapsible `<details>` block

No external dependencies. No ``<script src=``, no ``<link href=`` except
``data:`` URIs. Offline-first and accessible (ARIA labels, keyboard
navigation, ``prefers-reduced-motion`` fallback).
"""
from __future__ import annotations

import html
from typing import Iterable

from ._anim import ANIM_JS


# System font stack — same one used by myflames/output_diagram.py.
_FONT_STACK = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
    '"Helvetica Neue", Arial, sans-serif'
)

# Base CSS shared by every lesson.
_BASE_CSS = """
:root {
  --bg: #fafafa;
  --panel: #ffffff;
  --text: #1a1a1a;
  --muted: #6b7280;
  --border: #e5e7eb;
  --accent: #2563eb;
  --hot: #ff3d3d;
  --cool: #21918c;
  --warm: #fde725;
  --warn: #f59e0b;
  --ok: #10b981;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: __FONT_STACK__;
  -webkit-font-smoothing: antialiased;
  line-height: 1.5;
}
main { max-width: 1100px; margin: 0 auto; padding: 24px 20px 48px; }
header {
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  padding: 20px;
}
header .inner { max-width: 1100px; margin: 0 auto; }
header h1 {
  margin: 0 0 6px;
  font-size: 22px;
  letter-spacing: -0.3px;
  font-weight: 700;
}
header .subtitle {
  color: var(--muted);
  font-size: 13.5px;
  margin: 0;
}
header .version-chip {
  display: inline-block;
  margin-left: 8px;
  padding: 2px 10px;
  background: #eef2ff;
  color: #3730a3;
  border: 1px solid #c7d2fe;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  vertical-align: 2px;
}
.banner {
  margin: 0 0 20px;
  padding: 12px 16px;
  border-left: 4px solid var(--warn);
  background: #fffbeb;
  color: #78350f;
  border-radius: 4px;
  font-size: 13.5px;
}
.banner strong { color: #92400e; }
section.controls {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 18px 20px;
  margin-bottom: 20px;
}
section.controls h2 { margin: 0 0 12px; font-size: 15px; font-weight: 600; }
.control-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px 20px;
}
.control {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.control label {
  font-size: 12px;
  font-weight: 600;
  color: #374151;
}
.control .hint {
  font-size: 11px;
  color: var(--muted);
}
.control input[type="range"] { width: 100%; }
.control input[type="number"],
.control select {
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font: inherit;
  font-size: 13px;
  background: #fff;
}
.value-pill {
  display: inline-block;
  padding: 1px 8px;
  background: #f3f4f6;
  border-radius: 999px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  font-size: 12px;
  color: #111827;
}
section.stage {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 20px;
}
.query-card {
  margin-bottom: 14px;
  padding: 10px 14px;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 4px;
}
.query-card .query-label {
  margin: 0 0 6px;
  font-size: 10.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted);
}
.query-card .query-sql {
  margin: 0;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.5;
  color: #0f172a;
  white-space: pre-wrap;
}
.query-card .query-note {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--muted);
  font-style: italic;
}
.explainer-card {
  margin-bottom: 14px;
  padding: 12px 16px;
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  border-left: 3px solid #059669;
  border-radius: 4px;
}
.explainer-card .explainer-title {
  margin: 0 0 6px;
  font-size: 11.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: #065f46;
}
.explainer-card .explainer-list {
  margin: 0;
  padding-left: 20px;
  font-size: 12.5px;
  color: #064e3b;
  line-height: 1.55;
}
.explainer-card .explainer-list li {
  margin-bottom: 4px;
}
.explainer-card .explainer-list li:last-child {
  margin-bottom: 0;
}
section.stage .stage-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  font-size: 13px;
}
.stage-toolbar .speed-label {
  font-size: 12px;
  font-weight: 600;
  color: #374151;
  margin-left: 6px;
}
.stage-toolbar select {
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font: inherit;
  font-size: 12.5px;
  background: #fff;
  cursor: pointer;
}
.complexity-chart {
  margin-top: 14px;
  padding: 14px;
  background: #f9fafb;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.complexity-chart .chart-title {
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted);
}
.complexity-chart svg {
  width: 100%;
  max-width: 600px;
  height: auto;
}
button {
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: #fff;
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
}
button:hover { background: #f9fafb; }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
button.primary:hover { background: #1d4ed8; }
section.stage svg { width: 100%; height: auto; display: block; }
section.readout {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 18px 20px;
  margin-bottom: 20px;
}
section.readout h2 { margin: 0 0 12px; font-size: 15px; font-weight: 600; }
.readout-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px 18px;
}
.readout-grid .item {
  padding: 12px 14px;
  background: #f9fafb;
  border-radius: 6px;
  border: 1px solid var(--border);
}
.readout-grid .label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--muted);
  margin: 0 0 4px;
  font-weight: 600;
}
.readout-grid .value {
  font-size: 19px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: #111827;
}
.readout-grid .value.warn { color: var(--warn); }
.readout-grid .value.ok { color: var(--ok); }
.readout-grid .value.hot { color: var(--hot); }
details.learn-more {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px 20px;
}
details.learn-more summary {
  padding: 12px 0;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  color: #374151;
  outline: none;
}
details.learn-more[open] summary { color: #111827; }
details.learn-more .body {
  padding-bottom: 16px;
  font-size: 13.5px;
  color: #374151;
}
details.learn-more .body p { margin: 0 0 10px; }
details.learn-more .body code {
  background: #f3f4f6;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12.5px;
}
abbr.glossary-chip {
  text-decoration: underline dotted;
  text-decoration-color: #9ca3af;
  cursor: help;
}
.explanation {
  padding: 10px 14px;
  background: #f0f9ff;
  border-left: 3px solid #0284c7;
  border-radius: 4px;
  font-size: 13px;
  color: #0c4a6e;
  margin-top: 10px;
}
footer {
  max-width: 1100px;
  margin: 24px auto 0;
  padding: 16px 20px 32px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}
footer a { color: var(--muted); }
@media (max-width: 640px) {
  header h1 { font-size: 19px; }
  .readout-grid .value { font-size: 17px; }
}
@media (prefers-reduced-motion: reduce) {
  .anim-tween { transition: none !important; animation: none !important; }
}
""".replace("__FONT_STACK__", _FONT_STACK)


# Shared JS runtime used by every lesson. Provides:
#  - teachRuntime.readControls(): reads every input/select in the controls
#    section and updates the numeric pills.
#  - teachRuntime.wire(recompute): binds recompute to every input/select
#    and runs it once.
#  - teachRuntime.wireToolbar(playFn, resetFn): binds the shared play/pause
#    toggle, speed dropdown, and reset button.
#  - teachRuntime.formatInt/Bytes/Ms: number formatters.
_BASE_JS = """
var teachRuntime = (function() {
  function formatInt(n) {
    if (!isFinite(n)) return "—";
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n));
  }
  function formatBytes(b) {
    if (!isFinite(b) || b < 0) return "—";
    if (b >= 1024*1024*1024) return (b / (1024*1024*1024)).toFixed(2) + " GiB";
    if (b >= 1024*1024) return (b / (1024*1024)).toFixed(2) + " MiB";
    if (b >= 1024) return (b / 1024).toFixed(1) + " KiB";
    return b + " B";
  }
  function formatMs(ms) {
    if (!isFinite(ms)) return "—";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
    if (ms >= 1) return ms.toFixed(1) + " ms";
    return (ms * 1000).toFixed(1) + " µs";
  }
  function readControls(rootSel) {
    var root = document.querySelector(rootSel || "section.controls");
    var out = {};
    if (!root) return out;
    var els = root.querySelectorAll("input, select");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!el.name) continue;
      var v;
      if (el.type === "checkbox") v = el.checked;
      else if (el.type === "number" || el.type === "range") v = Number(el.value);
      else v = el.value;
      out[el.name] = v;
      var pill = document.querySelector('[data-pill-for="' + el.name + '"]');
      if (pill) pill.textContent = el.value;
    }
    return out;
  }
  function wire(recompute) {
    var els = document.querySelectorAll("section.controls input, section.controls select");
    for (var i = 0; i < els.length; i++) {
      els[i].addEventListener("input", recompute);
      els[i].addEventListener("change", recompute);
    }
    recompute();
  }

  // Wire the shared stage toolbar: play-pause toggle, speed dropdown, reset
  function wireToolbar(playFn, resetFn) {
    var btnPlay = document.getElementById("btn-play");
    var btnReset = document.getElementById("btn-reset");
    var selSpeed = document.getElementById("sel-speed");
    var state = { running: false, paused: false };

    function labelFor() {
      if (!state.running) return "▶ Play";
      if (state.paused) return "▶ Resume";
      return "⏸ Pause";
    }
    function updateLabel() { if (btnPlay) btnPlay.textContent = labelFor(); }

    // Lessons notify us when a timeline completes via teachRuntime.onAnimationDone
    window._teachOnDone = function() {
      state.running = false;
      state.paused = false;
      anim.setPaused(false);
      updateLabel();
    };

    if (btnPlay) btnPlay.addEventListener("click", function() {
      if (!state.running) {
        state.running = true;
        state.paused = false;
        anim.setPaused(false);
        updateLabel();
        if (playFn) playFn();
        return;
      }
      // Toggle pause
      state.paused = !state.paused;
      anim.setPaused(state.paused);
      updateLabel();
    });
    if (btnReset) btnReset.addEventListener("click", function() {
      state.running = false;
      state.paused = false;
      anim.setPaused(false);
      updateLabel();
      if (resetFn) resetFn();
    });
    if (selSpeed) selSpeed.addEventListener("change", function() {
      anim.setSpeed(Number(selSpeed.value));
    });
    // Set initial speed from the dropdown's default
    if (selSpeed) anim.setSpeed(Number(selSpeed.value));
  }

  // Helper that lesson code calls when its animation reaches the end.
  function animationDone() {
    if (typeof window._teachOnDone === "function") window._teachOnDone();
  }

  return {
    formatInt: formatInt,
    formatBytes: formatBytes,
    formatMs: formatMs,
    readControls: readControls,
    wire: wire,
    wireToolbar: wireToolbar,
    animationDone: animationDone
  };
})();
"""


def esc(s: str) -> str:
    """HTML-escape *s*."""
    return html.escape(s, quote=True)


def explainer(title: str, bullets: list) -> str:
    """Render a small 'what you'll see in the animation' card.

    Prepended above the stage to orient first-time viewers before they
    press Play. Every lesson should have one — the animation-expert
    skill flags 'silent phase transitions' as a top ugliness signal.
    """
    items = "".join(f"<li>{esc(b)}</li>" for b in bullets)
    return f"""
<div class="explainer-card">
  <p class="explainer-title">{esc(title)}</p>
  <ul class="explainer-list">{items}</ul>
</div>
"""


def query_card(sql: str, note: str = "") -> str:
    """Render a small SQL query card that sits above a lesson's stage.

    Uses real table names so the animation tracks something the user
    actually recognises from their own schemas. The SQL is the query
    that the lesson's algorithm would be asked to execute.
    """
    note_html = f'<p class="query-note">{esc(note)}</p>' if note else ""
    return f"""
<div class="query-card">
  <p class="query-label">Example query this animation executes</p>
  <pre class="query-sql">{esc(sql)}</pre>
  {note_html}
</div>
"""


def stage_toolbar(status_text: str = "Ready — press Play") -> str:
    """Render the shared stage toolbar: play/pause toggle, speed dropdown,
    reset button, and a status-label span. Every lesson uses this."""
    return f"""
<div class="stage-toolbar">
  <button id="btn-play" class="primary">▶ Play</button>
  <button id="btn-reset">Reset</button>
  <label for="sel-speed" class="speed-label">Speed:</label>
  <select id="sel-speed" aria-label="Animation speed">
    <option value="0.25">0.25×</option>
    <option value="0.5">0.5×</option>
    <option value="1" selected>1×</option>
    <option value="2">2×</option>
    <option value="4">4×</option>
  </select>
  <span style="margin-left:auto;font-size:12px;color:#6b7280" id="phase-label">{esc(status_text)}</span>
</div>
"""


def render_page(
    *,
    lesson_id: str,
    title: str,
    subtitle: str,
    version_chip: str = "MySQL 8.4 • MariaDB 11.4",
    banner_html: str = "",
    controls_html: str,
    stage_html: str,
    readout_html: str,
    learn_more_html: str,
    lesson_js: str,
    extra_css: str = "",
) -> str:
    """Assemble a complete self-contained HTML document for a lesson."""
    css = _BASE_CSS + "\n" + extra_css
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(subtitle)}">
  <meta name="generator" content="myflames teach">
  <style>{css}</style>
</head>
<body data-lesson="{esc(lesson_id)}">
  <header>
    <div class="inner">
      <h1>{esc(title)}<span class="version-chip">{esc(version_chip)}</span></h1>
      <p class="subtitle">{esc(subtitle)}</p>
    </div>
  </header>
  <main>
    {banner_html}
    {controls_html}
    {stage_html}
    {readout_html}
    {learn_more_html}
  </main>
  <footer>
    <p>Generated by <a href="https://github.com/vgrippa/myflames">myflames teach</a> —
    interactive database algorithm lessons. Offline-first, stdlib-only.</p>
  </footer>
  <script>{ANIM_JS}
{_BASE_JS}
{lesson_js}
  </script>
</body>
</html>
"""
