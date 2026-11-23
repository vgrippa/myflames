"""Real local executables exercise inventory; selection fixtures require no I/O."""
import copy
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from _client_tools_fixture import private_client_dir

from myflames import client_tools as ct


@unittest.skipIf(os.name == 'nt', 'POSIX executable scripts and symlinks')
class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        with ct._PROBE_LOCK:
            ct._PROBE_CACHE.clear()

    def make(self, folder, name='mysql', version='8.4.13', banner=None, exit_code=0):
        folder = Path(folder)
        private_client_dir(folder)
        binary = folder / name
        banner = banner if banner is not None else '%s Ver %s' % (name, version)
        binary.write_text("#!/bin/sh\nprintf '%s\\n' '" + banner.replace("'", "'\\''") + "'\nexit %d\n" % exit_code)
        binary.chmod(0o700)
        return binary

    def discover(self, *folders, **kwargs):
        return ct.discover(path=os.pathsep.join(str(p) for p in folders), common_dirs=(), bundled=False, **kwargs)

    def settings_and_connector(self, folders, server, choices=None):
        """Compare Settings' pure selection with the query client's resolution."""
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(map(str, folders)),
                                          'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')), \
                mock.patch.dict(ct.connector._CLIENT_CHOICES,
                                dict({'mysql': None, 'mariadb': None}, **(choices or {})), clear=True), \
                mock.patch.dict(ct._FACTS, clear=True):
            choices = ct.connector.client_choices()
            discovered = ct.discover_choices(choices)
            keys, errors = ct.choice_keys(discovered, choices)
            self.assertEqual(errors, {'mysql': None, 'mariadb': None})
            shown = ct.select(discovered, server, keys)['client']
            conn = ct.connector.MySQLConnection(host='mixed-layout')
            ct.remember_server(conn._endpoint_key(), server['engine'], server['version'])
            with mock.patch.object(ct.managed_shell, 'installed_bin_dir',
                                   side_effect=AssertionError('must not read the managed Shell pointer')), \
                    mock.patch.object(ct.connector, 'checked_shell_program',
                                      side_effect=AssertionError('must not check Shell trust')):
                try:
                    executed = conn._resolve_binary()
                except ct.connector.ClientMissingError:
                    executed = None
        return (shown or {}).get('realpath'), executed

    def test_shell_first_and_mixed_chosen_folder_keep_query_selection_order(self):
        shell = self.make(self.root / 'shell/bin', 'mysqlsh', version='9.7.0')
        mysql = self.make(self.root / 'z-path/bin')
        self.make(self.root / 'a-chosen/bin')
        maria = self.make(self.root / 'a-chosen/bin', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        shown, executed = self.settings_and_connector(
            (shell.parent, mysql.parent), dict(engine='mysql', version='8.4.13'),
            choices={'mariadb': str(maria)})
        self.assertEqual(shown, str(mysql))
        self.assertEqual(executed, shown)

    def test_chosen_folder_installations_follow_highest_discovered_order(self):
        dump = self.make(self.root / 'dump/bin', 'mysqldump')
        self.make(dump.parent, 'mysqlpump')
        mysql = self.make(self.root / 'z-path/bin')
        self.make(self.root / 'a-chosen/bin')
        maria = self.make(self.root / 'a-chosen/bin', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        shown, executed = self.settings_and_connector(
            (dump.parent, mysql.parent), dict(engine='mysql', version='8.4.13'),
            choices={'mariadb': str(maria)})
        self.assertEqual(shown, str(mysql))
        self.assertEqual(executed, shown)

    def test_lifecycle_duplicate_path_ranks_match_settings_and_query_client(self):
        from myflames import lifecycle
        dump = self.make(self.root / 'dump/bin', 'mysqldump', version='8.0.45')
        mysql = self.make(self.root / 'z-path/bin')
        self.make(mysql.parent, 'mysqldump')
        self.make(self.root / 'a-chosen/bin')
        self.make(self.root / 'a-chosen/bin', 'mysqldump')
        maria = self.make(self.root / 'a-chosen/bin', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        folders = (dump.parent,) * 4 + (mysql.parent,)
        choices = {'mariadb': str(maria)}
        server = dict(engine='mysql', version='8.4.13')
        shown, executed = self.settings_and_connector(folders, server, choices)
        self.assertEqual(shown, str(mysql))
        self.assertEqual(executed, shown)
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(map(str, folders)),
                                          'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ()), \
                mock.patch.object(ct.connector, 'client_choices', return_value=choices):
            report = lifecycle.detect_tools(server=server)
        for role, expected in (('client', mysql), ('load', mysql), ('dump', mysql.parent / 'mysqldump')):
            with self.subTest(role=role):
                self.assertEqual(report['used'][role]['path'], str(expected))

    def test_equal_version_chosen_shell_follows_path_in_settings_maintenance_and_cluster(self):
        from myflames import cluster_shell, lifecycle
        shell = self.make(self.root / 'z-path/bin', 'mysqlsh', version='9.7.0')
        chosen = self.make(self.root / 'a-chosen/bin')
        self.make(chosen.parent, 'mysqlsh', version='9.7.0')
        choices = {'mysql': str(chosen)}
        server = dict(engine='mysql', version='8.4.13')
        with mock.patch.dict(os.environ, {'PATH': str(shell.parent), 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ()), \
                mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')), \
                mock.patch.object(ct.connector, 'client_choices', return_value=choices):
            settings = ct.select(ct.discover_choices(choices), server)['shell']['path']
            maintenance = lifecycle.detect_tools(server=server)['used']['shell']['path']
            cluster = cluster_shell.find_shell()['path']
        for surface, selected in (('Settings', settings), ('Maintenance', maintenance), ('Cluster', cluster)):
            with self.subTest(surface=surface):
                self.assertEqual(selected, str(shell))

    def test_shell_symlink_cannot_hide_query_client_in_full_discovery(self):
        mysql = self.make(self.root / 'z-client/bin')
        shell_folder = private_client_dir(self.root / 'a-shell/bin')
        (shell_folder / 'mysqlsh').symlink_to(mysql)
        with mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')):
            full = self.discover(shell_folder, mysql.parent)
            filtered = self.discover(shell_folder, mysql.parent, names=ct.QUERY_DISCOVERY_NAMES)
        self.assertEqual(full['shells'], [])
        for label, report in (('full', full), ('filtered', filtered)):
            with self.subTest(discovery=label):
                self.assertEqual([g['programs']['mysql']['realpath'] for g in report['installations']],
                                 [str(mysql)])
                self.assertEqual(ct.select(report, dict(engine='mysql', version='8.4.13'))['client']['path'],
                                 str(mysql))

    def test_mysqlsh_link_to_mysql_is_unavailable_in_settings_and_maintenance(self):
        from myflames import lifecycle
        mysql = self.make(self.root / 'client/bin', banner='mysql Ver 8.4.13 for test')
        aliases = private_client_dir(self.root / 'aliases/bin')
        shell_link = aliases / 'mysqlsh'
        shell_link.symlink_to(mysql)
        server = dict(engine='mysql', version='8.4.13')
        path = os.pathsep.join((str(aliases), str(mysql.parent)))
        with mock.patch.dict(os.environ, {'PATH': path, 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ()), \
                mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')), \
                mock.patch.object(ct.connector, 'client_choices', return_value={'mysql': None, 'mariadb': None}):
            discovered = self.discover(aliases, mysql.parent)
            selected = ct.select(discovered, server)
            self.assertEqual(discovered['shells'], [])
            self.assertIsNone(selected['shell'])
            self.assertEqual(selected['client']['realpath'], str(mysql))
            bad_copy = next(item for item in discovered['copies'] if item['name'] == 'mysqlsh')
            self.assertFalse(bad_copy['available'])
            self.assertTrue(bad_copy.get('error'))
            for facts in (None, server):
                with self.subTest(server=facts):
                    report = lifecycle.detect_tools(server=facts)
                    self.assertFalse(report['tools']['mysqlsh']['available'])
                    offers = lifecycle.offered_tools(server, report)
                    self.assertNotIn('mysqlsh', offers['dump'])
                    self.assertEqual(offers['upgrade'], 'native_subset')
                    if facts is not None:
                        self.assertIsNone(report['used']['shell'])

    def test_renamed_shell_binary_is_identified_by_its_version_banner(self):
        binary = self.make(self.root / 'real/bin', 'shell-launcher',
                           banner='mysqlsh Ver 9.7.0 for macos15 on arm64 - for MySQL 9.7.0')
        aliases = private_client_dir(self.root / 'aliases/bin')
        shell_link = aliases / 'mysqlsh'
        shell_link.symlink_to(binary)
        with mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')):
            report = self.discover(aliases)
        self.assertEqual([item['realpath'] for item in report['shells']], [str(binary)])
        self.assertEqual(ct.select(report, dict(engine='mysql', version='8.4.13'))['shell']['path'],
                         str(shell_link))

    def test_mysql_banner_cannot_become_shell_when_file_is_named_mysqlsh(self):
        binary = self.make(self.root / 'renamed/bin', 'mysqlsh', banner='mysql Ver 8.4.13 for test')
        with mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')):
            report = self.discover(binary.parent)
        self.assertEqual(report['shells'], [])
        self.assertFalse(report['copies'][0]['available'])

    def test_F1_query_client_is_the_client_settings_reports_when_a_dump_tool_comes_first(self):
        a = private_client_dir(self.root / 'a/bin')
        b, c = self.root / 'b/bin', self.root / 'c/bin'
        self.make(b)
        expected = self.make(c)
        (a / 'mysqldump').symlink_to(self.make(c, 'mysqldump'))
        for folder in (b, c):
            self.make(folder, 'mysqlsh', version='9.6.0')
        shown, executed = self.settings_and_connector((a, b, c), dict(engine='mysql', version='8.4.13'))
        self.assertEqual(shown, str(expected))
        self.assertEqual(executed, shown)

    def test_F1_own_engine_tools_without_a_client_never_borrow_the_other_engine(self):
        # Only a server with no tools of its own engine may borrow a query client.
        a, b, c = self.root / 'a/bin', self.root / 'b/bin', self.root / 'c/bin'
        self.make(a)
        self.make(c, version='8.0.45')
        self.make(b, 'mariadb-dump', banner='mariadb-dump Ver 10.19 Distrib 11.4.2-MariaDB')
        shown, executed = self.settings_and_connector((a, b, c), dict(engine='mariadb', version='11.4.2-MariaDB'))
        self.assertIsNone(shown)
        self.assertEqual(executed, shown)

    def test_choice_keys_engines_alias_bundle_and_missing(self):
        mysql = self.make(self.root / 'mysql')
        maria = self.make(self.root / 'maria', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        report = ct.discover(path=str(maria.parent), common_dirs=(), bundled=str(mysql.parent))
        link = self.root / 'stable'
        link.symlink_to(mysql.parent, target_is_directory=True)
        for value in ('bundled', str(link / 'mysql'), str(link)):
            keys, errors = ct.choice_keys(report, dict(mysql=value, mariadb=str(maria)))
            self.assertTrue(keys['mysql'].startswith('mysql:'))
            self.assertTrue(keys['mariadb'].startswith('mariadb:'))
            self.assertEqual(errors, dict(mysql=None, mariadb=None))
        keys, errors = ct.choice_keys(report, dict(mysql='/missing/mysql'))
        self.assertIsNone(keys['mysql'])
        self.assertEqual(errors['mysql'], 'The client tools chosen in Settings > Client tools are no longer at /missing/mysql.')

    def test_validate_choice_preserves_spelling_and_refuses_wrong_engine(self):
        mysql = self.make(self.root / 'mysql')
        maria = self.make(self.root / 'maria', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        link = self.root / 'stable'
        link.symlink_to(mysql.parent, target_is_directory=True)
        self.assertEqual(ct.validate_choice('mysql', str(link / 'mysql')), str(link / 'mysql'))
        self.assertEqual(ct.validate_choice('mysql', str(link)), str(link))
        self.assertEqual(ct.validate_choice('mariadb', str(maria)), str(maria))
        with mock.patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': str(mysql.parent)}):
            self.assertEqual(ct.validate_choice('mysql', str(link / 'mysql')), 'bundled')
            self.assertEqual(ct.validate_choice('mysql', 'bundled'), 'bundled')
        for engine, path in (('mariadb', str(mysql)), ('mysql', str(maria)), ('other', str(mysql)),
                             ('mysql', 'relative/mysql'), ('mysql', str(self.root / 'missing'))):
            with self.subTest(engine=engine, path=path), self.assertRaises(ValueError):
                ct.validate_choice(engine, path)
        mysql.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'changed by other users'):
            ct.validate_choice('mysql', str(mysql))

    def test_discover_choices_includes_off_path_folder_and_rejects_untrusted(self):
        mysql = self.make(self.root / 'offpath')
        self.make(mysql.parent, 'mysqldump')
        with mock.patch.dict(os.environ, {'PATH': '', 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch.object(ct.connector, 'bundled_client_dir', return_value=None), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()):
            report = ct.discover_choices({'mysql': str(mysql)})
            self.assertEqual(set(report['installations'][0]['programs']), {'mysql', 'mysqldump'})
            mysql.chmod(0o777)
            report = ct.discover_choices({'mysql': str(mysql)})
            self.assertEqual(report['installations'], [])

    def test_chosen_folder_with_path_separator_is_scanned_as_one_folder(self):
        mysql = self.make(self.root / ('x' + os.pathsep + 'y'))
        self.make(self.root / 'x', banner='mysql Ver 9.7.0')
        with mock.patch.dict(os.environ, {'PATH': '', 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()):
            report = ct.discover_choices({'mysql': str(mysql)})
        self.assertEqual(len(report['installations']), 1)
        self.assertEqual(report['installations'][0]['programs']['mysql']['path'], str(mysql))

    def test_homebrew_kegs_bundle_and_aliases(self):
        brew = self.root / 'prefix'
        keg = brew / 'Cellar/mysql@8.4/8.4.8/bin'
        old = brew / 'Cellar/mysql@8.0/8.0.45/bin'
        links = brew / 'bin'
        private_client_dir(links)
        for name in ('mysql', 'mysqldump'):
            (links / name).symlink_to(self.make(keg, name, '8.4.8'))
            self.make(old, name, '8.0.45')
        opt = brew / 'opt/mysql@8.0'
        private_client_dir(opt.parent)
        opt.symlink_to(old.parent)
        bundle = self.root / 'app/bin'
        for name in ('mysql', 'mysqldump'):
            self.make(bundle, name)
        alias = self.root / 'bundle-alias'
        alias.symlink_to(bundle)
        report = ct.discover(path=os.pathsep.join(map(str, (alias, links, opt / 'bin'))),
                             common_dirs=(), bundled=str(bundle))
        groups = report['installations']
        self.assertEqual(len(groups), 3)
        self.assertEqual([g['version'] for g in groups], ['8.4.8', '8.0.45', '8.4.13'])
        self.assertEqual([g['origin_label'] for g in groups],
                         ['Homebrew mysql@8.4', 'Homebrew mysql@8.0', 'Bundled with myflames'])
        self.assertEqual(groups[0]['folder'], str(keg))
        self.assertEqual(groups[0]['found_in'], str(links))
        self.assertEqual(set(groups[0]['programs']), {'mysql', 'mysqldump'})

    def test_version_formats_ignore_path_version(self):
        banners = (
            ('mysql', '/Applications/myflames 3.1.0/bin/mysql  Ver 8.4.13 for macos', '8.4.13'),
            ('mysqldump', 'mysqldump  Ver 10.13 Distrib 5.7.44, for Linux', '5.7.44'),
            ('mariadb', 'mariadb from 11.8.2-MariaDB, client 15.2 for Linux', '11.8.2'),
            ('mariadb-dump', 'mariadb-dump from 11.4.2-MariaDB, client 10.19 for Linux', '11.4.2'),
            ('mysql', 'mysql Ver 15.1 Distrib 10.11.14-MariaDB, for Linux', '10.11.14'))
        for i, (name, banner, expected) in enumerate(banners):
            folder = self.root / str(i)
            self.make(folder, name, banner=banner)
            with self.subTest(banner=banner):
                group = self.discover(folder)['installations'][0]
                self.assertEqual(group['version'], expected)
                self.assertEqual(group['engine'], 'mariadb' if 'MariaDB' in banner else 'mysql')

    def test_failed_and_unrecognized_probes_are_unavailable(self):
        self.make(self.root, exit_code=2)
        self.make(self.root, 'mysqldump', banner='no version here')
        report = self.discover(self.root)
        self.assertEqual(report['installations'], [])
        self.assertEqual(len(report['skipped']), 2)
        self.assertTrue(all(not p['available'] and p['error'] for p in report['skipped']))

    def test_failure_and_timeout_are_not_cached(self):
        self.make(self.root, exit_code=1)
        with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            self.discover(self.root)
            self.discover(self.root)
            self.assertEqual(run.call_count, 2)
        with mock.patch.object(ct.subprocess, 'run', side_effect=subprocess.TimeoutExpired('mysql', 8)) as run:
            self.discover(self.root)
            report = self.discover(self.root)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(report['skipped'][0]['error'], 'TimeoutExpired')

    def test_unrecognized_numeric_banner_is_not_cached(self):
        self.make(self.root, banner='/Applications/myflames 3.1.0/mysql: no version')
        with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            for _ in range(2):
                report = self.discover(self.root)
                self.assertEqual(report['installations'], [])
                self.assertFalse(report['skipped'][0]['available'])
            self.assertEqual(run.call_count, 2)

    def test_trusted_common_nonclient_name_is_probed(self):
        binary = self.make(self.root / 'bin', 'mysqldump')
        report = ct.discover(path='', common_dirs=(str(binary.parent),), bundled=False)
        self.assertEqual(report['skipped'], [])
        self.assertIn('mysqldump', report['installations'][0]['programs'])

    def test_windows_snapshot_uses_exe_suffix_without_probing(self):
        binary = self.make(self.root, 'mysql.exe')
        self.make(self.root, 'mysql')
        # Windows root restrictions are exercised by TestWindowsClientTrust;
        # this POSIX fixture checks inventory suffixes without a subprocess.
        with mock.patch.object(ct.os, 'name', 'nt'), \
                mock.patch.object(ct.connector, '_check_client_trust'):
            candidates = ct.snapshot(str(self.root), (), False)
        self.assertEqual([c['path'] for c in candidates], [str(binary)])

    def test_untrusted_alias_cannot_hide_trusted_client_in_either_path_order(self):
        mysql = self.make(self.root / 'trusted')
        for later_maria in (False, True):
            with self.subTest(later_maria=later_maria):
                planted = self.root / ('evil-' + str(later_maria))
                planted.mkdir()
                alias = planted / ('mariadb' if later_maria else 'mysql')
                alias.symlink_to(mysql)
                planted.chmod(0o777)
                folders = (mysql.parent, planted) if later_maria else (planted, mysql.parent)
                with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
                    report = self.discover(*folders, refresh=True)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(len(report['installations']), 1)
                self.assertEqual(report['installations'][0]['programs']['mysql']['path'], str(mysql))
                self.assertEqual([s['path'] for s in report['skipped']], [str(alias)])
                self.assertIn('Other users can change', report['skipped'][0]['reason'])
                with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(map(str, folders)),
                                                  'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                        mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                        mock.patch.dict(ct.connector._CLIENT_CHOICES, {'mysql': None, 'mariadb': None}):
                    self.assertEqual(ct.connector.MySQLConnection(host='alias')._resolve_binary(), str(mysql))

    def test_untrusted_alias_cannot_force_bundled_fallback_for_mariadb(self):
        maria = self.make(self.root / 'maria/bin', 'mariadb', banner='mariadb Ver 11.8.2-MariaDB')
        bundle = self.make(self.root / 'bundle').parent
        planted = self.root / 'evil'
        planted.mkdir()
        alias = planted / 'mariadb'
        alias.symlink_to(maria)
        planted.chmod(0o777)
        path = os.pathsep.join(map(str, (planted, maria.parent)))
        with mock.patch.dict(os.environ, {'PATH': path, 'MYFLAMES_BUNDLED_CLIENT_DIR': str(bundle)}), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                mock.patch.dict(ct.connector._CLIENT_CHOICES, {'mysql': None, 'mariadb': None}), \
                mock.patch.dict(ct._FACTS, clear=True):
            report = ct.discover()
            self.assertEqual([s['path'] for s in report['skipped']], [str(alias)])
            ct.remember_server(ct.endpoint_key({'host': 'alias-bundle'}), 'mariadb', '11.8.2-MariaDB')
            self.assertEqual(ct.connector.MySQLConnection(host='alias-bundle')._resolve_binary(), str(maria))

    def test_mariadb_compatibility_symlink_counted_once(self):
        maria = self.make(self.root, 'mariadb', banner='mariadb from 11.8.2-MariaDB, client 15.2')
        (self.root / 'mysql').symlink_to(maria)
        with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            report = self.discover(self.root)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(set(report['installations'][0]['programs']), {'mariadb'})
        self.assertEqual(len(ct.snapshot(str(self.root), (), False)), 1)

    def test_common_and_path_copies_require_trust(self):
        binary = self.make(self.root / 'bin')
        binary.chmod(0o770)
        with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            report = ct.discover(path='', common_dirs=(str(binary.parent),), bundled=False)
            self.assertEqual(run.call_count, 0)
        self.assertIn('Other users can change', report['skipped'][0]['reason'])
        self.assertEqual(self.discover(binary.parent)['installations'], [])

    def test_later_world_writable_path_copies_never_run_or_win(self):
        trusted, planted = self.root / 'trusted/bin', self.root / 'planted/bin'
        marker = self.root / 'executed'
        for name in ('mysql', 'mysqldump', 'mysqlsh'):
            self.make(trusted, name, version='8.4.13')
            binary = self.make(planted, name, version='8.4.99')
            binary.write_text('#!/bin/sh\ntouch ' + str(marker) + '\necho "' + name + ' Ver 8.4.99"\n')
        plugins = planted.parent / 'lib/plugin'
        plugins.mkdir(parents=True)
        (plugins / 'client_ed25519.so').touch()
        planted.chmod(0o777)
        report = self.discover(trusted, planted)
        selected = ct.select(report, {'engine': 'mysql', 'version': '8.4.14'})
        self.assertFalse(marker.exists())
        self.assertEqual(len(report['skipped']), 3)
        self.assertTrue(all('Other users can change' in s['reason'] and
                            s['reason'].endswith('so myflames does not run programs from it.')
                            for s in report['skipped']))
        for role in ('client', 'dump', 'shell'):
            self.assertEqual(Path(selected[role]['path']).parent, trusted)

    def test_extra_literal_folder_cannot_claim_the_app_bundle_exemption(self):
        # Windows searches a chosen folder literally, using the bundled-folder
        # inventory slot to avoid splitting a semicolon in its name.
        mysql = self.make(self.root / 'chosen;folder')
        dump = self.make(mysql.parent, 'mysqldump')
        dump.chmod(0o777)
        with mock.patch.dict(os.environ, {'MYFLAMES_BUNDLED_CLIENT_DIR': ''}):
            report = ct.discover(path='', common_dirs=(), bundled=str(mysql.parent))
        self.assertNotIn('mysqldump', report['installations'][0]['programs'])
        self.assertEqual(report['skipped'][0]['path'], str(dump))

    def test_chosen_dump_is_checked_on_path_and_off_path(self):
        mysql = self.make(self.root / 'chosen')
        dump = self.make(mysql.parent, 'mysqldump')
        dump.chmod(0o777)
        for path in ('', str(mysql.parent)):
            with self.subTest(path=path):
                report = ct.discover_lifecycle(path=path, common_dirs=(), choices={'mysql': str(mysql)})
                self.assertNotIn('mysqldump', report['installations'][0]['programs'])
                self.assertTrue(any(s['path'] == str(dump) for s in report['skipped']))

    def test_trust_checks_parent_and_resolved_file(self):
        folder = self.root / 'bin'
        self.make(folder, 'mysqldump')
        folder.chmod(0o777)
        report = ct.discover(path='', common_dirs=(str(folder),), bundled=False)
        self.assertEqual(len(report['skipped']), 1)
        self.assertIn('Other users can change', report['skipped'][0]['error'])

    def test_authentication_plugins_exclude_qa_and_directories(self):
        folder = self.root / 'bin'
        self.make(folder)
        plugins = self.root / 'lib/plugin'
        plugins.mkdir(parents=True)
        (plugins / 'qa_auth_client.so').touch()
        (plugins / 'authentication_kerberos_client.so').mkdir()
        self.assertFalse(self.discover(folder)['installations'][0]['has_auth_plugins'])
        (plugins / 'authentication_ldap_sasl_client.so').touch()
        self.assertTrue(self.discover(folder)['installations'][0]['has_auth_plugins'])

    def test_mariadb_authentication_plugin(self):
        folder = self.root / 'bin'
        self.make(folder, 'mariadb', banner='mariadb from 11.8.2-MariaDB, client 15.2')
        plugins = self.root / 'lib64/mysql/plugin'
        plugins.mkdir(parents=True)
        (plugins / 'client_ed25519.so').touch()
        self.assertTrue(self.discover(folder)['installations'][0]['has_auth_plugins'])

    def test_cache_identity_refresh_and_independent_results(self):
        binary = self.make(self.root)
        with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            first = self.discover(self.root)
            first['installations'][0]['programs'].clear()
            self.assertTrue(self.discover(self.root)['installations'][0]['programs'])
            self.assertEqual(run.call_count, 1)
            info = binary.stat()
            os.utime(str(binary), ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
            self.discover(self.root)
            self.assertEqual(run.call_count, 2)
            self.discover(self.root, refresh=True)
            self.assertEqual(run.call_count, 3)

    def test_single_flight_and_probes_do_not_hold_global_lock(self):
        self.make(self.root)
        real_run = ct.subprocess.run
        started, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return real_run(*args, **kwargs)
        with mock.patch.object(ct.subprocess, 'run', side_effect=delayed) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.discover, self.root)
                self.assertTrue(started.wait(5))
                second = pool.submit(self.discover, self.root)
                acquired = ct._PROBE_LOCK.acquire(timeout=2)
                if acquired:
                    ct._PROBE_LOCK.release()
                release.set()
                self.assertEqual(first.result(), second.result())
            self.assertTrue(acquired)
            self.assertEqual(run.call_count, 1)

    def test_probe_limit(self):
        folders = [self.root / str(i) for i in range(35)]
        for folder in folders:
            self.make(folder)
        with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            report = self.discover(*folders)
        self.assertEqual(run.call_count, 32)
        self.assertEqual(len(report['installations']), 32)
        self.assertEqual([s['reason'] for s in report['skipped']], ['probe limit'] * 3)

    def test_shell_does_not_consume_query_tool_probe_budget(self):
        shell = self.make(self.root / 'shell/bin', 'mysqlsh', version='9.7.0')
        folders = [shell.parent]
        for index in range(30):
            folders.append(self.make(self.root / ('dump-%d/bin' % index), 'mysqldump').parent)
        old = self.make(self.root / 'old/bin', version='8.4.12')
        newest = self.make(self.root / 'newest/bin')
        folders.extend((old.parent, newest.parent))
        with mock.patch.object(ct.managed_shell, 'installed_bin_dir', return_value=None), \
                mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')):
            full = self.discover(*folders)
            filtered = self.discover(*folders, names=ct.QUERY_DISCOVERY_NAMES)
        self.assertEqual(full['installations'], filtered['installations'])
        self.assertEqual([s['path'] for s in full['shells']], [str(shell)])
        shown, executed = self.settings_and_connector(folders, dict(engine='mysql', version='8.4.13'))
        self.assertEqual(shown, str(newest))
        self.assertEqual(executed, shown)

    def test_shell_and_query_probe_budgets_have_a_hard_total_bound(self):
        folders = [self.make(self.root / ('shell-%d/bin' % index), 'mysqlsh', version='9.7.0').parent
                   for index in range(6)]
        folders.extend(self.make(self.root / ('client-%d/bin' % index)).parent for index in range(35))
        with mock.patch.object(ct.managed_shell, 'installed_bin_dir', return_value=None), \
                mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')), \
                mock.patch.object(ct, 'probe', wraps=ct.probe) as probe:
            report = self.discover(*folders)
        self.assertEqual(probe.call_count, 36)
        self.assertEqual(len(report['installations']), 32)
        self.assertEqual(len(report['shells']), 4)
        self.assertEqual([s['reason'] for s in report['skipped']], ['probe limit'] * 5)

    def test_shell_budget_preserves_managed_priority_and_path_order(self):
        managed = self.make(self.root / 'z-managed/bin', 'mysqlsh', version='9.7.0')
        paths = [self.make(self.root / ('path-%d/bin' % index), 'mysqlsh', version='10.0.0')
                 for index in reversed(range(5))]
        with mock.patch.object(ct.managed_shell, 'installed_bin_dir', return_value=str(managed.parent)), \
                mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')):
            report = self.discover(*(path.parent for path in paths))
        self.assertEqual([s['path'] for s in report['shells']], [str(managed)] + list(map(str, paths[:3])))
        self.assertEqual([s['path'] for s in report['skipped']], list(map(str, paths[3:])))

    def test_lru_bounds_and_refreshes_recency(self):
        for i in range(65):
            self.make(self.root / str(i))
            self.discover(self.root / str(i))
        self.assertEqual(len(ct._PROBE_CACHE), 64)
        with mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            self.discover(self.root / '1')
            self.assertEqual(run.call_count, 0)
            self.discover(self.root / '0')
            self.assertEqual(run.call_count, 1)
        self.assertIn(ct._probe_key('mysql', str(self.root / '1/mysql')), ct._PROBE_CACHE)

    def test_snapshot_and_mysqlpump_never_probe(self):
        self.make(self.root)
        self.make(self.root, 'mysqlpump', exit_code=99)
        with mock.patch.object(ct.subprocess, 'run', side_effect=AssertionError('no probe')):
            candidates = ct.snapshot(str(self.root), (), False)
            self.assertEqual(len(candidates), 2)
            self.assertEqual(len(candidates[0]['identity']), 7)
            (self.root / 'mysql').unlink()
            report = self.discover(self.root)
        self.assertTrue(report['installations'][0]['programs']['mysqlpump']['deprecated'])
        self.assertIsNone(ct.select(report, {'engine': 'mysql', 'version': '8.4.3'})['dump'])

    def test_query_client_snapshot_skips_other_programs_and_managed_pointer(self):
        mysql = self.make(self.root / 'bin')
        self.make(mysql.parent, 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        self.make(mysql.parent, 'mysqlsh')
        self.make(mysql.parent, 'mysqldump')
        with mock.patch.object(ct.managed_shell, 'installed_bin_dir',
                               side_effect=AssertionError('must not read the managed Shell pointer')) as installed, \
                mock.patch.object(ct.connector, 'checked_shell_program',
                                  side_effect=AssertionError('must not check Shell trust')) as shell_check, \
                mock.patch.object(ct.os.path, 'isfile', wraps=ct.os.path.isfile) as isfile, \
                mock.patch.object(ct.subprocess, 'run', side_effect=AssertionError('must not probe')):
            candidates = ct.snapshot(str(mysql.parent), (), False, names=('mysql', 'mariadb'))
        self.assertEqual([c['name'] for c in candidates], ['mysql', 'mariadb'])
        self.assertEqual({os.path.basename(call[0][0]) for call in isfile.call_args_list},
                         {'mysql', 'mariadb'})
        installed.assert_not_called()
        shell_check.assert_not_called()

    def test_snapshot_names_accepts_iterators_and_keeps_mariadb_alias_preference(self):
        maria = self.make(self.root / 'bin', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        (maria.parent / 'mysql').symlink_to(maria)
        candidates = ct.snapshot(str(maria.parent), (), False,
                                 names=iter(('mariadb', 'mysql')))
        self.assertEqual([(c['name'], c['path']) for c in candidates], [('mariadb', str(maria))])
        with mock.patch.object(ct.connector, 'bundled_client_dir',
                               side_effect=AssertionError('empty selection needs no filesystem checks')), \
                mock.patch.object(ct.managed_shell, 'installed_bin_dir',
                                  side_effect=AssertionError('empty selection needs no managed Shell')):
            self.assertEqual(ct.snapshot(names=()), [])

    def test_F2_names_outside_the_program_list_are_never_searched(self):
        mysql = self.make(self.root / 'bin')
        for other in ('sh', 'evil'):
            self.make(mysql.parent, other)
        with mock.patch.object(ct.os.path, 'isfile', wraps=ct.os.path.isfile) as isfile:
            candidates = ct.snapshot(str(mysql.parent), (), False, names=('sh', 'evil', '../bin/sh', 'mysql'))
        self.assertEqual([c['name'] for c in candidates], ['mysql'])
        self.assertEqual({os.path.basename(call[0][0]) for call in isfile.call_args_list}, {'mysql'})
        with mock.patch.object(ct.os.path, 'isfile', side_effect=AssertionError('must not search unknown names')):
            self.assertEqual(ct.snapshot(str(mysql.parent), (), False, names=('sh',)), [])
            # A bare string is not a sequence of program names.
            self.assertEqual(ct.snapshot(str(mysql.parent), (), False, names='mysql'), [])

    def test_default_snapshot_still_discovers_shell_and_walks_its_tree_fresh(self):
        mysql = self.make(self.root / 'bin')
        shell = self.make(mysql.parent, 'mysqlsh')
        startup = private_client_dir(self.root / 'share/mysqlsh') / 'mysqlshrc.py'
        startup.write_text('# Shell startup code\n')
        with mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')), \
                mock.patch.object(ct.managed_shell, 'installed_bin_dir', return_value=None) as installed, \
                mock.patch.object(ct.connector, '_check_shell_tree_trust',
                                  wraps=ct.connector._check_shell_tree_trust) as tree_check, \
                mock.patch.object(ct.connector.os, 'lstat', wraps=ct.connector.os.lstat) as lstat, \
                mock.patch.object(ct.subprocess, 'run', side_effect=AssertionError('must not probe')):
            candidates = ct.snapshot(str(mysql.parent), (), False)
            self.assertEqual([c['name'] for c in candidates], ['mysql', 'mysqlsh'])
            self.assertFalse(any(c.get('trust_error') for c in candidates))
            self.assertIn(mock.call(str(startup)), lstat.call_args_list)
            installed.assert_called_once_with(cache=False)
            tree_check.assert_called_once_with(str(shell))
            startup.chmod(0o777)
            candidates = ct.snapshot(str(mysql.parent), (), False)
        self.assertEqual(tree_check.call_count, 2)
        refused = next(c for c in candidates if c['name'] == 'mysqlsh')
        self.assertIn('changed by other users', refused['trust_error'])

    def test_resolve_query_client_never_checks_shell_tree(self):
        mysql = self.make(self.root / 'bin')
        self.make(mysql.parent, 'mysqlsh')
        startup = private_client_dir(self.root / 'share/mysqlsh') / 'mysqlshrc.py'
        startup.write_text('# Shell startup code\n')
        with mock.patch.dict(os.environ, {'PATH': str(mysql.parent), 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                mock.patch.dict(ct.connector._CLIENT_CHOICES, {'mysql': None, 'mariadb': None}, clear=True), \
                mock.patch.object(ct.managed_shell, 'installed_bin_dir',
                                  side_effect=AssertionError('must not read the managed Shell pointer')), \
                mock.patch.object(ct.connector, 'checked_shell_program',
                                  side_effect=AssertionError('must not check Shell trust')) as shell_check, \
                mock.patch.object(ct, 'snapshot', wraps=ct.snapshot) as snapshot, \
                mock.patch.object(ct, 'probe', side_effect=AssertionError('must not probe')):
            self.assertEqual(ct.connector.MySQLConnection(host='query-only')._resolve_binary(), str(mysql))
        snapshot.assert_called_once_with(names=ct.QUERY_DISCOVERY_NAMES)
        shell_check.assert_not_called()

    def test_F3_single_client_is_rechecked_after_snapshot(self):
        mysql = self.make(self.root / 'bin')
        real_snapshot = ct.snapshot
        for mode in (0o720, 0o702):
            mysql.chmod(0o700)

            def snapshot_then_loosen(*args, **kwargs):
                found = real_snapshot(*args, **kwargs)
                self.assertEqual([c['path'] for c in found if not c.get('trust_error')], [str(mysql)])
                mysql.chmod(mode)
                return found

            with self.subTest(mode=oct(mode)), \
                    mock.patch.dict(os.environ, {'PATH': str(mysql.parent), 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                    mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                    mock.patch.dict(ct.connector._CLIENT_CHOICES, {'mysql': None, 'mariadb': None}, clear=True), \
                    mock.patch.object(ct, 'snapshot', side_effect=snapshot_then_loosen), \
                    mock.patch.object(ct, 'probe', side_effect=AssertionError('shortcut must not probe')):
                conn = ct.connector.MySQLConnection(host='snapshot-race')
                with self.assertRaisesRegex(ct.connector.ClientMissingError, 'changed by other users'):
                    conn._resolve_binary()

    def test_F3_selected_client_is_rechecked_after_selection(self):
        mysql = self.make(self.root / 'a/bin')
        older = self.make(self.root / 'b/bin', version='8.0.45')
        real_select = ct.select

        def select_then_loosen(*args, **kwargs):
            selected = real_select(*args, **kwargs)
            self.assertEqual(selected['client']['path'], str(mysql))
            mysql.chmod(0o702)
            return selected

        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(map(str, (mysql.parent, older.parent))),
                                          'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                mock.patch.dict(ct.connector._CLIENT_CHOICES, {'mysql': None, 'mariadb': None}, clear=True), \
                mock.patch.dict(ct._FACTS, clear=True), \
                mock.patch.object(ct, 'select', side_effect=select_then_loosen):
            conn = ct.connector.MySQLConnection(host='selection-race')
            ct.remember_server(conn._endpoint_key(), 'mysql', '8.4.13')
            with self.assertRaisesRegex(ct.connector.ClientMissingError, 'changed by other users'):
                conn._resolve_binary()

    def test_F3_single_client_refuses_a_link_target_swapped_after_snapshot(self):
        first = self.make(self.root / 'v1/bin')
        second = self.make(self.root / 'v2/bin', version='9.7.0')
        link = private_client_dir(self.root / 'bin') / 'mysql'
        link.symlink_to(first)
        real_snapshot = ct.snapshot

        def snapshot_then_swap(*args, **kwargs):
            found = real_snapshot(*args, **kwargs)
            self.assertEqual([c['realpath'] for c in found if not c.get('trust_error')], [str(first)])
            link.unlink()
            link.symlink_to(second)
            return found

        with mock.patch.dict(os.environ, {'PATH': str(link.parent), 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                mock.patch.dict(ct.connector._CLIENT_CHOICES, {'mysql': None, 'mariadb': None}, clear=True), \
                mock.patch.object(ct, 'snapshot', side_effect=snapshot_then_swap), \
                mock.patch.object(ct, 'probe', side_effect=AssertionError('shortcut must not probe')):
            with self.assertRaises(ct.connector.ClientMissingError):
                ct.connector.MySQLConnection(host='link-race')._resolve_binary()

    def test_F3_discovery_rechecks_client_before_version_probe(self):
        mysql = self.make(self.root / 'bin')
        real_snapshot = ct.snapshot

        def snapshot_then_loosen(*args, **kwargs):
            found = real_snapshot(*args, **kwargs)
            self.assertEqual([c['path'] for c in found if not c.get('trust_error')], [str(mysql)])
            mysql.chmod(0o720)
            return found

        with mock.patch.object(ct, 'snapshot', side_effect=snapshot_then_loosen), \
                mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            report = self.discover(mysql.parent, names=('mysql',))
        # Inventory reports the trust refusal without launching even --version.
        self.assertEqual(report['installations'], [])
        self.assertEqual(len(report['skipped']), 1)
        self.assertIn('Other users can change', report['skipped'][0]['reason'])
        run.assert_not_called()

    def test_resolve_multiple_clients_and_settings_choices_skip_shell_discovery(self):
        mysql = self.make(self.root / 'mysql/bin')
        maria = self.make(self.root / 'maria/bin', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        for folder in (mysql.parent, maria.parent):
            self.make(folder, 'mysqlsh')
        for engine, binary in (('mysql', mysql), ('mariadb', maria)):
            for chosen in (False, True):
                with self.subTest(engine=engine, chosen=chosen), \
                        mock.patch.dict(os.environ, {'PATH': os.pathsep.join(map(str, (mysql.parent, maria.parent))),
                                                    'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                        mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                        mock.patch.dict(ct.connector._CLIENT_CHOICES,
                                        {'mysql': None, 'mariadb': None, **({engine: str(binary)} if chosen else {})},
                                        clear=True), \
                        mock.patch.dict(ct._FACTS, clear=True), \
                        mock.patch.object(ct.managed_shell, 'installed_bin_dir',
                                          side_effect=AssertionError('must not read the managed Shell pointer')), \
                        mock.patch.object(ct.connector, 'checked_shell_program',
                                          side_effect=AssertionError('must not check Shell trust')):
                    conn = ct.connector.MySQLConnection(host='multiple-query-clients')
                    ct.remember_server(conn._endpoint_key(), engine,
                                       '8.4.13' if engine == 'mysql' else '11.4.2-MariaDB')
                    self.assertEqual(conn._resolve_binary(), str(binary))

    def test_discover_choices_reuses_names_iterator_for_off_path_folders(self):
        mysql = self.make(self.root / 'mysql/bin')
        maria = self.make(self.root / 'maria/bin', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        for folder in (mysql.parent, maria.parent):
            self.make(folder, 'mysqlsh')
        with mock.patch.dict(os.environ, {'PATH': '', 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                mock.patch.object(ct.managed_shell, 'installed_bin_dir',
                                  side_effect=AssertionError('must not read the managed Shell pointer')), \
                mock.patch.object(ct.connector, 'checked_shell_program',
                                  side_effect=AssertionError('must not check Shell trust')):
            report = ct.discover_choices({'mysql': str(mysql), 'mariadb': str(maria)},
                                         automatic=False, names=iter(('mysql', 'mariadb')))
        self.assertEqual({g['engine'] for g in report['installations']}, {'mysql', 'mariadb'})
        self.assertEqual(report['shells'], [])

    def test_first_contact_and_auth_retry_skip_shell_discovery(self):
        mysql = self.make(self.root / 'mysql/bin')
        maria = self.make(self.root / 'maria/bin', 'mariadb', banner='mariadb Ver 11.4.2-MariaDB')
        for folder in (mysql.parent, maria.parent):
            self.make(folder, 'mysqlsh')
        mysql_facts = '8.4.13\tMySQL Community Server\n'
        maria_facts = '11.4.2-MariaDB\tMariaDB Server\n'
        cases = (
            ('chosen', {'mysql': str(mysql)}, [mysql_facts], mysql),
            ('auth-retry', {'mysql': str(mysql)},
             [ct.connector.ClientAuthPluginError('client plugin unavailable'), maria_facts], maria),
            ('broken-choice', {'mariadb': str(self.root / 'missing/mariadb')}, [mysql_facts], mysql))
        for label, choices, replies, expected in cases:
            with self.subTest(case=label), \
                    mock.patch.dict(os.environ, {'PATH': os.pathsep.join(map(str, (mysql.parent, maria.parent))),
                                                'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                    mock.patch('myflames.lifecycle.COMMON_BIN_DIRS', ()), \
                    mock.patch.dict(ct.connector._CLIENT_CHOICES,
                                    dict({'mysql': None, 'mariadb': None}, **choices), clear=True), \
                    mock.patch.dict(ct._FACTS, clear=True), \
                    mock.patch.object(ct.managed_shell, 'installed_bin_dir',
                                      side_effect=AssertionError('must not read the managed Shell pointer')), \
                    mock.patch.object(ct.connector, 'checked_shell_program',
                                      side_effect=AssertionError('must not check Shell trust')):
                conn = ct.connector.MySQLConnection(host='first-contact-' + label)
                with mock.patch.object(conn, '_run_with_binary', side_effect=replies) as run:
                    self.assertEqual(conn._resolve_binary(), str(expected))
                self.assertEqual(run.call_count, len(replies))

    def test_shells_separate_and_newest_first(self):
        for i, version in enumerate(('8.4.13', '9.6.0')):
            self.make(self.root / str(i), 'mysqlsh', banner='mysqlsh ' + version)
        report = self.discover(self.root / '0', self.root / '1')
        self.assertEqual(report['installations'], [])
        self.assertEqual([s['version'] for s in report['shells']], ['9.6.0', '8.4.13'])

    def test_managed_shell_appears_after_first_read_and_wins_over_newer_path_copy(self):
        from myflames import cluster_shell, lifecycle, managed_shell
        managed = self.make(self.root / 'managed/bin', 'mysqlsh', version='9.7.0')
        newer = self.make(self.root / 'path/bin', 'mysqlsh', version='10.0.0')
        with mock.patch.object(managed_shell, 'installed_bin_dir', return_value=None) as installed:
            self.assertEqual([s['path'] for s in self.discover(newer.parent)['shells']], [str(newer)])
            installed.return_value = str(managed.parent)
            # A PATH alias of the managed binary must not duplicate or relabel it.
            report = self.discover(newer.parent, managed.parent)
            self.assertEqual([s['path'] for s in report['shells']], [str(managed), str(newer)])
            self.assertEqual(report['shells'][0]['origin'], 'managed')
            self.assertEqual(report['shells'][0]['origin_label'], 'Downloaded by myflames')
            self.assertEqual(report['copies'][0]['origin'], 'managed')
            self.assertEqual(ct.select(report, {'engine': 'mysql', 'version': '8.4.13'})['shell']['path'],
                             str(managed))
            with mock.patch.dict(os.environ, {'PATH': str(newer.parent), 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                    mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ()), \
                    mock.patch.object(ct.connector, 'client_choices', return_value={}):
                self.assertEqual(cluster_shell.find_shell()['path'], str(managed))
                for server in (None, {'engine': 'mysql', 'version': '8.4.13'}):
                    with self.subTest(server=server):
                        tools = lifecycle.detect_tools(server=server)
                        self.assertEqual(tools['tools']['mysqlsh']['path'], str(managed))
                        self.assertEqual(tools['tools']['mysqlsh']['origin_label'], 'Downloaded by myflames')
            self.assertGreaterEqual(installed.call_count, 5)

    def test_managed_bin_only_adds_shell_and_rechecks_launch_trust(self):
        from myflames import managed_shell
        binary = self.make(self.root / 'managed/bin', 'mysqlsh', version='9.7.0')
        self.make(binary.parent, 'mysql')
        with mock.patch.object(managed_shell, 'installed_bin_dir', return_value=str(binary.parent)), \
                mock.patch.object(ct.connector, 'checked_shell_program',
                                  wraps=ct.connector.checked_shell_program) as checked:
            report = self.discover()
            self.assertEqual(report['installations'], [])
            self.assertEqual([s['path'] for s in report['shells']], [str(binary)])
            # Snapshot, discovery and the actual --version launch each check.
            self.assertGreaterEqual(checked.call_count, 3)
            self.assertEqual(checked.call_args, mock.call(str(binary)))

    def test_managed_inventory_listing_uses_the_trust_cache_only_for_cached_reads(self):
        from myflames import managed_shell
        binary = self.make(self.root / 'managed/bin', 'mysqlsh', version='9.7.0')
        with mock.patch.object(managed_shell, 'installed_bin_dir', return_value=str(binary.parent)) as installed:
            ct.snapshot(path='', common_dirs=(), bundled=False)
            self.assertEqual(installed.call_args, mock.call(cache=False))
            ct.snapshot(path='', common_dirs=(), bundled=False, cache_shell_trust=True)
            self.assertEqual(installed.call_args, mock.call(cache=True))

    def test_shell_startup_tree_abuse_is_skipped_without_any_version_probe(self):
        from myflames import cluster_shell, lifecycle
        system = self.root / 'etc-mysql'
        shell = self.make(self.root / 'shell/bin', 'mysqlsh', version='9.7.0')
        for relative, directory in (
                ('share/mysqlsh', True), ('share/mysqlsh/mysqlshrc.js', False),
                ('share/mysqlsh/mysqlshrc.py', False), ('bin/mysqlshrc.js', False),
                ('lib/mysqlsh/plugins/p/init.py', False), ('etc-mysql/mysqlsh', True),
                ('etc-mysql/mysqlsh/mysqlshrc.js', False)):
            path = self.root / relative if relative.startswith('etc-mysql') else shell.parent.parent / relative
            private_client_dir(path if directory else path.parent)
            if not directory:
                path.write_text('/* untrusted code */\n')
            path.chmod(0o777)
            try:
                with self.subTest(relative=relative), \
                        mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(system)), \
                        mock.patch.object(ct, 'probe') as probe, \
                        mock.patch.object(ct.subprocess, 'run') as run, \
                        mock.patch.dict(os.environ, {'PATH': str(shell.parent), 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                        mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ()):
                    candidates = ct.snapshot(path=str(shell.parent), common_dirs=(), bundled=False)
                    self.assertIn('trust_error', candidates[0])
                    report = self.discover(shell.parent)
                    self.assertEqual(report['shells'], [])
                    self.assertEqual([s['path'] for s in report['skipped']], [str(shell)])
                    self.assertIn('Other users can change', report['skipped'][0]['reason'])
                    self.assertIsNone(cluster_shell.find_shell(refresh=True))
                    probe.assert_not_called()
                    run.assert_not_called()
            finally:
                path.chmod(0o700)

    def test_shell_tree_symlink_to_world_writable_directory_is_never_probed(self):
        shell = self.make(self.root / 'shell/bin', 'mysqlsh')
        target = private_client_dir(self.root / 'other-user')
        target.chmod(0o777)
        share = private_client_dir(shell.parent.parent / 'share')
        (share / 'mysqlsh').symlink_to(target, target_is_directory=True)
        with mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')), \
                mock.patch.object(ct, 'probe') as probe:
            report = self.discover(shell.parent)
        self.assertFalse(report['shells'])
        self.assertEqual(len(report['skipped']), 1)
        probe.assert_not_called()

    def test_discover_rechecks_shell_tree_after_snapshot(self):
        shell = self.make(self.root / 'shell/bin', 'mysqlsh')
        directory = private_client_dir(shell.parent.parent / 'share/mysqlsh')
        candidates = ct.snapshot(path=str(shell.parent), common_dirs=(), bundled=False)
        directory.chmod(0o777)
        with mock.patch.object(ct, 'snapshot', return_value=candidates), mock.patch.object(ct, 'probe') as probe:
            report = self.discover(shell.parent)
        self.assertFalse(report['shells'])
        self.assertEqual(len(report['skipped']), 1)
        probe.assert_not_called()

    def test_linked_startup_file_oserror_and_budget_all_skip_before_probe(self):
        shell = self.make(self.root / 'shell/bin', 'mysqlsh')
        share = private_client_dir(shell.parent.parent / 'share')
        target = private_client_dir(self.root / 'linked-startup')
        code = target / 'mysqlshrc.js'
        code.write_text('/* code loaded through a top-level link */')
        (share / 'mysqlsh').symlink_to(target, target_is_directory=True)
        code.chmod(0o777)
        with mock.patch.object(ct, 'probe') as probe:
            report = self.discover(shell.parent)
            self.assertFalse(report['shells'])
            self.assertIn('Other users can change', report['skipped'][0]['reason'])
            probe.assert_not_called()
        code.chmod(0o600)
        with mock.patch.object(ct.connector.os, 'scandir', side_effect=PermissionError('private detail')), \
                mock.patch.object(ct, 'probe') as probe:
            report = self.discover(shell.parent)
            self.assertFalse(report['shells'])
            self.assertIn('could not be checked safely', report['skipped'][0]['reason'])
            probe.assert_not_called()
        # The full 40,001-entry boundary is also exercised by connector's
        # real filesystem test. Lower the same bound here to check inventory.
        with mock.patch.object(ct.connector, 'MAX_SHELL_TRUST_ENTRIES', 1), \
                mock.patch.object(ct, 'probe') as probe:
            report = self.discover(shell.parent)
            self.assertFalse(report['shells'])
            self.assertIn('too many files', report['skipped'][0]['reason'])
            probe.assert_not_called()

    def test_cached_inventory_never_lets_untrusted_shell_start_a_version_probe(self):
        shell = self.make(self.root / 'shell/bin', 'mysqlsh')
        directory = private_client_dir(shell.parent.parent / 'lib/mysqlsh')
        code = directory / 'plugin.py'
        code.write_text('/* trusted */')
        code.chmod(0o600)
        self.discover(shell.parent, cache_shell_trust=True)
        code.chmod(0o777)
        with mock.patch.object(ct.subprocess, 'run') as run:
            report = self.discover(shell.parent, cache_shell_trust=True, refresh=True)
            self.assertFalse(report['shells'])
            run.assert_not_called()
            # Direct probes also recheck, independently of discovery caching.
            self.assertFalse(ct.probe('mysqlsh', str(shell), refresh=True)['available'])
            run.assert_not_called()

    def test_find_shell_polling_reuses_walk_and_refresh_rechecks(self):
        from myflames import cluster_shell, lifecycle
        shell = self.make(self.root / 'shell/bin', 'mysqlsh', version='9.7.0')
        with mock.patch.dict(os.environ, {'PATH': str(shell.parent), 'MYFLAMES_BUNDLED_CLIENT_DIR': ''}), \
                mock.patch.object(lifecycle, 'COMMON_BIN_DIRS', ()), \
                mock.patch.object(ct.connector, 'MYSQLSH_SYSTEM_CONFIG_DIR', str(self.root / 'etc-mysql')), \
                mock.patch.object(ct.connector, '_check_shell_tree_trust',
                                  wraps=ct.connector._check_shell_tree_trust) as walk:
            self.assertIsNotNone(cluster_shell.find_shell())
            initial = walk.call_count
            self.assertGreater(initial, 0)
            self.assertIsNotNone(cluster_shell.find_shell())
            self.assertEqual(walk.call_count, initial)
            self.assertIsNotNone(cluster_shell.find_shell(refresh=True))
            self.assertGreater(walk.call_count, initial)

    def test_actual_shell_version_probe_disables_openssl_configuration(self):
        shell = self.make(self.root, 'mysqlsh', version='9.7.0')
        with mock.patch.dict(os.environ, {'OPENSSL_CONF': '/hostile/openssl.cnf'}), \
                mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            self.assertTrue(ct.probe('mysqlsh', str(shell))['available'])
        self.assertEqual(run.call_args[1]['env']['OPENSSL_CONF'], os.devnull)

    def test_inventory_copies_keep_banners_metadata_and_failed_probes(self):
        mysql = self.make(self.root / 'first', banner='mysql Ver 8.4.13 for test')
        failed = self.make(self.root / 'second', 'mysqldump', banner='unknown', exit_code=2)
        self.make(mysql.parent, 'mysqlpump', exit_code=99)
        report = self.discover(mysql.parent, failed.parent)
        self.assertEqual(len(report['copies']), 3)
        entries = {c['name']: c for c in report['copies']}
        self.assertEqual(entries['mysql']['version'], 'mysql Ver 8.4.13 for test')
        self.assertEqual(entries['mysql']['realpath'], str(mysql))
        self.assertEqual(entries['mysql']['installation'], 'mysql:' + str(mysql.parent))
        self.assertEqual(entries['mysql']['origin'], 'path')
        self.assertFalse(entries['mysqldump']['available'])
        self.assertIn('Version probe failed', entries['mysqldump']['error'])
        self.assertTrue(entries['mysqlpump']['available'])
        self.assertTrue(entries['mysqlpump']['deprecated'])
        self.assertIsNone(entries['mysqlpump']['version'])

    def test_lifecycle_inventory_includes_and_checks_chosen_offpath_folder(self):
        mysql = self.make(self.root / 'chosen')
        self.make(mysql.parent, 'mysqldump')
        with mock.patch.object(ct.connector, 'bundled_client_dir', return_value=None):
            report = ct.discover_lifecycle(path='', common_dirs=(), choices={'mysql': str(mysql)})
            self.assertEqual(len(report['copies']), 2)
            self.assertEqual(report['installations'][0]['origin_label'], 'Chosen folder')
            self.assertTrue(all(c['origin'] == 'folder' for c in report['copies']))
            mysql.chmod(0o777)
            report = ct.discover_lifecycle(path='', common_dirs=(), choices={'mysql': str(mysql)})
            self.assertEqual(report['copies'], [])
            self.assertEqual(report['installations'], [])

    def test_lifecycle_chosen_installation_beyond_probe_limit_is_not_stale(self):
        folders = [self.root / str(i) for i in range(32)]
        for folder in folders:
            self.make(folder)
        chosen = self.make(self.root / 'chosen')
        self.make(chosen.parent, 'mysqldump')
        with mock.patch.object(ct.connector, 'bundled_client_dir', return_value=None):
            report = ct.discover_lifecycle(path=os.pathsep.join(str(f) for f in folders + [chosen.parent]),
                                          common_dirs=(), choices={'mysql': str(chosen)})
        self.assertEqual(len(report['copies']), 34)
        self.assertEqual(report['skipped'], [])
        self.assertTrue(all(c['available'] for c in report['copies']))
        keys, errors = ct.choice_keys(report, {'mysql': str(chosen)})
        self.assertIsNone(errors['mysql'])
        selected = ct.select(report, {'engine': 'mysql', 'version': '8.4.3'}, keys)
        self.assertEqual(selected['dump']['path'], str(chosen.parent / 'mysqldump'))
        self.assertEqual(selected['dump']['reason'], 'chosen')

    def test_clean_environment_and_probe_arguments(self):
        binary = self.make(self.root)
        with mock.patch.dict(os.environ, {'MYSQL_PWD': 'secret', 'API_KEY': 'secret'}), \
                mock.patch.object(ct.subprocess, 'run', wraps=ct.subprocess.run) as run:
            self.discover(self.root)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(binary), '--version'])
        self.assertEqual(kwargs['timeout'], 8)
        self.assertNotIn('MYSQL_PWD', kwargs['env'])
        self.assertNotIn('API_KEY', kwargs['env'])
        self.assertFalse(os.path.exists(kwargs['env']['HOME']))
        self.assertNotIn('shell', kwargs)

    def test_job_cancellation_prevents_probe(self):
        self.make(self.root)
        job = mock.Mock()
        job.check_cancel.side_effect = RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'), \
                mock.patch.object(ct.subprocess, 'run', side_effect=AssertionError('no probe')):
            self.discover(self.root, job=job)

    def test_read_cancellation_stops_discovery_before_second_probe(self):
        from myflames import read_cancel
        folders = [self.make(self.root / str(i)).parent for i in range(3)]
        cancel = threading.Event()

        def probe_and_cancel(*args, **kwargs):
            cancel.set()
            return dict(available=True, version='mysql Ver 8.4.13',
                        version_tuple=[8, 4, 13], engine='mysql')

        with mock.patch.object(ct, 'probe', side_effect=probe_and_cancel) as probe:
            with read_cancel.scope(cancel), self.assertRaises(read_cancel.CancelledRead):
                self.discover(*folders)
        self.assertEqual(probe.call_count, 1)

    def test_read_cancellation_stops_snapshot_between_candidates(self):
        from myflames import read_cancel
        folders = [self.make(self.root / str(i)).parent for i in range(3)]
        cancel = threading.Event()
        checked = ct.connector.checked_program

        def check_and_cancel(*args, **kwargs):
            result = checked(*args, **kwargs)
            cancel.set()
            return result

        with mock.patch.object(ct.connector, 'checked_program', side_effect=check_and_cancel) as check:
            with read_cancel.scope(cancel), self.assertRaises(read_cancel.CancelledRead):
                ct.snapshot(path=os.pathsep.join(str(folder) for folder in folders),
                            common_dirs=(), bundled=False, names=('mysql',))
        self.assertEqual(check.call_count, 1)

    def test_read_cancellation_prevents_direct_version_probe(self):
        from myflames import read_cancel
        binary = self.make(self.root)
        cancel = threading.Event()
        result = subprocess.CompletedProcess([], 0, b'mysql Ver 8.4.13\n')
        with mock.patch.object(ct.subprocess, 'run', return_value=result) as run:
            with read_cancel.scope(cancel):
                cancel.set()
                with self.assertRaises(read_cancel.CancelledRead):
                    ct.probe('mysql', str(binary))
        run.assert_not_called()

    def test_read_cancellation_interrupts_wait_for_shared_version_probe(self):
        from myflames import read_cancel
        binary = self.make(self.root)
        key = ct._probe_key('mysql', str(binary))
        cancel = threading.Event()
        done = mock.Mock()

        def wait(timeout):
            cancel.set()
            # A second wait completes so the unfixed implementation fails
            # deterministically instead of leaving a test thread blocked.
            return done.wait.call_count > 1

        done.wait.side_effect = wait
        flight = dict(done=done, result=dict(available=True, version='mysql Ver 8.4.13',
                                            version_tuple=[8, 4, 13], engine='mysql'))
        with ct._PROBE_LOCK:
            ct._PROBE_FLIGHTS[key] = flight
        try:
            with mock.patch.object(ct.subprocess, 'run') as run:
                with read_cancel.scope(cancel), self.assertRaises(read_cancel.CancelledRead):
                    ct.probe('mysql', str(binary))
            run.assert_not_called()
            self.assertEqual(done.wait.call_count, 1)
        finally:
            with ct._PROBE_LOCK:
                ct._PROBE_FLIGHTS.pop(key, None)

    def test_read_cancellation_wins_when_shared_version_probe_finishes(self):
        from myflames import read_cancel
        binary = self.make(self.root)
        key = ct._probe_key('mysql', str(binary))
        cancel = threading.Event()
        done = mock.Mock()

        def finish_and_cancel(timeout):
            cancel.set()
            return True

        done.wait.side_effect = finish_and_cancel
        flight = dict(done=done, result=dict(available=True, version='mysql Ver 8.4.13',
                                            version_tuple=[8, 4, 13], engine='mysql'))
        with ct._PROBE_LOCK:
            ct._PROBE_FLIGHTS[key] = flight
        try:
            with mock.patch.object(ct.subprocess, 'run') as run:
                with read_cancel.scope(cancel), self.assertRaises(read_cancel.CancelledRead):
                    ct.probe('mysql', str(binary))
            run.assert_not_called()
            self.assertEqual(done.wait.call_count, 1)
            # A cancelled waiter must leave the shared result for other callers.
            with ct._PROBE_LOCK:
                self.assertIs(ct._PROBE_FLIGHTS[key], flight)
        finally:
            with ct._PROBE_LOCK:
                ct._PROBE_FLIGHTS.pop(key, None)

    def test_uncancelled_probe_waiter_retries_a_cancelled_owner(self):
        from myflames import read_cancel
        binary = self.make(self.root)
        key = ct._probe_key('mysql', str(binary))
        done = mock.Mock()
        flight = dict(done=done, result=None, error=read_cancel.CancelledRead())

        def finish_cancelled_owner(timeout):
            # The original owner removes its completed flight in finally.
            with ct._PROBE_LOCK:
                self.assertIs(ct._PROBE_FLIGHTS.pop(key), flight)
            return True

        done.wait.side_effect = finish_cancelled_owner
        with ct._PROBE_LOCK:
            ct._PROBE_FLIGHTS[key] = flight
        try:
            result = subprocess.CompletedProcess([], 0, b'mysql Ver 8.4.13\n')
            with read_cancel.scope(threading.Event()), \
                    mock.patch.object(ct.subprocess, 'run', return_value=result) as run:
                found = ct.probe('mysql', str(binary))
            self.assertTrue(found['available'])
            self.assertEqual(found['version_tuple'], [8, 4, 13])
            run.assert_called_once()
            self.assertEqual(done.wait.call_count, 1)
        finally:
            with ct._PROBE_LOCK:
                ct._PROBE_FLIGHTS.pop(key, None)

    def test_stop_event_stops_discover_between_candidates(self):
        folders = [self.make(self.root / str(i)).parent for i in range(3)]
        stop = threading.Event()
        real_run = ct.subprocess.run

        def run_and_stop(*args, **kwargs):
            stop.set()
            return real_run(*args, **kwargs)

        with mock.patch.object(ct.subprocess, 'run', side_effect=run_and_stop) as run:
            report = self.discover(*folders, stop=stop)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(report['installations']), 1)

        with mock.patch.object(ct, 'snapshot', side_effect=AssertionError('must not scan when already stopped')), \
                mock.patch.object(ct.subprocess, 'run', side_effect=AssertionError('no probe')):
            stopped = self.discover(*folders, stop=stop)
        self.assertEqual(stopped, dict(installations=[], shells=[], skipped=[], copies=[]))


class SelectionTests(unittest.TestCase):
    @staticmethod
    def group(version, order, bundled=False, plugins=False, engine='mysql', names=None, key=None):
        folder = '/tools/%s/%s' % (order, version)
        names = names if names is not None else (('mysql', 'mysqldump') if engine == 'mysql'
                                                else ('mariadb', 'mariadb-dump', 'mariadb-backup'))
        v = tuple(int(n) for n in version.split('.'))
        return dict(key=key or engine + ':' + folder, engine=engine, version=version,
                    version_tuple=v, series=version.rsplit('.', 1)[0], folder=folder,
                    found_in=folder, origin='bundled' if bundled else 'homebrew',
                    origin_label='Bundled with myflames' if bundled else 'Homebrew mysql',
                    bundled=bundled, has_auth_plugins=plugins, order=order,
                    programs={n: dict(path=folder + '/' + n, realpath=folder + '/' + n, version=version)
                              for n in names})

    def setUp(self):
        self.groups = [self.group('8.4.8', 0), self.group('8.0.45', 1), self.group('8.4.13', 2, True)]
        self.shell = dict(path='/shell/mysqlsh', realpath='/shell/mysqlsh', version='9.6.0',
                          version_tuple=(9, 6, 0), origin='path', origin_label='On PATH', order=5)
        self.discovered = dict(installations=self.groups, shells=[self.shell], skipped=[])

    def choose(self, version='8.4.3', engine='mysql', choice=None):
        return ct.select(self.discovered, dict(engine=engine, version=version), choice)

    def test_first_contact_prefers_valid_choices_then_automatic_fallback(self):
        maria = self.group('11.4.2', 3, engine='mariadb')
        self.groups.append(maria)
        for keys, expected in ((dict(mysql=self.groups[2]['key'], mariadb=maria['key']), self.groups[2]),
                               (dict(mysql=None, mariadb=maria['key']), maria),
                               (dict(mysql=None, mariadb=None), self.groups[0])):
            with self.subTest(keys=keys):
                self.assertEqual(ct.first_contact_client(self.discovered, keys)['installation'], expected['key'])
        self.assertIsNone(ct.first_contact_client(dict(installations=[], shells=[]), {}))

    def test_other_engine_prefers_newest_client_including_bundle(self):
        result = self.choose('11.4.5-MariaDB', engine='mariadb')
        self.assertEqual(result['client']['version'], '8.4.13')
        self.assertEqual(result['client']['reason'], 'other_engine')
        self.assertTrue(result['client']['bundled'])
        self.assertIsNone(result['dump'])
        self.assertIsNone(result['load'])

    def test_other_engine_auth_plugins_win_then_installed_breaks_ties(self):
        self.groups[0]['has_auth_plugins'] = True
        self.assertEqual(self.choose('11.4.5-MariaDB', engine='mariadb')['client']['version'], '8.4.8')
        self.groups[2]['has_auth_plugins'] = True
        self.groups.append(self.group('8.4.13', 9, plugins=True))
        selected = self.choose('11.4.5-MariaDB', engine='mariadb')['client']
        self.assertEqual(selected['installation'], self.groups[-1]['key'])
        self.assertFalse(selected['bundled'])

    def test_unknown_server_preserves_first_contact_order(self):
        self.groups[2]['has_auth_plugins'] = True
        for server in (None, {'engine': 'unknown', 'version': '11.4.5'}):
            selected = ct.select(self.discovered, server)['client']
            self.assertEqual(selected['version'], '8.4.8')
            self.assertEqual(selected['reason'], 'unknown_server')

    def test_owner_mac_selection_table(self):
        for server, version, reason in (('8.4.3', '8.4.13', 'series'), ('8.0.44', '8.0.45', 'series'),
                                        ('5.7.44', '8.0.45', 'newer'), ('9.7.2', '8.4.13', 'older')):
            with self.subTest(server=server):
                result = self.choose(server)
                for role in ('client', 'dump', 'load'):
                    self.assertEqual(result[role]['version'], version)
                    self.assertEqual(result[role]['reason'], reason)
                self.assertEqual(result['shell']['version'], '9.6.0')
                self.assertEqual(result['shell']['reason'], 'newest')
        result = self.choose('9.7.2')
        self.assertEqual(result['dump']['warning'], 'mysqldump 8.4.13 is older than this server (MySQL 9.7). '
                         'Install MySQL 9.7 client tools for a complete dump.')
        self.assertEqual(result['client']['warning'], 'mysql 8.4.13 is older than this server (MySQL 9.7).')

    def test_auth_plugins_precede_patch_then_installed_breaks_tie(self):
        workbench = self.group('8.4.11', 3, plugins=True)
        workbench.update(origin='workbench', origin_label='MySQL Workbench')
        self.groups.append(workbench)
        self.assertEqual(self.choose()['client']['installation'], workbench['key'])
        workbench['has_auth_plugins'] = False
        self.groups[0]['programs']['mysql']['version'] = '8.4.13'
        self.assertEqual(self.choose()['client']['installation'], self.groups[0]['key'])

    def test_choice_wins_every_supported_role(self):
        key = self.groups[2]['key']
        result = self.choose('8.0.44', choice={'mysql': key})
        for role in ('client', 'dump', 'load'):
            self.assertEqual(result[role]['installation'], key)
            self.assertEqual(result[role]['reason'], 'chosen')

    def test_choice_missing_dump_never_falls_back(self):
        self.groups[2]['programs'].pop('mysqldump')
        result = self.choose(choice={'mysql': self.groups[2]['key']})
        self.assertIsNone(result['dump'])
        self.assertEqual(result['missing'], [dict(role='dump', program='mysqldump',
                         text='The installation chosen in Settings > Client tools has no mysqldump.')])
        self.assertEqual(result['load']['installation'], self.groups[2]['key'])

    def test_stale_choice_never_falls_back(self):
        result = self.choose(choice={'mysql': 'missing-key'})
        self.assertIsNone(result['client'])
        self.assertIsNone(result['dump'])
        self.assertEqual(len(result['missing']), 3)

    def test_other_engine_mariadb_missing_roles(self):
        result = self.choose('11.8.2-MariaDB', 'mariadb')
        self.assertEqual(result['client']['reason'], 'other_engine')
        self.assertEqual(result['client']['version'], '8.4.13')
        for role in ('dump', 'load', 'backup', 'shell'):
            self.assertIsNone(result[role])
        self.assertEqual({m['role'] for m in result['missing']}, {'dump', 'load', 'backup'})
        self.assertEqual(result['notes'], ['No MariaDB client tools were found; the MySQL client reads '
                         'this server. Dumps and restores need MariaDB client tools.'])

    def test_mariadb_tools_client_alias_backup_and_no_shell(self):
        self.groups.append(self.group('11.8.2', 4, engine='mariadb',
                                      names=('mysql', 'mysqldump', 'mariadb-backup')))
        result = self.choose('11.8.1-MariaDB', 'mariadb')
        self.assertEqual(result['client']['program'], 'mysql')
        self.assertEqual(result['dump']['program'], 'mysqldump')
        self.assertEqual(result['backup']['program'], 'mariadb-backup')
        self.assertEqual(result['load']['engine'], 'mariadb')
        self.assertIsNone(result['shell'])

    def test_mysql_with_only_mariadb_never_dumps(self):
        self.discovered['installations'] = [self.group('11.8.2', 0, engine='mariadb')]
        result = self.choose()
        self.assertEqual(result['client']['reason'], 'other_engine')
        self.assertIsNone(result['dump'])
        self.assertIsNone(result['load'])

    def test_unknown_and_proxysql_keep_historical_order(self):
        for server in (None, {'engine': 'mysql', 'version': '2.7.3'},
                       {'engine': 'mysql', 'version': 'garbage'}, {'engine': 'proxy', 'version': '8.4.3'}):
            with self.subTest(server=server):
                result = ct.select(self.discovered, server)
                self.assertIsNone(result['server'])
                self.assertEqual(result['client']['version'], '8.4.8')
                self.assertEqual(result['client']['reason'], 'unknown_server')
                for role in ('dump', 'load', 'backup', 'shell'):
                    self.assertIsNone(result[role])

    def test_unknown_installed_mariadb_before_bundled_mysql(self):
        self.discovered['installations'] = [self.groups[2], self.group('11.8.2', 3, engine='mariadb')]
        self.assertEqual(ct.select(self.discovered)['client']['program'], 'mariadb')

    def test_load_follows_dump_installation(self):
        self.groups[2]['programs'].pop('mysql')
        result = self.choose()
        self.assertNotEqual(result['client']['installation'], result['dump']['installation'])
        self.assertIsNone(result['load'])
        self.assertIn('No usable mysql was found.', [m['text'] for m in result['missing'] if m['role'] == 'load'])
        self.groups[2]['programs']['mysql'] = dict(self.groups[0]['programs']['mysql'], version='8.0.1')
        result = self.choose()
        self.assertEqual(result['load']['installation'], result['dump']['installation'])

    def test_load_reason_and_warning_use_client_version_in_mixed_folder(self):
        self.groups[2]['programs']['mysql']['version'] = '8.0.45'
        result = self.choose()
        self.assertEqual(result['dump']['reason'], 'series')
        self.assertEqual(result['load']['reason'], 'older')
        self.assertEqual(result['load']['warning'], 'mysql 8.0.45 is older than this server (MySQL 8.4).')
        self.groups[2]['programs']['mysql']['version'] = '9.7.2'
        result = self.choose('9.7.2')
        self.assertEqual(result['dump']['reason'], 'older')
        self.assertEqual(result['load']['reason'], 'series')
        self.assertIsNone(result['load']['warning'])

    def test_auxiliary_program_ranks_with_server_series_and_choice(self):
        groups = [self.group(v, i, engine='mariadb', names=('mariadb-upgrade',))
                  for i, v in enumerate(('11.4.2', '10.11.10', '11.4.5'))]
        discovered = dict(installations=groups, shells=[])
        for version, index, reason in (('11.4.3', 2, 'series'), ('10.11.9', 1, 'series'), ('11.8.0', 2, 'older')):
            used = ct.select_program(discovered, {'engine': 'mariadb', 'version': version}, 'mariadb-upgrade')
            self.assertEqual(used['installation'], groups[index]['key'])
            self.assertEqual(used['program'], 'mariadb-upgrade')
            self.assertEqual(used['reason'], reason)
        used = ct.select_program(discovered, {'engine': 'mariadb', 'version': '11.4.3'},
                                 'mariadb-upgrade', {'mariadb': groups[1]['key']})
        self.assertEqual(used['installation'], groups[1]['key'])
        self.assertEqual(used['reason'], 'chosen')
        self.assertEqual(discovered['installations'], groups)

    def test_determinism_shuffled_inputs_and_no_mutation(self):
        self.discovered['shells'].append(dict(self.shell, path='/a/mysqlsh', realpath='/a/mysqlsh'))
        original = copy.deepcopy(self.discovered)
        expected = self.choose()
        for i in range(12):
            shuffled = copy.deepcopy(self.discovered)
            random.Random(i).shuffle(shuffled['installations'])
            random.Random(i).shuffle(shuffled['shells'])
            self.assertEqual(ct.select(shuffled, {'engine': 'mysql', 'version': '8.4.3'}), expected)
        self.assertEqual(self.discovered, original)

    def test_managed_shell_selection_wins_independently_of_list_order(self):
        managed = dict(self.shell, path='/managed/mysqlsh', realpath='/managed/mysqlsh',
                       version='8.4.13', version_tuple=(8, 4, 13), origin='managed',
                       origin_label='Downloaded by myflames', order=99)
        for shells in ([self.shell, managed], [managed, self.shell]):
            with self.subTest(shells=shells):
                self.discovered['shells'] = shells
                self.assertEqual(self.choose()['shell']['path'], managed['path'])

    def test_empty_inventory(self):
        result = ct.select(dict(installations=[], shells=[]))
        self.assertIsNone(result['client'])
        self.assertEqual(result['missing'], [])

    def test_origin_table_windows_and_posix(self):
        for folder, origin in (('/opt/local/bin', 'macports'), ('/usr/local/mysql-8.4/bin', 'mysql-installer'),
                               (r'C:\Program Files\MySQL\MySQL Workbench 8.4', 'workbench'),
                               (r'C:\Program Files\MySQL\MySQL Server 8.4\bin', 'mysql-installer'),
                               (r'C:\Program Files\MariaDB 11.8\bin', 'mariadb-installer')):
            self.assertEqual(ct._origin(folder, False)[0], origin)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        with ct._FACTS_LOCK:
            ct._FACTS.clear()

    def test_key_normalization_and_distinct_endpoints(self):
        self.assertEqual(ct.endpoint_key({}), ct.endpoint_key(dict(host=None, port='3306', ssh_port=22)))
        for options in (dict(host='127.0.0.1'), dict(socket='/mysql.sock'), dict(ssh_host='jump'),
                        dict(ssh_user='user'), dict(ssh_port=2222), dict(port=3307)):
            self.assertNotEqual(ct.endpoint_key(options), ct.endpoint_key({}))
        self.assertEqual(ct.endpoint_key(dict(user='a', password='secret')), ct.endpoint_key({}))

    def test_valid_facts_and_unknown_versions_ignored(self):
        key = ct.endpoint_key({})
        for engine, version in (('mysql', '2.7.3'), ('mysql', 'no'), ('unknown', '8.4.3')):
            ct.remember_server(key, engine, version)
            self.assertIsNone(ct.server_facts(key))
        ct.remember_server(key, 'mariadb', '11.8.2-MariaDB')
        facts = ct.server_facts(key)
        self.assertEqual(facts, dict(engine='mariadb', version='11.8.2-MariaDB'))
        facts['engine'] = 'changed'
        ct.remember_server(key, 'mysql', '2.7.3')
        self.assertEqual(ct.server_facts(key)['engine'], 'mariadb')

    def test_lru_eviction_at_256_and_read_recency(self):
        for i in range(256):
            ct.remember_server(i, 'mysql', '8.4.3')
        ct.server_facts(0)
        ct.remember_server(256, 'mysql', '8.4.3')
        self.assertIsNone(ct.server_facts(1))
        self.assertIsNotNone(ct.server_facts(0))
        self.assertEqual(len(ct._FACTS), 256)

    def test_unidentified_server_uses_same_endpoint_key_without_inventing_facts(self):
        key = ct.endpoint_key(dict(host='proxy', port=6032, user='first'))
        ct.remember_unidentified_server(key)
        same_endpoint = ct.endpoint_key(dict(host='proxy', port=6032, user='second',
                                             password='not-a-real-secret', ssl_mode='REQUIRED'))
        self.assertTrue(ct.first_contact_unidentified(same_endpoint))
        self.assertIsNone(ct.server_facts(key))
        for options in (dict(host='proxy', port=6033), dict(host='proxy-alias', port=6032),
                        dict(host='proxy', port=6032, ssh_host='jump')):
            self.assertFalse(ct.first_contact_unidentified(ct.endpoint_key(options)))
        ct.remember_server(key, 'mysql', '8.4.14')
        self.assertFalse(ct.first_contact_unidentified(key))
        self.assertEqual(ct.server_facts(key), dict(engine='mysql', version='8.4.14'))
        # A slower rejected probe cannot erase a successful concurrent read.
        ct.remember_unidentified_server(key)
        self.assertFalse(ct.first_contact_unidentified(key))
        self.assertEqual(ct.server_facts(key), dict(engine='mysql', version='8.4.14'))

    def test_unidentified_servers_share_fact_lru_limit_and_read_recency(self):
        for index in range(256):
            ct.remember_unidentified_server(index)
        self.assertTrue(ct.first_contact_unidentified(0))
        ct.remember_server(256, 'mysql', '8.4.14')
        self.assertFalse(ct.first_contact_unidentified(1))
        self.assertTrue(ct.first_contact_unidentified(0))
        self.assertEqual(len(ct._FACTS), 256)
        # Reading the unknown facts of a retained endpoint is still a use.
        self.assertIsNone(ct.server_facts(2))
        ct.remember_unidentified_server(257)
        self.assertTrue(ct.first_contact_unidentified(2))
        self.assertFalse(ct.first_contact_unidentified(3))

    def test_unidentified_server_and_valid_facts_are_thread_safe(self):
        def worker(index):
            for attempt in range(15):
                key = ct.endpoint_key(dict(host='refusal-%d-%d' % (index, attempt)))
                ct.remember_unidentified_server(key)
                self.assertTrue(ct.first_contact_unidentified(key))
                self.assertIsNone(ct.server_facts(key))
                ct.remember_server(key, 'mariadb', '11.8.2-MariaDB')
                ct.remember_unidentified_server(key)
                self.assertFalse(ct.first_contact_unidentified(key))
                self.assertEqual(ct.server_facts(key),
                                 dict(engine='mariadb', version='11.8.2-MariaDB'))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
        self.assertEqual(len(ct._FACTS), 60)

    def test_thread_safety(self):
        def worker(i):
            key = ct.endpoint_key(dict(host='host' + str(i)))
            for _ in range(25):
                ct.remember_server(key, 'mysql', '8.4.3')
                self.assertEqual(ct.server_facts(key), dict(engine='mysql', version='8.4.3'))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(32)))
        self.assertEqual(len(ct._FACTS), 32)


if __name__ == '__main__':
    unittest.main()
