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
section.stage .stage-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  font-size: 13px;
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


# Minimal shared JS runtime used by every lesson. Each lesson's own
# `<script>` can call ``teachRuntime.wire(recompute)`` to bind every
# ``input``/``select`` in the controls section to a ``recompute`` callback.
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
  return {
    formatInt: formatInt,
    formatBytes: formatBytes,
    formatMs: formatMs,
    readControls: readControls,
    wire: wire
  };
})();
"""


def esc(s: str) -> str:
    """HTML-escape *s*."""
    return html.escape(s, quote=True)


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
  <script>{_BASE_JS}
{lesson_js}
  </script>
</body>
</html>
"""
