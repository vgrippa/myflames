"""Read-only index advice for a server-owned statement digest.

Plan parsing stays in parser.py. SQL matching here is deliberately conservative:
ambiguous aliases, derived tables and unsupported expressions yield unknown.
"""
import re

from . import live
from .collectors import extract_table_references, _strip_comments_and_literals
from .connector import ConnectorError
from .dbops import redact
from .parser import _suggest_indexes, parse_explain
from .perf_common import Reader, ident as quote_ident, literal as sql_literal

_IDENT = r'(?:`(?:``|[^`])+`|[A-Za-z_$][A-Za-z0-9_$]*)'
_TARGET = r'(?P<first>' + _IDENT + r')(?:\s*\.\s*(?P<second>' + _IDENT + r'))?'
_ALL_ROWS = re.compile(r'^\s*SELECT\s+\*\s+FROM\s+' + _TARGET + r'(?:\s+(?:AS\s+)?(?P<alias>' + _IDENT + r'))?\s*;?\s*$', re.I)
_RESERVED = set('WHERE JOIN INNER OUTER LEFT RIGHT CROSS NATURAL STRAIGHT_JOIN ON USING GROUP ORDER HAVING LIMIT UNION EXCEPT INTERSECT FOR LOCK USE FORCE IGNORE PARTITION WINDOW'.split())
_SOURCE = re.compile(r'\b(?:FROM|JOIN)\s+' + _TARGET +
    r'(?:\s+(?:AS\s+)?(?P<alias>(?!(?:' + '|'.join(sorted(_RESERVED)) + r')\b)' + _IDENT + r'))?', re.I)
# The read runs without a click, so a sample that could only stall it is
# not explained automatically (planning evaluates constant expressions).
_STALLS = re.compile(r'\b(?:SLEEP|BENCHMARK)\s*\(', re.I)
_TAG = '/*myflames:index-hint*/ '
CAVEATS = ('Heuristic: check with Explain first; indexes add write cost. Ordinary InnoDB indexes can build online, '
           'but large tables need time and disk; engine and operation restrictions apply.')


def _unquote(name):
    return name[1:-1].replace('``', '`') if name.startswith('`') else name


def _comments(sql):
    # Keep literals intact: deleting one could make unsupported SQL match.
    return live.TOKEN.sub(lambda m: ' ' if m.group().startswith(('/*', '--', '#')) else m.group(), sql)


def _target(match, schema):
    first, second = match.group('first'), match.group('second')
    return (_unquote(first), _unquote(second)) if second else (schema or None, _unquote(first))


def _calls(value):
    for size, suffix in ((1000000000, 'B'), (1000000, 'M'), (1000, 'K')):
        if value >= size:
            return ('%.1f' % (value / size)).rstrip('0').rstrip('.') + ' ' + suffix
    return str(value)


def no_predicate(row):
    """Prove the narrow unfiltered SELECT-star case without any server I/O.

    Projection-only scans (covering indexes), COUNT, DISTINCT, LIMIT, joins,
    CTEs and subqueries are intentionally not declared unindexable.
    """
    text = row.get('digest_text') or ''
    if row.get('capture_problem') or live.capture_problem(text):
        return None
    match = _ALL_ROWS.fullmatch(_comments(text))
    if not match or (match.group('alias') or '').upper() in _RESERVED:
        return None
    schema, table = _target(match, row.get('schema'))
    label = '.'.join(part for part in (schema, table) if part)
    # Read from the digest alone: the name could be a view whose definition
    # filters, so the claim is stated for a base table.
    reason = ('No index can help if %s is a base table: this statement reads every row '
              '(no WHERE, JOIN condition, GROUP BY or ORDER BY), so no index can reduce the rows read.' % label)
    if row.get('rows_examined') == 0:
        # Digest counters aggregate executions; errors and instrumentation can
        # also produce zeros, so do not assert a historical table state as fact.
        reason += ' No rows were examined in the recorded calls; the table may have been empty.'
    reason += ' Its cost is how often it runs: %s calls.' % _calls(row.get('count') or 0)
    return {'status': 'no_predicate', 'reason': reason}


def _query(row):
    from .discover import digest_query
    query = digest_query(row)
    problem = row.get('capture_problem') or live.capture_problem(query['sql'])
    if not problem and query['names']:
        problem = ('The sample was cut off and the digest needs parameter values.' if row.get('sample_truncated')
                   else 'The normalized statement needs parameter values.')
    if problem:
        raise ValueError(problem)
    sql = live.bind_query(query['sql'])
    if _STALLS.search(live.TOKEN.sub(' ', sql)):
        raise ValueError('It calls SLEEP or BENCHMARK, so it is not explained automatically.')
    return sql


def _candidate_targets(row, root):
    sql = _query(row)
    pairs, notes = extract_table_references(sql, row.get('schema'), limit=20)
    # The text extractor cannot resolve nested scopes or CTE aliases. A
    # conservative refusal prevents DDL on a different table with the same name.
    masked = live.TOKEN.sub(' ', sql)
    if notes or re.search(r'\bWITH\b|\(\s*SELECT\b', masked, re.I):
        return []
    sources = {}
    clean = _strip_comments_and_literals(sql)
    quoted_spans = [m.span() for m in live.TOKEN.finditer(clean)]
    for match in _SOURCE.finditer(clean):
        if any(start <= match.start() < end for start, end in quoted_spans):
            continue
        pair = _target(match, row.get('schema'))
        if pair not in pairs:
            continue
        alias = match.group('alias')
        name = _unquote(alias) if alias and alias.upper() not in _RESERVED else pair[1]
        sources.setdefault(name, set()).add(pair)
    result = []
    for hint in _suggest_indexes(root):
        targets = sources.get(hint['table'], set())
        if len(targets) != 1:
            continue
        schema, table = next(iter(targets))
        columns = hint.get('columns') or []
        if not schema or not columns or not all(isinstance(x, str) and 0 < len(x) <= 64 and
                                               not any(c in x for c in '\x00\r\n')
                                               for x in [schema, table] + columns):
            continue
        result.append((hint, schema, table))
    return result


def suggest(row, plan=None, existing_indexes=None, error=None):
    """Pure result: no_predicate, suggestion, covered, not_explainable, unknown.

    existing_indexes is a list of {schema, table, name, columns, sub_parts,
    index_type, visible}. None means metadata was not read, never no indexes.
    """
    simple = no_predicate(row)
    if simple:
        return simple
    try:
        _query(row)
    except ValueError as exc:
        return {'status': 'not_explainable', 'reason': str(exc) + ' Open it in the Query editor.'}
    if error:
        return {'status': 'unknown', 'reason': 'Could not suggest an index: ' + str(error)}
    if plan is None:
        return {'status': 'unknown', 'reason': 'No estimated plan is available. Open it in the Query editor and use Explain.'}
    if existing_indexes is None:
        return {'status': 'unknown', 'reason': 'Existing indexes could not be checked. Open it in the Query editor and use Explain.'}
    covered = None
    for hint, schema, table in _candidate_targets(row, plan):
        columns = hint['columns']
        indexes = [i for i in existing_indexes if i['schema'] == schema and i['table'] == table]
        match = next((i for i in indexes if [c.lower() if c else None for c in i['columns'][:len(columns)]] ==
                      [c.lower() for c in columns] and not any((i.get('sub_parts') or [])[:len(columns)])
                      and i.get('index_type', 'BTREE').upper() == 'BTREE'), None)
        if match:
            if match.get('visible') is False:
                reason = ('Existing index %s leads with (%s), but it is invisible (MySQL) or ignored (MariaDB), so '
                          'the optimizer does not use it.' % (match['name'], ', '.join(columns)))
            else:
                reason = ('Existing index %s already leads with (%s); the optimizer did not use it, possibly because '
                          'of low selectivity, stale statistics, or a predicate that cannot use it (a function on '
                          'the column, a type or collation mismatch).' % (match['name'], ', '.join(columns)))
            covered = {'status': 'covered', 'table': table, 'columns': columns, 'index': match['name'], 'reason': reason}
            continue
        name = ('idx_%s_%s' % (table, '_'.join(columns)))[:64]
        used = {i['name'].lower() for i in indexes}
        base, suffix = name, 2
        while name.lower() in used:
            tail = '_%d' % suffix
            name = base[:64 - len(tail)] + tail
            suffix += 1
        # The parser may include DESC for a sort candidate; preserve direction.
        parts = [quote_ident(c) + (' DESC' if re.search(r'(?:`%s`|\b%s) DESC' %
                 (re.escape(c), re.escape(c)), hint['ddl'], re.I) else '') for c in columns]
        return {'status': 'suggestion', 'table': table, 'schema': schema, 'columns': columns,
                'ddl': 'CREATE INDEX %s ON %s.%s (%s);' %
                       (quote_ident(name), quote_ident(schema), quote_ident(table), ', '.join(parts)),
                'reason': hint['reason'] + '.', 'caveats': CAVEATS}
    return covered or {'status': 'unknown', 'reason': 'The estimated plan has no usable index suggestion. Open it in the Query editor and use Explain to inspect predicates, joins and sorting.'}


def read_suggestion(conn, digest, schema=None):
    """Resolve the digest server-side, then reuse the connector for EXPLAIN.

    Never accept client SQL, never run ANALYZE, never execute generated DDL.
    Each read uses the existing connection timeout; metadata is one scoped read.
    """
    from .discover import find_statement
    row = find_statement(conn, digest, schema)['statement']
    # find_statement falls back to a different schema when the row disappeared.
    if (row.get('schema') or None) != (schema or None):
        return {'status': 'unknown', 'reason': 'The statement is no longer recorded in this schema. Refresh Top statements.'}
    initial = suggest(row)
    if initial['status'] != 'unknown':
        return initial
    try:
        sql = _query(row)
        # Connector invocations use conn.database as their -D argument. The
        # digest's schema always wins over the connection's current database.
        previous = conn.database
        conn.database = row.get('schema') or None
        try:
            # As capture does: on standard input (never argv, where ps shows
            # the sample), inside a read-only transaction that is rolled back,
            # and the client stops at the first error, so the EXPLAIN never
            # runs when either read-only statement fails.
            raw = conn.run_write(_TAG + 'SET SESSION TRANSACTION READ ONLY;\n' + _TAG + 'START TRANSACTION READ ONLY;\n'
                                 + _TAG + 'EXPLAIN FORMAT=JSON ' + sql + '\n;\n' + _TAG + 'ROLLBACK;\n',
                                 extra_flags=['--raw'])
        finally:
            conn.database = previous
        plan = parse_explain(raw)
        targets = {(db, table) for _, db, table in _candidate_targets(row, plan)}
        if not targets:
            return suggest(row, plan, [])
        where = ' OR '.join(
            '(TABLE_SCHEMA = %s AND TABLE_NAME = %s AND (@@lower_case_table_names <> 0 OR '
            '(BINARY TABLE_SCHEMA = BINARY %s AND BINARY TABLE_NAME = BINARY %s)))' %
            (sql_literal(db), sql_literal(table), sql_literal(db), sql_literal(table))
            for db, table in sorted(targets))
        parts = Reader(conn, '/*myflames:index-hint*/ ').named(
            'SELECT STATISTICS.*, @@lower_case_table_names AS MYFLAMES_LOWER_CASE_TABLE_NAMES '
            'FROM information_schema.STATISTICS WHERE ' + where +
            ' ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX LIMIT 4097')
        if len(parts) > 4096:
            raise ValueError('The index metadata was truncated; inspect the table in the Query editor.')
        indexes = {}
        for part in parts:
            fold = str(part.get('MYFLAMES_LOWER_CASE_TABLE_NAMES')) in ('1', '2')
            actual = (part['TABLE_SCHEMA'], part['TABLE_NAME'])
            for db, table in targets:
                requested = (db, table)
                if actual != requested and not (fold and tuple(x.casefold() for x in actual) ==
                                                tuple(x.casefold() for x in requested)):
                    continue
                key = (db, table, part['INDEX_NAME'])
                index = indexes.setdefault(key, {'schema': db, 'table': table, 'name': key[2], 'columns': [],
                                                'sub_parts': [], 'index_type': part.get('INDEX_TYPE') or '',
                                                'visible': part.get('IS_VISIBLE') != 'NO' and part.get('IGNORED') != 'YES'})
                if str(part.get('SEQ_IN_INDEX')) != str(len(index['columns']) + 1):
                    raise ValueError('The index metadata has missing key parts; inspect the table in the Query editor.')
                index['columns'].append(part.get('COLUMN_NAME'))
                index['sub_parts'].append(part.get('SUB_PART'))
        return suggest(row, plan, list(indexes.values()))
    except (ValueError, ConnectorError) as exc:
        return suggest(row, error=redact(str(exc), getattr(conn, 'password', None)))
