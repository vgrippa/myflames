"""CLI for server lifecycle operations: upgrade-check, dump, restore, clone.

The work is in lifecycle.py; this module adds the command line. Writes
(dump, restore, clone) follow the db-operations contract: the connection's
write policy runs before any password prompt or connection, the plan (tool,
exact commands, checks, warnings) is shown on stderr, and the person
confirms: dump with [y/N], restore and clone by typing the target. Without a
terminal, --yes is required, plus --confirm-target TARGET for restore and
clone. Every attempt, declined and refused ones included, is audited.

Exit codes: 0 done (upgrade-check: report written, --fail-on not tripped);
1 a started job failed, was cancelled, or could not be verified; 2 nothing
was started, bad input or connection failure. Unsent failures use stderr only.
"""
import argparse
import getpass
import json
import signal
import sys

from .connector import ConnectorError, terminal_text
from .workspace_cli import (_add_connection_args, _add_output_args, _bounded_integer, _connection_settings,
                            _emit_text, _endpoint, _fail, _resolve_connection, _saved_profile)

PROMPT = '__PROMPT__'


def _parser(name, description, epilog):
    parser = argparse.ArgumentParser(prog='myflames ' + name, add_help=False, description=description,
                                     epilog=epilog)
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    _add_connection_args(parser)
    return parser


def _options(settings):
    """lifecycle connection fields from resolved CLI settings (never printed)."""
    from .live import connection_options
    options = connection_options(settings)
    options.pop('connect_timeout', None)
    options.pop('query_timeout', None)
    return options


def _planned(args):
    """The endpoint the flags and --profile point at, without a password: the
    write policy is decided from it before any prompt or connection."""
    from .workspace_store import PROFILE_FIELDS
    profile = _saved_profile(args)
    planned = {f: profile[f] for f in PROFILE_FIELDS if profile and profile.get(f) not in (None, '')}
    planned.update(_connection_settings(args, None))
    planned.setdefault('host', '127.0.0.1')
    planned.setdefault('port', 3306)
    return profile, planned


def _say(text, quiet=False):
    if not quiet:
        sys.stderr.write(terminal_text(text) + '\n')
        sys.stderr.flush()


# ---- text projections -------------------------------------------------------------------

_LEVEL = {'error': 'ERROR', 'warning': 'WARNING', 'notice': 'NOTICE'}


def _count(number, word):
    return '%d %s%s' % (number, word, '' if number == 1 else 's')


def format_upgrade(report, limit=20):
    """upgrade-check-1.0 as text: findings first, then passed and not checked."""
    server = report['server']
    method = {'mysqlsh': 'MySQL Shell', 'native_subset': 'native subset, partial',
              'mariadb_upgrade_guidance': 'MariaDB guidance'}.get(report.get('method'), report.get('method'))
    lines = ['Upgrade check: %s %s -> %s (%s)' % ('MariaDB' if server['engine'] == 'mariadb' else 'MySQL',
                                                 server['version'], report['target_version'], method)]
    counts = report.get('counts') or {}
    lines.append('Status: %s (%s, %s, %s)' % (
        str(report.get('status', '')).replace('_', ' '), _count(counts.get('error', 0), 'error'),
        _count(counts.get('warning', 0), 'warning'), _count(counts.get('notice', 0), 'notice')))
    found = [c for c in report.get('checks', []) if c.get('affected_objects') or
             c.get('status') in ('ERROR', 'CONFIGURATION_ERROR')]
    passed = [c['id'] for c in report.get('checks', []) if not c.get('affected_objects')
              and c.get('status') in ('passed', 'OK')]
    unchecked = [c for c in report.get('checks', []) if c.get('status') == 'not_checked']
    order = {'error': 0, 'warning': 1, 'notice': 2}
    for check in sorted(found, key=lambda c: order.get(c.get('severity'), 3)):
        lines.append('')
        lines.append('%-8s %s: %s' % (_LEVEL.get(check.get('severity'), 'NOTICE'), check['id'], check.get('title', '')))
        objects = check.get('affected_objects', [])
        for name in objects[:limit]:
            lines.append('         ' + str(name))
        if len(objects) > limit:
            lines.append('         ... and %d more (--json lists all)' % (len(objects) - limit))
        if check.get('description'):
            lines.append('         ' + ' '.join(str(check['description']).split()))
    if passed:
        lines.append('')
        lines.append('Passed: ' + ', '.join(passed))
    for check in unchecked:
        lines.append('Not checked: %s (%s)' % (check['id'], ' '.join(str(check.get('description') or '').split())))
    for item in report.get('manual_checks') or []:
        title = item.get('title') or item.get('id') if isinstance(item, dict) else item
        lines.append('Check by hand: ' + str(title))
    if report.get('limitations'):
        lines.append('')
        lines.append('Limitations:')
        lines.extend('  - ' + text for text in report['limitations'])
    return '\n'.join(lines) + '\n'


def format_plan(plan):
    """lifecycle-plan-1.0 as the description shown before the confirmation."""
    server = plan['server']
    lines = ['%s on %s %s at %s' % (plan['operation'].upper(), 'MariaDB' if server['engine'] == 'mariadb' else 'MySQL',
                                    server['version'], server['identity'].get('hostname') or '?'),
             'Target: ' + plan['target'],
             'Tool: %s (%s)' % (plan['tool'].get('name'), (plan['tool'].get('version') or '').strip()[:80])]
    if plan.get('donor'):
        lines.append('Donor: MySQL %s at %s, reached by the recipient as %s' % (
            plan['donor']['version'], plan['donor']['identity'].get('hostname'), plan['request'].get('donor_address')))
    lines.append('Commands:')
    lines.extend('  ' + command for command in plan['commands'])
    lines.append('Checks:')
    for check in plan['checks']:
        lines.append('  %-11s %s: %s' % (check['status'], check['check'], check['reason']))
    for warning in plan['warnings']:
        lines.append('Warning: ' + warning)
    if plan.get('required_acknowledgement'):
        lines.append('Required acknowledgement: ' + plan['required_acknowledgement'])
    if not plan['ready']:
        lines.append('Not ready: fix the failed checks above, then run it again.')
    return '\n'.join(lines) + '\n'


def format_job(snapshot):
    """lifecycle-job-1.0 as the text result."""
    result = snapshot.get('result') or {}
    state = snapshot.get('state') or ''
    status = str(result.get('status') or '').replace('_', ' ')
    heading = '%s: %s' % (snapshot.get('operation', '').capitalize(), state)
    if status and status != state:
        heading += ' (%s)' % status
    error = ' '.join((snapshot.get('error') or '').split())
    if error:
        heading += ': ' + error
    lines = [heading]
    if result.get('directory'):
        lines.append('Directory: ' + result['directory'])
    for item in result.get('verification', []):
        reason = ' '.join((item.get('reason') or '').split())
        if (item.get('check') == 'operation' and item.get('status') == 'not_checked'
                and (not reason or reason == error)):
            # Job failures also carry the error as an operation verification.
            # Keep it in JSON, but show the reason only once in text.
            continue
        extra = ''
        if item.get('check') == 'row_count' and 'expected' in item:
            extra = ' %s.%s expected %s, got %s' % (item.get('schema'), item.get('table'), item.get('expected'),
                                                    item.get('actual'))
        lines.append('  %-11s %s%s: %s' % (item['status'], item['check'], extra, item.get('reason', '')))
    if result.get('restarted'):
        lines.append('The recipient restarted after the clone.')
    if snapshot.get('state') in ('failed', 'cancelled') and result.get('partial_state'):
        lines.append(result['partial_state'])
    if result.get('cancel_error'):
        lines.append('Cancel: ' + result['cancel_error'])
    if result.get('audit_id'):
        lines.append('Audit record: %s (myflames audit)' % result['audit_id'])
    return '\n'.join(lines) + '\n'


def _emit(args, value, text):
    if value.get('schema_version') == 'lifecycle-job-1.0' and value.get('id') is None:
        _fail((value.get('result') or {}).get('message') or value.get('error') or text.strip())
    if args.json:
        _emit_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', args.output,
                   quiet=args.quiet)
    else:
        _emit_text(text, args.output, quiet=args.quiet)


def _unsent(operation, state, status, message, audit_id=None):
    """A lifecycle-job-1.0 envelope for an attempt that never started."""
    result = {'status': status, 'verification': []}
    if audit_id:
        result['audit_id'] = audit_id
    return {'schema_version': 'lifecycle-job-1.0', 'id': None, 'operation': operation, 'state': state,
            'progress': None, 'detail': None, 'events': [], 'result': result, 'error': message}


# ---- upgrade-check ----------------------------------------------------------------------

def cmd_upgrade_check(argv):
    """Check a MySQL server for problems before an upgrade (MySQL Shell, else a partial native subset)."""
    from . import lifecycle as lc
    parser = _parser(
        'upgrade-check',
        'Check a MySQL server before an upgrade to --target-version. Uses MySQL Shell '
        '(util.checkForServerUpgrade) when it is installed; otherwise, or with --no-shell, a partial '
        'native subset that lists what it checked and never says an upgrade is safe. On MariaDB it prints '
        'the mariadb-upgrade guidance (run on the server host after the new binaries are installed).',
        'Reads only: needs PROCESS and SELECT (Shell), or SELECT on mysql.user and the information_schema '
        'tables (native subset). Exit codes: 0 report written, 1 --fail-on tripped, 2 bad input, a version '
        'pair the Shell cannot check, or connection failure.')
    parser.add_argument('--target-version', required=True, metavar='X.Y.Z',
                        help='Check an upgrade to this MySQL version, for example 8.4.0.')
    parser.add_argument('--no-shell', action='store_true',
                        help='Run the partial native subset even when MySQL Shell is installed.')
    parser.add_argument('--config-path', metavar='PATH',
                        help='Pass the server option file to MySQL Shell (read for 5.7 sources).')
    parser.add_argument('--fail-on', choices=('error', 'warning'),
                        help='Exit 1 when a check reports this level or worse (CI gate).')
    _add_output_args(parser, 'the upgrade-check-1.0 report')
    args = parser.parse_args(argv)
    password = None if args.password == PROMPT else args.password
    try:
        settings = _resolve_connection(args)
        password = settings.get('password')
        options = _options(settings)
        with lc._connect(lc.LifecycleConnection, options) as conn:
            report = lc.upgrade_check(conn, args.target_version, options, config_path=args.config_path,
                                      prefer_shell=not args.no_shell)
    except KeyboardInterrupt:
        _fail('Cancelled.', password)
    except EOFError:
        _fail('Password prompt cancelled.', password)
    except lc.Refused as exc:
        message = str(exc)
        if 'MySQL Shell' in message:
            message += ' Use --no-shell for the partial native subset.'
        _fail(message, password)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        _fail(exc, password)
    report['server'] = {k: report['server'][k] for k in ('engine', 'version', 'identity')}
    _emit(args, report, format_upgrade(report))
    counts = report.get('counts') or {}
    failed_checks = any(c.get('status') in ('ERROR', 'CONFIGURATION_ERROR') for c in report.get('checks', []))
    tripped = (args.fail_on in ('error', 'warning') and failed_checks) or \
              (args.fail_on == 'error' and counts.get('error', 0) > 0) or \
              (args.fail_on == 'warning' and (counts.get('error', 0) + counts.get('warning', 0)) > 0)
    raise SystemExit(1 if tripped else 0)


# ---- dump, restore, clone ----------------------------------------------------------------

def _write_args(parser, destructive):
    parser.add_argument('--preflight', action='store_true',
                        help='Run the checks and print the plan, then stop: nothing is written '
                             '(exit 0 when ready, 1 when a check failed).')
    parser.add_argument('--yes', action='store_true',
                        help='Confirm without a prompt (required when stdin is not a terminal)' +
                             (', together with --confirm-target.' if destructive else '.'))
    if destructive:
        parser.add_argument('--confirm-target', metavar='TARGET',
                            help='The target exactly as the plan shows it (host:port, plus /schema or '
                                 ':directory), to confirm with --yes.')
    _add_output_args(parser, 'the plan (--preflight) or the lifecycle-job-1.0 result')


def _confirm(args, plan, destructive, passwords=()):
    """Return the typed target and an optional audited refusal description."""
    from . import dbops
    need = ' --yes --confirm-target %s' % plan['target'] if destructive else ' --yes'
    no_terminal = ('Not sent: stdin is not a terminal, so myflames cannot ask you '
                   'to confirm. Re-run with%s.' % need)
    required = plan.get('required_acknowledgement')
    if required:
        problem = dbops.cli_confirm(None,
            'Type the acknowledgement to permit system-table replacement: ' + required + '\n',
            assume_yes=args.yes, expected=required, supplied=getattr(args, 'acknowledge_system_tables', None),
            passwords=passwords, no_terminal_message=no_terminal,
            mismatch_message='Not sent: type the required acknowledgement, or pass '
                             '--acknowledge-system-tables ' + repr(required) + ' with --yes.',
            mismatch_outcome='refused')
        if problem:
            # Preserve the audit category for an incorrect acknowledgement.
            if problem[0] == 'refused' and problem[1] != 'no_terminal':
                problem = ('refused', 'system_tables', problem[2])
            return None, problem
        args.acknowledge_system_tables = required
    if destructive:
        supplied = getattr(args, 'confirm_target', None)
        mismatch = ('Not sent: with --yes, name the target as the plan shows it: --confirm-target %s'
                    if args.yes and supplied is None else 'Not sent: --confirm-target must be exactly %s.') % plan['target']
        if not args.yes:
            mismatch = 'Not sent: the typed target did not match.'
        if plan.get('operation') == 'restore' and (not args.yes or supplied is not None):
            from .lifecycle import restore_target_mismatch
            mismatch = restore_target_mismatch(plan['target'])
        problem = dbops.cli_confirm(None, 'Type the target to confirm (%s): ' % plan['target'],
            assume_yes=args.yes, expected=plan['target'], supplied=supplied, passwords=passwords,
            no_terminal_message=no_terminal, mismatch_message=mismatch,
            decline_message='Not sent: you did not confirm.')
    else:
        problem = dbops.cli_confirm(None, 'Start the dump into %s? [y/N] ' % plan['target'],
            assume_yes=args.yes, passwords=passwords, no_terminal_message=no_terminal,
            decline_message='Not sent: you did not confirm.')
    return (None, problem) if problem else (plan['target'], None)


def _run_job(args, job):
    """Start *job*, stream its progress to stderr, cancel it on Ctrl+C."""
    job.start()
    seen, last_progress = 0, None
    try:
        while job.thread.is_alive():
            job.thread.join(0.5)
            snap = job.snapshot()
            fresh = job.emitted - seen
            seen = job.emitted
            if not args.quiet:
                for event in snap['events'][-fresh:] if fresh > 0 else []:
                    _say('  ' + event['text'])
                if snap['progress'] is not None and snap['progress'] != last_progress:
                    _say('  %.0f%%' % snap['progress'])
                    last_progress = snap['progress']
    except KeyboardInterrupt:
        _say('Cancelling: stopping the tool and its server session...')
        job.cancel()
        previous = None
        try:
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
        try:
            job.close()
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
    finally:
        if job.thread.is_alive():
            job.close()
    return job.snapshot()


def _exit_code(snapshot):
    result = snapshot.get('result') or {}
    if snapshot.get('state') == 'failed' and result.get('failure_category') in ('input', 'connection'):
        return 2
    return 0 if snapshot.get('state') == 'done' and result.get('status') == 'completed' else 1


def _write_command(args, operation, prepare, make_job, destructive, passwords=None):
    """The shared flow of dump, restore and clone (db-operations contract)."""
    from . import dbops, lifecycle as lc
    from .workspace_store import WorkspaceStore
    profile, planned = _planned(args)
    safety = dbops.connection_safety(profile, planned)
    decision = dbops.decide('risky', safety['type'], safety['read_only'], safety['profile_name'])
    password = None if args.password == PROMPT else args.password
    # Share this list with donor resolution: a secret must be tracked before
    # validation or connection setup can raise, including during preflight.
    passwords = [] if passwords is None else passwords
    passwords.append(password)
    audit = None
    if not args.preflight:
        audit = dbops.AuditTrail(WorkspaceStore(), operation, 'cli', connection=_endpoint(planned), safety=safety,
                                 password=password, policy=dbops.policy_summary(decision))
        if decision['action'] == 'refuse':
            message = lc._safe(decision['text'], passwords)
            audit_id = audit.outcome([operation], 'refused', outcome='read_only', message=message)
            _emit(args, _unsent(operation, 'refused', 'refused', message, audit_id),
                  'Not sent: ' + message + '\n')
            raise SystemExit(1)
    plan = None
    try:
        settings = _resolve_connection(args)
        password = settings.get('password')
        passwords.append(password)
        options = _options(settings)
        options.update(profile_id=safety.get('profile_id'), type=safety['type'], read_only=safety['read_only'])
        if audit is not None:
            audit.password, audit.connection = password, _endpoint(settings)
        plan = prepare(options)
        if args.preflight:
            _emit(args, lc._report_safe(plan, passwords), lc._safe(format_plan(plan), passwords))
            raise SystemExit(0 if plan['ready'] else 1)
        _say(lc._safe(format_plan(plan).rstrip('\n') + '\n  ' + decision['text'], passwords), args.quiet and args.yes)
        if not plan['ready']:
            message = 'Not sent: the preflight has failed checks.'
            audit_id = audit.outcome(lc._safe(plan['commands'], passwords), 'refused', outcome='preflight', message=message,
                                     target=lc._safe(plan['server']['identity'], passwords))
            _emit(args, _unsent(operation, 'refused', 'refused', message, audit_id), message + '\n')
            raise SystemExit(1)
        typed, problem = _confirm(args, plan, destructive, passwords)
        if problem:
            status, outcome, message = problem
            message = lc._safe(message, passwords)
            audit_id = audit.outcome(lc._safe(plan['commands'], passwords), status, outcome=outcome, message=message,
                                     target=lc._safe(plan['server']['identity'], passwords))
            _emit(args, _unsent(operation, status, status, message, audit_id), message + '\n')
            raise SystemExit(1)
        job = make_job(options, plan, typed, audit, safety)
        lc.bind_profile_policy(job, options, safety, lambda key: audit.store.get('profile', key))
        snapshot = _run_job(args, job)
    except KeyboardInterrupt:
        if audit is not None and not audit.finished:
            audit.outcome(lc._safe(plan['commands'] if plan else [operation], passwords), 'declined', outcome='declined',
                          message='Cancelled before anything was sent.')
        _fail(lc._safe('Cancelled.', passwords))
    except EOFError:
        if audit is not None and not audit.finished:
            audit.outcome(lc._safe(plan['commands'] if plan else [operation], passwords), 'declined', outcome='declined',
                          message='Password prompt cancelled; nothing was sent.')
        _fail(lc._safe('Password prompt cancelled.', passwords))
    except lc.Refused as exc:
        # myflames refused at preflight (a non-empty directory, a changed
        # backup, a server without CLONE): exit 1, nothing was sent.
        audit_id = None
        message = lc._safe(str(exc), passwords)
        if audit is not None and not audit.finished:
            audit_id = audit.outcome(lc._safe(plan['commands'] if plan else [operation], passwords), 'refused',
                                     outcome='preflight', message=message)
        message = 'Not sent: ' + message
        _emit(args, _unsent(operation, 'refused', 'refused', message, audit_id), message + '\n')
        raise SystemExit(1)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        message = lc._safe(str(exc), passwords)
        if audit is not None and not audit.finished:
            audit.fail(message, lc._safe(plan['commands'] if plan else [operation], passwords))
        _fail(message)
    _emit(args, lc._report_safe(snapshot, passwords), lc._safe(format_job(snapshot), passwords))
    raise SystemExit(_exit_code(snapshot))


def cmd_dump(argv):
    """Dump schemas (or the whole instance) into a new directory with a verified manifest."""
    from . import lifecycle as lc
    parser = _parser(
        'dump',
        'Dump a server into DIRECTORY (absent or empty) with MySQL Shell util.dumpSchemas/dumpTables/'
        'dumpInstance when installed, else mysqldump (MySQL) or mariadb-dump (MariaDB). Writes '
        'myflames-manifest.json with every file\'s SHA-256 and exact row counts of small tables, which '
        'restore verifies.',
        'Shows the plan and asks before starting. The connection\'s write policy applies: a read-only '
        'connection refuses (MySQL Shell takes a brief global read lock). Passwords go to the tools in a 0600 '
        'option file, never on the command line. Ctrl+C stops the tool and keeps the partial directory, '
        'marked incomplete. Exit codes: 0 dumped and verified, 1 a started job failed or was cancelled, '
        '2 nothing was started, bad input or connection failure.')
    parser.add_argument('directory', metavar='DIRECTORY', help='Write the dump here (must be absent or empty).')
    parser.add_argument('--schema', action='append', metavar='NAME',
                        help='Dump this database; repeat for more (default: every database).')
    parser.add_argument('--table', action='append', metavar='NAME',
                        help='Dump only this table of the one --schema; repeat for more.')
    parser.add_argument('--threads', type=_bounded_integer(1, 64), default=4, metavar='N',
                        help='Use N threads with MySQL Shell, 1-64 (default: 4).')
    parser.add_argument('--no-shell', action='store_true',
                        help='Use mysqldump or mariadb-dump even when MySQL Shell is installed.')
    _write_args(parser, False)
    args = parser.parse_args(argv)

    def prepare(options):
        with lc._connect(lc.LifecycleConnection, options) as conn:
            return lc.prepare_dump(conn, options, args.directory, schemas=args.schema or [],
                                   tables=args.table or [], threads=args.threads, prefer_shell=not args.no_shell)

    def make_job(options, plan, typed, audit, safety):
        return lc.dump_job(options, plan, confirmed=True, typed_target=typed, audit=audit, safety=safety,
                           start=False)
    _write_command(args, 'dump', prepare, make_job, False)


def cmd_restore(argv):
    """Restore a verified myflames dump into a server, then verify objects and row counts."""
    from . import lifecycle as lc
    parser = _parser(
        'restore',
        'Restore DIRECTORY, a dump made by myflames dump, into the server. The manifest and every file\'s '
        'SHA-256 are checked first; MySQL Shell dumps load with util.loadDump (needs local_infile=ON on the '
        'target), SQL dumps through the mysql or mariadb client on stdin. After it, myflames compares the '
        'objects and the exact row counts recorded at dump time.',
        'Restore runs DDL and DML and cannot be undone: you type the target to confirm. It refuses when the '
        'target already has the dumped tables unless --existing names a choice, and when the target is the '
        'dump\'s own source unless --allow-source. Exit codes: 0 restored and verified, 1 a started job failed, '
        'was cancelled or could not be verified, 2 nothing was started, bad input or connection failure.')
    parser.add_argument('directory', metavar='DIRECTORY', help='Restore the dump in this directory.')
    parser.add_argument('--schema', metavar='NAME',
                        help='Load into this database (MySQL Shell dumps of tables from one database only).')
    parser.add_argument('--existing', choices=('refuse', 'ignore', 'drop'), default='refuse',
                        help='What to do with tables that already exist: refuse (default), ignore them '
                             '(MySQL Shell only), or drop and recreate them.')
    parser.add_argument('--threads', type=_bounded_integer(1, 64), default=4, metavar='N',
                        help='Use N threads with MySQL Shell, 1-64 (default: 4).')
    parser.add_argument('--resume', action='store_true',
                        help='Continue an interrupted MySQL Shell load from its progress file.')
    parser.add_argument('--allow-source', action='store_true',
                        help='Allow restoring into the server the dump was taken from.')
    parser.add_argument('--staging-directory', metavar='PATH',
                        help='Stage the checked restore snapshot here (default: system temporary directory).')
    parser.add_argument('--acknowledge-system-tables', metavar='TEXT',
                        help='With --yes, type the exact system-table acknowledgement from the preflight.')
    _write_args(parser, True)
    args = parser.parse_args(argv)
    if args.confirm_target and not args.yes:
        parser.error('--confirm-target needs --yes')

    def prepare(options):
        with lc._connect(lc.LifecycleConnection, options) as conn:
            return lc.prepare_restore(conn, options, args.directory, schema=args.schema,
                                      allow_source_restore=args.allow_source, existing_objects=args.existing,
                                      threads=args.threads, resume=args.resume, staging_directory=args.staging_directory)

    def make_job(options, plan, typed, audit, safety):
        return lc.restore_job(options, plan, confirmed=True, typed_target=typed, audit=audit, safety=safety,
                              start=False, typed_acknowledgement=args.acknowledge_system_tables)
    _write_command(args, 'restore', prepare, make_job, True)


def _donor_settings(args, passwords, recipient=None):
    """The donor connection from --donor-* flags and --donor-profile."""
    from . import dbops, lifecycle as lc
    ns = argparse.Namespace(profile=args.donor_profile, host=args.donor_host, port=args.donor_port,
                            user=args.donor_user, password=args.donor_password, database=None,
                            ssl_mode=args.donor_ssl_mode, ssl_ca=args.donor_ssl_ca)
    profile, planned = _planned(ns)
    safety = dbops.connection_safety(profile, planned)
    if args.no_require_ssl and (planned.get('ssl_mode') in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY') or
                               (profile or {}).get('ssl_mode') in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY')):
        raise lc.Refused('The donor requires TLS; clone must use REQUIRE SSL.')
    if ns.password == PROMPT:
        ns.password = getpass.getpass('Donor password: ')
        passwords.append(ns.password)
    saved = bool(profile and profile.get('remember_password') and ns.password is None)
    lc.require_clone_recipient_tls(planned, recipient or {}, saved=saved)
    settings = _resolve_connection(ns)
    passwords.append(settings.get('password'))
    options = _options(settings)
    # A read-only donor can supply data, but its policy is still bound into
    # the reviewed pair and must reach the job's fresh preflight unchanged.
    options.update(profile_id=safety.get('profile_id'), type=safety['type'], read_only=safety['read_only'])
    if saved:
        options['_saved_password'] = True
    return options


def cmd_clone(argv):
    """Clone a MySQL donor into the connected recipient with CLONE INSTANCE (MySQL only)."""
    from . import lifecycle as lc
    parser = _parser(
        'clone',
        'Copy a MySQL donor into the recipient (the server the connection flags point at) with CLONE '
        'INSTANCE. Without --data-directory the recipient DROPS ALL ITS USER DATA, takes the donor\'s data '
        'and accounts, and restarts; with it, the copy goes to a new directory on the recipient host and '
        'nothing restarts. MariaDB has no CLONE: myflames prints the mariadb-backup steps instead.',
        'Needs the clone plugin on both servers, BACKUP_ADMIN for the donor account, CLONE_ADMIN on the '
        'recipient (BACKUP_ADMIN with --data-directory), and the donor in the recipient\'s '
        'clone_valid_donor_list (myflames never sets it). The donor password goes on the client\'s stdin, '
        'never on a command line, and is redacted in the audit trail. Exit codes: 0 cloned and verified, '
        '1 a started job failed, was cancelled or not verified (for example a recipient that did not '
        'restart), 2 nothing was started, bad input or connection failure.')
    donor = parser.add_argument_group('Donor (the server copied from)')
    donor.add_argument('--donor-profile', metavar='NAME_OR_ID',
                       help='Use this saved connection, with its saved password, for the donor.')
    donor.add_argument('--donor-host', metavar='HOST', help='Connect to the donor at this host.')
    donor.add_argument('--donor-port', type=_bounded_integer(1, 65535), metavar='PORT',
                       help='Connect to the donor on this port (default: 3306).')
    donor.add_argument('--donor-user', metavar='USER', help='Use this donor account (needs BACKUP_ADMIN).')
    donor.add_argument('--donor-password', nargs='?', const=PROMPT, metavar='PASSWORD',
                       help="Use '--donor-password' alone to prompt, or '--donor-password=secret'.")
    donor.add_argument('--donor-ssl-mode', choices=('DISABLED', 'PREFERRED', 'REQUIRED', 'VERIFY_CA',
                                                    'VERIFY_IDENTITY'),
                       help='Choose the TLS mode myflames uses to read the donor.')
    donor.add_argument('--donor-ssl-ca', metavar='PATH', help='Verify the donor with this CA bundle.')
    donor.add_argument('--donor-address', metavar='HOST:PORT',
                       help='The donor as the recipient reaches it, when that differs from --donor-host and '
                            '--donor-port (containers, NAT, a proxy).')
    parser.add_argument('--data-directory', metavar='PATH',
                        help='Clone into this new absolute directory on the recipient host instead of '
                             'replacing the recipient\'s data.')
    parser.add_argument('--no-require-ssl', action='store_true',
                        help='Send REQUIRE NO SSL: copy without TLS between the servers (default: REQUIRE SSL).')
    parser.add_argument('--restart-wait', type=_bounded_integer(0, 3600), default=lc.RESTART_WAIT, metavar='S',
                        help='Wait up to S seconds for the recipient to come back after an in-place clone '
                             '(default: %d).' % lc.RESTART_WAIT)
    _write_args(parser, True)
    args = parser.parse_args(argv)
    if args.confirm_target and not args.yes:
        parser.error('--confirm-target needs --yes')
    if not (args.donor_profile or args.donor_host):
        parser.error('name the donor with --donor-host or --donor-profile')
    holder = {}
    passwords = [None if args.donor_password == PROMPT else args.donor_password]

    def prepare(options):
        donor_options = _donor_settings(args, passwords, options)
        holder['donor'] = donor_options
        with lc._connect(lc.LifecycleConnection, options) as recipient:
            server = lc.read_server(recipient, options)
            if server['engine'] == 'mariadb':
                guidance = lc.physical_backup_guidance(recipient, options, args.data_directory or '/backup')
                raise lc.Refused('MariaDB has no CLONE. Copy it with mariadb-backup on the source host: ' +
                                 '; '.join(guidance['commands']) + '. ' + ' '.join(guidance['warnings']))
            with lc._connect(lc.LifecycleConnection, donor_options) as donor_conn:
                return lc.prepare_clone(donor_conn, recipient, donor_options, options,
                                        data_directory=args.data_directory, require_ssl=not args.no_require_ssl,
                                        donor_address=args.donor_address)

    def make_job(options, plan, typed, audit, safety):
        from . import dbops
        donor = holder['donor']
        job = lc.clone_job(donor, options, plan, confirmed=True, typed_target=typed, audit=audit,
                           safety=safety, start=False, restart_wait=args.restart_wait)
        profile = audit.store.get('profile', donor['profile_id']) if donor.get('profile_id') else None
        donor_safety = dbops.connection_safety(profile, donor)
        donor_safety.update(type=donor['type'], read_only=donor['read_only'])
        lc.bind_profile_policy(job, donor, donor_safety, lambda key: audit.store.get('profile', key), write=False)
        return job
    _write_command(args, 'clone', prepare, make_job, True, passwords)
