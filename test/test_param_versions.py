"""Parameter version catalogs: param_versions over the fixture catalog modules
(test/fixtures/param_versions, the format of the generated sysvar_history
modules), the config versions CLI and the /api/config/versions HTTP endpoints.
"""
import contextlib
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_chart_data import validate  # noqa: E402

import myflames  # noqa: E402
from myflames import cli, param_versions, ui  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'param_versions'
DOCS = Path(__file__).resolve().parent.parent / 'docs' / 'schemas'
CATALOG_SCHEMA = json.loads((DOCS / 'parameter-version-catalog-1.0.json').read_text(encoding='utf-8'))
COMPARE_SCHEMA = json.loads((DOCS / 'parameter-version-compare-1.0.json').read_text(encoding='utf-8'))
HISTORY_SCHEMA = json.loads((DOCS / 'parameter-history-1.0.json').read_text(encoding='utf-8'))


def _fixture_module(engine):
    name = 'sysvar_history_' + engine
    spec = importlib.util.spec_from_file_location('myflames.' + name, FIXTURES / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(module, engine=None):
    """Install a catalog module so `from . import sysvar_history_<engine>` finds it."""
    engine = engine or module.ENGINE
    name = 'sysvar_history_' + engine
    sys.modules['myflames.' + name] = module
    setattr(myflames, name, module)
    return module


def _uninstall(engine, block=False):
    name = 'sysvar_history_' + engine
    if block:
        # None in sys.modules makes the import fail even when the generated
        # module file exists: the missing-catalog path is always testable.
        sys.modules['myflames.' + name] = None
    else:
        sys.modules.pop('myflames.' + name, None)
    if getattr(myflames, name, None) is not None:
        delattr(myflames, name)


def _adhoc_module(engine):
    module = types.ModuleType('sysvar_history_' + engine)
    module.ENGINE = engine
    module.GENERATED = {'on': '2026-10-28'}
    module.VERSIONS = (('1.0.0', engine + '-1.0.0', 'GA', '2020-01-01', None, 'image'),
                       ('2.0.0', engine + '-2.0.0', 'GA', '2021-01-01', 0, 'image'))
    module.SKIPPED = ()
    module.FIELDS = param_versions.FIELDS
    module.HISTORY = {}
    return module


class Cataloged(unittest.TestCase):
    def setUp(self):
        _install(_fixture_module('mysql'))
        _install(_fixture_module('mariadb'))
        self.addCleanup(_uninstall, 'mysql')
        self.addCleanup(_uninstall, 'mariadb')


class LoadTest(Cataloged):
    def test_catalog_index_contract(self):
        index = param_versions.catalog_index()
        validate(index, CATALOG_SCHEMA)
        mysql, mariadb = index['engines']
        self.assertEqual((mysql['engine'], mysql['product']), ('mysql', 'MySQL'))
        self.assertTrue(mysql['available'])
        self.assertEqual([v['version'] for v in mysql['versions']],
                         ['8.0.11', '8.0.30', '8.4.0', '8.4.3', '9.0.0'])
        self.assertEqual([v['series'] for v in mysql['versions']],
                         ['8.0', '8.0', '8.4', '8.4', '9.0'])
        self.assertEqual(mysql['versions'][2]['maturity'], 'LTS')
        self.assertEqual(mysql['skipped'],
                         [{'version': '8.4.1', 'reason': mock.ANY}, {'version': '9.1.0', 'reason': mock.ANY}])
        self.assertIn('always_on', mysql['names'])
        self.assertEqual(mysql['names'], sorted(mysql['names']))
        self.assertTrue(any('Percona' in note for note in mysql['notes']))
        self.assertTrue(any('skipped' in note for note in mysql['notes']))
        self.assertEqual(mariadb['versions'][0]['maturity'], 'stable')

    def test_missing_catalog_module(self):
        _uninstall('mysql', block=True)
        with self.assertRaises(param_versions.CatalogError) as raised:
            param_versions.load('mysql')
        self.assertIn('not available', str(raised.exception))
        with self.assertRaises(param_versions.CatalogError):
            param_versions.compare('mysql', '8.0.30', '8.4.0')
        with self.assertRaises(param_versions.CatalogError):
            param_versions.history('mysql', 'always_on')
        index = param_versions.catalog_index()
        validate(index, CATALOG_SCHEMA)
        mysql, mariadb = index['engines']
        self.assertFalse(mysql['available'])
        self.assertEqual(mysql['versions'], [])
        self.assertTrue(any('not available' in note for note in mysql['notes']))
        self.assertTrue(mariadb['available'])

    def test_load_rejects_unknown_engine_and_bad_modules(self):
        with self.assertRaises(ValueError) as raised:
            param_versions.load('postgres')
        self.assertEqual(str(raised.exception), 'Choose engine mysql or mariadb.')
        bad = _adhoc_module('mysql')
        bad.FIELDS = ('scope',)
        with self.assertRaises(param_versions.CatalogError):
            param_versions.load('mysql', module=bad)
        wrong = _adhoc_module('mariadb')
        with self.assertRaises(param_versions.CatalogError):
            param_versions.load('mysql', module=wrong)

    def test_resolve_version(self):
        self.assertEqual(param_versions.resolve_version('mysql', '8.0.30'), '8.0.30')
        self.assertEqual(param_versions.resolve_version('mysql', 'mysql-8.0.30'), '8.0.30')
        self.assertEqual(param_versions.resolve_version('mariadb', '11.4.0-MariaDB-log'), '11.4.0')
        self.assertIsNone(param_versions.resolve_version('mysql', '8.4.6-6'))  # not cataloged
        self.assertIsNone(param_versions.resolve_version('mysql', '11.4.5-MariaDB-log'))
        self.assertIsNone(param_versions.resolve_version('mysql', '9.1.0'))  # skipped, not cataloged
        self.assertIsNone(param_versions.resolve_version('mysql', '8.4'))
        self.assertIsNone(param_versions.resolve_version('mysql', 'no version here'))
        self.assertIsNone(param_versions.resolve_version('mysql', 8030))


class CompareTest(Cataloged):
    def compare(self, source, target, engine='mysql'):
        return param_versions.compare(engine, source, target)

    def by_name(self, report):
        return dict((change['name'], change) for change in report['changes'])

    def test_upgrade_kinds_and_contract(self):
        report = self.compare('8.0.30', '8.4.0')
        validate(report, COMPARE_SCHEMA)
        self.assertEqual(report['schema'], 'parameter-version-compare-1.0')
        self.assertEqual(report['direction'], 'upgrade')
        self.assertEqual(report['from'], '8.0.30')
        self.assertEqual(report['to'], '8.4.0')
        kinds = self.by_name(report)
        self.assertEqual(kinds['added_later']['kinds'], ['added'])
        self.assertEqual(kinds['removed_var']['kinds'], ['removed'])
        self.assertEqual(kinds['readded_var']['kinds'], ['removed'])
        self.assertEqual(kinds['cache_size']['kinds'], ['default'])
        self.assertEqual(kinds['legacy_var']['kinds'], ['undeprecated'])
        self.assertEqual(kinds['mode_var']['kinds'], ['values'])
        self.assertEqual(kinds['workers']['kinds'], ['dynamic'])
        self.assertEqual(kinds['counter_var']['kinds'], ['type'])
        counts = report['counts']
        self.assertEqual(counts['changed'], len(report['changes']))
        catalog = param_versions.load('mysql')
        present = [n for n in catalog.names
                   if catalog.row(n, 1) is not None or catalog.row(n, 2) is not None]
        self.assertEqual(counts['changed'] + counts['unchanged'], len(present))
        self.assertNotIn('always_on', kinds)
        self.assertNotIn('log_scope', kinds)  # changes at 8.4.3, outside this range

    def test_downgrade_flips_the_kinds(self):
        report = self.compare('8.4.0', '8.0.30')
        validate(report, COMPARE_SCHEMA)
        self.assertEqual(report['direction'], 'downgrade')
        kinds = self.by_name(report)
        self.assertEqual(kinds['added_later']['kinds'], ['removed'])
        self.assertEqual(kinds['removed_var']['kinds'], ['added'])
        self.assertEqual(kinds['legacy_var']['kinds'], ['deprecated'])
        # Every difference of a downgrade was made on From's line: no since,
        # and the sentence names the release that made it.
        deprecated = kinds['legacy_var']
        self.assertEqual(deprecated['since'], {'deprecated': None})
        self.assertEqual(deprecated['text'], ['8.4.0 no longer marks it deprecated; 8.0.30 still does.'])
        self.assertEqual(kinds['cache_size']['text'], ['8.4.0 changed the default from 400 to 800; 8.0.30 keeps 400.'])
        self.assertEqual(kinds['removed_var']['text'], ['8.4.0 removed it; 8.0.30 still has it.'])

    def test_state_value_empty_string_and_name_case(self):
        report = self.compare('8.0.30', '8.4.0')
        self.assertNotIn('session_state', self.by_name(report))
        self.assertIn('an empty string', self.by_name(report)['empty_default']['text'][0])
        history = param_versions.history('mysql', '  ALWAYS_ON ')
        self.assertTrue(history['found'])
        self.assertEqual(history['name'], 'always_on')

    def test_history_summary_keeps_its_facts_for_long_names(self):
        summary = param_versions.history('mysql', 'group_replication_a_very_long_parameter_name_for_summaries')['summary']
        self.assertLessEqual(len(summary), 120)
        self.assertFalse(summary.endswith('...'))
        self.assertRegex(summary, r'\d+ changes?\.$|no changes\.$')

    def test_source_check_wording(self):
        changes = self.by_name(self.compare('8.0.11', '8.4.0'))
        # Arrives deprecated: both said.
        self.assertEqual(changes['born_deprecated']['kinds'], ['added', 'deprecated'])
        self.assertEqual(changes['born_deprecated']['text'], ['Added in 8.4.0.', 'Deprecated in 8.4.0.'])
        # The suggested replacement is itself deprecated at To.
        self.assertEqual(changes['older_name']['text'],
                         ['Deprecated in 8.0.30; use born_deprecated instead (born_deprecated is itself deprecated since 8.4.0).'])
        # A removed parameter names its successor when To has it.
        self.assertEqual(changes['gone_var']['text'], ['Removed in 8.4.0; use always_on instead.'])
        # An ON/OFF boolean declared again as an enum, and a maximum rounded to its block size, are no change.
        self.assertNotIn('switch_var', changes)
        self.assertNotIn('rounded_var', changes)
        # utf8 printed under its full name.
        self.assertEqual(changes['charset_var']['text'],
                         ['The default reads utf8mb3 instead of utf8 in 8.4.0: the same character set under its full name.'])

    def test_second_review_rules(self):
        up = self.by_name(self.compare('8.0.11', '8.4.0'))
        # A boolean that gains a third value is a type and values change.
        self.assertEqual(up['enum_grown']['kinds'], ['type', 'values'])
        # Removed and added back on To's line: said once, after the kind sentences.
        self.assertEqual(up['comeback_var']['text'][-1], 'It was removed in 8.0.30 and added again in 8.4.0.')
        down = self.by_name(self.compare('8.4.0', '8.0.30'))
        # From does not have it: the deprecation is To's own, never "no longer deprecated".
        self.assertEqual(down['gone_var']['text'], [
            '8.4.0 removed it; 8.0.30 still has it.',
            'Deprecated since 8.0.11 (the first cataloged release) or earlier; use always_on instead.'])
        self.assertEqual(down['charset_var']['text'],
                         ['8.4.0 reads the default as utf8mb3, 8.0.30 as utf8: the same character set under two names.'])

    def test_a_patch_of_from_series_with_the_same_change_is_named(self):
        change = self.by_name(self.compare('8.4.0', '9.0.0'))['twin_var']
        self.assertEqual(change['since'], {'default': '9.0.0'})
        self.assertEqual(change['text'], ['Default changed from a to b in 9.0.0 (8.4.1 or 8.4.3 on the 8.4 line; 8.4.1 could not be read).'])

    def test_history_default_rule_and_aliases(self):
        history = param_versions.history('mysql', 'host_sized')
        validate(history, HISTORY_SCHEMA)
        self.assertEqual(history['default_rule'], 'The server sets it at startup to twice the number of CPUs.')
        self.assertEqual(param_versions.history('mysql', 'real_var')['aliases'], ['old_alias'])
        self.assertIsNone(param_versions.history('mysql', 'always_on')['default_rule'])

    def test_change_on_the_from_line_only(self):
        # 8.4.3 is a patch of 8.4; 9.0.0 follows 8.4.0 and never got its change.
        report = self.compare('8.4.3', '9.0.0')
        validate(report, COMPARE_SCHEMA)
        change = self.by_name(report)['branch_var']
        self.assertEqual(change['since'], {'default': None})
        self.assertEqual(change['text'], ['8.4.3 (or 8.4.1, which could not be read) changed the default from 1 to 2; 9.0.0 keeps 1.'])
        # The other way round the change is on To's line.
        change = self.by_name(self.compare('9.0.0', '8.4.3'))['branch_var']
        self.assertEqual(change['since'], {'default': '8.4.3'})
        self.assertEqual(change['text'], ['Default changed from 1 to 2 in 8.4.1 or 8.4.3 (8.4.1 could not be read).'])

    def test_same_version(self):
        report = self.compare('8.4.0', '8.4.0')
        self.assertEqual(report['direction'], 'same')
        self.assertEqual(report['changes'], [])
        self.assertEqual(report['counts']['changed'], 0)
        self.assertGreater(report['counts']['unchanged'], 0)
        validate(report, COMPARE_SCHEMA)

    def test_since_walks_the_lineage(self):
        report = self.compare('8.0.11', '8.4.3')
        kinds = self.by_name(report)
        # The default at 8.4.3 holds since 8.0.30: the walk passes 8.4.0 and
        # 8.0.30 and stops at 8.0.11, before which the value differed.
        self.assertEqual(kinds['buffer_size']['since'], {'default': '8.0.30'})
        self.assertEqual(kinds['buffer_size']['text'],
                         ['Default changed from 100 to 200 in 8.0.30.'])
        # A default change at a series boundary.
        self.assertEqual(kinds['cache_size']['since'], {'default': '8.4.0'})
        self.assertEqual(kinds['log_scope']['since'], {'scope': '8.4.3'})
        # Removed in 8.4.0: at 8.4.3 the absence holds since 8.4.0.
        self.assertEqual(kinds['removed_var']['since'], {'removed': '8.4.0'})
        self.assertEqual(kinds['removed_var']['text'], ['Removed in 8.4.0.'])

    def test_unknown_default_is_never_a_default_change(self):
        report = self.compare('8.0.30', '8.4.0')
        change = self.by_name(report)['mystery_var']
        self.assertEqual(change['kinds'], ['dynamic'])
        self.assertEqual(len(change['text']), 2)
        self.assertIn('not compared', change['text'][1])
        self.assertIn('unknown in 8.4.0', change['text'][1])
        self.assertTrue(any('not compared' in note for note in report['notes']))
        # Unknown on both sides is no difference at all.
        report = self.compare('8.4.0', '8.4.3')
        self.assertNotIn('mystery_var', self.by_name(report))
        self.assertFalse(any('not compared' in note for note in report['notes']))

    def test_host_dependent_default_says_so(self):
        report = self.compare('8.0.30', '8.4.0')
        change = self.by_name(report)['host_sized']
        self.assertEqual(change['kinds'], ['default'])
        self.assertEqual(change['text'][0], 'From 8.4.0 the server computes the default at startup; before, it was 128. '
                         'The server sets it at startup to twice the number of CPUs (a lab server got 256).')
        # Host-dependent on both sides: the sampled numbers are not compared.
        self.assertNotIn('host_both', self.by_name(report))
        # A value that names the release (version, build_id) is not a default.
        self.assertNotIn('version', self.by_name(report))
        # A default that follows the build's paths is not a default change.
        self.assertNotIn('plugin_dir', self.by_name(report))

    def test_build_only_name_is_never_added_or_removed(self):
        report = self.compare('8.0.30', '8.4.0')
        self.assertNotIn('numa_only', self.by_name(report))
        self.assertEqual(report['counts']['removed'], len([c for c in report['changes'] if 'removed' in c['kinds']]))
        self.assertTrue(any(note.startswith('numa_only: Only builds with NUMA support') for note in report['notes']))
        history = param_versions.history('mysql', 'numa_only')
        validate(history, HISTORY_SCHEMA)
        self.assertEqual(history['build_only'], 'Only builds with NUMA support have it.')
        self.assertFalse(any('removed' in event['kinds'] for event in history['events']))
        self.assertIn('Only builds with NUMA support have it.', history['notes'])
        self.assertIsNone(param_versions.history('mysql', 'always_on')['build_only'])

    def test_summary_fits_120_characters(self):
        for source, target in (('8.0.11', '9.0.0'), ('9.0.0', '8.0.11'), ('8.4.0', '8.4.0'),
                               ('8.0.30', '8.4.0')):
            with self.subTest(source=source, target=target):
                self.assertLessEqual(len(self.compare(source, target)['summary']), 120)

    def test_uncataloged_version_is_a_clear_error(self):
        for text in ('8.9.9', '9.1.0', 'eight'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as raised:
                    self.compare('8.0.30', text)
                self.assertIn('not a cataloged MySQL release', str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            self.compare('8.0.30', '')
        self.assertIn('Choose a cataloged version', str(raised.exception))

    def test_plugin_variable_facts(self):
        report = self.compare('8.0.11', '8.4.3')
        self.assertNotIn('group_replication_var', self.by_name(report))
        history = param_versions.history('mysql', 'group_replication_var')
        self.assertEqual(history['current']['plugin'], 'group_replication')


class HistoryTest(Cataloged):
    def test_present_since_the_first_cataloged_release(self):
        report = param_versions.history('mysql', 'always_on')
        validate(report, HISTORY_SCHEMA)
        self.assertTrue(report['found'])
        self.assertEqual(report['first'], '8.0.11')
        self.assertEqual(report['latest'], '9.0.0')
        self.assertEqual(report['events'], [])
        self.assertIn('first cataloged release', report['summary'])
        self.assertNotIn('dded in', report['summary'])
        self.assertEqual(report['current']['default'], 'ON')
        self.assertEqual([s['series'] for s in report['series']], ['8.0', '8.4', '9.0'])

    def test_removed_and_readded_in_a_later_series(self):
        report = param_versions.history('mysql', 'readded_var')
        validate(report, HISTORY_SCHEMA)
        self.assertEqual([(e['version'], e['previous'], e['kinds']) for e in report['events']],
                         [('8.4.0', '8.0.30', ['removed']), ('9.0.0', '8.4.0', ['added'])])
        series = dict((s['series'], s['ranges']) for s in report['series'])
        self.assertEqual(series['8.4'], [{'from': '8.4.0', 'to': '8.4.3', 'facts': None}])
        self.assertEqual(series['9.0'][0]['facts']['default'], 'ON')

    def test_lineage_follows_tag_dates(self):
        # 9.0.0 follows 8.4.0: the 8.4.3 tag is dated after the 9.0.0 tag.
        events = dict((e['version'], e) for e in param_versions.history('mysql', 'workers')['events'])
        self.assertEqual(events['9.0.0']['previous'], '8.4.0')
        self.assertEqual(events['9.0.0']['kinds'], ['range'])
        self.assertIn('maximum 64 to 128', events['9.0.0']['text'][0])
        self.assertEqual(events['8.4.0']['previous'], '8.0.30')
        log_scope = param_versions.history('mysql', 'log_scope')['events']
        # The scope change released in 8.4.3 also appears at 9.0.0, whose
        # lineage predecessor 8.4.0 predates it.
        self.assertEqual([(e['version'], e['previous'], e['kinds']) for e in log_scope],
                         [('8.4.3', '8.4.0', ['scope']), ('9.0.0', '8.4.0', ['scope'])])

    def test_deprecated_alias_facts(self):
        report = param_versions.history('mysql', 'old_alias')
        current = report['current']
        self.assertEqual(current['alias_of'], 'real_var')
        self.assertEqual(current['replacement'], 'real_var')
        self.assertEqual(current['deprecated'], ['server', 'source'])
        self.assertEqual(report['events'], [])

    def test_removed_parameter_summary(self):
        report = param_versions.history('mysql', 'removed_var')
        self.assertEqual(report['latest'], '8.0.30')
        # current: the facts at the latest version where the parameter is present.
        self.assertEqual(report['current']['default'], '/tmp/log')
        self.assertIn('removed in 8.4.0', report['summary'])
        validate(report, HISTORY_SCHEMA)

    def test_unknown_name_suggests_close_names(self):
        report = param_versions.history('mysql', 'always-on')
        validate(report, HISTORY_SCHEMA)
        self.assertFalse(report['found'])
        self.assertEqual(report['suggestions'], ['always_on'])
        self.assertIn('No parameter named always-on is cataloged for MySQL.', report['summary'])
        report = param_versions.history('mysql', 'zzzzzz')
        self.assertEqual(report['suggestions'], [])
        self.assertLessEqual(len(report['suggestions']), 8)

    def test_history_summary_fits_120_characters(self):
        for name in param_versions.load('mysql').names:
            with self.subTest(name=name):
                self.assertLessEqual(len(param_versions.history('mysql', name)['summary']), 120)

    def test_bad_name_is_a_clear_error(self):
        for name in ('', 'x' * 65, 42):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    param_versions.history('mysql', name)


class CliTest(Cataloged):
    def setUp(self):
        super().setUp()
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': tmp.name, 'USERPROFILE': tmp.name,
                                           'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        self.temp = Path(tmp.name)

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['myflames'] + args), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
        return code, out.getvalue(), err.getvalue()

    def test_list_text_json_engine_filter_and_output(self):
        code, out, err = self.run_cli(['config', 'versions', 'list'])
        self.assertEqual(code, 0, err)
        self.assertIn('MySQL', out)
        self.assertIn('MariaDB', out)
        self.assertIn('8.0.11', out)
        code, out, err = self.run_cli(['config', 'versions', 'list', '--json'])
        self.assertEqual(code, 0, err)
        index = json.loads(out)
        validate(index, CATALOG_SCHEMA)
        code, out, err = self.run_cli(['config', 'versions', 'list', '--engine', 'mariadb', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual([e['engine'] for e in json.loads(out)['engines']], ['mariadb'])
        destination = self.temp / 'list.json'
        code, out, err = self.run_cli(['config', 'versions', 'list', '--json', '-o', str(destination)])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '')
        self.assertEqual(json.loads(destination.read_text())['schema'],
                         'parameter-version-catalog-1.0')

    def test_compare_infers_the_engine(self):
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0', '--json'])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        validate(report, COMPARE_SCHEMA)
        self.assertEqual(report['engine'], 'mysql')
        self.assertEqual(report['direction'], 'upgrade')
        code, out, err = self.run_cli(['config', 'versions', 'compare', '10.6.0', '10.11.0', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['engine'], 'mariadb')
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.0.30'])
        self.assertEqual(code, 0, err)
        self.assertIn('the same version', out)

    def test_compare_engine_inference_failures_name_the_flag(self):
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '10.11.0'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('--engine', err)
        _install(_adhoc_module('mysql'))
        _install(_adhoc_module('mariadb'))
        code, out, err = self.run_cli(['config', 'versions', 'compare', '1.0.0', '2.0.0'])
        self.assertEqual(code, 2)
        self.assertIn('--engine', err)

    def test_compare_bad_input_is_exit_2_with_empty_stdout(self):
        for args in (['config', 'versions', 'compare', '8.0.30', '8.9.9'],
                     ['config', 'versions', 'compare', '8.0.30'],
                     ['config', 'versions', 'compare', '8.0.30', '8.4.0', '--engine', 'postgres'],
                     ['config', 'versions', 'compare', '8.0.30', '8.4.0', '--kind', 'bogus'],
                     ['config', 'versions']):
            with self.subTest(args=args):
                code, out, err = self.run_cli(args)
                self.assertEqual(code, 2, (args, out))
                self.assertEqual(out, '')
                self.assertTrue(err)

    def test_compare_kind_and_name_filters(self):
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0',
                                       '--engine', 'mysql', '--kind', 'default', '--json'])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertTrue(report['changes'])
        for change in report['changes']:
            self.assertIn('default', change['kinds'])
        names = [change['name'] for change in report['changes']]
        self.assertIn('cache_size', names)
        self.assertIn('host_sized', names)
        self.assertNotIn('mystery_var', names)
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0',
                                       '--name', 'CACHE', '--json'])
        self.assertEqual(code, 0, err)
        filtered = json.loads(out)
        self.assertEqual([change['name'] for change in filtered['changes']], ['cache_size'])
        # counts and summary still describe the whole comparison; filter and shown say what is listed.
        validate(filtered, COMPARE_SCHEMA)
        self.assertEqual((filtered['filter'], filtered['shown']), ({'kinds': [], 'name': 'CACHE'}, 1))
        self.assertGreater(filtered['counts']['changed'], filtered['shown'])

    def test_columns_show_the_value_that_changed(self):
        code, out, err = self.run_cli(['config', 'versions', 'history', 'workers', '--engine', 'mysql', '--markdown'])
        self.assertEqual(code, 0, err)
        self.assertIn('| 9.0.0 | range | 1 to 64 | 1 to 128 |', out)
        self.assertIn('| 8.4.0 | dynamic | dynamic | static |', out)

    def test_compare_text_markdown_and_output_file(self):
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0'])
        self.assertEqual(code, 0, err)
        self.assertIn('cache_size', out)
        self.assertIn('Default changed from 400 to 800 in 8.4.0.', out)
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0', '--markdown'])
        self.assertEqual(code, 0, err)
        self.assertIn('| Parameter | Change | 8.0.30 | 8.4.0 | Since |', out)
        destination = self.temp / 'compare.json'
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0',
                                       '--json', '-o', str(destination)])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '')
        self.assertEqual(json.loads(destination.read_text())['schema'],
                         'parameter-version-compare-1.0')

    def test_history_every_engine_or_one(self):
        code, out, err = self.run_cli(['config', 'versions', 'history', 'always_on'])
        self.assertEqual(code, 0, err)
        self.assertIn('MySQL', out)
        self.assertIn('MariaDB', out)
        code, out, err = self.run_cli(['config', 'versions', 'history', 'always_on', '--json'])
        self.assertEqual(code, 0, err)
        reports = json.loads(out)
        self.assertEqual([r['engine'] for r in reports], ['mysql', 'mariadb'])
        for report in reports:
            validate(report, HISTORY_SCHEMA)
        code, out, err = self.run_cli(['config', 'versions', 'history', 'mariadb_only', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual([r['engine'] for r in json.loads(out)], ['mariadb'])
        code, out, err = self.run_cli(['config', 'versions', 'history', 'workers',
                                       '--engine', 'mysql', '--json'])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report['schema'], 'parameter-history-1.0')
        self.assertTrue(report['found'])
        validate(report, HISTORY_SCHEMA)

    def test_history_unknown_name_suggests_and_succeeds(self):
        code, out, err = self.run_cli(['config', 'versions', 'history', 'always-on',
                                       '--engine', 'mysql'])
        self.assertEqual(code, 0, err)
        self.assertIn('No parameter named always-on is cataloged for MySQL.', out)
        self.assertIn('Did you mean: always_on?', out)
        code, out, err = self.run_cli(['config', 'versions', 'history', 'zzzzzz'])
        self.assertEqual(code, 0, err)
        self.assertIn('No parameter named zzzzzz', out)

    def test_history_markdown_and_output_file(self):
        code, out, err = self.run_cli(['config', 'versions', 'history', 'workers',
                                       '--engine', 'mysql', '--markdown'])
        self.assertEqual(code, 0, err)
        self.assertIn('| Version | Change | Before | After |', out)
        destination = self.temp / 'history.json'
        code, out, err = self.run_cli(['config', 'versions', 'history', 'workers',
                                       '--engine', 'mysql', '--json', '-o', str(destination)])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '')
        validate(json.loads(destination.read_text()), HISTORY_SCHEMA)

    def test_missing_catalogs(self):
        _uninstall('mysql', block=True)
        _uninstall('mariadb', block=True)
        code, out, err = self.run_cli(['config', 'versions', 'list'])
        self.assertEqual(code, 0, err)
        self.assertIn('not available', out)
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('--engine', err)
        code, out, err = self.run_cli(['config', 'versions', 'compare', '8.0.30', '8.4.0',
                                       '--engine', 'mysql'])
        self.assertEqual(code, 2)
        self.assertIn('not available', err)
        code, out, err = self.run_cli(['config', 'versions', 'history', 'always_on'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')

    def test_help(self):
        for args in (['config', 'versions', '--help'], ['config', 'versions', 'list', '--help'],
                     ['config', 'versions', 'compare', '--help'],
                     ['config', 'versions', 'history', '--help']):
            with self.subTest(args=args):
                code, out, err = self.run_cli(args)
                self.assertEqual(code, 0, err)
                self.assertIn('Exit codes: 0', out)


class NamesTest(Cataloged):
    """param_versions.names: the variable names of one release for the editor."""

    def test_exact_release(self):
        result = param_versions.names('mysql', '8.0.30')
        self.assertEqual(result['schema_version'], 'param-names-1.0')
        self.assertEqual(result['engine'], 'mysql')
        self.assertEqual(result['requested'], '8.0.30')
        self.assertEqual(result['release'], '8.0.30')
        self.assertIs(result['exact'], True)
        by_name = dict((row['name'], row) for row in result['names'])
        # added_later arrives in 8.4.0; gone_var leaves there.
        self.assertNotIn('added_later', by_name)
        self.assertIn('gone_var', by_name)
        self.assertEqual(by_name['always_on'], {'name': 'always_on', 'scope': 'global',
                                                'type': 'boolean', 'dynamic': True, 'deprecated': False})
        self.assertIs(by_name['old_alias']['deprecated'], True)
        self.assertIs(by_name['mystery_var']['dynamic'], True)

    def test_uncataloged_patch_and_percona_suffix_fall_back_to_the_earlier_release(self):
        for text in ('8.0.31', '8.0.36-28', 'mysql-8.0.35'):
            with self.subTest(version=text):
                result = param_versions.names('mysql', text)
                self.assertEqual(result['requested'], text)
                self.assertEqual(result['release'], '8.0.30')
                self.assertIs(result['exact'], False)
                self.assertTrue(result['names'])
        # A 9.0 patch newer than the one cataloged release falls back to it.
        result = param_versions.names('mysql', '9.0.1')
        self.assertEqual(result['release'], '9.0.0')
        self.assertIs(result['exact'], False)
        by_name = dict((row['name'], row) for row in result['names'])
        self.assertNotIn('removed_var', by_name)
        self.assertIn('readded_var', by_name)

    def test_unknown_series_and_only_later_releases_answer_empty(self):
        for text in ('8.1.0', '8.0.5', 'not-a-version'):
            with self.subTest(version=text):
                result = param_versions.names('mysql', text)
                self.assertEqual(result['release'], None)
                self.assertIs(result['exact'], False)
                self.assertEqual(result['names'], [])

    def test_mariadb(self):
        result = param_versions.names('mariadb', '10.11.0')
        self.assertEqual(result['release'], '10.11.0')
        self.assertIs(result['exact'], True)
        by_name = dict((row['name'], row) for row in result['names'])
        self.assertIn('mariadb_only', by_name)
        self.assertIn('always_on', by_name)
        self.assertEqual(by_name['mariadb_only']['type'], 'string')
        result = param_versions.names('mariadb', '10.11.9-MariaDB-log')
        self.assertEqual(result['release'], '10.11.0')
        self.assertIs(result['exact'], False)
        result = param_versions.names('mariadb', '10.5.9')
        self.assertEqual(result['release'], None)
        self.assertEqual(result['names'], [])

    def test_dynamic_unknown_stays_none(self):
        # A catalog that does not record dynamic must not read as "not
        # dynamic"; only deprecated is always a bool.
        module = _adhoc_module('mysql')
        module.HISTORY = {
            'hushed_var': ((0, ('global', 'integer', None, '4', None, None, None, None, None, None, None, None)),),
            'loud_var': ((0, ('global', 'integer', True, '4', None, None, None, None, None, None, None, None)),),
            'fading_var': ((0, ('global', 'integer', True, '4', None, None, None, None, ('source',), None, None, None)),),
        }
        _install(module)
        self.addCleanup(_uninstall, 'mysql')
        result = param_versions.names('mysql', '2.0.0')
        self.assertEqual(result['release'], '2.0.0')
        by_name = dict((row['name'], row) for row in result['names'])
        self.assertIsNone(by_name['hushed_var']['dynamic'])
        self.assertIs(by_name['loud_var']['dynamic'], True)
        self.assertIs(by_name['fading_var']['deprecated'], True)
        self.assertIs(by_name['hushed_var']['deprecated'], False)

    def test_bad_version_is_a_plain_refusal(self):
        for version in (None, 8030, '', '   ', 'x' * 33):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'cataloged version'):
                param_versions.names('mysql', version)
        with self.assertRaisesRegex(ValueError, 'Choose engine'):
            param_versions.names('postgres', '8.0.30')


class HttpApiTest(Cataloged):
    """The /api/config/versions endpoints through the real POST handler, in
    the style of test_ui_http.py: memory streams, never a listening socket."""

    def setUp(self):
        super().setUp()
        self.server = types.SimpleNamespace(
            origin='http://127.0.0.1:8765', token='test-session-token',
            cookie_name='myflames_test', session_secret='test-cookie-secret')

    def post(self, path, payload=None, headers=None):
        body = json.dumps(payload if payload is not None else {}).encode('utf-8')
        values = {'Host': '127.0.0.1:8765', 'Origin': self.server.origin,
                  'Cookie': 'myflames_test=test-cookie-secret',
                  'X-Myflames-Token': self.server.token, 'Content-Type': 'application/json',
                  'Content-Length': str(len(body))}
        values.update(headers or {})
        handler = object.__new__(ui.WorkspaceHandler)
        handler.server = self.server
        handler.connection = mock.Mock()
        handler.path, handler.command = path, 'POST'
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'POST %s HTTP/1.1' % path
        handler.headers = Message()
        for key, value in values.items():
            if value is not None:
                handler.headers[key] = value
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.do_POST()
        head, content = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        return int(head.decode('ascii').split('\r\n', 1)[0].split()[1]), json.loads(content)

    def test_catalog_index(self):
        with mock.patch('myflames.connector.MySQLConnection',
                        side_effect=AssertionError('no connection')):
            status, result = self.post('/api/config/versions', {})
        self.assertEqual(status, 200)
        validate(result, CATALOG_SCHEMA)
        self.assertEqual([e['engine'] for e in result['engines']], ['mysql', 'mariadb'])

    def test_compare(self):
        status, result = self.post('/api/config/versions/compare',
                                   {'engine': 'mysql', 'from': '8.0.30', 'to': '8.4.0'})
        self.assertEqual(status, 200)
        validate(result, COMPARE_SCHEMA)
        self.assertEqual(result['direction'], 'upgrade')

    def test_compare_bad_requests_are_one_plain_sentence(self):
        cases = [
            ({'engine': 'postgres', 'from': '8.0.30', 'to': '8.4.0'},
             'Could not complete the request: Choose engine mysql or mariadb.'),
            ({'engine': 'mysql', 'from': '8.0.30', 'to': '8.9.9'},
             'Could not complete the request: 8.9.9 is not a cataloged MySQL release; '
             'list them with: myflames config versions list.'),
            ({'engine': 'mysql', 'from': '8.0.30'},
             'Could not complete the request: Choose a cataloged version for to, at most 32 characters.'),
            ({'engine': 'mysql', 'from': '8.0.30', 'to': 'x' * 33},
             'Could not complete the request: Choose a cataloged version for to, at most 32 characters.'),
            ({'engine': 'mysql', 'from': 8030, 'to': '8.4.0'},
             'Could not complete the request: Choose a cataloged version for from, at most 32 characters.'),
        ]
        for payload, error in cases:
            with self.subTest(payload=payload):
                status, result = self.post('/api/config/versions/compare', payload)
                self.assertEqual(status, 400)
                self.assertEqual(result['error'], error)

    def test_history(self):
        status, result = self.post('/api/config/versions/history',
                                   {'engine': 'mysql', 'name': 'workers'})
        self.assertEqual(status, 200)
        validate(result, HISTORY_SCHEMA)
        self.assertTrue(result['found'])
        status, result = self.post('/api/config/versions/history',
                                   {'engine': 'mysql', 'name': 'always-on'})
        self.assertEqual(status, 200)
        self.assertFalse(result['found'])
        self.assertEqual(result['suggestions'], ['always_on'])
        for payload in ({'engine': 'mysql', 'name': 'x' * 65}, {'engine': 'mysql', 'name': 42},
                        {'engine': 'mariadb'}, {'name': 'workers'}):
            with self.subTest(payload=payload):
                status, result = self.post('/api/config/versions/history', payload)
                self.assertEqual(status, 400)
                self.assertIn('error', result)

    def test_auth_runs_before_dispatch_and_unknown_path_is_404(self):
        for path in ('/api/config/versions', '/api/config/versions/compare',
                     '/api/config/versions/history', '/api/config/versions/names'):
            with self.subTest(path=path):
                status, result = self.post(path, {}, headers={'X-Myflames-Token': 'wrong'})
                self.assertEqual(status, 403)
        status, result = self.post('/api/config/versions/nope', {})
        self.assertEqual(status, 404)

    def test_no_connection_and_no_workspace_are_needed(self):
        with mock.patch('myflames.connector.MySQLConnection',
                        side_effect=AssertionError('no connection')) as connect, \
                mock.patch.object(ui, '_with_saved_password',
                                  side_effect=AssertionError('no password fill')):
            status, result = self.post('/api/config/versions/compare',
                                       {'engine': 'mariadb', 'from': '10.6.0', 'to': '10.11.0'})
            self.assertEqual(status, 200)
            connect.assert_not_called()

    def test_names(self):
        status, result = self.post('/api/config/versions/names', {'engine': 'mysql', 'version': '8.0.30'})
        self.assertEqual(status, 200)
        self.assertEqual(result['schema_version'], 'param-names-1.0')
        self.assertIs(result['exact'], True)
        self.assertEqual(result['release'], '8.0.30')
        self.assertIn({'name': 'always_on', 'scope': 'global', 'type': 'boolean',
                       'dynamic': True, 'deprecated': False}, result['names'])
        status, result = self.post('/api/config/versions/names', {'engine': 'mysql', 'version': '8.0.36-28'})
        self.assertEqual(status, 200)
        self.assertIs(result['exact'], False)
        self.assertEqual(result['release'], '8.0.30')
        status, result = self.post('/api/config/versions/names', {'engine': 'mysql', 'version': '8.1.0'})
        self.assertEqual(status, 200)
        self.assertEqual(result['release'], None)
        self.assertEqual(result['names'], [])
        status, result = self.post('/api/config/versions/names', {'engine': 'mariadb', 'version': '10.11.9-MariaDB'})
        self.assertEqual(status, 200)
        self.assertEqual(result['release'], '10.11.0')

    def test_names_bad_requests_are_one_plain_sentence(self):
        for payload in ({'engine': 'postgres', 'version': '8.0.30'}, {'engine': 'mysql'},
                        {'engine': 'mysql', 'version': 8030}, {'engine': 'mysql', 'version': 'x' * 33}):
            with self.subTest(payload=payload):
                status, result = self.post('/api/config/versions/names', payload)
                self.assertEqual(status, 400)
                self.assertIn('error', result)

    def test_names_needs_no_connection(self):
        with mock.patch('myflames.connector.MySQLConnection',
                        side_effect=AssertionError('no connection')) as connect:
            status, _ = self.post('/api/config/versions/names', {'engine': 'mysql', 'version': '8.4.3'})
            self.assertEqual(status, 200)
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
