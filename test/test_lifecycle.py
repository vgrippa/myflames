"""Lifecycle paired tests: real fake executables, strict replay SQL, no live DB.

All child processes and files belong to a TemporaryDirectory; no real user
workspace, keychain, server or container is contacted. Fake executables record
argv/env and credential file metadata, never its contents or secret stdin.
"""
import ast
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from myflames import connector, dbops, lifecycle as lc, privileges
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore

FIXTURES = Path(__file__).parent / 'fixtures' / 'lifecycle'
PASSWORD = "secret'with spaces"
DONOR_PASSWORD = "donor'private"
CONNECTION = {'host': '127.0.0.1', 'port': 3306, 'user': 'operator', 'password': PASSWORD}
CATALOG = [['app', 't', 'BASE TABLE', 'InnoDB', '100', '20'],
           ['app', 'v', 'VIEW', None, '0', '0']]


def server_variables(engine='mysql', identity='donor', version=None):
    return {'version': version or ('11.4.5-MariaDB' if engine == 'mariadb' else '8.4.3'),
            'version_comment': engine, 'hostname': identity, 'server_id': '1' if identity == 'donor' else '2',
            'server_uuid': identity if engine == 'mysql' else '', 'port': '3306',
            'datadir': '/server/data/', 'socket': '/server/mysql.sock', 'local_infile': 'ON',
            'version_compile_os': 'Linux', 'version_compile_machine': 'x86_64',
            'character_set_server': 'utf8mb4', 'character_set_filesystem': 'binary',
            'collation_server': 'utf8mb4_0900_ai_ci', 'innodb_page_size': '16384',
            'max_allowed_packet': '4194304', 'clone_valid_donor_list': '127.0.0.1:3306',
            'sql_mode': 'STRICT_TRANS_TABLES,NO_ZERO_DATE', 'gtid_executed': 'a:1-2'}


def _tsv(rows):
    return ''.join('\t'.join('NULL' if cell is None else str(cell) for cell in row) + '\n' for row in rows)


class ReplayConnection(object):
    def __init__(self, variables=None, catalog=None, replay=None, restored_marker=None):
        self.variables = dict(variables or server_variables())
        self.catalog = CATALOG if catalog is None else catalog
        self.replay = replay or []
        self.calls = []
        self.restored_marker = restored_marker
        self.identity_reads = 0
        self.change_identity_at = None
        self.denied = None
        self.row_count = 2
        self.clone_state = 'Completed'
        self.grants = ['GRANT ALL PRIVILEGES ON *.* TO `operator`@`%`',
                       'GRANT BACKUP_ADMIN, CLONE_ADMIN ON *.* TO `operator`@`%`']
        self.plugins = [['clone', 'ACTIVE', 'mysql_clone.so']]
        self.closed = False
        self.clone_source = '127.0.0.1:3306'
        self.clone_destination = 'LOCAL INSTANCE'
        self.clone_begin = '2026-10-15 10:00:01.000'
        self.clone_gtid = 'a:1-2'
        self.down_reads = 0
        self.down_error = 'ERROR 2003 (HY000): Can\'t connect to MySQL server'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, extra_flags))
        if sql.startswith(privileges.TAG):
            if 'myflames_who' in sql:
                return _tsv([['myflames_who', 'operator@local', 'operator@%',
                              getattr(self, 'current_roles', 'NONE'), self.variables['version']]] +
                            [[g] for g in self.grants])
            if 'APPLICABLE_ROLES' in sql:
                return ''
        if not sql.startswith(lc.TAG):
            raise AssertionError('Missing lifecycle SQL tag: ' + sql)
        body = sql[len(lc.TAG):]
        if self.denied and self.denied in body:
            raise ConnectorError('ERROR 1142 SELECT command denied')
        if body.startswith('SHOW GLOBAL VARIABLES') and '7365727665725f75756964' in body:
            self.identity_reads += 1
            values = dict(self.variables)
            if self.change_identity_at and self.identity_reads >= self.change_identity_at:
                values['server_uuid'] = 'replacement'
            return _tsv(values.items())
        if body == 'SELECT USER(), CURRENT_USER() LIMIT 1':
            return 'operator@local\toperator@%\n'
        if body.startswith('SHOW GRANTS FOR CURRENT_USER() USING '):
            if getattr(self, 'deny_roles', False):
                raise ConnectorError('ERROR 1142: role grants unavailable')
            return _tsv([[g] for g in getattr(self, 'expanded_grants', self.grants)])
        if body == 'SHOW GRANTS':
            return _tsv([[g] for g in self.grants])
        if body.startswith('SELECT TABLE_SCHEMA,TABLE_NAME,TABLE_TYPE,ENGINE,COALESCE'):
            if self.restored_marker and os.path.exists(self.restored_marker):
                return _tsv(CATALOG)
            return _tsv(self.catalog)
        if body == 'SELECT @@lower_case_table_names':
            if 'lower_case_table_names' not in self.variables:
                raise ConnectorError('ERROR 1193 (HY000): Unknown system variable')
            return str(self.variables['lower_case_table_names']) + '\n'
        if body.startswith('SELECT COUNT(*) FROM '):
            return str(self.row_count) + '\n'
        if body.startswith('SELECT ID,USER,HOST,TIME,STATE FROM information_schema.PROCESSLIST'):
            return ''
        if body.startswith('SELECT PLUGIN_NAME,PLUGIN_STATUS,PLUGIN_LIBRARY'):
            return _tsv(self.plugins)
        if body == 'SELECT COLLATION_NAME FROM information_schema.COLLATIONS':
            return 'utf8mb4_0900_ai_ci\n'
        if body == 'SELECT NOW(3)':
            return '2026-10-15 10:00:00.000\n'
        if body.startswith(('SELECT STAGE,STATE,ESTIMATE', 'SELECT STATE,ERROR_NO')) and self.down_reads:
            self.down_reads -= 1
            raise ConnectorError(self.down_error)
        if body.startswith('SELECT STAGE,STATE,ESTIMATE,DATA,NETWORK,DATA_SPEED'):
            return 'FILE COPY\tCompleted\t100\t100\t100\t1\n'
        if body.startswith('SELECT STATE,ERROR_NO,ERROR_MESSAGE,BINLOG_FILE,BINLOG_POSITION,GTID_EXECUTED,'
                           'BEGIN_TIME,SOURCE,DESTINATION'):
            return _tsv([[self.clone_state, 0, '', 'binlog.1', 4, self.clone_gtid, self.clone_begin,
                          self.clone_source, self.clone_destination]])
        if body.startswith('KILL QUERY '):
            return ''
        for entry in self.replay:
            if entry['contains'] in body:
                return entry['result']
        raise AssertionError('Unexpected SQL: ' + body)


class StrictReplay(object):
    """Exact recorded SQL replies plus explicitly labelled synthetic supplements."""
    def __init__(self, calls, deny=None):
        self.answers = {call['sql']: call['result'] for call in calls}
        self.deny = deny
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, extra_flags))
        if self.deny and self.deny in sql:
            raise ConnectorError('ERROR 1142 (42000): SELECT command denied to user')
        if sql not in self.answers:
            raise AssertionError('unexpected SQL: ' + sql)
        return self.answers[sql]


FAKE_TOOL = r'''
import hashlib, json, os, re, signal, stat, sys
from pathlib import Path
root = Path(__file__).parent
name = Path(sys.argv[0]).name
versions = {'mysqlsh':'mysqlsh Ver 9.6.0', 'mysqldump':'mysqldump Ver 8.4.3',
            'mysqlpump':'mysqlpump Ver 8.0.36', 'mysql':'mysql Ver 8.4.3',
            'mariadb':'mariadb Ver 15.1 Distrib 11.4.5-MariaDB',
            'mariadb-dump':'mariadb-dump Ver 10.19 Distrib 11.4.5-MariaDB',
            'mariadb-backup':'mariadb-backup based on MariaDB server 11.4.5-MariaDB',
            'mariadb-upgrade':'mariadb-upgrade Ver 2.0.0 Distrib 11.4.5-MariaDB'}
if '--version' in sys.argv:
    print(versions[name]); sys.exit(0)
assert sys.argv[1].startswith('--defaults-file=')
cnf = sys.argv[1].split('=',1)[1]
config = Path(cnf).read_text(encoding="utf-8")
stdin = sys.stdin.read()
record = {'name':name, 'argv':sys.argv, 'env':dict(os.environ), 'cnf':cnf,
          'cnf_mode':stat.S_IMODE(os.stat(cnf).st_mode), 'password_seen':'password=' in config,
          'stdin_hash':hashlib.sha256(stdin.encode()).hexdigest(),
          'donor_secret_on_stdin':"donor''private" in stdin,
          'sql_on_argv':any('CLONE ' in arg or 'SELECT ' in arg for arg in sys.argv)}
with (root/'calls.jsonl').open('a') as log:
    log.write(json.dumps(record)+'\n')
mode = (root/'mode').read_text(encoding="utf-8") if (root/'mode').exists() else 'ok'
if mode == 'echo':
    print("secret'with spaces donor'private IDENTIFIED BY 'unknown-secret'", file=sys.stderr, flush=True)
if mode == 'fail':
    print('failure',file=sys.stderr); sys.exit(3)
if name == 'mysqlsh' and 'check-for-server-upgrade' in sys.argv:
    print((root/'upgrade.json').read_text(encoding="utf-8")); sys.exit(0)
if mode == 'cr':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print('42% copied',end='\r',file=sys.stderr,flush=True)
    while True: signal.pause()
if mode == 'long':
    print('x'*8200+"secret'with spaces",file=sys.stderr,flush=True)
if mode == 'tree':
    # A helper in the tool's process group that also ignores SIGTERM.
    import subprocess
    helper = subprocess.Popen([sys.executable, '-c', 'import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)'])
    (root/'grandchild').write_text(str(helper.pid))
    mode = 'slow'
if mode == 'slow':
    marker = re.search(r"(myflames_job_[a-z0-9]+:)", stdin)
    if marker: print(marker.group(1)+'41',flush=True)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print('25% waiting',file=sys.stderr,flush=True)
    (root/'started').touch()
    while True: signal.pause()
if name.endswith('dump') or name == 'mysqldump':
    print('25% dumped',file=sys.stderr,flush=True)
    print('CREATE DATABASE IF NOT EXISTS app;\nUSE app;\nCREATE TABLE t(id INT);\nINSERT INTO t VALUES (1),(2);')
    print('100% dumped',file=sys.stderr,flush=True)
elif name == 'mysqlsh':
    # JavaScript print() adds no line break; only an explicit "\\n" ends the line.
    marker = re.search(r'print\("(myflames_job_[^"]+:)" \+ session.getConnectionId\(\)( \+ "\\n")?\)', stdin)
    if marker: print(marker.group(1)+'41', end='\n' if marker.group(2) else '', flush=True)
    print('Initializing - done', flush=True)
    print('\x1b[36mNOTE: \x1b[0mProgress information uses estimated values', file=sys.stderr, flush=True)
    call = re.search(r'util\.(dumpInstance|dumpSchemas|dumpTables|loadDump)\((.*)\);?',stdin)
    if call:
        args, rest, decoder = [], call.group(2).rstrip(' ;\n'), json.JSONDecoder()
        while rest:
            arg, end = decoder.raw_decode(rest); args.append(arg); rest = rest[end:].lstrip(', ')
        options = args[-1]
        if not options['dryRun']:
            if call.group(1) == 'loadDump':
                (root/'restored').touch()
                if options.get('progressFile'): Path(options['progressFile']).write_text('{}')
            else:
                folder = Path(args[-2]); folder.mkdir(exist_ok=True)
                (folder/'@.done.json').write_text('{}')
                (folder/'app@t.tsv').write_text('1\n2\n')
    print('100% done',file=sys.stderr,flush=True)
elif name == 'mariadb-backup':
    target = Path(next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--target-dir=')))
    target.mkdir(exist_ok=True)
    (target/'xtrabackup_checkpoints').write_text('backup_type = '+('full-prepared' if '--prepare' in sys.argv else 'full-backuped'))
    (target/'ibdata1').write_bytes(b'physical bytes')
    print('completed OK!',file=sys.stderr,flush=True)
elif mode in ('restart', 'gone') and 'CLONE INSTANCE' in stdin:
    print({'restart': 'ERROR 3707 (HY000) at line 2: Restart server failed (mysqld is not managed by supervisor process).',
           'gone': 'ERROR 2013 (HY000) at line 2: Lost connection to MySQL server during query'}[mode],
          file=sys.stderr, flush=True)
    sys.exit(1)
else:
    marker = re.search(r"SELECT CONCAT\('(myflames_job_[^']+:)',CONNECTION_ID\(\)\)",stdin)
    if marker: print(marker.group(1)+'41',flush=True)
    (root/'restored').touch()
    print('100% restored',file=sys.stderr,flush=True)
'''


class ControlTransportTests(unittest.TestCase):
    """Real local children exercise control-query caps and option-file lifetime."""

    def child(self, script):
        real_popen = lc.subprocess.Popen
        self.children = []
        self.defaults = []

        def launch(argv, **kwargs):
            self.defaults.append(argv[1].split('=', 1)[1])
            child = real_popen([sys.executable, '-c', script] + argv[1:], **kwargs)
            self.children.append(child)
            return child
        return mock.patch.object(lc.subprocess, 'Popen', side_effect=launch)

    def test_control_queries_bound_each_pipe_and_reap_real_clients(self):
        self.assertEqual(connector.MAX_STDOUT_BYTES, 64 * 1024 * 1024)
        self.assertEqual(connector.MAX_STDERR_BYTES, 1024 * 1024)
        for stream, limit in (('stdout', connector.MAX_STDOUT_BYTES),
                              ('stderr', connector.MAX_STDERR_BYTES)):
            script = ('import sys\n'
                      'sys.stdin.buffer.read()\n'
                      'for i in range(%d):\n'
                      ' sys.%s.buffer.write(b"x" * 65536)\n'
                      'sys.%s.buffer.write(b"x")\n' % (limit // 65536, stream, stream))
            with self.subTest(stream=stream), self.child(script), \
                    lc.LifecycleConnection(host='localhost', binary='mysql') as conn:
                with self.assertRaisesRegex(ConnectorError, stream + ' response exceeds'):
                    conn.run('SELECT 1', timeout=10)
                self.assertIsNotNone(self.children[0].poll())
                self.assertFalse(os.path.exists(self.defaults[0]))

    def test_control_errors_keep_redaction_error_code_and_socket_flags(self):
        script = ('import sys\n'
                  'assert "--protocol=SOCKET" in sys.argv\n'
                  'assert "--socket=/private/mysql.sock" in sys.argv\n'
                  'assert "-e" not in sys.argv\n'
                  'assert sys.stdin.buffer.read() == b"SELECT 1"\n'
                  'sys.stderr.write("ERROR 1045 (28000): connection-secret IDENTIFIED BY \'sql-secret\'")\n'
                  'sys.exit(1)\n')
        with self.child(script), lc.LifecycleConnection(
                host='localhost', socket='/private/mysql.sock', password='connection-secret', binary='mysql') as conn:
            with self.assertRaises(ConnectorError) as caught:
                conn.run('SELECT 1')
            self.assertEqual(caught.exception.server_error_code, 1045)
            self.assertNotIn('connection-secret', str(caught.exception))
            self.assertNotIn('sql-secret', str(caught.exception))
            self.assertFalse(os.path.exists(self.defaults[0]))

    def test_control_timeout_reaps_client_and_removes_credentials(self):
        with self.child('import os\nr,w=os.pipe()\nos.read(r,1)'), \
                lc.LifecycleConnection(host='localhost', binary='mysql') as conn:
            with self.assertRaisesRegex(ConnectorError, 'timed out'):
                conn.run('SELECT 1', timeout=0.2)
        self.assertIsNotNone(self.children[0].poll())
        self.assertFalse(os.path.exists(self.defaults[0]))

    def test_lifecycle_option_files_pin_tcp_and_preserve_explicit_sockets(self):
        for name, engine in (('mysql', 'mysql'), ('mysqldump', 'mysql'),
                             ('mariadb', 'mariadb'), ('mariadb-dump', 'mariadb'),
                             ('mysqlsh', 'mysql')):
            tool = {'path': name, 'name': name, 'engine': engine}
            for socket_path in (None, '/private/mysql.sock'):
                connection = dict(CONNECTION, host='localhost', port=3317)
                if socket_path:
                    connection['socket'] = socket_path
                with self.subTest(tool=name, socket=socket_path), \
                        lc._credentials(connection, tool) as (argv, env):
                    content = Path(argv[1].split('=', 1)[1]).read_text(encoding="utf-8")
                    self.assertIn('host="localhost"\n', content)
                    if name == 'mysqlsh':
                        self.assertNotIn('protocol=', content)  # Shell has no such option.
                        if socket_path:
                            self.assertNotIn('port=', content)  # Shell's last transport option wins.
                        else:
                            self.assertIn('port="3317"\n', content)
                    else:
                        self.assertIn('protocol=%s\n' % ('SOCKET' if socket_path else 'TCP'), content)
                    if socket_path:
                        self.assertIn('socket="/private/mysql.sock"\n', content)
        with lc.LifecycleConnection(host='localhost', socket='/private/mysql.sock') as conn:
            content = conn._build_defaults_file_content()
            self.assertIn('protocol=SOCKET\n', content)
            self.assertIn('socket="/private/mysql.sock"\n', content)
            self.assertNotIn('protocol=TCP', content)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lc-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.root), 'USERPROFILE': str(self.root),
                         'TMPDIR': str(self.root), 'PATH': str(self.bin), 'MYFLAMES_SECRET_BACKEND': 'vault',
                         'MYSQL_PWD': PASSWORD, 'AWS_SECRET_ACCESS_KEY': 'never-inherit',
                         'MYFLAMES_VAULT_PASSPHRASE': 'never-inherit'})
        self.tempdir = mock.patch.object(tempfile, 'tempdir', str(self.root))
        self.tempdir.start()
        self.addCleanup(self.tempdir.stop)
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in lc.TOOLS:
            tool = self.bin / name
            tool.write_text('#!' + sys.executable + '\n' + FAKE_TOOL)
            tool.chmod(0o700)
        (self.bin / 'upgrade.json').write_text((FIXTURES / 'mysqlsh-upgrade-schema.json').read_text(encoding="utf-8"))
        self.tools = lc.detect_tools(path=str(self.bin), common_dirs=())
        self.detect = mock.patch.object(lc, 'detect_tools', return_value=self.tools)
        self.detect.start()
        self.detecting = True
        self.addCleanup(self.stop_detect)
        self.store = WorkspaceStore(str(self.root / 'workspace.sqlite3'))
        self.conn = ReplayConnection()
        self.output = str(self.root / 'backup')
        self.jobs = []
        self.addCleanup(self.close_jobs)

    def close_jobs(self):
        for job in self.jobs:
            job.close()

    def audit(self, operation='restore', connection=None):
        c = connection or CONNECTION
        return dbops.AuditTrail(self.store, operation, 'cli', connection=c, password=c.get('password'))

    def native_tools(self, engine='mysql'):
        allowed = ('mariadb-dump', 'mariadb') if engine == 'mariadb' else ('mysqldump', 'mysql')
        return {'tools': {name: value for name, value in self.tools['tools'].items() if name in allowed}}

    def calls(self):
        path = self.bin / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def make_dump(self, engine='mysql', shell=False):
        conn = ReplayConnection(server_variables(engine))
        tools = self.tools if shell else self.native_tools(engine)
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            plan = lc.prepare_dump(conn, CONNECTION, self.output, schemas=['app'], tools=tools)
            job = lc.dump_job(CONNECTION, plan, confirmed=True, typed_target=plan['target'],
                              audit=self.audit('dump'), factory=lambda **kw: conn, start=False)
            result = job.run()
        self.assertEqual(result['state'], 'done', result)
        return plan, result

    def target(self):
        return ReplayConnection(server_variables(identity='recipient'), catalog=[], restored_marker=str(self.bin / 'restored'))

    def stop_detect(self):
        # Python 3.7's patcher.stop() raises on a second call (3.8 made it a no-op).
        if self.detecting:
            self.detecting = False
            self.detect.stop()

    def test_tool_detection_and_engine_from_banner(self):
        self.assertTrue(all(v['available'] for v in self.tools['tools'].values()))
        self.assertEqual(self.tools['tools']['mariadb-dump']['version_tuple'], [11, 4, 5])
        self.assertTrue(self.tools['tools']['mysqlpump']['deprecated'])
        (self.bin / 'mysqldump').write_text('#!' + sys.executable + '\nprint("mysqldump Ver 10.19 Distrib 11.4.5-MariaDB")\n')
        self.stop_detect()
        tools = lc.detect_tools(path=str(self.bin), common_dirs=())
        self.assertEqual(tools['tools']['mysqldump']['engine'], 'mariadb')
        offers = lc.offered_tools(lc.read_server(self.conn), tools)
        self.assertNotIn('mysqlpump', offers['dump'])
        self.assertNotIn('mysqldump', offers['dump'])

    def test_tool_detection_common_location_failure_and_missing(self):
        self.stop_detect()
        empty = self.root / 'empty'
        empty.mkdir()
        tools = lc.detect_tools(path=str(empty), common_dirs=(str(self.bin),))
        self.assertTrue(tools['tools']['mysqlsh']['available'])
        (self.bin / 'mysqlsh').write_text('#!' + sys.executable + '\nprint("garbage")\n')
        (self.bin / 'mysql').write_text('#!' + sys.executable + '\nraise SystemExit(2)\n')
        tools = lc.detect_tools(path=str(empty), common_dirs=(str(self.bin),))
        self.assertFalse(tools['tools']['mysqlsh']['available'])
        self.assertFalse(tools['tools']['mysql']['available'])
        self.assertFalse(lc.detect_tools(path=str(empty), common_dirs=())['tools']['mysqldump']['available'])

    def test_option_file_escaping_and_permissions(self):
        password = 'a"b\\c\nd\r\t#;='
        connection = dict(CONNECTION, password=password)
        path = None
        with lc._credentials(connection, dict(self.tools['tools']['mysql'], name='mysql')) as (argv, env):
            path = argv[1].split('=', 1)[1]
            content = Path(path).read_text(encoding="utf-8")
            self.assertIn('password="a\\"b\\\\c\\nd\\r\\t#;="', content)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertNotIn('MYSQL_PWD', env)
            self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
            self.assertNotIn(password, repr(argv) + repr(env))
        self.assertFalse(os.path.exists(path))
        for key in ('host', 'user', 'ssl_ca', 'socket'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                lc._options(dict(CONNECTION, **{key: 'a\npassword=evil'}))

    def test_shell_json_exit_zero_with_errors_and_configuration_failure(self):
        conn = ReplayConnection(server_variables(version='8.0.36'))
        report = lc.upgrade_check(conn, '8.4.0', CONNECTION, self.tools)
        self.assertEqual(report['schema_version'], 'upgrade-check-1.0')
        self.assertEqual(report['method'], 'mysqlsh')
        self.assertEqual(report['status'], 'issues_found')
        self.assertEqual(report['counts']['error'], 1)
        self.assertEqual(len(report['checks']), 3)
        self.assertEqual(report['checks'][2]['status'], 'CONFIGURATION_ERROR')
        self.assertEqual(report['checks'][0]['details']['solutions'], ['Review account authentication before upgrading.'])
        self.assertTrue(report['manual_checks'])
        call = self.calls()[0]
        self.assertIn('--output-format=JSON', call['argv'])
        self.assertNotIn('--result-format=json', call['argv'])
        self.assert_secret_transport(call)
        self.assertEqual(report, json.loads(json.dumps(report, allow_nan=False)))

    def test_shell_nonzero_and_malformed_json(self):
        conn = ReplayConnection(server_variables(version='8.0.36'))
        (self.bin / 'mode').write_text('fail')
        with self.assertRaises(lc.LifecycleError):
            lc.upgrade_check(conn, '8.4.0', CONNECTION, self.tools)
        for text in ('bad', '{}', '{"checksPerformed": [], "errorCount": false}', '[]'):
            with self.subTest(text=text), self.assertRaises(lc.LifecycleError):
                lc.parse_upgrade_json(text)

    def test_shell_version_rules(self):
        for source, target in (('9.6.0', '9.7.0'), ('8.0.36', '9.7.0'),
                               ('8.4.0', '8.0.40'), ('5.6.51', '8.0.36')):
            with self.subTest(source=source, target=target), self.assertRaises(lc.Refused):
                lc.upgrade_check(ReplayConnection(server_variables(version=source)), target, CONNECTION, self.tools)
        self.assertFalse(self.calls())

    def native_live(self, name, deny=None):
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        conn = StrictReplay(data['calls'] + data.get('synthetic_calls', []), deny)
        return lc.upgrade_check(conn, data['target'], CONNECTION, {'tools': {}}), conn

    def test_native_subset_live_replay_mysql_80_to_84(self):
        """Recorded live on MySQL 8.0.46 seeded with a utf8mb3 table, a column named qualify, a table named
        parallel (not reserved in 8.4), a column named rank (already reserved in 8.0), a zero date default,
        a mysql_native_password account, and --default-authentication-plugin on the command line."""
        report, conn = self.native_live('native-8.0.46-to-8.4.0.json')
        checks = {c['id']: c for c in report['checks']}
        self.assertEqual(checks['removed_variables']['severity'], 'error')
        self.assertEqual(checks['removed_variables']['affected_objects'], ['default_authentication_plugin (COMMAND_LINE)'])
        self.assertEqual(checks['removed_persisted_variables']['status'], 'passed')
        self.assertEqual(checks['reserved_identifiers']['affected_objects'], ['app.parallel.qualify (column)'])
        self.assertIn('QUALIFY, TABLESAMPLE', checks['reserved_identifiers']['description'])
        self.assertNotIn('rank', json.dumps(checks['reserved_identifiers']))
        self.assertEqual(checks['utf8mb3']['affected_objects'], ['app.t3.c'])
        self.assertEqual(checks['utf8mb3']['severity'], 'notice')
        self.assertEqual(checks['zero_date_defaults']['affected_objects'], ['app.z.d'])
        self.assertEqual(checks['native_password_users']['severity'], 'warning')
        self.assertEqual(checks['native_password_users']['affected_objects'],
                         ["'legacy'@'%'", "'root'@'%'", "'root'@'localhost'"])
        self.assertNotIn('partition_engines', checks)  # 8.0 already partitions natively only
        self.assertEqual(checks['old_temporal']['status'], 'not_checked')
        self.assertEqual(report['counts'], {'error': 1, 'warning': 4, 'notice': 2})
        self.assertEqual(report['status'], 'issues_found')
        self.assertTrue(report['partial'])
        self.assertIn('Subset', report['limitations'][0])
        for sql, flags in conn.calls:
            self.assertTrue(sql.startswith(lc.TAG + 'SELECT') or sql.startswith(lc.TAG + 'SHOW'), sql)
            self.assertNotIn('CHECK TABLE', sql)

    def test_native_subset_live_replay_9x_target_and_permission_failure(self):
        report, unused = self.native_live('native-8.0.46-to-9.7.0.json')
        checks = {c['id']: c for c in report['checks']}
        self.assertEqual(checks['native_password_users']['severity'], 'error')
        self.assertIn('LIBRARY', checks['reserved_identifiers']['description'])
        report, unused = self.native_live('native-8.0.46-to-8.4.0.json', deny='FROM mysql.user')
        checks = {c['id']: c for c in report['checks']}
        self.assertEqual(checks['native_password_users']['status'], 'not_checked')

    def test_native_subset_57_source_gates(self):
        """Hand-authored 5.7 replay (no 5.7 server was run): the 5.7-only checks run and 8.0 words count."""
        replay = json.loads((FIXTURES / 'native-replay.json').read_text(encoding="utf-8"))['responses']
        conn = ReplayConnection(server_variables(version='5.7.44'), replay=replay)
        report = lc.upgrade_check(conn, '8.0.40', CONNECTION, {'tools': {}})
        checks = {c['id']: c for c in report['checks']}
        self.assertEqual(checks['partition_engines']['affected_objects'], ['app.partitioned.MyISAM'])
        self.assertEqual(checks['removed_variables']['severity'], 'warning')
        self.assertIn('RANK', checks['reserved_identifiers']['description'])
        self.assertIn('INTERSECT', checks['reserved_identifiers']['description'])
        self.assertEqual(checks['reserved_identifiers']['affected_objects'],
                         ['app.t.rank (column)', 'app.window (base table)'])
        self.assertEqual(checks['utf8mb3']['severity'], 'warning')
        self.assertNotIn('native_password_users', checks)
        self.assertFalse(any('KEYWORDS' in sql for sql, unused in conn.calls))
        self.assertFalse(any('variables_info' in sql for sql, unused in conn.calls))

    def test_reserved_words_match_live_keyword_lists(self):
        """RESERVED_SINCE reproduces the differences between information_schema.KEYWORDS read live."""
        servers = json.loads((FIXTURES / 'keywords-reserved.json').read_text(encoding="utf-8"))['servers']
        versions = sorted(servers, key=lc.version_tuple)
        for i, old in enumerate(versions):
            for new in versions[i + 1:]:
                with self.subTest(source=old, target=new):
                    added = set(servers[new]) - set(servers[old])
                    self.assertEqual(set(lc.reserved_words(lc.version_tuple(old), lc.version_tuple(new),
                                                           set(servers[old]))), added)
        for version, words in lc.RESERVED_SINCE:
            for server in versions:
                if lc.version_tuple(server) >= version:
                    self.assertTrue(set(words) <= set(servers[server]), (version, server))
        self.assertEqual(lc.reserved_words((8, 0, 46), (8, 0, 46)), [])

    def test_shell_json_recorded_live(self):
        """Raw mysqlsh 9.6.0 output for MySQL 8.0.46 -> 8.4.0 (the same seeded server)."""
        report = lc.parse_upgrade_json((FIXTURES / 'shell-upgrade-8.0.46-to-8.4.0.json').read_text(encoding="utf-8"))
        self.assertEqual(report['counts'], {'error': 2, 'warning': 27, 'notice': 2})
        self.assertEqual(report['status'], 'issues_found')
        checks = {c['id']: c for c in report['checks']}
        self.assertEqual(checks['authMethodUsage']['affected_objects'], ['legacy@%', 'root@%', 'root@localhost'])
        self.assertEqual(checks['deprecatedDefaultAuth']['severity'], 'error')
        self.assertIn('default_authentication_plugin', checks['sysVars']['affected_objects'])

    def test_mariadb_upgrade_guidance_never_invokes_shell(self):
        report = lc.upgrade_check(ReplayConnection(server_variables('mariadb')), '11.8.0', CONNECTION, self.tools)
        self.assertEqual(report['method'], 'mariadb_upgrade_guidance')
        self.assertIn('0 when upgrade IS needed', report['limitations'][-1])
        self.assertFalse(self.calls())

    def test_shell_older_than_server_is_named_and_points_to_no_shell(self):
        # MySQL Shell dumper.cc:5311-5317 refuses a higher server major.
        conn = ReplayConnection(server_variables(version='26.7.0'))
        with self.assertRaises(lc.Refused) as caught:
            lc.prepare_dump(conn, CONNECTION, self.output, schemas=['app'], tools=self.tools)
        self.assertIn('MySQL Shell 9.6 is older than the server (26.7.0)', str(caught.exception))
        self.assertIn('--no-shell', str(caught.exception))
        self.assertFalse(self.calls())
        # A higher minor only warns (dumper.cc:5319-5334).
        conn = ReplayConnection(server_variables(version='9.7.2'))
        plan = lc.prepare_dump(conn, CONNECTION, self.output, schemas=['app'], tools=self.tools)
        self.assertTrue(any('may fail on it' in w and '--no-shell' in w for w in plan['warnings']))

    def test_tool_error_text_drops_colour_codes_notes_and_position(self):
        shell = ('\x1b[36mNOTE: \x1b[0mdryRun enabled, no locks will be acquired.\n'
                 'Unsupported MySQL Server 26.7.0 detected, please upgrade the MySQL Shell first '
                 '(RuntimeError)  at STDIN:2\n')
        self.assertEqual(lc.tool_error_line(shell),
                         'Unsupported MySQL Server 26.7.0 detected, please upgrade the MySQL Shell first (RuntimeError)')
        escaped = '\\u001b[36mNOTE: \\u001b[0mdry run\nmysqldump: Got error: 1049: Unknown database'
        self.assertEqual(lc.tool_error_line(escaped), 'mysqldump: Got error: 1049: Unknown database')
        self.assertEqual(lc.strip_ansi('\x1b[1;31mred\x1b[0m'), 'red')
        self.assertEqual(lc.tool_error_line(''), '')

    def test_failed_tool_reports_one_clean_error_line(self):
        job = lc.LifecycleJob('dump', lambda job: None, CONNECTION)
        code = ("import sys\nsys.stderr.write('\\x1b[36mNOTE: \\x1b[0mdryRun enabled\\n"
                "Following schemas were not found: nosuch (ArgumentError)  at STDIN:2\\n')\nsys.exit(1)")
        with self.assertRaises(lc.LifecycleError) as caught:
            job.process_tool([sys.executable, '-c', code], dict(os.environ))
        self.assertEqual(str(caught.exception),
                         'Tool exited 1: Following schemas were not found: nosuch (ArgumentError)')

    def test_mariadb_upgrade_target_must_be_a_mariadb_version_not_older(self):
        conn = ReplayConnection(server_variables('mariadb', version='13.0.2-MariaDB-ubu2604-log'))
        for target, words in (('12.3.0', 'is older'), ('8.4.0', 'not an upgrade path'),
                              ('5.7.44', 'not an upgrade path')):
            with self.subTest(target=target), self.assertRaises(lc.Refused) as caught:
                lc.upgrade_check(conn, target, CONNECTION, self.tools)
            self.assertIn(words, str(caught.exception))
        report = lc.upgrade_check(conn, '13.1.0', CONNECTION, self.tools)
        self.assertEqual(report['method'], 'mariadb_upgrade_guidance')

    def test_server_below_the_supported_floor_adds_a_note_to_plans_not_upgrade_checks(self):
        # upgrade-check exists to leave old servers, so it adds no floor note.
        report = lc.upgrade_check(ReplayConnection(server_variables('mariadb', version='10.6.28-MariaDB')),
                                  '11.8.0', CONNECTION, self.tools)
        self.assertNotIn('supports MariaDB', ' '.join(report['limitations']))
        conn = ReplayConnection(server_variables(version='8.0.36'))
        plan = lc.prepare_dump(conn, CONNECTION, self.output, schemas=['app'], tools=self.native_tools())
        self.assertIn('supports MySQL 8.4 or later', plan['warnings'][0])

    def assert_secret_transport(self, call):
        self.assertEqual(call['cnf_mode'], 0o600)
        self.assertTrue(call['password_seen'])
        self.assertFalse(os.path.exists(call['cnf']))
        for secret in (PASSWORD, DONOR_PASSWORD, 'never-inherit'):
            self.assertNotIn(secret, json.dumps(call))
        self.assertNotIn('MYSQL_PWD', call['env'])
        self.assertNotIn('MYFLAMES_VAULT_PASSPHRASE', call['env'])
        self.assertFalse(call['sql_on_argv'])

    def test_native_dump_manifest_progress_checksum_and_no_secret_leaks(self):
        unused, result = self.make_dump()
        self.assertEqual(result['progress'], 100)
        manifest = lc.read_manifest(self.output)
        self.assertTrue(manifest['complete'])
        self.assertEqual(manifest['samples'], [{'schema': 'app', 'table': 't', 'count': 2}])
        self.assertEqual(manifest['files'][0]['size_bytes'], os.path.getsize(os.path.join(self.output, 'dump.sql')))
        self.assertEqual(manifest['files'][0]['sha256'], hashlib.sha256(Path(self.output, 'dump.sql').read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(os.stat(Path(self.output, lc.MANIFEST)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(Path(self.output, 'dump.sql')).st_mode), 0o600)
        self.assertEqual(self.store.audit_records()[0]['status'], 'ok')
        self.assert_secret_transport(self.calls()[0])
        self.assertNotIn(PASSWORD, json.dumps(result) + json.dumps(self.store.audit_records()))
        self.assertIn('--set-gtid-purged=OFF', self.calls()[0]['argv'])

    def test_regression_successful_dump_hashes_files_once(self):
        for engine, shell in (('mysql', False), ('mariadb', False), ('mysql', True)):
            with self.subTest(engine=engine, shell=shell):
                self.output = str(self.root / ('backup-%s-%s' % (engine, shell)))
                with mock.patch.object(lc, '_file_records', wraps=lc._file_records) as hashes:
                    unused, result = self.make_dump(engine, shell)
                self.assertEqual(hashes.call_count, 1)
                manifest = result['result']['manifest']
                self.assertTrue(manifest['complete'])
                self.assertTrue(manifest['files'])
                for record in manifest['files']:
                    content = Path(self.output, record['path']).read_bytes()
                    self.assertEqual(record['size_bytes'], len(content))
                    self.assertEqual(record['sha256'], hashlib.sha256(content).hexdigest())

    def test_mariadb_dump_uses_mariadb_flags(self):
        self.make_dump('mariadb')
        args = self.calls()[0]['argv']
        self.assertIn('mariadb-dump', args[0])
        self.assertIn('--single-transaction', args)
        self.assertNotIn('--source-data', args)
        self.assertFalse(any('gtid-purged' in arg for arg in args))

    def test_named_tables_must_exist_in_the_schema(self):
        tools = self.native_tools()
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tables=['t', 'v'], tools=tools)
        check = [c for c in plan['checks'] if c['check'] == 'tables'][0]
        self.assertEqual(check['status'], 'passed')
        self.assertTrue(plan['ready'])
        self.assertTrue(plan['commands'][0].endswith('app t v > ' + os.path.join(self.output, 'dump.sql')))
        tables_check = lambda plan: [c for c in plan['checks'] if c['check'] == 'tables'][0]
        # Names compare without case only when the server does (lower_case_table_names 1 or 2).
        self.assertEqual(tables_check(lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['APP'],
                                                      tables=['T'], tools=tools))['status'], 'failed')
        for setting, expected in (('1', 'passed'), ('2', 'passed'), ('0', 'failed')):
            conn = ReplayConnection(dict(server_variables(), lower_case_table_names=setting))
            self.assertEqual(tables_check(lc.prepare_dump(conn, CONNECTION, self.output, schemas=['APP'], tables=['T'],
                                                          tools=tools))['status'], expected, setting)
        # On a case-sensitive server a missing `orders` is not the existing `Orders`.
        upper = ReplayConnection(dict(server_variables(), lower_case_table_names='0'),
                                 catalog=[['app', 'Orders', 'BASE TABLE', 'InnoDB', '100', '20']])
        check = tables_check(lc.prepare_dump(upper, CONNECTION, self.output, schemas=['app'], tables=['Orders', 'orders'],
                                             tools=tools))
        self.assertEqual((check['status'], check['missing']), ('failed', ['orders']))
        gone = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tables=['t', 'gone'], tools=tools)
        check = [c for c in gone['checks'] if c['check'] == 'tables'][0]
        self.assertEqual((check['status'], check['missing']), ('failed', ['gone']))
        self.assertIn('Not found in app: gone', check['reason'])
        self.assertFalse(gone['ready'])
        # Schema and instance dumps do not add the check.
        self.assertNotIn('tables', [c['check'] for c in lc.prepare_dump(
            self.conn, CONNECTION, self.output, schemas=['app'], tools=tools)['checks']])

    def test_shell_dump_dry_run_and_dump_scope_methods(self):
        plan, result = self.make_dump(shell=True)
        self.assertIn('util.dumpSchemas', plan['commands'][0])
        self.assertGreaterEqual(len(self.calls()), 3)  # prepare dryRun, recheck dryRun, real dump
        self.assertTrue(Path(self.output, '@.done.json').exists())
        self.assertEqual(result['result']['manifest']['tool']['name'], 'mysqlsh')
        for schemas, tables, method in (([], [], 'dumpInstance'), (['app'], [], 'dumpSchemas'), (['app'], ['t'], 'dumpTables')):
            req = {'directory': self.output, 'schemas': schemas, 'tables': tables, 'threads': 4}
            self.assertIn('util.' + method, lc._shell_dump_expression(req))
            self.assertIn('"dryRun": true', lc._shell_dump_expression(req, True))

    def test_refuses_overwrite_symlink_and_bad_scope(self):
        Path(self.output).mkdir()
        Path(self.output, 'keep').write_text('original')
        with self.assertRaises(lc.Refused):
            lc.prepare_dump(self.conn, CONNECTION, self.output, tools=self.native_tools())
        self.assertEqual(Path(self.output, 'keep').read_text(encoding="utf-8"), 'original')
        link = self.root / 'link'
        link.symlink_to(self.output)
        with self.assertRaises(lc.Refused):
            lc.prepare_dump(self.conn, CONNECTION, str(link), tools=self.native_tools())
        with self.assertRaises(ValueError):
            lc.prepare_dump(self.conn, CONNECTION, str(self.root/'other'), schemas=['--all-databases'], tools=self.native_tools())
        with self.assertRaises(ValueError):
            lc.prepare_dump(self.conn, CONNECTION, str(self.root/'other'), tables=['t'], tools=self.native_tools())

    def test_cancel_terminates_process_and_keeps_incomplete_manifest(self):
        tools = self.native_tools()
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=tools)
            (self.bin/'mode').write_text('slow')
            event = threading.Event()
            job = lc.dump_job(CONNECTION, plan, confirmed=True, typed_target=plan['target'], audit=self.audit('dump'),
                              factory=lambda **kw: self.conn, start=False)
            self.jobs.append(job)
            emit = job.emit
            def saw_progress(text, stream='status'):
                emit(text, stream)
                if '25%' in text:
                    event.set()
            job.emit = saw_progress
            job.start()
            self.assertTrue(event.wait(8), job.snapshot())
            process = job.process
            job.cancel()
            result = job.wait(8)
        self.assertIsNotNone(process.poll())
        self.assertEqual(result['state'], 'cancelled', result)
        self.assertFalse(json.loads(Path(self.output, lc.MANIFEST).read_text(encoding="utf-8"))['complete'])
        self.assertEqual(self.store.audit_records()[0]['status'], 'unknown')
        self.assert_secret_transport(self.calls()[0])
        self.assertFalse(job.thread.is_alive())

    def test_policy_confirmation_and_no_audit_refuse_before_connect(self):
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.native_tools())
        for confirmed, target, safety in ((False, plan['target'], None), (True, 'wrong', None),
                                         (True, plan['target'], {'read_only': True, 'type': 'development'})):
            factory = mock.Mock(side_effect=AssertionError('must not connect'))
            job = lc.dump_job(CONNECTION, plan, confirmed=confirmed, typed_target=target, safety=safety,
                              audit=self.audit('dump'), factory=factory, start=False)
            self.assertEqual(job.run()['state'], 'refused')
            factory.assert_not_called()
        factory = mock.Mock()
        job = lc.dump_job(CONNECTION, plan, confirmed=True, typed_target=plan['target'], factory=factory, start=False)
        self.assertEqual(job.run()['state'], 'refused')
        factory.assert_not_called()
        self.assertFalse(os.path.exists(self.output))
        self.assertEqual(len(self.store.audit_records()), 3)

    def test_audit_failure_prevents_process(self):
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.native_tools())
        audit = self.audit('dump')
        with mock.patch.object(lc, 'detect_tools', return_value=self.native_tools()), mock.patch.object(audit, 'attempt', side_effect=OSError('disk full')):
            result = lc.dump_job(CONNECTION, plan, True, plan['target'], audit, factory=lambda **kw: self.conn, start=False).run()
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(self.calls())
        self.assertFalse(os.path.exists(self.output))

    def test_write_policy_is_recomputed_when_queued_job_runs(self):
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.native_tools())
        for callback in (False, True):
            with self.subTest(callback=callback):
                safety = {'type': 'development', 'read_only': False}
                factory = mock.Mock(side_effect=AssertionError('must not connect'))
                job = lc.dump_job(CONNECTION, plan, True, plan['target'], self.audit('dump'),
                                  safety=(lambda: safety) if callback else safety,
                                  factory=factory, start=False)
                safety['read_only'] = True
                result = job.run()
                self.assertEqual(result['state'], 'refused', result)
                self.assertIn('read-only', result['error'])
                factory.assert_not_called()
                self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_write_policy_is_recomputed_after_preflight(self):
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.native_tools())
        safety = {'type': 'development', 'read_only': False}
        action = mock.Mock(side_effect=AssertionError('must not write'))
        def rebuild(conn, job):
            safety['read_only'] = True
            return plan
        job = lc._write_job('dump', CONNECTION, plan, rebuild, action, True, plan['target'],
                            self.audit('dump'), lambda: safety, lambda **kw: self.conn, start=False)
        result = job.run()
        self.assertEqual(result['state'], 'refused', result)
        self.assertIn('read-only', result['error'])
        action.assert_not_called()
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_missing_live_write_policy_refuses_before_connect_and_is_audited(self):
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.native_tools())
        current = [{'type': 'development', 'read_only': False}]
        factory = mock.Mock(side_effect=AssertionError('must not connect'))
        job = lc.dump_job(CONNECTION, plan, True, plan['target'], self.audit('dump'),
                          safety=lambda: current[0], factory=factory, start=False)
        current[0] = None
        result = job.run()
        self.assertEqual(result['state'], 'refused', result)
        self.assertIn('policy is unavailable', result['error'])
        factory.assert_not_called()
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_saved_policy_changes_refuse_at_start_and_after_preflight(self):
        profile = dict(CONNECTION, id='saved', name='Source', type='production', read_only=False)
        options = dict(CONNECTION, profile_id='saved')
        safety = dbops.connection_safety(profile, options)
        tools = self.native_tools()
        plan = lc.prepare_dump(self.conn, options, self.output, schemas=['app'], tools=tools)
        mutations = ({'read_only': True}, {'type': 'development'}, {'host': 'elsewhere'},
                     {'ssl_mode': 'DISABLED'}, None)
        for during_preflight in (False, True):
            for change in mutations:
                with self.subTest(during_preflight=during_preflight, change=change):
                    current = [dict(profile)]
                    action = mock.Mock(side_effect=AssertionError('must not write'))
                    factory = mock.Mock(return_value=self.conn)
                    def mutate():
                        if change is None:
                            current[0] = None
                        else:
                            current[0].update(change)
                    def rebuild(conn, job):
                        if during_preflight:
                            mutate()
                        return plan
                    job = lc._write_job('dump', options, plan, rebuild, action, True, plan['target'],
                                        self.audit('dump'), safety, factory, start=False)
                    lc.bind_profile_policy(job, options, safety, lambda key: current[0])
                    if not during_preflight:
                        mutate()
                    result = job.run()
                    self.assertEqual(result['state'], 'refused', result)
                    action.assert_not_called()
                    if not during_preflight:
                        factory.assert_not_called()
                    self.assertEqual(self.store.audit_records()[0]['status'], 'refused')
        self.assertFalse(self.calls())

    def test_policy_is_checked_again_before_tool_and_donor_read_only_is_allowed(self):
        profile = dict(CONNECTION, id='saved', type='production', read_only=True)
        options = dict(CONNECTION, profile_id='saved')
        safety = dbops.connection_safety(profile, options)
        job = lc.LifecycleJob('dump', lambda job: None, options)
        lc.bind_profile_policy(job, options, safety, lambda key: profile, write=False)
        lc._check_profile_policy(job)  # A read-only donor is a valid source.
        profile['name'] = 'Renamed'
        lc._check_profile_policy(job)
        profile['port'] = 3307
        with mock.patch.object(job, 'process_tool') as process:
            with self.assertRaises(lc.Refused):
                lc._invoke(job, self.native_tools()['tools']['mysqldump'], options, [])
            process.assert_not_called()

    def test_restore_confirmation_identity_and_same_source(self):
        self.make_dump()
        tools = self.native_tools()
        same = lc.prepare_restore(self.conn, CONNECTION, self.output, tools=tools, existing_objects='drop')
        self.assertFalse(same['ready'])
        same = lc.prepare_restore(self.conn, CONNECTION, self.output, tools=tools, existing_objects='drop', allow_source_restore=True)
        self.assertTrue(same['ready'])
        target = self.target()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        target.variables['server_uuid'] = 'different-server'
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(), factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['state'], 'refused')
        self.assertFalse((self.bin/'restored').exists())
        self.assertEqual(self.store.audit_records()[0]['status'], 'refused')

    def test_identity_rechecked_after_preflight_and_typed_target_bound(self):
        self.make_dump()
        tools = self.native_tools()
        for mutate_target in (False, True):
            target = self.target()
            plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
            if mutate_target:
                plan['target'] = 'a different target'
            else:
                target.change_identity_at = target.identity_reads + 2
            with mock.patch.object(lc, 'detect_tools', return_value=tools):
                result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(), factory=lambda **kw: target, start=False).run()
            self.assertEqual(result['state'], 'refused', result)
        self.assertFalse((self.bin/'restored').exists())

    def test_restore_wrong_target_never_constructs_or_starts_a_job(self):
        self.make_dump()
        target = self.target()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=self.native_tools())
        for start in (False, True):
            with self.subTest(start=start), mock.patch.object(lc, 'LifecycleJob') as job, \
                    mock.patch.object(lc, '_connect') as connect:
                with self.assertRaisesRegex(lc.Refused,
                        'Nothing was restored: the typed target does not match '):
                    lc.restore_job(CONNECTION, plan, confirmed=True, typed_target='wrong',
                                   audit=self.audit(), start=start)
            job.assert_not_called()
            connect.assert_not_called()
        self.assertFalse((self.bin / 'restored').exists())
        records = self.store.audit_records()
        # The first record belongs to make_dump; both refused restores are unsent.
        self.assertEqual([r['status'] for r in records if r['operation'] == 'restore'], ['refused', 'refused'])
        for row in records:
            if row['operation'] == 'restore':
                self.assertEqual(row['outcome'], 'target_mismatch')
                self.assertEqual(row['target'], plan['server']['identity'])
        self.assertNotIn('Partial', json.dumps(records))

    def test_restore_success_rows_objects_and_secret_transport(self):
        self.make_dump()
        tools, target = self.native_tools(), self.target()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(), factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['state'], 'done', result)
        verification = result['result']['verification']
        self.assertEqual(next(v for v in verification if v['check'] == 'row_count')['status'], 'passed')
        self.assertEqual(next(v for v in verification if v['check'] == 'object_presence')['status'], 'passed')
        self.assert_secret_transport(self.calls()[-1])
        self.assertIn('--binary-mode', self.calls()[-1]['argv'])
        self.assertNotIn('-e', self.calls()[-1]['argv'])

    def test_restore_row_mismatch_is_failed_verification(self):
        self.make_dump()
        target, tools = self.target(), self.native_tools()
        target.row_count = 99
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(), factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['result']['status'], 'verification_failed')
        self.assertEqual(self.store.audit_records()[0]['status'], 'error')

    def test_restore_tampered_file_and_partial_manifest_refused(self):
        self.make_dump()
        target, tools = self.target(), self.native_tools()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        with Path(self.output, 'dump.sql').open('a') as handle:
            handle.write('DROP DATABASE unexpected;')
        with self.assertRaises(lc.Refused):
            lc.read_manifest(self.output)
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(), factory=lambda **kw: target, start=False).run()
        self.assertNotEqual(result['state'], 'done')
        self.assertFalse((self.bin/'restored').exists())
        data = json.loads(Path(self.output, lc.MANIFEST).read_text(encoding="utf-8"))
        data['complete'] = False
        Path(self.output, lc.MANIFEST).write_text(json.dumps(data))
        with self.assertRaises(lc.Refused):
            lc.prepare_restore(target, CONNECTION, self.output, tools=tools)

    def test_shell_restore_local_infile_duplicates_cross_engine_and_progress(self):
        self.make_dump(shell=True)
        target = self.target()
        target.variables['local_infile'] = 'OFF'
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=self.tools)
        self.assertFalse(plan['ready'])
        target.variables['local_infile'] = 'ON'
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=self.tools)
        self.assertIn('progressFile', plan['commands'][0])
        self.assertNotEqual(plan['request']['progress_file'], '')
        result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(), factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['state'], 'done', result)
        self.assertTrue(Path(plan['request']['progress_file']).exists())
        lc.read_manifest(self.output)  # resume state did not invalidate immutable backup
        again = lc.prepare_restore(target, CONNECTION, self.output, tools=self.tools, existing_objects='ignore')
        self.assertFalse(again['ready'])
        resumed = lc.prepare_restore(target, CONNECTION, self.output, tools=self.tools, existing_objects='ignore', resume=True)
        self.assertTrue(resumed['ready'])
        target.variables = server_variables('mariadb', identity='recipient')
        rejected = lc.prepare_restore(target, CONNECTION, self.output, tools=self.tools, existing_objects='drop', resume=True)
        self.assertFalse(rejected['ready'])

    def test_logs_audit_and_snapshot_redact_echoed_secrets(self):
        (self.bin/'mode').write_text('echo')
        unused, result = self.make_dump()
        text = json.dumps(result) + json.dumps(self.store.audit_records())
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn('unknown-secret', text)
        self.assertIn('[redacted]', text)

    def clone_facts(self, identity='donor', version='8.4.3', engine='mysql'):
        conn = ReplayConnection(server_variables(engine, identity, version))
        return lc._clone_facts(conn, CONNECTION)

    def test_clone_matrix_versions_plugins_privileges_platform_configuration(self):
        donor, recipient = self.clone_facts(), self.clone_facts('recipient')
        good = lc.clone_preflight(donor, recipient, CONNECTION, CONNECTION)
        self.assertTrue(all(c['status'] == 'passed' for c in good), good)
        cases = [('engine', lambda d, r: r['server'].update(engine='mariadb')),
                 ('donor_plugin', lambda d, r: d.update(plugins=[])),
                 ('recipient_privilege', lambda d, r: r['privileges'].update(dynamic=[], **{'global': []})),
                 ('version_compile_os', lambda d, r: r['server']['variables'].update(version_compile_os='Windows')),
                 ('innodb_page_size', lambda d, r: r['server']['variables'].update(innodb_page_size='8192')),
                 ('valid_donor', lambda d, r: r['server']['variables'].update(clone_valid_donor_list='')),
                 ('recipient_packet', lambda d, r: r['server']['variables'].update(max_allowed_packet='1000')),
                 ('collations', lambda d, r: r.update(collations=[])),
                 ('plugins', lambda d, r: d['plugins'].append(['other', 'ACTIVE', 'other.so']))]
        for key, change in cases:
            d, r = copy.deepcopy(donor), copy.deepcopy(recipient)
            change(d, r)
            checks = lc.clone_preflight(d, r, CONNECTION, CONNECTION)
            self.assertEqual(next(c for c in checks if c['check'] == key)['status'], 'failed', key)
        for source, target, ok in (('8.0.16', '8.0.16', False), ('8.0.36', '8.0.37', False),
                                   ('8.0.37', '8.0.40', True), ('8.4.1', '8.4.3', True),
                                   ('8.4.3', '9.0.0', False)):
            checks = lc.clone_preflight(self.clone_facts(version=source), self.clone_facts('recipient', target), CONNECTION, CONNECTION)
            self.assertEqual(next(c for c in checks if c['check'] == 'version')['status'] == 'passed', ok)

    def test_clone_data_directory_rules_and_directory_privilege(self):
        donor, recipient = self.clone_facts(), self.clone_facts('recipient')
        for path, probe, ok in (('relative', None, False), ('/copy', None, False),
                                ('/copy', lambda p: {'exists': True, 'realpath': p}, False),
                                ('/server/data/copy', lambda p: {'exists': p.rstrip('/') == '/server/data', 'realpath': p}, False),
                                ('/symlink', lambda p: {'exists': p.rstrip('/') == '/server/data', 'realpath': p if p.rstrip('/') == '/server/data' else '/server/data/copy'}, False),
                                ('/copy', lambda p: {'exists': p.rstrip('/') == '/server/data', 'realpath': p}, True)):
            checks = lc.clone_preflight(donor, recipient, CONNECTION, CONNECTION, path, probe)
            self.assertEqual(all(c['status'] == 'passed' for c in checks), ok, checks)
        checks = lc.clone_preflight(donor, recipient, CONNECTION, CONNECTION, '/mounted/data/copy',
            lambda p: {'exists': p.rstrip('/') == '/server/data',
                       'realpath': '/mounted/data' if p.rstrip('/') == '/server/data' else p})
        self.assertEqual(next(c for c in checks if c['check'] == 'directory_outside_datadir')['status'], 'failed')
        recipient['privileges'] = privileges.parse_grants(['GRANT BACKUP_ADMIN ON *.* TO `op`@`%`'])
        checks = lc.clone_preflight(donor, recipient, CONNECTION, CONNECTION, '/copy', lambda p: {'exists': p.rstrip('/') == '/server/data', 'realpath': p})
        self.assertTrue(all(c['status'] == 'passed' for c in checks))

    def test_saved_donor_requires_recipient_tls_before_facts_or_clone(self):
        donor_options = dict(CONNECTION, password=DONOR_PASSWORD, _saved_password=True)
        for mode in (None, 'DISABLED', 'PREFERRED'):
            recipient_options = dict(CONNECTION, ssl_mode=mode)
            with self.subTest(mode=mode), mock.patch.object(lc, '_clone_facts') as facts:
                with self.assertRaisesRegex(lc.Refused, 'saved donor password requires TLS on the recipient'):
                    lc.prepare_clone(ReplayConnection(), self.target(), donor_options,
                                     recipient_options, tools=self.tools)
                facts.assert_not_called()
        for mode in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
            recipient_options = dict(CONNECTION, ssl_mode=mode)
            plan = lc.prepare_clone(ReplayConnection(), self.target(), donor_options,
                                    recipient_options, tools=self.tools)
            self.assertTrue(plan['request']['saved_donor_password'])
            with mock.patch.object(lc, '_invoke') as send:
                result = lc.clone_job(donor_options, dict(recipient_options, ssl_mode='PREFERRED'),
                                      plan, True, plan['target'], self.audit('clone'),
                                      factory=lambda **kw: self.target(), start=False).run()
                self.assertEqual(result['state'], 'refused', result)
                send.assert_not_called()
        # An explicitly entered credential retains the settled override rule.
        lc.require_clone_recipient_tls(dict(CONNECTION, password=DONOR_PASSWORD), CONNECTION)

    def test_clone_recipient_tls_for_saved_profiles_and_tls_required_donors(self):
        donors = [dict(CONNECTION, password=DONOR_PASSWORD, profile_id='saved-donor')]
        donors.extend(dict(CONNECTION, password=DONOR_PASSWORD, ssl_mode=mode)
                      for mode in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'))
        recipients = [dict(CONNECTION, ssl_mode=mode) for mode in ('DISABLED', 'PREFERRED')]
        recipients.extend(dict(CONNECTION, host='recipient.internal', ssl_mode=mode)
                          for mode in (None, 'DISABLED', 'PREFERRED'))
        for donor in donors:
            for recipient in recipients:
                with self.subTest(donor=donor, recipient=recipient), \
                        mock.patch.object(lc, '_clone_facts') as facts:
                    with self.assertRaisesRegex(lc.Refused, 'recipient connection.*TLS'):
                        lc.prepare_clone(ReplayConnection(), self.target(), donor, recipient, tools=self.tools)
                    facts.assert_not_called()
            # Preserve the implicit local transport for legacy loopback callers.
            for host in ('127.0.0.1', '::1', 'localhost'):
                lc.require_clone_recipient_tls(donor, dict(CONNECTION, host=host))
            for mode in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
                lc.require_clone_recipient_tls(donor, dict(CONNECTION, host='recipient.internal', ssl_mode=mode))

    def test_clone_exact_statement_and_stdin_audit(self):
        donor_options = dict(CONNECTION, password=DONOR_PASSWORD)
        donor, recipient = ReplayConnection(), self.target()
        plan = lc.prepare_clone(donor, recipient, donor_options, CONNECTION, tools=self.tools)
        self.assertTrue(any('DROPS ALL RECIPIENT USER DATA' in w for w in plan['warnings']))
        self.assertTrue(any("donor's accounts" in w for w in plan['warnings']))
        self.assertIn("IDENTIFIED BY '[redacted]'", plan['commands'][0])
        statement = lc.clone_statement(donor_options, '/copy', redact=False)
        self.assertEqual(statement, lc.TAG + "CLONE INSTANCE FROM 'operator'@'127.0.0.1':3306 IDENTIFIED BY 'donor''private' DATA DIRECTORY = '/copy' REQUIRE SSL")
        connections = iter([recipient, donor])
        result = lc.clone_job(donor_options, CONNECTION, plan, True, plan['target'], self.audit('clone'),
                              factory=lambda **kw: next(connections), start=False).run()
        self.assertEqual(result['state'], 'done', result)
        self.assertTrue(self.calls()[-1]['donor_secret_on_stdin'])
        self.assert_secret_transport(self.calls()[-1])
        self.assertNotIn(DONOR_PASSWORD, json.dumps(result) + json.dumps(self.store.audit_records()))
        self.assertEqual(result['result']['verification'][0]['status'], 'passed')

    def test_restore_preflight_bounds_file_byte_and_directory_walk(self):
        self.make_dump()
        for limit, maximum, message in (('MAX_RESTORE_FILES', 0, 'file limit'),
                                         ('MAX_RESTORE_BYTES', 1, 'byte limit'),
                                         ('MAX_RESTORE_DIRECTORIES', 0, 'directory limit')):
            with self.subTest(limit=limit), mock.patch.object(lc, limit, maximum):
                with self.assertRaisesRegex(lc.Refused, message):
                    lc.prepare_restore(self.target(), CONNECTION, self.output, tools=self.native_tools())
        total = sum(r['size_bytes'] for r in lc._file_records(self.output))
        with mock.patch.object(lc, 'MAX_RESTORE_FILES', 1), mock.patch.object(lc, 'MAX_RESTORE_BYTES', total):
            self.assertTrue(lc.prepare_restore(self.target(), CONNECTION, self.output,
                                               tools=self.native_tools())['ready'])
        # A file that grows after stat must still consume the read budget.
        with mock.patch.object(lc.os.path, 'getsize', return_value=0):
            with self.assertRaisesRegex(lc.Refused, 'byte limit'):
                lc._file_records(self.output, max_bytes=1)

    def test_dump_dry_run_cannot_create_requested_destination(self):
        destinations = []
        def invoke(job, tool, connection, args, script, **kwargs):
            expression = next(line for line in script.splitlines() if 'util.dumpSchemas(' in line)
            args_json = expression.split('util.dumpSchemas(', 1)[1].split(');', 1)[0]
            unused, destination, options = json.loads('[' + args_json + ']')
            self.assertTrue(options['dryRun'])
            self.assertNotEqual(destination, self.output)
            os.makedirs(destination)
            destinations.append(destination)
            return '', ''
        with mock.patch.object(lc, '_invoke', side_effect=invoke):
            plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.tools)
        self.assertFalse(os.path.exists(self.output))
        self.assertTrue(destinations)
        self.assertFalse(any(os.path.exists(path) for path in destinations))
        self.assertIn(self.output, plan['commands'][0])

    def test_clone_pair_and_policy_are_bound_even_when_server_identity_matches(self):
        donor_options = dict(CONNECTION, password=DONOR_PASSWORD, type='development', read_only=True)
        donor, recipient = ReplayConnection(), self.target()
        plan = lc.prepare_clone(donor, recipient, donor_options, CONNECTION, tools=self.tools)
        for side, field, value in (('donor', 'host', 'alias'), ('recipient', 'port', 3311),
                                    ('donor', 'type', 'production')):
            d, r = dict(donor_options), dict(CONNECTION)
            (d if side == 'donor' else r)[field] = value
            connections = iter([recipient, donor])
            with self.subTest(side=side, field=field), mock.patch.object(lc, '_invoke') as send:
                result = lc.clone_job(d, r, plan, True, plan['target'], self.audit('clone'),
                                      factory=lambda **kw: next(connections), start=False).run()
                self.assertEqual(result['state'], 'refused', result)
                self.assertIn('changed', result['error'])
                send.assert_not_called()
        with self.assertRaisesRegex(lc.Refused, 'requires TLS'):
            lc.prepare_clone(donor, recipient, dict(donor_options, ssl_mode='VERIFY_CA'), CONNECTION,
                             require_ssl=False, tools=self.tools)

    def test_clone_early_refusals_redact_both_secrets_in_audit(self):
        self.assertEqual(lc._safe('secret secret-long', ('secret', 'secret-long')), '[redacted] [redacted]')
        donor_options = dict(CONNECTION, password=DONOR_PASSWORD)
        plan = lc.prepare_clone(ReplayConnection(), self.target(), donor_options, CONNECTION, tools=self.tools)
        plan['commands'] = ['echo ' + PASSWORD + ' ' + DONOR_PASSWORD]
        result = lc.clone_job(donor_options, CONNECTION, plan, True, 'wrong target', self.audit('clone'),
                              start=False).run()
        text = json.dumps(result) + json.dumps(self.store.audit_records())
        self.assertEqual(result['state'], 'refused')
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(DONOR_PASSWORD, text)
        # Both passwords are removed from logs and free-text manifest fields.
        job = lc.LifecycleJob('clone', lambda j: None, CONNECTION, passwords=(DONOR_PASSWORD,))
        job.emit(PASSWORD + ' ' + DONOR_PASSWORD)
        manifest = lc._report_safe({'schema_version': 'lifecycle-manifest-1.0',
                                    'verification': [{'reason': PASSWORD + ' ' + DONOR_PASSWORD}],
                                    'options': {'tool_options': [PASSWORD, DONOR_PASSWORD]}}, job.passwords)
        text = json.dumps(job.snapshot()) + json.dumps(manifest)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(DONOR_PASSWORD, text)

    def test_clone_progress_nested_rows_redacted_in_snapshots_and_audit(self):
        donor_options = dict(CONNECTION, password=DONOR_PASSWORD)
        donor, recipient = ReplayConnection(), self.target()
        plan = lc.prepare_clone(donor, recipient, donor_options, CONNECTION, tools=self.tools)
        progress = lc.clone_progress(recipient)
        message = PASSWORD + ' ' + DONOR_PASSWORD
        progress['status'][0][2] = message
        progress['progress'] = [['FILE COPY', message, '100', '10', '10', '1']]
        original = copy.deepcopy(progress)
        def invoke(job, *args, **kwargs):
            # Deterministically exercise the monitor's raw table payload.
            job.detail = copy.deepcopy(progress)
        connections = iter([recipient, donor])
        with mock.patch.object(lc, '_invoke', side_effect=invoke), \
                mock.patch.object(lc, 'clone_progress', return_value=progress):
            job = lc.clone_job(donor_options, CONNECTION, plan, True, plan['target'], self.audit('clone'),
                               factory=lambda **kw: next(connections), start=False)
            result = job.run()
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(result['detail']['schema_version'], 'clone-progress-1.0')
        self.assertEqual(result['detail']['status'][0][2], '[redacted] [redacted]')
        records = self.store.audit_records()
        self.assertEqual(records[0]['status'], 'ok')
        text = json.dumps(result) + json.dumps(records)
        for secret in (PASSWORD, DONOR_PASSWORD):
            self.assertNotIn(secret, text)
        self.assertEqual(progress, original)
        # Result tables may be nested more deeply than the monitor detail.
        job.result = {'status': 'completed', 'clone_progress': progress}
        for secret in (PASSWORD, DONOR_PASSWORD):
            self.assertNotIn(secret, json.dumps(job.snapshot()))

    def test_clone_cancel_kills_only_owned_session(self):
        control = ReplayConnection()
        job = lc.LifecycleJob('clone', lambda j: None, CONNECTION, factory=lambda **kw: control)
        job.connection_id = 412
        job.cancel()
        self.assertEqual(control.calls[0][0], lc.TAG + 'KILL QUERY 412')
        self.assertEqual(control.calls[0][1], list(dbops.WRITE_CLIENT_FLAGS))
        no_id = mock.Mock()
        lc.LifecycleJob('clone', lambda j: None, CONNECTION, factory=no_id).cancel()
        no_id.assert_not_called()

    def test_physical_backup_remote_guidance_and_local_backup_prepare(self):
        variables = server_variables('mariadb')
        remote = ReplayConnection(variables)
        plan = lc.prepare_physical_backup(remote, CONNECTION, self.output, tools=self.tools)
        self.assertFalse(plan['ready'])
        self.assertFalse(lc.physical_backup_guidance(remote, CONNECTION, self.output)['local_files_detected'])
        datadir = self.root / 'data'
        datadir.mkdir()
        sockpath = str(self.root / 'mysql.sock')
        # Sandbox forbids binding AF_UNIX. Replay only the socket inode
        # metadata, while all tools/files still run for real.
        Path(sockpath).touch()
        real_stat = os.stat
        def socket_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if os.fspath(path) == sockpath:
                return os.stat_result((stat.S_IFSOCK | 0o600,) + tuple(result)[1:])
            return result
        with mock.patch.object(lc.os, 'stat', side_effect=socket_stat):
            variables.update(socket=sockpath, datadir=str(datadir))
            connection = dict(CONNECTION, host='localhost', socket=sockpath)
            local = ReplayConnection(variables)
            plan = lc.prepare_physical_backup(local, connection, self.output, tools=self.tools, same_host_confirmed=True)
            self.assertTrue(plan['ready'], plan)
            result = lc.physical_backup_job(connection, plan, True, plan['target'], self.audit('physical_backup', connection),
                                             factory=lambda **kw: local, start=False).run()
        self.assertEqual(result['state'], 'done', result)
        self.assertTrue(lc.read_manifest(self.output)['complete'])
        self.assertEqual(len(self.calls()), 2)
        self.assertIn('--backup', self.calls()[0]['argv'])
        self.assertIn('--prepare', self.calls()[1]['argv'])
        with self.assertRaises(lc.Refused):
            lc.prepare_restore(local, connection, self.output, tools=self.tools)

    def test_short_password_does_not_corrupt_manifest_hashes_or_schema_ids(self):
        connection = dict(CONNECTION, password='a')
        tools = self.native_tools()
        plan = lc.prepare_dump(self.conn, connection, self.output, schemas=['app'], tools=tools)
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = lc.dump_job(connection, plan, True, plan['target'], self.audit('dump', connection),
                                 factory=lambda **kw: self.conn, start=False).run()
        self.assertEqual(result['schema_version'], 'lifecycle-job-1.0')
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(lc.read_manifest(self.output)['schema_version'], 'lifecycle-manifest-1.0')
        self.assertEqual(result['result']['manifest']['files'][0]['path'], 'dump.sql')

    def test_process_failure_retains_partial_dump_and_deletes_credentials(self):
        tools = self.native_tools()
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=tools)
        (self.bin/'mode').write_text('fail')
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = lc.dump_job(CONNECTION, plan, True, plan['target'], self.audit('dump'),
                                 factory=lambda **kw: self.conn, start=False).run()
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(json.loads(Path(self.output, lc.MANIFEST).read_text(encoding="utf-8"))['complete'])
        self.assertEqual(self.store.audit_records()[0]['status'], 'error')
        self.assert_secret_transport(self.calls()[0])

    def test_credentials_removed_after_spawn_failure(self):
        tool = dict(self.tools['tools']['mysql'], name='mysql', path=str(self.bin/'missing'))
        runner = lc.LifecycleJob('test', lambda j: None, CONNECTION)
        with mock.patch.object(lc.subprocess, 'Popen', side_effect=OSError('spawn failed')) as spawn:
            with self.assertRaises(OSError):
                lc._invoke(runner, tool, CONNECTION, [])
        cnf = spawn.call_args[0][0][1].split('=', 1)[1]
        self.assertFalse(os.path.exists(cnf))

    def test_progress_from_carriage_returns_is_streamed_before_process_ends(self):
        (self.bin/'mode').write_text('cr')
        ready = threading.Event()
        tool = dict(self.tools['tools']['mysql'], name='mysql')
        job = lc.LifecycleJob('test', lambda j: lc._invoke(j, tool, CONNECTION, []), CONNECTION)
        self.jobs.append(job)
        emit = job.emit
        def received(text, stream='status'):
            emit(text, stream)
            if '42%' in text:
                ready.set()
        job.emit = received
        job.start()
        self.assertTrue(ready.wait(8), job.snapshot())
        self.assertIsNone(job.process.poll())
        self.assertEqual(job.snapshot()['progress'], 42)
        job.cancel()
        self.assertEqual(job.wait(8)['state'], 'cancelled')

    def test_oversized_tool_lines_are_not_logged_in_fragments(self):
        (self.bin/'mode').write_text('long')
        unused, result = self.make_dump()
        text = json.dumps(result)
        self.assertNotIn(PASSWORD, text)
        self.assertIn('longer than 8192 bytes omitted', text)
        self.assertLess(max(len(event['text']) for event in result['events']), 8193)

    def test_restore_cancel_kills_executing_session_and_marks_audit_unknown(self):
        self.make_dump()
        target, tools = self.target(), self.native_tools()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        (self.bin/'mode').write_text('slow')
        ready = threading.Event()
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            job = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(),
                                 factory=lambda **kw: target, start=False)
            self.jobs.append(job)
            emit = job.emit
            def received(text, stream='status'):
                emit(text, stream)
                if '25%' in text:
                    ready.set()
            job.emit = received
            job.start()
            self.assertTrue(ready.wait(8), job.snapshot())
            with mock.patch.object(lc.os, 'killpg', wraps=os.killpg) as killpg:
                job.cancel()
                result = job.wait(8)
            self.assertTrue(killpg.called)
        self.assertEqual(result['state'], 'cancelled', result)
        self.assertTrue(any('KILL QUERY 41' in sql for sql, unused in target.calls))
        self.assertEqual(self.store.audit_records()[0]['status'], 'unknown')
        self.assertFalse(job.thread.is_alive())

    def test_connection_values_are_copied_before_background_start(self):
        tools = self.native_tools()
        connection = dict(CONNECTION)
        plan = lc.prepare_dump(self.conn, connection, self.output, schemas=['app'], tools=tools)
        factory = mock.Mock(return_value=self.conn)
        job = lc.dump_job(connection, plan, True, plan['target'], self.audit('dump'), factory=factory, start=False)
        connection.update(host='unconfirmed-host', password='changed')
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = job.run()
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(factory.call_args[1]['host'], CONNECTION['host'])
        self.assertEqual(factory.call_args[1]['password'], PASSWORD)

    def test_tool_version_change_invalidates_confirmed_plan(self):
        tools = self.native_tools()
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=tools)
        changed = copy.deepcopy(tools)
        changed['tools']['mysqldump']['version'] = 'mysqldump Ver 9.6.0'
        with mock.patch.object(lc, 'detect_tools', return_value=changed):
            result = lc.dump_job(CONNECTION, plan, True, plan['target'], self.audit('dump'),
                                 factory=lambda **kw: self.conn, start=False).run()
        self.assertEqual(result['state'], 'refused', result)
        self.assertFalse(os.path.exists(self.output))

    def test_clone_progress_report_and_mariadb_same_source_guard(self):
        report = lc.clone_progress(self.conn)
        self.assertEqual(report['schema_version'], 'clone-progress-1.0')
        self.assertEqual(report['status'][0][0], 'Completed')
        old = lc.read_server(ReplayConnection(server_variables('mariadb')))
        new = lc.read_server(ReplayConnection(server_variables('mariadb', version='11.8.0-MariaDB')))
        self.assertTrue(lc._same_server(old, new))

    def test_bounded_events_roundtrip_and_python37_syntax(self):
        job = lc.LifecycleJob('test', lambda j: {'status': 'completed'})
        for index in range(250):
            job.emit('line %d' % index)
        self.assertEqual(len(job.snapshot()['events']), 200)
        self.assertIsNone(job.snapshot()['progress'])
        self.assertEqual(json.loads(json.dumps(job.snapshot())), job.snapshot())
        if sys.version_info >= (3, 8):
            ast.parse(Path(lc.__file__).read_text(encoding="utf-8"), feature_version=7)
        with self.assertRaises(ValueError):
            lc.clone_statement(dict(CONNECTION, password='a\\b'), redact=False)

    # Synthetic regressions for review findings; no live server claims.

    def replace_sql_backup(self):
        Path(self.output, 'dump.sql').write_text('DROP DATABASE unexpected;\n')
        path = Path(self.output, lc.MANIFEST)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest['files'] = lc._file_records(self.output)
        path.write_text(json.dumps(manifest))

    def test_review_restore_replacement_after_rebuild_refused(self):
        self.make_dump()
        target, tools = self.target(), self.native_tools()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        audit = self.audit()
        attempt = audit.attempt
        def replace(*args, **kwargs):
            result = attempt(*args, **kwargs)
            self.replace_sql_backup()
            return result
        with mock.patch.object(lc, 'detect_tools', return_value=tools), mock.patch.object(audit, 'attempt', side_effect=replace):
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], audit,
                                    factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['state'], 'refused', result)
        self.assertFalse((self.bin / 'restored').exists())
        self.assertTrue(audit.finished)

    def test_review_sql_copy_uses_reviewed_record(self):
        self.make_dump()
        target, tools = self.target(), self.native_tools()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        original = lc._copy_reviewed_file
        def swapped(directory, record, output, job):
            self.replace_sql_backup()
            return original(directory, record, output, job)
        with mock.patch.object(lc, 'detect_tools', return_value=tools), mock.patch.object(lc, '_copy_reviewed_file', side_effect=swapped):
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(),
                                    factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['state'], 'refused', result)
        self.assertFalse((self.bin / 'restored').exists())

    def test_review_shell_load_uses_private_checked_snapshot(self):
        self.make_dump(shell=True)
        target = self.target()
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=self.tools)
        invoke, staged_paths = lc._invoke, []
        def swapped(job, tool, connection, args, script=None, **kwargs):
            if script and 'util.loadDump' in script and '"dryRun": false' in script:
                path = json.JSONDecoder().raw_decode(script.split('util.loadDump(', 1)[1])[0]
                staged_paths.append(path)
                self.assertNotEqual(os.path.realpath(path), os.path.realpath(self.output))
                Path(self.output, 'app@t.tsv').write_text('replacement')
                self.assertEqual(Path(path, 'app@t.tsv').read_text(encoding="utf-8"), '1\n2\n')
            return invoke(job, tool, connection, args, script, **kwargs)
        with mock.patch.object(lc, '_invoke', side_effect=swapped):
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(),
                                    factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(len(staged_paths), 1)
        self.assertFalse(os.path.exists(staged_paths[0]))

    def test_review_server_identity_reservation_blocks_alias_and_schema(self):
        """Two independent registries still cannot execute on the same UUID."""
        entered, release = threading.Event(), threading.Event()
        server = lc.read_server(self.target())
        alias = dict(CONNECTION, host='db-alias', database='app')
        def plan(operation, connection, target):
            return lc._plan(operation, server, connection, {}, {}, target, ['TEST'], [], [])
        first_plan = plan('restore', CONNECTION, 'db:3306/app')
        second_plan = plan('clone', alias, 'db-alias:3306:/copy')
        def action(job, conn, fresh):
            entered.set()
            release.wait(5)
            return {'status': 'completed', 'verification': []}
        first = lc._write_job('restore', CONNECTION, first_plan, lambda *a: first_plan, action,
                              True, first_plan['target'], self.audit(), factory=lambda **kw: self.target(), start=False)
        self.jobs.append(first)
        try:
            first.start()
            self.assertTrue(entered.wait(5))
            action2 = mock.Mock(return_value={'status': 'completed', 'verification': []})
            second = lc._write_job('clone', alias, second_plan, lambda *a: second_plan, action2,
                                   True, second_plan['target'], self.audit('clone'), factory=lambda **kw: self.target(), start=False)
            result = second.run()
            self.assertEqual(result['state'], 'refused', result)
            action2.assert_not_called()
        finally:
            release.set()
            first.wait(5)
        self.assertFalse(first.thread.is_alive())

    def test_review_directory_reservation_resolves_aliases(self):
        server = lc.read_server(self.target())
        other = lc.read_server(self.conn)
        (self.root / 'alias').symlink_to(self.root, target_is_directory=True)
        first = lc._plan('dump', server, CONNECTION, {'directory': self.output}, {}, 'a', [], [], [])
        second = lc._plan('dump', other, CONNECTION, {'directory': str(self.root / 'alias' / Path(self.output).name)}, {}, 'b', [], [], [])
        with lc._RESERVATIONS.hold('one', lc._resources(first)):
            with self.assertRaises(lc.Refused):
                with lc._RESERVATIONS.hold('two', lc._resources(second)):
                    self.fail('Aliased directory was not reserved')

    def test_review_instance_sql_requires_mysql_drop_and_acknowledgement(self):
        self.make_dump()
        path = Path(self.output, lc.MANIFEST)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest['scope'] = {'schemas': [], 'tables': []}
        path.write_text(json.dumps(manifest))
        target, tools = self.target(), self.native_tools()
        original = target.run
        def actual_catalog(sql, **kwargs):
            if 'FROM information_schema.TABLES WHERE' in sql:
                # Match the real server filter, unlike the generic replay.
                return '' if "NOT IN ('mysql'" in sql else _tsv([['mysql', 'user', 'BASE TABLE', 'InnoDB', 0, 0]])
            return original(sql, **kwargs)
        with mock.patch.object(target, 'run', side_effect=actual_catalog):
            plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
            duplicate = next(c for c in plan['checks'] if c['check'] == 'existing_objects')
            self.assertEqual(duplicate['status'], 'failed')
            self.assertIn('mysql.user', duplicate['objects'])
            plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools, existing_objects='drop')
            self.assertTrue(plan['ready'])
            with mock.patch.object(lc, 'detect_tools', return_value=tools):
                result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(),
                                        factory=lambda **kw: target, start=False).run()
                self.assertEqual(result['state'], 'refused', result)
                self.assertFalse((self.bin / 'restored').exists())
                result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(),
                                        typed_acknowledgement=plan['required_acknowledgement'],
                                        factory=lambda **kw: target, start=False).run()
                self.assertEqual(result['state'], 'done', result)

    def test_review_marker_redaction_does_not_disable_cancel(self):
        for password in ('a', '41'):
            conn = ReplayConnection()
            job = lc.LifecycleJob('restore', lambda j: None, dict(CONNECTION, password=password),
                                   factory=lambda **kw: conn)
            job.emit(job.marker + '41', 'stdout')
            self.assertEqual(job.connection_id, 41)
            self.assertEqual(job.snapshot()['events'], [])
            job.cancel()
            self.assertTrue(any(sql == lc.TAG + 'KILL QUERY 41' for sql, flags in conn.calls))
            job.emit('secret=' + password)
            self.assertNotIn('secret=' + password, job.snapshot()['events'][-1]['text'])

    def test_review_cancel_rebuilt_shell_preflights_reaps_process(self):
        """Real signal-ignoring fake Shell exercises both nested preflights."""
        for operation in ('dump', 'restore'):
            with self.subTest(operation=operation):
                if operation == 'restore':
                    self.make_dump(shell=True)
                    conn = self.target()
                    plan = lc.prepare_restore(conn, CONNECTION, self.output, tools=self.tools)
                    make = lc.restore_job
                else:
                    conn = self.conn
                    plan = lc.prepare_dump(conn, CONNECTION, self.output, schemas=['app'], tools=self.tools)
                    make = lc.dump_job
                (self.bin / 'mode').write_text('slow')
                audit, waiting = self.audit(operation), threading.Event()
                job = make(CONNECTION, plan, True, plan['target'], audit, factory=lambda **kw: conn, start=False)
                self.jobs.append(job)
                emit = job.emit
                def observed(text, stream='status'):
                    emit(text, stream)
                    if '25% waiting' in text:
                        waiting.set()
                job.emit = observed
                job.start()
                self.assertTrue(waiting.wait(8), job.snapshot())
                process = job.process
                try:
                    result = job.close()
                    self.assertEqual(result['state'], 'cancelled')
                    self.assertFalse(job.thread.is_alive())
                    self.assertIsNotNone(process.poll())
                    self.assertTrue(audit.finished)
                    self.assertFalse(os.path.exists(self.output) if operation == 'dump' else (self.bin / 'restored').exists())
                finally:
                    (self.bin / 'mode').unlink()

    def test_review_cancel_before_start_is_audited_once(self):
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.native_tools())
        audit = self.audit('dump')
        factory = mock.Mock(side_effect=AssertionError('must not connect'))
        job = lc.dump_job(CONNECTION, plan, True, plan['target'], audit, factory=factory, start=False)
        job.cancel()
        result = job.run()
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual([(r['status'], r['outcome']) for r in self.store.audit_records()], [('declined', 'cancelled')])
        self.assertTrue(audit.finished)
        self.assertEqual(result['result']['audit_id'], audit.id)
        job.close()
        self.assertEqual(len(self.store.audit_records()), 1)
        factory.assert_not_called()

    def test_review_preflight_runner_observes_owner_cancellation(self):
        """Deterministic counterpart to the real subprocess cleanup test."""
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=self.tools)
        owner = lc.dump_job(CONNECTION, plan, True, plan['target'], self.audit('dump'),
                            factory=lambda **kw: self.conn, start=False)
        runners = []
        def dry_run(runner, *args, **kwargs):
            runners.append(runner)
            owner.cancelled.set()
            runner.check_cancel()
            return '', ''
        with mock.patch.object(lc, '_invoke', side_effect=dry_run):
            result = owner.run()
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(len(runners), 1)
        self.assertTrue(runners[0].cancelled.is_set())

    def test_review_metadata_and_hash_work_observe_cancellation(self):
        self.make_dump()
        job = lc.LifecycleJob('restore', lambda j: None)
        job.cancelled.set()
        with self.assertRaises(lc.LifecycleError):
            lc.read_manifest(self.output, job=job)
        connection = mock.Mock(return_value='')
        bounded = lc._BoundedConnection(connection, job)
        with self.assertRaises(lc.LifecycleError):
            bounded.run('SELECT 1')
        connection.run.assert_not_called()
        job.cancelled.clear()
        def cancelled_during_read(*args, **kwargs):
            job.cancelled.set()
            return ''
        connection.run.side_effect = cancelled_during_read
        with self.assertRaises(lc.LifecycleError):
            bounded.run('SELECT 1')
        self.assertLessEqual(connection.run.call_args[1]['timeout'], 5)

    def test_review_cancel_during_final_hash_keeps_incomplete_manifest(self):
        tools = self.native_tools()
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=tools)
        owner = lc.dump_job(CONNECTION, plan, True, plan['target'], self.audit('dump'),
                            factory=lambda **kw: self.conn, start=False)
        original = lc._file_records
        def cancel_hash(directory, job=None):
            owner.cancelled.set()
            return original(directory, job=job)
        with mock.patch.object(lc, 'detect_tools', return_value=tools), mock.patch.object(lc, '_file_records', side_effect=cancel_hash):
            result = owner.run()
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse(json.loads(Path(self.output, lc.MANIFEST).read_text(encoding="utf-8"))['complete'])

    def test_review_windows_force_cancel_has_no_sigkill_and_kills_tree(self):
        job = lc.LifecycleJob('test', lambda j: None)
        job._started = True
        job.process = mock.Mock(pid=4242)
        job.thread = mock.Mock()
        job.thread.is_alive.side_effect = [True, True]
        with mock.patch.object(lc.os, 'name', 'nt'), mock.patch.object(lc, 'signal', types.SimpleNamespace(SIGTERM=15)), \
                mock.patch.object(lc.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as kill:
            job.close()
        commands = [call[0][0] for call in kill.call_args_list]
        self.assertTrue(any('/T' in c and '/F' in c and '4242' in c for c in commands), commands)
        self.assertEqual(job.thread.join.call_args[0], ())

    def test_review_registry_shutdown_joins_and_rejects_new_jobs(self):
        entered = threading.Event()
        def work(job):
            entered.set()
            job.cancelled.wait(5)
            job.check_cancel()
        registry = lc.LifecycleJobs()
        job = lc.LifecycleJob('test', work)
        registry.add(job, 'target')
        self.assertTrue(entered.wait(5))
        registry.close()
        self.assertFalse(job.thread.is_alive())
        with self.assertRaises(ValueError):
            registry.add(lc.LifecycleJob('test', lambda j: None), 'target')

    def private_groups_file(self):
        runtime = self.root / 'runtime'
        runtime.mkdir(mode=0o700, exist_ok=True)
        runtime.chmod(0o700)
        return str(runtime / 'tool-groups-test.json')

    @staticmethod
    def listed_groups(path):
        if not os.path.exists(path):
            return None
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        assert value['schema_version'] == 'tool-groups-1.0', value
        return value['groups']

    def close_workspace_during(self, operation):
        """Close the workspace (WorkspaceServer.server_close) while a tool runs.

        The fake tool ignores SIGTERM and has a helper in its process group
        that ignores it too. Editor cleanup stands in for the slow cleanup that
        used to run first (it can block on KILL QUERY); it records what it sees.
        """
        from myflames.ui import WorkspaceServer
        if operation == 'dump':
            conn, tools = self.conn, self.native_tools()
            plan = lc.prepare_dump(conn, CONNECTION, self.output, schemas=['app'], tools=tools)
            make = lc.dump_job
        else:
            self.make_dump()
            conn, tools = self.target(), self.native_tools()
            plan = lc.prepare_restore(conn, CONNECTION, self.output, tools=tools)
            make = lc.restore_job
        (self.bin / 'mode').write_text('tree')
        groups_file = self.private_groups_file()
        registry = lc.LifecycleJobs(groups_file)
        self.assertEqual(self.listed_groups(groups_file), [])
        ready = threading.Event()
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            job = make(CONNECTION, plan, True, plan['target'], self.audit(operation),
                       factory=lambda **kw: conn, start=False)
            self.jobs.append(job)
            emit = job.emit
            def observed(text, stream='status'):
                emit(text, stream)
                if '25% waiting' in text:
                    ready.set()
            job.emit = observed
            registry.add(job, plan['target'])
            self.assertTrue(ready.wait(8), job.snapshot())
            for unused in range(400):
                if operation == 'dump' or job.connection_id == 41:
                    break
                threading.Event().wait(0.01)
            pgid = job.process.pid
            self.assertEqual(self.listed_groups(groups_file), [pgid])
            self.assertTrue((self.bin / 'grandchild').exists())
            server = object.__new__(WorkspaceServer)
            server.lifecycle, server.jobs = registry, mock.Mock()
            seen = {}
            def editor_close():
                seen['tool_alive'] = lc._group_alive(pgid)
                seen['listed'] = self.listed_groups(groups_file)
                seen['audit'] = [(r['status'], r['outcome']) for r in self.store.audit_records()
                                 if r['operation'] == operation]
            server.editor = mock.Mock(**{'close.side_effect': editor_close})
            with mock.patch('http.server.ThreadingHTTPServer.server_close'):
                server.server_close()
        # The tool group, helper included, was gone before the slower cleanup
        # began, and the job had recorded the cancel exactly like a user's.
        self.assertFalse(seen['tool_alive'])
        self.assertEqual(seen['listed'], [])
        self.assertEqual(seen['audit'], [('unknown', 'cancelled')])
        self.assertFalse(lc._group_alive(pgid))
        self.assertEqual(self.listed_groups(groups_file), [])
        snapshot = job.snapshot()
        self.assertEqual(snapshot['state'], 'cancelled', snapshot)
        self.assertNotEqual(snapshot['result']['status'], 'completed')
        self.assertFalse(job.thread.is_alive())
        with self.assertRaises(ValueError):
            registry.add(lc.LifecycleJob('test', lambda j: None), 'other')
        return conn

    @unittest.skipUnless(os.name == 'posix', 'tool process groups are POSIX')
    def test_closing_workspace_mid_dump_stops_tool_group_before_slow_cleanup(self):
        self.close_workspace_during('dump')
        self.assertFalse(json.loads(Path(self.output, lc.MANIFEST).read_text(encoding="utf-8"))['complete'])

    @unittest.skipUnless(os.name == 'posix', 'tool process groups are POSIX')
    def test_closing_workspace_mid_restore_stops_tool_and_its_server_session(self):
        target = self.close_workspace_during('restore')
        # Read before the tool stopped, so the server-side statement is cancelled too.
        self.assertTrue(any(sql == lc.TAG + 'KILL QUERY 41' for sql, unused in target.calls))

    @unittest.skipUnless(os.name == 'posix', 'tool process groups are POSIX')
    def test_tool_groups_file_is_private_and_lists_only_live_groups(self):
        import subprocess
        path = self.private_groups_file()
        groups = lc.ToolGroups(path)
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), {'schema_version': 'tool-groups-1.0', 'groups': []})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        child = subprocess.Popen([sys.executable, '-c', 'import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                                  'print("ready", flush=True)\ntime.sleep(60)'],
                                 stdout=subprocess.PIPE, start_new_session=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), b'ready')
        # The job thread reaps its leader; here a helper thread does.
        threading.Thread(target=child.wait, daemon=True).start()
        groups.add(child.pid)
        self.assertEqual(self.listed_groups(path), [child.pid])
        self.assertEqual(groups.terminate(term_wait=0.2, kill_wait=5), [])
        self.assertEqual(child.wait(5), -9)
        self.assertEqual(self.listed_groups(path), [])
        groups.close()
        self.assertEqual(self.listed_groups(path), [])
        shared = self.root / 'shared'
        shared.mkdir()
        shared.chmod(0o755)
        (self.root / 'alias').symlink_to(self.root / 'runtime')
        for refused in (str(shared / 'groups.json'), str(self.root / 'alias' / 'groups.json'),
                        str(self.root / 'missing' / 'groups.json'), 'relative-groups.json'):
            with self.subTest(path=refused), self.assertRaises(ValueError):
                lc.ToolGroups(refused)
        self.assertEqual(os.listdir(str(shared)), [])

    @unittest.skipUnless(os.name == 'posix', 'tool process groups are POSIX')
    def test_tool_that_cannot_be_recorded_is_stopped_at_once(self):
        """If the desktop's record cannot be written, a force-killed workspace would orphan the tool."""
        import subprocess
        path = self.private_groups_file()
        registry = lc.LifecycleJobs(path)
        job = lc.LifecycleJob('dump', lambda j: None, CONNECTION)
        job.groups = registry.groups
        started = []
        real_popen = subprocess.Popen
        def popen(*args, **kwargs):
            started.append(real_popen(*args, **kwargs))
            return started[-1]
        script = 'import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)'
        with mock.patch.object(registry.groups, '_write', side_effect=OSError('disk full')), \
                mock.patch.object(lc.subprocess, 'Popen', side_effect=popen):
            with self.assertRaisesRegex(lc.LifecycleError, 'Could not record the tool process'):
                job.process_tool([sys.executable, '-c', script], dict(os.environ))
        self.assertIsNotNone(started[0].poll())
        self.assertFalse(lc._group_alive(started[0].pid))
        self.assertTrue(job.cancelled.is_set())
        self.assertIsNone(job.process)
        # Removed, not stale: the desktop treats a missing record as unconfirmed.
        self.assertIsNone(self.listed_groups(path))
        self.assertEqual(registry.groups.snapshot(), [])

    @unittest.skipUnless(os.name == 'posix', 'tool process groups are POSIX')
    def test_unwritable_record_is_never_left_readable(self):
        path = self.private_groups_file()
        groups = lc.ToolGroups(path)
        groups.add(4242)
        self.assertEqual(self.listed_groups(path), [4242])
        # Neither replace nor unlink works: the list is emptied in place.
        with mock.patch.object(groups, '_write', side_effect=OSError('read-only')), \
                mock.patch.object(lc.os, 'unlink', side_effect=PermissionError('read-only')):
            groups.discard(4242)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), '')

    def test_tool_start_is_serialized_with_shutdown(self):
        """A start in progress is listed before shutdown's snapshot; none starts afterwards."""
        groups = lc.ToolGroups()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def slow_spawn():
            entered.set()
            release.wait(5)
            return types.SimpleNamespace(pid=987654)
        starter = threading.Thread(target=groups.start, args=(slow_spawn,), daemon=True)
        starter.start()
        self.assertTrue(entered.wait(5))
        closer = threading.Thread(target=lambda: (groups.close_spawning(timeout=5), closed.set()), daemon=True)
        closer.start()
        self.assertFalse(closed.wait(0.2))
        release.set()
        self.assertTrue(closed.wait(5))
        self.assertEqual(groups.snapshot(), [987654])
        spawn = mock.Mock()
        with self.assertRaisesRegex(lc.LifecycleError, 'closing'):
            groups.start(spawn)
        spawn.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'tool process groups are POSIX')
    def test_shutdown_stops_preflight_runner_tools_and_refuses_new_ones(self):
        registry = lc.LifecycleJobs()
        started, errors = threading.Event(), []
        script = ('import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                  'sys.stderr.write("25% waiting\\n"); sys.stderr.flush()\ntime.sleep(60)')
        def preflight():
            with registry.runner('upgrade_check', CONNECTION) as runner:
                emit = runner.emit
                def observed(text, stream='status'):
                    emit(text, stream)
                    if '25% waiting' in text:
                        started.set()
                runner.emit = observed
                try:
                    runner.process_tool([sys.executable, '-c', script], dict(os.environ))
                except lc.LifecycleError as exc:
                    errors.append(str(exc))
        thread = threading.Thread(target=preflight, daemon=True)
        thread.start()
        self.assertTrue(started.wait(8))
        pgid = next(iter(registry.runners)).process.pid
        self.assertEqual(registry.groups.snapshot(), [pgid])
        self.assertEqual(registry.shutdown(term_wait=0.2), [])
        thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertFalse(lc._group_alive(pgid))
        self.assertTrue(errors and errors[0].startswith('Cancelled'), errors)
        self.assertEqual(registry.groups.snapshot(), [])
        with self.assertRaises(ValueError):
            with registry.runner('dump_preflight', CONNECTION):
                pass
        registry.close()

    @unittest.skipUnless(os.name == 'posix', 'tool process groups are POSIX')
    def test_workspace_server_keeps_tool_groups_file_only_where_the_desktop_asked(self):
        from myflames import ui
        path = self.private_groups_file()
        server = ui.WorkspaceServer(store_path=str(self.root / 'ws.sqlite3'), tool_groups_file=path)
        try:
            self.assertEqual(self.listed_groups(path), [])
        finally:
            server.server_close()
        self.assertEqual(self.listed_groups(path), [])
        assets = self.root / 'assets'
        assets.mkdir()
        (assets / 'index.html').write_text('<html></html>')
        with mock.patch.object(ui, 'ASSET_DIR', assets), \
                mock.patch.dict(os.environ, {'MYFLAMES_TOOL_GROUPS_FILE': str(self.root / 'missing' / 'g.json')}), \
                mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit) as caught:
                ui.cmd_ui(['--no-browser', '--no-restore'])
        self.assertEqual(caught.exception.code, 2)

    def test_review_tool_connection_failure_category_survives_password_redaction(self):
        # The password overlaps the numeric error; the display is redacted
        # while the failure category remains useful to the CLI.
        tool = self.bin / 'mysql'
        tool.write_text('#!' + sys.executable + '\nimport sys\nsys.stderr.write("ERROR 2003: Cannot connect")\nsys.exit(1)\n')
        connection = dict(CONNECTION, password='2003')
        job = lc.LifecycleJob('restore', lambda j: lc._invoke(j, self.tools['tools']['mysql'], connection, []), connection)
        result = job.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['failure_category'], 'connection')
        self.assertNotIn('2003', result['error'])

    def test_review_samples_bound_attempts_deadline_and_failures(self):
        catalog = [['app', 't%d' % n, 'BASE TABLE', 'InnoDB', '0', '0'] for n in range(5000)]
        conn = mock.Mock()
        conn.run.side_effect = ConnectorError('COUNT timeout')
        samples = lc._samples(conn, catalog)
        self.assertEqual(conn.run.call_count, 3)
        self.assertTrue(all(s['status'] == 'not_checked' for s in samples))
        clock = [0.0]
        conn.reset_mock()
        def timeout(sql, **kwargs):
            clock[0] += kwargs['timeout']
            raise ConnectorError('COUNT timeout')
        conn.run.side_effect = timeout
        with mock.patch.object(lc.time, 'monotonic', side_effect=lambda: clock[0]):
            lc._samples(conn, catalog)
        self.assertEqual(conn.run.call_count, 1)
        self.assertLessEqual(clock[0], 5)

    def test_review_restore_staging_space_checked_and_location_exposed(self):
        self.make_dump()
        tools, target = self.native_tools(), self.target()
        stage = self.root / 'staging'
        stage.mkdir()
        with mock.patch.object(lc.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=0)):
            plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools)
        self.assertFalse(plan['ready'])
        check = next(c for c in plan['checks'] if c['check'] == 'staging_space')
        self.assertGreater(check['required_bytes'], 0)
        plan = lc.prepare_restore(target, CONNECTION, self.output, tools=tools, staging_directory=str(stage))
        self.assertIn(str(stage), next(c for c in plan['checks'] if c['check'] == 'staging_space')['reason'])
        temporary = tempfile.TemporaryFile
        with mock.patch.object(lc, 'detect_tools', return_value=tools), mock.patch.object(lc.tempfile, 'TemporaryFile', wraps=temporary) as create:
            result = lc.restore_job(CONNECTION, plan, True, plan['target'], self.audit(),
                                    factory=lambda **kw: target, start=False).run()
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(create.call_args[1]['dir'], str(stage))

    def test_review_manifest_nested_paths_are_portable_and_confined(self):
        self.make_dump()
        nested = Path(self.output, 'app', 'chunk.tsv')
        nested.parent.mkdir()
        nested.write_text('1\n')
        relpath = lc.os.path.relpath
        with mock.patch.object(lc.os, 'sep', '\\'), mock.patch.object(lc.os.path, 'relpath',
                 side_effect=lambda path, directory: relpath(path, directory).replace('/', '\\')):
            records = lc._file_records(self.output)
        self.assertIn('app/chunk.tsv', [r['path'] for r in records])
        path = Path(self.output, lc.MANIFEST)
        data = json.loads(path.read_text(encoding="utf-8"))
        data['files'] = records
        path.write_text(json.dumps(data))
        self.assertEqual(lc.read_manifest(self.output)['files'], records)
        for unsafe in ('../dump.sql', '/absolute.sql', 'C:/file.sql', 'app\\chunk.tsv', 'app//chunk.tsv'):
            data['files'][0]['path'] = unsafe
            path.write_text(json.dumps(data))
            with self.subTest(path=unsafe), self.assertRaises(lc.Refused):
                lc.read_manifest(self.output)

    def test_review_removed_persisted_variables_independent_of_running_source(self):
        for source in ('DYNAMIC', 'COMPILED'):
            data = json.loads((FIXTURES / 'native-8.0.46-to-8.4.0.json').read_text(encoding="utf-8"))
            conn = StrictReplay(data['calls'] + data['synthetic_calls'])
            saved_sql = data['synthetic_calls'][0]['sql']
            conn.answers[saved_sql] = 'default_authentication_plugin\n'
            # SET PERSIST and SET PERSIST_ONLY leave these running sources;
            # the old WHERE source=PERSISTED correctly returns no rows.
            for call in data['calls']:
                if 'performance_schema.variables_info' in call['sql']:
                    # Neither COMPILED nor DYNAMIC matches either original
                    # filter, despite the persisted table containing a row.
                    conn.answers[call['sql']] = ''
            running = ('/*myflames:lifecycle*/ SELECT VARIABLE_NAME,VARIABLE_SOURCE FROM '
                       'performance_schema.variables_info')
            conn.answers[running] = 'default_authentication_plugin\t' + source + '\n'
            report = lc.upgrade_check(conn, '8.4.0', CONNECTION, {'tools': {}})
            check = next(c for c in report['checks'] if c['id'] == 'removed_persisted_variables')
            self.assertEqual(check['affected_objects'], ['default_authentication_plugin'])
            self.assertTrue(any('performance_schema.persisted_variables' in sql for sql, flags in conn.calls))

    def test_review_clone_effective_role_grants_and_unknown(self):
        donor, recipient = ReplayConnection(), self.target()
        for conn in (donor, recipient):
            conn.grants = ["GRANT `maintenance`@`%` TO `operator`@`%`"]
            conn.current_roles = "`maintenance`@`%`"
            conn.expanded_grants = ['GRANT BACKUP_ADMIN, CLONE_ADMIN ON *.* TO `operator`@`%`']
        plan = lc.prepare_clone(donor, recipient, dict(CONNECTION, password=DONOR_PASSWORD), CONNECTION, tools=self.tools)
        for side in ('donor', 'recipient'):
            self.assertEqual(next(c for c in plan['checks'] if c['check'] == side + '_privilege')['status'], 'passed')
        recipient.deny_roles = True
        plan = lc.prepare_clone(donor, recipient, dict(CONNECTION, password=DONOR_PASSWORD), CONNECTION, tools=self.tools)
        self.assertEqual(next(c for c in plan['checks'] if c['check'] == 'recipient_privilege')['status'], 'not_checked')
        self.assertTrue(plan['ready'])

    # ---- regressions found on live servers (2026-10-15, see PROVENANCE.txt) ----

    def test_restore_with_schema_reads_scope_first(self):
        """prepare_restore used the manifest scope before reading it (UnboundLocalError) whenever a schema was given."""
        self.make_dump()
        plan = lc.prepare_restore(self.target(), CONNECTION, self.output, schema='app', tools=self.native_tools())
        self.assertEqual(plan['target'], '127.0.0.1:3306/app')
        with self.assertRaises(lc.Refused):
            lc.prepare_restore(self.target(), CONNECTION, self.output, schema='other', tools=self.native_tools())

    def test_output_path_under_a_symlinked_folder(self):
        """macOS /tmp and /var are links: a dump under them was refused. The parent is resolved and bound."""
        real = self.root / 'real'
        real.mkdir()
        (self.root / 'alias').symlink_to(real)
        plan = lc.prepare_dump(self.conn, CONNECTION, str(self.root / 'alias' / 'out'), schemas=['app'],
                               tools=self.native_tools())
        self.assertEqual(plan['target'], os.path.join(os.path.realpath(str(real)), 'out'))
        (self.root / 'alias' / 'linked').symlink_to(real)
        with self.assertRaises(lc.Refused):
            lc.prepare_dump(self.conn, CONNECTION, str(self.root / 'alias' / 'linked'), tools=self.native_tools())
        # A folder swapped for a link after the preflight refuses before writing.
        swapped = os.path.join(str(self.root), 'alias', 'out')
        with self.assertRaises(lc.Refused):
            lc._empty_directory(swapped)

    def test_cancel_signal_tolerates_eperm_from_zombie_group(self):
        """macOS answers EPERM to killpg while the group holds only an unreaped zombie."""
        job = lc.LifecycleJob('test', lambda j: None, CONNECTION)
        job.process = mock.Mock(pid=4242)
        with mock.patch.object(lc.os, 'killpg', side_effect=PermissionError(1, 'Operation not permitted')):
            job._signal(15)
        with mock.patch.object(lc.os, 'killpg', side_effect=ProcessLookupError()):
            job._signal(15)

    def test_shell_marker_ends_its_line_and_colors_are_stripped(self):
        """mysqlsh print() adds no line break: the marker ran into the next output, the session id was never
        read (so cancel could not KILL QUERY) and the marker leaked into the log."""
        seen = []
        plan, result = self.make_dump(shell=True)
        texts = [event['text'] for event in result['events']]
        self.assertFalse(any('myflames_job_' in text for text in texts), texts)
        self.assertIn('Initializing - done', texts)
        self.assertFalse(any('\x1b' in text or '\\u001b' in text for text in texts), texts)
        self.assertIn('NOTE: Progress information uses estimated values', texts)
        job = lc.LifecycleJob('test', lambda j: None, CONNECTION)
        job.emit(job.marker + '73')
        self.assertEqual(job.connection_id, 73)
        self.assertIn('+ "\\n")', lc._shell_script(job, 'util.dumpInstance("/x", {})'))

    def test_tools_never_fetch_untrusted_server_public_keys(self):
        for name in ('mysqldump', 'mysqlsh', 'mariadb-dump'):
            tool = dict(self.tools['tools'][name], name=name)
            for mode in (None, 'DISABLED', 'PREFERRED', 'REQUIRED', 'VERIFY_IDENTITY'):
                with self.subTest(tool=name, mode=mode):
                    with lc._credentials(dict(CONNECTION, ssl_mode=mode), tool) as (argv, env):
                        content = Path(argv[1].split('=', 1)[1]).read_text(encoding="utf-8")
                    self.assertNotIn('get-server-public-key', content)
                    self.assertTrue(argv[1].startswith('--defaults-file='))

    def test_missing_tool_message_names_the_engine(self):
        with self.assertRaises(lc.Refused) as caught:
            lc._pick(self.native_tools('mysql'), ('mariadb-dump',), 'mariadb')
        self.assertIn('Install MariaDB client tools', str(caught.exception))
        tools = {'tools': {'mysql': dict(self.tools['tools']['mysql'])}}
        with self.assertRaises(lc.Refused) as caught:
            lc._pick(tools, ('mariadb', 'mysql'), 'mariadb')
        self.assertIn('mysql here is from MySQL', str(caught.exception))

    def test_clone_donor_address_as_the_recipient_reaches_it(self):
        """Live: myflames reached the donor at 127.0.0.1:33841 while the recipient container reached it as
        mflc7-src:3306; the statement and the clone_valid_donor_list check must use the latter."""
        donor_options = dict(CONNECTION, password=DONOR_PASSWORD, port=33841)
        statement = lc.clone_statement(donor_options, donor_address='donor-host:3306')
        self.assertIn("FROM 'operator'@'donor-host':3306", statement)
        donor, recipient = self.clone_facts(), self.clone_facts('recipient')
        recipient['server']['variables']['clone_valid_donor_list'] = 'donor-host:3306'
        checks = lc.clone_preflight(donor, recipient, donor_options, CONNECTION, donor_address='donor-host:3306')
        self.assertEqual(next(c for c in checks if c['check'] == 'valid_donor')['status'], 'passed')
        checks = lc.clone_preflight(donor, recipient, donor_options, CONNECTION)
        self.assertEqual(next(c for c in checks if c['check'] == 'valid_donor')['status'], 'failed')
        for bad in ('donor-host', "x'y:3306", 'host:0', 'host:port', ' :3306'):
            with self.subTest(address=bad), self.assertRaises(ValueError):
                lc.donor_endpoint(donor_options, bad)
        self.assertEqual(lc.donor_endpoint(donor_options, '[::1]:3307'), ('::1', 3307))

    def test_clone_data_directory_without_host_probe_is_ready(self):
        """The server refuses an existing directory or one inside its datadir before copying (seen live:
        1007 for /tmp), so without a host probe the check is not_checked, not a block."""
        donor, recipient = self.clone_facts(), self.clone_facts('recipient')
        checks = lc.clone_preflight(donor, recipient, CONNECTION, CONNECTION, '/backups/copy')
        status = {c['check']: c['status'] for c in checks}
        self.assertEqual(status['directory_checked'], 'not_checked')
        self.assertFalse(any(c['status'] == 'failed' for c in checks), checks)
        checks = lc.clone_preflight(donor, recipient, CONNECTION, CONNECTION, '/server/data/copy')
        self.assertEqual({c['check']: c['status'] for c in checks}['directory_outside_datadir_text'], 'failed')
        checks = lc.clone_preflight(donor, recipient, CONNECTION, CONNECTION, 'relative')
        failed = [c['check'] for c in checks if c['status'] == 'failed']
        self.assertEqual(failed, ['directory_absolute'])

    def run_clone(self, mode=None, data_directory=None, **target_changes):
        if mode:
            (self.bin / 'mode').write_text(mode)
        donor_options = dict(CONNECTION, password=DONOR_PASSWORD)
        donor, recipient = ReplayConnection(), self.target()
        recipient.variables['clone_valid_donor_list'] = '127.0.0.1:3306'
        if data_directory:
            recipient.clone_destination = data_directory
        plan = lc.prepare_clone(donor, recipient, donor_options, CONNECTION, data_directory=data_directory,
                                tools=self.tools)
        for key, value in target_changes.items():
            setattr(recipient, key, value)
        connections = iter([recipient, donor])
        def factory(**kw):
            return next(connections, recipient)
        with mock.patch.object(lc, 'RESTART_POLL', 0.01):
            return lc.clone_job(donor_options, CONNECTION, plan, True, plan['target'], self.audit('clone'),
                                factory=factory, start=False, restart_wait=2).run()

    def test_in_place_clone_restart_is_waited_for_and_verified(self):
        """Live on MySQL 8.4.11: the client got ERROR 3707, the server shut down, a supervisor restarted it,
        and clone_status then said Completed with the donor's GTID set."""
        for mode in ('restart', 'gone'):
            with self.subTest(mode=mode):
                result = self.run_clone(mode, down_reads=2)
                self.assertEqual(result['state'], 'done', result)
                self.assertTrue(result['result']['restarted'])
                self.assertEqual(result['result']['status'], 'completed')
                self.assertTrue(any('restarting after the clone' in e['text'] for e in result['events']))

    def test_in_place_clone_without_a_supervisor_needs_a_manual_start(self):
        result = self.run_clone('restart', down_reads=10 ** 6)
        self.assertEqual(result['result']['status'], 'needs_restart')
        self.assertIn('start it by hand', result['result']['verification'][0]['reason'])
        self.assertEqual(self.store.audit_records()[0]['status'], 'ok')

    def test_in_place_clone_recipient_now_has_donor_accounts(self):
        result = self.run_clone('restart', down_reads=10 ** 6,
                                down_error="ERROR 1045 (28000): Access denied for user 'operator'")
        self.assertEqual(result['result']['status'], 'needs_verification')
        self.assertIn("donor's accounts", result['result']['verification'][0]['reason'])

    def test_clone_status_row_from_an_earlier_clone_fails_verification(self):
        """clone_status keeps the previous clone's row; it must match this clone's begin time, source and
        destination, or a stale Completed row would pass."""
        for change in ({'clone_begin': '2026-10-15 09:00:00.000'}, {'clone_source': 'old-donor:3306'},
                       {'clone_destination': '/elsewhere/'}):
            with self.subTest(change=change):
                result = self.run_clone(**change)
                self.assertEqual(result['result']['status'], 'verification_failed', result)

    def test_data_directory_clone_without_gtid_in_clone_status_completes(self):
        """Live: a DATA DIRECTORY clone leaves GTID_EXECUTED empty in clone_status."""
        result = self.run_clone(data_directory='/backups/copy', clone_gtid='')
        self.assertEqual(result['result']['status'], 'completed', result)
        gtid = next(v for v in result['result']['verification'] if v['check'] == 'gtid')
        self.assertEqual(gtid['status'], 'not_checked')
        self.assertIn('DATA DIRECTORY', gtid['reason'])
        self.assertFalse(result['result']['restarted'])

    def test_clone_stage_percent_from_live_progress_rows(self):
        progress = {'progress': [['DROP DATA', 'Completed', '0', '0', '0', '0'],
                                 ['FILE COPY', 'In Progress', '485971560', '148330088', '148346956', '51880068'],
                                 ['PAGE COPY', 'Not Started', '0', '0', '0', '0'],
                                 ['REDO COPY', 'Not Started', '0', '0', '0', '0']]}
        self.assertEqual(lc._clone_stage(progress), ('FILE COPY', 'In Progress', 30.5))
        self.assertIsNone(lc._clone_stage({'progress': []}))
        self.assertEqual(lc._clone_stage({'progress': [['DROP DATA', 'Not Started', '0', '0']]})[2], None)

    def test_partial_state_names_what_remains(self):
        tools = self.native_tools()
        plan = lc.prepare_dump(self.conn, CONNECTION, self.output, schemas=['app'], tools=tools)
        (self.bin / 'mode').write_text('fail')
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            result = lc.dump_job(CONNECTION, plan, True, plan['target'], self.audit('dump'),
                                 factory=lambda **kw: self.conn, start=False).run()
        self.assertIn(os.path.realpath(self.output), result['result']['partial_state'])
        self.assertIn('marks it incomplete', result['result']['partial_state'])

    def test_registry_limits_running_jobs_and_targets(self):
        gate = threading.Event()
        def slow(job):
            gate.wait(5)
            return {'status': 'completed', 'verification': []}
        jobs = lc.LifecycleJobs()
        self.addCleanup(jobs.close)
        self.addCleanup(gate.set)
        first = jobs.add(lc.LifecycleJob('dump', slow), 'target-a')
        with self.assertRaises(ValueError):
            jobs.add(lc.LifecycleJob('restore', slow), 'target-a')
        jobs.add(lc.LifecycleJob('dump', slow), 'target-b')
        with self.assertRaises(ValueError):
            jobs.add(lc.LifecycleJob('dump', slow), 'target-c')
        gate.set()
        self.assertEqual(jobs.get(first['id']).wait(5)['state'], 'done')
        with self.assertRaises(ValueError):
            jobs.get('missing')


if __name__ == '__main__':
    unittest.main()
