"""``myflames privileges`` and ``myflames accounts`` (Users and roles).

``privileges`` prints the connected account's effective privileges and the
answer for every action key (privileges-1.0). ``accounts`` lists, shows and
changes users and roles:

    myflames accounts list | show ACCOUNT
    myflames accounts create | drop | lock | unlock | expire | password ACCOUNT
    myflames accounts rename ACCOUNT NEW_ACCOUNT
    myflames accounts grant | revoke ACCOUNT PRIVILEGES --on TARGET
    myflames accounts role grant | revoke ACCOUNT ROLE [ROLE ...]
    myflames accounts role default ACCOUNT [ROLE ... | --none | --all]

Writes follow the db-operations contract: the connection's write policy
first (a read-only connection refuses before any password prompt), the plan
and its statements on stderr, ``[y/N]`` on a terminal or ``--yes``, the
account name typed for protected accounts and self-lockout (``--confirm-name``
without a terminal), then accounts.apply, which re-reads and audits. A new
password comes from a prompt or ``--password-stdin``, never from argv.
Exit codes: 0 done, 1 a sent operation was not applied fully, 2 nothing was
sent, bad input, a connection failure or an unknown outcome. Unsent failures
write a diagnostic to stderr and leave stdout and output files untouched.
"""
import argparse
import getpass
import json
import sys

from . import accounts, dbops, privileges
from .connector import ConnectorError, terminal_text
from .server_info import server_label
from .workspace_cli import (_add_connection_args, _connection_settings, _emit_text, _endpoint, _fail,
                            _resolve_connection, _saved_profile, _table)

_ACCOUNT_HELP = "The account as user@host (quotes allowed: 'app'@'10.0.%%'); a MariaDB role is just its name."
_WRITE_OPERATIONS = ('create', 'drop', 'rename', 'lock', 'unlock', 'expire', 'password', 'grant', 'revoke', 'role')
_KINDS = {'PROCEDURE': 'PROCEDURE', 'FUNCTION': 'FUNCTION', 'LIBRARY': 'LIBRARY', 'PACKAGE': 'PACKAGE'}


def _parser(name, description, epilog=None):
    parser = argparse.ArgumentParser(prog='myflames ' + name, add_help=False, description=description, epilog=epilog,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    _add_connection_args(parser)
    return parser


def _read_output(parser, what):
    parser.add_argument('--json', action='store_true', help='Emit %s as machine-readable JSON.' % what)
    parser.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
    parser.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; rely on stdout and the '
                                                                   'exit code.')


def _write_flags(parser):
    parser.add_argument('--yes', action='store_true', help='Apply without the [y/N] prompt (needed without a '
                                                           'terminal).')
    parser.add_argument('--confirm-name', metavar='ACCOUNT', help='Type the account name in advance when the '
                                                                  'change needs it (protected account or your own).')
    parser.add_argument('--json', action='store_true', help='Emit a sent account-change-1.0 result as machine-readable '
                                                            'JSON.')
    parser.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
    parser.add_argument('-q', '--quiet', action='store_true', help='With --yes, drop the plan description on stderr.')


def _connect(args):
    from .connector import MySQLConnection
    from .live import connection_options
    settings = _resolve_connection(args)
    options = connection_options(settings)
    options['query_timeout'] = 30
    return settings, MySQLConnection(**options)


def _read(args, work):
    password = None if args.password == '__PROMPT__' else args.password
    try:
        settings, conn = _connect(args)
        password = settings.get('password')
        with conn:
            return work(conn)
    except KeyboardInterrupt:
        _fail('Cancelled.', password)
    except EOFError:
        _fail('Password prompt cancelled.', password)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        _fail(privileges.redact(str(exc), [password]), password)


# ---- privileges ------------------------------------------------------------------------

def format_privileges(report):
    lines = ['%s on %s' % (report.get('account'), server_label(report, 'version'))]
    active = ', '.join(r['name'] + ('@' + r['host'] if r.get('host') else '') for r in report.get('current_roles', []))
    lines.append('Active roles: %s' % (active or 'none'))
    lines.append('')
    lines.append('Grants (a new session of this account):')
    lines.extend('  ' + g for g in report.get('grants', []))
    for state, title in (('denied', 'Denied (the server would refuse)'),
                         ('unknown', 'Not verified (SHOW GRANTS cannot tell; the server decides)'),
                         ('allowed', 'Allowed')):
        rows = [[key, a['reason']] for key, a in report.get('actions', {}).items()
                if a['state'] == state and a.get('available', True)]
        if rows:
            lines.append('')
            lines.append('%s: %d' % (title, len(rows)))
            lines.append(_table(['ACTION', 'WHY'], rows).rstrip('\n'))
    unavailable = [key for key, a in report.get('actions', {}).items() if not a.get('available', True)]
    if unavailable:
        lines.append('')
        lines.append('Not available on this engine or version: ' + ', '.join(unavailable))
    return '\n'.join(lines) + '\n'


def cmd_privileges(argv):
    parser = _parser('privileges', "Show the connected account's effective privileges and, for every myflames "
                                   'action, whether it is allowed, denied or not verifiable from SHOW GRANTS.',
                     'Examples:\n  myflames privileges -u app -p\n  myflames privileges --profile prod --json')
    _read_output(parser, 'the privileges-1.0 report')
    args = parser.parse_args(argv)
    report = _read(args, privileges.read_privileges)
    if args.json:
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', args.output, quiet=args.quiet)
    else:
        _emit_text(format_privileges(report), args.output, quiet=args.quiet)
    if not args.quiet:
        for warning in report.get('warnings', []):
            sys.stderr.write('note: %s\n' % terminal_text(warning))
        sys.stderr.write('note: %s\n' % report.get('session_note', ''))


# ---- accounts: reads -------------------------------------------------------------------

def _yes(value):
    return 'yes' if value else ''


def format_list(report):
    rows = []
    for a in report['accounts']:
        name = a['name'] + ('' if a.get('role') and not a.get('host') else '@' + (a.get('host') or ''))
        kind = 'role' if a.get('role') else 'user'
        flags = ', '.join(f for f, on in (('system', a.get('system')), ('no password', a.get('no_password')),
                                          ('connected', a.get('connected'))) if on)
        rows.append([name, kind, a.get('plugin') or '', _yes(a.get('locked')), _yes(a.get('expired')), flags])
    text = _table(['ACCOUNT', 'TYPE', 'PLUGIN', 'LOCKED', 'EXPIRED', 'NOTES'], rows)
    if report['role_grants']:
        text += '\nRole grants:\n' + _table(['ROLE', 'GRANTED TO', 'ADMIN'], [
            [g['role']['name'] + ('@' + g['role']['host'] if g['role'].get('host') else ''),
             g['grantee']['name'] + ('@' + g['grantee']['host'] if g['grantee'].get('host') else ''),
             _yes(g['admin_option'])] for g in report['role_grants']])
    return text


def _role_hint(conn, name, host, role):
    """Diagnose a MariaDB role only after the requested user was not found.

    MySQL roles share the user namespace. Denied role reads cannot prove
    that a role exists, so keep the original missing-account diagnostic.
    """
    if role or not conn.is_mariadb():
        return None
    try:
        candidate = accounts.read_account(conn, name, host, True, include_catalog=False)
    except (ConnectorError, ValueError):
        return None
    if candidate.get("engine") == "mariadb" and candidate.get("exists"):
        return "%s is a role; repeat the command with --role." % name
    return None


def _read_account(conn, name, host, role):
    report = accounts.read_account(conn, name, host, role)
    if not report["exists"]:
        hint = _role_hint(conn, name, host, role)
        if hint:
            report["role_hint"] = hint
    return report


def format_account(a):
    if not a['exists']:
        return (a.get('role_hint') or 'No such account.') + '\n'
    name = a['name'] + ('' if a.get('role') and a['engine'] == 'mariadb' else '@' + a['host'])
    lines = [name + (' (role)' if a.get('role') else '')]
    attrs = a.get('attributes') or {}
    if attrs:
        lines.append('Locked: %s  Expired: %s  Plugins: %s' % ('yes' if attrs.get('locked') else 'no',
                                                               'yes' if attrs.get('expired') else 'no',
                                                               ', '.join(attrs.get('plugins') or []) or 'none'))
        if attrs.get('limits'):
            lines.append('Limits: ' + ', '.join('%s %s' % kv for kv in sorted(attrs['limits'].items())))
    if a.get('protected'):
        lines.append('Protected: ' + a['protected'])
    if a.get('dangerous'):
        lines.append('Flags: ' + ', '.join(a['dangerous']))
    lines.append('Roles: ' + (', '.join(r['name'] + ('@' + r['host'] if r.get('host') else '') +
                                        (' (admin)' if r.get('admin_option') else '') for r in a['roles']) or 'none'))
    lines.append('Default roles: ' + (', '.join(r['name'] + ('@' + r['host'] if r.get('host') else '')
                                                for r in a['default_roles']) or 'none'))
    lines.append('')
    lines.append('Grants:')
    lines.extend('  ' + g for g in a['grants'])
    if a.get('role_grants'):
        lines.append('')
        lines.append('Through roles:')
        lines.extend('  ' + g for g in a['role_grants'])
    return '\n'.join(lines) + '\n'


# ---- accounts: writes ------------------------------------------------------------------

def _target(text, columns=None, pattern=False, proxy=None):
    """Grant scope options from --on (``*.*``, ``db.*``, ``db.table``,
    ``PROCEDURE db.name``) or --proxy."""
    if proxy:
        name, host = accounts.parse_account(proxy)
        return {'level': 'proxy', 'proxy': {'name': name, 'host': host}}
    if not text:
        raise ValueError('Say where with --on: *.*, db.*, db.table or "PROCEDURE db.name".')
    parts = privileges.tokens(text)
    kind = None
    if parts and parts[0].upper() in _KINDS:
        kind = parts.pop(0).upper()
        if kind == 'PACKAGE' and parts and parts[0].upper() == 'BODY':
            parts.pop(0)
            kind = 'PACKAGE BODY'
    if len(parts) != 3 or parts[1] != '.':
        raise ValueError('Could not read --on %r: use *.*, db.*, db.table or "PROCEDURE db.name".' % text)
    database, obj = privileges.unquote(parts[0]), privileges.unquote(parts[2])
    if parts[0] == '*' and parts[2] == '*':
        if kind or columns:
            raise ValueError('*.* takes no routine type or columns.')
        return {'level': 'global'}
    if kind:
        return {'level': 'routine', 'database': database, 'routine': obj, 'kind': kind}
    if parts[2] == '*':
        if columns:
            raise ValueError('Columns need a table: --on db.table.')
        return {'level': 'database', 'database': database, 'pattern': bool(pattern)}
    if columns:
        return {'level': 'column', 'database': database, 'table': obj, 'columns': columns}
    return {'level': 'table', 'database': database, 'table': obj}


def _new_password(args, label):
    """The new password from --password-stdin or a prompt; never argv."""
    random = getattr(args, 'random_password', False)
    if random and not getattr(args, 'current_password', False):
        return None, None
    if getattr(args, 'password_stdin', False):
        lines = sys.stdin.read().split('\n')
        password = lines[0].rstrip('\r')
        current = lines[1].rstrip('\r') if getattr(args, 'current_password', False) and len(lines) > 1 else None
        if not password and not random:
            raise ValueError('--password-stdin read an empty first line.')
        if getattr(args, 'current_password', False) and not current:
            raise ValueError('--current-password with --password-stdin reads the current password from the second '
                             'line.')
        return None if random else password, current
    if not sys.stdin.isatty():
        if random:
            raise ValueError('No terminal to ask for the current password: use --password-stdin with the current '
                             'password on the second line.')
        raise ValueError('No terminal to ask for the new password: pipe it with --password-stdin, or use '
                         '--random-password.')
    current = getpass.getpass('Current password of %s: ' % label) if getattr(args, 'current_password', False) else None
    if random:
        if not current:
            raise ValueError('An empty current password was given; nothing was sent.')
        return None, current
    first = getpass.getpass('New password for %s: ' % label)
    second = getpass.getpass('Again: ')
    if first != second:
        raise ValueError('The two passwords differ; nothing was sent.')
    if not first:
        raise ValueError('An empty password was given; nothing was sent.')
    return first, current


def _describe(plan):
    lines = ['%s %s on %s %s' % (plan['operation'].upper(), plan['account'], 'MariaDB' if plan['engine'] == 'mariadb'
                                 else 'MySQL', plan['version']), '', 'Statements:']
    lines.extend('  ' + line for s in plan['statements'] for line in s.split('\n'))
    hint = plan['privileges']
    lines.append('')
    lines.append('Privileges: %s' % hint['reason'])
    for warning in plan['warnings']:
        lines.append('Warning: ' + warning)
    for block in plan['guards']['blocked']:
        lines.append('Blocked: ' + block)
    if plan['policy'].get('text'):
        lines.append(plan['policy']['text'])
    return terminal_text('\n'.join(lines))


def _confirm(plan, args):
    """None when confirmed; else (status, message)."""
    if plan['guards']['blocked'] or plan['privileges']['allowed'] is False:
        if not (args.quiet and args.yes):
            sys.stderr.write(_describe(plan) + '\n')
        return None  # apply records this refusal without sending.
    problem = dbops.cli_confirm(_describe(plan), 'Apply %d statement%s to %s? [y/N] ' % (
        len(plan['statements']), '' if len(plan['statements']) == 1 else 's', plan['account']),
        assume_yes=args.yes, quiet=args.quiet)
    return (problem[0], problem[2]) if problem else None


def _typed_name(plan, args):
    needed = plan['guards'].get('typed_name')
    if not needed or plan['guards']['blocked']:
        return args.confirm_name or ''
    if args.confirm_name is not None:
        return args.confirm_name  # apply checks the supplied guard exactly.
    if not sys.stdin.isatty():
        return ''  # apply records the missing typed guard.
    problem = dbops.cli_confirm(None, 'Type %s to confirm: ' % needed, expected=needed)
    return None if problem else needed


def format_change(result):
    lines = ['%s: %s' % (result['status'], result.get('message') or '')]
    for s in result['statements']:
        lines.append('  [%s] %s' % (s['status'], s['statement'].split('\n')[-2 if '\n' in s['statement'] else -1]))
    diff = result.get('diff') or {}
    for g in diff.get('added', []):
        lines.append('  + ' + g)
    for g in diff.get('removed', []):
        lines.append('  - ' + g)
    for w in result.get('warnings', []):
        lines.append('Warning: ' + w)
    if result.get('generated_password'):
        lines.append('Generated password (shown once, not stored): ' + result['generated_password'])
    return '\n'.join(lines) + '\n'


def _write(args, operation, name, host, role, options, label):
    """Policy gate, plan, confirm, apply. Returns the exit code."""
    from .workspace_store import WorkspaceStore, PROFILE_FIELDS
    profile = _saved_profile(args)
    planned = {f: profile[f] for f in PROFILE_FIELDS if profile and profile.get(f) not in (None, '')}
    planned.update(_connection_settings(args, None))
    planned.setdefault('host', '127.0.0.1')
    planned.setdefault('port', 3306)
    if profile:
        planned['profile_id'] = profile['id']
    safety = dbops.connection_safety(profile, planned)
    decision = dbops.decide({'kind': 'risky', 'category': 'account', 'statements': []}, safety['type'],
                            safety['read_only'], safety.get('profile_name'))
    store = WorkspaceStore()
    tagged = '%s%s %s' % (accounts.TAG, operation.upper(), label)
    def unsent(message, outcome, status='refused', plan=None, endpoint=None):
        audit = dbops.AuditTrail(store, 'accounts.' + operation, 'cli', connection=_endpoint(endpoint or planned),
                                 safety=safety, policy=dbops.policy_summary(decision))
        if plan:
            audit.server(plan['engine'], plan['version'])
        statements = list(plan['statements']) if plan else []
        change_set_id = plan['change_set_id'] if plan else None
        audit_id = audit.outcome(statements or [tagged], status, outcome=outcome, message=message,
                                 target={'account': label, 'change_set_id': change_set_id})
        return {'schema_version': accounts.CHANGE_SCHEMA, 'change_set_id': change_set_id,
                'operation': operation, 'account': label, 'status': status, 'message': message,
                'statements': [{'index': i, 'statement': s, 'status': 'not_sent'}
                               for i, s in enumerate(statements)],
                'before': plan['before'] if plan else {}, 'after': {}, 'diff': {}, 'warnings': [],
                'audit_ids': [audit_id]}

    if decision['action'] == 'refuse':
        result = unsent(decision['text'], 'read_only')
        return 1, result
    if not args.yes and (getattr(args, 'password_stdin', False) or not sys.stdin.isatty()):
        message = ('Not sent: --password-stdin requires --yes. Re-run with --yes.'
                   if getattr(args, 'password_stdin', False) else
                   'Not sent: stdin is not a terminal, so myflames cannot ask you to confirm. Re-run with --yes.')
        result = unsent(message, 'needs_confirmation')
        return 1, result
    secrets = {}
    cancelled_audited = False
    plan = None
    connection_password = None if args.password == '__PROMPT__' else args.password
    try:
        if operation in ('create', 'password') and not role:
            try:
                password, current = _new_password(args, label)
            except (EOFError, KeyboardInterrupt):
                unsent('Password prompt cancelled; nothing was sent.', 'declined', status='declined')
                cancelled_audited = True
                raise
            except ValueError:
                unsent('The password could not be supplied; nothing was sent.', 'password_input')
                raise
            if password is not None:
                secrets['password'] = password
            if current is not None:
                secrets['current_password'] = current
        settings, conn = _connect(args)
        connection_password = settings.get('password')
        identity = {k: v for k, v in planned.items() if k != 'password'}
        identity.update({k: settings.get(k) for k in ('host', 'port', 'user', 'ssl_mode', 'ssl_ca')})
        with conn:
            try:
                plan = accounts.plan_change(conn, operation, name, host, role, connection=identity, profile=profile,
                                            **dict(options, **secrets))
            except ValueError as exc:
                if str(exc) == 'That account does not exist.':
                    hint = _role_hint(conn, name, host, role)
                    if hint:
                        raise ValueError(hint) from None
                raise
            refusal = _confirm(plan, args)
            typed = ''
            if not refusal:
                typed = _typed_name(plan, args)
                if typed is None:
                    refusal = ('declined', 'Nothing was sent.')
            if refusal:
                result = unsent(refusal[1], 'declined' if refusal[0] == 'declined' else 'no_terminal',
                                status=refusal[0], plan=plan, endpoint=settings)
                return 1, result
            result = accounts.apply(conn, plan, plan['expect'], confirmed=True, typed_name=typed, store=store,
                                    surface='cli', connection=identity, profile=profile)
    except KeyboardInterrupt:
        if not cancelled_audited and plan is None:
            unsent('Password prompt cancelled; nothing was sent.', 'declined', status='declined')
        _fail('Cancelled. If a statement was already sent, check the account with: myflames accounts show %s'
              % label, connection_password)
    except EOFError:
        if not cancelled_audited:
            unsent('Password prompt cancelled; nothing was sent.', 'declined', status='declined')
        _fail('Password prompt cancelled; nothing was sent.', connection_password)
    except (ValueError, TypeError, OSError, ConnectorError) as exc:
        _fail(privileges.redact(str(exc), [connection_password] + list(secrets.values())), connection_password)
    if result['status'] == 'applied':
        return 0, result
    return (2 if result['status'] == 'unknown' else 1), result


def _account_args(parser, role_flag=True):
    parser.add_argument('account', metavar='ACCOUNT', help=_ACCOUNT_HELP)
    if role_flag:
        parser.add_argument('--role', action='store_true', help='The account is a role.')


def _split_list(text):
    return [part.strip() for part in (text or '').split(',') if part.strip()]


def cmd_accounts(argv):
    epilog = ('Subcommands:\n'
              '  list                         users and roles (no password hashes)\n'
              '  show ACCOUNT                 grants, roles, attributes, flags\n'
              '  create ACCOUNT               new user (--role: new role)\n'
              '  drop | lock | unlock | expire ACCOUNT\n'
              '  password ACCOUNT             set a password (prompt, --password-stdin or --random-password)\n'
              '  rename ACCOUNT NEW_ACCOUNT\n'
              '  grant | revoke ACCOUNT PRIVILEGES --on TARGET\n'
              '  role grant | revoke ACCOUNT ROLE [ROLE ...]\n'
              '  role default ACCOUNT [ROLE ... | --none | --all]\n\n'
              'Examples:\n'
              '  myflames accounts list --profile prod\n'
              "  myflames accounts create 'app'@'10.0.%' -u admin -p\n"
              "  myflames accounts grant app@% 'CREATE ROUTINE' --on 'shop.*'\n"
              '  myflames accounts grant app@% SELECT --on shop.orders --columns a,b\n'
              '  myflames accounts role grant app@% reader --admin-option\n'
              '  printf "%s\\n" "$NEW" | myflames accounts password app@% --password-stdin --yes')
    if not argv or argv[0] in ('-h', '--help'):
        sys.stdout.write('usage: myflames accounts SUBCOMMAND ...\n\nList, show and change users and roles.\n\n'
                         + epilog + '\n')
        return
    sub, rest = argv[0], argv[1:]
    if sub == 'list':
        parser = _parser('accounts list', 'List users and roles without password hashes.')
        _read_output(parser, 'the accounts-1.0 report')
        args = parser.parse_args(rest)
        report = _read(args, accounts.list_accounts)
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n' if args.json else format_list(report),
                   args.output, quiet=args.quiet)
        if not args.quiet:
            for warning in report['warnings']:
                sys.stderr.write('note: %s\n' % warning)
        return
    if sub == 'show':
        parser = _parser('accounts show', 'Show one account: grants, roles, default roles, attributes and flags.')
        _account_args(parser)
        _read_output(parser, 'the account-1.0 report')
        args = parser.parse_args(rest)
        try:
            name, host = accounts.parse_account(args.account)
        except ValueError as exc:
            _fail(exc)
        report = _read(args, lambda conn: _read_account(conn, name, host, args.role))
        _emit_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n' if args.json else format_account(report),
                   args.output, quiet=args.quiet)
        if not args.quiet:
            for warning in report.get('warnings', []):
                sys.stderr.write('note: %s\n' % warning)
        if not report['exists']:
            raise SystemExit(1)
        return
    if sub not in _WRITE_OPERATIONS:
        _fail('Unknown accounts subcommand %r. Run: myflames accounts --help' % sub)
    role_mode = None
    if sub == 'role':
        if not rest or rest[0] == '--help':
            sys.stdout.write('usage: myflames accounts role grant|revoke|default ACCOUNT ...\n\n'
                             '  grant ACCOUNT ROLE [ROLE ...]    grant roles (--admin-option optional)\n'
                             '  revoke ACCOUNT ROLE [ROLE ...]   revoke roles\n'
                             '  default ACCOUNT [ROLE ... | --none | --all]   set default roles\n\n'
                             'Run myflames accounts role ACTION --help for flags.\n')
            return
        if rest[0] not in ('grant', 'revoke', 'default'):
            _fail('Use: myflames accounts role grant|revoke|default ACCOUNT ...')
        role_mode, rest = rest[0], rest[1:]
    parser = _parser('accounts ' + sub + (' ' + role_mode if role_mode else ''),
                     'Plan the change, show its statements, confirm, then apply it with a fresh re-read.')
    _account_args(parser)
    options = {}
    if sub in ('create', 'password'):
        source = parser.add_argument_group('Password input')
        source.add_argument('--password-stdin', action='store_true', help='Read the new password from the first line '
                                                                          'of stdin.')
        source.add_argument('--random-password', action='store_true', help='Generate the password (MySQL: the server, '
                                                                           'MariaDB: myflames) and print it once.')
    if sub == 'create':
        parser.add_argument('--locked', action='store_true', help='Leave the new account locked.')
    if sub == 'password':
        parser.add_argument('--current-password', action='store_true', help='Also give your current password (MySQL '
                                                                            'REPLACE), prompted or as the second stdin '
                                                                            'line (leave the first line empty with '
                                                                            '--random-password).')
    if sub == 'rename':
        parser.add_argument('new_account', metavar='NEW_ACCOUNT', help='The new user@host.')
    if sub in ('grant', 'revoke'):
        parser.add_argument('privileges', metavar='PRIVILEGES', help="Comma-separated privileges, e.g. 'SELECT,INSERT' "
                                                                    "or 'CREATE ROUTINE'.")
        parser.add_argument('--on', metavar='TARGET', help='*.*, db.*, db.table or "PROCEDURE db.name" (also '
                                                           'FUNCTION, LIBRARY, PACKAGE, PACKAGE BODY).')
        parser.add_argument('--columns', metavar='LIST', help='Column grant: comma-separated columns of --on db.table.')
        parser.add_argument('--pattern', action='store_true', help='Treat _ and %% in the database name as wildcards.')
        parser.add_argument('--proxy', metavar='ACCOUNT', help='PROXY grant on this account (instead of --on).')
        if sub == 'grant':
            parser.add_argument('--grant-option', action='store_true', help='Add WITH GRANT OPTION.')
    if role_mode:
        parser.add_argument('roles', metavar='ROLE', nargs='*', help="Roles as name@host (MySQL) or name (MariaDB).")
        if role_mode == 'grant':
            parser.add_argument('--admin-option', action='store_true', help='Add WITH ADMIN OPTION.')
        if role_mode == 'default':
            special = parser.add_mutually_exclusive_group()
            special.add_argument('--none', action='store_true', help='No default role.')
            special.add_argument('--all', action='store_true', help='Every granted role (MySQL).')
    _write_flags(parser)
    args = parser.parse_args(rest)
    try:
        if (getattr(args, 'random_password', False) and getattr(args, 'password_stdin', False)
                and not getattr(args, 'current_password', False)):
            raise ValueError('--random-password with --password-stdin requires --current-password.')
        name, host = accounts.parse_account(args.account)
        role = bool(args.role)
        if role and '@' not in args.account:
            host = '%'
        if sub == 'create':
            options['locked'] = args.locked
            options['random_password'] = args.random_password
        if sub == 'password':
            options['random_password'] = args.random_password
        if sub == 'rename':
            options['new_name'], options['new_host'] = accounts.parse_account(args.new_account)
        if sub in ('grant', 'revoke'):
            options['privileges'] = _split_list(args.privileges)
            options.update(_target(args.on, _split_list(args.columns) or None, args.pattern, args.proxy))
            if sub == 'grant' and args.grant_option:
                options['grant_option'] = True
        if role_mode:
            options['mode'] = role_mode
            options['roles'] = [dict(zip(('name', 'host'), accounts.parse_account(r))) for r in args.roles]
            if role_mode == 'grant' and args.admin_option:
                options['admin_option'] = True
            if role_mode == 'default' and (args.none or args.all):
                if args.roles:
                    raise ValueError('Give roles or --none/--all, not both.')
                options['special'] = 'NONE' if args.none else 'ALL'
            elif role_mode == 'default' and not args.roles:
                raise ValueError('Give the default roles, or --none or --all.')
            elif role_mode != 'default' and not args.roles:
                raise ValueError('Name at least one role.')
    except ValueError as exc:
        _fail(exc)
    operation = 'roles' if sub == 'role' else sub
    label = name if role and '@' not in args.account else name + '@' + host
    code, result = _write(args, operation, name, host, role, options, label)
    if result is not None:
        if code and all(s.get('status') == 'not_sent' for s in result.get('statements', [])):
            _fail(result.get('message') or 'Nothing was sent.')
        if args.json:
            text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
        else:
            text = format_change(result)
        _emit_text(text, args.output, quiet=args.quiet)
        if result.get('generated_password') and not args.quiet:
            sys.stderr.write('note: copy the generated password now; myflames does not store it.\n')
    if code:
        raise SystemExit(code)
