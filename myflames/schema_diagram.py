"""Bounded, read-only schema diagrams and offline schema interchange.

The canonical model is schema-diagram-1.0. Imported SQL is tokenized, never
sent to a server. SQL exports contain only validated schema statements.
Metadata comes from I_S TABLES, COLUMNS, STATISTICS, KEY_COLUMN_USAGE and
REFERENTIAL_CONSTRAINTS; ordered key parts are kept in their server order.
"""
import hashlib
import html
import json
import math
import re
import time
import unicodedata
from collections import namedtuple

from .connector import ConnectorError
from .perf_common import check_schema, literal, named_rows

SCHEMA_VERSION = 'schema-diagram-1.0'
MAX_BYTES = 2 * 1024 * 1024
MAX_TABLES, MAX_COLUMNS, MAX_RELATIONSHIPS, MAX_KEY_PARTS = 200, 10000, 1000, 20000
MAX_TOKENS, MAX_DEPTH, MAX_STATEMENTS = 100000, 64, 2000
READ_SECONDS = 30
TAG = '/*myflames:schema-diagram*/ '
ACTIONS = ('RESTRICT', 'CASCADE', 'SET NULL', 'NO ACTION', 'SET DEFAULT')
CARDINALITIES = ('zero-or-one', 'one', 'zero-or-many', 'one-or-many')
Token = namedtuple('Token', 'value kind start end')


def _text(value, label, limit=256, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value) or '\x00' in value:
        raise ValueError('Invalid %s.' % label)
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Invalid Unicode in %s.' % label) from None
    return value


def _input_text(text):
    _text(text, 'diagram text', MAX_BYTES, empty=True)
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Diagram input exceeds 2 MiB.')
    return text


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]


def _blank():
    return {'schema_version': SCHEMA_VERSION, 'tables': [], 'relationships': [], 'warnings': []}


def _warn(diagram, text):
    if text not in diagram['warnings']:
        diagram['warnings'].append(text)


def _table(schema, name, external=False):
    out = dict(id=_id(schema, name), schema=schema, name=name, columns=[], indexes=[])
    if external:
        out['external'] = True
    return out


def validate_diagram(value):
    """Validate before copying, including cycles, numeric layout and graph refs."""
    # Iterative bounds prevent hostile JSON-shaped direct callers from
    # reaching deepcopy/json recursion, even before HTTP serialization.
    todo, active, nodes, size = [(value, 0, False)], set(), 0, 0
    while todo:
        item, depth, leave = todo.pop()
        if leave:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > 150000 or depth > 32:
            raise ValueError('Diagram structure exceeds its size limit.')
        if type(item) in (dict, list):
            if id(item) in active:
                raise ValueError('Diagram must not contain cycles.')
            active.add(id(item)); todo.append((item, depth, True))
            size += 2 + len(item)
            if len(item) + nodes + len(todo) > 150000:
                raise ValueError('Diagram structure exceeds its size limit.')
            if isinstance(item, dict):
                for key, part in item.items():
                    if not isinstance(key, str):
                        raise ValueError('Diagram field names must be text.')
                    todo.append((key, depth + 1, False)); todo.append((part, depth + 1, False))
            else:
                todo.extend((part, depth + 1, False) for part in item)
        elif type(item) is str:
            _text(item, 'diagram value', MAX_BYTES, empty=True)
            size += len(item.encode('utf-8')) + 2 + sum(5 if ord(c) < 32 else 1 if c in '\\"' else 0 for c in item)
        elif item is None or type(item) is bool:
            size += 5
        elif type(item) in (int, float):
            if isinstance(item, float) and not math.isfinite(item) or isinstance(item, int) and item.bit_length() > 64:
                raise ValueError('Invalid diagram number.')
            size += 24
        else:
            raise ValueError('Diagram values must be JSON values.')
        if size > MAX_BYTES:
            raise ValueError('Diagram input exceeds 2 MiB.')
    # The traversal bounds memory and recursion before serialization. Its
    # estimate is not exact JSON punctuation accounting; enforce the wire
    # byte limit too, using compact UTF-8 rather than character count.
    if len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_BYTES:
        raise ValueError('Diagram input exceeds 2 MiB.')
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Expected schema-diagram-1.0 JSON.')
    tables, relationships = value.get('tables'), value.get('relationships')
    if not isinstance(tables, list) or len(tables) > MAX_TABLES or not isinstance(relationships, list) \
            or len(relationships) > MAX_RELATIONSHIPS:
        raise ValueError('Diagram exceeds 200 tables or 1000 relationships.')
    out, ids, names, total_columns, key_parts = _blank(), {}, set(), 0, 0
    for table in tables:
        if not isinstance(table, dict):
            raise ValueError('Invalid diagram table.')
        tid = _text(table.get('id'), 'table id')
        schema = _text(table.get('schema', ''), 'database name', empty=True)
        name = _text(table.get('name'), 'table name')
        if tid in ids or (schema, name) in names:
            raise ValueError('Table identifiers and names must be unique.')
        columns, indexes = table.get('columns'), table.get('indexes', [])
        if not isinstance(columns, list) or not isinstance(indexes, list):
            raise ValueError('Table columns and indexes must be lists.')
        target = dict(id=tid, schema=schema, name=name, columns=[], indexes=[])
        if table.get('external') is True:
            target['external'] = True
        seen_columns = set()
        for col in columns:
            if not isinstance(col, dict):
                raise ValueError('Invalid diagram column.')
            cname = _text(col.get('name'), 'column name')
            dtype = _text(col.get('data_type'), 'column type', 2048)
            if cname in seen_columns or (col.get('nullable') is not None and type(col.get('nullable')) is not bool) \
                    or type(col.get('primary_key')) is not bool:
                raise ValueError('Invalid or repeated diagram column.')
            seen_columns.add(cname)
            target['columns'].append(dict(name=cname, data_type=dtype, nullable=col.get('nullable'),
                                          primary_key=col['primary_key']))
        total_columns += len(columns)
        if total_columns > MAX_COLUMNS:
            raise ValueError('Diagram exceeds 10000 columns.')
        seen_indexes = set()
        for index in indexes:
            if not isinstance(index, dict):
                raise ValueError('Invalid diagram index.')
            iname = _text(index.get('name'), 'index name')
            cols = index.get('columns')
            if iname in seen_indexes or not isinstance(cols, list) or not cols or any(not isinstance(c, str) for c in cols) or len(set(cols)) != len(cols) \
                    or any(c not in seen_columns for c in cols) or type(index.get('unique')) is not bool:
                raise ValueError('Invalid index columns or duplicate index name.')
            seen_indexes.add(iname); key_parts += len(cols)
            item = dict(name=iname, columns=list(cols), unique=index['unique'])
            if index.get('primary') is True:
                if not item['unique']:
                    raise ValueError('A primary key must be unique.')
                item['primary'] = True
            target['indexes'].append(item)
        primary = [i for i in target['indexes'] if i.get('primary')]
        if len(primary) > 1:
            raise ValueError('A table cannot have two primary keys.')
        if primary and set(primary[0]['columns']) != {c['name'] for c in target['columns'] if c['primary_key']}:
            raise ValueError('Primary key columns and flags must agree.')
        if any(c['primary_key'] and c['nullable'] is True for c in target['columns']):
            raise ValueError('Primary key columns cannot be nullable.')
        if 'sql_definition' in table:
            target['sql_definition'] = _input_text(table['sql_definition'])
        names.add((schema, name)); ids[tid] = target; out['tables'].append(target)
    relation_ids = set()
    for rel in relationships:
        if not isinstance(rel, dict):
            raise ValueError('Invalid diagram relationship.')
        rid = _text(rel.get('id'), 'relationship id')
        name = _text(rel.get('name', ''), 'relationship name', empty=True)
        if rid in relation_ids or not isinstance(rel.get('from_table'), str) or not isinstance(rel.get('to_table'), str) \
                or rel['from_table'] not in ids or rel['to_table'] not in ids:
            raise ValueError('Relationship references an unknown table or repeats an id.')
        item = dict(id=rid, name=name, from_table=rel['from_table'], to_table=rel['to_table'])
        for side in ('from', 'to'):
            cols = rel.get(side + '_columns')
            if not isinstance(cols, list) or any(not isinstance(c, str) for c in cols) or len(set(cols)) != len(cols):
                raise ValueError('Invalid relationship column list.')
            table = ids[rel[side + '_table']]
            if not table.get('external') and any(c not in {c['name'] for c in table['columns']} for c in cols):
                raise ValueError('Relationship references an unknown column.')
            for c in cols:
                _text(c, 'relationship column')
            item[side + '_columns'] = list(cols); key_parts += len(cols)
        if len(item['from_columns']) != len(item['to_columns']):
            raise ValueError('Foreign key column counts differ.')
        for action in ('on_update', 'on_delete'):
            if action in rel:
                if rel[action] not in ACTIONS:
                    raise ValueError('Invalid foreign key action.')
                item[action] = rel[action]
        if 'cardinality' in rel:
            card = rel['cardinality']
            if not isinstance(card, dict) or any(card.get(k) not in CARDINALITIES for k in ('from', 'to')):
                raise ValueError('Invalid relationship cardinality.')
            item['cardinality'] = {k: card[k] for k in ('from', 'to')}
        if 'identifying' in rel:
            if rel['identifying'] is not None and type(rel['identifying']) is not bool:
                raise ValueError('Invalid relationship identity flag.')
            item['identifying'] = rel['identifying']
        relation_ids.add(rid); out['relationships'].append(item)
    if key_parts > MAX_KEY_PARTS:
        raise ValueError('Diagram exceeds 20000 key parts.')
    warnings = value.get('warnings', [])
    if not isinstance(warnings, list) or len(warnings) > 2000:
        raise ValueError('Invalid diagram warnings.')
    out['warnings'] = [_text(w, 'diagram warning', 2048) for w in warnings]
    if 'incomplete' in value:
        if type(value['incomplete']) is not bool:
            raise ValueError('Invalid diagram completeness flag.')
        out['incomplete'] = value['incomplete']
    if 'layout' in value:
        layout = value['layout']
        if not isinstance(layout, dict) or not isinstance(layout.get('positions'), dict):
            raise ValueError('Invalid diagram layout.')
        positions = {}
        for tid, point in layout['positions'].items():
            if tid not in ids or not isinstance(point, dict):
                raise ValueError('Layout references an unknown table.')
            for axis in ('x', 'y'):
                n = point.get(axis)
                if type(n) not in (int, float) or not math.isfinite(n) or abs(n) > 100000:
                    raise ValueError('Invalid diagram position.')
            positions[tid] = {axis: point[axis] for axis in ('x', 'y')}
        out['layout'] = {'positions': positions}
    return out


def read_diagram(conn, database):
    """Four schema-scoped metadata reads with a shared 30-second budget."""
    database = check_schema(database)
    if database is None:
        raise ValueError('Choose a database for the diagram.')
    start, out = time.monotonic(), _blank()
    def rows(sql):
        remaining = READ_SECONDS - (time.monotonic() - start)
        if remaining <= 0:
            raise ValueError('The schema read time budget expired; try a smaller database.')
        try:
            # XML distinguishes an identifier spelled NULL from SQL NULL.
            data = conn.run(TAG + sql, extra_flags=['--comments', '--xml'], timeout=remaining)
            return [list(row.values()) for row in named_rows(data)]
        except ConnectorError:
            raise ValueError('Could not read schema metadata; check the connection and metadata privileges.') from None
    scope = literal(database)
    tables = rows("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s "
                  "AND TABLE_TYPE IN ('BASE TABLE', 'SYSTEM VERSIONED') ORDER BY BINARY TABLE_NAME LIMIT 201" % scope)
    if len(tables) > MAX_TABLES:
        raise ValueError('This database exceeds the diagram limit of 200 tables.')
    by_name = {}
    for row in tables:
        if row and row[0] is not None:
            table = _table(database, row[0]); out['tables'].append(table); by_name[row[0]] = table
    cols = rows('SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS '
                'WHERE TABLE_SCHEMA = %s ORDER BY BINARY TABLE_NAME, ORDINAL_POSITION LIMIT 10001' % scope)
    if len(cols) > MAX_COLUMNS:
        raise ValueError('This database exceeds the diagram limit of 10000 columns.')
    for row in cols:
        if len(row) >= 4 and row[0] in by_name:
            by_name[row[0]]['columns'].append(dict(name=row[1], data_type=row[2], nullable=row[3] == 'YES', primary_key=False))
    keys = rows('SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, INDEX_TYPE '
                'FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s '
                'ORDER BY BINARY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX LIMIT 20001' % scope)
    if len(keys) > MAX_KEY_PARTS:
        raise ValueError('This database exceeds the diagram limit of 20000 key parts.')
    indexes, unsupported = {}, set()
    for row in keys:
        if len(row) < 7 or row[0] not in by_name:
            continue
        key = (row[0], row[1])
        if row[4] is None or row[5] is not None or row[6] not in ('BTREE', 'HASH'):
            unsupported.add(key)
            _warn(out, 'Functional, prefix, fulltext or spatial indexes are omitted from the structural model.')
        index = indexes.setdefault(key, dict(name=row[1], unique=row[2] == '0', columns=[]))
        if row[1] == 'PRIMARY':
            index['primary'] = True
        index['columns'].append(row[4])
    for key, index in indexes.items():
        if key not in unsupported:
            by_name[key[0]]['indexes'].append(index)
            if index.get('primary'):
                for col in by_name[key[0]]['columns']:
                    if col['name'] in index['columns']:
                        col['primary_key'] = True
    fks = rows('SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, '
               'k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, k.ORDINAL_POSITION, r.UPDATE_RULE, r.DELETE_RULE '
               'FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r '
               'ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME '
               'AND r.TABLE_NAME = k.TABLE_NAME WHERE k.TABLE_SCHEMA = %s AND k.REFERENCED_TABLE_NAME IS NOT NULL '
               'ORDER BY BINARY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION LIMIT 20001' % scope)
    if len(fks) > MAX_KEY_PARTS:
        raise ValueError('This database exceeds the diagram limit of 20000 foreign key parts.')
    relationships, external = {}, {}
    for row in fks:
        if len(row) < 9 or row[0] not in by_name:
            continue
        if row[3] == database and row[4] in by_name:
            target = by_name[row[4]]
        else:
            key = (row[3], row[4])
            target = external.setdefault(key, _table(row[3], row[4], external=True))
        key = (row[0], row[1])
        rel = relationships.setdefault(key, dict(id=_id(database, *key), name=row[1],
            from_table=by_name[row[0]]['id'], from_columns=[], to_table=target['id'], to_columns=[],
            on_update=row[7], on_delete=row[8]))
        rel['from_columns'].append(row[2]); rel['to_columns'].append(row[5])
    out['tables'].extend(external.values()); out['relationships'] = list(relationships.values())
    _warn(out, 'Only visible base tables and their metadata are read. Views and table data are not included.')
    _warn(out, 'SQL export is structural: defaults, generated expressions, engine options and unmodeled constraints are not collected.')
    if external:
        _warn(out, 'Referenced tables outside the visible database are shown without fetched columns.')
    return validate_diagram(out)


def _tokens(text, warnings=None):
    """A bounded SQL lexer, with literals/comments kept out of grammar tests."""
    _input_text(text)
    out, pos, depth = [], 0, 0
    while pos < len(text):
        c, start = text[pos], pos
        if c.isspace():
            pos += 1; continue
        if c == '#' or text.startswith('--', pos) and (pos + 2 == len(text) or text[pos + 2].isspace()):
            end = text.find('\n', pos); pos = len(text) if end < 0 else end + 1; continue
        if text.startswith('/*', pos):
            end = text.find('*/', pos + 2)
            if end < 0:
                raise ValueError('Unclosed SQL comment.')
            if text.startswith(('/*!', '/*M!'), pos) and warnings is not None:
                warnings.append('Version-specific executable comments were not interpreted.')
            pos = end + 2; continue
        if c in "'\"`":
            quote, value = c, []
            pos += 1
            while pos < len(text):
                if text[pos] == quote:
                    if pos + 1 < len(text) and text[pos + 1] == quote:
                        value.append(quote); pos += 2; continue
                    pos += 1; break
                if text[pos] == '\\' and quote != '`' and pos + 1 < len(text):
                    # Literal content is not executed or regenerated. Keep the
                    # original spelling when exporting a verified definition.
                    value.extend(text[pos:pos + 2]); pos += 2; continue
                value.append(text[pos]); pos += 1
            else:
                raise ValueError('Unclosed SQL quote.')
            out.append(Token(''.join(value), 'quoted' if quote in '`"' else 'string', start, pos))
        elif c.isalpha() or c in '_$':
            pos += 1
            while pos < len(text) and (text[pos].isalnum() or text[pos] in '_$'):
                pos += 1
            out.append(Token(text[start:pos], 'word', start, pos))
        elif c.isdigit():
            pos += 1
            while pos < len(text) and (text[pos].isdigit() or text[pos] == '.'):
                pos += 1
            out.append(Token(text[start:pos], 'number', start, pos))
        else:
            pos += 1
            if c == '(':
                depth += 1
                if depth > MAX_DEPTH:
                    raise ValueError('SQL nesting exceeds 64 levels.')
            elif c == ')':
                depth -= 1
                if depth < 0:
                    raise ValueError('Unbalanced SQL parentheses.')
            out.append(Token(c, 'symbol', start, pos))
        if len(out) > MAX_TOKENS:
            raise ValueError('SQL input exceeds 100000 tokens.')
    if depth:
        raise ValueError('Unbalanced SQL parentheses.')
    return out


def _is(token, word):
    return token.kind == 'word' and token.value.upper() == word


def _raw(text, tokens):
    return text[tokens[0].start:tokens[-1].end].strip() if tokens else ''


def _split(tokens, separator=','):
    chunks, start, depth = [], 0, 0
    for i, token in enumerate(tokens):
        if token.kind == 'symbol':
            if token.value == '(':
                depth += 1
            elif token.value == ')':
                depth -= 1
            elif token.value == separator and depth == 0:
                if tokens[start:i]:
                    chunks.append(tokens[start:i])
                start = i + 1
    if tokens[start:]:
        chunks.append(tokens[start:])
    return chunks


def _identifier(tokens, index):
    if index >= len(tokens) or tokens[index].kind not in ('word', 'quoted'):
        raise ValueError('Expected a SQL identifier.')
    return _text(tokens[index].value, 'SQL identifier', 64), index + 1


def _qualified(tokens, index, schema):
    name, index = _identifier(tokens, index)
    if index < len(tokens) and tokens[index].value == '.':
        other, index = _identifier(tokens, index + 1)
        return name, other, index
    return schema, name, index


def _parenthesized(tokens, index):
    if index >= len(tokens) or tokens[index].value != '(':
        raise ValueError('Expected a parenthesized SQL list.')
    depth = 0
    for end in range(index, len(tokens)):
        if tokens[end].kind == 'symbol':
            if tokens[end].value == '(':
                depth += 1
            elif tokens[end].value == ')':
                depth -= 1
                if depth == 0:
                    return tokens[index + 1:end], end + 1
    raise ValueError('Unclosed SQL list.')


_TYPE_NAMES = set(('BIT TINYINT SMALLINT MEDIUMINT INT INTEGER BIGINT DECIMAL DEC NUMERIC FIXED FLOAT DOUBLE REAL '
                   'BOOL BOOLEAN DATE DATETIME TIMESTAMP TIME YEAR CHAR CHARACTER VARCHAR NCHAR NVARCHAR BINARY '
                   'VARBINARY TINYBLOB BLOB MEDIUMBLOB LONGBLOB TINYTEXT TEXT MEDIUMTEXT LONGTEXT ENUM SET JSON '
                   'GEOMETRY POINT LINESTRING POLYGON MULTIPOINT MULTILINESTRING MULTIPOLYGON GEOMETRYCOLLECTION '
                   'INET4 INET6 UUID VECTOR').split())
_COLUMN_ATTRIBUTES = set(('NOT NULL DEFAULT AUTO_INCREMENT PRIMARY UNIQUE COMMENT COLLATE CHARACTER REFERENCES '
                          'CHECK GENERATED AS INVISIBLE VISIBLE ON COLUMN_FORMAT STORAGE SRID').split())


def _type_safe(dtype):
    """Types are SQL syntax, so never interpolate arbitrary JSON text."""
    tokens = _tokens(dtype)
    if not tokens or tokens[0].kind != 'word' or tokens[0].value.upper() not in _TYPE_NAMES:
        return False
    depth = 0
    # Backslash literals depend on NO_BACKSLASH_ESCAPES. Do not preserve
    # their spelling into a script that may run under a different mode.
    if any(t.kind == 'string' and '\\' in t.value for t in tokens):
        return False
    for token in tokens[1:]:
        if token.kind == 'symbol':
            if token.value == '(':
                depth += 1
            elif token.value == ')':
                depth -= 1
            elif token.value != ',':
                return False
        elif token.kind == 'word':
            if depth or token.value.upper() not in ('UNSIGNED', 'ZEROFILL', 'BINARY', 'VARYING', 'PRECISION'):
                return False
        elif token.kind == 'number':
            if not depth or not token.value.isdigit():
                return False
        elif token.kind == 'string':
            if not depth or tokens[0].value.upper() not in ('ENUM', 'SET'):
                return False
        else:
            return False
    return depth == 0


def _column(text, tokens, table, warnings):
    name, index = _identifier(tokens, 0)
    start, depth = index, 0
    while index < len(tokens):
        token = tokens[index]
        if index > start and not depth and token.kind == 'word' and token.value.upper() in _COLUMN_ATTRIBUTES:
            break
        if token.value == '(' and token.kind == 'symbol': depth += 1
        if token.value == ')' and token.kind == 'symbol': depth -= 1
        index += 1
    dtype = _raw(text, tokens[start:index])
    if not _type_safe(dtype):
        raise ValueError('Unsupported or invalid column type in SQL schema.')
    col = dict(name=name, data_type=dtype, nullable=True, primary_key=False)
    safe = True
    while index < len(tokens):
        token, upper = tokens[index], tokens[index].value.upper()
        if _is(token, 'NOT') and index + 1 < len(tokens) and _is(tokens[index + 1], 'NULL'):
            col['nullable'] = False; index += 2
        elif _is(token, 'NULL'):
            col['nullable'] = True; index += 1
        elif upper in ('PRIMARY', 'UNIQUE') and token.kind == 'word':
            primary = upper == 'PRIMARY'; index += 1
            if index < len(tokens) and _is(tokens[index], 'KEY'): index += 1
            if primary:
                col['primary_key'] = True; col['nullable'] = False
            table['indexes'].append(dict(name='PRIMARY' if primary else name,
                columns=[name], unique=True, **({'primary': True} if primary else {})))
        elif upper in ('AUTO_INCREMENT', 'VISIBLE', 'INVISIBLE') and token.kind == 'word':
            index += 1
        elif upper in ('DEFAULT', 'COMMENT') and token.kind == 'word':
            index += 1
            if index >= len(tokens): raise ValueError('Missing column default or comment value.')
            value = tokens[index]
            if value.kind == 'string' or value.kind == 'number' or value.kind == 'quoted' or value.value.upper() == 'NULL':
                index += 1
            elif value.value in ('+', '-') and index + 1 < len(tokens) and tokens[index + 1].kind == 'number':
                index += 2
            elif _is(value, 'CURRENT_TIMESTAMP'):
                index += 1
                if index < len(tokens) and tokens[index].value == '(':
                    parts, index = _parenthesized(tokens, index)
                    if any(t.kind != 'number' for t in parts): safe = False
            else:
                safe = False
                warnings.append('A column default expression is outside the supported SQL subset.')
                break
        elif upper in ('COLLATE', 'CHARACTER') and token.kind == 'word':
            index += 1
            if upper == 'CHARACTER' and index < len(tokens) and _is(tokens[index], 'SET'): index += 1
            _value, index = _identifier(tokens, index)
        elif _is(token, 'ON') and index + 2 < len(tokens) and _is(tokens[index + 1], 'UPDATE') \
                and _is(tokens[index + 2], 'CURRENT_TIMESTAMP'):
            index += 3
            if index < len(tokens) and tokens[index].value == '(':
                parts, index = _parenthesized(tokens, index)
                if any(t.kind != 'number' for t in parts): safe = False
        else:
            safe = False
            warnings.append('Column attributes outside the supported SQL subset were not modeled.')
            break
    table['columns'].append(col)
    return safe


def _key_columns(tokens, index):
    parts, index = _parenthesized(tokens, index)
    columns, simple = [], True
    for part in _split(parts):
        if not part or part[0].kind not in ('word', 'quoted'):
            columns.append(''); simple = False; continue
        name, end = _identifier(part, 0)
        if end != len(part):
            simple = False
        columns.append(name)
    if not columns:
        raise ValueError('An index or foreign key needs columns.')
    return columns, index, simple


def _constraint(text, tokens, table, pending, warnings):
    """Return (recognized, safe, foreign_key) for a table definition item."""
    if not tokens or tokens[0].kind != 'word':
        return False, True, False
    index, cname = 0, None
    if _is(tokens[index], 'CONSTRAINT'):
        index += 1
        if index < len(tokens) and not any(_is(tokens[index], k) for k in ('PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK')):
            cname, index = _identifier(tokens, index)
    if index >= len(tokens): raise ValueError('Incomplete SQL constraint.')
    first = tokens[index].value.upper()
    if first == 'FOREIGN':
        index += 1
        if index >= len(tokens) or not _is(tokens[index], 'KEY'): raise ValueError('Expected FOREIGN KEY.')
        index += 1
        if index < len(tokens) and tokens[index].value != '(':
            keyname, index = _identifier(tokens, index); cname = cname or keyname
        columns, index, simple = _key_columns(tokens, index)
        if index >= len(tokens) or not _is(tokens[index], 'REFERENCES'): raise ValueError('Foreign key needs REFERENCES.')
        schema, name, index = _qualified(tokens, index + 1, table['schema'])
        target_columns, index, target_simple = _key_columns(tokens, index)
        if not simple or not target_simple: raise ValueError('Foreign key columns must be plain column names.')
        suffix = '_%d' % (len(pending) + 1)
        generated_name = 'fk_' + table['name'][:61 - len(suffix)] + suffix
        rel = dict(name=cname or generated_name, from_table=table['id'],
                   from_columns=columns, to_schema=schema, to_name=name, to_columns=target_columns)
        safe = True
        while index < len(tokens):
            if _is(tokens[index], 'ON') and index + 2 < len(tokens) and tokens[index + 1].value.upper() in ('UPDATE', 'DELETE'):
                action = 'on_' + tokens[index + 1].value.lower(); index += 2
                value = tokens[index].value.upper(); index += 1
                if value in ('SET', 'NO') and index < len(tokens):
                    value += ' ' + tokens[index].value.upper(); index += 1
                if value not in ACTIONS: raise ValueError('Unsupported foreign key action.')
                rel[action] = value
            else:
                warnings.append('Foreign key clauses outside ON UPDATE and ON DELETE were not modeled.')
                safe = False; break
        pending.append(rel)
        return True, safe, True
    if first in ('PRIMARY', 'UNIQUE', 'KEY', 'INDEX'):
        primary, unique = first == 'PRIMARY', first in ('PRIMARY', 'UNIQUE')
        index += 1
        if first in ('PRIMARY', 'UNIQUE') and index < len(tokens) and tokens[index].value.upper() in ('KEY', 'INDEX'):
            index += 1
        name = 'PRIMARY' if primary else cname
        if index < len(tokens) and tokens[index].value != '(' and not _is(tokens[index], 'USING'):
            name, index = _identifier(tokens, index)
        if index < len(tokens) and _is(tokens[index], 'USING'):
            if index + 1 >= len(tokens) or not any(_is(tokens[index + 1], algorithm) for algorithm in ('BTREE', 'HASH')):
                warnings.append('Index algorithms outside BTREE and HASH were omitted from the structural model.')
                return True, False, False
            index += 2
        columns, index, simple = _key_columns(tokens, index)
        if not simple:
            warnings.append('Prefix or expression indexes were omitted from the structural model.')
            return True, False, False
        if index < len(tokens):
            warnings.append('Index options outside the supported SQL subset were not modeled.')
            return True, False, False
        if name is None:
            name, suffix = columns[0], 2
            occupied = {i['name'] for i in table['indexes']}
            while name in occupied:
                ending = '_%d' % suffix
                name = columns[0][:64 - len(ending)] + ending; suffix += 1
        table['indexes'].append(dict(name=name, columns=columns, unique=unique,
                                     **({'primary': True} if primary else {})))
        return True, True, False
    if first in ('CHECK', 'FULLTEXT', 'SPATIAL') or cname is not None:
        warnings.append('CHECK, fulltext, spatial or unsupported constraints were not modeled.')
        return True, False, False
    return False, True, False


def _table_options(tokens):
    """Only schema-local options may survive in preserved CREATE statements."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != 'word': return False
        name = token.value.upper(); index += 1
        if name == 'DEFAULT' and index < len(tokens):
            name = tokens[index].value.upper(); index += 1
        if name == 'CHARACTER' and index < len(tokens) and _is(tokens[index], 'SET'):
            name = 'CHARSET'; index += 1
        if name not in ('ENGINE', 'CHARSET', 'COLLATE', 'COMMENT', 'AUTO_INCREMENT', 'ROW_FORMAT', 'KEY_BLOCK_SIZE'):
            return False
        if index < len(tokens) and tokens[index].value == '=': index += 1
        if index >= len(tokens) or tokens[index].kind not in ('word', 'number', 'string', 'quoted'): return False
        index += 1
    return True


def _finish(out, pending):
    by_name = {(t['schema'], t['name']): t for t in out['tables']}
    for table in out['tables']:
        primary = [i for i in table['indexes'] if i.get('primary')]
        if primary:
            for column in table['columns']:
                if column['name'] in primary[0]['columns']:
                    column['primary_key'] = True; column['nullable'] = False
    for rel in pending:
        key = (rel.pop('to_schema'), rel.pop('to_name'))
        if key not in by_name:
            external = _table(*key, external=True)
            out['tables'].append(external); by_name[key] = external
            _warn(out, 'Referenced tables without a definition are shown as external placeholders.')
        rel['to_table'] = by_name[key]['id']; rel['id'] = _id(rel['from_table'], rel['name'])
        out['relationships'].append(rel)
    out['warnings'] = list(dict.fromkeys(out['warnings']))
    return validate_diagram(out)


def import_sql(text, default_schema=''):
    """Parse CREATE TABLE and ALTER TABLE ADD constraints, never execute SQL.

    Other statements are ignored with named warnings. Unsupported table
    clauses disable original-DDL preservation, so export cannot silently
    reintroduce a clause that the diagram failed to understand.
    """
    out, pending, schema, alters = _blank(), [], default_schema, []
    all_tokens = _tokens(text, out['warnings'])
    statements = _split(all_tokens, ';')
    if len(statements) > MAX_STATEMENTS:
        raise ValueError('SQL input exceeds 2000 statements.')
    seen = set()
    for tokens in statements:
        first = tokens[0].value.upper() if tokens and tokens[0].kind == 'word' else ''
        if first == 'USE':
            schema, end = _identifier(tokens, 1)
            if end != len(tokens): raise ValueError('Invalid USE statement.')
            continue
        if first == 'ALTER' and len(tokens) > 2 and _is(tokens[1], 'TABLE'):
            alters.append((schema, tokens)); continue
        if first != 'CREATE' or len(tokens) < 2 or not _is(tokens[1], 'TABLE'):
            _warn(out, 'Ignored %s statement; only table definitions and added constraints are imported.' %
                  (first if re.fullmatch('[A-Z_]+', first) else 'unsupported'))
            continue
        index = 2
        if index + 2 < len(tokens) and [_is(tokens[index + i], w) for i, w in enumerate(('IF', 'NOT', 'EXISTS'))] == [True] * 3:
            index += 3
        db, name, index = _qualified(tokens, index, schema)
        if (db, name) in seen: raise ValueError('Repeated CREATE TABLE definition.')
        if index >= len(tokens) or tokens[index].value != '(':
            _warn(out, 'CREATE TABLE LIKE and CREATE TABLE AS SELECT are not imported.'); continue
        parts, end = _parenthesized(tokens, index)
        if any(_is(t, 'SELECT') for t in tokens[end:]):
            _warn(out, 'CREATE TABLE AS SELECT is not imported.'); continue
        table, safe = _table(db, name), True
        for part in _split(parts):
            recognized, supported, _foreign = _constraint(text, part, table, pending, out['warnings'])
            safe = safe and supported
            if not recognized:
                safe = _column(text, part, table, out['warnings']) and safe
        if not _table_options(tokens[end:]):
            safe = False; _warn(out, 'Table options outside the supported SQL subset were not modeled.')
        raw = _raw(text, tokens)
        if '/*!' in raw or '/*M!' in raw:
            safe = False
        if any(t.kind in ('string', 'quoted') and '\\' in t.value for t in tokens):
            safe = False
            _warn(out, 'Backslash literals or identifiers depend on SQL mode; original DDL was not retained.')
        if safe:
            # The exporter re-parses this untrusted field and compares its
            # modeled structure before preserving any original column DDL.
            table['sql_definition'] = raw
        else:
            _warn(out, 'SQL export for a partially imported table uses only its modeled structure.')
        out['tables'].append(table); seen.add((db, name))
        if len(out['tables']) > MAX_TABLES: raise ValueError('Diagram exceeds 200 tables.')
    for alter_schema, tokens in alters:
        db, name, index = _qualified(tokens, 2, alter_schema)
        table = next((t for t in out['tables'] if (t['schema'], t['name']) == (db, name)), None)
        if table is None:
            _warn(out, 'ALTER TABLE for a table without a CREATE definition was ignored.'); continue
        table.pop('sql_definition', None)
        _warn(out, 'Tables changed by ALTER TABLE export only their modeled structure.')
        for part in _split(tokens[index:]):
            if part and _is(part[0], 'ADD'):
                recognized, _safe, _fk = _constraint(text, part[1:], table, pending, out['warnings'])
                if recognized: continue
            _warn(out, 'Only ALTER TABLE ADD key and foreign key constraints are imported.')
    return _finish(out, pending)


def _quote(name):
    _text(name, 'SQL identifier', 64)
    return '`' + name.replace('`', '``') + '`'


def _qname(table):
    return (_quote(table['schema']) + '.' if table['schema'] else '') + _quote(table['name'])


def _type_sql(dtype):
    if not _type_safe(dtype):
        raise ValueError('Column type is not a supported SQL type.')
    return ' '.join(dtype[t.start:t.end] for t in _tokens(dtype))


def _signature(table, relationships, tables):
    """Compare modeled semantics, not mutable IDs or optional display layout."""
    base = {k: table[k] for k in ('schema', 'name')}
    base['columns'] = [dict(c, data_type=[(t.value.upper() if t.kind == 'word' else t.value, t.kind)
                                        for t in _tokens(c['data_type'])]) for c in table['columns']]
    base['indexes'] = sorted(table['indexes'], key=lambda i: i['name'])
    base['relationships'] = []
    for rel in relationships:
        if rel['from_table'] == table['id'] and rel['from_columns']:
            target = tables[rel['to_table']]
            base['relationships'].append(dict(name=rel['name'], from_columns=rel['from_columns'],
                to_schema=target['schema'], to_name=target['name'], to_columns=rel['to_columns'],
                on_update=rel.get('on_update', 'RESTRICT'), on_delete=rel.get('on_delete', 'RESTRICT')))
    base['relationships'].sort(key=lambda r: r['name'])
    return base


def _preserved_definition(table, diagram):
    text = table.get('sql_definition')
    if not text:
        return None
    try:
        parsed = import_sql(text, table['schema'])
        real = [t for t in parsed['tables'] if not t.get('external')]
        # External placeholders are expected for FKs in an isolated CREATE.
        unexpected = [w for w in parsed['warnings'] if w !=
                      'Referenced tables without a definition are shown as external placeholders.']
        if len(real) != 1 or unexpected or 'sql_definition' not in real[0]:
            return None
        lookup = {t['id']: t for t in diagram['tables']}
        other = {t['id']: t for t in parsed['tables']}
        if _signature(table, diagram['relationships'], lookup) != _signature(real[0], parsed['relationships'], other):
            return None
        tokens = _tokens(text)
        # Only one CREATE statement may survive. All FKs are emitted after
        # CREATEs, allowing self-references, cycles and forward references.
        statements = _split(tokens, ';')
        if len(statements) != 1: return None
        tokens = statements[0]
        start = next(i for i, t in enumerate(tokens) if t.value == '(' and t.kind == 'symbol')
        parts, end = _parenthesized(tokens, start)
        columns = []
        for part in _split(parts):
            _recognized, _safe, foreign = _constraint(text, part, _table(table['schema'], table['name']), [], [])
            if not foreign:
                columns.append(_raw(text, part))
        return 'CREATE TABLE ' + _qname(table) + ' (\n  ' + ',\n  '.join(columns) + '\n)' + \
               (' ' + _raw(text, tokens[end:]) if tokens[end:] else '') + ';'
    except (ValueError, KeyError, StopIteration):
        return None


def export_sql(diagram):
    diagram = validate_diagram(diagram)
    warnings, sql, emitted = list(diagram['warnings']), [], set()
    if diagram.get('incomplete'):
        warnings.append('The diagram is marked incomplete; SQL export may omit schema objects.')
    tables = {t['id']: t for t in diagram['tables']}
    for table in diagram['tables']:
        if table.get('external'):
            continue
        if not table['columns']:
            warnings.append('A table without column definitions was omitted from SQL export.')
            continue
        preserved = _preserved_definition(table, diagram)
        if preserved:
            sql.append(preserved); emitted.add(table['id']); continue
        if table.get('sql_definition'):
            warnings.append('An original SQL definition no longer matches the diagram or is unsupported; exported its modeled structure.')
        definitions = []
        for col in table['columns']:
            dtype = col['data_type']
            if not _type_safe(dtype):
                mapping = {'string': 'TEXT', 'number': 'DECIMAL', 'boolean': 'BOOLEAN'}
                dtype = mapping.get(dtype.lower(), 'TEXT')
                warnings.append('A conceptual or unsupported column type was exported as a SQL fallback type; review the DDL.')
            definitions.append(_quote(col['name']) + ' ' + _type_sql(dtype) +
                               (' NOT NULL' if col['nullable'] is False or col['primary_key'] else ''))
        indexes = list(table['indexes'])
        if not any(i.get('primary') for i in indexes):
            pk = [c['name'] for c in table['columns'] if c['primary_key']]
            if pk:
                indexes.append(dict(name='PRIMARY', columns=pk, unique=True, primary=True))
        for index in indexes:
            name = 'PRIMARY KEY' if index.get('primary') else ('UNIQUE KEY ' if index['unique'] else 'KEY ') + _quote(index['name'])
            definitions.append(name + ' (' + ', '.join(_quote(c) for c in index['columns']) + ')')
        sql.append('CREATE TABLE ' + _qname(table) + ' (\n  ' + ',\n  '.join(definitions) + '\n);')
        emitted.add(table['id'])
        warnings.append('Structural SQL omits defaults, generated expressions, engine options and constraints absent from the model.')
    for rel in diagram['relationships']:
        if not rel['from_columns']:
            warnings.append('Conceptual relationships have no FK column mapping and were not exported as SQL constraints.')
            continue
        if rel['from_table'] not in emitted:
            warnings.append('A foreign key from an omitted table was not exported.'); continue
        target = tables[rel['to_table']]
        if not target.get('external') and rel['to_table'] not in emitted:
            warnings.append('A foreign key to an omitted table was not exported.'); continue
        statement = 'ALTER TABLE ' + _qname(tables[rel['from_table']]) + ' ADD '
        if rel['name']:
            statement += 'CONSTRAINT ' + _quote(rel['name']) + ' '
        statement += 'FOREIGN KEY (' + ', '.join(_quote(c) for c in rel['from_columns']) + ') REFERENCES ' + \
                     _qname(target) + ' (' + ', '.join(_quote(c) for c in rel['to_columns']) + ')'
        for action in ('on_update', 'on_delete'):
            if action in rel:
                statement += ' ' + action.replace('_', ' ').upper() + ' ' + rel[action]
        sql.append(statement + ';')
    return {'text': '\n\n'.join(sql) + ('\n' if sql else ''), 'warnings': list(dict.fromkeys(warnings))}


_MERMAID_ENTITY = r'(?:"[^"\n]+"|[^\s\[\]{}:"]+)'
_MERMAID_RELATION = re.compile(r'^(' + _MERMAID_ENTITY + r')\s+(\|o|\|\||\}o|\}\|)(--|\.\.)(o\||\|\||o\{|\|\{)\s+(' +
                               _MERMAID_ENTITY + r')\s*:\s*(?:"([^"\n]*)"|([^"\n]+))\s*$')
_LEFT_CARD = {'|o': 'zero-or-one', '||': 'one', '}o': 'zero-or-many', '}|': 'one-or-many'}
_RIGHT_CARD = {'o|': 'zero-or-one', '||': 'one', 'o{': 'zero-or-many', '|{': 'one-or-many'}


_MERMAID_TEXT_ENTITY = re.compile(r'(?:#\d+|#[A-Za-z]+|&#[0-9]+|&#[xX][0-9A-Fa-f]+|&[A-Za-z]+);')
_MERMAID_METADATA = re.compile(r'(?:^|;\s*)(Original name|SQL type|Nullability):\s*("(?:[^"\\]|\\.)*")')


def _mermaid_unescape(text):
    def decode(match):
        token = match.group(0)
        if token.startswith('#'):
            token = '&' + token if token[1].isdigit() else '&' + token[1:]
        return html.unescape(token)
    return _MERMAID_TEXT_ENTITY.sub(decode, text)


def _mermaid_name(token):
    return _mermaid_unescape(token[1:-1] if token.startswith('"') else token)


def _mermaid_comment_metadata(comment):
    metadata = {}
    for match in _MERMAID_METADATA.finditer(comment):
        try:
            value = json.loads(match.group(2))
        except ValueError:
            continue
        if isinstance(value, str):
            metadata[match.group(1)] = value
    return metadata


def import_mermaid(text):
    """A bounded erDiagram subset. Relationships are conceptual, not SQL FKs."""
    _input_text(text)
    out, entities, active, header, total_columns = _blank(), {}, None, False, 0
    def entity(token, alias=None):
        key = _mermaid_name(token)
        if key not in entities:
            entities[key] = _table('', _mermaid_name(alias) if alias else key)
            # Stable per-source identifier even when entity aliases repeat.
            entities[key]['id'] = _id('mermaid', key)
            out['tables'].append(entities[key])
            if len(out['tables']) > MAX_TABLES: raise ValueError('Diagram exceeds 200 tables.')
        elif alias:
            entities[key]['name'] = _mermaid_name(alias)
        return entities[key]
    def attribute(line, table):
        nonlocal total_columns
        match = re.fullmatch(r'([\w][\w()\[\],.?-]*)\s+(\*?[\w][\w()\[\]-]*)(?:\s+((?:PK|FK|UK)(?:\s*,\s*(?:PK|FK|UK))*))?(?:\s+"([^"\n]*)")?', line)
        if not match:
            _warn(out, 'Unsupported Mermaid attribute syntax was ignored.'); return
        dtype, name, keys, comment = match.groups()
        keys = {k.strip() for k in (keys or '').split(',')}
        pk = 'PK' in keys or name.startswith('*')
        # Legacy exports started with a labeled JSON string. New comments
        # escape colons inside values, so literal metadata-looking text cannot
        # be mistaken for the old format.
        legacy = re.match(r'^(Original name|SQL type|Nullability):', comment or '')
        metadata = _mermaid_comment_metadata(_mermaid_unescape(comment or '')) if legacy else {}
        nullable = False if pk or not dtype.endswith('?') else True
        sql_type = _mermaid_sql_type(dtype.rstrip('?'))
        if not metadata and comment is not None:
            parts, start = [], 0
            # Entity terminators may also be followed by a space. Only split
            # separators outside an encoded entity.
            for delimiter in re.finditer(_MERMAID_TEXT_ENTITY.pattern + r'|(; )', comment):
                if delimiter.group(1):
                    parts.append(comment[start:delimiter.start()]); start = delimiter.end()
            parts.append(comment[start:])
            if dtype.rstrip('?').lower() in ('enum', 'set'):
                values = [_mermaid_unescape(v) for v in parts.pop(0).split(', ')]
                sql_type = dtype.rstrip('?') + '(' + ','.join("'" + v.replace("'", "''") + "'" for v in values) + ')'
            for part in parts:
                if part == 'nullability unknown':
                    nullable = None
                elif part.startswith('name: '):
                    metadata['Original name'] = _mermaid_unescape(part[6:])
        name = metadata.get('Original name', name.lstrip('*'))
        if metadata.get('Nullability') == 'unknown':
            nullable = None
        elif metadata.get('Nullability') == 'NULL':
            nullable = True
        elif metadata.get('Nullability') == 'NOT NULL':
            nullable = False
        total_columns += 1
        if total_columns > MAX_COLUMNS:
            raise ValueError('Diagram exceeds 10000 columns.')
        table['columns'].append(dict(name=name, data_type=metadata.get('SQL type', sql_type),
                                     nullable=nullable, primary_key=pk))
        if 'FK' in keys:
            _warn(out, 'Mermaid FK markers do not identify referenced columns; no SQL foreign key was inferred.')
        if 'UK' in keys:
            _warn(out, 'Mermaid UK markers do not distinguish single-column and composite unique keys; no SQL index was inferred.')
    lines = text.splitlines()
    if len(lines) > MAX_TOKENS: raise ValueError('Mermaid input has too many lines.')
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('%%'): continue
        if not header:
            if line != 'erDiagram': raise ValueError('Expected Mermaid erDiagram syntax.')
            header = True; continue
        if active is not None:
            if line == '}': active = None
            else: attribute(line, active)
            continue
        relation = _MERMAID_RELATION.fullmatch(line)
        if relation:
            if len(out['relationships']) >= MAX_RELATIONSHIPS:
                raise ValueError('Diagram exceeds 1000 relationships.')
            left, lc, kind, rc, right, quoted_label, label = relation.groups()
            a, b = entity(left), entity(right)
            out['relationships'].append(dict(id=_id(a['id'], b['id'], len(out['relationships'])),
                name=_mermaid_unescape(quoted_label if quoted_label is not None else label.strip()),
                from_table=a['id'], from_columns=[], to_table=b['id'], to_columns=[],
                cardinality={'from': _LEFT_CARD[lc], 'to': _RIGHT_CARD[rc]}, identifying=kind == '--'))
            continue
        declaration = re.fullmatch(r'(' + _MERMAID_ENTITY + r')(?:\s*\[\s*(' + _MERMAID_ENTITY + r')\s*\])?\s*(?:\{(.*))?', line)
        if declaration:
            token, alias, content = declaration.groups()
            table = entity(token, alias)
            if content is not None:
                if content.strip().endswith('}'):
                    body = content.strip()[:-1].strip()
                    if body: attribute(body, table)
                else:
                    active = table
                    if content.strip(): attribute(content.strip(), table)
            continue
        _warn(out, 'Unsupported Mermaid directives, styles or relationship syntax were ignored.')
    if not header or active is not None:
        raise ValueError('Incomplete Mermaid erDiagram.')
    _warn(out, 'Mermaid relationships are conceptual; column mappings, SQL defaults and ordered constraints are not inferred.')
    return validate_diagram(out)


def _mermaid_escape(text):
    escapes = {'#': '#35;', '&': '#amp;', '"': '#quot;', "'": '#39;',
               '<': '#lt;', '>': '#gt;', '\n': '#10;', '\r': '#13;'}
    return ''.join(escapes.get(c, '#%d;' % ord(c) if ord(c) < 32 or 127 <= ord(c) <= 159 else c)
                   for c in text)


def _mermaid_attribute_type(dtype):
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9_()\[\]-]*', dtype):
        return dtype
    value = re.sub(r'\s*([(),])\s*', r'\1', dtype.strip())
    value = re.sub(r'\s+', '_', value).replace(',', '-').replace("'", '').replace('"', '')
    value = re.sub(r'[^A-Za-z0-9_()\[\]-]+', '_', value)
    value = re.sub(r'_+', '_', value)
    if not value:
        value = 'string'
    if not re.match(r'[A-Za-z]', value):
        value = 'type_' + value
    return value


def _mermaid_attribute_name(name, index, occupied):
    if re.fullmatch(r'[^\W\d][\w-]*', name):
        candidate = name
    else:
        candidate = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
        candidate = re.sub(r'[^A-Za-z0-9_-]+', '_', candidate)
        candidate = re.sub(r'_+', '_', candidate)
        if not candidate:
            candidate = 'column_%d' % (index + 1)
        if not re.match(r'[A-Za-z_]', candidate):
            candidate = '_' + candidate
    base, suffix = candidate, 2
    while candidate in occupied:
        candidate = '%s_%d' % (base, suffix)
        suffix += 1
    occupied.add(candidate)
    return candidate


def _mermaid_sql_type(dtype):
    # Reverse only known SQL spellings whose display forms cannot be SQL types.
    numeric = re.fullmatch(r'(decimal|numeric|float|double)\((\d+)-(\d+)\)', dtype, re.I)
    if numeric:
        return '%s(%s,%s)' % numeric.groups()
    if re.fullmatch(r'(?:tinyint|smallint|mediumint|int|integer|bigint|decimal|numeric|float|double|real)'
                    r'(?:\(\d+(?:-\d+)?\))?(?:_unsigned(?:_zerofill)?|_zerofill)', dtype, re.I):
        return dtype.replace('_', ' ').replace('-', ',')
    return dtype


def _mermaid_enum_values(dtype):
    if not re.match(r'^(enum|set)\s*\(', dtype, re.I):
        return None
    try:
        tokens = _tokens(dtype)
        if len(tokens) >= 4 and tokens[1].value == '(' and tokens[-1].value == ')':
            body = tokens[2:-1]
            if all(t.kind in ('string', 'quoted') if i % 2 == 0 else t.value == ','
                   for i, t in enumerate(body)) and len(body) % 2:
                return [t.value for t in body[::2]]
    except ValueError:
        pass
    return None


def _mermaid_comment_value(value):
    # Escape separators before joining so names and enum values round-trip even
    # when their rendered text contains the delimiters used by these comments.
    return ''.join(
        '#59;' if c == ';' else '#44;' if c == ',' else '#58;' if c == ':' else _mermaid_escape(c) for c in value)


def _mermaid_attribute_comment(col, name, values):
    parts = []
    if values is not None:
        parts.append(', '.join(_mermaid_comment_value(v) for v in values))
    if col['nullable'] is None:
        parts.append('nullability unknown')
    if name != col['name']:
        parts.append('name: ' + _mermaid_comment_value(col['name']))
    return ' "' + '; '.join(parts) + '"' if parts else ''


def export_mermaid(diagram):
    diagram = validate_diagram(diagram)
    ids = {t['id']: 'T%d' % (i + 1) for i, t in enumerate(diagram['tables'])}
    tables = {t['id']: t for t in diagram['tables']}
    lines, warnings = ['erDiagram'], list(diagram['warnings'])
    simplified_types = aliased_names = unknown_nullability = composite_unique = False
    if diagram.get('incomplete'):
        warnings.append('The diagram is marked incomplete; Mermaid export may omit schema objects.')
    for table in diagram['tables']:
        label = (table['schema'] + '.' if table['schema'] else '') + table['name']
        lines.append('    %s["%s"] {' % (ids[table['id']], _mermaid_escape(label)))
        occupied = set()
        foreign = {name for rel in diagram['relationships'] if rel['from_table'] == table['id']
                   for name in rel['from_columns']}
        unique_single = set()
        for index in table['indexes']:
            if index['unique'] and not index.get('primary'):
                if len(index['columns']) == 1:
                    unique_single.add(index['columns'][0])
                else:
                    composite_unique = True
        for i, col in enumerate(table['columns']):
            values = _mermaid_enum_values(col['data_type'])
            dtype = col['data_type'].split('(', 1)[0].strip() if values is not None else _mermaid_attribute_type(col['data_type'])
            name = _mermaid_attribute_name(col['name'], i, occupied)
            simplified_types = simplified_types or dtype != col['data_type']
            aliased_names = aliased_names or name != col['name']
            unknown_nullability = unknown_nullability or col['nullable'] is None
            keys = []
            if col['primary_key']:
                keys.append('PK')
            if col['name'] in foreign:
                keys.append('FK')
            if col['name'] in unique_single:
                keys.append('UK')
            lines.append('        %s%s %s%s%s' % (dtype, '?' if col['nullable'] is True else '', name,
                ' ' + ', '.join(keys) if keys else '', _mermaid_attribute_comment(col, name, values)))
        lines.append('    }')
    if simplified_types:
        warnings.append('SQL types are simplified for display; enum and set values appear in comments. JSON retains exact types.')
    if aliased_names:
        warnings.append('Aliased column names appear in comments.')
    if unknown_nullability:
        warnings.append('Unknown nullability appears in comments.')
    if composite_unique:
        warnings.append('Composite unique keys are not marked as UK in Mermaid; JSON retains the key definitions.')
    left = {v: k for k, v in _LEFT_CARD.items()}; right = {v: k for k, v in _RIGHT_CARD.items()}
    for rel in diagram['relationships']:
        card = rel.get('cardinality')
        if not card:
            source, target = tables[rel['from_table']], tables[rel['to_table']]
            def unique(table, cols):
                return bool(cols) and any(i['unique'] and set(i['columns']) == set(cols) for i in table['indexes'])
            mandatory = bool(rel['from_columns']) and all(c['nullable'] is False for c in source['columns']
                                                                         if c['name'] in rel['from_columns'])
            card = {'from': 'zero-or-one' if unique(source, rel['from_columns']) else 'zero-or-many',
                    'to': ('one' if mandatory else 'zero-or-one') if unique(target, rel['to_columns']) else 'zero-or-many'}
            if not unique(target, rel['to_columns']):
                warnings.append('A referenced key is not known unique; its Mermaid end is shown as zero or many.')
        lines.append('    %s %s%s%s %s : "%s"' % (ids[rel['from_table']], left[card['from']],
            '--' if rel.get('identifying') else '..', right[card['to']], ids[rel['to_table']], _mermaid_escape(rel['name'])))
    if any('cardinality' not in r for r in diagram['relationships']):
        warnings.append('Relationship cardinalities are inferred from declared keys and nullability, not verified table data.')
    warnings.append('Mermaid does not preserve database separation, ordered SQL key mappings or full DDL; use JSON for a lossless editable model.')
    return {'text': '\n'.join(lines) + '\n', 'warnings': list(dict.fromkeys(warnings))}


def import_diagram(format, text):
    _input_text(text)
    if format == 'sql': return import_sql(text)
    if format == 'mermaid': return import_mermaid(text)
    if format == 'json':
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            raise ValueError('Invalid diagram JSON.') from None
        return validate_diagram(value)
    raise ValueError('Choose SQL, Mermaid or JSON.')


def export_diagram(format, diagram):
    if format == 'sql': return export_sql(diagram)
    if format == 'mermaid': return export_mermaid(diagram)
    raise ValueError('Choose SQL or Mermaid export.')
