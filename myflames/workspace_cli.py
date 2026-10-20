"""CLI access to the same query capture and exploration used by the workspace."""
import argparse
import getpass
import json
import math
import sys

from . import __version__
from .cli import _fail
from .connector import terminal_text
from .live import LiveJob
# Compatibility export for one release.
from .text_format import _table


def _bounded_integer(lower, upper):
    def parse(value):
        try:
            number = int(value)
        except (ValueError, TypeError):
            raise argparse.ArgumentTypeError('Choose an integer from %d to %d.' % (lower, upper))
        if not lower <= number <= upper:
            raise argparse.ArgumentTypeError('Choose an integer from %d to %d.' % (lower, upper))
        return number
    return parse


def _parameters(value):
    def finite_number(text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError('Numbers must be finite.')
        return number
    try:
        result = json.loads(value, parse_constant=finite_number, parse_float=finite_number)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('Pass --parameters as a valid JSON object with finite numbers.')
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError('Pass --parameters as a JSON object, for example \'{"id": 1}\'.')
    return result


def _emit_text(text, output_path, quiet=False):
    """Write a server report (text or JSON) to stdout or -o, with terminal
    control characters escaped (connector.terminal_text). Server strings
    such as another session's SQL can carry them; JSON values are unchanged."""
    from .cli import _write_output
    _write_output(terminal_text(text), output_path, quiet=quiet)


def _add_connection_args(parser, client_paths=False):
    """The connection flags shared by capture, top and running (mysql-CLI style)."""
    # host/port default to None so explicit flags can override a --profile;
    # connection_options() applies 127.0.0.1:3306 when neither sets them.
    parser.add_argument('--profile', metavar='NAME_OR_ID', help='Use a connection profile saved in the workspace, with its saved password; other flags override its fields.')
    parser.add_argument('-h', '--host', help='Connect to this server (default: 127.0.0.1).')
    parser.add_argument('-P', '--port', type=_bounded_integer(1, 65535), help='Connect to this port (default: 3306).')
    parser.add_argument('-u', '--user', help='Connect with this username.')
    parser.add_argument('-p', '--password', nargs='?', const='__PROMPT__', metavar='PASSWORD', help="Use '-p' alone to prompt for a password, or '-psecret' inline.")
    parser.add_argument('-D', '--database', help='Use this default database.')
    parser.add_argument('--ssl-mode', choices=('DISABLED', 'PREFERRED', 'REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'), help='Choose the connection TLS mode.')
    parser.add_argument('--ssl-ca', metavar='PATH', help='Verify the server with this CA bundle.')
    # Other CLI modules still use live.connection_options directly. Opt in
    # only at callers that preserve these fields, so no accepted flag is lost.
    if client_paths:
        parser.add_argument('--ssl-cert', metavar='PATH', help='Authenticate with this TLS client certificate.')
        parser.add_argument('--ssl-key', metavar='PATH', help='Use this TLS client private key.')
        parser.add_argument('--mysql-binary', metavar='PATH', help='Use this mysql/mariadb client executable.')


def _connection_settings(args, password):
    return {key: value for key, value in {
        'host': args.host, 'port': args.port, 'user': args.user,
        'password': password, 'database': args.database,
        'ssl_mode': getattr(args, 'ssl_mode', None), 'ssl_ca': getattr(args, 'ssl_ca', None),
        'ssl_cert': getattr(args, 'ssl_cert', None), 'ssl_key': getattr(args, 'ssl_key', None),
        'binary': getattr(args, 'mysql_binary', None),
    }.items() if value is not None}


def _connection_options(settings):
    """Keep CLI-only client options when using the workspace's live engine.

    live.connection_options validates the shared API fields. These extra
    client paths are CLI inputs, not additions to any capture/JSON schema.
    """
    from .live import connection_options
    options = connection_options(settings)
    for key in ('ssl_cert', 'ssl_key', 'binary'):
        value = settings.get(key)
        if value is not None:
            if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in '\n\r\x00'):
                raise ValueError('Invalid connection field: ' + key)
            options[key] = value
    return options


class _SingleSchema(argparse.Action):
    """Reject repeated filters while a command's backend accepts one schema."""
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error('--schema accepts one schema for this command; run it separately for each schema')
        setattr(namespace, self.dest, value)


def _resolve_connection(args):
    """Connection settings from --profile (and its saved password), explicit
    flags, and the -p prompt. Explicit flags win over profile fields."""
    password = args.password
    if password == '__PROMPT__':
        password = getpass.getpass('Enter password: ')
    explicit = _connection_settings(args, password)
    if not getattr(args, 'profile', None):
        explicit.setdefault('host', '127.0.0.1')
        explicit.setdefault('port', 3306)
        return explicit
    import sqlite3
    from .workspace_store import WorkspaceStore, PROFILE_FIELDS
    store = WorkspaceStore()
    try:
        profile = store.find_profile(args.profile)
        settings = {f: profile[f] for f in PROFILE_FIELDS if f != 'name' and profile.get(f) not in (None, '')}
        settings.update(explicit)
        if password is None and profile.get('remember_password'):
            settings['profile_id'] = profile['id']
            settings = store.connection_with_saved_password(settings)
            settings.pop('profile_id', None)
    except sqlite3.Error as exc:
        raise ValueError('Cannot read the workspace profiles in %s: %s' % (store.path, exc))
    settings.setdefault('host', '127.0.0.1')
    settings.setdefault('port', 3306)
    return settings


def _digest_arg(value):
    import re
    if not re.match(r'^[0-9a-fA-F]{8,64}$', value or ''):
        raise argparse.ArgumentTypeError('Pass a statement digest: 8 to 64 hexadecimal characters (myflames top lists them).')
    return value.lower()


_DIGEST_REASONS = {
    'normalized': 'the server records only normalized text (MariaDB, and MySQL before 8.0.3, keep no sample statement)',
    'truncated': 'its sample statement was cut off at performance_schema_max_sql_text_length',
    'placeholders': 'its sample statement has ? markers (a prepared statement)',
}


def _digest_capture(args, connection, password):
    """Resolve --statement-digest: (query, connection, source), or exit 2 with the
    parameterized SQL when the server kept no runnable sample statement."""
    import shlex
    from .connector import MySQLConnection
    from .discover import digest_capture_problem, digest_query, find_statement, short_digest
    options = _connection_options(connection)
    with MySQLConnection(**options) as conn:
        found = find_statement(conn, args.digest, connection.get('database'))
    statement = found['statement']
    label = 'Digest %s%s' % (short_digest(statement['digest']),
                             ' (schema %s)' % statement['schema'] if statement['schema'] else '')
    query = digest_query(statement)
    problem = digest_capture_problem(query)
    if problem:
        _fail('%s cannot be captured: %s' % (label, problem), password)
    schema = connection.get('database') or statement['schema']
    if query['reason']:
        if query['reason'] == 'missing':
            _fail('%s has no statement text to capture.' % label, password)
        params = '{%s}' % ', '.join('"%s": null' % n for n in query['names'])
        lines = ['myflames: %s has no runnable sample statement: %s. Its literal values are '
                 'unknown, so fill in each parameter and capture it with -e and --parameters '
                 '(plus your connection flags):' % (label, _DIGEST_REASONS[query['reason']]),
                 '  myflames capture%s -e %s --parameters %s' % (
                     ' -D ' + shlex.quote(schema) if schema else '',
                     shlex.quote(' '.join(query['sql'].split())), shlex.quote(params))]
        for name in query['lists']:
            lines.append('note: %s replaces a collapsed list of values; bind one value or write '
                         'the list into the SQL.' % name)
        text = '\n'.join(lines)
        if password:
            text = text.replace(password, '[redacted]')
        sys.stderr.write(terminal_text(text) + '\n')
        raise SystemExit(2)
    if schema and not connection.get('database'):
        connection = dict(connection, database=schema)
    source = {'kind': 'digest', 'digest': statement['digest'], 'schema': statement['schema']}
    return query['sql'], connection, source


def cmd_capture(argv):
    """Capture query evidence as JSON, with explicit execution opt-in."""
    from .connector import ConnectorError
    parser = argparse.ArgumentParser(
        prog='myflames capture', add_help=False,
        description='Capture a query plan, server context, and optional execution evidence as JSON.',
        epilog='Explain estimates the plan. --mode analyze executes the SELECT; --repeat repeats execution. '
               '--statement-digest captures a statement found with myflames top: its sample text runs in its '
               'recorded schema (unless -D or the profile sets one), and the capture records '
               'source.kind "digest". Without a sample (MariaDB, or a sample cut off) it exits 2 '
               'and prints the SQL with :p1, :p2 parameters for -e and --parameters. '
               'Passwords are not saved in captures. Exit codes: 0 success, 2 bad input or failed capture.',
    )
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    statement = parser.add_mutually_exclusive_group(required=True)
    statement.add_argument('-e', '--execute', metavar='SQL', help='Capture one SELECT, optionally using :named parameters.')
    statement.add_argument('--statement-digest', '--digest', dest='digest', type=_digest_arg, metavar='HASH', help='Capture this recorded statement digest (full or 8+ character prefix, from myflames top); --digest is a deprecated alias.')
    _add_connection_args(parser, client_paths=True)
    parser.add_argument('--mode', choices=('explain', 'analyze'), default='explain', help='Estimate the plan, or execute it with analyze (default: explain).')
    parser.add_argument('--repeat', type=_bounded_integer(1, 10), default=1, metavar='N', help='Measure 1–10 executions in analyze mode (default: 1).')
    parser.add_argument('--timeout', type=_bounded_integer(1, 300), default=30, metavar='SECONDS', help='Cancel each run after 1–300 seconds (default: 30).')
    parser.add_argument('--trace', action='store_true', help='Collect the optimizer trace when the server permits it.')
    parser.add_argument('--parameters', type=_parameters, default={}, metavar='JSON', help='Bind :named values from a JSON object.')
    parser.add_argument('--conditions', default='', metavar='NOTE', help='Record execution conditions alongside the measurements.')
    parser.add_argument('-o', '--output', metavar='PATH', help='Write capture JSON to this file instead of stdout.')
    from .cli import _add_quiet_flag, _warn_alias
    _add_quiet_flag(parser)
    args = parser.parse_args(argv)
    if any(a == '--digest' or a.startswith('--digest=') for a in argv):
        _warn_alias(args, "warning: 'capture --digest' is deprecated; use '--statement-digest'.\n")
    password = None if args.password == '__PROMPT__' else args.password
    job = None
    try:
        connection = _resolve_connection(args)
        password = connection.get('password')
        query, source = args.execute, None
        if args.digest:
            query, connection, source = _digest_capture(args, connection, password)
        payload = {
            'connection': connection,
            'query': query, 'mode': args.mode, 'repeat': args.repeat,
            'timeout': args.timeout, 'trace': args.trace, 'parameters': args.parameters,
            'conditions': args.conditions, 'source': source,
        }
        job = LiveJob(payload)
        # LiveJob deliberately validates API fields only. Preserve CLI client
        # paths without changing the capture schema or the UI API contract.
        options = _connection_options(connection)
        for key in ('ssl_cert', 'ssl_key', 'binary'):
            if key in options:
                job.options[key] = options[key]
        job.run()
        if job.state != 'complete' or not isinstance(job.result, dict):
            _fail(job.error or 'Capture did not complete.', password)
        result = dict(job.result)
        result.setdefault('schema_version', 'capture-1.0')
        result.setdefault('myflames_version', __version__)
        _emit_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n', args.output, quiet=args.quiet)
        _notes([w for w in result.get('warnings') or () if isinstance(w, str) and w.startswith('myflames supports ')],
               args.quiet)
    except KeyboardInterrupt:
        if job is not None:
            job.cancel()
        _fail('Capture cancelled.', password)
    except EOFError:
        _fail('Password prompt cancelled.', password)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        _fail(exc, password)


def cmd_explore(argv):
    """Export an interactive plan projection with stable operator identity."""
    from .cli import _read_plan_source, _parse_explain_input, _write_output, _add_quiet_flag, _warn_alias
    from .exploration import VIEWS, METRICS, render_exploration
    from .parser import flatten_nodes
    parser = argparse.ArgumentParser(
        prog='myflames explore',
        description='Export an interactive plan with operator selection, branch focus, and a metric list.',
    )
    parser.add_argument('input', metavar='PLAN', help="Read EXPLAIN JSON from a file, or '-' for stdin.")
    parser.add_argument('--type', choices=VIEWS + ('treemap',), default='workbench', help="Choose the chart (default: workbench / Visual Explain; diagram is an alias; 'treemap' is deprecated and renders the bar chart).")
    parser.add_argument('--metric', choices=tuple(METRICS), default='self_time', help='Choose the metric list; native chart encodings stay unchanged.')
    parser.add_argument('--focus', default='', metavar='NODE_ID', help='Show only this operator and its descendants.')
    parser.add_argument('--collapse', action='append', default=[], metavar='NODE_ID', help='Hide descendants of this operator; repeat for multiple branches.')
    parser.add_argument('--selected', default='', metavar='NODE_ID', help='Select this canonical operator ID when the report opens.')
    parser.add_argument('-o', '--output', metavar='PATH', help='Write interactive HTML to this file instead of stdout.')
    _add_quiet_flag(parser)
    args = parser.parse_args(argv)
    if args.type == 'treemap':
        _warn_alias(args, "warning: '--type treemap' is deprecated; the treemap view was removed. Rendering the bar chart instead (--type bargraph).\n")
        args.type = 'bargraph'
    root = _parse_explain_input(_read_plan_source(args.input, quiet=args.quiet)[0])
    try:
        if args.selected and args.selected not in {node['node_id'] for node in flatten_nodes(root)}:
            raise ValueError('Unknown selected operator. Use a node_id from the plan sidecar.')
        output = render_exploration(root, args.type, selected_id=args.selected,
                                    focus_id=args.focus, collapsed_ids=args.collapse,
                                    metric=args.metric)
        _write_output(output, args.output, quiet=args.quiet)
    except (ValueError, TypeError, OSError) as exc:
        _fail(exc)


def _compact(text, width):
    text = terminal_text(' '.join((text or '').split()))
    return text if len(text) <= width else text[:width - 1] + '…'


def _duration(ms):
    if ms is None:
        return '—'
    if ms >= 1000:
        return '%.2f s' % (ms / 1000.0)
    if ms >= 1:
        return '%.1f ms' % ms
    return '%.0f µs' % (ms * 1000)


def _count(value):
    if value is None:
        return '—'
    for limit, suffix in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k')):
        if value >= limit * 10:
            return '%.0f%s' % (value / limit, suffix)
    return '{:,}'.format(int(value))


def _discover_parser(name, description, epilog):
    parser = argparse.ArgumentParser(prog='myflames ' + name, add_help=False,
                                     description=description, epilog=epilog)
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    _add_connection_args(parser, client_paths=True)
    return parser


def _add_output_args(parser, what):
    parser.add_argument('--json', action='store_true', help='Emit %s as machine-readable JSON.' % what)
    parser.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
    parser.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; rely on stdout and the exit code.')


def _discover_run(args, work, query_timeout=12):
    """Open one connection, run *work(conn)*, and map failures to exit 2."""
    from .connector import ConnectorError, MySQLConnection
    password = None if args.password == '__PROMPT__' else args.password
    try:
        settings = _resolve_connection(args)
        password = settings.get('password')
        options = _connection_options(settings)
        options['query_timeout'] = query_timeout
        with MySQLConnection(**options) as conn:
            result = work(conn)
            _version_note(conn, getattr(args, 'quiet', False), result)
            return result
    except KeyboardInterrupt:
        _fail('Cancelled.', password)
    except EOFError:
        _fail('Password prompt cancelled.', password)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        _fail(exc, password)


def _version_note(conn, quiet, report=None):
    """Note on stderr when the server is below the supported floor. Uses the
    version the command already read; never sends a query of its own."""
    from .server_info import unsupported_version_note
    version = getattr(conn, '_version_cache', None)
    comment = getattr(conn, '_version_comment_cache', None) or ''
    if not version and isinstance(report, dict):
        # Most readers use SHOW GLOBAL VARIABLES rather than the connection's
        # cached @@version. Privileges calls this field version.
        version = report.get('server_version') or report.get('version')
        comment = report.get('engine') or ''
    if version:
        note = unsupported_version_note(version, comment)
        if note:
            _notes([note], quiet)


def _notes(notes, quiet):
    if not quiet:
        for note in notes:
            sys.stderr.write('note: %s\n' % terminal_text(note))


def cmd_top(argv):
    """List the heaviest statements recorded by Performance Schema."""
    from .discover import ORDERS, short_digest, top_report
    parser = _discover_parser(
        'top', 'List the heaviest normalized statements from performance_schema statement digests.',
        'Read-only. Needs Performance Schema (off by default on MariaDB) and SELECT on '
        'performance_schema. Pick a row, then run: myflames capture --statement-digest DIGEST '
        '(or -e "<SQL>"). --order full_join, scan and no_index list the statements behind Health\'s '
        '"Joins without an index" and "Full scans of the first table" and reads without an index; '
        '--table SCHEMA.TABLE lists statements that read some table without an index and whose digest '
        'text names that table (a text match, not proof). '
        'Exit codes: 0 success, 2 bad input, connection failure, or Performance Schema unavailable.')
    parser.add_argument('--limit', type=_bounded_integer(1, 200), default=20, metavar='N', help='Show the top 1–200 statements (default: 20).')
    parser.add_argument('--schema', metavar='NAME', action=_SingleSchema, help='Show only statements run with this default schema.')
    parser.add_argument('--order', choices=tuple(ORDERS), default='total',
                        help='Rank by total time, average time, rows examined, call count, joins without an index '
                             '(full_join, with range checks), first-table full scans (scan), or executions that '
                             'read a table without an index (no_index); the last three list only statements where '
                             'that count is above zero (default: total).')
    parser.add_argument('--table', metavar='SCHEMA.TABLE',
                        help='Show only statements that read a table without an index and whose digest text names '
                             'this table (a text match, not proof; backquote a part that contains a dot).')
    _add_output_args(parser, 'the ranked statements')
    args = parser.parse_args(argv)
    if args.table is not None:
        from .discover import parse_table_name
        try:
            parse_table_name(args.table)
        except ValueError as exc:
            parser.error(str(exc))
    report = _discover_run(args, lambda conn: top_report(conn, args.limit, args.schema, args.order, args.table))
    if args.json:
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
        return
    statements = report['statements']
    if not statements:
        empty = 'No statements recorded yet. Run the workload, then try again.'
        if args.table is not None or args.order in ('full_join', 'scan', 'no_index'):
            empty = 'No recorded statement matches.'
        _notes([empty], args.quiet)
    attribution = args.order in ('full_join', 'scan', 'no_index') or args.table is not None
    rows = []
    for rank, item in enumerate(statements, 1):
        text = item['sample_text'] or item['digest_text']
        row = [str(rank), _duration(item['total_ms']), _duration(item['avg_ms']),
               _count(item['count']), _count(item['rows_examined']), _count(item['rows_sent'])]
        if attribution:
            row += [_count(item['select_full_join']), _count(item['select_scan']), _count(item['no_index_used'])]
        if args.table is not None:
            row.append(item['table_match']['kind'])
        rows.append(row + [','.join(item['flags']) or '—', item['schema'] or '—',
                           short_digest(item['digest']) or '—', _compact(text, 72)])
    headers = ['>#', '>TOTAL', '>AVG', '>CALLS', '>EXAMINED', '>SENT']
    if attribution:
        headers += ['>FULL JOINS', '>SCANS', '>NO INDEX']
    if args.table is not None:
        headers.append('MATCH')
    _emit_text(_table(headers + ['FLAGS', 'SCHEMA', 'DIGEST', 'SQL'], rows), args.output, quiet=args.quiet)
    _notes(report['notes'], args.quiet)


def _explain_output(args):
    """--explain ID (sessions, and the deprecated running): the plan JSON of
    one running statement, or with --json the plan with its metadata."""
    from .discover import explain_connection
    result = _discover_run(args, lambda conn: explain_connection(conn, args.explain), query_timeout=40)
    if args.json:
        _emit_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
    else:
        _emit_text(result['raw'].rstrip('\n') + '\n', args.output, quiet=args.quiet)
    _notes(['Connection %d: %s' % (result['connection_id'], w) for w in result['warnings']], args.quiet)


def cmd_running(argv):
    """Deprecated alias of sessions: list statements running now, or print one running statement's plan."""
    from .discover import running_report
    parser = _discover_parser(
        'running', 'List statements running now, longest first, or print the plan of one of them.',
        'Read-only: never kills a session. Without PROCESS you see only your own sessions. '
        '--explain prints EXPLAIN JSON that pipes into myflames, for example: '
        'myflames running --explain 42 | myflames --output plan.html - . '
        'Deprecated: use myflames sessions (--min-seconds N, --explain ID). '
        'Exit codes: 0 success, 2 bad input, connection failure, or unsupported server.')
    parser.add_argument('--min-seconds', type=float, default=0, metavar='N', help='Show only statements running for at least N seconds (default: 0).')
    parser.add_argument('--explain', type=_bounded_integer(1, 2 ** 63 - 1), metavar='ID', help="Print the plan JSON of connection ID's running statement.")
    _add_output_args(parser, 'the statement list (or, with --explain, the plan with its metadata)')
    from .cli import _parse_alias_args
    args = _parse_alias_args(parser, argv, (
        "warning: 'running' is deprecated; use 'sessions' (myflames sessions --min-seconds N, "
        "or --explain ID). 'running' keeps its running-1.0 output until it is removed in a "
        "future release.\n"))
    if not 0 <= args.min_seconds <= 31536000 or args.min_seconds != args.min_seconds:
        parser.error('--min-seconds must be between 0 and 31536000')
    if args.explain is not None:
        _explain_output(args)
        return
    report = _discover_run(args, lambda conn: running_report(conn, args.min_seconds))
    if args.json:
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
        return
    statements = report['statements']
    if not statements:
        _notes(['No statements are running%s.' % (
            ' for at least %g seconds' % args.min_seconds if args.min_seconds else '')], args.quiet)
    rows = [[str(s['id']), '—' if s['time_s'] is None else '%.1f s' % s['time_s'], s['user'] or '—',
             s['db'] or '—', _compact(s['state'] or s['command'], 24), _compact(s['info'], 72) or '—']
            for s in statements]
    _emit_text(_table(['>ID', '>TIME', 'USER', 'DB', 'STATE', 'SQL'], rows), args.output, quiet=args.quiet)
    _notes(report['notes'] + ['Show a plan with: myflames sessions --explain ID'], args.quiet)


def _seconds_arg(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = -1
    if not 0 <= number <= 31536000 or number != number:
        raise argparse.ArgumentTypeError('Choose a number of seconds from 0 to 31536000.')
    return int(number) if number == int(number) else number


def cmd_sessions(argv):
    """Every connection, post-processed: summary, flagged sessions, the list; or one statement's plan."""
    from .sessions import DEFAULT_LIMIT, MAX_LIMIT, format_text, sessions_report
    from .locks import format_kill, kill_cli
    parser = _discover_parser(
        'sessions', 'Show every connection with its open InnoDB transaction: a summary (connections against '
                    'max_connections, counts by command, state, user, client host and database, the oldest '
                    'transaction), the flagged sessions (long-running, idle in transaction, waiting for a lock), '
                    'then the list; or print the plan of one running statement.',
        'Reads only, except --kill ID (an alias of myflames locks --kill), which ends one session after '
        'confirmation, follows the connection\'s write policy and records every attempt in the audit trail. '
        'Without PROCESS you '
        'see only your own sessions and no transactions. Statuses are rules of thumb: a statement running 10 s '
        'is watched, 60 s is a problem; the same for an open transaction. The summary always covers every '
        'session; the filters narrow only the list. --explain prints EXPLAIN JSON that pipes into myflames, '
        'for example: myflames sessions --explain 42 | myflames --output plan.html - . '
        'Replaces myflames running (deprecated). '
        'Exit codes: 0 success, 1 with --kill: a sent operation failed, '
        '2 nothing was sent, bad input, connection failure, or unsupported server.')
    parser.add_argument('--all', action='store_true',
                        help='List idle and system sessions too (default: only sessions running a statement, '
                             'idle in a transaction, or waiting for a lock).')
    parser.add_argument('--session-user', metavar='USER', help='List only sessions of this user.')
    parser.add_argument('--session-host', metavar='HOST', help='List only sessions from this client host (with or without the port).')
    parser.add_argument('--schema', metavar='NAME', action=_SingleSchema, help='List only sessions whose default database is NAME.')
    parser.add_argument('--min-seconds', type=_seconds_arg, default=0, metavar='N',
                        help='List only sessions in their current state for at least N seconds (default: 0).')
    parser.add_argument('--limit', type=_bounded_integer(1, MAX_LIMIT), default=DEFAULT_LIMIT, metavar='N',
                        help='List at most N sessions, 1–%d (default: %d).' % (MAX_LIMIT, DEFAULT_LIMIT))
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--explain', type=_bounded_integer(1, 2 ** 63 - 1), metavar='ID',
                        help="Print the plan JSON of connection ID's running statement.")
    action.add_argument('--kill', type=_bounded_integer(1, 2 ** 63 - 1), metavar='ID',
                        help='End connection ID after confirmation (alias of myflames locks --kill).')
    parser.add_argument('--query', action='store_true',
                        help='With --kill: use KILL QUERY; its transaction and locks stay unless in autocommit mode.')
    parser.add_argument('--yes', action='store_true',
                        help='With --kill: confirm without a prompt (required when stdin is not a terminal).')
    _add_output_args(parser, 'the sessions-1.0 report (with --kill, the kill-1.0 result; '
                            'with --explain, the plan with its metadata)')
    args = parser.parse_args(argv)
    if (args.query or args.yes) and args.kill is None:
        parser.error('--query and --yes need --kill ID')
    if args.kill is not None:
        code, result = _kill_command(args, kill_cli)
        text = json.dumps(result, indent=2, ensure_ascii=False) + '\n' if args.json else format_kill(result)
        _emit_text(text, args.output, quiet=args.quiet)
        raise SystemExit(code)
    if args.explain is not None:
        _explain_output(args)
        return
    report = _discover_run(args, lambda conn: sessions_report(
        conn, active_only=not args.all, user=args.session_user, host=args.session_host, db=args.schema,
        min_seconds=args.min_seconds, limit=args.limit))
    _emit_report(args, report, format_text)


def cmd_innodb(argv):
    """SHOW ENGINE INNODB STATUS, parsed into sections with statuses and plain-language summaries."""
    from .innodb_status import at_or_worse, format_text, innodb_report
    parser = _discover_parser(
        'innodb', 'Show SHOW ENGINE INNODB STATUS parsed into sections: semaphores, transactions and purge '
                  '(history list length, the oldest transaction), the latest deadlock and foreign key error, '
                  'file I/O, the change buffer and adaptive hash index, the redo log and checkpoint age, the '
                  'buffer pool, and row operations, each with a summary and ok/watch/problem statuses.',
        'Read-only; needs PROCESS. Each read restarts InnoDB\'s per-second averages for every reader. The raw '
        'text is shown only with --raw, for support cases. Statuses are rules of thumb from one snapshot; '
        '--json includes each rule. --fail-on turns the report into a gate for CI or cron. '
        'Exit codes: 0 success (nothing at the --fail-on level), 1 a section reached the --fail-on level, '
        '2 bad input, connection failure, or no PROCESS privilege.')
    parser.add_argument('--raw', action='store_true',
                        help='Print the raw status text instead of the report (with --json, add it as raw.text).')
    parser.add_argument('--fail-on', choices=('problem', 'watch'), metavar='LEVEL',
                        help="Exit 1 if any section is at LEVEL or worse: 'problem', or 'watch' (watch or problem).")
    _add_output_args(parser, 'the innodb-1.0 report')
    args = parser.parse_args(argv)
    report = _discover_run(args, lambda conn: innodb_report(conn, raw=args.raw), query_timeout=30)
    if not report['available']:
        _fail(report['error']['message'])
    if args.raw and not args.json:
        _emit_text(report['raw']['text'].strip('\n') + '\n', args.output, quiet=args.quiet)
    else:
        _emit_report(args, report, format_text)
    if args.fail_on:
        tripped = at_or_worse(report, args.fail_on)
        if not tripped:
            if not args.quiet:
                sys.stderr.write('OK: no section at --fail-on %s\n' % args.fail_on)
            return
        if not args.quiet:
            sys.stderr.write('FAIL: %d finding(s) at --fail-on %s or worse:\n' % (len(tripped), args.fail_on))
            for item in tripped:
                sys.stderr.write(terminal_text('  [%s] %s: %s %s\n' % (item['status'], item['title'], item['label'], item['text'])))
        raise SystemExit(1)


def cmd_error_log(argv):
    """Read the MySQL error log buffer or a verified local MariaDB log tail."""
    from .error_log import DEFAULT_LIMIT, MAX_LIMIT, at_or_worse, check_levels, error_log_report, format_text
    parser = _discover_parser(
        'error-log', 'Show the Error log: recent entries, levels and current-run error counts.',
        'Read-only. MySQL needs SELECT on performance_schema.error_log and a Performance Schema logging sink. '
        'MariaDB reads at most the last 1 MiB of a verified local error log file. '
        'Exit codes: 0 success, 1 the --fail-on threshold was reached, '
        '2 bad input, connection failure, or an unavailable error log (including with --fail-on).')
    parser.add_argument('--limit', type=_bounded_integer(1, MAX_LIMIT), default=DEFAULT_LIMIT, metavar='N',
                        help='List at most N entries, 1–%d (default: %d).' % (MAX_LIMIT, DEFAULT_LIMIT))
    parser.add_argument('--level', metavar='LEVEL[,LEVEL...]',
                        help='Include system,error,warning,note levels (case-insensitive; default: all).')
    parser.add_argument('--fail-on', choices=('problem', 'watch'), metavar='LEVEL',
                        help="Exit 1 at LEVEL or worse: 'watch' for current-run Error entries. "
                             "This view never reports 'problem'.")
    _add_output_args(parser, 'the error-log-1.0 report')
    args = parser.parse_args(argv)
    try:
        levels = check_levels(args.level)
    except ValueError as exc:
        parser.error(str(exc))
    report = _discover_run(args, lambda conn: error_log_report(conn, limit=args.limit, levels=levels), query_timeout=30)
    if not report['available']:
        # An unavailable read still has a useful machine-readable result.
        # Keep the documented exit 2 and the plain stderr diagnostic.
        if args.json:
            _emit_report(args, report, format_text)
        _fail(' '.join(text for text in (report['reason_text'], report['how_to_view']) if text))
    _emit_report(args, report, format_text)
    if args.fail_on:
        tripped = at_or_worse(report, args.fail_on)
        _notes(['%s: %s (--fail-on %s)' % ('FAIL' if tripped else 'OK', report['summary']['text'], args.fail_on)], args.quiet)
        if tripped:
            raise SystemExit(1)


def cmd_locks(argv):
    """Show who blocks whom: DDL waits, transactions, row and metadata locks, the latest deadlock."""
    from .locks import MAX_LIMIT, format_kill, format_text, kill_cli, lock_report
    parser = _discover_parser(
        'locks', 'Show lock waits as a blocking tree with the root blockers, DDL statements waiting '
                 'for a metadata lock with their blockers, then open transactions, row locks, '
                 'metadata lock waits, and the latest deadlock.',
        'Reads only, except --kill, which ends one session after showing it and asking you to '
        'confirm (needs CONNECTION_ADMIN or SUPER on MySQL, plus SYSTEM_USER to end a session of an '
        'account that has SYSTEM_USER; CONNECTION ADMIN on MariaDB, or SUPER before 11.0; not needed '
        'for sessions with your own user name). --kill follows the connection\'s write policy: a read-only '
        'profile refuses it before connecting, and every attempt is recorded (myflames audit). '
        'MySQL lists every row lock (performance_schema.data_locks); MariaDB lists only the '
        'locks involved in a wait. PROCESS is needed for transactions and the deadlock. On MySQL '
        'before 8.0.40, 8.1 to 8.4.2, and 9.0, reading the lock tables holds the global '
        'lock-system latch; --waits-only reads less, so it holds the latch for less time. '
        'Exit codes: 0 success (with --kill: ended), 1 with --kill: a sent operation failed, '
        '2 bad input or connection failure.')
    parser.add_argument('--limit', type=_bounded_integer(1, MAX_LIMIT), default=500, metavar='N',
                        help='Show at most N rows per section, 1–%d (default: 500).' % MAX_LIMIT)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--waits-only', action='store_true',
                       help='Show only sessions in a wait and the locks involved (skips the full lock list and the deadlock).')
    scope.add_argument('--deadlock', action='store_true',
                       help='Show only the latest detected deadlock, parsed and as raw text.')
    scope.add_argument('--kill', type=_bounded_integer(1, 2 ** 63 - 1), metavar='ID',
                       help='End connection ID with KILL CONNECTION (rolls back its open transaction) after '
                            'showing what it holds and blocks and asking you to confirm.')
    parser.add_argument('--query', action='store_true',
                        help='With --kill: use KILL QUERY, which stops only the running statement; its '
                             'transaction and locks stay unless it runs in autocommit mode.')
    parser.add_argument('--yes', action='store_true',
                        help='With --kill: confirm without a prompt (required when stdin is not a terminal).')
    _add_output_args(parser, 'the locks-1.1 report (with --kill, the kill-1.0 result)')
    args = parser.parse_args(argv)
    if (args.query or args.yes) and args.kill is None:
        parser.error('--query and --yes need --kill ID')
    if args.kill is not None:
        code, result = _kill_command(args, kill_cli)
        text = json.dumps(result, indent=2, ensure_ascii=False) + '\n' if args.json else format_kill(result)
        _emit_text(text, args.output, quiet=args.quiet)
        raise SystemExit(code)
    report = _discover_run(args, lambda conn: lock_report(
        conn, args.limit, waits_only=args.waits_only, deadlock_only=args.deadlock))
    if args.json:
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
        return
    _emit_text(format_text(report), args.output, quiet=args.quiet)
    sys.stdout.flush()
    _notes(report['notes'], args.quiet)


def _saved_profile(args):
    """The --profile record (without its password), or None. Exits 2 when absent."""
    if not getattr(args, 'profile', None):
        return None
    import sqlite3
    from .workspace_store import WorkspaceStore
    store = WorkspaceStore()
    try:
        return store.find_profile(args.profile)
    except sqlite3.Error as exc:
        _fail('Cannot read the workspace profiles in %s: %s' % (store.path, exc))
    except ValueError as exc:
        _fail(exc)


def _endpoint(settings):
    """Host, port and user for the audit trail; never the password."""
    return {k: settings.get(k) for k in ('host', 'port', 'user') if settings.get(k) not in (None, '')}


def _kill_command(args, kill_cli):
    """locks --kill: the connection's write policy first (a read-only
    connection refuses before any password prompt or connection), then the
    confirmed KILL, with an audit record for every attempt."""
    from . import dbops
    from .connector import ConnectorError, MySQLConnection
    from .locks import refused_result
    from .workspace_store import WorkspaceStore, PROFILE_FIELDS
    mode = 'query' if args.query else 'connection'
    statement = ('KILL QUERY %d' if mode == 'query' else 'KILL CONNECTION %d') % args.kill
    profile = _saved_profile(args)
    password = None if args.password == '__PROMPT__' else args.password
    planned = {f: profile[f] for f in PROFILE_FIELDS if profile and profile.get(f) not in (None, '')}
    planned.update(_connection_settings(args, None))
    planned.setdefault('host', '127.0.0.1')
    planned.setdefault('port', 3306)
    # A profile's type applies only while the flags keep its host, port, user and TLS settings.
    safety = dbops.connection_safety(profile, planned)
    decision = dbops.decide(dbops.classify(statement), safety['type'], safety['read_only'], safety['profile_name'])
    audit = dbops.AuditTrail(WorkspaceStore(), 'kill_query' if mode == 'query' else 'kill', 'cli',
                             connection=_endpoint(planned), safety=safety, password=password,
                             policy=dbops.policy_summary(decision))
    tagged = '/*myflames:kill*/ ' + statement
    if decision['action'] == 'refuse':
        result = refused_result(args.kill, mode, decision['text'])
        result['audit_id'] = audit.outcome([tagged], 'refused', outcome='read_only', message=decision['text'],
                                           target={'connection_id': args.kill, 'mode': mode})
        _fail(result['message'], password)
    try:
        settings = _resolve_connection(args)
        password = settings.get('password')
        audit.password = password
        audit.connection = _endpoint(settings)
        options = _connection_options(settings)
        options['query_timeout'] = 12
        with MySQLConnection(**options) as conn:
            code, result = kill_cli(conn, args.kill, mode, args.yes, args.quiet, audit=audit,
                                    policy_text=decision['text'])
            if code and result.get('statement') is None:
                _fail(result.get('message') or 'Nothing was sent.', password)
            return code, result
    except KeyboardInterrupt:
        sent = audit.pending
        if sent or audit.finished:
            audit.fail(KeyboardInterrupt(), [tagged])
        else:
            audit.outcome([tagged], 'declined', outcome='declined', message='Cancelled before sending.')
        _fail('Cancelled.' + (' The KILL may already have reached the server, so its outcome is unknown; check with: '
                              'myflames sessions --all' if sent else ''), password)
    except EOFError:
        audit.outcome([tagged], 'declined', outcome='declined', message='Password prompt cancelled.')
        _fail('Password prompt cancelled.', password)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        audit.fail(exc, [tagged])
        _fail(exc, password)


def cmd_health(argv):
    """Sample SHOW GLOBAL STATUS twice and report rates with rule-of-thumb statuses."""
    from .health import DEFAULT_INTERVAL, MAX_INTERVAL, MIN_INTERVAL, at_or_worse, format_text, health_report
    parser = _discover_parser(
        'health', 'Show how the server is doing now: diagnostics first (is the buffer pool full or '
                  'warming, does the redo log fit the write rate, table, thread and binary log caches, '
                  'open files), then throughput, connections, the InnoDB buffer pool, row locks, '
                  'temporary tables and sorts, the table cache, and redo/IO, as rates over a short '
                  'window with ok/watch/problem statuses.',
        'Read-only: two SHOW GLOBAL STATUS samples and a few SHOW GLOBAL VARIABLES; Performance '
        'Schema is not needed. Statuses are rules of thumb, not verdicts; --json includes each '
        'rule. --fail-on turns the report into a gate for CI or cron. '
        'Exit codes: 0 success (no diagnostic or metric at the --fail-on level), 1 a diagnostic '
        'or metric reached the --fail-on level, 2 bad input or connection failure.')
    parser.add_argument('--interval', type=_bounded_integer(MIN_INTERVAL, MAX_INTERVAL), default=DEFAULT_INTERVAL,
                        metavar='N', help='Wait N seconds between the two samples, %d–%d (default: %d).'
                        % (MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL))
    parser.add_argument('--fail-on', choices=('problem', 'watch'), metavar='LEVEL',
                        help="Exit 1 if any diagnostic or metric is at LEVEL or worse: 'problem', or 'watch' "
                             "(watch or problem).")
    _add_output_args(parser, 'the health-1.0 report')
    args = parser.parse_args(argv)
    report = _discover_run(args, lambda conn: health_report(conn, args.interval))
    if args.json:
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
                      args.output, quiet=args.quiet)
    else:
        _emit_text(format_text(report), args.output, quiet=args.quiet)
        sys.stdout.flush()
        _notes(report['notes'], args.quiet)
    if args.fail_on:
        tripped = at_or_worse(report, args.fail_on)
        if not tripped:
            if not args.quiet:
                sys.stderr.write('OK: no diagnostic or metric at --fail-on %s\n' % args.fail_on)
            return
        if not args.quiet:
            sys.stderr.write('FAIL: %d diagnostic(s) or metric(s) at --fail-on %s or worse:\n'
                             % (len(tripped), args.fail_on))
            for item in tripped:
                # A diagnostic has a title and a verdict; a metric has a label and a why.
                sys.stderr.write(terminal_text('  [%s] %s: %s\n' % (item['status'], item.get('title') or item['label'],
                                                                     item.get('verdict') or item['why'])))
        raise SystemExit(1)


def _emit_report(args, report, format_text):
    """--json or text to stdout (or -o), then the report's notes on stderr."""
    if args.json:
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
                      args.output, quiet=args.quiet)
        return
    _emit_text(format_text(report), args.output, quiet=args.quiet)
    sys.stdout.flush()
    _notes(report['notes'], args.quiet)


def cmd_waits(argv):
    """Top wait events over a short window, by class, and since server start."""
    from .perf_common import DEFAULT_WINDOW, MAX_WINDOW, MIN_WINDOW
    from .waits import DEFAULT_LIMIT, format_text, waits_report
    parser = _discover_parser(
        'waits', 'Show where server threads spent their waiting time: the top wait events over a short '
                 'window, grouped by class, the same since server start, and which wait classes are not '
                 'instrumented.',
        'Read-only: two reads of performance_schema.events_waits_summary_global_by_event_name. Never '
        'truncates a table or changes instruments; the statements that turn a class on are printed for '
        'you. Needs Performance Schema (off by default on MariaDB) and SELECT on performance_schema; when '
        'it is off, the report says how to turn it on. Wait time is summed across threads and classes '
        'overlap, so they are never added up. '
        'Exit codes: 0 success, 2 bad input or connection failure.')
    parser.add_argument('--interval', type=_bounded_integer(MIN_WINDOW, MAX_WINDOW), default=DEFAULT_WINDOW,
                        metavar='N', help='Wait N seconds between the two snapshots, %d–%d (default: %d).'
                        % (MIN_WINDOW, MAX_WINDOW, DEFAULT_WINDOW))
    parser.add_argument('--limit', type=_bounded_integer(1, 200), default=DEFAULT_LIMIT, metavar='N',
                        help='Show the top 1–200 events (default: %d).' % DEFAULT_LIMIT)
    _add_output_args(parser, 'the waits-1.0 report')
    args = parser.parse_args(argv)
    report = _discover_run(args, lambda conn: waits_report(conn, args.interval, args.limit))
    _emit_report(args, report, format_text)


def _add_schema_args(parser, default_note):
    """--schema NAME (repeatable) and --all-schemas, shared by indexes and tables.

    --all-schemas also lifts the 2,000-table limit for the chosen scope, so
    --schema big --all-schemas reads one schema that holds more tables (the
    API's all_schemas has the same meaning)."""
    parser.add_argument('--schema', metavar='NAME', action='append',
                        help='Read only this schema; repeat the flag to read several.')
    parser.add_argument('--all-schemas', action='store_true',
                        help='Read even when the scope holds more than 2,000 tables: every schema except mysql, '
                             'sys, performance_schema and information_schema, or with --schema only those '
                             '(%s).' % default_note)


def cmd_indexes(argv):
    """Unused and redundant indexes, and tables read mostly by full scans."""
    from .indexes import DEFAULT_LIMIT, format_text, index_report
    parser = _discover_parser(
        'indexes', 'Show unused indexes, redundant indexes, and tables read mostly without an index, for '
                   'one schema or the whole server.',
        'Read-only, one read when you run it. Usage comes from '
        'performance_schema.table_io_waits_summary_by_index_usage (since server start or the last change '
        'to a table\'s indexes), or on MariaDB with Performance Schema off and userstat=ON from '
        'information_schema.INDEX_STATISTICS. PRIMARY and UNIQUE indexes are never listed as unused; '
        'indexes that back a foreign key are marked keep. Redundant indexes need only '
        'information_schema. Without --schema every user schema is read, which is slow on servers with '
        'many tables (MariaDB opens every table); --schema is recommended and may be repeated. '
        'Exit codes: 0 success, 2 bad input or connection failure.')
    _add_schema_args(parser, 'the default without --schema; slow on servers with many tables')
    parser.add_argument('--limit', type=_bounded_integer(1, 5000), default=DEFAULT_LIMIT, metavar='N',
                        help='Show at most N rows per section, 1–5000 (default: %d).' % DEFAULT_LIMIT)
    _add_output_args(parser, 'the indexes-1.0 report')
    args = parser.parse_args(argv)
    if not args.schema and not args.all_schemas and not args.quiet:
        sys.stderr.write('note: no --schema, so every schema except the system ones is read. On servers with '
                         'many tables this is slow (MariaDB opens every table); pass --schema NAME (repeatable) '
                         'or --all-schemas.\n')
    report = _discover_run(args, lambda conn: index_report(conn, schemas=args.schema, limit=args.limit,
                                                                  require_choice_above=None if args.all_schemas else 2000),
                           query_timeout=60)
    _emit_report(args, report, format_text)


def cmd_tables(argv):
    """Tables without a primary key or a usable unique key, and InnoDB free space."""
    from .schema_health import DEFAULT_LIMIT, at_or_worse, format_text, table_report
    parser = _discover_parser(
        'tables', 'Show tables without a primary key or a usable unique key, and InnoDB tables whose '
                  'tablespace has free space a rebuild could return (fragmentation), for chosen schemas or '
                  'every user schema.',
        'Read-only: information_schema.TABLES, STATISTICS and COLUMNS, and with PROCESS '
        'INNODB_TABLESPACES (MySQL; plus SHOW CREATE TABLE when general tablespaces exist) or '
        'INNODB_SYS_TABLES (MariaDB) to tell file-per-table tablespaces from shared ones. Never runs OPTIMIZE TABLE or ALTER TABLE: the statements are printed for you, and '
        '--json adds the checks to run first and the caveats. Statuses are rules of thumb. --fail-on turns '
        'the report into a gate for CI or cron. '
        'Exit codes: 0 success (no table at the --fail-on level), 1 a table reached the --fail-on level, '
        '2 bad input or connection failure.')
    _add_schema_args(parser, 'the default')
    parser.add_argument('--fresh', action='store_true',
                        help='Read MySQL table statistics from InnoDB now instead of the information_schema cache '
                             '(sets information_schema_stats_expiry = 0 for this connection only; slower on many '
                             'tables). MariaDB always reads them now.')
    parser.add_argument('--limit', type=_bounded_integer(1, 5000), default=DEFAULT_LIMIT, metavar='N',
                        help='Show at most N rows per section, 1–5000 (default: %d).' % DEFAULT_LIMIT)
    parser.add_argument('--fail-on', choices=('problem', 'watch'), metavar='LEVEL',
                        help="Exit 1 if any table is at LEVEL or worse: 'problem', or 'watch' (watch or problem).")
    _add_output_args(parser, 'the schema-2.0 report')
    args = parser.parse_args(argv)
    report = _discover_run(args, lambda conn: table_report(conn, args.schema, args.fresh, args.limit,
                                                                  require_choice_above=None if args.all_schemas else 2000),
                           query_timeout=60)
    _emit_report(args, report, format_text)
    if args.fail_on:
        tripped = at_or_worse(report, args.fail_on)
        if not tripped:
            if not args.quiet:
                sys.stderr.write('OK: no table at --fail-on %s\n' % args.fail_on)
            return
        if not args.quiet:
            sys.stderr.write('FAIL: %d table finding(s) at --fail-on %s or worse:\n' % (len(tripped), args.fail_on))
            for item in tripped:
                sys.stderr.write(terminal_text('  [%s] %s: %s\n' % (item['status'], item['label'], item['why'])))
        raise SystemExit(1)


def cmd_io(argv):
    """Tables and files with the most I/O over a short window, and since server start."""
    from .io_stats import DEFAULT_LIMIT, format_text, io_report
    from .perf_common import DEFAULT_WINDOW, MAX_WINDOW, MIN_WINDOW
    parser = _discover_parser(
        'io', 'Show the tables and files with the most I/O over a short window (rows read and written per '
              'table, bytes and time per file), server-wide InnoDB I/O rates, and the same since server '
              'start.',
        'Read-only: two snapshots of SHOW GLOBAL STATUS and, with Performance Schema, '
        'table_io_waits_summary_by_table and the file summaries (on MariaDB with Performance Schema off '
        'and userstat=ON, information_schema.TABLE_STATISTICS). Reading per-table I/O costs time on '
        'servers with many tables; --schema limits it. '
        'Exit codes: 0 success, 2 bad input or connection failure.')
    parser.add_argument('--interval', type=_bounded_integer(MIN_WINDOW, MAX_WINDOW), default=DEFAULT_WINDOW,
                        metavar='N', help='Wait N seconds between the two snapshots, %d–%d (default: %d).'
                        % (MIN_WINDOW, MAX_WINDOW, DEFAULT_WINDOW))
    parser.add_argument('--schema', metavar='NAME', action=_SingleSchema, help='Show only tables in this schema (files stay server-wide).')
    parser.add_argument('--limit', type=_bounded_integer(1, 200), default=DEFAULT_LIMIT, metavar='N',
                        help='Show the top 1–200 tables and files (default: %d).' % DEFAULT_LIMIT)
    parser.add_argument('--all-instances', action='store_true',
                        help='Read per-table and per-file I/O even above 2,000 instances (still within the '
                             '30-second collection budget).')
    _add_output_args(parser, 'the io-1.0 report')
    args = parser.parse_args(argv)
    report = _discover_run(args, lambda conn: io_report(conn, args.interval, args.schema, args.limit,
                                                        all_instances=args.all_instances),
                           query_timeout=30)
    _emit_report(args, report, format_text)


def cmd_config(argv):
    """Review key server settings against engine defaults and rules of thumb."""
    if argv and argv[0] in ('set', 'reset-persist'):
        return _config_change(argv[0], argv[1:])
    from .config_review import at_or_worse, config_report, format_text
    parser = _discover_parser(
        'config', 'Review key server settings (buffer pool, redo, durability, connections, caches, temporary '
                  'tables, session buffers, Performance Schema, the slow query log) with the engine default, '
                  'where each value came from, and ok/watch/problem statuses.',
        'Read-only: SHOW GLOBAL VARIABLES and STATUS, performance_schema.variables_info (MySQL) or '
        'information_schema.SYSTEM_VARIABLES (MariaDB), and the InnoDB tablespace sizes (needs PROCESS). '
        'The review never changes a setting: each suggestion comes with why and a caveat. To change one, '
        'use myflames config set NAME VALUE (or reset-persist NAME); see myflames config set --help. '
        '--fail-on turns the report into a gate for CI or cron. '
        'Exit codes: 0 success (no check at the --fail-on level), 1 a check reached the --fail-on level, '
        '2 bad input or connection failure.')
    parser.add_argument('--fail-on', choices=('problem', 'watch'), metavar='LEVEL',
                        help="Exit 1 if any check is at LEVEL or worse: 'problem', or 'watch' (watch or problem).")
    _add_output_args(parser, 'the config-1.0 report')
    args = parser.parse_args(argv)
    report = _discover_run(args, config_report, query_timeout=30)
    _emit_report(args, report, format_text)
    if args.fail_on:
        tripped = at_or_worse(report, args.fail_on)
        if not tripped:
            if not args.quiet:
                sys.stderr.write('OK: no check at --fail-on %s\n' % args.fail_on)
            return
        if not args.quiet:
            sys.stderr.write('FAIL: %d check(s) at --fail-on %s or worse:\n' % (len(tripped), args.fail_on))
            for check in tripped:
                sys.stderr.write(terminal_text('  [%s] %s: %s\n' % (check['status'], check['label'], check['why'])))
        raise SystemExit(1)


def cmd_report(argv):
    """Full analysis: every Server view once, ranked findings, per-area sections, statements behind the numbers."""
    from .perf_common import ALL_SCHEMAS_TABLE_LIMIT
    from .report import DEFAULT_INTERVAL, MAX_INTERVAL, MIN_INTERVAL, analysis_report, at_or_worse, export
    parser = _discover_parser(
        'report', 'Run every Server view once and compose one report: the server, a ranked list of findings '
                  '(problem, watch, info) with their numbers, rule, why, caveat and a suggestion, sections for '
                  'memory, redo and durability, connections, temporary tables and sorts, caches, the query cache '
                  '(MariaDB), indexes, schema health, waits, I/O, InnoDB status, replication, security basics and '
                  'Performance Schema coverage, and the statements behind joins without an index and tables read '
                  'without an index.',
        'Read-only: it runs the health, waits, io, config, innodb, sessions, instruments, tables and indexes '
        'views (at most 3 at a time), reads SHOW REPLICA STATUS and mysql.user for account names (never '
        'password hashes), and Performance Schema statement digests. Nothing is changed and no suggestion is '
        'applied. A view that fails is listed and the rest of the report is kept. Without --schema, Indexes and '
        'Tables read every user schema up to %s tables and are skipped above that. Statuses are rules of thumb. '
        '--fail-on turns the report into a gate for CI or cron. '
        'Exit codes: 0 success (no finding at the --fail-on level), 1 a finding reached the --fail-on level, '
        '2 bad input or connection failure.' % '{:,}'.format(ALL_SCHEMAS_TABLE_LIMIT))
    parser.add_argument('--interval', type=_bounded_integer(MIN_INTERVAL, MAX_INTERVAL), default=DEFAULT_INTERVAL,
                        metavar='N', help='Sample Health, Waits and I/O over N seconds, %d–%d (default: %d).'
                        % (MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL))
    _add_schema_args(parser, 'with no table limit')
    form = parser.add_mutually_exclusive_group()
    form.add_argument('--json', action='store_true', help='Emit the report-1.0 report as machine-readable JSON.')
    form.add_argument('--markdown', action='store_true', help='Emit the report as Markdown, for a ticket or a wiki.')
    form.add_argument('--html', action='store_true', help='Emit the report as one static HTML page (no scripts).')
    form.add_argument('--digest', action='store_true',
                      help='Emit a compact, token-cheap text digest of the findings for an LLM.')
    parser.add_argument('--fail-on', choices=('problem', 'watch'), metavar='LEVEL',
                        help="Exit 1 if any finding is at LEVEL or worse: 'problem', or 'watch' (watch or problem).")
    parser.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
    parser.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; rely on stdout and the exit code.')
    args = parser.parse_args(argv)
    limit = None if args.all_schemas else ALL_SCHEMAS_TABLE_LIMIT
    report = _discover_run(args, lambda conn: analysis_report(conn, args.interval, schemas=args.schema,
                                                              require_choice_above=limit), query_timeout=60)
    views = report['header']['views']
    attempted = [view for view in views if view['status'] != 'skipped']
    failed = [view for view in attempted if view['status'] == 'failed']
    if failed and len(failed) == len(attempted):
        reasons = '; '.join('%s: %s' % (view['label'], view['error']) for view in failed)
        sys.stderr.write(terminal_text('error: Could not read the server: all report views failed: %s\n'
                                       % reasons))
        raise SystemExit(2)
    if failed:
        sys.stderr.write(terminal_text('warning: Report views failed: %s\n'
                                       % ', '.join(view['label'] for view in failed)))
    fmt = 'json' if args.json else 'markdown' if args.markdown else 'html' if args.html else \
        'digest' if args.digest else 'text'
    _emit_text(export(report, fmt), args.output, quiet=args.quiet)
    sys.stdout.flush()
    if fmt == 'text':
        _notes(report['notes'], args.quiet)
    if args.fail_on:
        tripped = at_or_worse(report, args.fail_on)
        if not tripped:
            if not args.quiet:
                sys.stderr.write('OK: no finding at --fail-on %s\n' % args.fail_on)
            return
        if not args.quiet:
            sys.stderr.write('FAIL: %d finding(s) at --fail-on %s or worse:\n' % (len(tripped), args.fail_on))
            for item in tripped:
                sys.stderr.write(terminal_text('  [%s] %s: %s\n' % (item['status'], item['title'], item['headline'])))
        raise SystemExit(1)


def cmd_instruments(argv):
    """Performance Schema instruments and consumers: state, gain, cost, and the statements."""
    from .ps_instruments import PRESET_KEYS, format_text, instruments_report, preset_text
    parser = _discover_parser(
        'instruments', 'Show which Performance Schema instruments and consumers are on, what each one gives '
                       'myflames and you, what it costs, and the statements that turn it on or off, with '
                       'ready-made presets.',
        'Read-only: reads setup_instruments, setup_consumers, setup_actors and setup_objects. Never runs an '
        'UPDATE: the runtime statements and my.cnf lines are printed for you to run (they need UPDATE on those '
        'performance_schema tables). --preset NAME prints one preset: minimal (what the myflames views read), '
        'locks (lock troubleshooting), waits (every wait instrument for a short investigation, with an undo '
        'statement), defaults (the engine\'s compiled-in defaults). '
        'Exit codes: 0 success, 2 bad input or connection failure.')
    parser.add_argument('--preset', choices=PRESET_KEYS, metavar='NAME',
                        help="Print only this preset's runtime SQL and my.cnf lines: %s." % ', '.join(PRESET_KEYS))
    _add_output_args(parser, 'the instruments-1.0 report (with --preset, that preset)')
    args = parser.parse_args(argv)
    report = _discover_run(args, instruments_report)
    if not args.preset:
        _emit_report(args, report, format_text)
        return
    if args.json:
        preset = next(p for p in report['presets'] if p['key'] == args.preset)
        preset = dict(preset, engine=report['engine'], server_version=report['server_version'],
                      performance_schema=report['performance_schema'])
        _emit_text(json.dumps(preset, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
                      args.output, quiet=args.quiet)
    else:
        _emit_text(preset_text(report, args.preset), args.output, quiet=args.quiet)


def cmd_profiles(argv):
    """List saved connection profiles, or forget/delete one and its saved password."""
    import sqlite3
    from . import secret_store
    from .workspace_store import WorkspaceStore
    parser = argparse.ArgumentParser(
        prog='myflames profiles',
        description='Manage the connection profiles saved by the workspace (myflames ui).',
        epilog='Profiles live in ~/.myflames/workspace.sqlite3 without passwords. A remembered password is '
               'kept in the OS credential store, or in an encrypted file when none is available. '
               'Each profile has a type (production asks before every write, development only before risky '
               'ones) and can be read-only (every write refused); set them with: myflames profiles set NAME '
               '--type development --read-only. '
               'Use a profile with: myflames capture --profile NAME -e "SELECT ...". '
               'Exit codes: 0 success, 2 bad input or unknown profile.')
    actions = parser.add_subparsers(dest='action', metavar='ACTION')
    actions.required = True
    listing = actions.add_parser('list', help='List saved profiles and where their passwords are kept.',
                                 description='List saved connection profiles. Never prints passwords.')
    _add_output_args(listing, 'the profiles')
    for name, text in (('forget', 'Delete the saved password; keep the profile.'),
                       ('delete', 'Delete the profile and its saved password.')):
        action = actions.add_parser(name, help=text, description=text)
        action.add_argument('profile', metavar='NAME_OR_ID', help='Choose the profile by name or ID.')
        action.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; rely on the exit code.')
    setting = actions.add_parser(
        'set', help='Set the connection type (production or development) and read-only mode.',
        description='Set how myflames guards writes on this connection. Production asks before every write; '
                    'development asks only before risky writes (DDL, DML without a key in WHERE, KILL, '
                    'SET GLOBAL or PERSIST, replication, CLONE). Read-only refuses every write, whatever the type. '
                    'New and existing connections are production and read-write until you change them.')
    setting.add_argument('profile', metavar='NAME_OR_ID', help='Choose the profile by name or ID.')
    setting.add_argument('--type', choices=('production', 'development'), dest='connection_type',
                         help='Set the connection type.')
    access = setting.add_mutually_exclusive_group()
    access.add_argument('--read-only', dest='read_only', action='store_const', const=True,
                        help='Refuse every write on this connection.')
    access.add_argument('--read-write', dest='read_only', action='store_const', const=False,
                        help='Allow writes again (each still follows the connection type).')
    setting.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; rely on the exit code.')
    args = parser.parse_args(argv)
    if args.action == 'set' and args.connection_type is None and args.read_only is None:
        setting.error('choose --type, --read-only or --read-write')
    store = WorkspaceStore()
    try:
        if args.action == 'list':
            profiles = store.list('profile')
            if args.json:
                result = {'schema_version': 'profiles-1.0', 'secret_store': secret_store.describe(),
                          'profiles': profiles}
                _emit_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
                return
            if not profiles:
                _notes(['No saved connection profiles. Save one in the workspace: myflames ui'], args.quiet)
            rows = [[p['name'], p.get('user') or '—', '%s:%s' % (p['host'], p['port']), p.get('database') or '—',
                     p['type'], 'read-only' if p['read_only'] else 'read-write',
                     ('saved (%s)' % p['secret_backend_label']) if p['has_saved_password'] else 'not saved', p['id']]
                    for p in profiles]
            _emit_text(_table(['NAME', 'USER', 'HOST', 'DATABASE', 'TYPE', 'WRITES', 'PASSWORD', 'ID'], rows),
                       args.output, quiet=args.quiet)
            return
        profile = store.find_profile(args.profile)
        if args.action == 'set':
            changed = store.set_profile_safety(profile['id'], args.connection_type, args.read_only)
            _notes(['Profile %r is now %s and %s.' % (changed['name'], changed['type'].capitalize(),
                                                      'read-only' if changed['read_only'] else 'read-write')],
                   args.quiet)
        elif args.action == 'forget':
            had = profile['has_saved_password']
            store.forget_password(profile['id'])
            _notes(['Forgot the saved password for %r.' % profile['name'] if had
                    else 'No password was saved for %r.' % profile['name']], args.quiet)
        else:
            store.delete('profile', profile['id'])
            _notes(['Deleted profile %r%s.' % (profile['name'], ' and its saved password' if profile['has_saved_password'] else '')],
                   args.quiet)
    except sqlite3.Error as exc:
        _fail('Cannot read the workspace profiles in %s: %s' % (store.path, exc))
    except (ValueError, OSError) as exc:
        _fail(exc)


def _days_arg(lower, upper):
    def parse(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = None
        if number is None or number != number or not lower <= number <= upper:
            raise argparse.ArgumentTypeError('Choose a number of days from %g to %g.' % (lower, upper))
        return int(number) if number == int(number) else number
    return parse


def _audit_when(stamp):
    """2026-10-14T10:00:00.123456+00:00 -> 2026-10-14 10:00:00Z."""
    return (stamp or '')[:19].replace('T', ' ') + ('Z' if stamp else '')


def cmd_audit(argv):
    """The local audit trail of server writes: list, export, purge."""
    import sqlite3
    from . import dbops
    from .workspace_store import WorkspaceStore
    if not argv or (argv[0].startswith('-') and argv[0] not in ('--help', '-h')):
        argv = ['list'] + list(argv)
    parser = argparse.ArgumentParser(
        prog='myflames audit',
        description='Show, export or purge the local audit trail of writes myflames sent to a server (or '
                    'refused, or you declined): who, when, which server, the exact statements with secrets '
                    'redacted, and the result.',
        epilog='Records live in ~/.myflames/workspace.sqlite3 (owner-only) and are kept for 90 days unless '
               'changed in the workspace Settings. A record whose status is pending has no result: myflames '
               'stopped before it could record whether the server ran the statement (outcome unknown). '
               'Passwords are never stored. Exit codes: 0 success, 1 purge declined or refused, 2 bad input.')
    actions = parser.add_subparsers(dest='action', metavar='ACTION')
    actions.required = True

    def filters(sub):
        sub.add_argument('--since', type=_days_arg(1, 36500), metavar='DAYS',
                         help='Show only records from the last DAYS days.')
        sub.add_argument('--server', metavar='NAME',
                         help='Show only records for this saved connection name, host, or host:port.')
        form = sub.add_mutually_exclusive_group()
        form.add_argument('--json', action='store_true', help='Emit the audit-1.0 records as machine-readable JSON.')
        form.add_argument('--csv', action='store_true', help='Emit the records as CSV, for a spreadsheet.')
        sub.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
        sub.add_argument('-q', '--quiet', action='store_true',
                         help='Suppress notes on stderr; rely on stdout and the exit code.')
    listing = actions.add_parser('list', help='List recent records, newest first (default).',
                                 description='List audit records, newest first.')
    filters(listing)
    listing.add_argument('--limit', type=_bounded_integer(1, 100000), default=50, metavar='N',
                         help='Show at most N records (default: 50).')
    export = actions.add_parser('export', help='Export every matching record (JSON by default, or --csv).',
                                description='Export every matching audit record with all its fields.')
    filters(export)
    purge = actions.add_parser('purge', help='Delete records older than --older-than DAYS.',
                               description='Delete audit records older than DAYS days (0 deletes every record).')
    purge.add_argument('--older-than', type=_days_arg(0, 36500), required=True, metavar='DAYS',
                       help='Delete records attempted more than DAYS days ago (0: all records).')
    purge.add_argument('--yes', action='store_true',
                       help='Delete without a prompt (required when stdin is not a terminal).')
    purge.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; rely on the exit code.')
    args = parser.parse_args(argv)
    store = WorkspaceStore()
    try:
        if args.action == 'purge':
            what = ('every audit record' if args.older_than == 0
                    else 'audit records older than %g days' % args.older_than)
            if not args.yes:
                if not sys.stdin.isatty():
                    sys.stderr.write('myflames: not deleted: stdin is not a terminal, so myflames cannot ask you to '
                                     'confirm. Re-run with --yes to delete %s.\n' % what)
                    raise SystemExit(1)
                sys.stderr.write('Delete %s from %s? [y/N] ' % (what, store.path))
                sys.stderr.flush()
                try:
                    answer = sys.stdin.readline()
                except (KeyboardInterrupt, EOFError):
                    answer = ''
                if answer.strip().lower() not in ('y', 'yes'):
                    sys.stderr.write('Nothing was deleted.\n')
                    raise SystemExit(1)
            count = store.audit_purge(args.older_than)
            _notes(['Deleted %d audit record%s.' % (count, '' if count == 1 else 's')], args.quiet)
            return
        if args.action == 'export':
            result = dbops.audit_export(store, 'csv' if args.csv else 'json', args.since, args.server)
            _emit_text(result['content'], args.output, quiet=args.quiet)
            return
        report = dbops.audit_report(store, args.since, args.server, args.limit)
        if args.json:
            _emit_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n', args.output,
                       quiet=args.quiet)
            return
        if args.csv:
            _emit_text(dbops.audit_csv(report['records']), args.output, quiet=args.quiet)
            return
        records = report['records']
        if not records:
            _notes(['No audit records%s. myflames records every write it sends, refuses, or you decline.'
                    % (' match' if (args.since or args.server) else ' yet')], args.quiet)
        rows = []
        for r in records:
            c = r['connection']
            server = c.get('profile_name') or ('%s:%s' % (c.get('host'), c.get('port')) if c.get('host') else '—')
            status = 'unknown' if r['outcome_unknown'] else r['status']
            if r.get('outcome') and r['outcome'] not in (r['status'], 'killed'):
                status += ' (%s)' % r['outcome']
            rows.append([_audit_when(r['attempted_at']), status, r['surface'], r['operation'],
                         _compact(server, 30), _compact(' ; '.join(r['statements']), 60) or '—', r['id'][:8]])
        _emit_text(_table(['WHEN (UTC)', 'STATUS', 'FROM', 'OPERATION', 'SERVER', 'STATEMENT', 'ID'], rows),
                   args.output, quiet=args.quiet)
        sys.stdout.flush()
        _notes(['Kept for %d days. Full records: myflames audit export (--csv for a spreadsheet).'
                % report['retention_days']], args.quiet)
    except sqlite3.Error as exc:
        _fail('Cannot read the audit trail in %s: %s' % (store.path, exc))
    except (ValueError, OSError) as exc:
        _fail(exc)


# ---- replication (status, skip one transaction, skip loop) ------------------------------

def _error_codes_arg(value):
    codes = []
    for part in (value or '').split(','):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= 99999:
            raise argparse.ArgumentTypeError('Pass error codes as numbers separated by commas, for example 1062,1032.')
        if int(part) not in codes:
            codes.append(int(part))
    if not codes:
        raise argparse.ArgumentTypeError('Choose at least one error code, for example 1062 (duplicate key).')
    return codes


def _replication_policy(args, operation):
    """The connection's write policy and an audit factory, before any
    password prompt or connection (a read-only connection refuses here)."""
    from . import dbops
    from .workspace_store import WorkspaceStore, PROFILE_FIELDS
    store = WorkspaceStore()
    try:
        profile = store.find_profile(args.profile) if getattr(args, 'profile', None) else None
    except Exception as exc:
        connection = _connection_settings(args, None)
        safety = dbops.connection_safety(None, connection)
        decision = dbops.decide('risky', safety['type'], safety['read_only'], safety['profile_name'])
        password = None if args.password == '__PROMPT__' else args.password
        dbops.AuditTrail(store, operation, 'cli', connection=_endpoint(connection), safety=safety,
                        password=password, policy=dbops.policy_summary(decision)).outcome(
                            [], 'refused', outcome='unsent', error=str(exc),
                            message='Nothing was sent: %s' % exc,
                            target={'channel': args.channel, 'profile': args.profile})
        _fail(exc, password)
    planned = {f: profile[f] for f in PROFILE_FIELDS if profile and profile.get(f) not in (None, '')}
    planned.update(_connection_settings(args, None))
    planned.setdefault('host', '127.0.0.1')
    planned.setdefault('port', 3306)
    safety = dbops.connection_safety(profile, planned)
    decision = dbops.decide(dbops.classify('START REPLICA SQL_THREAD'), safety['type'], safety['read_only'],
                            safety['profile_name'])
    state = {'password': None if args.password == '__PROMPT__' else args.password, 'endpoint': _endpoint(planned),
             'settings': planned}

    def decision_reader():
        # Resolve by ID so a rename cannot bind an active loop to a different
        # profile with the old name. Compare against the connection in use.
        current_profile = store.get('profile', profile['id']) if profile else None
        current = dbops.connection_safety(current_profile, state['settings'])
        return dbops.decide(dbops.classify('START REPLICA SQL_THREAD'), current['type'], current['read_only'],
                            current['profile_name'])

    def audit():
        return dbops.AuditTrail(store, operation, 'cli', connection=state['endpoint'], safety=safety,
                                password=state['password'], policy=dbops.policy_summary(decision))
    state['decision_reader'] = decision_reader
    return decision, audit, state


def _replication_connect(args, state, work):
    """Connect (saved password, -p prompt), run *work(conn)*; failures exit 2."""
    from .connector import ConnectorError, MySQLConnection

    def failed(exc):
        audit = state['audit']
        audit.password, audit.connection = state['password'], state['endpoint']
        if audit.finished:
            return
        if audit.pending:
            audit.fail(exc, state.get('statements', ()))
        else:
            text = str(exc) or 'Cancelled before sending.'
            cancelled = isinstance(exc, (KeyboardInterrupt, EOFError))
            audit.outcome(state.get('statements', []), 'declined' if cancelled else 'refused',
                          outcome='declined' if cancelled else 'unsent', error=text,
                          message='Nothing was sent: %s' % text, target=state['target'])

    try:
        settings = _resolve_connection(args)
        state['password'] = settings.get('password')
        state['endpoint'] = _endpoint(settings)
        state['settings'] = {k: v for k, v in settings.items() if k != 'password'}
        state['audit'].password, state['audit'].connection = state['password'], state['endpoint']
        options = _connection_options(settings)
        options['query_timeout'] = 30
        with MySQLConnection(**options) as conn:
            return work(conn)
    except KeyboardInterrupt as exc:
        failed(exc)
        _fail('Cancelled.', state['password'])
    except EOFError as exc:
        failed(exc)
        _fail('Password prompt cancelled.', state['password'])
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        failed(exc)
        _fail(exc, state['password'])


def _replication_confirm(prompt, assume_yes, quiet, description):
    """None when confirmed; else (audit status, outcome, message)."""
    from . import dbops
    return dbops.cli_confirm(description, prompt + ' [y/N] ', assume_yes=assume_yes, quiet=quiet)


def _replication_channel(report, name):
    from .replication import find_channel
    if not report.get('available'):
        raise ValueError(report.get('message'))
    if not report.get('replica'):
        raise ValueError(report.get('message') or 'This server is not a replica.')
    channel = find_channel(report, name)
    if channel is None:
        names = ', '.join(repr(c['name']) for c in report['channels'])
        raise ValueError('This server has no replication channel named %r (it has %s).' % (name, names))
    return channel


def cmd_replication(argv):
    """Replication status per channel; skip one failed transaction; skip until stopped."""
    if argv and argv[0] in ('skip', 'skip-loop'):
        return (_replication_skip if argv[0] == 'skip' else _replication_skip_loop)(argv[1:])
    from .replication import format_text, read_status
    parser = _discover_parser(
        'replication', 'Show every replication channel (MySQL) or connection (MariaDB): receiver and applier '
                       'threads, the last errors, positions or GTIDs, lag with its caveats, and ok/watch/problem. '
                       'A channel stopped on an error shows the exact statements that would skip that one '
                       'transaction.',
        'Read-only: SHOW REPLICA STATUS (SHOW SLAVE STATUS before MySQL 8.0.22; SHOW ALL SLAVES STATUS on '
        'MariaDB), the Performance Schema replication tables on MySQL, and the relay log at the applier position '
        'of a failed channel. Needs REPLICATION CLIENT or SUPER on MySQL, SLAVE MONITOR on MariaDB. '
        'Group Replication channels are listed but not judged. Writes: myflames replication skip (one '
        'transaction) and myflames replication skip-loop (skip chosen errors until stopped). '
        'Exit codes: 0 success, 2 bad input, missing privilege, or connection failure.')
    parser.add_argument('--channel', metavar='NAME',
                        help="Show only this channel (MySQL) or connection (MariaDB); '' is the default one.")
    _add_output_args(parser, 'the replication-1.0 report')
    args = parser.parse_args(argv)

    def work(conn):
        report = read_status(conn)
        if args.channel is not None:
            channel = _replication_channel(report, args.channel)
            report = dict(report, channels=[channel])
        elif not report.get('available'):
            raise ValueError(report['message'])
        return report
    report = _discover_run(args, work, query_timeout=30)
    _emit_report(args, report, format_text)


def _replication_skip(argv):
    from .replication import (OPERATION_SKIP, SKIP_SCHEMA_VERSION, SKIP_TAG, describe_plan, format_skip,
                              read_status, skip_one)
    parser = _discover_parser(
        'replication skip', 'Skip the one transaction a replication channel stopped on, after showing the error, '
                            'the exact statements and the drift warning and asking you to confirm.',
        'MySQL with a GTID for the failed transaction: commits an empty transaction with that GTID and '
        'starts the applier. Otherwise (anonymous transactions, a channel that assigns GTIDs to anonymous '
        'transactions, MariaDB): sets the skip counter to 1 and starts that channel\'s applier. Right before '
        'sending, myflames reads the channel again and refuses if the error, GTID, position or source changed. '
        'The skipped transaction is missing on the replica afterwards: verify the affected tables with '
        'checksums on both servers. Follows the connection\'s write policy (a read-only profile refuses before '
        'connecting); every attempt is recorded (myflames audit). '
        'Exit codes: 0 skipped, 1 a sent operation failed or the applier did not pass the failed '
        'transaction, 2 nothing was sent, bad input or connection failure.')
    parser.add_argument('--channel', metavar='NAME', required=True,
                        help="Skip on this channel (MySQL) or connection (MariaDB); pass '' for the default one.")
    parser.add_argument('--yes', action='store_true',
                        help='Confirm without a prompt (required when stdin is not a terminal).')
    _add_output_args(parser, 'the replication-skip-1.0 result')
    args = parser.parse_args(argv)
    decision, audit_for, state = _replication_policy(args, OPERATION_SKIP)
    target = {'channel': args.channel}
    audit = state['audit'] = audit_for()
    state['target'] = target

    def finish(code, result):
        if code and not result.get('sent'):
            _fail(result.get('message') or 'Nothing was sent.', state['password'])
        text = json.dumps(result, indent=2, ensure_ascii=False) + '\n' if args.json else format_skip(result)
        _emit_text(text, args.output, quiet=args.quiet)
        raise SystemExit(code)

    def unsent(audit, status, outcome, message, statements=(), identity=None):
        audit.outcome([SKIP_TAG + statement for statement in statements], status, outcome=outcome,
                      message=message, target=identity or target)
        _fail(message, state['password'])
    if decision['action'] == 'refuse':
        finish(*unsent(audit, 'refused', 'read_only', decision['text']))

    def work(conn):
        report = read_status(conn)
        audit.server(report['engine'], report['server_version'])
        channel = _replication_channel(report, args.channel)
        if channel['kind'] == 'group_replication':
            return unsent(audit, 'refused', 'unavailable',
                          '%r is a Group Replication channel; myflames does not skip transactions there.'
                          % args.channel)
        plan = channel.get('skip_plan')
        if plan is None:
            return unsent(audit, 'refused', 'nothing_to_skip',
                          'Channel %r has no failed transaction to skip: its applier is %s and reports no error.'
                          % (args.channel, 'running' if channel['sql']['running'] == 'Yes' else 'stopped'))
        if not plan['available']:
            return unsent(audit, 'refused', 'unavailable', plan['message'], (), plan['identity'])
        state['target'] = plan['identity']
        state['statements'] = [SKIP_TAG + statement for statement in plan['statements']]
        label = 'connection' if report['engine'] == 'mariadb' else 'channel'
        description = describe_plan(plan, label) + '\n  ' + decision['text']
        problem = _replication_confirm('Skip this transaction?', args.yes, args.quiet, description)
        if problem:
            return unsent(audit, problem[0], problem[1], problem[2], plan['statements'], plan['identity'])
        result = skip_one(conn, args.channel, plan['identity'], audit=audit, decision=decision, confirmed=True,
                          statements=plan['statements'], decision_reader=state['decision_reader'])
        ok = result['status'] == 'skipped' and (result.get('after') or {}).get('passed') is True
        return (0 if ok else 1), result
    try:
        finish(*_replication_connect(args, state, work))
    except KeyboardInterrupt:
        _fail('Cancelled. If the statements were already sent, their outcome is unknown; check with: '
              'myflames replication', state['password'])


def _replication_skip_loop(argv):
    from .replication import (COMMON_ERRORS, DRIFT_WARNING, LOOP_DEFAULT_POLL_S, LOOP_MAX_SKIPS,
                              OPERATION_LOOP, SkipLoop, describe_plan, format_loop_event, read_status,
                              receiver_guard_plan)
    parser = _discover_parser(
        'replication skip-loop', 'Watch one replication channel and skip each transaction that stops it with '
                                 'one of the error codes you choose, until you press Ctrl+C or a limit is '
                                 'reached. Prints each skip as it happens.',
        'Each skip re-reads the channel and uses the same method as myflames replication skip, and is recorded '
        'in the audit trail. The loop stops at once on an error you did not choose, the same transaction failing '
        'again, the applier stopped without an error, a change of source, gtid_mode or channel, a server '
        'restart, a lost connection, --max skips, or --max-minutes. It never resumes after it stops. Every '
        'skipped transaction is missing on the replica: verify the affected tables afterwards. Common codes: %s. '
        'Exit codes: 0 stopped by Ctrl+C, --max or --max-minutes, 1 a started loop stopped on a condition '
        'that needs attention, 2 the loop was not started, bad input or connection failure.'
        % ', '.join('%d %s' % item for item in sorted(COMMON_ERRORS.items())))
    parser.add_argument('--channel', metavar='NAME', required=True,
                        help="Watch this channel (MySQL) or connection (MariaDB); pass '' for the default one.")
    parser.add_argument('--errors', type=_error_codes_arg, required=True, metavar='CODES',
                        help='Skip only these applier error codes, separated by commas (none are preselected).')
    parser.add_argument('--max', type=_bounded_integer(1, LOOP_MAX_SKIPS), default=10, metavar='N',
                        help='Stop after N skips, 1–%d (default: 10).' % LOOP_MAX_SKIPS)
    parser.add_argument('--max-minutes', type=_bounded_integer(1, 1440), metavar='M',
                        help='Stop after M minutes, 1–1440 (default: no time limit).')
    parser.add_argument('--interval', type=_bounded_integer(1, 60), default=int(LOOP_DEFAULT_POLL_S), metavar='S',
                        help='Check the channel every S seconds, 1–60 (default: %d); waits grow while skips '
                             'keep coming.' % LOOP_DEFAULT_POLL_S)
    parser.add_argument('--yes', action='store_true',
                        help='Start without a prompt (required when stdin is not a terminal).')
    parser.add_argument('--json', action='store_true',
                        help='Emit the final replication-skip-loop-1.0 status as machine-readable JSON (skips '
                             'are printed on stderr).')
    parser.add_argument('-o', '--output', metavar='PATH', help='Save the final status; live events go to stderr.')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Suppress the description and notes on stderr; rely on stdout and the exit code.')
    args = parser.parse_args(argv)
    decision, audit_for, state = _replication_policy(args, OPERATION_LOOP)
    target = {'channel': args.channel, 'codes': args.errors, 'max_skips': args.max,
              'max_minutes': args.max_minutes}
    audit = state['audit'] = audit_for()
    state['target'] = target

    def refuse(status, outcome, message):
        audit.outcome([], status, outcome=outcome, message=message, target=target)
        _fail(message, state['password'])
    if decision['action'] == 'refuse':
        refuse('refused', 'read_only', decision['text'])

    def emit(event):
        line = format_loop_event(event)
        if not args.quiet:
            sys.stderr.write(line)
            sys.stderr.flush()

    def work(conn):
        report = read_status(conn)
        audit.server(report.get('engine'), report.get('server_version'))
        channel = _replication_channel(report, args.channel)
        if channel['kind'] == 'group_replication':
            raise ValueError('%r is a Group Replication channel; myflames does not skip transactions there.'
                             % args.channel)
        label = 'connection' if report['engine'] == 'mariadb' else 'channel'
        lines = ['Skip until stopped on %s %r (%s %s).' % (label, args.channel, report['engine'],
                                                          report['server_version'] or '?'),
                 '  Errors to skip: %s' % ', '.join('%d%s' % (c, ' (%s)' % COMMON_ERRORS[c] if c in COMMON_ERRORS
                                                                else '') for c in args.errors),
                 '  At most %d skip%s%s; checks every %d s.' % (args.max, '' if args.max == 1 else 's',
                                                              ', for at most %d min' % args.max_minutes
                                                              if args.max_minutes else '', args.interval)]
        plan = channel.get('skip_plan')
        if plan and plan['available'] and plan['error']['errno'] in args.errors:
            lines.append('  It is failing now; the first skip would be:')
            lines.append(describe_plan(plan, label))
        else:
            if plan:
                lines.append('  It is failing now with error %d, which is not in your list, so the loop will stop '
                             'at once.' % plan['error']['errno'])
            lines.append('  Warning: %s' % DRIFT_WARNING)
        guard = receiver_guard_plan(conn, report)
        lines.append('  ' + ('Stops if a receiver reconnects.' if guard['receiver_guard'] == 'on'
                             else guard['receiver_guard_note']))
        lines.append('  ' + decision['text'])
        problem = _replication_confirm('Start skipping?', args.yes, args.quiet, terminal_text('\n'.join(lines)))
        if problem:
            refuse(problem[0], problem[1], problem[2])
        loop = SkipLoop(conn, args.channel, args.errors, args.max,
                        max_duration_s=args.max_minutes * 60 if args.max_minutes else None,
                        poll_s=args.interval, decision=decision, audit_factory=audit_for, on_event=emit,
                        decision_reader=state['decision_reader'])
        if not args.quiet:
            sys.stderr.write('Watching; press Ctrl+C to stop.\n')
        return loop.run()
    status = _replication_connect(args, state, work)
    if args.json:
        _emit_text(json.dumps(status, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
    else:
        _emit_text(status['message'] + '\n', args.output, quiet=args.quiet)
    if status['stop_reason'] == 'connection_lost':
        raise SystemExit(2)
    raise SystemExit(1 if status['needs_attention'] else 0)


# ---- config set / reset-persist (variables.py) ----------------------------------------

def _config_change(sub, argv):
    """``myflames config set NAME VALUE [--persist | --persist-only | --session]``
    and ``myflames config reset-persist NAME``: one variable change, planned
    from a fresh read, described, confirmed and audited (variables.change_cli).
    The connection's write policy runs before any password prompt or
    connection (a read-only profile refuses here)."""
    from . import dbops, variables
    from .connector import ConnectorError, MySQLConnection
    from .workspace_store import WorkspaceStore, PROFILE_FIELDS
    reset = sub == 'reset-persist'
    if reset:
        parser = _discover_parser(
            'config reset-persist', 'Remove one variable from mysqld-auto.cnf with RESET PERSIST (MySQL), after '
                                    'showing its persisted value and asking you to confirm. The running value '
                                    'stays; after a restart the server uses the option files or its default.',
            'MySQL only: MariaDB has no SET PERSIST, so there is nothing to reset (edit the option file instead). '
            'Needs SYSTEM_VARIABLES_ADMIN or SUPER, or SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN for '
            'an entry stored as read-only (a read-only variable, or innodb_buffer_pool_size and other variables '
            'persisted as read-only). Follows the connection\'s write policy; every attempt is recorded '
            '(myflames audit). Exit codes: 0 removed, 1 a sent operation failed or was not verified, 2 nothing was sent, bad input or '
            'connection failure.')
    else:
        parser = _discover_parser(
            'config set', 'Change one server variable after showing the exact statement, the current value, the '
                          'effect, the privileges it needs and the caveats, and asking you to confirm. Without a '
                          'flag it runs SET GLOBAL (now, until the server restarts).',
            'Before sending, myflames reads the variable again and refuses if it changed since the plan. Values are '
            'checked against the variable\'s type, range and allowed values; sizes take K, M, G and T suffixes '
            '(converted to bytes) and DEFAULT restores the server\'s default. --persist and --persist-only are MySQL '
            'only (mysqld-auto.cnf in the data directory); on MariaDB they are refused and the option file line to '
            'add is printed. Privileges: SET GLOBAL and SET PERSIST need SYSTEM_VARIABLES_ADMIN or SUPER on MySQL '
            '(some variables need more), SUPER or a variable\'s own privilege on MariaDB (CONNECTION ADMIN for '
            'max_connections, BINLOG ADMIN for sync_binlog); SET PERSIST_ONLY needs SYSTEM_VARIABLES_ADMIN and '
            'PERSIST_RO_VARIABLES_ADMIN. --session runs in a short session of its own, so it only checks the value '
            'the server takes. Follows the connection\'s write policy (Production confirms every change, '
            'Development confirms SET GLOBAL and SET PERSIST, read-only refuses); every attempt is recorded '
            '(myflames audit). Exit codes: 0 changed, 1 a sent operation failed or was not verified, 2 nothing was sent, bad input or '
            'connection failure.')
    parser.add_argument('name', metavar='NAME', help='The system variable, for example max_connections.')
    if not reset:
        parser.add_argument('value', metavar='VALUE',
                            help='The new value: a number (sizes may end in K, M, G or T), ON or OFF, one of the '
                                 'allowed values, text, or DEFAULT.')
        mode_group = parser.add_mutually_exclusive_group()
        mode_group.add_argument('--persist', action='store_true',
                                help='Use SET PERSIST: change it now and keep it after a restart (MySQL only).')
        mode_group.add_argument('--persist-only', action='store_true',
                                help='Use SET PERSIST_ONLY: write it for the next restart only, for read-only '
                                     'variables (MySQL only).')
        mode_group.add_argument('--session', action='store_true',
                                help='Use SET SESSION in a short myflames session, to check the value the server '
                                     'takes for one session.')
    parser.add_argument('--yes', action='store_true',
                        help='Confirm without a prompt (required when stdin is not a terminal).')
    _add_output_args(parser, 'the variable-change-1.0 result')
    args = parser.parse_args(argv)
    value = None if reset else args.value
    mode = 'reset_persist' if reset else ('persist' if args.persist else 'persist_only' if args.persist_only
                                          else 'session' if args.session else 'global')
    try:
        name = variables.check_name(args.name)
    except ValueError as exc:
        _fail(exc)
    profile = _saved_profile(args)
    password = None if args.password == '__PROMPT__' else args.password
    planned = {f: profile[f] for f in PROFILE_FIELDS if profile and profile.get(f) not in (None, '')}
    planned.update(_connection_settings(args, None))
    planned.setdefault('host', '127.0.0.1')
    planned.setdefault('port', 3306)
    # A profile's type applies only while the flags keep its host, port, user and TLS settings.
    safety = dbops.connection_safety(profile, planned)
    decision = dbops.decide(dbops.classify(variables.representative_statement(name, mode)), safety['type'],
                            safety['read_only'], safety['profile_name'])
    audit = dbops.AuditTrail(WorkspaceStore(), variables.OPERATIONS[mode], 'cli', connection=_endpoint(planned),
                             safety=safety, password=password, policy=dbops.policy_summary(decision))
    typed = [variables.TAG + variables.typed_statement(name, mode, value)]
    target = {'name': name, 'mode': mode, 'requested': value}

    def finish(code, result):
        if code and result.get('statement') is None:
            _fail(variables.format_change(result).rstrip(), password)
        text = json.dumps(result, indent=2, ensure_ascii=False) + '\n' if args.json else variables.format_change(result)
        _emit_text(text, args.output, quiet=args.quiet)
        raise SystemExit(code)
    if decision['action'] == 'refuse':
        result = variables.unsent_result(name, mode, value, 'refused', decision['text'])
        result['audit_id'] = audit.outcome(typed, 'refused', outcome='read_only', message=decision['text'],
                                           target=target)
        finish(1, result)
    try:
        settings = _resolve_connection(args)
        password = audit.password = settings.get('password')
        audit.connection = _endpoint(settings)
        options = _connection_options(settings)
        options['query_timeout'] = 30
        with MySQLConnection(**options) as conn:
            code, result = variables.change_cli(conn, name, value, mode, args.yes, args.quiet, audit=audit,
                                                decision=decision)
    except KeyboardInterrupt:
        sent = audit.pending
        if sent or audit.finished:
            audit.fail(KeyboardInterrupt(), typed)
        else:
            audit.outcome(typed, 'declined', outcome='declined', message='Cancelled before sending.', target=target)
        _fail('Cancelled.' + (' The change may already have reached the server, so its outcome is unknown; check '
                              'with: myflames config' if sent else ''), password)
    except EOFError:
        audit.outcome(typed, 'declined', outcome='declined', message='Password prompt cancelled.', target=target)
        _fail('Password prompt cancelled.', password)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        audit.fail(exc, typed)
        _fail(exc, password)
    finish(code, result)
