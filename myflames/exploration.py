"""Interactive, identity-preserving projections of the canonical operator tree.

Native charts retain their existing encodings. The metric list is a separate
linear comparison, so choosing rows never relabels a time-based flame graph.
"""
import copy
import html
import math
import re
from pathlib import Path

from .output_html_report import _render_svg
from .parser import flatten_nodes

VIEWS = ('diagram', 'workbench', 'tree', 'bargraph', 'treemap', 'flamegraph')
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

    Parent messages: ``{type: 'myflames-select', node_id: 'n:...'}``.
    Child messages: ``{type: 'myflames-node', node_id: 'n:...'}``.
    Unknown IDs and messages from windows other than the parent are ignored.
    """
    if view not in VIEWS:
        raise ValueError('Unknown exploration view.')
    if not isinstance(metric, str) or metric not in METRICS:
        raise ValueError('Unknown exploration metric.')
    width = max(720, min(3000, int(width)))
    projected = project_tree(root, focus_id, collapsed_ids)
    svg = _render_svg(projected, view, width, 'Query plan', 'ms', node_identity=True)
    svg = re.sub(r'<\?xml[^>]*\?>|<!DOCTYPE[^>]*>', '', svg)
    if not svg:
        svg = '<p role="status" style="padding:20px">No positive time is available at this chart’s resolution. Inspect the operator metrics or use Visual Explain.</p>'
    visible = list(flatten_nodes(projected))
    values = [metric_value(node, metric) for node in visible]
    maximum = max([value for value, _ in values if value is not None] or [1]) or 1
    label, unit = METRICS[metric]
    rows = []

    def append_rows(node, depth=0):
        value, display = metric_value(node, metric)
        proportion = 0 if value is None else min(100, value / maximum * 100)
        name = node.get('short_label') or node.get('full_label') or 'Operator'
        hidden = node.get('_hidden_descendants', 0)
        if hidden:
            name += ' ({} hidden)'.format(hidden)
        relationship = ' \u00b7 SELECT-list subquery' if (node.get('details') or {}).get('input_kind') == 'select_list' else ''
        rows.append('<button class="metric-row" data-node-id="{}" style="--depth:{};--amount:{:.4f}%"><span class="metric-name">{}</span><span class="metric-value">{}</span><span class="metric-track"><span></span></span></button>'.format(
            html.escape(node['node_id'], quote=True), depth, proportion,
            html.escape(name + relationship), html.escape(display)))
        for child in node.get('children', []):
            append_rows(child, depth + 1)
    append_rows(projected)
    note = ('Native chart uses its original time or operation encoding. Metric bars compare the visible operators; total times overlap and must not be summed.')
    if collapsed_ids:
        note += ' Collapsed descendants are excluded from the chart; displayed measurements are unchanged.'
    if any((n.get('details') or {}).get('input_kind') == 'select_list' for n in visible):
        note += ' SELECT-list subqueries are separate execution branches; their timings are not subtracted from the enclosing iterator.'
    script = Path(__file__).with_name('exploration.js').read_text(encoding='utf-8')
    template = '''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Query exploration</title><style>
body{margin:0;background:#fff;color:#1d1d1f;font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.chart{overflow:auto}.chart>svg{max-width:100%;height:auto;display:block}.metric-panel{padding:18px 20px;border-top:1px solid #e5e5ea}
h2{margin:0 0 4px;font-size:14px}p{color:#6e6e73;font-size:12px;line-height:1.5;margin:4px 0 14px}
.metric-row{--depth:0;display:grid;grid-template-columns:minmax(150px,1fr) 150px 100px;gap:16px;align-items:center;text-align:left;width:100%;border:0;border-radius:7px;background:transparent;padding:9px 10px;cursor:pointer;color:inherit;font:inherit}
.metric-row:hover{background:#f5f5f7}.metric-name{padding-left:calc(var(--depth)*14px);overflow-wrap:anywhere}.metric-value{font-variant-numeric:tabular-nums;text-align:right;font-size:12px}.metric-track{height:5px;background:#f0f0f3;border-radius:5px}.metric-track>span{display:block;width:var(--amount);height:100%;border-radius:inherit;background:#007aff}
.metric-row.is-selected{background:#eaf3ff;box-shadow:inset 0 0 0 1px #007aff}.chart [data-node-id]{cursor:pointer}.chart .is-selected{filter:drop-shadow(0 0 3px #007aff)}.chart rect.is-selected,.chart .is-selected>rect,.chart .is-selected>polygon{stroke:#007aff!important;stroke-width:3!important}
[data-node-id]:focus-visible{outline:2px solid #007aff;outline-offset:2px}
@media(max-width:640px){.metric-row{grid-template-columns:minmax(100px,1fr) 110px}.metric-track{display:none}}
</style></head><body data-selected="__SELECTED__"><div class="chart">__SVG__</div><section class="metric-panel" aria-label="Operator metrics"><h2>__LABEL__ <small>· __UNIT__</small></h2><p>__NOTE__</p>__ROWS__</section><script>__SCRIPT__</script></body></html>'''
    substitutions = {'__SELECTED__': html.escape(str(selected_id), quote=True),
                     '__SVG__': svg, '__LABEL__': html.escape(label),
                     '__UNIT__': html.escape(unit), '__NOTE__': html.escape(note),
                     '__ROWS__': ''.join(rows), '__SCRIPT__': script}
    return re.sub('|'.join(substitutions), lambda match: substitutions[match.group(0)], template)
