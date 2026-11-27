"""Keep request-only passwords out of response, audit and file producers.

This source invariant follows ordinary Python import/assignment aliases and
registry handles. It deliberately does not claim whole-program information-flow
analysis of reflection or arbitrary helpers: changes to the three approved
readers still need security review.
"""
import ast
from collections import Counter
from pathlib import Path
import textwrap
import unittest


_PACKAGE = Path(__file__).resolve().parents[1] / 'myflames'
_APPROVED = Counter({
    ('ui.py', '_log_unexpected_error', 'request_secrets.values'): 1,
    ('report.py', '_Flight.__init__', 'request_secrets.values'): 1,
    ('report.py', 'run_single_flight', 'registry.values'): 1,
})


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return prefix + '.' + node.attr if prefix else None
    return None


def _registry_readers(source):
    """Return (qualified function, read kind, line) for registry value access.

    Bindings are conservative within each module: a name or stored attribute
    that holds a registry remains sensitive. This also recognizes a registry
    stored as ``self.secrets`` and later accessed as ``flight.secrets`` without
    requiring a general Python type checker.
    """
    tree = ast.parse(source)
    # _Flight.secrets also remains sensitive in a consumer module that only
    # receives the flight as an argument, without constructing it itself.
    bindings, registry_attributes = {}, {'secrets'}

    def kind(node):
        dotted = _dotted(node)
        if dotted in bindings:
            return bindings[dotted]
        if isinstance(node, ast.Attribute):
            owner = kind(node.value)
            if owner == 'module':
                return {'values': 'request_secrets.values', 'current': 'factory',
                        'Registry': 'factory', 'scope': 'scope'}.get(node.attr)
            if owner in ('registry', 'factory') and node.attr in ('values', '_values'):
                return 'registry.' + node.attr
            if node.attr in registry_attributes:
                return 'registry'
        if isinstance(node, ast.Call):
            call = kind(node.func)
            if call == 'factory':
                return 'registry'
            if call == 'scope':
                return 'scope_result'
            # Literal getattr is a common way to retain a bound reader.
            if (isinstance(node.func, ast.Name) and node.func.id == 'getattr'
                    and len(node.args) >= 2):
                # Str exists on 3.7; Constant replaces it on newer Python.
                member = getattr(node.args[1], 'value', getattr(node.args[1], 's', None))
                if isinstance(member, str):
                    return kind(ast.Attribute(value=node.args[0], attr=member, ctx=ast.Load()))
        return None

    def bind(target, value_kind):
        dotted = _dotted(target)
        if dotted and value_kind:
            bindings[dotted] = value_kind
            if value_kind == 'registry' and isinstance(target, ast.Attribute):
                registry_attributes.add(target.attr)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'myflames.request_secrets':
                    bindings[alias.asname or alias.name] = 'module'
        elif isinstance(node, ast.ImportFrom):
            if node.module in (None, 'myflames'):
                for alias in node.names:
                    if alias.name == 'request_secrets':
                        bindings[alias.asname or alias.name] = 'module'
            elif node.module in ('request_secrets', 'myflames.request_secrets'):
                for alias in node.names:
                    bindings[alias.asname or alias.name] = {
                        'values': 'request_secrets.values', 'Registry': 'factory',
                        'current': 'factory', 'scope': 'scope',
                        # A wildcard import cannot silently evade the inventory.
                        '*': 'wildcard',
                    }.get(alias.name)

    # Fixed point handles chained aliases and a class attribute used before
    # its constructor appears in source. The set grows monotonically.
    while True:
        before = (dict(bindings), set(registry_attributes))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    bind(target, kind(node.value))
            elif isinstance(node, ast.AnnAssign):
                bind(node.target, kind(node.value))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if kind(item.context_expr) == 'scope_result':
                        bind(item.optional_vars, 'registry')
        if before == (bindings, registry_attributes):
            break

    readers = []

    class Find(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_scope(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_ClassDef = visit_scope
        visit_FunctionDef = visit_scope
        visit_AsyncFunctionDef = visit_scope

        def visit(self, node):
            value_kind = kind(node)
            if (value_kind in ('request_secrets.values', 'registry.values', 'registry._values')
                    and not isinstance(getattr(node, 'ctx', None), ast.Store)):
                readers.append(('.'.join(self.scope), value_kind, node.lineno))
                # The getattr call and its derived attribute are one access.
                return
            return super().visit(node)

        def visit_ImportFrom(self, node):
            if (node.module in ('request_secrets', 'myflames.request_secrets')
                    and any(alias.name == '*' for alias in node.names)):
                readers.append(('.'.join(self.scope), 'wildcard', node.lineno))

    Find().visit(tree)
    return readers


class TestRequestSecretReaders(unittest.TestCase):
    def test_only_logger_and_shared_report_transfers_read_registry_values(self):
        actual = Counter()
        locations = []
        for path in sorted(_PACKAGE.rglob('*.py')):
            # The implementation owns its storage and the public values()
            # accessor. All consumers, including nested packages, are checked.
            if path == _PACKAGE / 'request_secrets.py':
                continue
            relative = path.relative_to(_PACKAGE).as_posix()
            for function, reader, line in _registry_readers(path.read_text(encoding='utf-8')):
                actual[(relative, function, reader)] += 1
                locations.append('%s:%d %s %s' % (relative, line, function, reader))
        self.assertEqual(actual, _APPROVED, '\n'.join(locations))

    def test_direct_and_aliased_readers_cannot_enter_other_sinks(self):
        cases = (
            ('from . import request_secrets', 'request_secrets.values()'),
            ('from . import request_secrets as secrets', 'secrets.values()'),
            ('import myflames.request_secrets', 'myflames.request_secrets.values()'),
            ('import myflames.request_secrets as secrets', 'secrets.values()'),
            ('from .request_secrets import values as read', 'read()'),
            ('from myflames.request_secrets import values', 'values()'),
            ('from . import request_secrets\nread = request_secrets.values', 'read()'),
            ('from . import request_secrets\nsecrets = request_secrets', 'secrets.values()'),
            ('from . import request_secrets', 'request_secrets.current().values()'),
            ('from . import request_secrets\nregistry = request_secrets.current()', 'registry.values()'),
            ('from .request_secrets import Registry as New\nregistry = New()', 'registry.values()'),
            ('from .request_secrets import Registry as New', 'New.values(registry)'),
            ('from . import request_secrets\nregistry = request_secrets.current()\nread = registry.values', 'read()'),
            ('from . import request_secrets', 'getattr(request_secrets, "values")()'),
            ('from . import request_secrets\nregistry = request_secrets.current()', 'registry._values'),
        )
        sinks = ('return {"passwords": %s}', 'audit.record(%s)', 'stream.write(str(%s))')
        for imports, value in cases:
            for sink in sinks:
                source = imports + '\ndef response():\n    ' + (sink % value) + '\n'
                with self.subTest(imports=imports, sink=sink):
                    readers = _registry_readers(source)
                    self.assertTrue(any(function == 'response' for function, _, _ in readers), readers)

    def test_scope_and_retained_shared_registry_readers_are_detected(self):
        source = textwrap.dedent('''
            from . import request_secrets
            class Flight:
                def __init__(self):
                    self.secrets = request_secrets.Registry()
            def response(flight):
                return flight.secrets.values()
            def audit():
                with request_secrets.scope() as registry:
                    record(registry.values())
        ''')
        self.assertEqual([(function, reader) for function, reader, _ in _registry_readers(source)],
                         [('response', 'registry.values'), ('audit', 'registry.values')])
        self.assertEqual(_registry_readers('def response(flight):\n    return flight.secrets.values()\n'),
                         [('response', 'registry.values', 2)])

    def test_wildcard_import_cannot_hide_the_reader_name(self):
        self.assertEqual(_registry_readers('from .request_secrets import *\n'), [('', 'wildcard', 1)])

    def test_function_and_count_are_pinned_even_inside_approved_modules(self):
        for function in ('api_response', '_log_unexpected_error'):
            source = 'from . import request_secrets\ndef %s():\n    return request_secrets.values()\n' % function
            changed = _APPROVED.copy()
            for scope, reader, _ in _registry_readers(source):
                changed[('ui.py', scope, reader)] += 1
            self.assertNotEqual(changed, _APPROVED)

    def test_ordinary_dictionary_values_are_not_registry_readers(self):
        self.assertEqual(_registry_readers('def response(data):\n    return list(data.values())\n'), [])


if __name__ == '__main__':
    unittest.main()
