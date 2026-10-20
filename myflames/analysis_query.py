"""Conservative read projections for workspace plan analysis.

This is not a general SQL rewriter. Only explicit supported DML shapes are
projected, then the capture engine's independent SELECT guard checks the whole
result. No statement in this module is executed. A projection cannot measure
write cost, triggers, constraints, locking or row changes.
"""
import re
from .live import TOKEN, TOKEN_NO_BACKSLASH, DASH_CONTROL, capture_problem, UNSAFE_SELECT_FUNCTION, _function_token

_IDENTIFIER = r'(?:`(?:``|[^`])+`|[A-Za-z_$][A-Za-z0-9_$]*)'
_TABLE = _IDENTIFIER + r'(?:\s*\.\s*' + _IDENTIFIER + r')?'
_TARGET = re.compile(r'^\s*' + _TABLE + r'(?:\s+(?:AS\s+)?' + _IDENTIFIER + r')?\s*$', re.I)
_WARNING = ('The original statement can change data. Myflames converted it to a read-only SELECT for analysis. '
            'This is a different query plan: UPDATE and DELETE measure target-row selection only; UPDATE assignment expressions and their subqueries are omitted. INSERT and REPLACE measure their SELECT source only. Write cost, triggers, constraints and locking are not reproduced. '
            'Review the SELECT before running analysis.')


def _scan(query):
    """Mask quoted/comment spans, preserving positions; track top-level words."""
    spans = [m.span() for m in TOKEN.finditer(query)]
    if spans != [m.span() for m in TOKEN_NO_BACKSLASH.finditer(query)]:
        raise ValueError('Ambiguous quote escaping; write a SELECT explicitly.')
    masked = TOKEN.sub(lambda m: ' ' * len(m.group()), query)
    if (re.search(r"['\"`]|/\*", masked) or DASH_CONTROL.search(masked) or
            ';' in masked or '\\' in masked or '\x00' in query or
            re.search(r'/\*(?:!|M!)', query, re.I)):
        raise ValueError('Unsupported SQL text; write one read-only SELECT explicitly.')
    depth, words = 0, []
    for match in re.finditer(r'[A-Za-z_][A-Za-z0-9_$]*|[()]', masked):
        word = match.group()
        if word == '(':
            depth += 1
            if depth > 64:
                raise ValueError('SQL nesting is too deep.')
        elif word == ')':
            depth -= 1
            if depth < 0:
                raise ValueError('Unbalanced SQL parentheses.')
        elif depth == 0:
            words.append((word.upper(), match.start(), match.end()))
    if depth:
        raise ValueError('Unbalanced SQL parentheses.')
    return masked, words


def prepare(query):
    """Return SELECT, conversion warning, or a refusal, without connecting."""
    result = {'sql': '', 'converted': False, 'warning': '', 'problem': ''}
    if not isinstance(query, str) or not query.strip() or len(query) > 200000:
        result['problem'] = 'Enter one statement of at most 200,000 characters.'
        return result
    query = query.strip()
    if query.endswith(';'):
        query = query[:-1].rstrip()
    try:
        masked, words = _scan(query)
        if not words:
            raise ValueError('No SQL statement was supplied.')
        # A pasted EXPLAIN wrapper selects a capture mode in the UI; never
        # carry it (or ANALYZE) into the statement sent to the capture engine.
        if words[0][0] == 'EXPLAIN':
            prefix = re.match(r'^\s*EXPLAIN\s+(?:ANALYZE\s+)?(?:FORMAT\s*=\s*(?:JSON|TREE|TRADITIONAL)\s+)?', masked, re.I)
            if not prefix:
                raise ValueError('Unsupported EXPLAIN wrapper.')
            query = query[prefix.end():].strip()
            masked, words = _scan(query)
            if not words:
                raise ValueError('EXPLAIN has no statement.')
        if any(word[0] == 'RETURNING' for word in words):
            raise ValueError('RETURNING cannot be converted. Write a read-only SELECT explicitly.')
        verb = words[0][0]
        if verb in ('SELECT', 'WITH'):
            sql = query
        elif verb == 'UPDATE':
            sets = [w for w in words[1:] if w[0] == 'SET']
            if len(sets) != 1:
                raise ValueError('Only a simple single-table UPDATE can be converted.')
            target = query[words[0][2]:sets[0][1]]
            if not _TARGET.fullmatch(target):
                raise ValueError('Only a simple single-table UPDATE can be converted.')
            suffix = next((w[1] for w in words if w[1] > sets[0][2] and w[0] in ('WHERE', 'ORDER', 'LIMIT')), len(query))
            assignments = query[sets[0][2]:suffix].strip()
            if not re.match(r'^' + _TABLE + r'\s*=\s*\S', assignments):
                raise ValueError('UPDATE must start with a column assignment.')
            sql = 'SELECT * FROM ' + target.strip() + (' ' + query[suffix:].strip() if suffix < len(query) else '')
        elif verb == 'DELETE':
            if len(words) < 2 or words[1][0] != 'FROM':
                raise ValueError('Only a simple single-table DELETE FROM can be converted.')
            suffix = next((w[1] for w in words[2:] if w[0] in ('WHERE', 'ORDER', 'LIMIT')), len(query))
            target = query[words[1][2]:suffix]
            if not _TARGET.fullmatch(target):
                raise ValueError('Only a simple single-table DELETE FROM can be converted.')
            sql = 'SELECT * FROM ' + target.strip() + (' ' + query[suffix:].strip() if suffix < len(query) else '')
        elif verb in ('INSERT', 'REPLACE'):
            # Keep only a top-level SELECT source. VALUES/SET forms and
            # duplicate-key updates do not have an unambiguous read projection.
            selected = next((w for w in words[1:] if w[0] == 'SELECT'), None)
            if not selected or any(w[0] in ('VALUES', 'VALUE', 'SET', 'WITH') for w in words[1:] if w[1] < selected[1]):
                raise ValueError('Only INSERT or REPLACE with a SELECT source can be converted.')
            prefix = query[words[0][2]:selected[1]].strip()
            # Validate the target and optional column list; do not use an
            # arbitrary SELECT found later in malformed SQL.
            if not re.fullmatch(r'(?:INTO\s+)?' + _TABLE + r'\s*(?:\(\s*' + _IDENTIFIER + r'(?:\s*,\s*' + _IDENTIFIER + r')*\s*\))?\s*', prefix, re.I):
                raise ValueError('Unsupported INSERT or REPLACE target.')
            sql = query[selected[1]:]
        else:
            raise ValueError('This statement cannot be safely converted. Write a read-only SELECT explicitly; analysis is blocked.')
        if UNSAFE_SELECT_FUNCTION.search(TOKEN.sub(_function_token, sql)):
            raise ValueError("State-changing or waiting functions cannot be analyzed.")
        problem = capture_problem(sql)
        if problem:
            raise ValueError(problem)
        converted = verb not in ('SELECT', 'WITH')
        result.update(sql=sql, converted=converted, warning=_WARNING if converted else '')
    except ValueError as exc:
        result['problem'] = str(exc)
    return result
