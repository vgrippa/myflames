"""Interactive, identity-preserving projections of the canonical operator tree.

Native charts retain their existing encodings. The metric list is a separate
linear comparison, so choosing rows never relabels a time-based flame graph.
"""
import copy
import math

from .parser import flatten_nodes

VIEWS = ('diagram', 'workbench', 'tree', 'bargraph', 'flamegraph')
#: Views drawn by the myflames-charts.js bundle from chart-data-1.0 (all of
#: them). 'diagram' is an alias of Visual Explain and renders identically.
BUNDLE_VIEWS = VIEWS
METRICS = {
    'self_time': ('Self time', 'ms across loops'),
    'total_time': ('Total time', 'ms across loops; includes inputs'),
    'rows': ('Actual rows', 'rows per loop'),
    'estimate_error': ('Estimate error', 'larger/smaller row estimate, 1\u00d7 = exact'),
}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def metric_value(node, metric):
    """Return a finite display value and label; unavailable is never zero."""
    details = node.get('details') or {}
    if metric in ('self_time', 'total_time'):
        value = _number(node.get(metric)) if details.get('actual_last_row_ms') is not None else None
        return value, '{:,.3g} ms'.format(value) if value is not None else 'Not measured'
    actual = _number(details.get('actual_rows'))
    if metric == 'rows':
        return actual, '{:,.3g} rows'.format(actual) if actual is not None else 'Not measured'
    if metric != 'estimate_error':
        raise ValueError('Unknown exploration metric: ' + str(metric))
    estimated = _number(details.get('estimated_rows'))
    if actual is None or estimated is None:
        return None, 'Not available'
    if actual == estimated:
        return 1, '1\u00d7 exact'
    if min(actual, estimated) == 0:
        return None, 'Zero estimate' if estimated == 0 else 'Zero actual rows'
    ratio = max(actual, estimated) / min(actual, estimated)
    if not math.isfinite(ratio):
        return None, 'Beyond numeric range'
    return ratio, '{:,.3g}\u00d7 {}'.format(ratio, 'underestimated' if actual > estimated else 'overestimated')


def project_tree(root, focus_id='', collapsed_ids=None):
    """Copy one branch and hide descendants without rewriting measurements."""
    if not isinstance(focus_id, str):
        raise ValueError('Focus operator must be a node ID.')
    nodes = {node['node_id']: node for node in flatten_nodes(root)}
    if focus_id and focus_id not in nodes:
        raise ValueError('Unknown focus operator.')
    if collapsed_ids is None:
        collapsed_ids = []
    if not isinstance(collapsed_ids, (list, tuple, set)) or any(not isinstance(x, str) for x in collapsed_ids):
        raise ValueError('Collapsed operators must be a list of node IDs.')
    collapsed = set(collapsed_ids)
    if collapsed.difference(nodes):
        raise ValueError('Unknown collapsed operator.')
    result = copy.deepcopy(nodes.get(focus_id, root))
    for node in list(flatten_nodes(result)):
        if node['node_id'] in collapsed and node.get('children'):
            node['_hidden_descendants'] = sum(1 for _ in flatten_nodes(node)) - 1
            node['children'] = []
    return result


def render_exploration(root, view, width=1200, selected_id='', focus_id='',
                       collapsed_ids=None, metric='self_time'):
    """Return standalone HTML with a native chart, metric list, and selection.

    Every view is drawn by the inlined ``myflames-charts.js`` bundle from
    ``chart-data-1.0``. The plain ``--type X > out.svg`` path keeps using
    the Python renderers.

    Parent messages: ``{type: 'myflames-select', node_id: 'n:...'}``.
    Child messages: ``{type: 'myflames-node', node_id: 'n:...'}``.
    Unknown IDs and messages from windows other than the parent are ignored.
    """
    if view not in VIEWS:
        raise ValueError('Unknown exploration view.')
    if not isinstance(metric, str) or metric not in METRICS:
        raise ValueError('Unknown exploration metric.')
    width = max(720, min(3000, int(width)))
    # Validates focus/collapse exactly as before; the bundle projects a copy.
    project_tree(root, focus_id, collapsed_ids)
    return _render_bundle_page(root, view, width, selected_id, focus_id, collapsed_ids, metric)


_BUNDLE_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Query exploration</title><style>body{margin:0;background:#fff;color:#1d1d1f;'
    'font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}</style></head>'
    '<body>__EMBED__</body></html>')


def _render_bundle_page(root, view, width, selected_id, focus_id, collapsed_ids, metric):
    """Exploration page drawn by the inlined bundle.

    The full canonical tree is serialized once; focus/collapse/selection are a
    copy-only projection the bundle applies, so measurements never change.
    Everything is inline for the workspace's sandboxed srcdoc iframe.
    """
    from .chart_assets import chart_embed
    from .chart_data import build_chart_data
    data = build_chart_data(root, title='Query plan', projection={
        'focus_id': focus_id or '', 'collapsed_ids': list(collapsed_ids or []),
        'selected_id': selected_id if isinstance(selected_id, str) else str(selected_id),
        'metric': metric})
    # 'diagram' is an alias of Visual Explain and renders identically.
    embed = chart_embed(data, 'workbench' if view == 'diagram' else view,
                        width=width, panels=('metrics',))
    return _BUNDLE_PAGE.replace('__EMBED__', embed)
