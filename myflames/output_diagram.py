"""Compatibility entry point for the unified Visual Explain renderer.

The former diagram and Workbench views share one implementation. Existing
Python callers and ``--type diagram`` continue to work.
"""
from .output_workbench import render_workbench


def render_diagram(root, width=1200, title="MySQL Query Plan", unit_display="ms",
                   analysis=None, teach_index_by_folded=None):
    """Render Visual Explain; compatibility alias for :func:`render_workbench`."""
    return render_workbench(root, width=width, title=title,
                            unit_display=unit_display, analysis=analysis,
                            teach_index_by_folded=teach_index_by_folded)
