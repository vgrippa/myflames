"""Copy-only literal hiding for shared plans, never executable SQL rewriting.

With unknown SQL mode the union of both quote interpretations is hidden. This
can hide extra text in ambiguous SQL, but does not expose the other mode's data.
SQL comments are removed too: they can contain arbitrary private text.
"""
import re

_NUMBER = re.compile(r'(?:0[xX][0-9a-fA-F]+|0[bB][01]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)')
_WORD = re.compile(r'[\w$]+', re.UNICODE)
_COMPARISON = re.compile(r'<=>|<>|!=|<=|>=|[=<>]')


def _quoted_end(text, start, backslash_escapes, identifier=False):
    quote, i, size = text[start], start + 1, len(text)
    while i < size:
        if text[i] == quote:
            i += 1
            if i < size and text[i] == quote:
                i += 1
                continue
            break
        if text[i] == "\\" and (backslash_escapes or identifier):
            i += 2
        else:
            i += 1
    return min(i, size)


def _tokens(text, backslash_escapes, ansi_quotes, prose=False):
    """Yield SQL token spans without interpreting identifiers as numbers."""
    i, size = 0, len(text)
    while i < size:
        start = i
        if not prose and text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = size if end < 0 else end + 2
            yield 'comment', start, i
            continue
        if not prose and (text[i] == '#' or (text.startswith('--', i) and
                              (i + 2 == size or text[i + 2].isspace()))):
            end = text.find('\n', i)
            i = size if end < 0 else end
            yield 'comment', start, i
            continue
        quote = text[i]
        # Apostrophes within prose words are punctuation, not SQL quotes.
        if prose and quote == "'" and i > 0 and text[i - 1].isalnum():
            i += 1
            yield 'symbol', start, i
            continue
        if quote in "'\"`":
            identifier = quote == '`' or (quote == '"' and ansi_quotes)
            i = _quoted_end(text, i, backslash_escapes, identifier)
            yield 'identifier' if identifier else 'literal', start, i
            continue
        # X'AB', B'01', N'...', _utf8mb4'...' are single literal tokens.
        if (quote.isalpha() or quote == '_'):
            word = _WORD.match(text, i)
            i = word.end() if word else i + 1
            prefix = text[start:i]
            if i < size and text[i] == "'" and (prefix.lower() in ('x', 'b', 'n') or prefix.startswith('_')):
                end = _quoted_end(text, i, backslash_escapes)
                yield 'literal', start, end
                i = end
            else:
                yield 'word', start, i
            continue
        number = _NUMBER.match(text, i)
        if number:
            end = number.end()
            # 123column and column123 are identifiers, not numeric constants.
            if end == size or not (text[end].isalnum() or text[end] in '_$'):
                yield 'number', start, end
                i = end
                continue
        if quote.isdigit():
            word = _WORD.match(text, i)
            i = word.end() if word else i + 1
            yield 'word', start, i
        else:
            comparison = _COMPARISON.match(text, i)
            i = comparison.end() if comparison else i + 1
            if not quote.isspace():
                yield 'comparison' if comparison else 'symbol', start, i


def _spans(text, backslash_escapes, ansi_quotes):
    return [(start, end) for kind, start, end in _tokens(text, backslash_escapes, ansi_quotes)
            if kind in ('literal', 'number', 'comment')]


def _replace_spans(text, spans):
    """Merge the two quote-mode interpretations and replace their union."""
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        elif not merged or (start, end) != merged[-1]:
            merged.append((start, end))
    parts, previous = [], 0
    for start, end in merged:
        parts.extend((text[previous:start], '?'))
        previous = end
    parts.append(text[previous:])
    return ''.join(parts)


def hide_literals(text, backslash_escapes=None, ansi_quotes=False):
    """Replace SQL string/number/hex/bit/date values and comments with ``?``.

    Backticks and their doubled escapes are preserved. Double quotes are
    strings unless ``ansi_quotes`` is true. Signs remain SQL operators. An
    unknown backslash mode (the default for exports) hides the union of both
    interpretations. This is a sharing aid, not an anonymizer of identifiers.
    """
    if not isinstance(text, str):
        raise TypeError('SQL text must be a string.')
    modes = (True, False) if backslash_escapes is None else (backslash_escapes,)
    return _replace_spans(text, (span for mode in modes for span in _spans(text, mode, ansi_quotes)))


def hide_prose_literals(text, ansi_quotes=False):
    """Hide quoted values and predicate operands, retaining prose measurements.

    This is a display lexer, not a SQL or natural-language parser. Parentheses
    containing a comparison, LIKE, IN or BETWEEN are copied conditions: all
    their values (including function arguments) are hidden. Other parentheses
    keep measurements. Double quotes are strings unless ANSI_QUOTES is known;
    operand position alone cannot distinguish a string from an identifier.
    """
    spans = []
    for mode in (True, False):
        tokens = list(_tokens(text, mode, ansi_quotes, prose=True))
        words = [text[start:end].upper() for _, start, end in tokens]
        size = len(tokens)
        # Difference markers keep nested conditions and long IN lists linear.
        covered = [0] * (size + 1)
        stack, pairs = [], {}
        for i, (kind, _, _) in enumerate(tokens):
            word = words[i]
            if word == '(':
                stack.append([i, False])
            elif word == ')' and stack:
                opening, condition = stack.pop()
                pairs[opening] = i
                if condition:
                    covered[opening] += 1
                    covered[i + 1] -= 1
                    if stack:
                        stack[-1][1] = True
            elif stack and (kind == 'comparison' or
                            (kind == 'word' and word in ('LIKE', 'IN', 'BETWEEN'))):
                stack[-1][1] = True
        for opening, condition in stack:
            if condition:
                covered[opening] += 1
                covered[size] -= 1

        def operand_end(start):
            if start < size and words[start] in ('+', '-'):
                start += 1
            if start < size and words[start] == '(':
                return pairs.get(start, size - 1) + 1
            return min(start + 1, size)

        for i, (kind, _, _) in enumerate(tokens):
            if kind != 'word':
                continue
            if words[i] == 'IN' and i + 1 < size and words[i + 1] == '(':
                closing = pairs.get(i + 1, size - 1)
                covered[i + 1] += 1
                covered[closing + 1] -= 1
            elif words[i] == 'BETWEEN':
                end = operand_end(i + 1)
                covered[i + 1] += 1
                covered[end] -= 1
                if end < size and words[end] == 'AND':
                    upper_end = operand_end(end + 1)
                    covered[end + 1] += 1
                    covered[upper_end] -= 1

        depth = 0
        for i, (kind, start, end) in enumerate(tokens):
            depth += covered[i]
            previous = i - 1
            if previous >= 0 and words[previous] in ('+', '-'):
                previous -= 1
            following = i + 1
            if kind in ('comment', 'literal'):
                spans.append((start, end))
            elif kind == 'number' and (depth or
                    (previous >= 0 and (tokens[previous][0] == 'comparison' or words[previous] == 'LIKE')) or
                    (following < size and tokens[following][0] == 'comparison')):
                spans.append((start, end))
    return _replace_spans(text, spans)


# Raw-plan fields, including MySQL format 1/2 and MariaDB expressions. Numeric
# counters/costs and identifier fields are deliberately never rewritten.
LITERAL_FIELDS = frozenset((
    'operation', 'condition', 'attached_condition', 'index_condition',
    'pushed_index_condition', 'having_condition', 'const_condition',
    'hash_condition', 'ranges', 'range', 'sort_key', 'sort_fields',
    'grouping_operation', 'expression', 'cache', 'query', 'query_text',
    'query_sql', 'sql',
))


def hide_plan_literals(value, field=''):
    """Copy raw plan data, hiding only literal-bearing expression fields."""
    if isinstance(value, dict):
        return {key: hide_plan_literals(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [hide_plan_literals(item, field) for item in value]
    if isinstance(value, str) and field in LITERAL_FIELDS:
        return hide_literals(value)
    if isinstance(value, str) and field == 'message':
        return hide_prose_literals(value)
    return value

# Sidecars also carry generated prose and collected DDL. Hide values in those
# text channels while preserving their numeric metrics and metadata structure.
_LABEL_FIELDS = frozenset(('short_label', 'full_label', 'folded_label', 'node_labels',
                           'label', 'before_label', 'after_label'))
_SHARE_SQL_FIELDS = LITERAL_FIELDS | _LABEL_FIELDS | frozenset((
    'raw', 'beautified', 'ddl', 'query_text_lines',
))
_SHARE_PROSE_FIELDS = frozenset((
    'text', 'action', 'why', 'reason', 'note', 'rationale', 'executive_summary',
    'message', 'explanation', 'description', 'hint', 'summary', 'recommendation',
))


_SCHEMA_SQL_FIELDS = frozenset((
    'ddl', 'rest', 'default', 'column_default', 'default_expression',
    'expression', 'generated_expression', 'generation_expression',
    'check_clause', 'check_expression', 'partition_expression',
    'subpartition_expression', 'view_definition', 'routine_definition',
    'definition', 'body',
))


def _hide_schema_literals(value, field='', ddl_values=False):
    """Copy collected DDL/expressions without scanning names or type text."""
    if isinstance(value, dict):
        result = {key: _hide_schema_literals(item, key)
                  for key, item in value.items()}
        # Functional index parts put SQL in a field otherwise used for names.
        if value.get('expression') is True and isinstance(value.get('name'), str):
            result['name'] = hide_literals(value['name'])
        if isinstance(value.get('parts'), list) and isinstance(value.get('columns'), list):
            result['columns'] = []
            for i, name in enumerate(value['columns']):
                part = value['parts'][i] if i < len(value['parts']) else None
                if isinstance(name, str) and isinstance(part, dict) and part.get('expression') is True:
                    name = hide_literals(name)
                result['columns'].append(name)
        return result
    if isinstance(value, list):
        return [_hide_schema_literals(item, field, ddl_values) for item in value]
    # Unquoted structured DEFAULT strings are values, even without SQL quotes.
    if field.lower() in ('default', 'column_default') and value is not None:
        return '?'
    if isinstance(value, str) and (ddl_values or field.lower() in _SCHEMA_SQL_FIELDS):
        return hide_literals(value)
    return value


def plan_literal_labels(*roots):
    """Map original labels to canonical redacted labels, retaining identifiers."""
    from .parser import flatten_nodes, hide_parsed_plan_literals
    labels = {}
    for root in roots:
        redacted = hide_parsed_plan_literals(root)
        for old, new in zip(flatten_nodes(root), flatten_nodes(redacted)):
            for key in ('short_label', 'full_label', 'folded_label'):
                labels[old[key]] = new[key]
    return labels


def hide_analysis_literals(value, labels=None, field=''):
    """Copy a sidecar, preserving collected evidence while hiding SQL values.

    ``labels`` comes from the canonical label builders, so formatted numeric
    identifiers do not accidentally become placeholders. Numeric measurements,
    version strings and identifier fields are retained.
    """
    labels = labels or {}
    if field == 'schema' and isinstance(value, dict):
        # Legacy schema maps store DDL strings directly under each table key.
        return {key: _hide_schema_literals(item, ddl_values=True)
                for key, item in value.items()}
    if isinstance(value, dict):
        return {key: hide_analysis_literals(item, labels, key)
                for key, item in value.items()}
    if isinstance(value, list):
        return [hide_analysis_literals(item, labels, field) for item in value]
    if isinstance(value, str):
        if field in _LABEL_FIELDS and value in labels:
            return labels[value]
        if field in _SHARE_SQL_FIELDS:
            return hide_literals(value)
        if field in _SHARE_PROSE_FIELDS:
            return hide_prose_literals(value)
    return value
