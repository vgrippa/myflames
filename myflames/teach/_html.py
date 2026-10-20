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


def _load_tier1_runtime() -> str:
    """Read the committed Tier-1 bundle produced by assets/build.mjs.

    The bundle wraps Motion One (FLIP/spring animations) and d3
    (squarified treemap, Catmull-Rom paths) into additive helpers on
    window.anim. Install order matters: ANIM_JS runs first to set up
    the hand-rolled primitives (tween, timeline, pulse, arrival,
    svgEl, complexityChart…); this bundle then merges its new keys
    (flip, spring, squarify, smoothPath) without overwriting the
    existing ones. If the bundle file is missing (not yet built on
    the contributor's machine) we return an empty string so lessons
    still work — they just lose the Tier-1 helpers.
    """
    import os as _os
    bundle = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "assets", "anim-runtime.js",
    )
    try:
        with open(bundle, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return "/* tier-1 anim-runtime.js not built — run 'cd assets && npm install && npm run build' */"


TIER1_RUNTIME_JS = _load_tier1_runtime()


# Workspace tokens are shared with the standalone catalog. SVG colors have
# separate roles because they encode algorithm state, not UI selection.
_LIGHT_TOKENS = """
  --ink-secondary: #3c3c46;
  --line-subtle: #efeff4;
  --control-line-hover: #a8a8b3;
  --shadow-popover: 0 8px 24px rgba(15, 15, 20, 0.12), 0 0 0 1px rgba(15, 15, 20, 0.08);
  --bg: #ffffff;
  --surface: #ffffff;
  --surface-raised: #ffffff;
  --surface-muted: #f4f5f6;
  --surface-hover: rgba(15, 17, 23, 0.045);
  --surface-subtle: #fafbfc;
  --ink: #202429;
  --ink-muted: #59616b;
  --ink-faint: #626a74;
  --line: #d1d5da;
  --line-strong: #a7afb9;
  --control-line: #8c939b;
  --accent-line: #8ebcab;
  --accent: #16654e;
  --accent-hover: #104d3b;
  --accent-soft: rgba(22, 101, 78, 0.10);
  --accent-ink: #ffffff;
  --focus: var(--accent);
  --ok: #157347;
  --ok-soft: rgba(21, 115, 71, 0.10);
  --warn: #945300;
  --warn-soft: rgba(148, 83, 0, 0.12);
  --warn-line: #efdfc1;
  --danger: #c42b1c;
  --danger-soft: rgba(196, 43, 28, 0.10);
  --danger-line: rgba(196, 43, 28, 0.25);
  --info: #005fb8;
  --info-soft: rgba(0, 95, 184, 0.10);
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto,
    sans-serif;
  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  --text-xs: 11px;
  --text-sm: 12px;
  --leading-sm: 18px;
  --text-md: 13px;
  --leading-md: 20px;
  --text-lg: 15px;
  --text-xl: 20px;
  --radius-sm: 5px;
  --radius-md: 7px;
  --radius-lg: 10px;
  --control-h: 28px;
  --svg-node-bg: #ffffff;
  --svg-node-muted: #f4f5f6;
  --svg-node-stroke: #b8bfc7;
  --svg-edge: #b8bfc7;
  --svg-line: #e4e7eb;
  --svg-ink: #17191c;
  --svg-ink-muted: #636b75;
  --svg-counter-bg: #17191c;
  --svg-counter-ink: #ffffff;
  --svg-on-solid: #ffffff;
  --svg-build-ink: #85400b;
  --svg-token-ink: #17598a;
  --svg-token: #17598a;
  --svg-token-soft: #eaf2f8;
  --svg-highlight: #fef08a;
  --svg-highlight-stroke: #d97706;
  --svg-hop: #b45309;
  --svg-hop-soft: #fdf3dc;
  --svg-build: #b45309;
  --svg-build-soft: #fdf3dc;
  --svg-probe: #0f766e;
  --svg-probe-soft: #e6f7f5;
  --svg-ok: #146c43;
  --svg-ok-soft: #e8f5ee;
  --svg-danger: #b42318;
  --svg-danger-soft: #fdeceb;
  --svg-group: #6d28d9;
  --svg-group-soft: #f3e8ff;
  --svg-group-alt: #be185d;
  --svg-group-alt-soft: #fce7f3;
  --panel: var(--surface);
  --panel-soft: var(--surface-muted);
  --panel-muted: var(--surface-muted);
  --text: var(--ink);
  --muted: var(--ink-muted);
  --border: var(--line);
  --border-strong: var(--line-strong);
  --code-bg: var(--surface-subtle);
  --accent-strong: var(--accent-hover);
  --hot: var(--danger);
  --cool: var(--svg-probe);
  --warm: var(--svg-highlight);
  --problem: var(--danger);
  --radius: var(--radius-md);
"""
_DARK_TOKENS = """
  --bg: #151719;
  --surface: #151719;
  --surface-raised: #25282c;
  --surface-muted: #1c1e21;
  --surface-hover: rgba(255, 255, 255, 0.06);
  --surface-subtle: #191b1e;
  --ink: #ededee;
  --ink-secondary: #d0d0d6;
  --ink-muted: #a5a9af;
  --ink-faint: #989ea7;
  --line: #34383d;
  --line-subtle: #292c30;
  --line-strong: #646c77;
  --control-line: #656e79;
  --control-line-hover: #6c6c74;
  --accent: #a9e7d3;
  --accent-hover: #c0eedf;
  --accent-soft: rgba(169, 231, 211, 0.12);
  --accent-line: #507b6e;
  --accent-ink: #143b31;
  --ok: #46c487;
  --ok-soft: rgba(70, 196, 135, 0.16);
  --warn: #e5a843;
  --warn-soft: rgba(229, 168, 67, 0.16);
  --warn-line: #5b4622;
  --danger: #ff6b5e;
  --danger-soft: rgba(255, 107, 94, 0.16);
  --danger-line: rgba(255, 107, 94, 0.4);
  --info: #63b3ff;
  --info-soft: rgba(99, 179, 255, 0.16);
  --shadow-popover: 0 8px 24px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.08);
  --svg-node-bg: #1c1e21;
  --svg-node-muted: #23272b;
  --svg-node-stroke: #454b52;
  --svg-edge: #454b52;
  --svg-line: #34383d;
  --svg-ink: #f0f2f4;
  --svg-ink-muted: #9ca3af;
  --svg-counter-bg: #25292e;
  --svg-counter-ink: #f0f2f4;
  --svg-on-solid: #151719;
  --svg-build-ink: #fcd38d;
  --svg-token-ink: #93c5fd;
  --svg-token: #60a5fa;
  --svg-token-soft: #1e3a5f;
  --svg-highlight: #713f12;
  --svg-highlight-stroke: #facc15;
  --svg-hop: #f59e0b;
  --svg-hop-soft: #3b2a14;
  --svg-build: #f59e0b;
  --svg-build-soft: #382814;
  --svg-probe: #2dd4bf;
  --svg-probe-soft: #133633;
  --svg-ok: #4ade80;
  --svg-ok-soft: #163324;
  --svg-danger: #f87171;
  --svg-danger-soft: #3b1d1d;
  --svg-group: #c4b5fd;
  --svg-group-soft: #302044;
  --svg-group-alt: #f9a8d4;
  --svg-group-alt-soft: #431e34;
"""
_THEME_CSS = (
    ":root { color-scheme: light;\n" + _LIGHT_TOKENS + "}\n"
    "@media (prefers-color-scheme: dark) {\n"
    ":root:not([data-theme=\"light\"]) { color-scheme: dark;\n"
    + _DARK_TOKENS + "}\n}\n"
    ':root[data-theme="dark"] { color-scheme: dark;\n' + _DARK_TOKENS + "}\n"
    ':root[data-theme="light"] { color-scheme: light; }\n'
)

# Base CSS shared by every lesson; names are also used by browser tests.
_BASE_CSS = _THEME_CSS + """
* {

  box-sizing: border-box;

}
html, body {
  margin: 0; padding: 0;
  background: var(--bg); color: var(--ink);
  font-family: var(--font-sans); font-size: var(--text-md);
  -webkit-font-smoothing: antialiased; line-height: 1.5;
}
main {
  max-width: 1120px;
  margin: 0 auto;
  padding: 16px 20px 32px;
}
header {
  padding: 12px 20px;
  border-bottom: 1px solid var(--line);
}
header .inner {
  max-width: 1080px;
  margin: 0 auto;
}
header h1 {
  margin: 0 0 4px;
  font-size: var(--text-lg);
  font-weight: 600;
  letter-spacing: -0.01em;
}
header .subtitle {
  margin: 0;
  color: var(--ink-secondary);
}
header .lesson-meta {
  margin-top: 8px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
header .meta-label {
  font-size: var(--text-xs);
  color: var(--ink-muted);
}
header .family-chip, header .version-chip {
  display: inline-flex; align-items: center; gap: 6px;
  min-height: 19px; padding: 1px 6px;
  border: 1px solid var(--line-subtle); border-radius: var(--radius-sm);
  background: var(--surface-muted); color: var(--ink-secondary);
  font-size: var(--text-xs); font-weight: 500; letter-spacing: 0;
}
header .version-chip {
  margin-left: 8px;
  vertical-align: 1px;
}
header .chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
html[data-embedded="true"] body > header,
html[data-embedded="true"] body > footer {
  display: none;
}
html[data-embedded="true"] main {
  max-width: none;
  padding: 12px 16px 20px;
}
.banner {
  margin: 0 0 10px; padding: 8px 12px;
  border: 1px solid var(--warn-line); border-radius: var(--radius-md);
  background: var(--warn-soft); color: var(--ink); font-size: var(--text-sm);
}
.banner strong {
  color: var(--warn);
}
section.controls {
  padding: 10px 12px; margin-bottom: 10px;
  border: 1px solid var(--line); border-radius: var(--radius-md);
  background: var(--surface-muted);
}
section.controls h2, section.readout h2 {
  margin: 0 0 8px;
  font-size: var(--text-lg);
  font-weight: 600;
}
.control-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px 14px;
}
.control {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.control label {
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--ink-secondary);
}
.control .hint {
  font-size: var(--text-sm);
  color: var(--ink-muted);
}
.control input[type="number"], select {
  min-width: 0; max-width: 100%; min-height: var(--control-h);
  padding: 3px 8px; border: 1px solid var(--control-line); border-radius: var(--radius-sm);
  font: inherit; font-size: var(--text-sm); font-weight: 500;
  background-color: var(--surface); color: var(--ink);
}
select {
  appearance: none; padding-right: 24px;
  background-image: linear-gradient(45deg, transparent 50%, var(--ink-muted) 50%),
    linear-gradient(135deg, var(--ink-muted) 50%, transparent 50%);
  background-position: calc(100% - 12px) 50%, calc(100% - 8px) 50%;
  background-size: 4px 4px; background-repeat: no-repeat;
}
:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.value-pill {
  display: inline-block; padding: 1px 6px; border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm); background: var(--surface); color: var(--ink);
  font: 600 var(--text-sm) var(--font-mono); font-variant-numeric: tabular-nums;
}
input[type="range"] {
  appearance: none;
  height: 4px;
  border-radius: 999px;
  background: var(--line);
  accent-color: var(--accent);
  cursor: pointer;
}
.control input[type="range"] {
  width: 100%;
  margin: 10px 0;
}
input[type="range"]::-webkit-slider-runnable-track {
  height: 4px;
  border-radius: 999px;
  background: var(--line);
}
input[type="range"]::-moz-range-track {
  height: 4px;
  border-radius: 999px;
  background: var(--line);
}
input[type="range"]::-webkit-slider-thumb {
  appearance: none; margin-top: -5px; width: 14px; height: 14px;
  border-radius: 50%; border: 2px solid var(--surface); background: var(--accent); box-shadow: none;
}
input[type="range"]::-moz-range-thumb {
  width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--surface);
  background: var(--accent); box-shadow: none;
}
section.stage {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  padding: 12px;
  margin-bottom: 10px;
}
.query-card, .explainer-card, .explanation {
  margin: 0 0 10px; padding: 8px 12px; background: var(--surface-subtle);
  border: 1px solid var(--line); border-radius: var(--radius-md);
  color: var(--ink-secondary); font-size: var(--text-sm);
}
.query-card .query-label, .explainer-card .explainer-title, .complexity-chart .chart-title {
  margin: 0 0 3px; font-size: var(--text-xs); font-weight: 600; color: var(--ink-muted);
}
.query-card .query-sql {
  margin: 0;
  font: var(--text-sm)/1.45 var(--font-mono);
  color: var(--ink);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.query-card .query-note {
  margin: 4px 0 0;
  color: var(--ink-muted);
}
.explainer-card .explainer-title {
  color: var(--ink);
}
.explainer-card .explainer-title::before {
  content: "";
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  margin-right: 6px;
  vertical-align: 1px;
}
.explainer-card .explainer-list {
  margin: 0;
  padding-left: 18px;
}
.explainer-card li {
  margin-bottom: 4px;
}
.explainer-card li:last-child {
  margin-bottom: 0;
}
.stage-toolbar {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  min-height: 38px; padding: 5px 10px; background: var(--surface-muted);
  border: 1px solid var(--line); border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  font-size: var(--text-sm);
}
.stage-toolbar .speed-label {
  color: var(--ink-secondary);
  font-size: var(--text-sm);
}
.phase-label {
  font: var(--text-sm)/1.5 var(--font-mono); color: var(--ink-secondary);
  background: var(--surface-subtle); border: 1px solid var(--line-subtle);
  padding: 3px 8px; border-radius: var(--radius-sm); overflow-wrap: anywhere;
}
.stage-toolbar .phase-label {
  margin-left: auto;
}
.stage-scrubber {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 5px 10px;
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-top: 0;
}
.stage-scrubber input[type="range"] {
  flex: 1;
  min-width: 0;
}
.stage-scrubber .scrubber-time {
  font: var(--text-xs) var(--font-mono);
  color: var(--ink-muted);
  min-width: 42px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
button {
  display: inline-flex; align-items: center; gap: 6px; min-height: var(--control-h);
  padding: 4px 10px; border: 1px solid var(--control-line); border-radius: var(--radius-sm);
  background: var(--surface); color: var(--ink); font: inherit; font-size: var(--text-sm); font-weight: 500; cursor: pointer;
}
button:hover:not(:disabled) {
  background: var(--surface-subtle);
  border-color: var(--ink-muted);
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
button.primary {
  background: var(--accent);
  color: var(--accent-ink);
  border-color: var(--accent);
  font-weight: 600;
}
button.primary:hover:not(:disabled) {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
button[aria-pressed="true"] {
  background: var(--accent-soft);
  color: var(--accent);
  border-color: var(--accent-line);
  font-weight: 600;
}
button .btn-icon, button .btn-icon svg {
  width: 16px;
  height: 16px;
  display: inline-flex;
  fill: currentColor;
}
button .btn-label {
  line-height: 1;
  white-space: nowrap;
}
section.stage svg {
  width: 100%;
  height: auto;
  display: block;
}
section.stage button svg {
  width: 16px;
  height: 16px;
}
.stage-with-phases {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  overflow-x: auto;
}
.stage-with-phases > svg {
  flex: 1;
  min-width: 0;
}
.phase-nav {
  width: 180px;
  flex-shrink: 0;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  padding: 8px 0;
  font-size: var(--text-sm);
}
.phase-nav-title {
  padding: 0 10px 6px;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--ink-muted);
  border-bottom: 1px solid var(--line);
  margin-bottom: 4px;
}
.phase-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  cursor: pointer;
  color: var(--ink-secondary);
  border-left: 3px solid transparent;
}
.phase-item:hover {
  background: var(--surface-hover);
}
.phase-item.active {
  background: var(--accent-soft);
  color: var(--accent);
  border-left-color: var(--accent);
  font-weight: 600;
}
.phase-item.done {
  color: var(--ok);
}
.phase-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--line-strong);
  flex-shrink: 0;
}
.phase-item.active .phase-dot {
  background: var(--accent);
}
.phase-item.done .phase-dot {
  background: var(--ok);
}
section.readout {
  margin-bottom: 10px;
}
.readout-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}
.readout-grid .item {
  background: var(--surface);
  padding: 9px 12px;
  min-width: 0;
}
.readout-grid .label {
  margin: 0 0 2px;
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--ink-muted);
}
.readout-grid .value {
  margin: 0;
  font: 600 16px/1.5 var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--ink);
  overflow-wrap: anywhere;
}
.readout-grid .value.warn {
  color: var(--warn);
}
.readout-grid .value.ok {
  color: var(--ok);
}
.readout-grid .value.hot {
  color: var(--danger);
}
.help-tip {
  display: inline-block;
  position: relative;
  cursor: help;
  color: var(--ink-muted);
  border: 1px solid var(--control-line);
  border-radius: 50%;
  width: 15px;
  height: 15px;
  font-size: var(--text-xs);
  line-height: 14px;
  text-align: center;
  margin-left: 4px;
}
.help-tip .tip-text {
  display: none;
  position: absolute;
  bottom: 22px;
  left: -100px;
  width: 220px;
  padding: 8px 10px;
  background: var(--surface-raised);
  border: 1px solid var(--line);
  color: var(--ink);
  box-shadow: var(--shadow-popover);
  font-size: var(--text-sm);
  line-height: 1.45;
  border-radius: var(--radius-md);
  text-align: left;
  z-index: 10;
  pointer-events: none;
}
.help-tip:hover .tip-text, .help-tip:focus .tip-text {
  display: block;
}
.explanation {
  margin-top: 10px;
}
.complexity-chart {
  margin-top: 10px;
  padding: 10px 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  overflow-x: auto;
}
.complexity-chart svg {
  width: 100%;
  max-width: 600px;
  height: auto;
}
details.learn-more {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  overflow: hidden;
}
details.learn-more summary {
  min-height: 34px;
  padding: 6px 12px;
  cursor: pointer;
  background: var(--surface-muted);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--ink);
}
details.learn-more .body {
  padding: 12px;
  font-size: var(--text-md);
  color: var(--ink-secondary);
  overflow-wrap: anywhere;
}
details.learn-more .body p {
  margin: 0 0 10px;
}
code, pre {
  font-family: var(--font-mono);
}
details.learn-more code {
  background: var(--surface-muted);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
}
abbr.glossary-chip {
  text-decoration: underline dotted;
  text-decoration-color: var(--ink-muted);
  cursor: help;
}
footer {
  max-width: 1120px;
  margin: 16px auto 0;
  padding: 12px 20px 24px;
  font-size: var(--text-sm);
  color: var(--ink-muted);
}
a {
  color: var(--accent);
}
@media (max-width: 700px) {
  .stage-with-phases {
    display: block;
  }
  /* Scroll drawings at a readable scale; keep the surrounding page narrow. */
  .stage-with-phases > svg {
    min-width: 760px;
  }
  .stage-with-phases > div:not(.phase-nav) {
    min-width: 800px;
  }
  .phase-nav {
    width: 100%;
    margin-top: 8px;
  }
  .complexity-chart svg {
    min-width: 560px;
  }
  .stage-toolbar .phase-label {
    width: 100%;
    margin-left: 0;
  }
}
@media (max-width: 650px) {
  :root {
    --control-h: 32px;
  }
  main, html[data-embedded="true"] main {
    padding: 12px;
  }
  header {
    padding: 12px;
  }
  header .version-chip {
    margin: 4px 0 0;
  }
  .control-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (prefers-reduced-motion: reduce) {
  .anim-tween {
    transition: none !important;
    animation: none !important;
  }
}
"""


# Shared JS runtime used by every lesson. Provides:
#  - teachRuntime.readControls(): reads every input/select in the controls
#    section and updates the numeric pills.
#  - teachRuntime.wire(recompute): binds recompute to every input/select
#    and runs it once.
#  - teachRuntime.wireToolbar(playFn, resetFn): binds the shared play/pause
#    toggle, speed dropdown, and reset button.
#  - teachRuntime.formatInt/Bytes/Ms: number formatters.
_THEME_JS = """
(function() {
  var params = new URLSearchParams(window.location.search);
  var theme = params.get("theme");
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  }
  if (params.get("embedded") === "1") {
    document.documentElement.setAttribute("data-embedded", "true");
  }
})();
"""
_BASE_JS = _THEME_JS + """
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
    window.__teachRecompute = recompute;
    recompute();
  }
  function bootstrapFromObject(ctx) {
    ctx = ctx || {};
    var controls = ctx.controls || {};
    var root = document.querySelector("section.controls");
    if (root) {
      var els = root.querySelectorAll("input, select");
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (!el.name || !Object.prototype.hasOwnProperty.call(controls, el.name)) continue;
        var v = controls[el.name];
        if (el.type === "checkbox") {
          el.checked = !!v;
        } else {
          el.value = String(v);
        }
        var pill = document.querySelector('[data-pill-for="' + el.name + '"]');
        if (pill) pill.textContent = el.value;
      }
    }
    // Keep the lesson's own example query — it is a pedagogical classic
    // that matches the animated sample data (Alice, Bob, Carol, …).
    // Only inject the operator note as context for which operator the
    // user clicked on.
    if (ctx.note) {
      var note = document.querySelector(".query-card .query-note");
      if (note) note.textContent = String(ctx.note);
    }
    if (typeof window.__teachRecompute === "function") {
      window.__teachRecompute();
    } else {
      // Legacy fallback: try to trigger any control-driven recompute path.
      var evt;
      if (typeof Event === "function") evt = new Event("input", { bubbles: true });
      var root2 = document.querySelector("section.controls");
      if (evt && root2) {
        var first = root2.querySelector("input, select");
        if (first) first.dispatchEvent(evt);
      }
    }
  }

  // =====================================================================
  // Playback controller — unified toolbar, scrubber, and phase navigation.
  //
  // One state machine, one RAF loop, one seek() primitive that every entry
  // point (Play, Pause, Reset, scrubber drag, phase click) routes through.
  //
  // State diagram:
  //   IDLE  → Play    → PLAYING
  //   PLAYING → Pause  → PAUSED
  //   PAUSED  → Resume → PLAYING  (continues from currentMs)
  //   PAUSED  → Play   → PLAYING  (restart from 0)
  //   *       → seek() → SEEKED   (paused at targetMs, Resume → PLAYING)
  //   *       → Reset  → IDLE
  // =====================================================================
  var _ctrl = {
    build: null,         // fn → returns fresh anim.timeline()
    reset: null,         // fn → clears stage to ready state
    legacyPlay: null,    // fn → old-style play callback
    tl: null,            // current anim.timeline()
    phase: "idle",       // "idle" | "playing" | "paused" | "seeked"
    seekMs: 0,           // ms position we seeked to (valid when phase=seeked)
    loopEnabled: false,
    rafId: 0,            // current RAF id (0 = none)
    dragging: false,     // true while scrubber thumb is held
    // Phase nav
    phaseContainer: null,
    marks: []
  };
  // User-calibrated baseline: the dropdown's "1×" maps to this internal
  // speed. Started at 0.52 (original); user asked for 40% slower →
  // 0.312 felt too slow for state-swap lessons (icp etc.) where flat
  // tl.delay times then stretched to 3× wallclock and looked frozen.
  // 0.42 is a compromise: ~20% slower than the original 0.52 baseline
  // — tweened animations still breathe, stepped ones stay responsive.
  var BASELINE_SPEED_SCALE = 0.42;

  // --- DOM refs (resolved once in wireToolbar) ---
  var _dom = {};

  // --- Helpers ---
  function _formatSec(ms) { return (Math.max(0, ms) / 1000).toFixed(1) + "s"; }

  function _setBtnLabel(btn, txt) {
    if (!btn) return;
    var lbl = btn.querySelector(".btn-label");
    if (lbl) lbl.textContent = txt; else btn.textContent = txt;
  }

  function _syncUI() {
    // Button label
    var label = _ctrl.phase === "idle" ? "Play"
              : _ctrl.phase === "playing" ? "Pause"
              : "Resume";
    _setBtnLabel(_dom.btnPlay, label);
    // Scrubber + time labels
    if (_ctrl.tl && _dom.scrubber) {
      var total = _ctrl.tl.getTotalDuration();
      if (_dom.timeTot) _dom.timeTot.textContent = _formatSec(total);
      if (total > 0) {
        var cur = _ctrl.tl.getCurrentTime();
        _dom.scrubber.value = String(Math.round(Math.min(1, cur / total) * 1000));
        if (_dom.timeCur) _dom.timeCur.textContent = _formatSec(cur);
      }
    } else {
      if (_dom.scrubber) _dom.scrubber.value = "0";
      if (_dom.timeCur) _dom.timeCur.textContent = "0.0s";
    }
    // Loop button
    if (_dom.btnLoop) {
      _setBtnLabel(_dom.btnLoop, _ctrl.loopEnabled ? "Loop: On" : "Loop: Off");
      _dom.btnLoop.setAttribute("aria-pressed", _ctrl.loopEnabled ? "true" : "false");
    }
  }

  // --- Single RAF loop: runs only while phase = "playing" ---
  // Uses a generation counter so stale loops from previous playbacks die.
  var _tickGen = 0;
  function _tick(gen) {
    if (gen !== _tickGen) return;                // stale loop — die
    if (_ctrl.phase !== "playing") { _ctrl.rafId = 0; return; }
    if (_ctrl.tl && _dom.scrubber && !_ctrl.dragging) {
      var total = _ctrl.tl.getTotalDuration();
      if (total > 0) {
        var cur = _ctrl.tl.getCurrentTime();
        var frac = Math.min(1, cur / total);
        _dom.scrubber.value = String(Math.round(frac * 1000));
        if (_dom.timeCur) _dom.timeCur.textContent = _formatSec(cur);
        _updatePhaseHighlight(cur);
      }
    }
    _ctrl.rafId = requestAnimationFrame(function() { _tick(gen); });
  }
  function _startTick() {
    _tickGen++;
    var gen = _tickGen;
    _ctrl.rafId = requestAnimationFrame(function() { _tick(gen); });
  }

  // --- Core operations ---

  // Fully stop everything and return to idle.  Does NOT call reset().
  function _stopAll() {
    if (_ctrl.tl) _ctrl.tl.stop();
    _ctrl.tl = null;
    _ctrl.phase = "idle";
    _ctrl.seekMs = 0;
    anim.setPaused(false);
    _tickGen++;                                    // kill any stale _tick loop
    if (_ctrl.rafId) { cancelAnimationFrame(_ctrl.rafId); _ctrl.rafId = 0; }
  }

  // Build a fresh timeline, apply all state up to `ms`, leave paused there.
  // This is the single seek primitive — scrubber, phase-nav, and toolbar
  // all route through here.
  function _seek(ms) {
    _stopAll();
    if (_ctrl.reset) _ctrl.reset();
    if (!_ctrl.build) return;
    _ctrl.tl = _ctrl.build();
    var total = _ctrl.tl.getTotalDuration();
    if (ms > total) ms = total;
    if (ms < 0) ms = 0;
    _ctrl.tl.fastForwardTo(ms);
    _ctrl.phase = "seeked";
    _ctrl.seekMs = ms;
    anim.setPaused(true);
    _renderPhaseNav();
    _syncUI();
    // Override scrubber to exact seek position
    if (_dom.scrubber && total > 0) {
      _dom.scrubber.value = String(Math.round((ms / total) * 1000));
    }
    if (_dom.timeCur) _dom.timeCur.textContent = _formatSec(ms);
  }

  // Start playing from `ms` (0 for fresh start).
  function _playFrom(ms) {
    _stopAll();
    if (_ctrl.reset) _ctrl.reset();
    if (!_ctrl.build) return;
    _ctrl.tl = _ctrl.build();
    _ctrl.phase = "playing";
    _ctrl.seekMs = 0;
    anim.setPaused(false);
    _renderPhaseNav();
    _syncUI();
    if (ms > 0) {
      _ctrl.tl.playFrom(ms, _onDone);
    } else {
      _ctrl.tl.play(_onDone);
    }
    _startTick();
  }

  function _onDone() {
    if (_ctrl.loopEnabled) { _playFrom(0); return; }
    _ctrl.phase = "idle";
    if (_ctrl.rafId) { cancelAnimationFrame(_ctrl.rafId); _ctrl.rafId = 0; }
    anim.setPaused(false);
    _syncUI();
    // Pin scrubber to end
    if (_ctrl.tl && _dom.scrubber) _dom.scrubber.value = "1000";
    if (_ctrl.tl && _dom.timeCur) _dom.timeCur.textContent = _formatSec(_ctrl.tl.getTotalDuration());
  }

  // --- Phase nav rendering & highlighting ---
  function _renderPhaseNav() {
    var c = _ctrl.phaseContainer;
    if (!c || !_ctrl.tl || !_ctrl.tl.getMarks) return;
    _ctrl.marks = _ctrl.tl.getMarks();
    c.innerHTML = '<div class="phase-nav-title">Phases</div>';
    for (var i = 0; i < _ctrl.marks.length; i++) {
      (function(idx) {
        var item = document.createElement("div");
        item.className = "phase-item";
        item.setAttribute("data-phase-idx", idx);
        item.innerHTML = '<span class="phase-dot"></span>' + _ctrl.marks[idx].name;
        item.addEventListener("click", function() {
          _seek(_ctrl.marks[idx].ms);
          _highlightPhaseIdx(idx);
        });
        c.appendChild(item);
      })(i);
    }
    _highlightPhaseIdx(0);
  }

  function _highlightPhaseIdx(activeIdx) {
    var c = _ctrl.phaseContainer;
    if (!c) return;
    var items = c.querySelectorAll(".phase-item");
    for (var i = 0; i < items.length; i++) {
      var cls = "phase-item";
      if (i === activeIdx) cls += " active";
      else if (i < activeIdx) cls += " done";
      items[i].className = cls;
    }
  }

  function _updatePhaseHighlight(currentMs) {
    if (!_ctrl.marks.length) return;
    var active = 0;
    for (var i = _ctrl.marks.length - 1; i >= 0; i--) {
      if (currentMs >= _ctrl.marks[i].ms) { active = i; break; }
    }
    _highlightPhaseIdx(active);
  }

  // --- Public API: wireToolbar ---
  function wireToolbar(optsOrPlay, maybeReset) {
    _dom.btnPlay  = document.getElementById("btn-play");
    _dom.btnReset = document.getElementById("btn-reset");
    _dom.btnLoop  = document.getElementById("btn-loop");
    _dom.selSpeed = document.getElementById("sel-speed");
    _dom.scrubber = document.getElementById("scrubber");
    _dom.timeCur  = document.getElementById("scrubber-time-current");
    _dom.timeTot  = document.getElementById("scrubber-time-total");

    if (typeof optsOrPlay === "function") {
      _ctrl.legacyPlay = optsOrPlay;
      _ctrl.reset = maybeReset;
    } else if (optsOrPlay && typeof optsOrPlay === "object") {
      _ctrl.build = optsOrPlay.build;
      _ctrl.reset = optsOrPlay.reset;
    }

    // --- Play / Pause / Resume button ---
    if (_dom.btnPlay) _dom.btnPlay.addEventListener("click", function() {
      switch (_ctrl.phase) {
        case "idle":
          if (_ctrl.legacyPlay) { _ctrl.legacyPlay(); return; }
          _playFrom(0);
          break;
        case "playing":
          // Pause
          _ctrl.phase = "paused";
          anim.setPaused(true);
          _syncUI();
          break;
        case "paused":
          // Resume from current position
          _ctrl.phase = "playing";
          anim.setPaused(false);
          _syncUI();
          _startTick();
          break;
        case "seeked":
          // Resume from seeked position
          _playFrom(_ctrl.seekMs);
          break;
      }
    });

    // --- Reset button ---
    if (_dom.btnReset) _dom.btnReset.addEventListener("click", function() {
      _stopAll();
      if (_ctrl.reset) _ctrl.reset();
      _syncUI();
    });

    // --- Loop button ---
    if (_dom.btnLoop) _dom.btnLoop.addEventListener("click", function() {
      _ctrl.loopEnabled = !_ctrl.loopEnabled;
      _syncUI();
    });

    // --- Speed dropdown ---
    function applyUiSpeed() {
      if (!_dom.selSpeed) return;
      anim.setSpeed(Number(_dom.selSpeed.value) * BASELINE_SPEED_SCALE);
    }
    if (_dom.selSpeed) _dom.selSpeed.addEventListener("change", applyUiSpeed);
    if (_dom.selSpeed) applyUiSpeed();

    // --- Scrubber (range input) ---
    if (_dom.scrubber && _ctrl.build) {
      _dom.scrubber.addEventListener("mousedown",  function() { _ctrl.dragging = true; });
      _dom.scrubber.addEventListener("touchstart", function() { _ctrl.dragging = true; });
      _dom.scrubber.addEventListener("input", function() {
        var frac = Number(_dom.scrubber.value) / 1000;
        if (!_ctrl.build) return;
        // _seek rebuilds the timeline internally; we need total from a
        // fresh build to convert frac → ms. Use _seek(0) first to get a
        // timeline, read its total, then re-seek to the real target.
        // Optimisation: if we already have a tl, reuse its totalDuration.
        var total;
        if (_ctrl.tl) {
          total = _ctrl.tl.getTotalDuration();
        } else {
          // No timeline yet — do a throwaway seek to 0 to get one
          _seek(0);
          if (!_ctrl.tl) return;
          total = _ctrl.tl.getTotalDuration();
        }
        _seek(frac * total);
      });
      function endDrag() { _ctrl.dragging = false; }
      _dom.scrubber.addEventListener("change",  endDrag);
      _dom.scrubber.addEventListener("mouseup",  endDrag);
      _dom.scrubber.addEventListener("touchend", endDrag);
    }

    // Static var() attributes follow CSS automatically. Replaying to the same
    // playhead also refreshes colors materialized by an interpolation.
    function redrawAppearance() {
      var phase = _ctrl.phase;
      var position = _ctrl.tl ? _ctrl.tl.getCurrentTime() : 0;
      if (phase === "seeked") position = _ctrl.seekMs;
      var completed = phase === "idle" && _ctrl.tl;
      if (completed) position = _ctrl.tl.getTotalDuration();
      if (phase === "playing") {
        _playFrom(position);
      } else if (phase === "paused" || phase === "seeked" || completed) {
        _seek(position);
        if (completed) {
          _ctrl.phase = "idle";
          anim.setPaused(false);
          _syncUI();
        }
      } else {
        _stopAll();
        if (_ctrl.reset) _ctrl.reset();
        _syncUI();
      }
    }
    var appearance = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    if (appearance) {
      if (appearance.addEventListener) appearance.addEventListener("change", redrawAppearance);
      else if (appearance.addListener) appearance.addListener(redrawAppearance);
    }
    if (typeof MutationObserver === "function") {
      new MutationObserver(redrawAppearance).observe(document.documentElement, {
        attributes: true, attributeFilter: ["data-theme"]
      });
    }

    // Legacy done callback
    window._teachOnDone = function() { _onDone(); };

    _syncUI();
  }

  // --- Public API: wirePhaseNav ---
  function wirePhaseNav(containerId, opts) {
    var container = document.getElementById(containerId);
    if (!container) return;
    _ctrl.phaseContainer = container;
    // build/reset already set by wireToolbar; opts here are redundant but
    // kept for backward compat.
    if (opts && opts.build && !_ctrl.build) _ctrl.build = opts.build;
    if (opts && opts.reset && !_ctrl.reset) _ctrl.reset = opts.reset;
    container.innerHTML = '<div class="phase-nav-title">Phases</div>';
  }

  // Backward-compat aliases used by older code paths
  function _updatePhaseNav(tl) { _renderPhaseNav(); }
  function _tickPhaseNav(currentMs) { _updatePhaseHighlight(currentMs); }

  function animationDone() {
    if (typeof window._teachOnDone === "function") window._teachOnDone();
  }

  return {
    formatInt: formatInt,
    formatBytes: formatBytes,
    formatMs: formatMs,
    readControls: readControls,
    wire: wire,
    bootstrapFromObject: bootstrapFromObject,
    wireToolbar: wireToolbar,
    wirePhaseNav: wirePhaseNav,
    _updatePhaseNav: _updatePhaseNav,
    _tickPhaseNav: _tickPhaseNav,
    animationDone: animationDone
  };
})();
"""


def load_lesson_js(module_file: str, js_name: str = None) -> str:
    """Load a lesson's JavaScript from a sibling ``.js`` file on disk.

    Tier-0 authoring ergonomics: each lesson's JS lives in a real
    ``.js`` file next to its ``.py``, not inside a Python raw-string
    literal. Editors highlight the syntax, eslint/prettier can touch
    it, and the whole class of ``%% vs %`` escape bugs goes away.

    Usage from a lesson module::

        from .. import _html
        _LESSON_JS = _html.load_lesson_js(__file__)

    The helper looks up ``<same-dir>/<same-basename>.js`` by default.
    Pass ``js_name`` to override (e.g. when a lesson bundles multiple
    JS files).
    """
    import os as _os
    if js_name is None:
        base = _os.path.splitext(_os.path.basename(module_file))[0]
        js_name = base + ".js"
    js_path = _os.path.join(_os.path.dirname(_os.path.abspath(module_file)),
                             js_name)
    with open(js_path, "r", encoding="utf-8") as f:
        return f.read()


def esc(s: str) -> str:
    """HTML-escape *s*."""
    return html.escape(s, quote=True)


def help_tip(tip: str) -> str:
    """Render a small (?) help icon with a hover tooltip.

    Used inside readout labels so every metric box explains itself to
    first-time viewers. Example:

        <p class="label">Fan-out {help_tip("How many child...")}</p>
    """
    return (
        '<span class="help-tip">?'
        f'<span class="tip-text">{esc(tip)}</span>'
        "</span>"
    )


def explainer(title: str, bullets: list) -> str:
    """Render a small 'what you'll see in the animation' card.

    Prepended above the stage to orient first-time viewers before they
    press Play. Every lesson should have one — the animation-expert
    skill flags 'silent phase transitions' as a top ugliness signal.
    """
    # Lesson authors use code spans for SQL terms. Escape everything first,
    # then allow only these two exact tags; other markup stays inert text.
    items = "".join(
        "<li>" + esc(b).replace("&lt;code&gt;", "<code>")
        .replace("&lt;/code&gt;", "</code>") + "</li>"
        for b in bullets
    )
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


def phase_nav(nav_id: str = "phase-nav") -> str:
    """Render the phase navigation sidebar container.

    The actual phase items are populated dynamically by JS when the
    timeline is built (via ``tl.mark("Phase name")`` calls). Clicking
    a phase seeks the animation to that point.
    """
    return f'<div id="{esc(nav_id)}" class="phase-nav"></div>'


def stage_toolbar(status_text: str = "Ready — press Play") -> str:
    """Render the shared stage toolbar: play/pause toggle, speed dropdown,
    reset button, a scrubber (YouTube-style timeline seek), and a
    status-label span. Every lesson uses this."""
    return f"""
<div class="stage-toolbar">
  <button id="btn-play" class="primary">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
    </span>
    <span class="btn-label">Play</span>
  </button>
  <button id="btn-reset">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M12 6V3L8 7l4 4V8c2.8 0 5 2.2 5 5a5 5 0 1 1-9.9-1H5.1A7 7 0 1 0 12 6z"/></svg>
    </span>
    <span class="btn-label">Reset</span>
  </button>
  <button id="btn-loop" aria-pressed="false">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M7 7h9v3l4-4-4-4v3H6a4 4 0 0 0-4 4v3h2V9a2 2 0 0 1 2-2zm10 10H8v-3l-4 4 4 4v-3h10a4 4 0 0 0 4-4v-3h-2v2a2 2 0 0 1-2 2z"/></svg>
    </span>
    <span class="btn-label">Loop: Off</span>
  </button>
  <label for="sel-speed" class="speed-label">Speed:</label>
  <select id="sel-speed" aria-label="Animation speed">
    <option value="0.25">0.25×</option>
    <option value="0.5">0.5×</option>
    <option value="1" selected>1×</option>
    <option value="2">2×</option>
    <option value="4">4×</option>
  </select>
  <span class="phase-label" id="phase-label">{esc(status_text)}</span>
</div>
<div class="stage-scrubber">
  <span class="scrubber-time" id="scrubber-time-current">0.0s</span>
  <input type="range" id="scrubber" min="0" max="1000" value="0" step="1"
    aria-label="Animation timeline scrubber" />
  <span class="scrubber-time" id="scrubber-time-total">0.0s</span>
</div>
"""


_LESSON_FAMILY_LABELS = {
    "bnl": "Join operator",
    "hash": "Join operator",
    "join": "Join operator",
    "nested_loop": "Join operator",
    "full_scan": "Scan operator",
    "filter": "Filter operator",
    "filesort": "Sort operator",
    "tmp": "Temp operator",
    "btree": "Index access",
    "non_unique_lookup": "Index access",
    "unique_lookup": "Index access",
    "icp": "Index access",
    "index_merge": "Index access",
    "lru": "Buffer cache",
    "derived_table": "Temp operator",
}


def _family_chip(lesson_id: str) -> str:
    label = _LESSON_FAMILY_LABELS.get(lesson_id)
    if not label:
        return ""
    return (
        '<div class="lesson-meta"><span class="meta-label">Lesson family</span>'
        '<span class="family-chip"><span class="chip-dot" aria-hidden="true"></span>'
        + esc(label)
        + "</span></div>"
    )


def lesson_stage(
    *,
    sql: str,
    note: str = "",
    bullets: list = None,
    svg_id: str = "viz",
    viewbox: str = "0 0 960 540",
    phase_default: str = "",
    extra_stage_svg: str = "",
    readout_placeholders: list = None,
    explainer_title: str = "What you'll see",
    learn_more_html: str = "",
    toolbar_status: str = "Ready — press Play",
) -> dict:
    """Slice 3 / T4 — shared stage scaffold for every teach lesson.

    Returns the four HTML blobs ``render_page`` expects, each already
    composed using the smaller helpers above (``query_card``,
    ``explainer``, ``stage_toolbar``, ``phase_nav``):

        {
          "controls_html": …,  # query card + explainer + toolbar
          "stage_html":    …,  # <svg id=svg_id viewBox=viewbox>…</svg>
          "readout_html":  …,  # optional dl of live-updated stats
          "learn_more_html": … # as-passed; for curriculum Prev/Next etc.
        }

    Every output is pure HTML — the lesson is responsible only for the
    SVG elements and the JS that animates them. Authors call
    ``render_page(**lesson_stage(...), lesson_js=...)`` which
    eliminates ~80 lines of duplicated per-lesson chrome and makes
    cross-family drift impossible.

    ``readout_placeholders`` is a list of ``(id, label)`` tuples that
    render as ``<dt>label</dt><dd id="…">…</dd>`` for the lesson's JS
    to populate at runtime.

    ``phase_default`` seeds the visible phase label inside the
    phase-nav block. The JS engine will swap it as the timeline runs.
    """
    if bullets is None:
        bullets = []
    if readout_placeholders is None:
        readout_placeholders = []

    controls = []
    if sql:
        controls.append(query_card(sql, note))
    if bullets:
        controls.append(explainer(explainer_title, bullets))
    controls.append(stage_toolbar(toolbar_status))
    controls_html = "\n".join(controls)

    stage_html = (
        '<div class="stage-wrap">\n'
        f'  <svg id="{esc(svg_id)}" viewBox="{esc(viewbox)}"'
        f' preserveAspectRatio="xMidYMid meet"'
        f' role="img" aria-label="Lesson animation stage">\n'
        f'    {extra_stage_svg}\n'
        f'  </svg>\n'
        f'  {phase_nav()}\n'
        f'  <p class="phase-label" id="phase-label">{esc(phase_default)}</p>\n'
        '</div>'
    )

    if readout_placeholders:
        readout_items = "\n".join(
            f'    <dt>{esc(label)}</dt>\n    <dd id="{esc(rid)}">—</dd>'
            for rid, label in readout_placeholders
        )
        readout_html = (
            '<section class="readout" aria-label="Live stats">\n'
            '  <dl>\n'
            f'{readout_items}\n'
            '  </dl>\n'
            '</section>'
        )
    else:
        readout_html = ""

    return {
        "controls_html": controls_html,
        "stage_html": stage_html,
        "readout_html": readout_html,
        "learn_more_html": learn_more_html,
    }


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
    family_chip_html = _family_chip(lesson_id)
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
      {family_chip_html}
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
{TIER1_RUNTIME_JS}
{_BASE_JS}
{lesson_js}
  </script>
</body>
</html>
"""
