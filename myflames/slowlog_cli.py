"""``myflames slow-log``: a slow query log file, or the mysql.slow_log table,
summed by statement fingerprint (``slow-log-1.0``)."""
import json
import sys

from . import slowlog

DESCRIPTION = ("Sum a slow query log by statement fingerprint: count, total, average, P95 and maximum time, lock "
               "time and rows, within an optional time range. Read a log file (MySQL 5.7 to 9.x, Percona Server "
               "or MariaDB), standard input, or the mysql.slow_log table of a server with --table.")
EPILOG = ("Times are compared as the log writes them, in its own clock (UTC, an offset, or the server's local "
          "time; the report says which). --from and --to take YYYY-MM-DD HH:MM[:SS] and are inclusive to the "
          "precision written. Read-only: a file is only read; --table sends only SELECTs (mysql.slow_log and "
          "the server's time zone). "
          "Examples: myflames slow-log /var/log/mysql/slow.log --from '2026-10-31 09:00' --to '2026-10-31 10:00'; "
          "myflames slow-log --table --profile prod --order avg --limit 10. "
          "Exit codes: 0 success, 2 bad input, unreadable file, or connection failure.")


def _parser():
    from .workspace_cli import _add_output_args, _bounded_integer, _discover_parser
    parser = _discover_parser('slow-log', DESCRIPTION, EPILOG)
    parser.add_argument('file', nargs='?', metavar='FILE',
                        help="Read this slow log file ('-' reads standard input).")
    parser.add_argument('--table', action='store_true',
                        help='Read the mysql.slow_log table of the server the connection flags name instead of a file.')
    parser.add_argument('--from', dest='range_from', metavar='TIME',
                        help='Keep entries logged at or after TIME (YYYY-MM-DD HH:MM[:SS], in the log\'s clock).')
    parser.add_argument('--to', dest='range_to', metavar='TIME',
                        help='Keep entries logged at or before TIME (inclusive to the precision written).')
    parser.add_argument('--executed-by', dest='run_by', metavar='USER',
                        help='Keep only statements this user ran (the user of the User@Host line).')
    parser.add_argument('--schema', metavar='NAME', help='Keep only statements run with this default schema.')
    parser.add_argument('--order', choices=tuple(slowlog.ORDERS), default='total',
                        help='Rank by total time, calls (count), average, maximum, P95, lock time, rows examined or '
                             'rows sent (default: total).')
    parser.add_argument('--limit', type=_bounded_integer(1, slowlog.MAX_LIMIT), default=20, metavar='N',
                        help='Show the top 1-%d statements (default: 20).' % slowlog.MAX_LIMIT)
    _add_output_args(parser, 'the slow-log-1.0 report')
    return parser


class _Bounded(object):
    """Standard input read line by line, refused past MAX_FILE_BYTES (never held whole)."""

    def __init__(self, stream):
        self.stream = stream
        self.read = 0

    def readline(self, size=-1):
        line = self.stream.readline(size)
        self.read += len(line)
        if self.read > slowlog.MAX_FILE_BYTES:
            raise ValueError('Standard input is larger than 4 GiB. Split the log, or copy the part you need.')
        return line


def _read_stdin(args):
    source = {"kind": "file", "name": "stdin", "size_bytes": 0, "modified": None, "kind_name": "slow_log_file"}
    stream = _Bounded(sys.stdin.buffer)
    report = slowlog.read_stream(stream, source, args.range_from, args.range_to, args.run_by,
                                 args.schema, args.order, args.limit)
    report['source']['size_bytes'] = stream.read
    return report


def cmd_slow_log(argv):
    from .cli import _fail
    from .connector import terminal_text
    from .workspace_cli import _compact, _count, _discover_run, _duration, _emit_text, _notes
    from .text_format import _table
    parser = _parser()
    args = parser.parse_args(argv)
    if bool(args.file) == bool(args.table):
        parser.error('name a slow log FILE, or pass --table to read mysql.slow_log')
    try:
        slowlog.check_options(args.order, args.limit, args.run_by, args.schema)
        slowlog.check_range(args.range_from, args.range_to)
    except ValueError as exc:
        parser.error(str(exc))
    if args.table:
        from .slow_queries import slow_log_table_report
        report = _discover_run(args, lambda conn: slow_log_table_report(
            conn, args.range_from, args.range_to, args.run_by, args.schema, args.order, args.limit), query_timeout=30)
    else:
        try:
            if args.file == '-':
                report = _read_stdin(args)
            else:
                report = slowlog.read_file(_absolute(args.file), args.range_from, args.range_to, args.run_by,
                                           args.schema, args.order, args.limit)
        except KeyboardInterrupt:
            _fail('Cancelled.')
        except (ValueError, OSError) as exc:
            _fail(exc)
    if args.json:
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
        return
    rows = []
    for rank, item in enumerate(report['statements'], 1):
        rows.append([str(rank), _duration(item['total_ms']), _count(item['count']), _duration(item['avg_ms']),
                     _duration(item['p95_ms']), _duration(item['max_ms']), _duration(item['lock_time_ms']),
                     _count(item['rows_examined']), _count(item['rows_sent']), item['schema'] or '—',
                     _compact(item['digest_text'], 72)])
    headers = ['>#', '>TOTAL', '>CALLS', '>AVG', '>P95', '>MAX', '>LOCK', '>EXAMINED', '>SENT', 'SCHEMA', 'FINGERPRINT']
    _emit_text(_table(headers, rows), args.output, quiet=args.quiet)
    if not args.quiet:
        summary = summary_lines(report)
        for line in summary:
            sys.stderr.write(terminal_text(line) + '\n')
    _notes(report['notes'], args.quiet)


def _absolute(path):
    import os
    return os.path.abspath(path)


def summary_lines(report):
    """The source, clock, range and totals as short lines (stderr)."""
    source = report['source']
    if source.get('kind') == 'table':
        what = 'mysql.slow_log (%s rows read)' % '{:,}'.format(source.get('rows_read') or 0)
    else:
        what = 'Slow log file: %s' % source.get('name')
    totals = report['totals']
    lines = ['source: %s' % what,
             'clock: %s' % report['clock']['text'],
             'counted: %s statements, %s executions, %s total' % (
                 '{:,}'.format(totals['statements']), '{:,}'.format(totals['executions']),
                 '%.3f s' % (totals['total_ms'] / 1000.0))]
    if report['range']['applied']:
        lines.append('range: %s to %s' % (report['range']['from'] or 'the start', report['range']['to'] or 'the end'))
    span = report.get('span') or {}
    if span.get('first'):
        lines.append('log spans: %s to %s' % (span['first'], span['last']))
    return lines
