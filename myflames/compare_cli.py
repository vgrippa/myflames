"""CLI for read-only server comparisons and local parameter snapshots."""
import argparse
import json
import os
import sqlite3
import sys

from . import account_compare, compare_api, param_compare, dbops, workspace_cli as workspace
from .cli import _fail
from .connector import ConnectorError, MySQLConnection
from .perf_common import table_text
from .workspace_store import WorkspaceStore

EPILOG = ('Reads server settings and grants only. SQL exports are text for review; nothing is executed.\n'
          'Exit codes: 0 success, 1 --fail-on different tripped, 2 bad input or a side could not be read.')
#: Snapshot and export commands have no --fail-on gate.
EPILOG_NO_GATE = ('Reads server settings and grants only. SQL exports are text for review; nothing is executed.\n'
                  'Exit codes: 0 success, 2 bad input or a server or file that could not be read.')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        _fail(message)


def _parser(area, action):
    prefix = 'myflames ' + area + ' ' + action
    description = ('Save and inspect parameter snapshots on this computer.' if action == 'snapshot' else
                   'Export credential-free account grants as SQL for review.' if action == 'export' else
                   'Compare %s from a source and a target without changing either server.' %
                   ('global parameters' if area == 'config' else 'users and roles'))
    parser = Parser(prog=prefix, add_help=False, description=description,
                    epilog=EPILOG if action == 'compare' else EPILOG_NO_GATE,
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    if action == 'snapshot':
        subs = parser.add_subparsers(dest='operation', metavar='ACTION')
        for op, help_text in (('save', 'Read and save global parameters locally.'),
                              ('list', 'List saved parameter snapshots.'),
                              ('show', 'Show a saved parameter snapshot.'),
                              ('delete', 'Delete a parameter snapshot locally.')):
            sub = subs.add_parser(op, add_help=False, help=help_text, description=help_text, epilog=EPILOG_NO_GATE)
            sub.add_argument('--help', action='help', help='Show this help message and exit.')
            if op == 'save':
                workspace._add_connection_args(sub, client_paths=True)
                sub.add_argument('--label', default='', metavar='TEXT', help='Set the snapshot label (up to 200 characters).')
            elif op in ('show', 'delete'):
                sub.add_argument('snapshot', metavar='SNAPSHOT', help='Choose a snapshot ID or latest.')
            if op == 'delete':
                sub.add_argument('--yes', action='store_true', help='Delete without the [y/N] prompt (required without a terminal).')
            else:
                sub.add_argument('--json', action='store_true', help='Emit the result as machine-readable JSON.')
                sub.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
            sub.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; keep the result payload.')
    else:
        workspace._add_connection_args(parser, client_paths=True)
        choices = ('live, profile:NAME, snapshot:ID, latest, or file:PATH (.json also works).'
                   if area == 'config' else 'live or profile:NAME.')
        if action == 'compare':
            parser.add_argument('source', metavar='SOURCE', help='Choose the source: ' + choices)
            parser.add_argument('target', metavar='TARGET', help='Choose the target: ' + choices)
            parser.add_argument('--json', action='store_true', help='Emit the %s report as machine-readable JSON.' %
                                (param_compare.COMPARE_SCHEMA if area == 'config' else account_compare.SCHEMA_VERSION))
            if area == 'config':
                parser.add_argument('--all', action='store_true', help='Include Identical and identity parameters in text output.')
            parser.add_argument('--fail-on', choices=('different',), help='Exit 1 when any parameter or account differs, including identity values.')
        else:
            parser.add_argument('side', metavar='SIDE', help='Choose the export side: live or profile:NAME.')
            parser.add_argument('--account', action='append', metavar='USER@HOST', help='Export this account and its required roles; repeat to choose more accounts.')
        parser.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
        parser.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; keep the result payload.')
    return parser


class Sources(object):
    def __init__(self, args, area):
        self.store, self.args, self.area, self.passwords = WorkspaceStore(), args, area, []

    def capture(self, settings):
        if settings.get('password'):
            self.passwords.append(settings['password'])
        options = workspace._connection_options(settings)
        options['query_timeout'] = 30
        server = {'host': options['host'], 'port': options['port']}
        if settings.get('profile_id'):
            profile = self.store.get('profile', settings['profile_id'])
            if profile:
                server.update(profile_id=profile['id'], profile_name=profile['name'])
        module = account_compare if self.area == 'accounts' else param_compare
        with MySQLConnection(**options) as conn:
            return dbops.redact_value(module.capture(conn, server), settings.get('password'))

    def side(self, ref, source=None):
        if ref == 'live':
            return self.capture(workspace._resolve_connection(self.args)), {'kind': 'live'}
        if ref.startswith('profile:'):
            settings = compare_api.profile_connection(self.store, ref[len('profile:'):])
            settings = self.store.connection_with_saved_password(settings)
            return self.capture(settings), {'kind': 'live'}
        if self.area == 'config':
            if ref.startswith('file:') or ref.endswith('.json'):
                path = ref[5:] if ref.startswith('file:') else ref
                with open(path, 'rb') as handle:
                    data = handle.read(param_compare.MAX_FILE_BYTES + 1)
                if len(data) > param_compare.MAX_FILE_BYTES:
                    raise ValueError('Read a parameter-snapshot-1.0 JSON file of at most 8 MiB.')
                return compare_api.parse_file(data.decode('utf-8')), {'kind': 'file', 'file': os.path.basename(path)}
            if ref.startswith('snapshot:') or ref == 'latest':
                item = self.store.parameter_snapshot_find(ref[9:] if ref.startswith('snapshot:') else ref, source)
                return item['snapshot'], dict(kind='snapshot', **{k: item[k] for k in ('id', 'number', 'label', 'saved_at')})
        raise ValueError('Choose live or profile:NAME%s.' %
                         (', snapshot:ID, latest or file:PATH' if self.area == 'config' else ''))

    def safe_error(self, exc):
        message = str(exc)
        for password in self.passwords:
            message = message.replace(password, '[redacted]')
        return message

    def pair(self):
        refs = [self.args.source, self.args.target]
        if refs[0] == refs[1] and refs[0] in ('live', 'latest', 'snapshot:latest'):
            raise ValueError('Choose two distinct sides; use profile:NAME for another server.')
        result = [None, None]
        order = [1, 0] if refs[0] in ('latest', 'snapshot:latest') else [0, 1]
        for i in order:
            other = result[1 - i]
            endpoint = param_compare.source_key(other[0]) if other and other[0].get('server') else None
            try:
                if refs[i] in ('latest', 'snapshot:latest') and other and not endpoint:
                    raise ValueError('Choose snapshot:ID because the other side could not identify its server.')
                result[i] = self.side(refs[i], endpoint)
            except (ValueError, OSError, ConnectorError) as exc:
                result[i] = param_compare.failed_side(self.safe_error(exc)), {'kind': 'unavailable'}
        return result


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def format_compare(report, show_all=False):
    parameter = report['schema_version'] == param_compare.COMPARE_SCHEMA
    rows = []
    for item in report['items']:
        if parameter and not show_all and (item['status'] == 'identical' or item['identity']):
            continue
        if parameter:
            rows.append([item['name'], item['status_text'],
                         str(item['source'].get('value')) if item['source'] else '',
                         str(item['target'].get('value')) if item['target'] else ''])
        else:
            rows.append([item['account'], item['status_text'], item.get('reason', '')])
    text = table_text(['Parameter', 'Status', 'Source', 'Target'] if parameter else ['Account', 'Status', 'Reason'], rows) + '\n'
    text += ', '.join('%s: %d' % (param_compare.STATUS_TEXT[s], n) for s, n in report['counts'].items()) + '\n'
    if parameter and not show_all:
        text += 'Identical and identity parameters are hidden. Use --all to show them.\n'
    for item in report['items']:
        if item.get('reason') and (not parameter or show_all or not item.get('identity')):
            text += '%s: %s\n' % (item.get('account', item.get('name')), item['reason'])
        if not parameter and item.get('differences') and item['status'] == 'different':
            changes = item['differences']
            for field in ('grants', 'roles', 'default_roles'):
                for direction in ('only_in_source', 'only_in_target'):
                    for value in changes[field][direction]:
                        text += '%s %s %s: %s\n' % (item['account'], field, param_compare.STATUS_TEXT[direction], value)
            for value in changes['attributes']:
                text += '%s %s: source %s; target %s\n' % (item['account'], value['name'], value['source'], value['target'])
    text += ''.join('Note: %s\n' % n for n in report['notes'])
    return text


def command(area, action, argv):
    parser = _parser(area, action)
    args = parser.parse_args(argv)
    if action == 'snapshot' and not args.operation:
        parser.error('Choose save, list, show or delete; see: myflames config snapshot --help')
    sources = Sources(args, area)
    report = None
    try:
        if action == 'snapshot':
            if args.operation == 'save':
                if len(args.label) > 200:
                    raise ValueError('Use a snapshot label of at most 200 characters.')
                snapshot, _ = sources.side('live')
                result = sources.store.parameter_snapshot_save(snapshot, args.label)
            elif args.operation == 'list':
                result = sources.store.parameter_snapshot_list()
            else:
                result = sources.store.parameter_snapshot_find(args.snapshot)
                if args.operation == 'delete':
                    if not args.yes:
                        if not sys.stdin.isatty():
                            raise ValueError('Deleting without a terminal requires --yes.')
                        sys.stderr.write('Delete parameter snapshot %s? [y/N] ' % result['id'])
                        sys.stderr.flush()
                        if input().strip().lower() not in ('y', 'yes'):
                            raise ValueError('Not deleted.')
                    result = sources.store.parameter_snapshot_delete(result['id'])
            if getattr(args, 'json', False):
                text = _json(result['snapshot'] if args.operation == 'show' else result)
            elif args.operation == 'show':
                data = result['snapshot']
                server = data['server']
                text = 'Parameter snapshot %s: %s\nServer: %s:%s (%s %s)\nCaptured: %s\n' % (
                    result['number'], result['label'], server['host'], server['port'], server.get('server_product') or '',
                    server.get('server_version') or '', data['captured_at'])
                text += table_text(['Parameter', 'Value', 'Default', 'Dynamic', 'Source'],
                                   [[i['name'], str(i.get('value')), str(i.get('default')), str(i.get('dynamic')),
                                     i.get('source') or ''] for i in data['items']]) + '\n'
                text += ''.join('Note: %s\n' % n for n in data['notes'])
            elif args.operation == 'list':
                text = table_text(['ID', 'Number', 'Label', 'Server', 'Saved'], [[i['id'], str(i['number']), i['label'],
                       '%s:%s' % (i['server']['host'], i['server']['port']), i['saved_at']] for i in result['items']]) + '\n'
            else:
                text = ('Deleted parameter snapshot %s.\n' % result['id'] if args.operation == 'delete' else
                        'Saved parameter snapshot %s, number %s.\n' % (result['id'], result['number']))
        elif action == 'export':
            value, _ = sources.side(args.side)
            text = account_compare.export_sql(value, args.account)
        else:
            pair = sources.pair()
            module = account_compare if area == 'accounts' else param_compare
            report = module.compare(pair[0][0], pair[1][0], pair[0][1], pair[1][1])
            text = _json(report) if args.json else format_compare(report, getattr(args, 'all', False))
        workspace._emit_text(text, getattr(args, 'output', None), quiet=args.quiet)
    except (KeyboardInterrupt, EOFError):
        _fail('Cancelled.')
    except (ValueError, TypeError, OSError, ConnectorError, sqlite3.Error) as exc:
        _fail(sources.safe_error(exc))
    if report:
        if report['source'].get('error') or report['target'].get('error') or report['counts']['not_compared']:
            raise SystemExit(2)
        different = any(report['counts'][s] for s in ('different', 'only_in_source', 'only_in_target'))
        if args.fail_on and different:
            if not args.quiet:
                sys.stderr.write('Differences found (--fail-on different).\n')
            raise SystemExit(1)
