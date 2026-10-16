"""Inline the prebuilt ``myflames-charts.js`` bundle and its chart data.

The bundle is built from ``apps/workspace/src/charts`` by
``apps/workspace/vite.charts.config.ts`` and committed under
``myflames/assets/``; Python users need no Node.js. Pages that embed charts
must work inside a sandboxed ``srcdoc`` iframe whose CSP is
``default-src 'none'; script-src 'unsafe-inline'``, so everything is inline.
"""
import html
import json
import re
from pathlib import Path

BUNDLE = Path(__file__).with_name("assets") / "myflames-charts.js"
_SHA = re.compile(r"sources-sha256=([0-9a-f]{64})")
_runtime_cache = {}


def chart_runtime():
    """Return the bundle source, refusing text that could end a ``<script>``."""
    text = _runtime_cache.get("text")
    if text is None:
        text = BUNDLE.read_text(encoding="utf-8")
        lowered = text.lower()
        if "</script" in lowered or "<!--" in lowered:
            raise ValueError("myflames-charts.js cannot be inlined in a <script> element.")
        _runtime_cache["text"] = text
    return text


def bundle_sources_sha():
    """The source hash recorded in the bundle banner (for staleness checks)."""
    match = _SHA.search(chart_runtime()[:400])
    return match.group(1) if match else None


def script_json(data):
    """Serialize *data* for a ``<script type="application/json">`` element.

    Compact JSON; ``<``, ``>``, ``&`` and the JS line separators are escaped
    so untrusted labels cannot close the element or start a comment.
    """
    text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return (text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def chart_mount(data, view, width=1200, panels=(), options=None):
    """Return the mount element holding the chart data for one chart.

    *options* are per-view presentation settings (for example the flame
    graph's ``inverted`` and ``frame_height``); they never change data.
    """
    attrs = {"class": "mfc-mount", "data-mfc-view": view, "data-mfc-width": str(int(width))}
    if panels:
        attrs["data-mfc-panels"] = " ".join(panels)
    if options:
        attrs["data-mfc-options"] = json.dumps(options, sort_keys=True, separators=(",", ":"))
    attributes = "".join(' {}="{}"'.format(k, html.escape(v, quote=True)) for k, v in attrs.items())
    return ('<div{}><script type="application/json" class="mfc-data">{}</script>'
            '<noscript><p class="mfc-noscript">This interactive chart needs JavaScript. '
            'Use <code>myflames --type {} plan.json -o plan.svg</code> for a static SVG.</p></noscript>'
            '</div>').format(attributes, script_json(data), html.escape(view))


def chart_embed(data, view, width=1200, panels=(), runtime=True, options=None):
    """Return the mount, the bundle (when *runtime*), and the mount call.

    Pass ``runtime=False`` for the second and later charts on one page so the
    bundle is inlined once; ``MyflamesCharts.mountAll`` mounts every pending
    chart and skips charts that are already mounted.
    """
    parts = [chart_mount(data, view, width, panels, options)]
    if runtime:
        parts.append("<script>" + chart_runtime() + "</script>")
    parts.append("<script>MyflamesCharts.mountAll(document);</script>")
    return "".join(parts)
