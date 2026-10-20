"""Cancellable saved-query capture shared by the UI and capture CLI.

Each measured run uses one database session for EXPLAIN, status deltas,
Performance Schema and optional optimizer trace. Metadata is collected later.
"""
import datetime
import json
import math
import re
import statistics
import subprocess
import threading
import time
import uuid

from .connector import MySQLConnection, ConnectorError, client_environment, MAX_STDOUT_BYTES, MAX_STDERR_BYTES
from .collectors import collect_schema, collect_stats, collect_stats_freshness, collect_session_variables, extract_table_names
from .parser import parse_explain
from .server_info import parse_version_tuple, unsupported_version_note

#: Comments and quoted strings as the server and the mysql client read them by
#: default: a backslash escapes the next character inside '...' and "...".
#: "-- " starts a comment only before the client's ASCII white space (space,
#: tab, newline, vertical tab, form feed, carriage return: my_isspace), never
#: Python's wider \s: the client would not see "--\x1c" or "--\xa0" as a
#: comment and would split at a later ';'. The server lexer also treats "--"
#: before another control character as a comment (sql_lex.cc), so such text
#: is refused as ambiguous (DASH_CONTROL).
TOKEN = re.compile(r"/\*.*?\*/|--(?=[ \t\n\r\f\v]|$)[^\r\n]*|\#[^\r\n]*|'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`", re.S)
#: The same under sql_mode NO_BACKSLASH_ESCAPES, where a backslash is an
#: ordinary character. "..." without backslash escapes is also how the server
#: reads an ANSI_QUOTES identifier.
TOKEN_NO_BACKSLASH = re.compile(r"/\*.*?\*/|--(?=[ \t\n\r\f\v]|$)[^\r\n]*|\#[^\r\n]*|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`", re.S)
#: "--" before a control character that is not white space: the client reads
#: code, the server a comment. Outside strings and comments it is refused.
DASH_CONTROL = re.compile(r"--[\x00-\x08\x0e-\x1f\x7f]")

# These calls can change server/session state or deliberately wait even inside
# a read-only transaction. Match calls, not columns with the same names.
UNSAFE_SELECT_FUNCTION = re.compile(
    r'(?<![\w$])(?:NEXTVAL|SETVAL|GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS|SLEEP|BENCHMARK|'
    r'WAIT_FOR_EXECUTED_GTID_SET|WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS|MASTER_POS_WAIT|SOURCE_POS_WAIT|'
    r'(?:GROUP_REPLICATION_|ASYNCHRONOUS_CONNECTION_FAILOVER_|KEYRING_|AUDIT_LOG_|FIREWALL_|FLUSH_)[\w$]*)\s*\(', re.I)
QUALIFIED_SELECT_FUNCTION = re.compile(r'(?<![\w$])[\w$]+\s*\.\s*[\w$]+\s*\(')


def _function_token(match):
    """Mask literals/comments but retain identifiers for the function guard.

    Double quotes can name identifiers under ANSI_QUOTES. Normalize unusual
    quoted names to one identifier so qualified calls cannot hide in quotes;
    keep simple names so quoted SLEEP and other blocked calls are recognized.
    """
    token = match.group()
    if token[0] in ('`', '"'):
        name = token[1:-1]
        return name if re.fullmatch(r'[\w$]+', name) else '__quoted_identifier__'
    return ' '


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (ValueError, OverflowError):
        return False


def integer(value, label):
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise ValueError(label + ' must be an integer.')
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(label + ' must be an integer.')


def connection_options(value):
    if not isinstance(value, dict):
        raise ValueError('Enter connection settings.')
    out = {}
    for name in ('host', 'user', 'database', 'ssl_mode', 'ssl_ca', 'password'):
        v = value.get(name)
        if v is None or v == '':
            continue
        if not isinstance(v, str) or len(v) > 4096 or (name != 'password' and any(c in v for c in '\n\r\x00')):
            raise ValueError('Invalid connection field: ' + name)
        out[name] = v
    out.setdefault('host', '127.0.0.1')
    if out.get('ssl_mode') not in (None, 'DISABLED', 'PREFERRED', 'REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
        raise ValueError('Choose a supported TLS mode.')
    port = integer(value.get('port', 3306), 'Port')
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    out['port'] = port
    out['connect_timeout'] = 8
    out['query_timeout'] = 12
    return out


def bind_query(query, parameters=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 200000:
        raise ValueError('Enter a SELECT statement of at most 200,000 characters.')
    params = {} if parameters is None else parameters
    if not isinstance(params, dict):
        raise ValueError('Parameters must be a JSON object.')
    def literal(match):
        key = match.group(1)
        if key not in params:
            raise ValueError('Missing parameter: ' + key)
        value = params[key]
        if value is None: return 'NULL'
        if isinstance(value, bool): return '1' if value else '0'
        if finite_number(value): return str(value)
        if isinstance(value, str): return "CONVERT(X'%s' USING utf8mb4)" % value.encode('utf-8').hex()
        raise ValueError('Parameter %s must be a finite number, string, boolean or null.' % key)
    parts, at = [], 0
    for token in TOKEN.finditer(query):
        parts.extend([re.sub(r'(?<!:):([A-Za-z_][A-Za-z0-9_]*)', literal, query[at:token.start()]), token.group()])
        at = token.end()
    parts.append(re.sub(r'(?<!:):([A-Za-z_][A-Za-z0-9_]*)', literal, query[at:]))
    query = ''.join(parts).strip().rstrip(';').strip()
    problem = _select_problem(query)
    if problem:
        raise ValueError(problem)
    return query


def _select_problem(query):
    """Why capture refuses an already trimmed *query*, or None.

    The mysql client splits the capture script into statements itself, and it
    reads a backslash inside a string as an escape unless the session's
    sql_mode has NO_BACKSLASH_ESCAPES (client/mysql.cc add_line checks
    SERVER_STATUS_NO_BACKSLASH_ESCAPES). myflames cannot know that mode before
    the client runs, so the text must mean the same under both readings: its
    strings, identifiers and comments must start and end at the same places
    with and without backslash escapes. Otherwise ``SELECT '\\'; DROP ...``
    is one string to one reading and several statements to the other. When
    the boundaries agree, so does every mix (ANSI_QUOTES reads "..." without
    escapes and '...' with them), and the checks below see what the server
    sees. Backslash sequences that do not move a boundary ('a\\nb', 'C:\\\\x')
    are accepted; a quote escaped with a backslash ('it\\'s') is not, so
    double it instead ('it''s').
    """
    if [m.span() for m in TOKEN.finditer(query)] != [m.span() for m in TOKEN_NO_BACKSLASH.finditer(query)]:
        return ("A backslash before a quote is read differently when sql_mode has NO_BACKSLASH_ESCAPES. "
                "Write a quote inside a string by doubling it ('it''s'), not with a backslash.")
    masked = TOKEN.sub(' ', query)
    if DASH_CONTROL.search(masked):
        return ('Write "-- " comments with a space after the dashes; before a control character the client and '
                'the server read them differently.')
    if re.search(r"['\"`]|/\*", masked):
        # An unclosed string or comment would run on into the statements
        # myflames sends after the query.
        return 'Close every quoted string, quoted identifier and comment.'
    if not re.match(r'^\s*(SELECT|WITH)\b', masked, re.I) or ';' in masked:
        return 'Capture accepts one SELECT or WITH … SELECT statement.'
    if re.search(r'\b(INTO|OUTFILE|DUMPFILE|INSERT|UPDATE|DELETE|REPLACE|CALL|LOAD|LOCK|CREATE|DROP|ALTER|SET)\b', masked, re.I) or re.search(r'\bFOR\s+SHARE\b', masked, re.I) or '\x00' in query or re.search(r'(?im)^\s*(delimiter|source|system|connect|tee|pager)\b', masked) or ':=' in masked or '\\' in masked or re.search(r'/\*(?:!|M!)', query, re.I):
        return 'Use a read-only SELECT without assignments, file output or locking clauses.'
    # Import after the lexical definitions are loaded: dbops reuses this scanner.
    from .dbops import classify
    verdict = classify(query)
    if verdict['kind'] != 'read':
        return 'Capture requires a read-only SELECT. ' + ' '.join(verdict['reasons'])
    return None


def _version_at_least(version, minimum):
    """True when the leading major.minor of *version* is at least *minimum*,
    or when it cannot be read."""
    parsed = parse_version_tuple(version, leading=True, components=2, default=None)
    return parsed is None or parsed >= minimum


ANALYZE_JSON_NEEDS_84 = 'EXPLAIN ANALYZE FORMAT=JSON needs MySQL 8.4 or later'


def client_error_text(text, mode='explain', version='', maria=False):
    """Make mysql client diagnostics readable for a capture.

    The client numbers lines of the script myflames wraps around the user's
    statement ("at line 8"), which means nothing to the user, so the
    position is dropped. Error 1235 (ER_NOT_SUPPORTED_YET) for EXPLAIN
    ANALYZE on MySQL before 8.4 becomes a plain version message. Source:
    MySQL sql/opt_explain.cc:2296-2302 raises ER_NOT_SUPPORTED_YET for an
    EXPLAIN ANALYZE format the server cannot run; the 8.0 server reported
    it for the JSON format (seen live on 8.0), and the hierarchical JSON
    format with ANALYZE is what 8.4 and later accept.
    """
    text = re.sub(r'(ERROR \d+ \([0-9A-Za-z]+\)) at line \d+:', r'\1:', text or '')
    if mode == 'analyze' and not maria and 'ERROR 1235' in text and not _version_at_least(version, (8, 4)):
        return (ANALYZE_JSON_NEEDS_84 + ' (this server is %s); '
                'capture with --mode explain instead.' % (str(version).split('-')[0] or 'older'))
    return text


def capture_problem(query):
    """The reason capture would refuse *query*, or None when it is one
    read-only SELECT or WITH … SELECT. The same rule :func:`bind_query`
    enforces, without binding parameters, so ``?`` and ``:name``
    placeholders pass."""
    if not isinstance(query, str) or not query.strip():
        return 'No statement text was recorded.'
    if len(query) > 200000:
        return 'Enter a SELECT statement of at most 200,000 characters.'
    return _select_problem(query.strip().rstrip(';').strip())


#: Performance Schema statement digests: MD5 (32 hex, MySQL 5.7 and MariaDB)
#: or SHA-256 (64 hex, MySQL 8.0+).
DIGEST_HEX = re.compile(r'^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})$')


def normalize_source(value):
    """Validate the optional capture ``source``: where the evidence came from.

    Either a short text label (``import-shell`` writes ``"mysqlsh-diagnostics"``)
    or ``{"kind": "digest", "digest": HEX, "schema": NAME or null}``, recorded
    when a capture starts from a Performance Schema statement digest.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not 0 < len(value) <= 100 or any(ord(c) < 32 for c in value):
            raise ValueError('Capture source labels are 1 to 100 printable characters.')
        return value
    if not isinstance(value, dict) or value.get('kind') != 'digest':
        raise ValueError('Capture source must be a text label or {"kind": "digest", "digest": ..., "schema": ...}.')
    digest = value.get('digest')
    if not isinstance(digest, str) or not DIGEST_HEX.match(digest):
        raise ValueError('Capture source digest must be a 32 or 64 character hexadecimal statement digest.')
    schema = value.get('schema')
    if schema is not None and (not isinstance(schema, str) or len(schema) > 64 or '\x00' in schema):
        raise ValueError('Capture source schema must be text of at most 64 characters, or null.')
    return {'kind': 'digest', 'digest': digest.lower(), 'schema': schema or None}


def _sections(text, marker):
    result, current = {}, None
    prefix = marker + ':'
    for line in text.splitlines():
        if line.startswith(prefix):
            current = line[len(prefix):]
            result[current] = []
        elif current:
            result[current].append(line)
    return {k: '\n'.join(v).strip() for k, v in result.items()}


def _status(text):
    values = {}
    for line in text.splitlines():
        parts = line.split('\t')
        if len(parts) == 2:
            try: values[parts[0]] = int(parts[1])
            except ValueError: pass
    return values


def measurement_summary(runs):
    values = [r['total_time_ms'] for r in runs if r.get('total_time_ms') is not None]
    return {'count': len(values), 'median_ms': statistics.median(values) if values else None,
            'min_ms': min(values) if values else None, 'max_ms': max(values) if values else None,
            'stdev_ms': statistics.stdev(values) if len(values) > 1 else 0 if values else None}


def normalize_capture(value):
    """Validate imported evidence and fill unavailable fields without inventing data."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Capture metadata must be an object.')
    mode = value.get('mode', 'explain')
    if mode not in ('explain', 'analyze'):
        raise ValueError('Unknown capture mode.')
    result = {'schema_version': 'capture-1.0', 'mode': mode}
    for field in ('raw', 'query', 'engine', 'server_version', 'captured_at', 'conditions'):
        content = value.get(field, '')
        if not isinstance(content, str):
            raise ValueError('Capture ' + field + ' must be text.')
        result[field] = content
    if result['raw']:
        from .plan_input import _plan
        _plan({'plan': result['raw']})
    for field in ('schema', 'table_stats', 'variables', 'stats_freshness'):
        content = value.get(field, {})
        if not isinstance(content, dict):
            raise ValueError('Capture ' + field + ' must be an object.')
        result[field] = content
    for entry in result['stats_freshness'].values():
        if (not isinstance(entry, dict)
                or not isinstance(entry.get('last_update'), (str, type(None)))
                or not (entry.get('n_rows') is None or (isinstance(entry.get('n_rows'), int)
                                                       and not isinstance(entry.get('n_rows'), bool)))
                or not isinstance(entry.get('histograms', []), list)
                or any(not isinstance(c, str) for c in entry.get('histograms', []))):
            raise ValueError('Statistics freshness entries need last_update text, an integer n_rows, and a list of histogram column names.')
    warnings = value.get('warnings', [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
        raise ValueError('Capture warnings must be a list of text notices.')
    result['warnings'] = warnings
    runs = value.get('runs', [])
    if not isinstance(runs, list) or len(runs) > 10:
        raise ValueError('Capture runs must be a list with at most ten entries.')
    result['runs'] = []
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError('Every captured run must be an object.')
        total = run.get('total_time_ms')
        if total is not None and (not finite_number(total) or total < 0):
            raise ValueError('Measured time must be a finite nonnegative number or null.')
        stats, delta = run.get('statistics'), run.get('session_status_delta', {})
        if stats is not None and not isinstance(stats, dict) or not isinstance(delta, dict):
            raise ValueError('Run counters must be objects.')
        for counters in (stats or {}, delta):
            if any(not finite_number(v) for v in counters.values()):
                raise ValueError('Run counters must contain finite numbers.')
        raw = run.get('plan', '')
        if not isinstance(raw, str):
            raise ValueError('Run plans must be JSON text.')
        if raw:
            from .plan_input import _plan
            _plan({'plan': raw})
        result['runs'].append({'total_time_ms': total if mode == 'analyze' else None,
                              'statistics': stats, 'session_status_delta': delta, 'plan': raw})
    result['measurements'] = measurement_summary(result['runs'])
    trace = value.get('trace')
    if trace is not None:
        if not isinstance(trace, dict) or 'data' not in trace:
            raise ValueError('Optimizer trace must contain its data object.')
        trace = {'data': trace['data'],
                 'missing_bytes': integer(trace.get('missing_bytes', 0), 'Missing trace bytes'),
                 'insufficient_privileges': integer(trace.get('insufficient_privileges', 0), 'Trace privileges')}
        if trace['missing_bytes'] < 0 or trace['insufficient_privileges'] not in (0, 1):
            raise ValueError('Invalid optimizer trace availability metadata.')
    result['trace'] = trace
    source = normalize_source(value.get('source'))
    if source:
        result['source'] = source
    json.dumps(result, allow_nan=False)
    return result


class LiveJob:
    def __init__(self, payload):
        from . import dbops
        self.options = connection_options(payload.get('connection'))
        # Keep the non-secret owner independently of the mutable client options.
        self.identity = dbops.identity_endpoint(dbops.connection_identity(payload.get('connection')))
        self.query = bind_query(payload.get('query'), payload.get('parameters'))
        self.mode = payload.get('mode', 'explain')
        if self.mode not in ('explain', 'analyze'):
            raise ValueError('Mode must be explain or analyze.')
        self.repeat = integer(payload.get('repeat', 1), 'Run count')
        self.timeout = integer(payload.get('timeout', 30), 'Time limit')
        if not 1 <= self.repeat <= 10 or not 1 <= self.timeout <= 300:
            raise ValueError('Choose 1–10 runs and a timeout of 1–300 seconds per run.')
        if self.mode == 'explain': self.repeat = 1
        self.trace = bool(payload.get('trace', False))
        self.conditions = str(payload.get('conditions') or '')[:2000]
        self.source = normalize_source(payload.get('source'))
        self.id = uuid.uuid4().hex
        self.state = 'queued'
        self.error = ''
        self.result = None
        self.progress = 0
        self.cancelled = threading.Event()
        self.process = None
        self.connection_id = None
        self.created = time.monotonic()

    def snapshot(self):
        return {'id': self.id, 'state': self.state, 'error': self.error,
                'progress': self.progress, 'repeat': self.repeat, 'result': self.result}

    def cancel(self):
        self.cancelled.set()
        # Kill only the connection owned by this job, never another user's query.
        connection_id = self.connection_id
        if connection_id:
            try:
                with MySQLConnection(**self.options) as control:
                    control.run('KILL QUERY %d' % connection_id, timeout=5)
            except ConnectorError: pass
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def _run_script(self, conn, sql, marker):
        with conn._invocation_defaults() as defaults_path, client_environment() as env:
            return self._run_script_isolated(conn, sql, marker, env, defaults_path)

    def _run_script_isolated(self, conn, sql, marker, env, defaults_path):
        # No --force: the client stops at the first failing statement, so a
        # failed SET SESSION TRANSACTION READ ONLY or START TRANSACTION READ
        # ONLY never lets the query run outside the read-only transaction.
        # Statements after the query (trace, statistics) may fail; the session
        # ends with the client and its transaction rolls back.
        argv = conn._build_argv('', ['--raw', '--unbuffered', '--binary-mode', '--comments'],
                                defaults_path=defaults_path)[:-2]
        # Feed SQL on stdin; credentials and query literals stay out of argv.
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        self.process = proc
        out, err, failed = bytearray(), bytearray(), []
        def drain(pipe, buffer, limit, label, watch):
            # Bounded chunks with a cumulative limit, like the connector's own
            # reader: a huge plan or trace must not exhaust memory.
            tail, section = b'', ''
            try:
                while True:
                    chunk = pipe.read1(65536)
                    if not chunk: break
                    if len(buffer) + len(chunk) > limit:
                        failed.append(ConnectorError(
                            'Lost connection to the MySQL client: %s response exceeds the %d-byte limit; '
                            'the client was stopped and the outcome is unknown.' % (label, limit)))
                        proc.kill()
                        break
                    buffer.extend(chunk)
                    if not watch: continue
                    # Only the short marker lines matter; never keep a long partial line.
                    pieces = (tail + chunk).split(b'\n')
                    tail = pieces.pop()[-512:]
                    for piece in pieces:
                        text = piece.decode('utf-8', errors='replace').strip()
                        if text == marker + ':connection': section = 'connection'
                        elif section == 'connection':
                            if text.isdigit(): self.connection_id = int(text)
                            section = ''
            except (OSError, ValueError):
                if not failed:
                    failed.append(ConnectorError('Lost connection to the MySQL client while reading; '
                                                 'the outcome is unknown.'))
            finally:
                pipe.close()
        readers = [threading.Thread(target=drain, args=(proc.stdout, out, MAX_STDOUT_BYTES, 'stdout', True),
                                    daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, err, MAX_STDERR_BYTES, 'stderr', False),
                                    daemon=True)]
        timer = threading.Timer(self.timeout, self.cancel)
        timer.daemon = True
        timer.start()
        try:
            for reader in readers: reader.start()
            try:
                proc.stdin.write(sql.encode('utf-8'))
                proc.stdin.close()
            except OSError:
                if not failed: raise
            for reader in readers: reader.join()
            proc.wait()
            if failed:
                raise failed[0]
            stderr = err.decode('utf-8', errors='replace')
            output = out.decode('utf-8', errors='replace')
            if self.cancelled.is_set():
                raise ConnectorError('Capture cancelled or its time limit was reached.')
            if proc.returncode != 0 and not output:
                raise ConnectorError('MySQL client failed: ' + client_error_text(stderr)[:1500])
            return output, stderr
        except KeyboardInterrupt:
            # Ctrl+C: stop the statement on the server while the connection ID
            # and the password are still known. Ending only the local client
            # (the finally below) would leave the query running there.
            self.cancel()
            raise
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill(); proc.wait()
            for reader in readers: reader.join(timeout=5)
            self.process = None
            self.connection_id = None

    def run(self):
        self.state = 'running'
        try:
            with MySQLConnection(**self.options) as conn:
                version = conn.server_version()
                maria = conn.is_mariadb()
                runs, warnings, raw, trace_data = [], [], '', None
                note = unsupported_version_note(version, 'mariadb' if maria else '')
                if note: warnings.append(note)
                for index in range(self.repeat):
                    if self.cancelled.is_set(): raise ConnectorError('Capture cancelled.')
                    marker = '__myflames_' + uuid.uuid4().hex
                    tag = 'myflames:' + uuid.uuid4().hex
                    # Every helper statement carries the capture tag so `myflames top`
                    # can drop them from MySQL's digest samples (discover._excluded).
                    own = '/*myflames:capture*/ '
                    section = lambda key: "%sSELECT '%s:%s';\n" % (own, marker, key)
                    stmt = ('ANALYZE FORMAT=JSON ' if maria else 'EXPLAIN ANALYZE FORMAT=JSON ') if self.mode == 'analyze' else 'EXPLAIN FORMAT=JSON '
                    script = section('connection') + own + 'SELECT CONNECTION_ID();\n'
                    # explain_json_format_version exists from MySQL 8.3.0; an
                    # older server would stop the script here.
                    if not maria and _version_at_least(version, (8, 3)):
                        script += own + 'SET SESSION explain_json_format_version=2;\n'
                    script += own + 'SET SESSION TRANSACTION READ ONLY;\n' + own + 'START TRANSACTION READ ONLY;\n'
                    if self.trace: script += own + "SET SESSION optimizer_trace='enabled=on';\n"
                    script += section('before') + own + "SHOW SESSION STATUS WHERE Variable_name LIKE 'Handler_%' OR Variable_name IN ('Created_tmp_tables','Created_tmp_disk_tables','Sort_merge_passes','Sort_rows','Select_scan','Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests','Innodb_buffer_pool_read_ahead');\n"
                    script += section('plan') + '/*' + tag + '*/ ' + stmt + self.query + '\n;\n'
                    if self.trace: script += own + "SET SESSION optimizer_trace='enabled=off';\n"
                    script += section('after') + own + "SHOW SESSION STATUS WHERE Variable_name LIKE 'Handler_%' OR Variable_name IN ('Created_tmp_tables','Created_tmp_disk_tables','Sort_merge_passes','Sort_rows','Select_scan','Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests','Innodb_buffer_pool_read_ahead');\n"
                    if self.trace:
                        script += section('trace') + own + 'SELECT TRACE, MISSING_BYTES_BEYOND_MAX_MEM_SIZE, INSUFFICIENT_PRIVILEGES FROM information_schema.OPTIMIZER_TRACE;\n'
                    script += section('statistics') + own + "SELECT JSON_OBJECT('timer_wait_ps', TIMER_WAIT, 'rows_examined', ROWS_EXAMINED, 'rows_sent', ROWS_SENT, 'tmp_tables', CREATED_TMP_TABLES, 'tmp_disk_tables', CREATED_TMP_DISK_TABLES, 'sort_rows', SORT_ROWS, 'sort_merge_passes', SORT_MERGE_PASSES, 'no_index_used', NO_INDEX_USED) FROM performance_schema.events_statements_history WHERE THREAD_ID=(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID=CONNECTION_ID()) AND SQL_TEXT LIKE '/*" + tag + "*/%' ORDER BY EVENT_ID DESC LIMIT 1;\n"
                    script += section('done') + own + 'ROLLBACK;\n'
                    output, diagnostics = self._run_script(conn, script, marker)
                    sections = _sections(output, marker)
                    raw = sections.get('plan', '')
                    try:
                        from .plan_input import _plan
                        _, root, _, _ = _plan({'plan': raw})
                    except (ValueError, TypeError, KeyError) as exc:
                        cleaned = client_error_text(diagnostics, self.mode, version, maria)[:1500]
                        raise ConnectorError(cleaned if cleaned.startswith(ANALYZE_JSON_NEEDS_84) else
                                             'The server did not return a supported JSON plan. ' + cleaned) from exc
                    total = root['total_time'] if self.mode == 'analyze' and (root.get('details') or {}).get('actual_last_row_ms') is not None else None
                    before, after = _status(sections.get('before', '')), _status(sections.get('after', ''))
                    stats = None
                    try: stats = json.loads(sections.get('statistics', ''))
                    except ValueError: warnings.append('Statement statistics unavailable: check Performance Schema instruments, history consumer, and privileges.')
                    if self.trace:
                        trace_text = sections.get('trace', '')
                        try:
                            body, missing, denied = trace_text.rsplit('\t', 2)
                            trace_data = {'data': json.loads(body), 'missing_bytes': int(missing), 'insufficient_privileges': int(denied)}
                        except (ValueError, TypeError): warnings.append('Optimizer trace was not available for this capture.')
                    if diagnostics:
                        diagnostics = client_error_text(diagnostics, self.mode, version, maria)
                        if self.options.get('password'): diagnostics = diagnostics.replace(self.options['password'], '[redacted]')
                        warnings.append(diagnostics.strip()[:2000])
                    runs.append({'total_time_ms': total, 'statistics': stats,
                                 'session_status_delta': {k: v - before[k] for k, v in after.items() if k in before},
                                 'plan': raw})
                    self.progress = index + 1
                metadata = {}
                for key, collect in (('schema', lambda: collect_schema(conn, extract_table_names(self.query, self.options.get('database')))),
                                     ('table_stats', lambda: collect_stats(conn, extract_table_names(self.query, self.options.get('database')))),
                                     ('stats_freshness', lambda: collect_stats_freshness(conn, extract_table_names(self.query, self.options.get('database')), mariadb=maria)),
                                     ('variables', lambda: collect_session_variables(conn))):
                    if self.cancelled.is_set(): raise ConnectorError('Capture cancelled.')
                    try: metadata[key] = collect()
                    except ConnectorError: warnings.append(key.replace('_', ' ') + ' could not be collected with this connection.')
                self.result = dict(schema_version='capture-1.0', raw=raw, query=self.query, mode=self.mode, engine='mariadb' if maria else 'mysql',
                    server_version=version, captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    runs=runs, measurements=measurement_summary(runs), trace=trace_data, conditions=self.conditions,
                    warnings=list(dict.fromkeys(warnings)), **metadata)
                if self.source:
                    self.result['source'] = dict(self.source) if isinstance(self.source, dict) else self.source
                self.state = 'complete'
        except KeyboardInterrupt:
            self.cancelled.set()
            self.state, self.error = 'cancelled', 'Capture cancelled.'
            raise
        except Exception as exc:
            self.error = str(exc)
            if self.options.get('password'): self.error = self.error.replace(self.options['password'], '[redacted]')
            self.error = self.error[:2500]
            self.state = 'cancelled' if self.cancelled.is_set() else 'failed'
        finally:
            self.options.pop('password', None)


class LiveJobs:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, payload):
        job = LiveJob(payload)
        with self.lock:
            active = [j for j in self.jobs.values() if j.state in ('queued', 'running')]
            if len(active) >= 2: raise ValueError('Two captures are running. Wait or cancel one first.')
            self.jobs = {k: j for k, j in self.jobs.items() if j in active or time.monotonic() - j.created < 3600}
            if len(self.jobs) >= 50:
                oldest = next(k for k, j in self.jobs.items() if j not in active)
                del self.jobs[oldest]
            self.jobs[job.id] = job
        threading.Thread(target=job.run, daemon=True).start()
        return {'id': job.id, 'state': 'queued'}

    def get(self, key, identity=None):
        """Look up a capture only for the endpoint that started it."""
        with self.lock:
            job = self.jobs.get(key) if isinstance(key, str) else None
        if job is None or identity is None or job.identity != identity:
            raise ValueError('Capture not found. It may have expired.')
        return job

    def close(self):
        for job in list(self.jobs.values()):
            if job.state in ('queued', 'running'): job.cancel()
