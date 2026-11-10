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
        folder.mkdir(parents=True, exist_ok=True)
        binary = folder / name
        banner = banner if banner is not None else '%s Ver %s' % (name, version)
        binary.write_text("#!/bin/sh\nprintf '%s\\n' '" + banner.replace("'", "'\\''") + "'\nexit %d\n" % exit_code)
        binary.chmod(0o700)
        return binary

    def discover(self, *folders, **kwargs):
        return ct.discover(path=os.pathsep.join(str(p) for p in folders), common_dirs=(), bundled=False, **kwargs)

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
        links.mkdir(parents=True)
        for name in ('mysql', 'mysqldump'):
            (links / name).symlink_to(self.make(keg, name, '8.4.8'))
            self.make(old, name, '8.0.45')
        opt = brew / 'opt/mysql@8.0'
        opt.parent.mkdir()
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

    def test_shells_separate_and_newest_first(self):
        for i, version in enumerate(('8.4.13', '9.6.0')):
            self.make(self.root / str(i), 'mysqlsh', banner='mysqlsh ' + version)
        report = self.discover(self.root / '0', self.root / '1')
        self.assertEqual(report['installations'], [])
        self.assertEqual([s['version'] for s in report['shells']], ['9.6.0', '8.4.13'])

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
        self.assertEqual(result['client']['version'], '8.4.8')
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
