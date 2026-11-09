"""`myflames config versions`: the offline parameter version catalog.

Answers come from the generated sysvar_history catalogs only; no server is
contacted. Follows the cli-ux contract: payload on stdout, diagnostics on
stderr, exit 0 on success and 2 on bad input.
"""
import argparse
import json
import sys

from . import param_versions
from .cli import _fail
from .perf_common import table_text
from .workspace_cli import _emit_text

EPILOG = ('Reads the generated parameter catalogs only; no server is contacted.\n'
          'Exit codes: 0 success (history of an unknown name included: it lists close names),\n'
          '2 bad input or a version the catalog does not have.')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        _fail(message)


def _parser():
    parser = Parser(prog='myflames config versions', add_help=False,
                    description='Compare the parameter catalogs of two server versions, or trace one parameter.',
                    epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    subs = parser.add_subparsers(dest='operation', metavar='ACTION')

    list_p = subs.add_parser('list', add_help=False, epilog=EPILOG,
                             formatter_class=argparse.RawDescriptionHelpFormatter,
                             help='List the cataloged releases per engine.')
    list_p.add_argument('--help', action='help', help='Show this help message and exit.')
    list_p.add_argument('--engine', choices=param_versions.ENGINES, help='Show only this engine.')
    list_p.add_argument('--json', action='store_true',
                        help='Emit the %s report as machine-readable JSON.' % param_versions.CATALOG_SCHEMA)
    list_p.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
    list_p.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; keep the result payload.')

    compare_p = subs.add_parser('compare', add_help=False, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                help='Compare the parameters of two cataloged releases.')
    compare_p.add_argument('--help', action='help', help='Show this help message and exit.')
    compare_p.add_argument('from_version', metavar='FROM',
                           help='Cataloged release to compare from, for example 8.0.30.')
    compare_p.add_argument('to_version', metavar='TO',
                           help='Cataloged release to compare to, for example 8.4.0.')
    compare_p.add_argument('--engine', choices=param_versions.ENGINES,
                           help='Choose the engine; inferred when only one catalog has both versions.')
    compare_p.add_argument('--kind', action='append', choices=param_versions.KINDS, metavar='KIND',
                           help='Show only this kind of change; repeat to choose more kinds.')
    compare_p.add_argument('--name', metavar='TEXT', help='Show only parameters whose name contains TEXT.')
    group = compare_p.add_mutually_exclusive_group()
    group.add_argument('--json', action='store_true',
                       help='Emit the %s report as machine-readable JSON.' % param_versions.COMPARE_SCHEMA)
    group.add_argument('--markdown', action='store_true', help='Emit the changes as a Markdown table.')
    compare_p.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
    compare_p.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; keep the result payload.')

    history_p = subs.add_parser('history', add_help=False, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                help='Trace one parameter through the cataloged releases.')
    history_p.add_argument('--help', action='help', help='Show this help message and exit.')
    history_p.add_argument('name', metavar='NAME', help='Parameter name, for example innodb_io_capacity.')
    history_p.add_argument('--engine', choices=param_versions.ENGINES,
                           help='Choose the engine; without it every engine that has the name is shown.')
    group = history_p.add_mutually_exclusive_group()
    group.add_argument('--json', action='store_true',
                       help='Emit the %s report as machine-readable JSON (an array of reports without --engine).'
                       % param_versions.HISTORY_SCHEMA)
    group.add_argument('--markdown', action='store_true', help='Emit the changes as a Markdown table.')
    history_p.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
    history_p.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; keep the result payload.')
    return parser


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def _default_text(facts):
    if facts is None:
        return '(absent)'
    if facts['default'] is not None:
        value = facts['default']
        return value + ' (host)' if facts['default_note'] == 'host' else value
    return {'null': 'NULL', 'host': 'depends on the host'}.get(facts['default_note'], 'unknown')


#: The kind whose value the From/To and Before/After columns show, as in the
#: workspace (paramVersionsApi.ts SHOWN): what changed, not always the default.
_SHOWN = ('default', 'range', 'values', 'type', 'scope', 'dynamic', 'deprecated', 'undeprecated', 'added', 'removed')


def _kind_text(kinds, facts):
    if facts is None:
        return '(absent)'
    kind = next((k for k in _SHOWN if k in kinds), 'default')
    if kind == 'range':
        return '%s to %s' % (facts['min'] if facts['min'] is not None else '?',
                             facts['max'] if facts['max'] is not None else '?')
    if kind == 'values':
        return ', '.join(facts['values']) if facts['values'] is not None else 'unknown'
    if kind in ('type', 'scope'):
        return facts[kind] or 'unknown'
    if kind == 'dynamic':
        return {True: 'dynamic', False: 'static'}.get(facts['dynamic'], 'unknown')
    if kind in ('deprecated', 'undeprecated'):
        return 'deprecated' if facts['deprecated'] else 'not deprecated'
    return _default_text(facts)


def _list_text(report):
    rows = []
    notes = []
    for engine in report['engines']:
        if engine['available']:
            rows.append([engine['product'], str(len(engine['versions'])),
                         engine['versions'][0]['version'] if engine['versions'] else '',
                         engine['versions'][-1]['version'] if engine['versions'] else '',
                         str(len(engine['names'])), str(len(engine['skipped']))])
        else:
            rows.append([engine['product'], 'not available', '', '', '', ''])
        notes.extend('%s: %s' % (engine['product'], note) for note in engine['notes'])
    text = table_text(['Engine', '>Releases', 'First', 'Latest', '>Parameters', '>Skipped'], rows) + '\n'
    text += ''.join('Note: %s\n' % note for note in notes)
    return text


def _change_row(change, report):
    since = ', '.join(dict.fromkeys(v for v in change['since'].values() if v))
    return [change['name'], ', '.join(change['kinds']),
            _kind_text(change['kinds'], change['from']), _kind_text(change['kinds'], change['to']), since]


def _filtered(report, kinds, name):
    if not kinds and not name:
        return report
    changes = []
    for change in report['changes']:
        if kinds and not set(change['kinds']) & set(kinds):
            continue
        if name and name.lower() not in change['name'].lower():
            continue
        changes.append(change)
    # counts and summary keep describing the whole comparison; filter and
    # shown say what this output lists.
    return dict(report, changes=changes, filter={'kinds': list(kinds or []), 'name': name}, shown=len(changes))


def _compare_text(report):
    text = report['summary'] + '\n\n'
    text += table_text(['Parameter', 'Change', report['from'], report['to'], 'Since'],
                       [_change_row(change, report) for change in report['changes']]) + '\n'
    for change in report['changes']:
        for sentence in change['text']:
            text += '%s: %s\n' % (change['name'], sentence)
    text += ''.join('Note: %s\n' % note for note in report['notes'])
    return text


def _markdown_table(headers, rows):
    def cell(value):
        return str(value).replace('|', '\\|')
    lines = ['| ' + ' | '.join(cell(h) for h in headers) + ' |',
             '| ' + ' | '.join('---' for _ in headers) + ' |']
    lines += ['| ' + ' | '.join(cell(c) for c in row) + ' |' for row in rows]
    return '\n'.join(lines) + '\n'


def _compare_markdown(report):
    return (report['summary'] + '\n\n'
            + _markdown_table(['Parameter', 'Change', report['from'], report['to'], 'Since'],
                              [_change_row(change, report) for change in report['changes']]))


def _history_text(report):
    text = report['summary'] + '\n'
    if not report['found']:
        if report['suggestions']:
            text += 'Did you mean: %s?\n' % ', '.join(report['suggestions'])
        return text + ''.join('Note: %s\n' % note for note in report['notes'])
    text += '\nCurrent (%s):\n' % report['latest']
    for field in param_versions.FIELDS:
        text += '  %s: %s\n' % (field, _fact_text(field, report['current'][field]))
    text += '\n'
    text += table_text(['Version', 'Change', 'Before', 'After'],
                       [[event['version'], ', '.join(event['kinds']),
                         _kind_text(event['kinds'], event['before']), _kind_text(event['kinds'], event['after'])]
                        for event in report['events']]) + '\n'
    for event in report['events']:
        for sentence in event['text']:
            text += '%s: %s\n' % (event['version'], sentence)
    text += ''.join('Note: %s\n' % note for note in report['notes'])
    return text


def _history_markdown(report):
    text = report['summary'] + '\n\n'
    if report['found']:
        text += _markdown_table(['Version', 'Change', 'Before', 'After'],
                                [[event['version'], ', '.join(event['kinds']),
                                  _kind_text(event['kinds'], event['before']), _kind_text(event['kinds'], event['after'])]
                                 for event in report['events']])
    elif report['suggestions']:
        text += 'Did you mean: %s?\n' % ', '.join(report['suggestions'])
    return text


def _fact_text(field, value):
    if value is None:
        return '-'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, list):
        return ', '.join(value)
    return str(value)


def _infer_engine(from_version, to_version):
    matches = []
    for engine in param_versions.ENGINES:
        try:
            param_versions.load(engine)
        except param_versions.CatalogError:
            continue
        if param_versions.resolve_version(engine, from_version) \
                and param_versions.resolve_version(engine, to_version):
            matches.append(engine)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        _fail('No catalog has both %s and %s; name the engine with --engine.'
              % (from_version, to_version))
    _fail('Both MySQL and MariaDB catalog %s and %s; name the engine with --engine.'
          % (from_version, to_version))


def _histories(engine, name):
    if engine:
        return [param_versions.history(engine, name)]
    reports = []
    for candidate in param_versions.ENGINES:
        try:
            report = param_versions.history(candidate, name)
        except param_versions.CatalogError:
            continue
        if report['found']:
            reports.append(report)
    if reports:
        return reports
    # Nothing found anywhere: report every available engine's answer so the
    # suggestions of each engine reach the user.
    for candidate in param_versions.ENGINES:
        try:
            reports.append(param_versions.history(candidate, name))
        except param_versions.CatalogError:
            continue
    if not reports:
        _fail('No parameter version catalog is available in this build.')
    return reports


def command(argv):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.operation:
        parser.error('Choose list, compare or history; see: myflames config versions --help')
    try:
        if args.operation == 'list':
            report = param_versions.catalog_index()
            if args.engine:
                report = dict(report, engines=[e for e in report['engines'] if e['engine'] == args.engine])
            text = _json(report) if args.json else _list_text(report)
        elif args.operation == 'compare':
            engine = args.engine or _infer_engine(args.from_version, args.to_version)
            report = param_versions.compare(engine, args.from_version, args.to_version)
            if args.json:
                text = _json(_filtered(report, args.kind, args.name))
            else:
                text = (_compare_markdown if args.markdown else _compare_text)(
                    _filtered(report, args.kind, args.name))
        else:
            reports = _histories(args.engine, args.name)
            if args.json:
                text = _json(reports[0] if args.engine else reports)
            else:
                render = _history_markdown if args.markdown else _history_text
                text = '\n'.join(render(report) for report in reports)
    except ValueError as exc:
        _fail(str(exc))
    _emit_text(text, args.output, quiet=args.quiet)
