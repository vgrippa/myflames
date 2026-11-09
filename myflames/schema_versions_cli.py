"""Read-only schema capture, local version management and sync output.

Server access captures schemas or reads lint metadata through schema_lint.
Sync scripts are payloads for review; this module never executes them.
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys

from . import schema_versions as schemas
from . import workspace_cli as workspace
from .cli import _fail
from .connector import MySQLConnection, ConnectorError, terminal_text
from .perf_common import table_text
from .workspace_store import WorkspaceStore, PROFILE_FIELDS

_READ_ONLY = ('Reads information_schema and SHOW CREATE output; never changes a server. '
              'The sync script is for you to review and run; myflames never runs it.')
_EXIT_CODES = 'Exit codes: 0 success, 1 --fail-on tripped, 2 bad input or connection failure.'


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Keep usage failures as actionable one-line diagnostics too.
        _fail(message)


def _parser():
    parser = _Parser(prog='myflames schema', add_help=False,
                     description='Save and compare database schemas locally. ' + _READ_ONLY,
                     epilog=_READ_ONLY + '\n' + _EXIT_CODES,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    actions = parser.add_subparsers(dest='action', metavar='ACTION')
    descriptions = (
        ('read', 'Read and print a database schema without saving it.'),
        ('save', 'Read and save a database schema as a local version.'),
        ('list', 'List saved schema versions.'),
        ('show', 'Show a saved version or an object definition.'),
        ('compare', 'Compare saved, file or live schemas and print the sync script.'),
        ('lint', 'Check a saved, file or live schema for risks.'),
        ('edit', 'Change a saved version name or note.'),
        ('delete', 'Delete a saved version locally.'),
        ('export', 'Export a saved version as a schema snapshot file, or a schema as a SQL script that recreates it '
                   '(--sql).'),
        ('import', 'Import a schema snapshot file as a local version.'),
    )
    for action, description in descriptions:
        sub = actions.add_parser(action, add_help=False, help=description,
                                 description=description + ' ' + _READ_ONLY,
                                 epilog=_READ_ONLY + '\n' + _EXIT_CODES,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
        sub.add_argument('--help', action='help', help='Show this help message and exit.')
        if action in ('read', 'save', 'compare', 'lint', 'export'):
            workspace._add_connection_args(sub, client_paths=True)
        if action in ('show', 'edit', 'delete'):
            sub.add_argument('version', metavar='VERSION',
                             help='Choose an ID, ID prefix (6+ characters), exact name or latest.')
        if action == 'export':
            sub.add_argument('version', metavar='VERSION',
                             help='Choose an ID, ID prefix (6+ characters), exact name or latest; with --sql also live, '
                                  'profile:NAME[/DATABASE] or file:PATH (.json also works).')
            sub.add_argument('--sql', action='store_true',
                             help='Emit a SQL script that recreates the schema on another server; never run it.')
            sub.add_argument('--create-database', action='store_true',
                             help='Start the SQL script with CREATE DATABASE (the source\'s character set and '
                                  'collation); by default it only switches to the database.')
            sub.add_argument('--keep-definers', action='store_true',
                             help='Keep the DEFINER of views, routines, triggers and events in the SQL script; by '
                                  'default they are left out.')
            sub.add_argument('--target-database', metavar='NAME',
                             help='Write the SQL script for a database with this name instead of the source\'s name.')
        if action in ('save', 'edit', 'import'):
            sub.add_argument('--name', metavar='NAME', help='Set the saved version name.')
            sub.add_argument('--note', metavar='TEXT', help='Set the saved version note.')
        if action == 'list':
            sub.add_argument('--database', metavar='NAME', help='Keep versions of this exact database name.')
        if action == 'show':
            sub.add_argument('--object', metavar='NAME', help='Print definitions of objects with this exact name.')
        if action == 'lint':
            sub.add_argument('side', metavar='SIDE', help='Choose a saved version, latest, live, profile:NAME[/DATABASE], or file:PATH (.json also works).')
            sub.add_argument('--fail-on', choices=('watch',), help='Exit 1 when a Watch finding is reported; Not judged does not trip the gate.')
        if action == 'compare':
            sub.add_argument('source', metavar='SOURCE', help='Choose the schema to match: a saved version, latest, live, profile:NAME[/DATABASE], or file:PATH (.json also works).')
            sub.add_argument('target', metavar='TARGET', help='Choose the schema the sync script changes: a saved version, latest, live, profile:NAME[/DATABASE], or file:PATH (.json also works).')
            sub.add_argument('--ignore-definers', action='store_true', help='Ignore differences in object definers.')
            selection = sub.add_mutually_exclusive_group()
            selection.add_argument('--include-drops', action='store_true', help='Include objects only in the target; their DROP statements are omitted by default.')
            selection.add_argument('--only', action='append', metavar='KEY', help='Include only this comparison key (namespace:name); repeat to choose more objects.')
            sub.add_argument('--unchanged', action='store_true', help='Include identical objects in text output.')
            sub.add_argument('--fail-on', choices=('any', 'drops'), help='Exit 1 for any difference, or objects only in target or sync statements that drop data.')
            formats = sub.add_mutually_exclusive_group()
            formats.add_argument('--json', action='store_true', help='Emit the schema-compare-1.0 report as machine-readable JSON.')
            formats.add_argument('--markdown', action='store_true', help='Emit the comparison as Markdown.')
            formats.add_argument('--sql', action='store_true', help='Emit only the sync script for review; never run it.')
        elif action not in ('export', 'delete'):
            sub.add_argument('--json', action='store_true', help='Emit the result as machine-readable JSON.')
        if action not in ('edit', 'delete', 'import'):
            sub.add_argument('-o', '--output', metavar='PATH', help='Write the result to this file instead of stdout.')
        if action == 'delete':
            sub.add_argument('--yes', action='store_true', help='Delete without the [y/N] prompt (required without a terminal).')
        if action == 'import':
            sub.add_argument('path', metavar='PATH', help='Read a schema snapshot JSON file from this path.')
        sub.add_argument('-q', '--quiet', action='store_true', help='Suppress notes on stderr; keep the result payload.')
    return parser


class _Sources(object):
    """Resolve snapshots and retain every used secret for error redaction."""
    def __init__(self):
        self.store = WorkspaceStore()
        self.passwords = []

    def capture(self, settings, database, profile=None):
        password = settings.get('password')
        if password:
            self.passwords.append(password)
        if not database:
            raise ValueError('Choose a database with -D NAME.')
        options = workspace._connection_options(dict(settings, database=database))
        options['query_timeout'] = schemas.READ_SECONDS
        source = schemas.read_source(options)
        if profile:
            source.update(profile_id=profile['id'], profile_name=profile['name'])
        with MySQLConnection(**options) as conn:
            return schemas.capture(conn, database, source)

    def live(self, args):
        settings = workspace._resolve_connection(args)
        return self.capture(settings, settings.get('database'), workspace._saved_profile(args))

    def profile(self, ref):
        name, separator, database = ref.partition('/')
        profile = self.store.find_profile(name)
        settings = {f: profile[f] for f in PROFILE_FIELDS if f != 'name' and profile.get(f) not in (None, '')}
        settings['profile_id'] = profile['id']
        settings = self.store.connection_with_saved_password(settings)
        if not settings.get('password'):
            raise ValueError('The saved connection %s has no saved password; use it as the live side: '
                             '--profile %s -p' % (profile['name'], profile['name']))
        return self.capture(settings, database if separator else profile.get('database'), profile)

    def version(self, ref, source=None):
        item = self.store.schema_version_find(ref, source=source)
        full = self.store.schema_version_get(item['id'])
        if full is None:
            raise ValueError('That saved schema version no longer exists.')
        meta = {'kind': 'version'}
        meta.update((key, item[key]) for key in ('id', 'number', 'name', 'saved_at'))
        return full['snapshot'], meta

    def side(self, ref, args):
        if ref == 'live':
            return self.live(args), {'kind': 'live'}
        if ref.startswith('profile:'):
            return self.profile(ref[len('profile:'):]), {'kind': 'live'}
        if ref.startswith('file:') or ref.endswith('.json'):
            path = ref[len('file:'):] if ref.startswith('file:') else ref
            return _read_file(path), {'kind': 'file', 'file': os.path.basename(path)}
        return self.version(ref)


    def lint(self, ref, args, connection_requested=False):
        """Keep capture and checks on one opened connection; never prompt twice."""
        from . import schema_lint
        snapshot, settings, profile = None, None, None
        if ref == 'live':
            settings = workspace._resolve_connection(args)
            profile = workspace._saved_profile(args)
        elif ref.startswith('profile:'):
            name, separator, database = ref[len('profile:'):].partition('/')
            profile = self.store.find_profile(name)
            settings = {f: profile[f] for f in PROFILE_FIELDS if f != 'name' and profile.get(f) not in (None, '')}
            settings['profile_id'] = profile['id']
            settings = self.store.connection_with_saved_password(settings)
            if not settings.get('password'):
                raise ValueError('The saved connection has no saved password; use live with --profile NAME -p.')
            if separator:
                settings['database'] = database
        else:
            snapshot, _ = self.side(ref, args)
            if connection_requested:
                settings = workspace._resolve_connection(args)
        if settings is None:
            return schema_lint.lint(snapshot)
        options = workspace._connection_options(settings)
        options['query_timeout'] = schemas.READ_SECONDS
        if options.get('password'):
            self.passwords.append(options['password'])
        server = schemas.read_source(options)
        try:
            with MySQLConnection(**options) as conn:
                if snapshot is None:
                    database = settings.get('database')
                    if not database:
                        raise ValueError('Choose a database with -D NAME.')
                    source = dict(server)
                    if profile:
                        source.update(profile_id=profile['id'], profile_name=profile['name'])
                    snapshot = schemas.capture(conn, database, source)
                report = schema_lint.lint(snapshot, dict(server, connection=conn))
                for item in report['not_judged']:
                    for password in self.passwords:
                        item['reason'] = item['reason'].replace(password, '[redacted]')
                return report
        except ConnectorError as exc:
            if snapshot is None:
                raise
            message = str(exc)
            for password in self.passwords:
                message = message.replace(password, '[redacted]')
            return schema_lint.lint(snapshot, dict(server, connection=None, error=message))


def _lint_text(report):
    out = ['Schema checks: %s' % report['database'], '%d Watch findings.' % len(report['findings'])]
    for item in report['findings']:
        where = item['object'] + ('.' + item['column'] if item['column'] else '')
        out.append('Watch: %s: %s %s' % (where, item['text'], item['detail']))
    for item in report['not_judged']:
        where = ': ' + item['object'] if item['object'] else ''
        out.append('Not judged: %s%s: %s' % (item['rule'], where, item['reason']))
    return '\n'.join(out) + '\n'


def _connection_requested(args):
    # Connection options default to None. Inspect parsed fields, never argument
    # values that might be a filename or snapshot name starting with '-p'.
    fields = ('profile', 'host', 'port', 'user', 'password', 'ssl_mode', 'ssl_ca',
              'ssl_cert', 'ssl_key', 'mysql_binary')
    return any(getattr(args, name, None) is not None for name in fields)


def _export_options(args):
    """The first SQL-only export option given, or ''."""
    for flag, value in (('--create-database', args.create_database), ('--keep-definers', args.keep_definers),
                        ('--target-database', args.target_database is not None)):
        if value:
            return flag
    return ''


def _is_side(ref):
    """A side that is not a saved version (live, a saved connection, a file)."""
    return ref == 'live' or ref.startswith(('profile:', 'file:')) or ref.endswith('.json')


def _read_file(path):
    with open(path, 'rb') as handle:
        data = handle.read(schemas.MAX_SNAPSHOT_BYTES + 1)
    if len(data) > schemas.MAX_SNAPSHOT_BYTES:
        raise ValueError('Choose a schema version file of at most %d MiB.' % (schemas.MAX_SNAPSHOT_BYTES // (1024 * 1024)))
    return schemas.parse_file(data.decode('utf-8'))


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def _snapshot_notes(snapshot):
    notes = list(snapshot.get('notes') or [])
    for kind, cover in sorted((snapshot.get('coverage') or {}).items()):
        if cover.get('state') != 'complete':
            notes.append('%s may be incomplete: %s' % (kind, cover.get('reason')))
    return notes


def _saved_text(item, imported=False):
    server = item['server']
    counts = schemas.counts_text(item['counts'])
    text = '%s version %s of %s (%s:%s%s, %s %s): %s. ID %s.\n' % (
        'Imported as' if imported else 'Saved', item['number'], item['database'],
        server.get('host'), server.get('port'), schemas.ssh_text(server), server.get('server_product'),
        server.get('server_version'),
        counts, item['id'][:12])
    same = item.get('same_as')
    if same:
        text += 'No changes since version %s%s.\n' % (same['number'], ' ("%s")' % same['name'] if same.get('name') else '')
    return text


def _saved_time(value):
    # The store writes ISO 8601 UTC. Accept offsets too when formatting a read.
    date = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is not None:
        date = date.astimezone(datetime.timezone.utc)
    return date.strftime('%Y-%m-%d %H:%M UTC')


def _list_text(report):
    if not report['items']:
        return 'No saved schema versions. Save one with: myflames schema save --profile NAME -D DATABASE\n'
    rows = []
    for item in report['items']:
        server = item['server']
        count = sum(n for kind, n in item['counts'].items() if kind not in ('database', 'unreadable'))
        rows.append([str(item['number']), item['name'], item['database'],
                     '%s:%s%s' % (server.get('host'), server.get('port'), schemas.ssh_text(server)),
                     ' '.join(p for p in (server.get('server_product'), server.get('server_version')) if p),
                     _saved_time(item['saved_at']), str(count) + ('' if item['complete'] else '*'), item['id'][:12]])
    text = table_text(['Version', 'Name', 'Database', 'Server', 'Product', 'Saved', 'Objects', 'ID'], rows) + '\n'
    if any(not item['complete'] for item in report['items']):
        text += '* The account that read it may not see every object; see: myflames schema show ID\n'
    return text


def _compare(args, sources):
    if args.target == args.source == 'live':
        raise ValueError('At most one side may be live; use profile:NAME/DATABASE for another server.')
    if args.target == args.source == 'latest':
        raise ValueError('Both sides cannot be latest; choose a version, file or live side.')
    if args.target == 'latest':
        source, source_meta = sources.side(args.source, args)
        target, target_meta = sources.version('latest', schemas.source_key(source))
    else:
        target, target_meta = sources.side(args.target, args)
        if args.source == 'latest':
            source, source_meta = sources.version('latest', schemas.source_key(target))
        else:
            source, source_meta = sources.side(args.source, args)
    report = schemas.compare(source, target, source_meta, target_meta, ignore_definers=args.ignore_definers,
                             sync=False)
    from .schema_sync import build
    chosen = args.only if args.only is not None else [e['key'] for e in report['objects']] if args.include_drops else None
    report['sync'] = build(target, source, report, chosen_keys=chosen)
    if args.json:
        text = _json(report)
    elif args.markdown:
        text = schemas.format_markdown(report)
    elif args.sql:
        text = report['sync']['script']
    else:
        text = schemas.format_text(report, show_unchanged=args.unchanged)
    # The report's notes (coverage of both sides included) are in the payload
    # itself (text, Markdown and JSON); the script lists them as comments.
    return text, report, [] if not args.sql else list(report.get('notes') or [])


def _delete(args, sources, item):
    if not args.yes:
        if not sys.stdin.isatty():
            raise ValueError('Deleting without a terminal requires --yes.')
        sys.stderr.write(terminal_text('Delete version %s of %s (saved %s)? [y/N] ' % (
            item['number'], item['database'], _saved_time(item['saved_at']))))
        sys.stderr.flush()
        try:
            answer = input()
        except EOFError:
            answer = ''
        if answer.strip().lower() not in ('y', 'yes'):
            raise ValueError('Not deleted.')
    sources.store.schema_version_delete(item['id'])
    return 'Deleted version %s of %s.\n' % (item['number'], item['database'])


def cmd_schema(argv):
    """Dispatch schema operations with the shared stdout/stderr and 0/1/2 contract."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.action:
        parser.error('Choose an action; see: myflames schema --help')
    sources = None
    password = getattr(args, 'password', None)
    if password == '__PROMPT__':
        password = None
    notes, report = [], None
    try:
        sources = _Sources()
        store = sources.store
        if args.action in ('read', 'save'):
            snapshot = sources.live(args)
            notes = _snapshot_notes(snapshot)
            if args.action == 'read':
                text = _json(snapshot) if args.json else schemas.format_snapshot_text(snapshot)
            else:
                item = store.schema_version_save(snapshot, args.name or '', args.note or '')
                text = _json(item) if args.json else _saved_text(item)
        elif args.action == 'list':
            result = store.schema_version_list()
            if args.database is not None:
                result['items'] = [i for i in result['items'] if i['database'] == args.database]
            text = _json(result) if args.json else _list_text(result)
        elif args.action == 'compare':
            text, report, notes = _compare(args, sources)
        elif args.action == 'lint':
            report = sources.lint(args.side, args, _connection_requested(args))
            text = _json(report) if args.json else _lint_text(report)
        elif args.action == 'export' and args.sql:
            from . import schema_export
            snapshot, meta = sources.side(args.version, args)
            result = schema_export.build(snapshot, database=args.target_database,
                                         create_database=args.create_database, keep_definers=args.keep_definers,
                                         meta=meta)
            text = result['script']
            notes = [schema_export.format_summary(result) + ' myflames never runs it.']
        elif args.action == 'export' and (_export_options(args) or _connection_requested(args) or _is_side(args.version)):
            raise ValueError('%s needs --sql: a schema version file is exported from a saved version only.'
                             % (_export_options(args) or ('a live side' if _is_side(args.version) else
                                                           'a connection option')))
        elif args.action == 'import':
            snapshot = _read_file(args.path)
            item = store.schema_version_save(snapshot, args.name if args.name is not None else snapshot.get('name', ''),
                                             args.note if args.note is not None else snapshot.get('note', ''))
            text = _json(item) if args.json else _saved_text(item, imported=True)
        else:
            item = store.schema_version_find(args.version)
            if args.action in ('show', 'export'):
                full = store.schema_version_get(item['id'])
                if full is None:
                    raise ValueError('That saved schema version no longer exists.')
                if args.action == 'export':
                    text = schemas.export_text(full)
                elif args.object is not None:
                    objects = [o for o in full['snapshot']['objects'] if o['name'] == args.object]
                    if not objects:
                        raise ValueError('No object named %s in this version.' % args.object)
                    text = '\n\n'.join('-- %s %s\n%s' % (schemas.TYPE_LABELS[o['type']], o['name'],
                                                       o['ddl'] if o.get('ddl') is not None else o.get('reason', ''))
                                       for o in objects) + '\n'
                else:
                    text = _json(full['snapshot']) if args.json else schemas.format_snapshot_text(full['snapshot'], item)
            elif args.action == 'edit':
                if args.name is None and args.note is None:
                    raise ValueError('Choose at least one of --name or --note.')
                item = store.schema_version_update(item['id'], args.name, args.note)
                text = _json(item) if args.json else 'Version %s of %s %s.\n' % (
                    item['number'], item['database'], 'is now named %s' % item['name'] if args.name is not None else 'note updated')
            else:
                text = _delete(args, sources, item)
        workspace._emit_text(text, getattr(args, 'output', None), quiet=args.quiet)
        if not getattr(args, 'json', False):
            workspace._notes(notes, args.quiet)
    except (KeyboardInterrupt, EOFError) as exc:
        _fail('Cancelled.' if isinstance(exc, KeyboardInterrupt) else 'Password prompt cancelled.', password)
    except (ValueError, TypeError, OSError, ConnectorError, sqlite3.Error) as exc:
        message = str(exc)
        for secret in sources.passwords if sources else []:
            message = message.replace(secret, '[redacted]')
        _fail(message, password)
    if args.action == 'compare' and args.fail_on:
        tripped = not report['identical'] if args.fail_on == 'any' else (
            any(o['status'] == 'only_in_target' for o in report['objects']) or
            any('drops_data' in s['risks'] for s in report['sync']['statements']))
        if not args.quiet:
            finding = 'schema differences' if args.fail_on == 'any' else 'objects only in target or sync statements that drop data'
            sys.stderr.write('%s: %s%s (--fail-on %s)\n' % (
                'FAIL' if tripped else 'OK', '' if tripped else 'no ', finding, args.fail_on))
        if tripped:
            raise SystemExit(1)

    if args.action == 'lint' and args.fail_on and report['findings']:
        if not args.quiet:
            sys.stderr.write('Watch: schema findings (--fail-on watch)\n')
        raise SystemExit(1)
