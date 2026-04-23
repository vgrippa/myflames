"""
Shared Big O complexity legend — an SVG fragment embeddable in any of the
four renderers' output so a newcomer can decode the ``O(...)`` chips
without leaving the page.

The legend is deliberately compact (~110 px tall) and collapsible-by-CSS:
senior DBAs who already know Big O should be able to hide it in a browser
without the primary chart reflowing.

Usage::

    from .complexity_legend import render_complexity_legend_svg
    lines = render_complexity_legend_svg(x=20, y=600, width=1160)
    svg_output.extend(lines)

One-line summary printed along the top of the legend keeps the "what even
is this?" question answerable inside the SVG itself, so the file stays
self-describing when exported.
"""
from .complexity import SEVERITY_COLORS, SEVERITY_BORDERS


# The decision-table classes we surface in real plans, from cheapest to
# most dangerous. Paired with (short, one-line explanation, severity).
# Kept separate from ``myflames.complexity`` because the legend is a
# pedagogy artifact — the underlying complexities can evolve without
# forcing a visual redesign here.
LEGEND_ROWS = [
    ("O(1)",              "good",   "constant — one step, regardless of size"),
    ("O(log n)",          "good",   "logarithmic — B-tree / hash lookup"),
    ("O(log n + k)",      "good",   "indexed range — descent + k matches"),
    ("O(n)",              "medium", "linear — full table or index scan"),
    ("O(n log n)",        "medium", "sort / group by (filesort)"),
    ("O(n + m)",          "good",   "hash join — one pass each side"),
    ("O(n · log m)",      "medium", "indexed nested-loop join"),
    ("O(n · m)",          "bad",    "unindexed nested-loop — quadratic blow-up"),
    ("O(2ⁿ)",             "bad",    "exponential — rare at run time, real at plan time"),
]


def render_complexity_legend_svg(x=16, y=0, width=1200, title="Big O complexity — how this query scales"):
    """Return a list of SVG lines rendering the legend at ``(x, y)``.

    The caller is responsible for positioning; the legend occupies
    approximately ``width`` × 108 px. Text colors use the same palette
    as the chips so the visual vocabulary is consistent across views.
    """
    chip_h = 18
    chip_gap_y = 22
    chip_col_w = 128      # width reserved for the chip itself
    text_gap_x = 12       # space between chip and its explanation
    # Legend shows 3 columns of 3 rows (fits comfortably in 1200 px canvas).
    n_cols = 3
    col_w = (width - 16) // n_cols
    rows_per_col = (len(LEGEND_ROWS) + n_cols - 1) // n_cols

    lines = []
    lines.append(
        f'<g class="complexity-legend" aria-label="Big O complexity legend">'
    )
    # Outer background
    legend_h = 20 + rows_per_col * chip_gap_y + 12
    lines.append(
        f'<rect x="{x}" y="{y}" width="{width}" height="{legend_h}" '
        f'rx="6" ry="6" fill="#f8fafc" stroke="#e2e8f0" stroke-width="1"/>'
    )
    lines.append(
        f'<text x="{x + 10}" y="{y + 14}" '
        f'style="font: 600 11px ui-monospace, SFMono-Regular, Menlo, monospace; fill:#0f172a;">'
        f'{title}</text>'
    )
    lines.append(
        f'<text x="{x + width - 10}" y="{y + 14}" text-anchor="end" '
        f'style="font: 10px ui-monospace, Menlo, monospace; fill:#64748b;">'
        f'good = cheap  ·  medium = scales with data  ·  bad = risks timeout at scale'
        f'</text>'
    )
    # Rows
    start_y = y + 28
    for i, (big_o, sev, explanation) in enumerate(LEGEND_ROWS):
        col = i // rows_per_col
        row = i % rows_per_col
        row_x = x + 10 + col * col_w
        row_y = start_y + row * chip_gap_y
        # Chip
        chip_fill = SEVERITY_COLORS.get(sev, SEVERITY_COLORS["medium"])
        chip_stroke = SEVERITY_BORDERS.get(sev, SEVERITY_BORDERS["medium"])
        chip_w = min(chip_col_w - 8, max(60, 7 * len(big_o) + 18))
        lines.append(
            f'<rect x="{row_x}" y="{row_y}" width="{chip_w}" height="{chip_h}" '
            f'rx="9" ry="9" fill="{chip_fill}" stroke="{chip_stroke}" stroke-width="0.8"/>'
        )
        lines.append(
            f'<text x="{row_x + chip_w/2}" y="{row_y + chip_h - 5}" text-anchor="middle" '
            f'style="font: 700 10px ui-monospace, Menlo, monospace; fill:#0f172a;">'
            f'{big_o}</text>'
        )
        # Explanation
        lines.append(
            f'<text x="{row_x + chip_w + text_gap_x}" y="{row_y + chip_h - 5}" '
            f'style="font: 11px ui-sans-serif, -apple-system, BlinkMacSystemFont, \'Segoe UI\', sans-serif; fill:#1f2937;">'
            f'{explanation}</text>'
        )
    lines.append('</g>')
    return lines, legend_h


__all__ = ["render_complexity_legend_svg", "LEGEND_ROWS"]
