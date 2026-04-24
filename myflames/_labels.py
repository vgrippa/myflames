"""
Shared label-fitting for every renderer (Slice 3.5 / V5).

Each renderer used to have its own truncation logic with a different
ellipsis style (``..`` vs ``...``) and a different fixed-character cap
that didn't honor the actual drawn width. Two problems followed:

1. Qualified names like ``users.very_long_column_name`` always lost
   their tail to the truncation — but the tail is the *discriminating*
   part (``..name`` identifies the column; ``users..`` doesn't).
2. Variable character widths (M, i, ., CJK) meant the "character cap"
   was a lie: some labels overflowed, others had unused space.

This module replaces all of that with:

* ``fit_label(text, px_width, font_size, font_width=0.59)`` — one
  helper, one ellipsis (``…``), and a heuristic width estimator that
  callers can feed a monospace-ish ``font_width`` ratio to.
* ``_middle_ellipsis`` — prefers preserving the dotted tail of a
  qualified name (the shape ``table.column``) by eliding from the
  *middle* instead of the end.

The helper is pure — no I/O, no SVG, no DOM. Unit tests at
``test/test_labels.py`` cover CJK, surrogate pairs, empty input, and
the middle-ellipsis branch.
"""

ELLIPSIS = "…"  # Unicode HORIZONTAL ELLIPSIS — single glyph, not three dots


def _char_width(ch, base_ratio):
    """Per-char width multiplier.

    CJK / fullwidth characters roughly double the advance width of a
    monospace-ish font. Anything outside the BMP (emoji, rare CJK) is
    counted as two as well so we don't over-commit column budget.
    """
    cp = ord(ch)
    if cp > 0xFFFF:                # SMP / supplementary planes
        return 2.0 * base_ratio
    if 0x1100 <= cp <= 0x115F:     # Hangul Jamo
        return 2.0 * base_ratio
    if 0x2E80 <= cp <= 0x9FFF:     # CJK
        return 2.0 * base_ratio
    if 0xAC00 <= cp <= 0xD7A3:     # Hangul syllables
        return 2.0 * base_ratio
    if 0xFF00 <= cp <= 0xFF60 or 0xFFE0 <= cp <= 0xFFE6:  # Fullwidth forms
        return 2.0 * base_ratio
    return base_ratio


def _width_px(text, font_size, font_width):
    """Heuristic pixel width of ``text`` at ``font_size``.

    Uses a per-character multiplier so CJK / emoji don't under-count.
    Callers who need the *true* pixel width (e.g. flamegraph.py's
    runtime SVG ``getSubStringLength``) should still use that; this
    helper is for Python-side layout decisions where we can't measure
    the browser's box.
    """
    if not text:
        return 0.0
    total = 0.0
    for ch in text:
        total += _char_width(ch, font_width)
    return total * font_size


def _middle_ellipsis(text, keep_chars):
    """Collapse the middle of ``text`` to fit within ``keep_chars``.

    Preserves the qualified-name tail when possible. ``users.long_name``
    with keep=8 becomes ``users.l…me`` rather than ``users..`` — the
    column tail is the discriminating token.

    Falls back to end-ellipsis when the input has no ``.`` delimiter
    (single identifier) since there's no "tail" to preserve.
    """
    if keep_chars <= 1:
        return ELLIPSIS
    if len(text) <= keep_chars:
        return text

    dot = text.rfind(".")
    if dot <= 0 or dot >= len(text) - 1:
        # Single identifier (no dotted qualification). End-ellipsis is fine.
        return text[: keep_chars - 1] + ELLIPSIS

    # Qualified name. Budget: prefix + ellipsis + suffix = keep_chars.
    budget = keep_chars - 1  # reserve 1 for the ellipsis glyph
    # Preferred split: keep the whole table part, then as much of the
    # tail as fits. Fall back to balanced halves when the table part
    # alone exceeds the budget.
    table = text[:dot + 1]
    col = text[dot + 1:]
    if len(table) < budget:
        tail = col[-(budget - len(table)):]
        return table + ELLIPSIS + tail
    # Table longer than budget — fall back to halving.
    half = budget // 2
    head = text[:half]
    tail_len = budget - half
    tail = text[-tail_len:]
    return head + ELLIPSIS + tail


def fit_label(text, px_width, font_size, font_width=0.59, prefer_middle=True):
    """Return ``text`` truncated to fit within ``px_width`` pixels.

    The returned string, when rendered at ``font_size`` with the given
    monospace-ish ``font_width`` ratio, is estimated to be <= px_width.
    Unicode ellipsis (``…``) replaces elided chars.

    Parameters
    ----------
    text : str
        The label to fit. Empty string returns an empty string.
    px_width : float
        Budgeted pixel width of the text box.
    font_size : int | float
        Rendered em size in pixels.
    font_width : float
        Advance-width ratio of the font (Verdana ≈ 0.59, Arial ≈ 0.55).
    prefer_middle : bool
        When True (default) and the label contains a ``.`` (qualified
        name), elide from the middle so the discriminating tail
        survives. When False, always elide from the end — useful for
        comment-like labels where the beginning is more important.

    Returns
    -------
    str
        A fitted version of ``text``. May be empty when ``px_width`` is
        narrower than a single ellipsis glyph.
    """
    if not text:
        return ""
    if _width_px(text, font_size, font_width) <= px_width:
        return text

    # Width available if we reserve the ellipsis. Convert to an
    # approximate character budget using the average char width.
    avg_char_px = font_size * font_width
    if avg_char_px <= 0:
        return ELLIPSIS
    budget_chars = max(1, int(px_width / avg_char_px))

    if prefer_middle and "." in text:
        fitted = _middle_ellipsis(text, budget_chars)
    else:
        fitted = text[: max(0, budget_chars - 1)] + ELLIPSIS

    # Safety: if our heuristic over-committed (CJK-heavy strings), trim
    # one char at a time until we fit. Guaranteed to terminate because
    # each iteration removes at least one real char.
    while fitted and _width_px(fitted, font_size, font_width) > px_width:
        if len(fitted) <= 1:
            return ELLIPSIS
        # Strip from the middle if we still have an ellipsis, else end.
        ell_idx = fitted.find(ELLIPSIS)
        if ell_idx > 0 and ell_idx < len(fitted) - 1:
            fitted = fitted[: ell_idx] + ELLIPSIS + fitted[ell_idx + 2:]
        else:
            fitted = fitted[:-2] + ELLIPSIS
    return fitted
