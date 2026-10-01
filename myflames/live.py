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

from .connector import MySQLConnection, ConnectorError
from .collectors import collect_schema, collect_stats, collect_session_variables, extract_table_names
from .parser import parse_explain

TOKEN = re.compile(r"/\*.*?\*/|--(?=\s|$)[^\r\n]*|\#[^\r\n]*|'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`", re.S)


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
    masked = TOKEN.sub(' ', query)
    if not re.match(r'^\s*(SELECT|WITH)\b', masked, re.I) or ';' in masked:
        raise ValueError('Capture accepts one SELECT or WITH … SELECT statement.')
    if re.search(r'\b(INTO|OUTFILE|DUMPFILE|INSERT|UPDATE|DELETE|REPLACE|CALL|LOAD|LOCK|CREATE|DROP|ALTER|SET)\b', masked, re.I) or '\x00' in query or re.search(r'(?im)^\s*(delimiter|source|system|connect|tee|pager)\b', masked) or ':=' in masked or '\\' in masked or re.search(r'/\*(?:!|M!)', query, re.I):
        raise ValueError('Use a read-only SELECT without assignments, file output or locking clauses.')
    return query


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
        from .ui import _plan
        _plan({'plan': result['raw']})
    for field in ('schema', 'table_stats', 'variables'):
        content = value.get(field, {})
        if not isinstance(content, dict):
            raise ValueError('Capture ' + field + ' must be an object.')
        result[field] = content
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
            from .ui import _plan
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
    json.dumps(result, allow_nan=False)
    return result


class LiveJob:
    def __init__(self, payload):
        self.options = connection_options(payload.get('connection'))
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
        argv = conn._build_argv('', ['--raw', '--unbuffered', '--binary-mode', '--comments', '--force'])[:-2]
        # Feed SQL on stdin; credentials and query literals stay out of argv.
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.process = proc
        lines = []
        def read_output():
            section = ''
            for line in iter(proc.stdout.readline, b''):
                text = line.decode('utf-8', errors='replace')
                lines.append(text)
                if text.strip() == marker + ':connection': section = 'connection'
                elif section == 'connection':
                    if text.strip().isdigit(): self.connection_id = int(text.strip())
                    section = ''
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        timer = threading.Timer(self.timeout, self.cancel)
        timer.daemon = True
        timer.start()
        try:
            proc.stdin.write(sql.encode('utf-8'))
            proc.stdin.close()
            stderr = proc.stderr.read().decode('utf-8', errors='replace')
            proc.wait()
            reader.join(timeout=5)
            if self.cancelled.is_set():
                raise ConnectorError('Capture cancelled or its time limit was reached.')
            if proc.returncode != 0 and not lines:
                raise ConnectorError('MySQL client failed: ' + stderr[:1500])
            return ''.join(lines), stderr
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill(); proc.wait()
            self.process = None
            self.connection_id = None

    def run(self):
        self.state = 'running'
        try:
            with MySQLConnection(**self.options) as conn:
                version = conn.server_version()
                maria = conn.is_mariadb()
                runs, warnings, raw, trace_data = [], [], '', None
                for index in range(self.repeat):
                    if self.cancelled.is_set(): raise ConnectorError('Capture cancelled.')
                    marker = '__myflames_' + uuid.uuid4().hex
                    tag = 'myflames:' + uuid.uuid4().hex
                    section = lambda key: "SELECT '%s:%s';\n" % (marker, key)
                    stmt = ('ANALYZE FORMAT=JSON ' if maria else 'EXPLAIN ANALYZE FORMAT=JSON ') if self.mode == 'analyze' else 'EXPLAIN FORMAT=JSON '
                    script = section('connection') + 'SELECT CONNECTION_ID();\n'
                    if not maria: script += 'SET SESSION explain_json_format_version=2;\n'
                    script += 'SET SESSION TRANSACTION READ ONLY;\nSTART TRANSACTION READ ONLY;\n'
                    if self.trace: script += "SET SESSION optimizer_trace='enabled=on';\n"
                    script += section('before') + "SHOW SESSION STATUS WHERE Variable_name LIKE 'Handler_%' OR Variable_name IN ('Created_tmp_tables','Created_tmp_disk_tables','Sort_merge_passes','Sort_rows','Select_scan');\n"
                    script += section('plan') + '/*' + tag + '*/ ' + stmt + self.query + '\n;\n'
                    if self.trace: script += "SET SESSION optimizer_trace='enabled=off';\n"
                    script += section('after') + "SHOW SESSION STATUS WHERE Variable_name LIKE 'Handler_%' OR Variable_name IN ('Created_tmp_tables','Created_tmp_disk_tables','Sort_merge_passes','Sort_rows','Select_scan');\n"
                    if self.trace:
                        script += section('trace') + 'SELECT TRACE, MISSING_BYTES_BEYOND_MAX_MEM_SIZE, INSUFFICIENT_PRIVILEGES FROM information_schema.OPTIMIZER_TRACE;\n'
                    script += section('statistics') + "SELECT JSON_OBJECT('timer_wait_ps', TIMER_WAIT, 'rows_examined', ROWS_EXAMINED, 'rows_sent', ROWS_SENT, 'tmp_tables', CREATED_TMP_TABLES, 'tmp_disk_tables', CREATED_TMP_DISK_TABLES, 'sort_rows', SORT_ROWS, 'sort_merge_passes', SORT_MERGE_PASSES, 'no_index_used', NO_INDEX_USED) FROM performance_schema.events_statements_history WHERE THREAD_ID=(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID=CONNECTION_ID()) AND SQL_TEXT LIKE '/*" + tag + "*/%' ORDER BY EVENT_ID DESC LIMIT 1;\n"
                    script += section('done') + 'ROLLBACK;\n'
                    output, diagnostics = self._run_script(conn, script, marker)
                    sections = _sections(output, marker)
                    raw = sections.get('plan', '')
                    try:
                        from .ui import _plan
                        _, root, _, _ = _plan({'plan': raw})
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ConnectorError('The server did not return a supported JSON plan. ' + diagnostics[:1500]) from exc
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
                        if self.options.get('password'): diagnostics = diagnostics.replace(self.options['password'], '[redacted]')
                        warnings.append(diagnostics.strip()[:2000])
                    runs.append({'total_time_ms': total, 'statistics': stats,
                                 'session_status_delta': {k: v - before[k] for k, v in after.items() if k in before},
                                 'plan': raw})
                    self.progress = index + 1
                metadata = {}
                for key, collect in (('schema', lambda: collect_schema(conn, extract_table_names(self.query, self.options.get('database')))),
                                     ('table_stats', lambda: collect_stats(conn, extract_table_names(self.query, self.options.get('database')))),
                                     ('variables', lambda: collect_session_variables(conn))):
                    if self.cancelled.is_set(): raise ConnectorError('Capture cancelled.')
                    try: metadata[key] = collect()
                    except ConnectorError: warnings.append(key.replace('_', ' ') + ' could not be collected with this connection.')
                self.result = dict(schema_version='capture-1.0', raw=raw, query=self.query, mode=self.mode, engine='mariadb' if maria else 'mysql',
                    server_version=version, captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    runs=runs, measurements=measurement_summary(runs), trace=trace_data, conditions=self.conditions,
                    warnings=list(dict.fromkeys(warnings)), **metadata)
                self.state = 'complete'
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

    def get(self, key):
        with self.lock:
            job = self.jobs.get(key)
        if not job: raise ValueError('Capture not found. It may have expired.')
        return job

    def close(self):
        for job in list(self.jobs.values()):
            if job.state in ('queued', 'running'): job.cancel()
