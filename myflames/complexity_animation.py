"""
Animated Big O complexity chart — for the "Learn this operator" teach dialog.

Emits a self-contained SVG fragment that:

* Plots six canonical complexity classes on a **log-log** chart — the
  standard academic presentation that shows four decades of n across twelve
  decades of operations without any curve escaping or plateauing.
* Computes every curve point from the actual function. No hand-drawn
  béziers, no approximations: polynomials become straight lines with slope
  equal to their exponent; exponential exits the chart when 2ⁿ > 10¹² at
  n ≈ 40. The plotted data is 100% mathematically accurate.
* Highlights the curve matching the operator's complexity class (if known)
  with a thicker stroke, a bold label, and an animated marker dot that
  rides along the curve as a vertical "n cursor" sweeps left→right.
* Uses pure SMIL animation so it renders without JavaScript in offline
  HTML reports.

Public API::

    render_complexity_animation_svg(complexity_dict=None,
                                    width=560, height=280) -> str

The optional ``complexity_dict`` is one complexity entry (as produced by
:func:`myflames.complexity.compute_complexity`); when provided, the matching
curve is highlighted. Pass ``None`` for the "generic" chart used as a
template.
"""
from __future__ import annotations

import math

from .complexity import SEVERITY_COLORS, SEVERITY_BORDERS


# ---------------------------------------------------------------------------
# Curve specification
# ---------------------------------------------------------------------------
# Each row: (kind_key, human label, color, fn-of-n, curve-specific metadata)
# The ``kind_key`` string is what ``render_complexity_animation_svg`` matches
# against the "big_o" field of a passed-in complexity dict to decide which
# curve to highlight. The matching is substring-based on the formula.

_CURVES = [
    # key            label           color       fn
    ("const",        "O(1)",         "#10b981",  lambda n: 1),
    ("log",          "O(log n)",     "#0ea5e9",  lambda n: max(math.log2(n + 1), 1e-9)),
    ("linear",       "O(n)",         "#6366f1",  lambda n: float(n)),
    ("nlogn",        "O(n log n)",   "#a855f7",  lambda n: float(n) * max(math.log2(n + 1), 1e-9)),
    ("quad",         "O(n²)",        "#ef4444",  lambda n: float(n) * float(n)),
    ("exp",          "O(2ⁿ)",        "#be123c",  lambda n: 2.0 ** min(n, 50)),
]


# Map a complexity formula → which of our canonical classes to highlight.
# We inspect the ``big_o`` text (after normalising Unicode) to decide.
_MATCHERS = [
    # Ordered most-specific first so we don't match "O(n · log m)" as "O(n)".
    ("exp",    ("o(2", "2^n", "2ⁿ", "exponential")),
    ("quad",   ("o(n²)", "o(n^2)", "n · m", "n * m", "n×m", "n.log", "o(n*n)")),
    ("nlogn",  ("n log n", "n·log n", "n*log n", "log(n+m)", "nlogn", "n · log m", "n·log m", "n log m")),
    ("linear", ("o(n)", "o(n + m)", "o(m + n)", "o(n+m)", "o(m+n)", "o(n · log n)")),  # catchall linear forms
    ("log",    ("log n", "log n + k", "log m", "log k")),
    ("const",  ("o(1)",)),
]


def _classify(complexity_dict):
    """Return the curve key (``'linear'`` etc.) that best matches the
    complexity's ``big_o`` text, or ``None`` if unknown."""
    if not complexity_dict:
        return None
    big_o = (complexity_dict.get("big_o") or "").lower()
    if not big_o:
        return None
    for key, needles in _MATCHERS:
        for needle in needles:
            if needle in big_o:
                return key
    return None


# ---------------------------------------------------------------------------
# SVG emission
# ---------------------------------------------------------------------------

def render_complexity_animation_svg(
    complexity_dict=None,
    width=560,
    height=280,
    n_max=1_000_000,
    ops_max=10 ** 12,
    animation_seconds=11,   # 8s × (1/0.7) ≈ 11 — 30% slower per user
    label="Big O complexity",
):
    """Return an SVG fragment (no outer wrapper) plotting six complexity
    classes on log-log axes with an animated n-cursor.

    Parameters
    ----------
    complexity_dict : dict or None
        A ``myflames.complexity.compute_complexity`` dict. If recognisable,
        the matching curve is highlighted; otherwise all curves are drawn
        uniformly.
    width, height : int
        SVG canvas dimensions.
    n_max, ops_max : int
        Plot ceilings. Default 10⁶ and 10¹² — enough to show every
        polynomial fanning out while keeping exponential comfortably
        contained.
    animation_seconds : int
        Total loop duration for the cursor sweep.
    label : str
        Title drawn on the chart.
    """
    highlight_key = _classify(complexity_dict)

    # --- plot geometry ------------------------------------------------------
    # Generous top margin so the title never collides with the y-axis ticks
    # (the old 32 px put the subtitle on the same line as the first gridline
    # label which overlapped at narrow widths).
    margin_l, margin_r = 64, 96         # room for y-tick labels / curve labels
    margin_t, margin_b = 44, 48         # room for title / x-tick labels
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    x0, x1 = margin_l, margin_l + plot_w
    y0, y1 = margin_t + plot_h, margin_t           # y0 = bottom, y1 = top

    # --- log-log mappings ---------------------------------------------------
    log_n_max = math.log10(n_max)
    log_ops_max = math.log10(ops_max)

    def sx(log_n):
        return x0 + (log_n / log_n_max) * plot_w

    def sy(log_ops):
        log_ops = max(0.0, min(log_ops, log_ops_max))
        return y0 - (log_ops / log_ops_max) * plot_h

    # --- compute each curve's polyline --------------------------------------
    # We sample at 120 points in log-n space so all curves look smooth on a
    # log-log plot (polynomials should look like straight lines).
    samples_n = 120
    log_ns = [log_n_max * i / (samples_n - 1) for i in range(samples_n)]

    def path_for(fn):
        pts = []
        for ln in log_ns:
            n = 10 ** ln
            try:
                ops = fn(n)
            except OverflowError:
                ops = float("inf")
            if ops <= 0 or math.isinf(ops):
                if not pts:
                    continue
                # Stop the line if we've exceeded the chart top (exponential
                # exits early — we visually terminate at the intersection).
                break
            if ops > ops_max:
                # clip at the ceiling so exp exits the chart visibly
                # (interpolate to the exact intersection for a clean edge)
                if pts:
                    prev_ln, prev_ops = pts[-1]
                    if prev_ops <= ops_max:
                        # linear interpolation on log scale between prev_ln
                        # and ln until ops reaches ops_max
                        t = (math.log10(ops_max) - math.log10(prev_ops)) / (
                            math.log10(ops) - math.log10(prev_ops)
                        )
                        exit_ln = prev_ln + t * (ln - prev_ln)
                        pts.append((exit_ln, ops_max))
                break
            pts.append((ln, ops))
        return pts

    # --- gridlines / tick labels -------------------------------------------
    grid_lines = []
    tick_labels = []
    # vertical gridlines at every decade of n
    n_decades = int(log_n_max) + 1
    n_tick_labels = ["1", "10", "100", "1K", "10K", "100K", "1M", "10M"]
    for k in range(n_decades):
        gx = sx(k)
        grid_lines.append(
            f'<line x1="{gx:.1f}" y1="{y1}" x2="{gx:.1f}" y2="{y0}" '
            f'stroke="#e5e7eb" stroke-width="1" stroke-dasharray="2 3"/>'
        )
        tick_labels.append(
            f'<text x="{gx:.1f}" y="{y0 + 14}" text-anchor="middle" '
            f'font-family="Inter, system-ui, sans-serif" font-size="10" fill="#64748b">'
            f'{n_tick_labels[k] if k < len(n_tick_labels) else "10^" + str(k)}</text>'
        )
    # horizontal gridlines at a few decades of ops
    op_ticks = [(0, "1"), (2, "100"), (4, "10K"), (6, "1M"), (9, "1B"), (12, "1T")]
    for log_ops, lbl in op_ticks:
        if log_ops > log_ops_max:
            continue
        gy = sy(log_ops)
        grid_lines.append(
            f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1" stroke-dasharray="2 3"/>'
        )
        tick_labels.append(
            f'<text x="{x0 - 6}" y="{gy + 4:.1f}" text-anchor="end" '
            f'font-family="Inter, system-ui, sans-serif" font-size="10" fill="#64748b">{lbl}</text>'
        )

    # --- assemble curve paths (plus labels) ---------------------------------
    curve_blocks = []
    marker_blocks = []
    curve_labels = []
    for key, lbl, color, fn in _CURVES:
        pts = path_for(fn)
        if not pts:
            continue
        d = " ".join(
            ("M" if i == 0 else "L") + f" {sx(ln):.1f} {sy(math.log10(ops)):.1f}"
            for i, (ln, ops) in enumerate(pts)
        )
        highlighted = key == highlight_key
        stroke_w = 3.6 if highlighted else 1.8
        opacity = 1.0 if highlighted else 0.45
        class_attr = "complexity-curve highlighted" if highlighted else "complexity-curve"
        curve_blocks.append(
            f'<path class="{class_attr}" data-complexity-kind="{key}" '
            f'd="{d}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke_w}" stroke-linecap="round" '
            f'stroke-linejoin="round" opacity="{opacity}"/>'
        )
        # Label at the end of the curve — always rendered so the user can
        # identify which line is which.
        last_ln, last_ops = pts[-1]
        end_x = sx(last_ln)
        end_y = sy(math.log10(last_ops))
        label_x = end_x + 6
        # Keep label inside the plot band; stagger highlighted labels slightly
        # higher so the bold label doesn't collide with adjacent ones.
        label_y = max(end_y + 4, y1 + 10)
        font_size = 12 if highlighted else 11
        font_weight = 700 if highlighted else 600
        curve_labels.append(
            f'<text x="{label_x:.1f}" y="{label_y:.1f}" '
            f'font-family="Inter, system-ui, sans-serif" font-size="{font_size}" '
            f'font-weight="{font_weight}" fill="{color}">{lbl}</text>'
        )

        # Animated marker that rides the curve. Highlighted curve gets a
        # bigger, darker-stroked marker; the rest fade into the background.
        # Motion uses calcMode="spline" with a cubic ease-in-out (Slice 3
        # A3: linear motion is banned except for progress bars, per the
        # animation-expert skill). Class "anim" lets the page-level
        # reduced-motion CSS collapse the sweep for users who opt out.
        marker_path_id = f"cpath-{key}"
        marker_blocks.append(
            f'<path id="{marker_path_id}" d="{d}" fill="none" stroke="none"/>'
            f'<circle class="anim" r="{"5" if highlighted else "3"}" '
            f'fill="{color}" stroke="{"#0f172a" if highlighted else "white"}" '
            f'stroke-width="{1.4 if highlighted else 1}" opacity="{1 if highlighted else 0.65}">'
            f'<animateMotion dur="{animation_seconds}s" repeatCount="indefinite" '
            f'calcMode="spline" keyTimes="0;1" '
            f'keySplines="0.4 0 0.2 1" rotate="0">'
            f'<mpath href="#{marker_path_id}"/>'
            f'</animateMotion></circle>'
        )

    # --- animated n-cursor (vertical dashed line that sweeps left→right) ---
    cursor_block = (
        f'<line class="anim n-cursor" x1="{x0}" y1="{y1}" x2="{x0}" y2="{y0}" '
        f'stroke="#0f172a" stroke-width="1.2" stroke-dasharray="3 3" opacity="0.55">'
        f'<animate attributeName="x1" from="{x0}" to="{x1}" '
        f'dur="{animation_seconds}s" repeatCount="indefinite" '
        f'calcMode="spline" keyTimes="0;1" keySplines="0.4 0 0.2 1"/>'
        f'<animate attributeName="x2" from="{x0}" to="{x1}" '
        f'dur="{animation_seconds}s" repeatCount="indefinite" '
        f'calcMode="spline" keyTimes="0;1" keySplines="0.4 0 0.2 1"/>'
        f'</line>'
    )

    # --- axis titles + chart title ------------------------------------------
    # Title is on its own line with ~14 px clearance above the plot border.
    # The old "log-log · computed from …" subtitle was redundant (axis labels
    # already say "log scale") and collided with the title at narrow widths;
    # it has been removed.
    title_y = margin_t - 16
    title_text = (
        f'<text x="{x0}" y="{title_y}" font-family="Inter, system-ui, sans-serif" '
        f'font-size="13" font-weight="700" fill="#0f172a">{_xml_escape(label)}</text>'
    )
    subtitle_text = ""  # retained for grep/back-compat; no longer drawn.
    x_axis_title = (
        f'<text x="{(x0 + x1) / 2}" y="{y0 + 36}" text-anchor="middle" '
        f'font-family="Inter, system-ui, sans-serif" font-size="10" fill="#475569">'
        f'input size n (log scale)</text>'
    )
    y_axis_title = (
        f'<text x="{x0 - 48}" y="{(y0 + y1) / 2}" text-anchor="middle" '
        f'transform="rotate(-90 {x0 - 48} {(y0 + y1) / 2})" '
        f'font-family="Inter, system-ui, sans-serif" font-size="10" fill="#475569">'
        f'operations (log scale)</text>'
    )

    # --- plot border --------------------------------------------------------
    border = (
        f'<line x1="{x0}" y1="{y1}" x2="{x0}" y2="{y0}" stroke="#cbd5e1"/>'
        f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="#cbd5e1"/>'
    )

    # Optional severity badge — rendered on the title row, right-aligned.
    # Positioned at margin_t - 30 so it sits above the plot, on the same
    # horizontal line as the title but at the far right so the two never
    # collide even at the narrowest intended width (≈ 540 px).
    badge_block = ""
    if complexity_dict and complexity_dict.get("big_o"):
        sev = complexity_dict.get("severity") or "medium"
        fill = SEVERITY_COLORS.get(sev, SEVERITY_COLORS["medium"])
        stroke = SEVERITY_BORDERS.get(sev, SEVERITY_BORDERS["medium"])
        big_o = complexity_dict.get("big_o")
        badge_w = max(68, 8 * len(big_o) + 16)
        badge_h = 22
        badge_x = x1 - badge_w
        badge_y = title_y - badge_h + 6
        badge_block = (
            f'<g class="complexity-animation-badge">'
            f'<rect x="{badge_x}" y="{badge_y}" width="{badge_w}" height="{badge_h}" '
            f'rx="11" ry="11" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
            f'<text x="{badge_x + badge_w/2:.1f}" y="{badge_y + badge_h - 6}" '
            f'text-anchor="middle" font-family="ui-monospace, Menlo, monospace" '
            f'font-size="12" font-weight="700" fill="#0f172a">'
            f'{_xml_escape(big_o)}</text></g>'
        )

    # Inline style block so the standalone SVG honors OS-level
    # reduced-motion even when embedded outside an HTML page that
    # carries the global rule. Freezing animations via SMIL "end" is
    # the only CSS-free way to halt SMIL cleanly across browsers.
    reduced_motion_style = (
        '<style>'
        '@media (prefers-reduced-motion: reduce) {'
        '  .anim animateMotion, .anim animate, .anim animateTransform {'
        '    display: none;'
        '  }'
        '  .anim { animation: none !important; }'
        '}'
        '</style>'
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Animated log-log plot of Big O complexity classes — '
        f'O(1), O(log n), O(n), O(n log n), O(n squared), and O(2 to the n) — '
        f'drawn from their actual functions. A vertical cursor sweeps left to right '
        f'showing where each curve sits at each n.">'
        f'{reduced_motion_style}'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" rx="8" ry="8"/>'
        f'{title_text}{subtitle_text}'
        f'{"".join(grid_lines)}'
        f'{border}'
        f'{"".join(curve_blocks)}'
        f'{"".join(marker_blocks)}'
        f'{cursor_block}'
        f'{"".join(tick_labels)}'
        f'{"".join(curve_labels)}'
        f'{x_axis_title}{y_axis_title}'
        f'{badge_block}'
        f'</svg>'
    )


def _xml_escape(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


__all__ = ["render_complexity_animation_svg"]
