"""SQL that would change Target into Source: ``sync`` of schema-compare-1.0.

Written for a person to review, copy, or open in the Query editor, which runs
one statement at a time with its own confirmation; myflames never runs it.
Statements are ordered so each stays valid after the ones before it ran:

1. ``USE`` the Target database; database options (and READ ONLY = 0 when
   Target is a read-only MySQL database: no DDL runs in one).
2. Drop the triggers, events, routines and views that are removed or
   change (routines and triggers have no OR REPLACE on MySQL).
3. Drop foreign keys that are removed or change, and those on or to a
   column whose type changes or a key that is dropped or changes; then the
   checks that are removed or change (MySQL check names are unique per
   database, so one may move to another table); then the removed tables and
   sequences (their foreign keys would block changes to their parents, and
   their constraint names may be reused).
4. New sequences (a column default can call NEXT VALUE FOR them).
5. One ALTER TABLE per changed table: dropped keys and columns, then added
   and changed columns in Source's order (AFTER or FIRST only when a column
   has to move), then keys and checks, then only the table options that
   differ (restating ENGINE would rebuild the table), then partitioning at
   its end (a column the old partitioning uses can only be dropped by the
   statement that repartitions). On MySQL a second new FULLTEXT key is its
   own ALTER TABLE (InnoDB adds one per statement).
6. New tables (without their foreign keys), then every foreign key to add.
7. Routines and packages, then views in dependency order (a view that calls
   a function needs it), triggers, events, each under the sql_mode (and time
   zone) it was created with. Triggers of one table, timing and event are
   recreated in Source's order when an added or changed one must run before
   one that stays. Then READ ONLY = 1 again when Source is read-only.

A column whose text is the same on both sides but whose character set or
collation differs (it follows a table default that changed) gets an
explicit CHARACTER SET and COLLATE in its MODIFY. Anything that cannot be
generated safely (sequences that change, system versioning, generated
columns that switch between STORED and VIRTUAL, letter-case renames,
definitions that could not be split) is listed in ``skipped`` with
its reason and written as a comment in the script.
"""
import re

from . import __version__, alter_algorithm, schema_ddl
from .schema_ddl import quote

#: Risk codes on a statement.
RISKS = ('drops_data', 'may_change_data', 'checks_rows', 'drops_privileges')
#: How to turn off a table option Source no longer has.
_RESET_OPTION = {'COMMENT': "COMMENT=''", 'ROW_FORMAT': 'ROW_FORMAT=DEFAULT', 'KEY_BLOCK_SIZE': 'KEY_BLOCK_SIZE=0',
                 'STATS_PERSISTENT': 'STATS_PERSISTENT=DEFAULT', 'STATS_AUTO_RECALC': 'STATS_AUTO_RECALC=DEFAULT',
                 'STATS_SAMPLE_PAGES': 'STATS_SAMPLE_PAGES=DEFAULT', 'PACK_KEYS': 'PACK_KEYS=DEFAULT',
                 'CHECKSUM': 'CHECKSUM=0', 'DELAY_KEY_WRITE': 'DELAY_KEY_WRITE=0', 'MIN_ROWS': 'MIN_ROWS=0',
                 'MAX_ROWS': 'MAX_ROWS=0', 'AVG_ROW_LENGTH': 'AVG_ROW_LENGTH=0'}
_PHASES = ('session', 'database', 'drop_objects', 'drop_foreign_keys', 'drop_checks', 'drop_tables',
           'create_sequences', 'alter_tables', 'create_tables', 'add_foreign_keys', 'routines', 'views', 'triggers',
           'events', 'database_end')
#: MySQL prints a read-only database as a comment (sql/sql_show.cc:1417-1421).
_READ_ONLY = re.compile(r'\s*/\* READ ONLY = (\d) \*/')
#: A generated column's storage: VIRTUAL, or STORED (PERSISTENT on MariaDB).
_GENERATED = re.compile(r' GENERATED ALWAYS AS \(.*\) (VIRTUAL|STORED|PERSISTENT)\b')
_DELIMITERS = ('$$', ';;', '//', '$$$$')
_ORDER_CLAUSE = re.compile(r' (?:FOLLOWS|PRECEDES) (?:`(?:[^`]|``)*`|"(?:[^"]|"")*"|[0-9A-Za-z_$]+)')
_ROUTINES = ('procedure', 'function', 'package', 'package_body')
_GRANTS_WARNING = ('Dropping a routine also revokes the privileges granted on it (automatic_sp_privileges); grant '
                   'them again after it is created.')
_DROP = {'procedure': 'PROCEDURE', 'function': 'FUNCTION', 'package': 'PACKAGE', 'package_body': 'PACKAGE BODY',
         'trigger': 'TRIGGER', 'event': 'EVENT', 'view': 'VIEW', 'sequence': 'SEQUENCE', 'table': 'TABLE'}


def _literal(text):
    return "'" + (text or '').replace('\\', '\\\\').replace("'", "''") + "'"


def _storage(line):
    """VIRTUAL or STORED for a generated column line, else ''."""
    match = _GENERATED.search(line or '')
    return '' if not match else 'VIRTUAL' if match.group(1) == 'VIRTUAL' else 'STORED'


_SIGN = re.compile(r'(?: (?:unsigned|signed|zerofill))+', re.I)


def _type_text(line, preserve_values=False):
    """A column's type with its signedness (int and int unsigned differ for
    a foreign key and for the values they hold)."""
    end = schema_ddl.column_type_end(line)
    ident = schema_ddl.read_identifier(line, 0)
    if not end or not ident:
        return line
    sign = _SIGN.match(line, end)
    text = line[ident[1]:sign.end() if sign else end].strip()
    if preserve_values:
        # Type words ignore case; ENUM/SET member values do not.
        return re.sub(r"'(?:[^'\\]|\\.|'')*'|[^']+",
                      lambda m: m.group(0) if m.group(0).startswith("'") else m.group(0).lower(), text)
    return text.lower()


def empty_risks():
    """Additive risk shape; null means no known table rebuild or copy."""
    return {'destructive': [], 'data_dependent': [], 'rebuild': None}


def _merge_risks(*items):
    result = empty_risks()
    for item in items:
        for kind in ('destructive', 'data_dependent'):
            for text in item.get(kind, []):
                if text not in result[kind]:
                    result[kind].append(text)
    return result


def _syntax(text, keep_identifiers=False):
    """Mask quoted values, names and comments before looking for SQL words.

    A COMMENT 'NOT NULL' or a column named `unsigned` is not an attribute.
    Keep positions so type/attribute slices continue to refer to the DDL.
    """
    out, i = [], 0
    while i < len(text):
        start = i
        if text[i] in "'\"`":
            mark = text[i]
            i += 1
            while i < len(text):
                if text[i] == '\\':
                    i += 2
                elif text[i] == mark:
                    i += 1
                    if i < len(text) and text[i] == mark:
                        i += 1
                    else:
                        break
                else:
                    i += 1
            out.append(text[start:i] if keep_identifiers and mark == '`' else ' ' * (min(i, len(text)) - start))
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = len(text) if end < 0 else end + 2
            out.append(' ' * (i - start))
        elif text[i] == '#' or (text.startswith('--', i) and (i + 2 == len(text) or text[i + 2].isspace())):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
            out.append(' ' * (i - start))
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def _type_may_lose(old, new):
    """Conservative value-domain comparison, not an ALTER algorithm guess.

    Integer display widths do not limit values. Prove common widenings;
    other type conversions can depend on the stored rows and SQL mode.
    """
    a, b = _type_text(old, True), _type_text(new, True)
    if a == b:
        return False
    def shape(text):
        match = re.match(r'([a-z]+)(?:\(([^)]*)\))?', text)
        return (match.group(1), match.group(2)) if match else ('', None)
    at, ap = shape(a)
    bt, bp = shape(b)
    ints = {'tinyint': 8, 'smallint': 16, 'mediumint': 24, 'int': 32, 'integer': 32, 'bigint': 64}
    if at in ints and bt in ints:
        def bounds(word, bits):
            unsigned = 'unsigned' in word or 'zerofill' in word
            return (0, 2 ** bits - 1) if unsigned else (-2 ** (bits - 1), 2 ** (bits - 1) - 1)
        lo, hi = bounds(a, ints[at])
        nlo, nhi = bounds(b, ints[bt])
        return nlo > lo or nhi < hi
    if at == bt and at in ('varchar', 'char', 'binary', 'varbinary', 'bit', 'datetime', 'timestamp', 'time'):
        try:
            return int(bp or 0) < int(ap or 0)
        except ValueError:
            return True
    if at in ('decimal', 'numeric') and bt in ('decimal', 'numeric'):
        def digits(arg):
            parts = [int(n.strip()) for n in (arg or '10,0').split(',')]
            return (parts[0], parts[1] if len(parts) > 1 else 0)
        try:
            p, d = digits(ap)
            np, nd = digits(bp)
            return np - nd < p - d or nd < d or ('unsigned' in b and 'unsigned' not in a)
        except ValueError:
            return True
    widths = {'tinytext': 1, 'text': 2, 'mediumtext': 3, 'longtext': 4,
              'tinyblob': 1, 'blob': 2, 'mediumblob': 3, 'longblob': 4}
    if at in widths and bt in widths and at.endswith('text') == bt.endswith('text'):
        return widths[bt] < widths[at]
    if at == 'float' and bt in ('double', 'real'):
        return any(word in b for word in ('unsigned', 'zerofill')) and not any(word in a for word in ('unsigned', 'zerofill'))
    if at == bt and at in ('enum', 'set'):
        # Appending members preserves every existing member and its ordinal.
        return not (ap is not None and bp is not None and bp.startswith(ap + ','))
    return True


def change_risks(change):
    """Risk sentences about the change from target to source, from DDL only."""
    result = empty_risks()
    part, action, name = change['part'], change['change'], change.get('name') or ''
    source, target = change.get('source') or '', change.get('target') or ''
    if part == 'column':
        if action == 'only_in_target':
            result['destructive'].append('Drops column %s; its data is lost.' % name)
        elif action == 'different':
            a, b = _syntax(target).upper(), _syntax(source).upper()
            if re.search(r'\bNOT\s+NULL\b', b) and not re.search(r'\bNOT\s+NULL\b', a):
                result['data_dependent'].append('Makes column %s NOT NULL; existing NULL values may prevent the change or be converted.' % name)
            if _type_may_lose(target, source):
                result['data_dependent'].append('Changes column %s from %s to %s; existing values may not fit or may change.' % (
                    name, _type_text(target, True), _type_text(source, True)))
            if change.get('collation'):
                result['data_dependent'].append('Changes the character set or collation of column %s; existing values or uniqueness checks may change.' % name)
    elif action in ('only_in_source', 'different'):
        if part == 'index' and source.startswith(('PRIMARY KEY', 'UNIQUE ')):
            kind = 'PRIMARY KEY' if source.startswith('PRIMARY KEY') else 'UNIQUE'
            columns = ', '.join(schema_ddl.index_columns(source)) or name
            result['data_dependent'].append('Adds %s (%s); fails if existing rows have duplicate values%s.' % (
                kind, columns, ' or NULL values' if kind == 'PRIMARY KEY' else ' for a fully non-NULL key'))
        elif part == 'foreign_key':
            result['data_dependent'].append('Adds FOREIGN KEY %s; existing non-NULL child keys must match parent rows when the constraint is checked.' % name)
        elif part in ('check', 'constraint') and re.search(r'\bCHECK\s*\(', _syntax(source), re.I):
            result['data_dependent'].append('Adds CHECK %s; existing rows must satisfy it when the constraint is enforced.' % name)
    return result


def _drop_risks(kind, name):
    risks = empty_risks()
    tail = '; its data is lost' if kind == 'table' else '; its current value is lost' if kind == 'sequence' else ''
    risks['destructive'].append('Drops %s %s%s.' % (kind.replace('_', ' '), name, tail))
    return risks


def annotate_risks(report, engine):
    """Attach risk text once; tables, details and SQL comments reuse it."""
    for entry in report['objects']:
        risks = empty_risks()
        for change in entry.get('changes', []):
            change['risks'] = change_risks(change)
            if change['part'] == 'type':
                change['risks'] = _merge_risks(change['risks'], _drop_risks(change['target'], entry.get('target_name', entry['name'])))
            risks = _merge_risks(risks, change['risks'])
        if entry['status'] == 'only_in_target':
            risks = _merge_risks(risks, _drop_risks(entry['type'], entry['name']))
        elif entry['status'] == 'different':
            old_type = next((c['target'] for c in entry['changes'] if c['part'] == 'type'), entry['type'])
            if old_type != entry['type'] or entry['type'] in ('trigger', 'event') or (
                    entry['type'] in _ROUTINES and engine == 'mysql'):
                risks = _merge_risks(risks, _drop_risks(old_type, entry.get('target_name', entry['name'])))
        entry['risks'] = risks


def selected_keys(report, chosen_keys=None):
    """None is safe defaults; [] deliberately generates no statements."""
    keys = [entry['key'] for entry in report['objects']]
    if chosen_keys is None:
        return [e['key'] for e in report['objects'] if e['status'] != 'only_in_target']
    if not isinstance(chosen_keys, list) or len(chosen_keys) > len(keys) or any(not isinstance(k, str) for k in chosen_keys):
        raise ValueError('chosen_keys must be a list of comparison keys.')
    if len(set(chosen_keys)) != len(chosen_keys):
        raise ValueError('Choose each comparison key only once.')
    unknown = set(chosen_keys) - set(keys)
    if unknown:
        raise ValueError('Unknown comparison key: %s. Compare again and choose a listed key.' % sorted(unknown)[0])
    chosen = set(chosen_keys)
    return [key for key in keys if key in chosen]


def _table_elements(obj):
    parts = schema_ddl.split_table(obj.get('ddl')) if obj and obj['type'] == 'table' else None
    return parts['elements'] if parts else []


def _fk_column(obj, name):
    """Comparable FK type and effective charset/collation, from the snapshot.

    Display widths do not change integer types, and string lengths need not
    match. Other differences are conservatively left for a full selection.
    """
    column = next((e for e in _table_elements(obj)
                   if e['kind'] == 'column' and e['name'].lower() == name.lower()), None)
    if column is None:
        return None
    text = _type_text(column['text'])
    text = re.sub(r'^(tinyint|smallint|mediumint|int|integer|bigint)\(\d+\)', r'\1', text)
    text = re.sub(r'^(var)?(char|binary)\(\d+\)', r'\1\2', text)
    text = re.sub(r'^integer\b', 'int', text)
    pair = next((p for n, p in (obj.get('columns') or {}).items() if n.lower() == name.lower()), None)
    return text, tuple(pair or ())


def _table_name(snapshot, name):
    """Resolve table names using the recorded server, never this computer.

    Missing metadata is treated as case-sensitive. Folding then could pick a
    different existing table on a case-sensitive MySQL server.
    """
    return name.lower() if snapshot['server'].get('lower_case_table_names') in ('1', '2') else name


def _table_engine(obj):
    parts = schema_ddl.split_table(obj.get('ddl'))
    token = schema_ddl.table_options(parts['options']).get('ENGINE', '') if parts else ''
    return token.partition('=')[2].strip().strip('`\'"').lower()


def _standard_fk_key(snapshot):
    """Snapshots do not record restrict_fk_on_non_standard_key; use its default.

    MySQL 8.4+ requires a complete unique referenced key. Older InnoDB and
    MariaDB allow a leftmost index prefix. Unknown MySQL versions use the
    stricter requirement rather than asserting an unverified exception.
    """
    version = re.match(r'(\d+)\.(\d+)', snapshot['server'].get('server_version') or '')
    return snapshot['server']['engine'] == 'mysql' and (
        not version or tuple(int(n) for n in version.groups()) >= (8, 4))


def _has_fk_index(obj, columns, standard=False):
    """An ordinary index must start with the full FK columns in order.

    A standard referenced key must also be unique and end at those columns.
    Do not use index_columns here: it discards expressions and prefix lengths,
    which can make an unusable index look like a matching leftmost key.
    """
    for element in _table_elements(obj):
        if element['kind'] != 'index':
            continue
        text = element['text']
        masked = _syntax(text).upper()
        if standard and not masked.startswith(('PRIMARY KEY', 'UNIQUE ')):
            continue
        if masked.startswith(('FULLTEXT ', 'SPATIAL ', 'VECTOR ')) or 'USING HASH' in masked:
            continue
        pos = masked.find('(') + 1
        if not pos:
            continue
        end = None
        for index, column in enumerate(columns):
            pos += len(text[pos:]) - len(text[pos:].lstrip())
            ident = schema_ddl.read_identifier(text, pos)
            if not ident or ident[0].lower() != column.lower():
                break
            pos = ident[1]
            suffix = re.match(r'\s*(?:ASC\s*)?([,)])', text[pos:], re.I)
            if not suffix:
                break
            pos += suffix.end()
            end = suffix.group(1)
            if index + 1 < len(columns) and suffix.group(1) != ',':
                break
        else:
            if not standard or end == ')':
                return True
    return False


def _source_references(obj, database):
    """Known local object references, without interpreting literals/comments.

    SHOW CREATE qualifies view references. Also recognize bare relation,
    sequence and CALL operands in program bodies. Table FKs are checked with
    their columns separately; here only defaults/generated expressions count.
    This is a conservative dependency scan, not execution of stored SQL.
    """
    text = obj.get('ddl') or ''
    if obj['type'] == 'table':
        text = '\n'.join(e['text'] for e in _table_elements(obj) if e['kind'] == 'column')
    text = _syntax(text, keep_identifiers=True)
    ident = r'(?:`(?:[^`]|``)*`|[\w$]+)'
    refs = set()
    for match in re.finditer(r'(%s)\s*\.\s*(%s)' % (ident, ident), text):
        schema = schema_ddl.read_identifier(match.group(1), 0)[0]
        if schema == database:
            refs.add(schema_ddl.read_identifier(match.group(2), 0)[0])
    for match in re.finditer(r'\b(?:FROM|JOIN|UPDATE|INTO|CALL|NEXT\s+VALUE\s+FOR)\s+(%s)(?:\s*\.\s*(%s))?' % (ident, ident), text, re.I):
        first = schema_ddl.read_identifier(match.group(1), 0)[0]
        if match.group(2):
            if first == database:
                refs.add(schema_ddl.read_identifier(match.group(2), 0)[0])
        else:
            refs.add(first)
    if obj['type'] == 'trigger' and obj.get('table'):
        refs.add(obj['table'])
    return refs


class _Selection(object):
    """Resolve selection dependencies before emitting any statements.

    A blocked table keeps its entire Target definition. Repeating the checks
    against that effective target closes dependencies, including constraint
    names that a skipped DROP or ALTER no longer frees. Keeping each table's
    changes together avoids a half-created table or a dropped FK without its
    replacement. Unrelated tables can still be synchronized.
    """
    def __init__(self, target, source, report, chosen, retained):
        self.target, self.source = target, source
        self.entries = {e['key']: e for e in report['objects']}
        self.active = set(chosen) - set(retained)
        self.active.difference_update(e['key'] for e in report['objects'] if e['status'] == 'not_compared')
        self.blocked, self.reasons = {}, {}
        self.old, self.new = {}, {}
        self.old_keys, self.new_keys = {}, {}
        for entry in report['objects']:
            entry['blocked_by'] = []
            kind, name = entry['type'], entry['name']
            old_kind = next((c['target'] for c in entry.get('changes', []) if c['part'] == 'type'), kind)
            self.old_keys[(old_kind, entry.get('target_name', name))] = entry['key']
            self.new_keys[(kind, name)] = entry['key']
        for snap, objects, keys in ((target, self.old, self.old_keys), (source, self.new, self.new_keys)):
            for obj in snap['objects']:
                key = keys.get((obj['type'], obj['name']))
                if key is not None:
                    objects[key] = obj
        self.old_tables = {_table_name(target, o['name']): k for k, o in self.old.items() if o['type'] == 'table'}
        self.new_tables = {_table_name(source, o['name']): k for k, o in self.new.items() if o['type'] == 'table'}
        by_name = {}
        for key, obj in self.new.items():
            by_name.setdefault(obj['name'], set()).add(key)
        self.references = {key: set(k for name in _source_references(obj, source['database'])
                                    for k in by_name.get(name, ()) if k != key)
                           for key, obj in self.new.items()}

    def block(self, key, needs, reason):
        blockers = set()
        for need in needs:
            blockers.update(self.blocked.get(need) or [need])
        self.blocked.setdefault(key, set()).update(blockers)
        reasons = self.reasons.setdefault(key, [])
        if reason not in reasons:
            reasons.append(reason)

    def choose(self, key):
        if key in self.blocked:
            return ' '.join(self.choose(need) for need in sorted(self.blocked[key]) if need != key)
        entry = self.entries[key]
        action = 'drop' if entry['status'] == 'only_in_target' else 'change'
        return 'Choose %s to include its %s.' % (entry['name'], action)

    def kept_children(self, key, obj, kept):
        """Incoming Target FKs still constrain a chosen parent ALTER/DROP."""
        entry = self.entries[key]
        removing = key not in self.new or self.new[key]['type'] != 'table'
        for child_key in sorted(kept):
            child = self.old[child_key]
            for element in _table_elements(child):
                if element['kind'] != 'foreign_key':
                    continue
                fk = schema_ddl.foreign_key(element['text'])
                if not fk or fk['schema'] not in (None, self.target['database']) or (
                        _table_name(self.target, fk['table']) != _table_name(self.target, obj['name'])):
                    continue
                for change in ([None] if removing else entry.get('changes', [])):
                    columns = {n.lower() for n in fk['ref_columns']}
                    if change is None:
                        detail = 'Dropping %s' % obj['name']
                    elif change['part'] == 'column' and change['name'].lower() in columns and (
                            change['change'] == 'only_in_target' or change.get('collation') or
                            (change['change'] == 'different' and _type_text(change['target']) != _type_text(change['source']))):
                        detail = ('Dropping %s.%s' if change['change'] == 'only_in_target' else 'Changing %s.%s') % (obj['name'], change['name'])
                        if change.get('source'):
                            detail += ' to %s' % _type_text(change['source']).upper()
                    elif change['part'] == 'index' and change['change'] in ('only_in_target', 'different') and columns.intersection(
                            n.lower() for n in schema_ddl.index_columns(change['target'])):
                        detail = 'Dropping or changing index %s on %s' % (change['name'], obj['name'])
                    else:
                        continue
                    used = ', '.join('%s (%s)' % (n, (_fk_column(child, n) or ('unknown',))[0].upper()) for n in fk['columns'])
                    self.block(key, [child_key], '%s needs %s changed or dropped first: its foreign key %s uses %s. %s' % (
                        detail, child['name'], element['name'], used, self.choose(child_key)))

    def source_constraints(self, key, obj, kept):
        """Names and outgoing FKs must work against what actually stays."""
        for element in _table_elements(obj):
            kind = element['kind']
            if kind == 'foreign_key' or (kind == 'check' and self.target['server']['engine'] == 'mysql'):
                for other_key in sorted(kept):
                    other = self.old[other_key]
                    if any(e['kind'] == kind and e['name'].lower() == element['name'].lower() for e in _table_elements(other)):
                        self.block(key, [other_key], 'Constraint %s on %s needs %s changed or dropped first: it already uses that %s name. %s' % (
                            element['name'], obj['name'], other['name'], 'foreign key' if kind == 'foreign_key' else 'CHECK', self.choose(other_key)))
            if kind != 'foreign_key':
                continue
            fk = schema_ddl.foreign_key(element['text'])
            if not fk or fk['schema'] not in (None, self.source['database']):
                continue
            parent_key = self.new_tables.get(_table_name(self.source, fk['table'])) or self.old_tables.get(
                _table_name(self.target, fk['table']))
            if parent_key is None or parent_key in self.active:
                continue
            parent = self.old.get(parent_key)
            problem = None
            if parent is None or parent['type'] != 'table':
                problem = 'its referenced table is not available'
            elif _table_engine(parent) != 'innodb' or _table_engine(obj) != 'innodb':
                problem = 'both the child and the kept parent must use InnoDB for an enforced foreign key'
            else:
                for child_col, parent_col in zip(fk['columns'], fk['ref_columns']):
                    a, b = _fk_column(obj, child_col), _fk_column(parent, parent_col)
                    if a is None or b is None or a != b:
                        problem = '%s.%s does not match the type, character set or collation of the kept %s.%s' % (
                            obj['name'], child_col, parent['name'], parent_col)
                        break
                if not problem and not _has_fk_index(parent, fk['ref_columns']):
                    problem = 'the kept parent has no index for its referenced columns'
                elif not problem and _standard_fk_key(self.target) and not _has_fk_index(parent, fk['ref_columns'], True):
                    problem = 'the kept parent needs a full PRIMARY KEY or UNIQUE key on its referenced columns (MySQL 8.4+)'
            if problem:
                self.block(key, [parent_key], 'Foreign key %s on %s needs %s: %s. %s' % (
                    element['name'], obj['name'], self.entries[parent_key]['name'], problem, self.choose(parent_key)))

    def resolve(self):
        while True:
            kept = set(self.old) - self.active
            for key in sorted(self.active):
                entry, old, new = self.entries[key], self.old.get(key), self.new.get(key)
                if entry['status'] in ('identical', 'not_compared'):
                    continue
                if old and old['type'] == 'table':
                    self.kept_children(key, old, kept)
                    if new is None or new['type'] != 'table':
                        for trigger_key in sorted(kept):
                            trigger = self.old[trigger_key]
                            if trigger['type'] == 'trigger' and trigger.get('table') == old['name']:
                                self.block(key, [trigger_key], 'Dropping %s also drops trigger %s. %s' % (
                                    old['name'], trigger['name'], self.choose(trigger_key)))
                if new and new['type'] == 'table':
                    self.source_constraints(key, new, kept)
                if new:
                    for need in sorted(self.references.get(key, ()) - self.active):
                        # Stored programs can be created against existing old
                        # tables. A view, however, resolves its columns now.
                        # Always propagate a skipped prerequisite or one that
                        # does not exist at all in Target.
                        if need in self.blocked or need not in self.old or (
                                new['type'] == 'view' and self.entries[need]['status'] != 'identical'):
                            self.block(key, [need], '%s needs the source definition of %s first. %s' % (
                                new['name'], self.entries[need]['name'], self.choose(need)))
            blocked = self.active.intersection(self.blocked)
            if not blocked:
                break
            self.active.difference_update(blocked)
        for key, needs in self.blocked.items():
            # Flatten late-discovered blockers as well (order must not matter).
            pending, roots, seen = list(needs), set(), {key}
            while pending:
                need = pending.pop()
                if need in seen:
                    continue
                seen.add(need)
                if need in self.blocked:
                    pending.extend(self.blocked[need])
                else:
                    roots.add(need)
            self.entries[key]['blocked_by'] = sorted(roots)
        return self.active


class _Plan(object):
    def __init__(self, target, source, report, chosen_keys, retained=None):
        self.target, self.source, self.report = target, source, report
        self.db, self.source_db = target['database'], source['database']
        self.engine = target['server'].get('engine')
        self.old = {(o['type'], o['name']): o for o in target['objects']}
        self.new = {(o['type'], o['name']): o for o in source['objects']}
        self.statements, self.skipped, self.warnings = [], [], []
        self.change_statements = []
        self.saves = False
        retained = retained or {}
        selection = _Selection(target, source, report, chosen_keys, retained)
        chosen = selection.resolve()
        self.selection = selection
        for key in chosen_keys:
            entry = selection.entries[key]
            if key in retained:
                for item in retained[key]:
                    self.skip(item['type'], item['name'], item['reason'])
            elif entry['status'] == 'not_compared':
                self.skip(entry['type'], entry['name'], 'Not compared. %s' % entry.get('reason', ''))
        if any(s['server'].get('lower_case_table_names') not in ('0', '1', '2') for s in (target, source)):
            self.warnings.append('lower_case_table_names was not recorded; foreign key table names are assumed case-sensitive.')
        for key, reasons in selection.reasons.items():
            entry = selection.entries[key]
            self.skip(entry['type'], entry['name'], ' '.join(reasons))
        self.entries = [e for e in report['objects'] if e['key'] in chosen]
        self.allowed = set()
        for entry in self.entries:
            self.allowed.add((entry['type'], entry['name']))
            old_kind = next((c['target'] for c in entry.get('changes', []) if c['part'] == 'type'), entry['type'])
            self.allowed.add((old_kind, entry.get('target_name', entry['name'])))
            if entry['type'] == 'database':
                self.allowed.add(('database', self.db))

    # -- helpers ------------------------------------------------------------------------------

    def add(self, phase, obj, sql, risks=(), warnings=(), compound=False, risk_details=None, changes=()):
        if obj is not None and obj not in self.allowed:
            self.skip(obj[0], obj[1], 'Not chosen for the sync script.')
            return
        if risk_details is None:
            risk_details = _drop_risks(*obj) if obj and sql.startswith('DROP ') else empty_risks()
        statement = {'phase': phase, 'object': {'type': obj[0], 'name': obj[1]} if obj else None,
                     'sql': sql, 'risks': list(risks), 'warnings': list(warnings),
                     'compound': compound, 'risk_details': risk_details}
        self.statements.append(statement)
        self.change_statements.extend((change, statement) for change in changes)

    def skip(self, kind, name, reason):
        self.skipped.append({'type': kind, 'name': name, 'reason': reason})

    def foreign_key_changes(self, table, name):
        return [c for e in self.entries if e['type'] == 'table'
                and e.get('target_name', e['name']) == table
                for c in e.get('changes', []) if c['part'] == 'foreign_key' and c['name'].lower() == name.lower()]

    def ddl(self, obj):
        """A Source definition with Source's database name replaced by Target's."""
        return self.q(obj['ddl'])

    def q(self, text):
        """Copied Source text (a column, key, partitioning) with Source's
        database name replaced by Target's, outside literals."""
        return schema_ddl.requalify(text, self.source_db, self.db)

    # -- tables -------------------------------------------------------------------------------

    def table_plan(self, entry, old, new):
        """Statements for one changed table; returns foreign keys to drop
        and add as {table: {name: text}}, filled into self.fk_drop/fk_add."""
        changes = entry['changes']
        name = old['name']
        parts = {c['part'] for c in changes}
        if 'definition' in parts:
            return self.skip('table', name, 'Its definition could not be split into columns and keys; compare '
                                            'it by hand.')
        if 'name' in parts:
            return self.skip('table', name, 'The name differs only in letter case; rename it by hand if needed.')
        old_parts, new_parts = schema_ddl.split_table(old['ddl']), schema_ddl.split_table(new['ddl'])
        old_opts = schema_ddl.table_options(old_parts['options'])
        new_opts = schema_ddl.table_options(new_parts['options'])
        if 'period' in parts or ('WITH SYSTEM VERSIONING' in old_opts) != ('WITH SYSTEM VERSIONING' in new_opts):
            return self.skip('table', name, 'System versioning changes; use ALTER TABLE ... ADD or DROP SYSTEM '
                                            'VERSIONING by hand.')
        for c in changes:
            if c['part'] == 'foreign_key' and c['change'] in ('only_in_target', 'different'):
                self.fk_drop.setdefault(name, {})[c['name'].lower()] = c['name']
            if c['part'] == 'foreign_key' and c['change'] in ('only_in_source', 'different'):
                self.fk_add.setdefault(name, {})[c['name'].lower()] = (c['name'], c['source'])
        clauses = []
        warnings = []
        # DROP CHECK from MySQL 8.0.16; MariaDB drops a check with DROP CONSTRAINT.
        drop_check = 'DROP CHECK %s' if self.engine == 'mysql' else 'DROP CONSTRAINT %s'
        checks = [drop_check % quote(c['name']) for c in changes
                  if c['part'] in ('check', 'constraint') and c['change'] in ('only_in_target', 'different')]
        if checks:
            self.add('drop_checks', ('table', name), 'ALTER TABLE %s\n  %s' % (quote(name), ',\n  '.join(checks)),
                     changes=[c for c in changes if c['part'] in ('check', 'constraint')
                              and c['change'] in ('only_in_target', 'different')])
        for c in changes:
            if c['part'] == 'index' and c['change'] in ('only_in_target', 'different'):
                clauses.append(('DROP PRIMARY KEY' if c['name'] == 'PRIMARY' else 'DROP INDEX %s' % quote(c['name']),
                                ()))
        for c in changes:
            if c['part'] == 'column' and c['change'] == 'only_in_target':
                clauses.append(('DROP COLUMN %s' % quote(c['name']), ('drops_data',)))
                warnings.append('Drops column %s and its data.' % c['name'])
        dropped = [c['name'] for c in changes if c['part'] == 'column' and c['change'] == 'only_in_target']
        added = [c['name'] for c in changes if c['part'] == 'column' and c['change'] == 'only_in_source']
        if dropped and added:
            warnings.append('If a column was renamed (dropped: %s; added: %s), use RENAME COLUMN instead so its '
                            'data is kept.' % (', '.join(dropped), ', '.join(added)))
        self.columns(old_parts, new_parts, new, changes, clauses, name)
        fulltext = []
        for c in changes:
            if c['part'] in ('index', 'check', 'constraint') and c['change'] in ('only_in_source', 'different'):
                risky = c['part'] in ('check', 'constraint') or c['source'].startswith(('PRIMARY KEY', 'UNIQUE '))
                if c['source'].startswith('FULLTEXT ') and self.engine == 'mysql':
                    fulltext.append(c['source'])
                    if len(fulltext) > 1:
                        # InnoDB adds one FULLTEXT index per ALTER TABLE (ER_INNODB_FT_LIMIT,
                        # mysql-server storage/innobase/handler/handler0alter.cc:4508-4510).
                        continue
                clauses.append(('ADD ' + self.q(c['source']), ('checks_rows',) if risky else ()))
            elif c['part'] == 'other':
                warnings.append('Not generated: a definition line myflames does not recognize (%s).'
                                % (c.get('source') or c.get('target')))
        clauses.extend((option, ()) for option in self.options(old_opts, new_opts, warnings))
        # Partitioning ends the same ALTER TABLE, without a comma: MySQL drops a
        # column the old partitioning uses only in the statement that
        # repartitions (sql/sql_table.cc:15330-15350).
        partition = next((c for c in changes if c['part'] == 'partitioning'), None)
        tail = ''
        if partition:
            tail = '\nREMOVE PARTITIONING' if partition['change'] == 'only_in_target' else '\n' + self.q(partition['source'])
            warnings.append('Repartitioning copies every row of the table.')
        if clauses or tail:
            risks = sorted({r for _, rs in clauses for r in rs}, key=RISKS.index)
            body = ('\n  ' + ',\n  '.join(text for text, _ in clauses)) if clauses else ''
            # Only changes whose clauses are emitted belong above this statement.
            details = empty_risks()
            emitted = []
            for change in changes:
                if change['part'] == 'foreign_key':
                    continue
                if change['part'] == 'column':
                    markers = ('DROP COLUMN ' + quote(change['name']), 'MODIFY COLUMN ' + quote(change['name']),
                               'ADD COLUMN ' + quote(change['name']))
                    if not any(text.startswith(markers) for text, _ in clauses):
                        continue
                if change['part'] in ('check', 'constraint') and change['change'] == 'only_in_target':
                    continue
                if change['part'] == 'index' and change['change'] == 'only_in_source' and change.get('source') in fulltext[1:]:
                    continue
                emitted.append(change)
                details = _merge_risks(details, change.get('risks', empty_risks()))
            self.add('alter_tables', ('table', name), 'ALTER TABLE %s%s%s' % (quote(name), body, tail), risks, warnings,
                     risk_details=details, changes=emitted)
        elif warnings:
            self.warnings.extend('Table %s: %s' % (name, w) for w in warnings)
        for text in fulltext[1:]:
            self.add('alter_tables', ('table', name), 'ALTER TABLE %s ADD %s' % (quote(name), self.q(text)), (),
                     ['A separate statement: InnoDB adds one FULLTEXT index at a time.'],
                     changes=[c for c in changes if c['part'] == 'index' and c.get('source') == text])

    def columns(self, old_parts, new_parts, new, changes, clauses, table):
        """ADD and MODIFY clauses that give the columns Source's definitions
        and order, simulating the order as each clause applies. A text column
        names its character set and collation (Source's effective values), so
        it does not depend on the table default when the clause runs."""
        pairs = {name.lower(): pair for name, pair in (new.get('columns') or {}).items()}
        by_name = {c['name'].lower(): c for c in changes if c['part'] == 'column'}
        current = [e['name'] for e in old_parts['elements'] if e['kind'] == 'column'
                   and by_name.get(e['name'].lower(), {}).get('change') != 'only_in_target']
        lower = [n.lower() for n in current]
        new_cols = [e for e in new_parts['elements'] if e['kind'] == 'column']
        for index, col in enumerate(new_cols):
            key = col['name'].lower()
            pred = new_cols[index - 1]['name'] if index else None
            where = 'AFTER %s' % quote(pred) if pred else 'FIRST'
            spot = lower.index(pred.lower()) + 1 if pred else 0
            change = by_name.get(key)
            if change and change['change'] == 'only_in_source':
                clauses.append(('ADD COLUMN %s %s' % (self.q(schema_ddl.with_charset(col['text'], pairs.get(key))), where),
                                ()))
                lower.insert(spot, key)
                continue
            at = lower.index(key)
            move = (lower[at - 1] if at else None) != (pred.lower() if pred else None)
            if not move and not (change and change['change'] == 'different'):
                continue
            if change and change['change'] == 'different' and _storage(change['target']) != _storage(change['source']):
                # MODIFY cannot switch a generated column between STORED and
                # VIRTUAL, or make one (not) generated (sql/sql_table.cc:15741-15744).
                self.skip('table', table, 'Column %s changes how it is generated (%s to %s); drop and add it by hand.'
                          % (col['name'], _storage(change['target']) or 'not generated',
                             _storage(change['source']) or 'not generated'))
                continue
            text = self.q(schema_ddl.with_charset(col['text'], pairs.get(key)))
            risks = ()
            if change and change['change'] == 'different':
                collation = change.get('collation')
                if collation or _type_text(change['target']) != _type_text(change['source']) or \
                        ('NOT NULL' in change['source']) != ('NOT NULL' in change['target']):
                    risks = ('may_change_data',)
            clauses.append(('MODIFY COLUMN %s%s' % (text, ' ' + where if move else ''), risks))
            if move:
                lower.pop(at)
                spot = lower.index(pred.lower()) + 1 if pred else 0
                lower.insert(spot, key)

    def options(self, old, new, warnings):
        out = []
        charset = [n for n in ('DEFAULT CHARSET', 'COLLATE') if old.get(n) != new.get(n)]
        if charset:
            out.extend(new[n] for n in ('DEFAULT CHARSET', 'COLLATE') if n in new)
            warnings.append('A new default character set or collation applies to columns added later; columns '
                            'that change are listed with their own.')
        for name in ('DATA DIRECTORY', 'INDEX DIRECTORY'):
            if old.get(name) != new.get(name):
                warnings.append('Not generated: %s changes; move the table\'s files by hand.' % name)
        if old.get('KEY_BLOCK_SIZE') != new.get('KEY_BLOCK_SIZE') and 'KEY_BLOCK_SIZE' in old:
            # Kept keys keep their own block size (sql/sql_table.cc:16115-16116).
            warnings.append('Indexes this statement keeps keep their old KEY_BLOCK_SIZE; drop and add them to change it.')
        for name, token in new.items():
            if name in ('DEFAULT CHARSET', 'COLLATE', 'WITH SYSTEM VERSIONING', 'DATA DIRECTORY', 'INDEX DIRECTORY') \
                    or old.get(name) == token:
                continue
            out.append(token)
            if name == 'ENGINE':
                warnings.append('Changing the storage engine copies every row of the table.')
        for name in old:
            if name in new or name in ('DEFAULT CHARSET', 'COLLATE', 'WITH SYSTEM VERSIONING', 'DATA DIRECTORY',
                                       'INDEX DIRECTORY'):
                continue
            if name in _RESET_OPTION:
                out.append(_RESET_OPTION[name])
            else:
                warnings.append('Not generated: removing the table option %s; remove it by hand.' % old[name])
        return out

    def touched_foreign_keys(self, entries):
        """Unchanged foreign keys on or to a column whose type changes, or a
        key that is dropped or changes, are dropped and added again around
        the ALTER TABLE (otherwise MySQL refuses the change)."""
        columns, keys = {}, {}
        for entry, old, new in entries:
            for c in entry['changes']:
                if c['part'] == 'column' and c['change'] == 'different' and (
                        c.get('collation') or _type_text(c['target']) != _type_text(c['source'])):
                    columns.setdefault(_table_name(self.target, old['name']), set()).add(c['name'].lower())
                if c['part'] == 'index' and c['change'] in ('only_in_target', 'different'):
                    keys.setdefault(_table_name(self.target, old['name']), set()).update(
                        n.lower() for n in schema_ddl.index_columns(c['target']))
        if not columns and not keys:
            return
        existing = set()
        for obj in self.target['objects']:
            parts = schema_ddl.split_table(obj['ddl']) if obj['type'] == 'table' and obj.get('ddl') else None
            existing.update((_table_name(self.target, obj['name']), e['name'].lower()) for e in (parts['elements'] if parts else [])
                            if e['kind'] == 'foreign_key')
        pending = []
        # Dependencies that still exist in Target govern partial selection,
        # even when an unchecked Source table would remove its foreign key.
        objects = self.target['objects']
        for obj in objects:
            if obj['type'] != 'table' or not obj.get('ddl') or ('table', obj['name']) not in self.old:
                continue
            parts = schema_ddl.split_table(obj['ddl'])
            for item in (parts['elements'] if parts else []):
                if item['kind'] != 'foreign_key':
                    continue
                fk = schema_ddl.foreign_key(item['text'])
                if not fk:
                    continue
                table = _table_name(self.target, obj['name'])
                mine = {n.lower() for n in fk['columns']}
                ref_table = _table_name(self.target, fk['table']) if fk['schema'] in (None, self.db) else None
                theirs = {n.lower() for n in fk['ref_columns']}
                hit = (mine & (columns.get(table, set()) | keys.get(table, set()))) or (
                    ref_table and theirs & (columns.get(ref_table, set()) | keys.get(ref_table, set())))
                if not hit or item['name'].lower() in self.fk_add.get(obj['name'], {}) \
                        or (table, item['name'].lower()) not in existing:
                    # A key Target does not have is added later, never dropped.
                    continue
                if ('table', obj['name']) not in self.allowed:
                    # Selection dependencies were resolved before planning.
                    continue
                elif ('table', obj['name']) in self.new:
                    source_parts = schema_ddl.split_table(self.new[('table', obj['name'])].get('ddl'))
                    replacement = next((e for e in (source_parts['elements'] if source_parts else [])
                                        if e['kind'] == 'foreign_key' and e['name'].lower() == item['name'].lower()), None)
                    if replacement:
                        pending.append((obj['name'], replacement))
        for table, item in pending:
            self.fk_drop.setdefault(table, {})[item['name'].lower()] = item['name']
            self.fk_add.setdefault(table, {})[item['name'].lower()] = (item['name'], item['text'])
            self.fk_rebuilt.add((table, item['name']))
        return True

    def create_table(self, obj):
        parts = schema_ddl.split_table(obj['ddl'])
        if parts is None:
            self.add('create_tables', ('table', obj['name']), self.q(obj['ddl']), (),
                     ['Created with its foreign keys: its definition could not be split.'])
            return
        kept = []
        for item in parts['elements']:
            if item['kind'] == 'foreign_key':
                self.fk_add.setdefault(obj['name'], {})[item['name'].lower()] = (item['name'], item['text'])
            else:
                kept.append(item['text'])
        sql = 'CREATE TABLE %s (\n  %s\n) %s' % (quote(obj['name']), ',\n  '.join(kept), parts['options'])
        if parts['partitioning']:
            sql += '\n' + parts['partitioning']
        self.add('create_tables', ('table', obj['name']), self.q(sql))

    # -- other objects ------------------------------------------------------------------------

    def drop(self, kind, name, risks=(), warnings=()):
        if kind == 'trigger' or kind in _ROUTINES or kind == 'event':
            sql = 'DROP %s IF EXISTS %s' % (_DROP[kind], quote(name))
        else:
            sql = 'DROP %s %s' % (_DROP[kind], quote(name))
        phase = 'drop_tables' if kind in ('table', 'sequence') else 'drop_objects'
        self.add(phase, (kind, name), sql, risks, warnings)

    def create_program(self, phase, obj, replace=False):
        """A routine, package, trigger or event created under its own
        sql_mode (and an event under its time zone), then the session's
        saved values back, so later statements run as before. *replace*
        writes CREATE OR REPLACE (MariaDB routines and packages)."""
        ref = (obj['type'], obj['name'])
        settings = []
        if obj.get('sql_mode') is not None:
            settings.append(('sql_mode', obj['sql_mode']))
        if obj['type'] == 'event' and obj.get('time_zone'):
            settings.append(('time_zone', obj['time_zone']))
        if settings:
            self.saves = True
            self.add(phase, ref, 'SET SESSION ' + ', '.join('%s = %s' % (k, _literal(v)) for k, v in settings))
        text = self.ddl(obj)
        if replace and text.startswith('CREATE '):
            text = 'CREATE OR REPLACE ' + text[len('CREATE '):]
        if obj['type'] == 'trigger':
            # Triggers are created in Source's order, so an order clause kept
            # in the original text could name one not created yet. Only the
            # header's: the first FOR EACH ROW ends it, the body follows.
            head = text.find(' FOR EACH ROW')
            if head >= 0:
                match = _ORDER_CLAUSE.match(text, head + len(' FOR EACH ROW'))
                if match:
                    text = text[:match.start()] + text[match.end():]
        self.add(phase, ref, text, compound=True)
        if settings:
            self.add(phase, ref, 'SET SESSION ' + ', '.join('%s = @myflames_%s' % (k, k) for k, _ in settings))

    def views(self, views):
        """CREATE (OR REPLACE) VIEW in dependency order."""
        texts = {}
        for obj, replace in views:
            text = self.ddl(obj)
            if replace and text.startswith('CREATE '):
                text = 'CREATE OR REPLACE ' + text[len('CREATE '):]
            texts[obj['name']] = text
        done, order = set(), []

        def visit(name, path):
            if name in done or name in path:
                return
            for other in sorted(texts):
                if other != name and (quote(self.db) + '.' + quote(other)) in texts[name]:
                    visit(other, path | {name})
            done.add(name)
            order.append(name)
        for name in sorted(texts):
            visit(name, frozenset())
        for name in order:
            self.add('views', ('view', name), texts[name])

    def triggers(self, drops, creates):
        """Drop then create triggers; a group (table, timing, event) whose
        new or changed trigger must run before one that stays is recreated
        whole in Source's order, since a trigger created later runs last."""
        groups = {}
        for obj in self.source['objects']:
            if obj['type'] == 'trigger':
                key = ((obj.get('table') or '').lower(), obj.get('timing'), obj.get('event'))
                groups.setdefault(key, []).append(obj)
        recreated = []
        for key, members in groups.items():
            members.sort(key=lambda o: o.get('order') or 0)
            names = [m['name'] for m in members]
            new_at = [i for i, n in enumerate(names) if n in creates]
            stay_at = [i for i, n in enumerate(names) if n not in creates]
            if new_at and stay_at and min(new_at) < max(stay_at):
                unreadable = [obj['name'] for obj in members if obj.get('ddl') is None]
                unchecked = [n for n in names if ('trigger', n) not in self.allowed]
                if unreadable or unchecked:
                    for name in names:
                        if name in drops or name in creates:
                            self.skip('trigger', name, 'The order group includes unreadable triggers: %s.' % ', '.join(unreadable)
                                      if unreadable else 'Select the other triggers in this order group too: %s.' % ', '.join(unchecked))
                        drops.discard(name)
                        creates.discard(name)
                    continue
                for name in names:
                    if ('trigger', name) in self.old and name not in drops:
                        drops.add(name)
                    creates.add(name)
                recreated.append('%s %s triggers on %s' % (key[1], key[2], members[0].get('table')))
        for name in sorted(drops):
            self.drop('trigger', name)
        if recreated:
            self.warnings.append('Recreates all %s in Source\'s order, because a trigger created later runs after '
                                 'the others.' % '; '.join(recreated))
        for key in sorted(groups, key=lambda k: (k[0], k[1] or '', k[2] or '')):
            for obj in groups[key]:
                if obj['name'] in creates:
                    if obj.get('ddl') is None:
                        self.skip('trigger', obj['name'], 'Source\'s definition could not be read.')
                    else:
                        self.create_program('triggers', obj)

    # -- plan ---------------------------------------------------------------------------------

    def run(self):
        report = self.report
        self.fk_drop, self.fk_add, self.fk_rebuilt = {}, {}, set()
        tables, views, programs, events = [], [], [], []
        trigger_drops, trigger_creates = set(), set()
        drop_tables = []
        for entry in self.entries:
            status, kind, name = entry['status'], entry['type'], entry['name']
            if status == 'identical':
                continue
            if status == 'not_compared':
                self.skip(kind, name, 'Not compared. %s' % entry.get('reason', ''))
                continue
            old_kind = next((c['target'] for c in entry.get('changes', []) if c['part'] == 'type'), kind)
            old = self.old.get((old_kind, entry.get('target_name', name)))
            new = self.new.get((kind, name))
            if status == 'different' and kind == 'database':
                span = schema_ddl.object_name_span(new['ddl'], 'DATABASE')
                old_span = schema_ddl.object_name_span(old['ddl'], 'DATABASE')
                if span is None or old_span is None:
                    self.skip(kind, name, 'Its options could not be read from the definition.')
                    continue
                options = _READ_ONLY.sub('', new['ddl'][span[1]:]).strip()
                if options and options != _READ_ONLY.sub('', old['ddl'][old_span[1]:]).strip():
                    self.add('database', ('database', self.db), 'ALTER DATABASE %s %s' % (quote(self.db), options))
                continue
            if status == 'different' and old_kind != kind:
                if not self.remove(old, drop_tables, trigger_drops):
                    continue
                status = 'only_in_source'
            if status == 'only_in_target':
                self.remove(old, drop_tables, trigger_drops)
                continue
            if status == 'only_in_source':
                if new.get('ddl') is None:
                    self.skip(kind, name, 'Source\'s definition could not be read.')
                elif kind == 'table':
                    self.create_table(new)
                elif kind == 'sequence':
                    self.add('create_sequences', (kind, name), self.ddl(new))
                elif kind == 'view':
                    views.append((new, False))
                elif kind == 'trigger':
                    trigger_creates.add(name)
                elif kind == 'event':
                    events.append(new)
                else:
                    programs.append((new, False))
                continue
            # changed, same type
            if kind == 'table':
                tables.append((entry, old, new))
            elif kind == 'sequence':
                self.skip(kind, name, 'The sequence changes; use ALTER SEQUENCE by hand so its next value is kept.')
            elif any(c['part'] == 'name' for c in entry['changes']):
                self.skip(kind, name, 'The name differs only in letter case; rename it by hand if needed.')
            elif kind == 'view':
                views.append((new, True))
            elif kind == 'trigger':
                trigger_drops.add(old['name'])
                trigger_creates.add(name)
            elif kind in _ROUTINES and self.engine == 'mariadb':
                # CREATE OR REPLACE keeps the privileges granted on it
                # (mariadb-server sql/sp.cc:1341-1346 never revokes them).
                programs.append((new, True))
            elif kind == 'event':
                self.drop(kind, old['name'])
                events.append(new)
            else:
                # MySQL has no OR REPLACE for routines, and DROP revokes the
                # privileges granted on it while automatic_sp_privileges is ON
                # (the default; mysql-server sql/sql_parse.cc:4603-4606).
                self.drop(kind, old['name'], ('drops_privileges',), [_GRANTS_WARNING])
                programs.append((new, False))
        self.package_bodies(programs)
        self.touched_foreign_keys(tables)
        for entry, old, new in tables:
            self.table_plan(entry, old, new)
        # Foreign keys are dropped before any ALTER TABLE: phase order sorts them.
        for table in sorted(self.fk_drop):
            for key, fk_name in sorted(self.fk_drop[table].items()):
                note = (['Dropped and added again because a column or key it uses changes.']
                        if (table, fk_name) in self.fk_rebuilt else [])
                self.add('drop_foreign_keys', ('table', table),
                         'ALTER TABLE %s DROP FOREIGN KEY %s' % (quote(table), quote(fk_name)), (), note,
                         changes=self.foreign_key_changes(table, fk_name))
        for table in sorted(self.fk_add):
            for key, (fk_name, text) in sorted(self.fk_add[table].items()):
                self.add('add_foreign_keys', ('table', table), 'ALTER TABLE %s ADD %s'
                         % (quote(table), schema_ddl.requalify(text, self.source_db, self.db)), ('checks_rows',),
                         risk_details=change_risks({'part': 'foreign_key', 'name': fk_name,
                                                   'change': 'only_in_source', 'source': text}),
                         changes=self.foreign_key_changes(table, fk_name))
        self.dropped_tables = [n for k, n in drop_tables if k == 'table']
        if drop_tables:
            names = self.dropped_tables
            if names:
                self.add('drop_tables', ('table', names[0]) if len(names) == 1 else None,
                         'DROP TABLE %s' % ', '.join(quote(n) for n in names), ('drops_data',),
                         ['Drops %s and %s data.' % (', '.join(names), 'its' if len(names) == 1 else 'their')],
                         risk_details=_merge_risks(*[_drop_risks('table', n) for n in names]))
            for kind, name in drop_tables:
                if kind == 'sequence':
                    self.drop('sequence', name, ('drops_data',), ['Drops the sequence and its current value.'])
        programs.sort(key=lambda p: (_ROUTINES.index(p[0]['type']), p[0]['name']))
        for obj, replace in programs:
            self.create_program('routines', obj, replace)
        self.views(views)
        self.triggers(trigger_drops, trigger_creates)
        for obj in sorted(events, key=lambda o: o['name']):
            self.create_program('events', obj)
            self.event_warnings(obj)
        self.read_only()
        self.finish()

    def event_warnings(self, obj):
        text = obj.get('ddl') or ''
        # AT and ENDS in the past: not created (NOT PRESERVE) or created disabled
        # (mysql-server sql/event_parse_data.cc:100-118).
        if re.search(r'\bON SCHEDULE AT \'| ENDS \'', text):
            self.warnings.append('Event %s has a fixed AT or ENDS time: if it has passed when the script runs, the '
                                 'server does not create the event, or creates it disabled.' % obj['name'])
        zone = obj.get('time_zone') or 'SYSTEM'
        if zone != 'SYSTEM' and not re.match(r'^[+-]\d', zone):
            self.warnings.append('Event %s uses the time zone %s, which needs the time zone tables on the Target '
                                 'server.' % (obj['name'], zone))

    def package_bodies(self, programs):
        """CREATE OR REPLACE PACKAGE drops the package body (mariadb-server
        sql/sp.cc:1336-1340), so a replaced package gets Source's body again."""
        replaced = {o['name'] for o, replace in programs if o['type'] == 'package' and replace}
        listed = {o['name'] for o, _ in programs if o['type'] == 'package_body'}
        for name in sorted(replaced - listed):
            body = self.new.get(('package_body', name))
            if body is not None and body.get('ddl') is None:
                programs[:] = [(obj, replace) for obj, replace in programs if not (obj['type'] == 'package' and obj['name'] == name)]
                self.skip('package', name, 'Its source package body is unreadable; replacing the package would drop a body that cannot be recreated.')
                continue
            if (('package_body', name) in self.new or ('package_body', name) in self.old) and ('package_body', name) not in self.allowed:
                programs[:] = [(obj, replace) for obj, replace in programs if not (obj['type'] == 'package' and obj['name'] == name)]
                self.skip('package', name, 'Select its package body too; replacing the package drops that body.')
                continue
            body = self.new.get(('package_body', name))
            if body is not None and body.get('ddl'):
                programs.append((body, True))

    def read_only(self):
        """A read-only MySQL database refuses DDL (ER_SCHEMA_READ_ONLY): open it
        first, and close it at the end when Source is read-only."""
        flags = []
        for snapshot in (self.target, self.source):
            ddl = next((o.get('ddl') or '' for o in snapshot['objects'] if o['type'] == 'database'), '')
            match = _READ_ONLY.search(ddl)
            flags.append(bool(match and match.group(1) == '1'))
        if ('database', self.db) not in self.allowed:
            # Do not adopt an unchecked source database's READ ONLY option.
            flags[1] = flags[0]
            if flags[0] and self.statements:
                for kind, name in sorted({(s['object']['type'], s['object']['name'])
                                          for s in self.statements if s['object']}):
                    self.skip(kind, name, 'Select the database too; the target is read only and must be opened for DDL.')
                self.statements = []
                self.saves = False
            return
        body = [s for s in self.statements if s['phase'] != 'database']
        if flags[0] and (body or not flags[1]):
            # First of all, before the database's own options.
            self.add('database', ('database', self.db), 'ALTER DATABASE %s READ ONLY = 0' % quote(self.db), (),
                     ['Target is read only; it is opened for these statements.'] if flags[1] or body else [])
            self.statements.insert(0, self.statements.pop())
        if flags[1] and (body or not flags[0]):
            self.add('database_end', ('database', self.db), 'ALTER DATABASE %s READ ONLY = 1' % quote(self.db))

    def remove(self, old, drop_tables, trigger_drops):
        kind = old['type']
        if kind == 'package' and ('package_body', old['name']) in self.old and ('package_body', old['name']) not in self.allowed:
            self.skip(kind, old['name'], 'Select its package body too; dropping the package also drops that body.')
            return False
        if kind == 'table':
            blocked = []
            for child in self.target['objects']:
                if child['type'] != 'table' or child['name'] == old['name']:
                    continue
                parts = schema_ddl.split_table(child.get('ddl'))
                for element in parts['elements'] if parts else []:
                    if element['kind'] != 'foreign_key':
                        continue
                    fk = schema_ddl.foreign_key(element['text'])
                    if not fk or fk['schema'] not in (None, self.db) or (
                            _table_name(self.target, fk['table']) != _table_name(self.target, old['name'])):
                        continue
                    if ('table', child['name']) not in self.allowed:
                        blocked.append(child['name'])
            if blocked:
                self.skip(kind, old['name'], 'Select the tables with dependent foreign keys too: %s.' % ', '.join(sorted(set(blocked))))
                return False
            unchecked = [o['name'] for o in self.target['objects'] if o['type'] == 'trigger'
                         and o.get('table') == old['name'] and ('trigger', o['name']) not in self.allowed]
            if unchecked:
                self.skip(kind, old['name'], 'Select its triggers too; dropping this table also drops %s.' % ', '.join(unchecked))
                return False
        if kind in ('table', 'sequence'):
            drop_tables.append((kind, old['name']))
        elif kind == 'trigger':
            trigger_drops.add(old['name'])
        else:
            routine = kind in _ROUTINES
            self.drop(kind, old['name'], ('drops_privileges',) if routine else (),
                      [_GRANTS_WARNING] if routine else ())
        return True

    def finish(self):
        order = {phase: i for i, phase in enumerate(_PHASES)}
        body = sorted(self.statements, key=lambda s: order[s['phase']])
        session = {'phase': 'session', 'object': None, 'risks': [], 'warnings': [], 'compound': False,
                   'risk_details': empty_risks()}
        self.statements = [dict(session, sql='USE %s' % quote(self.db))] if body else []
        if self.saves:
            first = next(i for i, s in enumerate(body) if s['phase'] in ('routines', 'triggers', 'events'))
            body.insert(first, dict(session, sql='SET @myflames_sql_mode = @@SESSION.sql_mode, '
                                                 '@myflames_time_zone = @@SESSION.time_zone'))
        self.statements.extend(body)
        created = {s['object']['type'] for s in body if s['object'] and s['sql'].startswith('CREATE')}
        new_tables = [s['object']['name'] for s in body if s['phase'] == 'create_tables'
                      and s['sql'].startswith('CREATE TABLE')]
        if new_tables and self.dropped_tables:
            self.warnings.append('If a table was renamed (dropped: %s; new: %s), use RENAME TABLE instead so its rows '
                                 'are kept.' % (', '.join(self.dropped_tables), ', '.join(new_tables)))
        if any('\\' in s['sql'] for s in body):
            # SHOW CREATE writes a line break in a literal as \n (append_unescaped).
            self.warnings.append('Some definitions escape characters with a backslash (\\n in a comment or default): '
                                 'run the script in a session whose sql_mode does not include NO_BACKSLASH_ESCAPES.')
        if self.target['server'].get('engine') != self.source['server'].get('engine'):
            self.warnings.append('Source is %s and Target is %s: the statements copy %s definitions, which may not '
                                 'run on %s.' % (self.source['server'].get('server_product'),
                                                 self.target['server'].get('server_product'),
                                                 self.source['server'].get('server_product'),
                                                 self.target['server'].get('server_product')))
        if _newer(self.source['server'], self.target['server']):
            self.warnings.append('Source was read from a newer server (%s %s) than Target (%s %s): its definitions can '
                                 'use syntax Target does not accept.' % (
                                     self.source['server'].get('server_product'), self.source['server'].get('server_version'),
                                     self.target['server'].get('server_product'),
                                     self.target['server'].get('server_version')))
        if self.db != self.source_db:
            self.warnings.append('Statements use Target\'s database name %s where Source\'s definitions say %s.'
                                 % (self.db, self.source_db))
        if created & {'view', 'procedure', 'function', 'package', 'package_body', 'trigger', 'event'}:
            self.warnings.append('Views, routines, triggers and events keep Source\'s DEFINER: the account that runs '
                                 'the script needs the privilege to name another definer, and that account must '
                                 'exist on the Target server.')
        for label, snapshot in (('Source', self.source), ('Target', self.target)):
            unknown = [k for k, v in sorted((snapshot.get('coverage') or {}).items()) if v.get('state') != 'complete']
            if unknown:
                self.warnings.append('%s may not list every object (%s): an object it could not see can make a '
                                     'CREATE or DROP fail.' % (label, ', '.join(unknown)))



def _newer(a, b):
    """Server *a* runs a newer release of the same engine than *b*."""
    def parts(server):
        match = re.match(r'(\d+)\.(\d+)\.(\d+)', server.get('server_version') or '')
        return tuple(int(n) for n in match.groups()) if match else None
    return a.get('engine') == b.get('engine') and parts(a) is not None and parts(b) is not None and parts(a) > parts(b)


def _when(stamp):
    from .schema_versions import when_text
    return when_text(stamp)


def _delimiter(sql):
    for mark in _DELIMITERS:
        if mark not in sql:
            return mark
    n = 1
    while '$$%d' % n in sql:
        n += 1
    return '$$%d' % n


def _one_line(text):
    """Text for a -- comment line: line breaks become spaces."""
    return ' '.join((text or '').splitlines())


def _predict_algorithms(plan):
    """Judge the final emitted plan only, using captured Target DDL.

    Do not invent hidden table facts or project Source DDL onto Target.
    A later ALTER still uses the captured definition, not simulated live
    metadata. Missing definitions and hidden facts remain unpredicted.
    """
    fields = ('predicted', 'algorithm', 'rebuild', 'concurrent_dml', 'summary',
              'reason', 'source', 'needs', 'clauses', 'notes')
    for entry in plan.report['objects']:
        entry['risks']['rebuild'] = None
        for change in entry.get('changes', []):
            change['risks']['rebuild'] = None
    by_object = {}
    for statement in plan.statements:
        if not re.match(r'^ALTER\s+TABLE\b', statement['sql'], re.I):
            continue
        obj = statement['object']
        identity = (obj['type'], obj['name'])
        old = plan.old.get(identity, {})
        prediction = alter_algorithm.predict(statement['sql'], old.get('ddl'), plan.engine,
                                             plan.target['server'].get('server_version'))
        statement['algorithm'] = {key: prediction[key] for key in fields}
        statement['algorithm']['notes'] = prediction['notes'] + [
            'Uses the captured target definition; no live table facts were read.']
        sentence = None
        if prediction['predicted'] and (prediction['rebuild'] or prediction['algorithm'] == 'COPY'):
            head = ('Copies the table (COPY)' if prediction['algorithm'] == 'COPY'
                    else 'Rebuilds the table in place (INPLACE)')
            sentence = head + ('; writes continue.' if prediction['concurrent_dml']
                               else '; writes wait until it finishes.')
        statement['risk_details']['rebuild'] = sentence
        if sentence and sentence not in by_object.setdefault(identity, []):
            by_object[identity].append(sentence)
    for entry in plan.report['objects']:
        sentences = by_object.get((entry['type'], entry.get('target_name', entry['name'])), [])
        entry['risks']['rebuild'] = ' '.join(sentences) or None
    # A change split over several ALTERs has no single algorithm judgment.
    # Object and statement risks still show every known rebuild or copy.
    mapped = {}
    for change, statement in plan.change_statements:
        mapped.setdefault(id(change), (change, []))[1].append(statement)
    for change, statements in mapped.values():
        if len(statements) == 1:
            change['risks']['rebuild'] = statements[0]['risk_details']['rebuild']


def _algorithm_comment(prediction):
    if not prediction['predicted']:
        return 'not predicted (%s)' % _one_line(prediction['reason'])
    algorithm = prediction['algorithm']
    if algorithm == 'INSTANT':
        return algorithm
    if algorithm == 'COPY':
        return algorithm + (', writes continue' if prediction['concurrent_dml'] else ', writes wait')
    return prediction['summary'] + (', writes wait' if prediction['concurrent_dml'] is False else '')


def script(sync, target_label, source_label, generated_at):
    """The sync as one SQL text for a file or the Query editor."""
    statements = sync['statements']
    lines = ['-- myflames schema sync',
             '-- Changes the target: %s' % target_label,
             '-- To match the source: %s' % source_label,
             '-- Generated by myflames %s at %s. Review every statement before you run it;' % (
                 __version__, _when(generated_at)),
             '-- myflames never runs this script.']
    lines = [_one_line(line) for line in lines]
    lines.append('-- %d warnings.' % sync.get('summary', {}).get('warnings', 0))
    omitted = sync.get('omitted', [])
    if omitted:
        lines.append('-- %d differences left out.' % len(omitted))
        target_only = sum(1 for item in omitted if item['reason'] == 'Only in target; not chosen for DROP.')
        if target_only:
            lines.append('-- %d object%s only in the target %s not dropped; choose %s to include.' % (
                target_only, '' if target_only == 1 else 's', 'is' if target_only == 1 else 'are',
                'it' if target_only == 1 else 'them'))
        others = len(omitted) - target_only
        if others:
            lines.append('-- %d other difference%s not chosen for the sync script.' % (others, '' if others == 1 else 's'))
    if not statements:
        lines.extend(['', '-- No statements selected or generated.' if omitted or sync['skipped'] else '-- No differences to apply.'])
    previous = ()
    for statement in statements:
        obj = statement['object']
        if obj and statement['phase'] != 'session' and obj != previous:
            lines.extend(['', '-- %s %s' % (obj['type'].replace('_', ' '), _one_line(obj['name']))])
        elif statement['warnings'] or obj != previous:
            lines.append('')
        previous = obj
        for warning in statement['warnings']:
            lines.append('--   %s' % _one_line(warning))
        details = statement.get('risk_details', {})
        for kind, label in (('destructive', 'WARNING'), ('data_dependent', 'CHECK')):
            for sentence in details.get(kind, []):
                lines.append('-- %s: %s' % (label, _one_line(sentence)))
        if details.get('rebuild'):
            lines.append('-- WARNING: %s' % _one_line(details['rebuild']))
        if 'algorithm' in statement:
            lines.append('-- ALGORITHM: %s' % _algorithm_comment(statement['algorithm']))
        if statement['compound']:
            # The delimiter on a line of its own: a body may end in a -- comment.
            mark = _delimiter(statement['sql'])
            lines.extend(['DELIMITER %s' % mark, statement['sql'], mark, 'DELIMITER ;'])
        else:
            lines.append(statement['sql'] + ';')
    if sync['skipped']:
        lines.extend(['', '-- Not generated:'])
        lines.extend('--   %s %s: %s' % (s['type'].replace('_', ' '), _one_line(s['name']), _one_line(s['reason']))
                     for s in sync['skipped'])
    if sync['warnings']:
        lines.extend(['', '-- Notes:'])
        lines.extend('--   %s' % _one_line(w) for w in sync['warnings'])
    return '\n'.join(lines) + '\n'


def build(target, source, report, chosen_keys=None):
    """The ``sync`` object of a schema-compare-1.0 report."""
    chosen = selected_keys(report, chosen_keys)
    chosen_set = set(chosen)
    omitted = [{'key': e['key'], 'reason': 'Only in target; not chosen for DROP.' if e['status'] == 'only_in_target'
                else 'Not chosen for the sync script.'}
               for e in report['objects'] if e['key'] not in chosen_set and e['status'] != 'identical']
    # Planning can decline an object (or one of its columns) after selection
    # has resolved dependencies. Discard that tentative script, retain the
    # whole Target object and close dependencies again before emitting SQL.
    # The retained set only grows, so this terminates after at most one pass
    # per chosen object, plus the final stable pass.
    retained = {}
    while True:
        plan = _Plan(target, source, report, chosen, retained)
        plan.run()
        added = False
        for item in plan.skipped:
            identity = (item['type'], item['name'])
            key = plan.selection.new_keys.get(identity) or plan.selection.old_keys.get(identity)
            if key not in plan.selection.active or key in retained:
                continue
            retained[key] = [s for s in plan.skipped if (s['type'], s['name']) == identity]
            added = True
        if not added:
            break
    _predict_algorithms(plan)
    sync = {
        'database': plan.db,
        'chosen_keys': chosen,
        'omitted': omitted,
        'statements': plan.statements,
        'skipped': plan.skipped,
        'warnings': plan.warnings,
        'summary': {'statements': sum(1 for s in plan.statements if s['phase'] != 'session'),
                    'drops_data': sum(1 for s in plan.statements if 'drops_data' in s['risks']),
                    'skipped': len(plan.skipped),
                    'warnings': sum(len(s['risk_details'][kind]) for s in plan.statements
                                    for kind in ('destructive', 'data_dependent')) +
                                sum(bool(s['risk_details']['rebuild']) for s in plan.statements)},
    }
    sync['script'] = script(sync, report['target']['label'], report['source']['label'],
                                 report['compared_at'])
    return sync
