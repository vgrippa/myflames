"""Export as SQL: a script that recreates a schema version on another server.

A schema version (``schema-snapshot-1.0``, schema_versions.py) holds every
definition of one database as its server printed it. :func:`build` turns
one into a script for a person to review, copy, download or open in the
Query editor, which runs it with its own confirmations; myflames never runs
it. The script only creates (it drops nothing), in an order where each
statement can run after the ones before it:

1. Save the session's sql_mode, time zone and character set settings, then
   ``SET NAMES utf8mb4`` when the script holds text outside ASCII (it is
   written in UTF-8), and ``SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'``:
   the definitions were printed with sql_mode '' (schema_versions
   SESSION_SETTINGS), so no flag the person's session may have (ANSI_QUOTES,
   NO_BACKSLASH_ESCAPES, strict zero-date checks, MariaDB's ORACLE) changes
   how they parse, and a storage engine the server lacks is an error rather
   than a silent substitute.
2. ``CREATE DATABASE`` with the source's options, when chosen, then ``USE``.
3. Sequences (MariaDB; a column default can call NEXT VALUE FOR one).
4. Tables in foreign key order: a parent before the tables that refer to it.
   Tables that refer to each other (a cycle) are created without those
   foreign keys, which are added afterwards with ``ALTER TABLE ... ADD``, as
   are foreign keys to tables not in the script or in another database.
5. Routines and packages (a view can call a function), then views in
   dependency order, triggers in each table's order, events. Routines,
   triggers and events are created under the sql_mode, character set and
   collation the server recorded (an event also under its time zone); views
   under their recorded character set and collation, as no sql_mode is kept
   for a view.
6. The saved session settings back, and READ ONLY = 1 for a MySQL database
   that is read only in the source.

Definers are left out unless chosen (the account that runs the script
becomes the definer). What cannot be recreated as captured is a ``--
WARNING:`` line: unreadable definitions with their reason, a sequence's
current value, system-versioned history, files and tablespaces that must
exist first.
"""
import re

from . import __version__, schema_ddl, schema_versions
from .perf_common import check_schema, now
from .schema_ddl import quote
from .schema_sync import _ORDER_CLAUSE, _delimiter, _literal, _one_line

#: Order of object kinds in the script after the tables.
_PROGRAMS = ('procedure', 'function', 'package', 'package_body')
_SESSION = ('character_set_client', 'collation_connection', 'sql_mode', 'time_zone')
#: The mode the tables and views run under (see the module docstring).
TABLE_SQL_MODE = 'NO_ENGINE_SUBSTITUTION'
#: Client character sets that can read the script's UTF-8 text as it is.
_UTF8 = ('utf8mb4', 'utf8mb3', 'utf8')
_READ_ONLY = re.compile(r'\s*/\* READ ONLY = (\d) \*/')
_REPLICA = re.compile(r' DISABLE ON (SLAVE|REPLICA)\b')
#: Tablespaces every InnoDB server has.
_BUILT_IN_SPACES = ('innodb_system', 'innodb_file_per_table', 'innodb_temporary')


def _ascii(text):
    return all(ord(c) < 128 for c in text or '')


def _when(stamp):
    return schema_versions.when_text(stamp)


def _plural(n, one, many=None):
    return '%d %s' % (n, one if n == 1 else (many or one + 's'))


class _Export(object):
    def __init__(self, snapshot, database, create_database, keep_definers):
        self.snapshot = snapshot
        self.source_db = snapshot['database']
        self.db = database
        self.engine = snapshot['server'].get('engine')
        self.create_database = create_database
        self.keep_definers = keep_definers
        self.items = []          # {phase, object, sql, compound, warnings, settings}
        self.not_exported = []
        self.warnings = []       # Script-wide WARNING lines (top of the script).
        self.notes = []          # Script-wide notes (end of the script).
        #: Table names as the server compares them -> the name in the snapshot.
        self.table_names = {self.table_name(o['name']): o['name'] for o in snapshot['objects'] if o['type'] == 'table'}
        #: Every object name of the source database, lowercased (requalify).
        self.object_names = {o['name'].lower() for o in snapshot['objects'] if o['type'] != 'database'}

    # -- helpers ----------------------------------------------------------------------------------

    def q(self, text):
        """Source text with the source's database name in backquoted
        qualifiers replaced by the target's (outside literals and comments)."""
        return schema_ddl.requalify(text, self.source_db, self.db, self.object_names)

    def definition(self, obj):
        text = obj['ddl']
        if not self.keep_definers:
            text = schema_ddl.with_definer_removed(text)
        return self.q(text)

    def add(self, phase, obj, sql, compound=False, warnings=(), settings=None):
        self.items.append({'phase': phase, 'object': {'type': obj['type'], 'name': obj['name']} if obj else None,
                           'sql': sql, 'compound': compound, 'warnings': list(warnings),
                           'settings': settings or {}})

    def table_name(self, name):
        return name.lower() if self.snapshot['server'].get('lower_case_table_names') in ('1', '2') else name

    def creation_settings(self, obj, sql_mode=None):
        """The session settings *obj* is created under: its own character set
        and collation (a view or program records them) and *sql_mode*."""
        settings = {}
        client, collation = obj.get('character_set_client'), obj.get('collation_connection')
        if client:
            # The text as the script writes it: another database name can
            # bring characters outside ASCII into an ASCII definition.
            settings['character_set_client'] = client if (_ascii(self.q(obj['ddl'])) or client.lower() in _UTF8) \
                else 'utf8mb4'
        if collation:
            settings['collation_connection'] = collation
        if sql_mode is not None:
            settings['sql_mode'] = sql_mode
        return settings

    # -- objects ---------------------------------------------------------------------------------

    def unreadable(self):
        for obj in self.snapshot['objects']:
            if obj['type'] != 'database' and obj.get('ddl') is None:
                self.not_exported.append({'type': obj['type'], 'name': obj['name'],
                                          'reason': obj.get('reason') or 'Its definition could not be read.'})

    def database(self):
        # An imported snapshot may lack its database object: no options then.
        obj = next((o for o in self.snapshot['objects'] if o['type'] == 'database'), {})
        ddl = obj.get('ddl') or ''
        read_only = _READ_ONLY.search(ddl)
        self.read_only = bool(read_only and read_only.group(1) == '1')
        span = schema_ddl.object_name_span(ddl, 'DATABASE')
        options = _READ_ONLY.sub('', ddl[span[1]:]).strip() if span else ''
        if not options and obj.get('character_set'):
            options = 'DEFAULT CHARACTER SET %s%s' % (obj['character_set'],
                                                      ' COLLATE %s' % obj['collation'] if obj.get('collation') else '')
        self.defaults = '%s / %s' % (obj.get('character_set') or 'unknown', obj.get('collation') or 'unknown')
        if self.create_database:
            self.add('database', obj, ('CREATE DATABASE %s %s' % (quote(self.db), options)).rstrip())
        self.add('use', None, 'USE %s' % quote(self.db))

    def sequences(self):
        for obj in self.sorted('sequence'):
            self.add('sequences', obj, self.definition(obj), warnings=[
                'WARNING: Sequence %s starts again at its START WITH value: a schema version keeps definitions, '
                'not the current value. Set it with SETVAL after the script if needed.' % obj['name']])

    def sorted(self, kind):
        return sorted((o for o in self.snapshot['objects'] if o['type'] == kind and o.get('ddl') is not None),
                      key=lambda o: o['name'])

    def tables(self):
        tables = self.sorted('table')
        exported = {self.table_name(o['name']): o for o in tables}
        unreadable = {self.table_name(o['name']) for o in self.snapshot['objects']
                      if o['type'] == 'table' and o.get('ddl') is None}
        parts, edges, foreign = {}, {}, {}
        for obj in tables:
            split = schema_ddl.split_table(obj['ddl'])
            parts[obj['name']] = split
            edges[obj['name']] = set()
            foreign[obj['name']] = []
            for element in (split['elements'] if split else []):
                if element['kind'] != 'foreign_key':
                    continue
                fk = schema_ddl.foreign_key(element['text'])
                if fk is None:
                    continue
                if fk['schema'] not in (None, self.source_db):
                    foreign[obj['name']].append((element, 'Foreign key %s on %s refers to %s.%s in another database: '
                                                 'that table must exist first, so the key is added at the end.'
                                                 % (element['name'], obj['name'], fk['schema'], fk['table'])))
                    continue
                parent = exported.get(self.table_name(fk['table']))
                if parent is None:
                    why = 'could not be read' if self.table_name(fk['table']) in unreadable else 'is not in the schema version'
                    foreign[obj['name']].append((element, 'Foreign key %s on %s refers to table %s, which %s: the key '
                                                 'is added at the end and fails unless that table exists.'
                                                 % (element['name'], obj['name'], fk['table'], why)))
                elif parent['name'] != obj['name']:
                    edges[obj['name']].add(parent['name'])
        order, cycles = _table_order(sorted(edges), edges)
        deferred = []
        by_name = {o['name']: o for o in tables}
        for name in order:
            obj = by_name[name]
            split = parts[name]
            later = [(element, None) for element in (split['elements'] if split else [])
                     if element['kind'] == 'foreign_key' and _fk_parent(element, self) in cycles.get(name, ())]
            later.extend(foreign[name])
            warnings = self.table_warnings(obj, split)
            if split is None:
                warnings.append('Its definition could not be split into lines, so its foreign keys are created with it; '
                                'create the tables it refers to first.')
            if later and split is not None:
                skip = {id(element) for element, _ in later}
                kept = [e['text'] for e in split['elements'] if id(e) not in skip]
                sql = 'CREATE TABLE %s (\n  %s\n) %s' % (quote(name), ',\n  '.join(kept), split['options'])
                if split['partitioning']:
                    sql += '\n' + split['partitioning']
                for element, why in later:
                    deferred.append((obj, element, why))
                names = ', '.join(e['name'] for e, _ in later)
                warnings.insert(0, 'Without %s %s; %s added after all tables are created.' % (
                    'its foreign key' if len(later) == 1 else 'its foreign keys', names,
                    'it is' if len(later) == 1 else 'they are'))
                self.add('tables', obj, self.q(sql), warnings=warnings)
            else:
                self.add('tables', obj, self.q(obj['ddl']), warnings=warnings)
        for obj, element, why in deferred:
            warnings = ['WARNING: %s' % why] if why else [
                '%s and %s refer to each other, so this foreign key is added once both exist.' % (
                    obj['name'], _fk_parent(element, self))]
            self.add('foreign_keys', obj, 'ALTER TABLE %s ADD %s' % (quote(obj['name']), self.q(element['text'])),
                     warnings=warnings)
        if tables:
            self.notes.append('Tables start their AUTO_INCREMENT counters again: a schema version does not keep them.')

    def table_warnings(self, obj, split):
        out = []
        options = schema_ddl.table_options(split['options']) if split else {}
        if 'WITH SYSTEM VERSIONING' in options or (split and any(e['kind'] == 'period' and
                                                                  'SYSTEM_TIME' in e['text'] for e in split['elements'])):
            out.append('WARNING: Table %s uses system versioning: it is created with versioning and without the '
                       'source\'s history rows, which are data and not part of a schema version.' % obj['name'])
        for name in ('DATA DIRECTORY', 'INDEX DIRECTORY'):
            if name in options:
                # MySQL 8.0.21+ refuses an InnoDB DATA DIRECTORY outside the
                # known directories (storage/innobase/handler/ha_innodb.cc:
                # 12519-12530, srv/srv0start.cc:1442-1451); InnoDB creates a
                # missing directory itself (fil/fil0fil.cc:5562).
                out.append('WARNING: Table %s keeps its files in %s: the server must be able to create files there%s'
                           % (obj['name'], options[name].split('=', 1)[-1],
                              ', and on MySQL 8.0.21 or later an InnoDB table\'s directory must be under the data '
                              'directory, innodb_data_home_dir or a directory in innodb_directories.'
                              if self.engine == 'mysql' else '.'))
        space = options.get('TABLESPACE')
        if space:
            found = re.search(r'TABLESPACE\s*=?\s*(`(?:[^`]|``)*`|\w+)', space)
            name = schema_ddl.read_identifier(found.group(1), 0)[0] if found else ''
            if name and name.lower() not in _BUILT_IN_SPACES:
                out.append('WARNING: Table %s is stored in tablespace %s, which must exist on the server first.'
                           % (obj['name'], name))
        encrypted = options.get('ENCRYPTION', '') + options.get('ENCRYPTED', '')
        if re.search(r"ENCRYPTION='[Yy]'|ENCRYPTED=YES", encrypted):
            out.append('WARNING: Table %s is encrypted: the server needs a keyring (MySQL) or an encryption plugin '
                       '(MariaDB) for the CREATE TABLE to succeed.' % obj['name'])
        return out

    def programs(self):
        programs = [o for kind in _PROGRAMS for o in self.sorted(kind)]
        programs.sort(key=lambda o: (_PROGRAMS.index(o['type']), o['name']))
        for obj in programs:
            self.add('routines', obj, self.definition(obj), compound=True,
                     settings=self.creation_settings(obj, obj.get('sql_mode')))

    def views(self):
        views = {o['name']: o for o in self.sorted('view')}
        missing = {o['name'] for o in self.snapshot['objects'] if o['type'] == 'view' and o.get('ddl') is None}
        refs = {name: _references(obj['ddl'], self.source_db, set(views) | missing, self.table_name) - {name}
                for name, obj in views.items()}
        done, order = set(), []

        def visit(name, path):
            if name in done or name in path:
                return
            for other in sorted(refs[name] & set(views)):
                visit(other, path | {name})
            done.add(name)
            order.append(name)
        for name in sorted(views):
            visit(name, frozenset())
        hidden = sorted(o['name'] for o in self.snapshot['objects'] if o['type'] == 'function' and o.get('ddl') is None)
        for name in order:
            obj = views[name]
            warnings = ['WARNING: View %s uses view %s, which is not in this script: create it first.' % (name, other)
                        for other in sorted(refs[name] & missing)]
            warnings.extend('WARNING: View %s calls function %s, which is not in this script: create it first.'
                            % (name, function) for function in hidden if _calls(obj['ddl'], self.source_db, function))
            self.add('views', obj, self.definition(obj), warnings=warnings,
                     settings=self.creation_settings(obj, TABLE_SQL_MODE))

    def triggers(self):
        tables = {self.table_name(o['name']) for o in self.snapshot['objects'] if o['type'] == 'table' and o.get('ddl')}
        groups = {}
        for obj in self.sorted('trigger'):
            key = (self.table_name(obj.get('table') or ''), obj.get('timing') or '', obj.get('event') or '')
            groups.setdefault(key, []).append(obj)
        for key in sorted(groups):
            members = sorted(groups[key], key=lambda o: (o.get('order') or 0, o['name']))
            unreadable = [o['name'] for o in self.snapshot['objects'] if o['type'] == 'trigger' and o.get('ddl') is None
                          and (self.table_name(o.get('table') or ''), o.get('timing') or '', o.get('event') or '') == key]
            for obj in members:
                text = self.definition(obj)
                # Created in the source's order, each runs after the ones
                # before it; an order clause could name one not created yet.
                head = text.find(' FOR EACH ROW')
                if head >= 0:
                    match = _ORDER_CLAUSE.match(text, head + len(' FOR EACH ROW'))
                    if match:
                        text = text[:match.start()] + text[match.end():]
                warnings = []
                if key[0] not in tables:
                    warnings.append('WARNING: Trigger %s is on table %s, which is not in this script: create the table '
                                    'first.' % (obj['name'], obj.get('table')))
                if unreadable and len(members) + len(unreadable) > 1:
                    warnings.append('WARNING: Trigger %s shares its table, timing and event with %s, which could not be '
                                    'read: their order on the server may differ.' % (obj['name'], ', '.join(unreadable)))
                self.add('triggers', obj, text, compound=True,
                         settings=self.creation_settings(obj, obj.get('sql_mode')), warnings=warnings)

    def events(self):
        for obj in self.sorted('event'):
            settings = self.creation_settings(obj, obj.get('sql_mode'))
            if obj.get('time_zone'):
                settings['time_zone'] = obj['time_zone']
            text = obj['ddl']
            warnings = []
            body = text.find(' DO ')
            header = text[:body] if body >= 0 else text
            # AT and ENDS in the past: not created (NOT PRESERVE) or created
            # disabled (mysql-server sql/event_parse_data.cc:100-118).
            if re.search(r"\bON SCHEDULE AT '| ENDS '", header):
                warnings.append('WARNING: Event %s has a fixed AT or ENDS time: if it has passed when the script '
                                'runs, the server does not create the event, or creates it disabled.' % obj['name'])
            replica = _REPLICA.search(header)
            if replica:
                # A replica marks replicated events this way; it can also be
                # set by hand (mysql-server sql/sql_yacc.yy:3682-3693).
                warnings.append('WARNING: Event %s is marked DISABLE ON %s (a replica marks replicated events this way; '
                                'it can also be set by hand) and is created that way; enable it with ALTER EVENT ... '
                                'ENABLE where it should run.%s' % (
                                    obj['name'], replica.group(1),
                                    ' DISABLE ON REPLICA needs MySQL 8.2 or later; older MySQL and MariaDB accept '
                                    'DISABLE ON SLAVE.' if replica.group(1) == 'REPLICA' else ''))
            zone = obj.get('time_zone') or 'SYSTEM'
            if zone != 'SYSTEM' and not re.match(r'^[+-]\d', zone):
                warnings.append('Event %s uses the time zone %s, which needs the time zone tables on the server.'
                                % (obj['name'], zone))
            self.add('events', obj, self.definition(obj), compound=True, settings=settings, warnings=warnings)

    # -- the script -------------------------------------------------------------------------------

    def run(self):
        self.unreadable()
        self.database()
        self.sequences()
        self.tables()
        self.programs()
        self.views()
        self.triggers()
        self.events()
        if self.keep_definers:
            if any(i['object'] and i['object']['type'] in schema_versions.DEFINER_TYPES for i in self.items):
                self.notes.append('Views, routines, triggers and events keep their DEFINER: the account that runs the '
                                  'script needs the privilege to name another definer, and each definer account should '
                                  'exist on the server. MySQL 8.2 and later refuse a missing definer without '
                                  'ALLOW_NONEXISTENT_DEFINER (or SUPER); elsewhere a trigger, an event or an SQL '
                                  'SECURITY DEFINER view or routine whose definer is missing fails when it runs.')
        elif any(schema_ddl.split_definer(o.get('ddl'))[0] for o in self.snapshot['objects']
                 if o['type'] in schema_versions.DEFINER_TYPES and o.get('ddl')):
            self.notes.append('Definers are left out: the account that runs the script becomes the DEFINER of every '
                              'view, routine, trigger and event.')
        if self.db != self.source_db:
            self.notes.append('Definitions use the database name %s where the source says %s.' % (self.db, self.source_db))
            bare = re.compile(r'(?<![`\w$.])%s\s*\.' % re.escape(self.source_db))
            if any(bare.search(_masked(i['sql'])) for i in self.items if i['compound']):
                self.notes.append('A routine, trigger or event body names %s without backquotes: those references '
                                  'are not changed.' % self.source_db)
        if not self.create_database:
            self.notes.append('The script does not create the database: run it where %s exists and is empty. The '
                              'source\'s default character set and collation are %s.' % (self.db, self.defaults))
        server = self.snapshot['server']
        product = ' '.join(p for p in (server.get('server_product') or server.get('engine'),
                                       server.get('server_version')) if p)
        self.notes.append('Read from %s: another product or an older release may not accept every definition, '
                          'character set or collation.' % product)
        views = any(o['type'] == 'view' for o in self.snapshot['objects'])
        for label, item in sorted((self.snapshot.get('coverage') or {}).items()):
            if item.get('state') != 'complete':
                self.warnings.append('The account that read the source may not see every %s: %s%s' % (
                    'table or view' if label == 'tables' else label.rstrip('s'), item.get('reason'),
                    # CREATE VIEW resolves the functions a view calls.
                    ' A view that calls a function it could not see fails to be created.'
                    if label == 'routines' and views else ''))
        for note in self.snapshot.get('notes') or []:
            self.warnings.append(note)
        for item in self.not_exported:
            self.warnings.append('%s %s is not in this script: %s' % (
                schema_versions.TYPE_LABELS[item['type']].capitalize(), item['name'], item['reason']))


def _masked(sql):
    from .schema_sync import _syntax
    return _syntax(sql, keep_identifiers=True)


def _references(ddl, database, names, fold=None):
    """Names of *names* referenced as `database`.`name` in *ddl*, outside
    literals and comments (SHOW CREATE VIEW qualifies every table and view
    a view reads with its database). *fold* compares names the way the
    server does (lower_case_table_names 1 and 2: in lower case)."""
    fold = fold or (lambda name: name)
    folded = {fold(n): n for n in names}
    text = _masked(ddl or '')
    out = set()
    marker = quote(database) + '.'
    at = text.find(marker)
    while at >= 0:
        ident = schema_ddl.read_identifier(text, at + len(marker))
        if ident and fold(ident[0]) in folded:
            out.add(folded[fold(ident[0])])
        at = text.find(marker, at + 1)
    return out


def _calls(ddl, database, function):
    """Whether a view's text calls stored function *function* of
    *database*: the servers print such a call backquoted, `f`( or `db`.`f`(."""
    text = _masked(ddl or '')
    # Stored function names compare without letter case on both servers.
    name = '(?i:%s)' % re.escape(quote(function))
    return bool(re.search(r'(?<![.\w`])%s\(|%s\.%s\(' % (name, re.escape(quote(database)), name), text))


def _fk_parent(element, export):
    """The snapshot's name of the table a foreign key line refers to in the
    same database (its own spelling when the snapshot lacks it), or None."""
    fk = schema_ddl.foreign_key(element['text'])
    if not fk or fk['schema'] not in (None, export.source_db):
        return None
    return export.table_names.get(export.table_name(fk['table']), fk['table'])


def _table_order(names, edges):
    """(names in foreign key order, {table: set of tables in its cycle}).

    Tarjan's strongly connected components over "refers to" edges; a
    component of two or more tables is a cycle, whose foreign keys among its
    members are added after all tables. The components are then ordered
    parents first, ties by name, so the order never depends on the input's.
    """
    index, low, stack, on_stack, components = {}, {}, [], set(), []
    counter = [0]

    def connect(start):
        # Iterative Tarjan: (node, iterator over its parents).
        work = [(start, iter(sorted(edges[start])))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, parents = work[-1]
            advanced = False
            for parent in parents:
                if parent not in edges:
                    continue
                if parent not in index:
                    index[parent] = low[parent] = counter[0]
                    counter[0] += 1
                    stack.append(parent)
                    on_stack.add(parent)
                    work.append((parent, iter(sorted(edges[parent]))))
                    advanced = True
                    break
                if parent in on_stack:
                    low[node] = min(low[node], index[parent])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.append(member)
                    if member == node:
                        break
                components.append(sorted(members))

    for name in names:
        if name not in index:
            connect(name)
    owner = {}
    for number, members in enumerate(components):
        for member in members:
            owner[member] = number
    cycles = {}
    for members in components:
        if len(members) > 1:
            for member in members:
                cycles[member] = set(members) - {member}
    # Parents first (Kahn), ties by the component's first name.
    needs = {n: set() for n in range(len(components))}
    for name in names:
        for parent in edges[name]:
            if parent in owner and owner[parent] != owner[name]:
                needs[owner[name]].add(owner[parent])
    order, placed = [], set()
    while len(placed) < len(components):
        ready = sorted((components[n][0], n) for n in needs if n not in placed and needs[n] <= placed)
        _, chosen = ready[0]
        placed.add(chosen)
        order.extend(components[chosen])
    return order, cycles


def _statements(export):
    order = ('database', 'use', 'sequences', 'tables', 'foreign_keys', 'routines', 'views', 'triggers', 'events')
    return sorted(export.items, key=lambda item: order.index(item['phase']))


def build(snapshot, database=None, create_database=False, keep_definers=False, meta=None, generated_at=None):
    """The SQL that recreates *snapshot* (a validated schema-snapshot-1.0) in
    the database *database* (default: the source's name). Returns
    ``{database, source_database, options, statements, not_exported,
    warnings, notes, summary, label, taken_at, filename, script}``."""
    target = check_schema(database) if database not in (None, '') else snapshot['database']
    if target is None:
        target = snapshot['database']
    if target.lower() in schema_versions.UNSUPPORTED_DATABASES:
        raise ValueError('Choose another database name than %s.' % target)
    if not isinstance(create_database, bool) or not isinstance(keep_definers, bool):
        raise ValueError('create_database and keep_definers must be true or false.')
    export = _Export(snapshot, target, create_database, keep_definers)
    export.run()
    statements = _statements(export)
    side = schema_versions.describe_side(snapshot, meta)
    stamp = generated_at or now()
    script, warnings = _script(export, statements, side, stamp)
    counts = {}
    for item in statements:
        if item['object'] and item['phase'] not in ('foreign_keys', 'database'):
            counts[item['object']['type']] = counts.get(item['object']['type'], 0) + 1
    clean = [{'phase': i['phase'], 'object': i['object'], 'sql': i['sql'], 'compound': i['compound'],
              'warnings': [w[len('WARNING: '):] if w.startswith('WARNING: ') else w for w in i['warnings']]}
             for i in statements]
    return {
        'database': target,
        'source_database': snapshot['database'],
        'options': {'database': target, 'create_database': create_database, 'keep_definers': keep_definers},
        'label': side['label'],
        'taken_at': snapshot['taken_at'],
        'generated_at': stamp,
        'statements': clean,
        'not_exported': export.not_exported,
        'warnings': warnings,
        'notes': export.notes,
        'summary': {'statements': sum(1 for s in clean if s['phase'] not in ('use',)),
                    'objects': counts, 'not_exported': len(export.not_exported), 'warnings': len(warnings)},
        'filename': filename(snapshot, meta),
        'script': script,
    }


def filename(snapshot, meta=None):
    stem = ''.join(c if c.isalnum() or c in '-_' else '_' for c in snapshot['database'])[:64]
    number = (meta or {}).get('number')
    return 'schema-%s%s.sql' % (stem, '-v%d' % number if isinstance(number, int) else '')


def _script(export, statements, side, stamp):
    """The script text and every WARNING sentence it holds, in order."""
    lines = ['-- myflames schema export',
             '-- Recreates %s as read from %s, read %s.' % (export.source_db, side['label'], _when(side['taken_at'])),
             '-- Generated by myflames %s at %s. Review it before you run it; myflames never runs this script.'
             % (__version__, _when(stamp)),
             '-- It creates %s and drops nothing.' % (
                 'the database %s and its objects' % export.db if export.create_database
                 else 'the objects in the database %s' % export.db)]
    lines = [_one_line(line) for line in lines]
    warnings = list(export.warnings)
    for item in statements:
        warnings.extend(w[len('WARNING: '):] for w in item['warnings'] if w.startswith('WARNING: '))
    lines.append('-- %s.' % _plural(len(warnings), 'warning'))
    if export.warnings:
        lines.append('')
        lines.extend('-- WARNING: %s' % _one_line(w) for w in export.warnings)
    # The script is UTF-8: definitions with text outside ASCII need a client
    # character set that reads it (SET NAMES utf8mb4 in the preamble).
    names = any(not _ascii(item['sql']) for item in statements)
    state = {'character_set_client': 'utf8mb4'} if names else {}
    saved = ['character_set_client', 'character_set_results', 'collation_connection'] if names else []
    body = []

    def set_session(values):
        changes = [(k, values[k]) for k in _SESSION if k in values and state.get(k) != values[k]]
        # A setting an earlier object changed and this one does not record
        # goes back to the session's own value, never carries over.
        restores = [k for k in _SESSION if k not in values and state.get(k) is not None]
        if not changes and not restores:
            return
        for key, _ in changes:
            if key not in saved:
                saved.append(key)
        state.update(changes)
        for key in restores:
            state[key] = None
        body.append('SET SESSION %s;' % ', '.join(['%s = %s' % (k, _literal(v)) for k, v in changes]
                                                   + ['%s = @myflames_%s' % (k, k) for k in restores]))

    previous = None
    for item in statements:
        obj = item['object']
        if item['phase'] in ('database', 'use'):
            body.append('')
        elif obj and (obj != previous or item['warnings']):
            label = schema_versions.TYPE_LABELS[obj['type']]
            body.extend(['', '-- %s %s' % (label, _one_line(obj['name']))])
        previous = obj
        for warning in item['warnings']:
            body.append('-- %s' % _one_line(warning) if warning.startswith('WARNING: ') else '--   %s' % _one_line(warning))
        if item['phase'] in ('sequences', 'tables', 'foreign_keys'):
            values = {'sql_mode': TABLE_SQL_MODE}
            if names:
                values['character_set_client'] = 'utf8mb4'
            set_session(values)
        elif item['settings']:
            values = dict(item['settings'])
            values.setdefault('sql_mode', TABLE_SQL_MODE)
            if names:
                values.setdefault('character_set_client', 'utf8mb4')
            set_session(values)
        if item['compound']:
            # The delimiter on a line of its own: a body may end in a -- comment.
            mark = _delimiter(item['sql'])
            body.extend(['DELIMITER %s' % mark, item['sql'], mark, 'DELIMITER ;'])
        else:
            body.append(item['sql'] + ';')
    if saved:
        lines.extend(['', 'SET %s;' % ', '.join('@myflames_%s = @@SESSION.%s' % (k, k) for k in saved)])
    if names:
        lines.append('SET NAMES utf8mb4;')
    lines.extend(body)
    if export.read_only and export.engine == 'mysql':
        lines.extend(['', '-- The source database is read only: it is made read only again at the end.',
                      'ALTER DATABASE %s READ ONLY = 1;' % quote(export.db)])
    if saved:
        lines.extend(['', 'SET SESSION %s;' % ', '.join('%s = @myflames_%s' % (k, k) for k in saved)])
    if export.notes:
        lines.extend(['', '-- Notes:'])
        lines.extend('--   %s' % _one_line(n) for n in export.notes)
    return '\n'.join(lines) + '\n', warnings


def format_summary(export):
    """One line for a terminal: what the script creates and its warnings."""
    counts = export['summary']['objects']
    what = ', '.join(_plural(counts[t], schema_versions.TYPE_LABELS[t], schema_versions.TYPE_PLURALS[t])
                     for t in schema_versions.TYPES if counts.get(t) and t != 'database') or 'no objects'
    parts = ['%s for %s: %s' % (_plural(export['summary']['statements'], 'statement'), export['database'], what)]
    if export['summary']['not_exported']:
        parts.append('%d not exported' % export['summary']['not_exported'])
    if export['summary']['warnings']:
        parts.append(_plural(export['summary']['warnings'], 'warning'))
    return '; '.join(parts) + '.'
