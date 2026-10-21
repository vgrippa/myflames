"""Read-only Error log view, shared by the CLI and workspace (error-log-1.0).

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"):

* performance_schema.error_log columns and its SELECT-only ACL:
  M/storage/perfschema/table_error_log.cc:64-88 (pfs_readonly_acl, so a
  normal SELECT grant applies, M/storage/perfschema/pfs_engine_table.cc:
  1041-1070). It first shipped in MySQL 8.0.22 (WL#13681). With
  log_sink_json, DATA holds the JSON event and its msg key is the message
  (header comment of table_error_log.cc).
* Rows live in a 5 MiB ring buffer (ring_buffer_size, M/sql/server_component/
  log_sink_perfschema.cc:73) described by the Error_log_* status counters
  (M/sql/mysqld.cc:11766-11773); Error_log_latest_write is microseconds since
  the epoch (my_micro_time). When @@log_error names a file, the server
  re-reads its tail (at most 5 MiB) at startup through the leftmost parsing
  sink (log_sink_internal reads @@log_error, log_sink_json its own JSON
  file), so rows older than the current start are from earlier runs. With
  stderr or no parsing sink the table holds only events since this start
  (M/sql/mysqld.cc:7903-7933; log_sink_perfschema.cc:584-597, 656-658,
  695-728).
* Only sinks with performance_schema support feed the table:
  log_sink_internal, log_sink_json, log_sink_syseventlog (M/sql/
  server_component/log_builtins.cc:275-278, 2140-2154; M/components/logging/
  log_sink_json.cc:576-580; log_sink_syseventlog.cc:948).
* log_filter_internal drops events above log_error_verbosity and never drops
  System or Error (M/sql/server_component/log_builtins_filter.cc:300-330); the
  verbosity filter applies only while log_filter_internal is listed
  (log_builtins_filter.cc:790-799). log_filter_dragnet runs its own rules
  (M/components/logging/log_filter_dragnet.cc:71-73, 1546-1547).
* LOGGED is stored as epoch time and shown in the session time zone
  (M/storage/perfschema/table_helper.cc:411-416). The current start is
  UNIX_TIMESTAMP() minus Uptime (M/sql/mysqld.cc:11283-11289), so the
  previous-run test compares epoch seconds and is not shifted by daylight
  saving changes.
* MariaDB has no error_log or error summary tables (Ma/storage/perfschema).
  LOAD_FILE needs FILE, secure_file_priv, a world-readable file and a size
  under max_allowed_packet (Ma/sql/item_strfunc.cc:4183-4216); this module
  never calls it and reads the file named by @@log_error itself.
* MariaDB lines: server SYSTEM local time with a space-padded hour, thread
  id, then [ERROR], [Warning] or [Note] (Ma/sql/log.cc:11940-11950). A crash
  report starts "YYMMDD H:MM:SS [ERROR] <prog> got signal N ;" without a
  thread id (Ma/sql/signal_handler.cc:181-196). The startup banner
  (ER_STARTUP, always English, Ma/sql/share/errmsg-utf8.txt:6916-6918) reads
  "Version: '<@@version>'", or "'<built version>' as '<@@version>'" when
  version was set at startup, and "port: <@@port>" (Ma/sql/mysqld.cc:
  6324-6341). server_start_time is set early in startup (Ma/sql/mysqld.cc:
  3974), before the banner, so the running server's banner is logged at or
  after its start.
* Empty @@log_error means standard error (Ma/sql/mysqld.cc:8775-8781,
  9099-9102). A bare name becomes ./name.err and is opened after the server
  changes into datadir (Ma/mysys/my_static.c:59; Ma/sql/mysqld.cc:5052-5076,
  6157, 6207), so relative names resolve against @@datadir.

MySQL: counts and code groups cover the whole buffer; entries alone are
filtered and limited. The reads are not an atomic snapshot of a changing
buffer. MariaDB: the file is read only for a loopback connection whose
@@hostname is this computer's, at most its last TAIL_BYTES, and only when
the latest startup banner in those bytes names @@version and @@port and was
logged within BANNER_WINDOW_S after the server's start. Only parsed entries
(with at most MAX_CONTINUATION continuation lines each) are returned.
"""
import datetime
import json
import os
import re
import shlex
import socket
import stat

from .connector import ConnectorError
from .perf_common import (Reader, Server, _bytes_text, base_report, check_limit,
                          denied, preflight, ps_off_text, reason, table_text)

SCHEMA_VERSION = "error-log-1.0"
ERROR_LOG_TAG = "/*myflames:error-log*/ "
DEFAULT_LIMIT, MAX_LIMIT = 200, 1000
TAIL_BYTES = 1 << 20
MESSAGE_CHARS = 4000
CAPACITY_BYTES = 5 << 20
LEVELS = ("System", "Error", "Warning", "Note")
SEVERITY = {"ok": 0, "watch": 1, "problem": 2}
PFS_SINKS = frozenset(("log_sink_internal", "log_sink_json", "log_sink_syseventlog"))
#: Settings read with the preflight's SHOW GLOBAL VARIABLES (a variable the
#: engine does not have is simply absent).
SETTINGS = ("log_error", "log_error_verbosity", "log_error_services", "datadir", "hostname", "port",
            "log_warnings")
MAX_CONTINUATION = 100
BANNER_WINDOW_S = 3600
_COUNTERS = ("Error_log_buffered_bytes", "Error_log_buffered_events", "Error_log_expired_events",
             "Error_log_latest_write")
# The start as epoch seconds (for comparisons) and in the session time zone
# (for display, like LOGGED), from one Uptime read.
_MYSQL_STATUS = ("SELECT g.VARIABLE_NAME, g.VARIABLE_VALUE, s.start_epoch, FROM_UNIXTIME(s.start_epoch) "
                 "FROM performance_schema.global_status g CROSS JOIN "
                 "(SELECT UNIX_TIMESTAMP() - CAST(VARIABLE_VALUE AS UNSIGNED) AS start_epoch "
                 "FROM performance_schema.global_status WHERE VARIABLE_NAME = 'Uptime') s "
                 "WHERE g.VARIABLE_NAME IN (%s)" % ", ".join("'%s'" % n for n in _COUNTERS + ("Uptime",)))
# MariaDB writes its log in the server's SYSTEM time (localtime_r), even if
# the SQL session uses another time zone. Subtract Uptime in UTC, then
# convert, so a daylight saving change since the start does not shift it.
_MARIA_STATUS = ("SELECT VARIABLE_VALUE, CONVERT_TZ(UTC_TIMESTAMP() - INTERVAL CAST(VARIABLE_VALUE AS UNSIGNED) "
                 "SECOND, '+00:00', 'SYSTEM') FROM information_schema.GLOBAL_STATUS WHERE VARIABLE_NAME = 'Uptime'")
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) +([ 0-9]\d:\d{2}:\d{2}) (\d+) "
                   r"\[(ERROR|Warning|Note)\] (.*)$")
_CRASH = re.compile(r"^(\d{2})(\d{2})(\d{2}) +(\d{1,2}:\d{2}:\d{2}) \[(ERROR|Note)\] (.*)$")
_BANNER = re.compile(r"^Version: '([^'\n]{1,128})'(?: as '([^'\n]{1,128})')?\s+socket: '[^'\n]{0,512}'"
                     r"\s+port: (\d{1,5})(?:\s|$)", re.M)


def check_levels(levels):
    """Canonical, deduplicated levels; an empty filter means all levels."""
    if levels is None:
        return list(LEVELS)
    if isinstance(levels, str):
        levels = levels.split(',') if levels.strip() else []
    if not isinstance(levels, (list, tuple)):
        raise ValueError("Choose levels from System, Error, Warning, Note.")
    chosen = set()
    for level in levels:
        if not isinstance(level, str) or level.strip().title() not in LEVELS:
            raise ValueError("Choose levels from System, Error, Warning, Note.")
        chosen.add(level.strip().title())
    return [level for level in LEVELS if not chosen or level in chosen]


def decode_data(data):
    """Decode log_sink_json's msg, keeping malformed or other JSON as text."""
    data = data or ""
    if data.lstrip().startswith('{'):
        try:
            event = json.loads(data)
            if isinstance(event, dict) and isinstance(event.get('msg'), str):
                return event['msg']
        except (ValueError, RecursionError):
            pass
    return data


def local_connection(host):
    return isinstance(host, str) and host.lower() in ('', 'localhost', '127.0.0.1', '::1')


def same_host(server_host, local_host):
    """MariaDB's @@hostname and Python both come from gethostname() on the
    server's machine, so on the same computer they are equal."""
    return bool(server_host and local_host) and server_host.rstrip('.').lower() == local_host.rstrip('.').lower()


def resolve_path(log_error, datadir):
    return os.path.realpath(os.path.join(datadir or '', log_error))


def latest_banner(entries):
    """(version, port, logged) of the latest startup banner in file order,
    never an earlier one (such as a Docker entrypoint's temporary server);
    version and port are None when that banner is incomplete."""
    for entry in reversed(entries):
        message = entry['message']
        if message.split('\n', 1)[0].endswith(': ready for connections.'):
            match = _BANNER.search(message)
            # An incomplete latest banner must not authenticate an older one.
            if not match:
                return None, None, entry['logged']
            return match.group(2) or match.group(1), int(match.group(3)), entry['logged']
    return None


def parse_mariadb(text, server_started=None):
    """Parse complete formatted lines and attach subsequent continuation lines."""
    entries = []
    message_lines = []
    for line in text.splitlines():
        match = _LINE.match(line)
        crash = None if match else _CRASH.match(line)
        if crash:
            yy, mm, dd, clock, level, message = crash.groups()
            match_groups = ('20%s-%s-%s' % (yy, mm, dd), clock, None, level, message)
        elif match:
            match_groups = match.groups()
        if match or crash:
            date, clock, thread, level, message = match_groups
            logged = date + ' ' + clock.strip().zfill(8)
            try:
                datetime.datetime.strptime(logged, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            if entries:
                entries[-1]['message'] = '\n'.join(message_lines)
            message_lines = [message]
            entries.append({'logged': logged, 'thread_id': int(thread) if thread is not None else None,
                            'prio': 'Error' if level == 'ERROR' else level, 'error_code': None,
                            'subsystem': 'InnoDB' if message.startswith('InnoDB: ') else None,
                            'message': message, 'previous_run': bool(server_started and logged < server_started)})
        elif entries:
            if len(message_lines) <= MAX_CONTINUATION:
                message_lines.append(line)
            elif len(message_lines) == MAX_CONTINUATION + 1:
                message_lines.append('[more lines not shown]')
    if entries:
        entries[-1]['message'] = '\n'.join(message_lines)
    return entries


def _counts():
    return {level.lower(): 0 for level in LEVELS}


def aggregate_entries(entries):
    """Counts and at most 20 message groups across the entire parsed tail."""
    counts, current, groups = _counts(), _counts(), {}
    for entry in entries:
        key = entry['prio'].lower()
        counts[key] += 1
        if not entry['previous_run']:
            current[key] += 1
        key = (entry['prio'], entry['subsystem'], entry['message'][:80])
        group = groups.setdefault(key, {'error_code': None, 'prio': entry['prio'],
                                      'subsystem': entry['subsystem'], 'count': 0,
                                      'first_logged': entry['logged'], 'last_logged': entry['logged'],
                                      'sample': entry['message'][:MESSAGE_CHARS]})
        group['count'] += 1
        group['first_logged'] = min(group['first_logged'], entry['logged'])
        group['last_logged'] = max(group['last_logged'], entry['logged'])
    codes = sorted(groups.values(), key=lambda g: (-g['count'], g['prio'], g['subsystem'] or '', g['sample']))[:20]
    return counts, current, codes


def _new_report(server, limit, levels):
    report = base_report(SCHEMA_VERSION, server)
    report.update({'available': False, 'reason': None, 'reason_text': None, 'how_to_view': None,
                   'source': None, 'settings': {'log_error': None, 'log_error_verbosity': None,
                   'log_error_services': None, 'log_warnings': None, 'datadir': None},
                   'buffer': None, 'file': None, 'server_started': None,
                   'counts': _counts(), 'counts_current_run': _counts(), 'codes': [], 'entries': [],
                   'entries_total': 0, 'limit': limit, 'levels': levels,
                   'summary': {'status': 'unknown', 'text': 'Error log has not been read.'}, 'notes': []})
    return report


def _unavailable(report, code, text, hint=None):
    report.update(available=False, reason=code, reason_text=text, how_to_view=hint)
    report['summary'] = {'status': 'unknown', 'text': text}
    return report


def _plural(n, word):
    return '%d %s%s' % (n, word, '' if n == 1 else 's')


def _finish(report):
    current = report['counts_current_run']
    report['available'] = True
    report['summary'] = {'status': 'watch' if current['error'] else 'ok',
                         'text': '%s and %s recorded since the server started at %s.'
                         % (_plural(current['error'], 'error'), _plural(current['warning'], 'warning'),
                            report['server_started'])}
    return report


def _mysql_status(report, rows):
    """{counter: int}, and the start as epoch seconds; sets server_started."""
    values = {}
    start_epoch = None
    for row in rows:
        if len(row) != 4 or _int(row[1]) is None or _int(row[2]) is None or not row[3]:
            raise ValueError('Could not read the error log status counters.')
        values[row[0].lower()] = int(row[1])
        start_epoch, report['server_started'] = int(row[2]), row[3]
    missing = [name for name in _COUNTERS + ('Uptime',) if name.lower() not in values]
    if missing:
        raise ValueError('Could not read the error log status counters (%s).' % ', '.join(missing))
    report['uptime_s'] = values['uptime']
    return values, start_epoch


def _maria_status(report, rows):
    if not rows or len(rows[0]) != 2 or _int(rows[0][0]) is None or not rows[0][1]:
        raise ValueError('Could not read the server start time for the error log.')
    report['uptime_s'], report['server_started'] = int(rows[0][0]), rows[0][1]


def _latest_write(value):
    micros = int(value)
    if not micros:
        return None
    # Integer arithmetic avoids float rounding of microsecond epoch values.
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=micros)).strftime('%Y-%m-%d %H:%M:%SZ')


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mysql_report(reader, server, report):
    # WL#13681 added the table and its status counters in MySQL 8.0.22.
    # Gate before settings too: 5.7 has no log_error_services variable.
    if server.version_tuple < (8, 0, 22):
        path = server.var('log_error')
        hint = (_how_to_view(path, server.var('datadir')) if path and path != 'stderr' else
                "Read the error log file on the server host, or the service manager's log (for example journalctl).")
        return _unavailable(report, 'unsupported_version',
                            'performance_schema.error_log needs MySQL 8.0.22 or later.', hint)
    path, services = server.var('log_error'), server.var('log_error_services')
    verbosity = _int(server.var('log_error_verbosity'))
    if services is None or verbosity is None:
        raise ValueError('Could not read the error log settings.')
    report['settings'].update(log_error=path, log_error_verbosity=verbosity, log_error_services=services)
    if not server.ps_on:
        return _unavailable(report, 'performance_schema_off', ps_off_text(server, 'the error log buffer'))
    services_set = set(part.strip() for part in re.split('[;,]', services or '') if part.strip())
    if not services_set.intersection(PFS_SINKS):
        return _unavailable(report, 'no_pfs_sink', 'No configured error log sink feeds performance_schema.error_log.')
    values, start_epoch = _mysql_status(report, reader.rows(_MYSQL_STATUS))
    report['buffer'] = {'bytes': values['error_log_buffered_bytes'],
                        'events': values['error_log_buffered_events'],
                        'expired_events': values['error_log_expired_events'],
                        'latest_write': _latest_write(values['error_log_latest_write']),
                        'capacity_bytes': CAPACITY_BYTES}
    # start_epoch is an int parsed from the server's answer, never user input.
    rows = reader.rows("SELECT PRIO, COUNT(*), SUM(UNIX_TIMESTAMP(LOGGED) >= %d) "
                       "FROM performance_schema.error_log GROUP BY PRIO" % start_epoch)
    for prio, count, current in rows:
        report['counts'][prio.lower()] = int(count)
        report['counts_current_run'][prio.lower()] = int(current or 0)
    rows = reader.rows('SELECT ERROR_CODE, PRIO, SUBSYSTEM, COUNT(*), MIN(LOGGED), MAX(LOGGED), MAX(DATA) '
                       'FROM performance_schema.error_log GROUP BY ERROR_CODE, PRIO, SUBSYSTEM '
                       'ORDER BY COUNT(*) DESC LIMIT 20')
    report['codes'] = [{'error_code': code, 'prio': prio, 'subsystem': subsystem, 'count': int(count),
                        'first_logged': first, 'last_logged': last, 'sample': decode_data(data)[:MESSAGE_CHARS]}
                       for code, prio, subsystem, count, first, last, data in rows]
    where = " WHERE PRIO IN (%s)" % ', '.join("'%s'" % level for level in report['levels'])
    # A LEFT JOIN keeps the count even when the filtered source has no rows.
    # Both derived tables are bounded outputs; no full log is sent to Python.
    rows = reader.rows('SELECT e.LOGGED, e.THREAD_ID, e.PRIO, e.ERROR_CODE, e.SUBSYSTEM, e.DATA, '
                       'UNIX_TIMESTAMP(e.LOGGED), t.entries_total '
                       'FROM (SELECT COUNT(*) AS entries_total FROM performance_schema.error_log' + where + ') t '
                       'LEFT JOIN (SELECT LOGGED, THREAD_ID, PRIO, ERROR_CODE, SUBSYSTEM, DATA '
                       'FROM performance_schema.error_log' + where + ' ORDER BY LOGGED DESC LIMIT %d) e '
                       'ON TRUE ORDER BY e.LOGGED DESC' % report['limit'])
    for logged, thread, prio, code, subsystem, data, epoch, total in rows:
        report['entries_total'] = int(total)
        if logged is not None:
            report['entries'].append({'logged': logged, 'thread_id': int(thread) if thread is not None else None,
                                      'prio': prio, 'error_code': code, 'subsystem': subsystem,
                                      'message': decode_data(data)[:MESSAGE_CHARS],
                                      'previous_run': float(epoch) < start_epoch})
    report['source'] = 'performance_schema.error_log'
    if 'log_filter_dragnet' in services_set:
        report['notes'].append('log_filter_dragnet rules decide which events are recorded; verbosity alone does not describe coverage.')
    elif 'log_filter_internal' in services_set:
        if verbosity < 3:
            hidden = 'Warning and Note events are' if verbosity == 1 else 'Note events are'
            report['notes'].append('%s not recorded by the built-in filter at log_error_verbosity=%s; zero does not mean none happened.'
                                   % (hidden, verbosity))
    expired = report['buffer']['expired_events']
    if expired:
        report['notes'].append('The buffer expired %d older events; counts cover only retained events.' % expired)
    report['notes'].append('Counts, code groups and entries are separate reads of a changing buffer and may differ slightly.')
    return _finish(report)


def _read_tail(path):
    """Read a bounded regular-file tail; nonblocking open also guards FIFO races."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise FileNotFoundError('The log path is not a regular file.')
        offset = max(0, info.st_size - TAIL_BYTES)
        # One byte before the cut tells whether the first line read is whole.
        handle.seek(offset - 1 if offset else 0)
        data = handle.read(TAIL_BYTES + (1 if offset else 0))
    if offset:
        whole = data[:1] == b'\n'
        data = data[1:]
    metadata = {'path': path, 'size_bytes': info.st_size, 'read_bytes': len(data),
                'truncated': offset > 0, 'banner_found': False}
    if offset and not whole:
        data = data.split(b'\n', 1)[1] if b'\n' in data else b''
    return data.decode('utf-8', errors='replace'), metadata


def _how_to_view(path, datadir):
    """A shell hint for the server host; never executed. Paths the server
    reports with control characters get words instead of a command."""
    full = os.path.normpath(os.path.join(datadir or '', path))
    if any(ord(c) < 32 or ord(c) == 127 for c in full):
        return 'Read the file named by log_error on the server host (its path has control characters).'
    return 'Read it on the server host: tail -n 200 -- ' + shlex.quote(full)


def _seconds_between(earlier, later):
    try:
        return (datetime.datetime.strptime(later, '%Y-%m-%d %H:%M:%S')
                - datetime.datetime.strptime(earlier, '%Y-%m-%d %H:%M:%S')).total_seconds()
    except (TypeError, ValueError):
        return None


def _mariadb_report(conn, server, reader, report):
    path, datadir, hostname = server.var('log_error'), server.var('datadir'), server.var('hostname')
    port, warnings, version = _int(server.var('port')), _int(server.var('log_warnings')), server.version
    if path is None or port is None:
        raise ValueError('Could not read the error log settings.')
    report['settings'].update(log_error=path, datadir=datadir, log_warnings=warnings)
    _maria_status(report, reader.rows(_MARIA_STATUS))
    if not path:
        return _unavailable(report, 'log_to_stderr',
                            'log_error is empty, so the server writes its error log to standard error and '
                            'there is no file to read.',
                            'Read it where the service manager keeps the server\'s standard error, for example '
                            'journalctl -u mariadb under systemd, or docker logs <container> in Docker.')
    # Remote hints only join path strings: never resolve local symlinks for a
    # server whose host checks have not passed.
    hint = _how_to_view(path, datadir)
    if not local_connection(getattr(conn, 'host', None)):
        return _unavailable(report, 'remote', 'This MariaDB server runs on another computer, and MariaDB has no SQL '
                            'access to its error log, so the log file cannot be read from here.', hint)
    if not same_host(hostname, socket.gethostname()):
        if not hostname:
            text = 'The server reports no host name, so its log file cannot be matched to this computer.'
        else:
            text = ('The server reports host name %s, not this computer\'s, so its log file is not on this '
                    'computer (for example a container or an SSH tunnel).' % hostname)
        return _unavailable(report, 'other_host', text, hint)
    if not os.path.isabs(path) and not datadir:
        return _unavailable(report, 'not_found', 'log_error is the relative name %s and the server reports no '
                            'data directory, so the file cannot be located.' % path, hint)
    try:
        path = resolve_path(path, datadir)
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return _unavailable(report, 'not_found', 'The error log file %s was not found on this computer.' % path, hint)
        if not os.access(path, os.R_OK):
            return _unavailable(report, 'not_readable', 'myflames cannot read the error log file %s; check its '
                                'permissions.' % path, hint)
        text, metadata = _read_tail(path)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return _unavailable(report, 'not_found', 'The error log file %s was not found on this computer.' % path, hint)
    except OSError:
        return _unavailable(report, 'not_readable', 'myflames cannot read the error log file %s; check its '
                            'permissions.' % path, hint)
    entries = parse_mariadb(text, report['server_started'])
    report['file'] = metadata
    if not entries:
        return _unavailable(report, 'unrecognized_format', 'The file %s does not look like a MariaDB error log, so '
                            'nothing from it is shown.' % path, hint)
    # Identity: the latest startup banner must be this server's (version and
    # port) and from this run (logged shortly after its start). Without it
    # nothing from the file is shown.
    banner = latest_banner(entries)
    metadata['banner_found'] = banner is not None
    if banner is None:
        return _unavailable(report, 'identity_unverified', 'The last %s of %s has no startup line, so myflames cannot '
                            'confirm the file is this server\'s log.' % (_bytes_text(TAIL_BYTES), path), hint)
    banner_version, banner_port, banner_logged = banner
    if banner_version is None:
        return _unavailable(report, 'identity_mismatch', 'The latest startup line in %s is incomplete, so the file '
                            'cannot be matched to this server.' % path, hint)
    if (banner_version, banner_port) != (version, port):
        return _unavailable(report, 'identity_mismatch', 'The latest startup line in %s names version %s and port %s, '
                            'not this server, so the file is another server\'s log.'
                            % (path, banner_version, banner_port), hint)
    after_start = _seconds_between(report['server_started'], banner_logged)
    if after_start is None or after_start < -2:
        return _unavailable(report, 'identity_unverified', 'The latest startup line in %s was logged at %s, before this '
                            'server started at %s, so the file cannot be confirmed as its log.'
                            % (path, banner_logged, report['server_started']), hint)
    if after_start > BANNER_WINDOW_S:
        return _unavailable(report, 'identity_mismatch', 'The latest startup line in %s was logged at %s, more than an '
                            'hour after this server started at %s, so it belongs to another start.'
                            % (path, banner_logged, report['server_started']), hint)
    report['counts'], report['counts_current_run'], report['codes'] = aggregate_entries(entries)
    selected = [entry for entry in reversed(entries) if entry['prio'] in report['levels']]
    selected.sort(key=lambda entry: entry['logged'], reverse=True)
    report['entries_total'] = len(selected)
    report['entries'] = [dict(entry, message=entry['message'][:MESSAGE_CHARS]) for entry in selected[:report['limit']]]
    report['source'] = 'file'
    report['notes'].append('Counts and code groups cover only the parsed file tail, not the whole log or rotated files.')
    if warnings is not None:
        report['notes'].append('MariaDB log_warnings=%s controls recording; absent events do not prove none happened.' % warnings)
    return _finish(report)


def error_log_report(conn, limit=DEFAULT_LIMIT, levels=None):
    """Return error-log-1.0; invalid filters raise ValueError before any read."""
    limit, levels = check_limit(limit, MAX_LIMIT), check_levels(levels)
    reader = Reader(conn, ERROR_LOG_TAG)
    report = _new_report(Server({}, {}), limit, levels)
    try:
        server = preflight(reader, SETTINGS)
        report = _new_report(server, limit, levels)
        if server.engine == 'mariadb':
            return _mariadb_report(conn, server, reader, report)
        return _mysql_report(reader, server, report)
    except ConnectorError as exc:
        if denied(exc):
            # Drop whatever earlier reads filled in: an unavailable report
            # carries no counts, codes, entries or buffer.
            report.update(counts=_counts(), counts_current_run=_counts(), codes=[], entries=[], entries_total=0,
                          buffer=None, file=None, source=None, notes=[])
            return _unavailable(report, 'denied', 'The server denied an error log read; MySQL needs SELECT on performance_schema.error_log.')
        raise ValueError('Could not read the error log: ' + reason(exc))


def at_or_worse(report, level):
    """A summary finding when its status meets the threshold; unknown never trips."""
    return [report['summary']] if SEVERITY.get(report['summary']['status'], -1) >= SEVERITY[level] else []


def format_text(report):
    head = 'Error log: %s, server started %s' % (report['source'] or '?', report['server_started'] or '?')
    if report['buffer']:
        buf = report['buffer']
        head += ', buffer %s / %s, %d events, %d expired' % (
            _bytes_text(buf['bytes']), _bytes_text(buf['capacity_bytes']), buf['events'], buf['expired_events'])
    if report['file']:
        info = report['file']
        head += ', read %s of %s (%s)' % (_bytes_text(info['read_bytes']), _bytes_text(info['size_bytes']), info['path'])
    levels = [level for level in LEVELS if report['engine'] != 'mariadb' or level != 'System']
    lines = [head, 'Retained: ' + ', '.join('%d %s' % (report['counts'][level.lower()], level) for level in levels),
             report['summary']['text']]
    if not report['available']:
        if report['how_to_view']:
            lines.append(report['how_to_view'])
    else:
        # InnoDB's text output uses 100 columns. Keep messages on one line;
        # terminal control escaping remains the shared CLI emitter's job.
        rows = [[entry['logged'][:26], entry['prio'], entry['error_code'] or '-', entry['subsystem'] or '-',
                 ' '.join(entry['message'].split())] for entry in report['entries']]
        widths = [max([len(row[i]) for row in rows] + [len(title)])
                  for i, title in enumerate(('TIME', 'LEVEL', 'CODE', 'SUBSYSTEM'))]
        message_width = max(1, 100 - sum(widths) - 8)
        for row in rows:
            if len(row[-1]) > message_width:
                row[-1] = row[-1][:message_width - 1] + '…'
        lines += ['', table_text(['TIME', 'LEVEL', 'CODE', 'SUBSYSTEM', 'MESSAGE'], rows)]
    return '\n'.join(lines) + '\n'
