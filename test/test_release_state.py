"""Offline release guards and orchestration. No Apple services or real signing.

The shell integration lab puts fakes ahead of PATH for every Apple/packaging
tool. It uses synthetic artifacts and isolated HOME, and never launches an app.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'apps' / 'desktop' / 'scripts'


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script('release_state_tests', SCRIPTS / 'release_state.py')
fetcher = load_script('release_client_tests', SCRIPTS / 'fetch-mysql-client.py')


class ReleaseEntryPointTest(unittest.TestCase):
    """Static dependency guards: never install, build or call Apple services."""

    DESKTOP_INSTALL = r"^python3 scripts/release_state\.py run --label '[^'\n]+' -- npm ci$"
    WORKSPACE_INSTALL = (r"^PUPPETEER_SKIP_DOWNLOAD=true python3 scripts/release_state\.py run "
                         r"--label '[^'\n]+' -- npm --prefix \"\$root/apps/workspace\" ci$")
    WORKSPACE_BUILD = (r"^NODE_ENV=production python3 scripts/release_state\.py run --label '[^'\n]+' "
                       r"-- npm --prefix \"\$root/apps/workspace\" run build$")

    def assert_unconditional_install(self, script, pattern):
        install = re.search(pattern, script, re.MULTILINE)
        self.assertIsNotNone(install, 'release must always install the locked dependencies')
        before = script[:install.start()]
        self.assertEqual(len(re.findall(r'(?m)^\s*if\b', before)),
                         len(re.findall(r'(?m)^\s*fi\b', before)),
                         'dependency installation must be outside every conditional')
        self.assertLess(install.start(), script.index('if [ "$resume" = --resume ] && '),
                        'install dependencies before the backend build or resume decision')
        return install

    def test_desktop_dependencies_always_install_before_backend_build(self):
        script = (SCRIPTS / 'release-mac.sh').read_text(encoding='utf-8')
        self.assert_unconditional_install(script, self.DESKTOP_INSTALL)
        self.assertNotIn('node_modules/.bin/electron-builder', script,
                         'an existing builder does not prove dependencies match the lockfile')

    def test_workspace_dependencies_always_install_after_desktop_before_backend(self):
        script = (SCRIPTS / 'release-mac.sh').read_text(encoding='utf-8')
        workspace = self.assert_unconditional_install(script, self.WORKSPACE_INSTALL)
        desktop = self.assert_unconditional_install(script, self.DESKTOP_INSTALL)
        self.assertLess(desktop.start(), workspace.start(), 'install desktop dependencies first')

    def test_workspace_build_precedes_backend_build_or_resume_and_output_is_rechecked(self):
        script = (SCRIPTS / 'release-mac.sh').read_text(encoding='utf-8')
        install = self.assert_unconditional_install(script, self.WORKSPACE_INSTALL)
        build = self.assert_unconditional_install(script, self.WORKSPACE_BUILD)
        self.assertLess(install.start(), build.start())
        self.assertLess(script.index('git tree changed during the build'),
                        script.rindex('python3 scripts/release_state.py manifest-check --root "$root"'))
        packager = (SCRIPTS / 'package-mac-release.sh').read_text(encoding='utf-8')
        self.assertLess(packager.index('state manifest-check'), packager.index('if needs app; then'))
        self.assertGreater(packager.rindex('state manifest-check'), packager.index('state complete checksums'))

    def test_backend_refuses_invalid_assets_before_any_build_subprocess(self):
        for reason in ('missing index.html', 'stale source stamp'):
            error = reason + ': run `npm ci` and `npm run build` in apps/workspace'
            with self.subTest(reason=reason), mock.patch.dict(sys.modules, {'release_state': release}), \
                    mock.patch.object(release, 'check_ui_assets', side_effect=ValueError(error)), \
                    mock.patch.object(subprocess, 'call') as build, contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_path(str(SCRIPTS / 'build-backend.py'), run_name='__main__')
                self.assertEqual(caught.exception.code, 2)
                self.assertIn('Cannot build the backend: ' + error, stderr.getvalue())
                build.assert_not_called()

    def test_workflow_workspace_builds_force_production_and_publish_compares_reference(self):
        for name in ('publish.yml', 'macos-desktop.yml', 'windows-desktop.yml'):
            workflow = (ROOT / '.github' / 'workflows' / name).read_text(encoding='utf-8')
            build = workflow.split('- name: Build the workspace\n', 1)[1].split('\n      - ', 1)[0]
            with self.subTest(workflow=name):
                self.assertRegex(build, r'(?m)^\s+NODE_ENV=production npm run build$')
        publish = (ROOT / '.github' / 'workflows' / 'publish.yml').read_text(encoding='utf-8')
        self.assertIn('python scripts/check_ui_assets.py dist/*.whl dist/*.tar.gz --reference myflames/ui_assets',
                      publish)


class ReleaseStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / 'executable').write_bytes(b'app')
        self.dmg, self.zip = self.root / 'test.dmg', self.root / 'test.zip'
        self.dmg.write_bytes(b'unsigned dmg')
        self.zip.write_bytes(b'zip')
        self.inputs = {'commit': 'a' * 40, 'identity': 'B' * 40, 'ui_assets_sha256': 'c' * 64}
        self.state = release.new_state(self.inputs)

    def app_done(self):
        release.complete_stage(self.state, 'app', [self.app, self.dmg, self.zip])

    def test_stage_order_is_the_release_contract(self):
        self.assertEqual(release.STAGES, ('app', 'verify-app', 'dmg-notarize', 'staple',
                                          'verify', 'size', 'checksums'))
        with self.assertRaisesRegex(ValueError, 'out of order'):
            release.complete_stage(self.state, 'verify-app', [self.app])

    def test_resume_first_incomplete_and_stage_records_hashes_and_inputs(self):
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'app')
        self.app_done()
        record = self.state['stages']['app']
        self.assertEqual(record['inputs'], self.inputs)
        self.assertEqual(record['outputs'][str(self.dmg)], hashlib.sha256(b'unsigned dmg').hexdigest())
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'verify-app')
        release.complete_stage(self.state, 'verify-app', [self.app])
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'dmg-notarize')

    def test_each_stage_completes_in_order_and_fully_complete_resume_is_none(self):
        for stage in release.STAGES:
            release.complete_stage(self.state, stage, [self.app, self.dmg, self.zip])
        self.assertIsNone(release.resume_stage(self.state, self.inputs))

    def test_earlier_changed_output_restarts_its_producer(self):
        self.app_done()
        release.complete_stage(self.state, 'verify-app', [self.app])
        (self.app / 'executable').write_bytes(b'changed')
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'app')

    def test_deleted_output_restarts_its_producer(self):
        self.app_done()
        self.zip.unlink()
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'app')

    def test_identity_or_manifest_input_change_restarts_app(self):
        self.app_done()
        for field in ('identity', 'commit', 'ui_assets_sha256'):
            changed = dict(self.inputs, **{field: 'changed'})
            self.assertEqual(release.resume_stage(self.state, changed), 'app')

    def test_missing_stage_cannot_be_skipped_by_later_completion(self):
        self.app_done()
        self.state['stages']['verify'] = copy.deepcopy(self.state['stages']['app'])
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'verify-app')

    def test_signed_pending_dmg_does_not_invalidate_app_snapshot(self):
        self.app_done()
        release.complete_stage(self.state, 'verify-app', [self.app])
        original = self.state['stages']['app']['outputs'][str(self.dmg)]
        self.dmg.write_bytes(b'signed dmg')
        release.checkpoint(self.state, 'dmg-notarize', [self.dmg], 'submission')
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'dmg-notarize')
        self.assertEqual(self.state['stages']['app']['outputs'][str(self.dmg)], original)
        self.assertEqual(self.state['pending']['submission_id'], 'submission')

    def test_stapled_dmg_supersedes_earlier_hashes_but_corruption_rebuilds_app(self):
        self.app_done()
        release.complete_stage(self.state, 'verify-app', [self.app])
        self.dmg.write_bytes(b'signed dmg')
        release.checkpoint(self.state, 'dmg-notarize', [self.dmg], 'submission')
        release.complete_stage(self.state, 'dmg-notarize', [self.dmg])
        self.dmg.write_bytes(b'stapled signed dmg')
        release.checkpoint(self.state, 'staple', [self.dmg])
        release.complete_stage(self.state, 'staple', [self.dmg])
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'verify')
        self.dmg.write_bytes(b'corrupted stapled dmg')
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'app')

    def test_uncheckpointed_partial_mutation_is_conservatively_rebuilt(self):
        self.app_done()
        release.complete_stage(self.state, 'verify-app', [self.app])
        self.dmg.write_bytes(b'crashed during signing')
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'app')

    def test_checksum_corruption_restarts_only_checksums(self):
        for stage in release.STAGES[:-1]:
            release.complete_stage(self.state, stage, [self.app, self.dmg, self.zip])
        checksum = self.root / 'test.dmg.sha256'
        checksum.write_bytes(b'checksum')
        release.complete_stage(self.state, 'checksums', [checksum])
        checksum.write_bytes(b'wrong checksum')
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'checksums')

    def complete_through_staple(self):
        """The signed path: codesign and stapling legitimately rewrite the DMG."""
        self.app_done()
        release.complete_stage(self.state, 'verify-app', [self.app])
        self.dmg.write_bytes(b'signed dmg')
        release.checkpoint(self.state, 'dmg-notarize', [self.dmg], 'submission')
        release.complete_stage(self.state, 'dmg-notarize', [self.dmg])
        self.dmg.write_bytes(b'stapled dmg')
        release.checkpoint(self.state, 'staple', [self.dmg])
        release.complete_stage(self.state, 'staple', [self.dmg])

    def test_read_only_stages_refuse_a_dmg_replaced_after_staple(self):
        self.complete_through_staple()
        self.dmg.write_bytes(b'tampered after stapling')
        with self.assertRaises(ValueError) as caught:
            release.complete_stage(self.state, 'verify', [self.app, self.dmg, self.zip])
        self.assertIn('verify', str(caught.exception))
        self.assertIn(str(self.dmg), str(caught.exception))
        self.assertNotIn('verify', self.state['stages'])

    def test_checksums_refuses_a_zip_replaced_after_verify(self):
        self.complete_through_staple()
        release.complete_stage(self.state, 'verify', [self.app, self.dmg, self.zip])
        release.complete_stage(self.state, 'size', [self.dmg, self.zip])
        # The attack: .sha256 files would be written for bytes verify never saw.
        self.zip.write_bytes(b'tampered after size')
        with self.assertRaises(ValueError) as caught:
            release.complete_stage(self.state, 'checksums', [self.root / 'test.zip.sha256'])
        self.assertIn('checksums', str(caught.exception))
        self.assertIn(str(self.zip), str(caught.exception))
        self.assertNotIn('checksums', self.state['stages'])

    def test_writers_may_replace_only_the_dmg(self):
        self.app_done()
        release.complete_stage(self.state, 'verify-app', [self.app])
        self.dmg.write_bytes(b'signed dmg')
        self.zip.write_bytes(b'tampered zip')
        with self.assertRaises(ValueError) as caught:
            release.complete_stage(self.state, 'dmg-notarize', [self.dmg])
        self.assertIn('dmg-notarize', str(caught.exception))
        self.assertIn(str(self.zip), str(caught.exception))
        self.zip.write_bytes(b'zip')
        release.complete_stage(self.state, 'dmg-notarize', [self.dmg])
        self.dmg.write_bytes(b'stapled dmg')
        release.complete_stage(self.state, 'staple', [self.dmg])
        self.assertEqual(release.resume_stage(self.state, self.inputs), 'verify')

    def test_read_only_stages_refuse_a_missing_consumed_artifact(self):
        self.app_done()
        self.zip.unlink()
        with self.assertRaisesRegex(ValueError, 'verify-app'):
            release.complete_stage(self.state, 'verify-app', [self.app])

    def test_normal_path_still_completes_with_the_consumed_check(self):
        self.complete_through_staple()
        release.complete_stage(self.state, 'verify', [self.app, self.dmg, self.zip])
        release.complete_stage(self.state, 'size', [self.dmg, self.zip])
        checksum = self.root / 'test.dmg.sha256'
        checksum.write_bytes(b'checksum')
        release.complete_stage(self.state, 'checksums', [checksum])
        self.assertIsNone(release.resume_stage(self.state, self.inputs))

    def test_tree_hash_detects_rename_mode_and_link_target_but_ignores_mtime(self):
        path = self.app / 'executable'
        before = release.hash_path(self.app)
        os.utime(str(path), (1, 1))
        self.assertEqual(release.hash_path(self.app), before)
        path.chmod(0o755)
        self.assertNotEqual(release.hash_path(self.app), before)
        renamed = self.app / 'renamed'
        before = release.hash_path(self.app)
        path.rename(renamed)
        self.assertNotEqual(release.hash_path(self.app), before)
        link = self.app / 'framework'
        link.symlink_to('renamed')
        before = release.hash_path(self.app)
        link.unlink()
        link.symlink_to('elsewhere')
        self.assertNotEqual(release.hash_path(self.app), before)

    def test_tree_hash_is_independent_of_creation_order_and_rejects_root_link(self):
        other = self.root / 'other'
        other.mkdir()
        (other / 'z').write_bytes(b'z')
        (other / 'a').write_bytes(b'a')
        tree = self.root / 'tree'
        tree.mkdir()
        (tree / 'a').write_bytes(b'a')
        (tree / 'z').write_bytes(b'z')
        self.assertEqual(release.hash_path(other), release.hash_path(tree))
        link = self.root / 'link'
        link.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'not a regular file or directory'):
            release.hash_path(link)

    def test_state_atomic_write_refuses_symlink(self):
        target = self.root / 'unrelated'
        target.write_bytes(b'preserve')
        link = self.root / 'state'
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            release.write_json(link, self.state)
        self.assertEqual(target.read_bytes(), b'preserve')

    def test_manifest_accepts_only_matching_commit_ui_and_requirements(self):
        manifest = {'commit': 'a' * 40, 'ui_assets_sha256': 'b' * 64, 'requirements_sha256': 'c' * 64}
        self.assertEqual(release.validate_manifest(manifest, 'a' * 40, 'b' * 64, 'c' * 64), manifest)
        for commit, ui, requirements, message in (
                ('d' * 40, 'b' * 64, 'c' * 64, 'commit'),
                ('a' * 40, 'd' * 64, 'c' * 64, 'ui_assets'),
                ('a' * 40, 'b' * 64, 'd' * 64, 'requirements'),
                (None, 'b' * 64, 'c' * 64, 'commit')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                release.validate_manifest(manifest, commit, ui, requirements)

    def build_backend_manifest(self, freeze=('PyInstaller==6.22.3',), asset_change=None):
        desktop = self.root / 'apps' / 'desktop'
        backend = desktop / 'build' / 'backend'
        (backend / 'myflames-backend').mkdir(parents=True)
        (backend / 'myflames-backend' / 'binary').write_bytes(b'backend')
        (backend / 'Python-LICENSE.txt').write_bytes(b'license')
        requirements = desktop / 'requirements-build.txt'
        requirements.write_bytes(b'PyInstaller==6.22.3\n')
        assets = self.build_workspace()
        bundled = backend / 'myflames-backend' / '_internal' / 'myflames' / 'ui_assets'
        shutil.copytree(str(assets), str(bundled))
        if asset_change == 'missing':
            (bundled / 'assets' / 'lazy.js').unlink()
        elif asset_change == 'altered':
            (bundled / 'assets' / 'lazy.js').write_bytes(b'altered bundled chunk')
        elif asset_change == 'extra':
            (bundled / 'assets' / 'extra.js').write_bytes(b'extra bundled chunk')
        before = {'commit': 'a' * 40, 'ui_assets_sha256': release.ui_assets_digest(self.root),
                  'requirements_sha256': hashlib.sha256(requirements.read_bytes()).hexdigest()}
        with mock.patch.object(release, 'git_head', return_value='a' * 40):
            release.build_manifest(self.root, before, list(freeze))
        return backend

    def build_workspace(self):
        """A small build with a real source stamp, independent of repo output."""
        sources_sha = load_script('check_ui_assets_tests', ROOT / 'scripts' / 'check_ui_assets.py').sources_sha
        for name in ('src/main.ts', 'index.html', 'vite.config.ts', 'tsconfig.json', 'package-lock.json'):
            path = self.root / 'apps' / 'workspace' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'workspace source')
        logo = self.root / 'docs' / 'brand' / 'myflames-app-logo.png'
        logo.parent.mkdir(parents=True, exist_ok=True)
        logo.write_bytes(b'logo')
        assets = self.root / 'myflames' / 'ui_assets'
        (assets / 'assets').mkdir(parents=True, exist_ok=True)
        (assets / 'assets' / 'app.js').write_bytes(b'freshly built workspace')
        (assets / 'assets' / 'lazy.js').write_bytes(b'freshly built lazy chunk')
        (assets / 'index.html').write_text(
            ('<meta name="myflames-sources-sha256" content="%s">'
             '<script src="/assets/app.js"></script>') % sources_sha(self.root), encoding='utf-8')
        return assets

    def build_venv(self, python_version=None, freeze=('PyInstaller==6.22.3',)):
        """Mock the build venv answers check_manifest asks its interpreter for."""
        version = python_version or '%d.%d' % sys.version_info[:2]
        return (mock.patch.object(release, 'venv_python_version', return_value=version),
                mock.patch.object(release, 'venv_freeze', return_value=list(freeze)))

    def test_build_manifest_roundtrip_checks_backend_and_tree(self):
        backend = self.build_backend_manifest()
        version, freeze = self.build_venv()
        with mock.patch.object(release, 'git_head', return_value='a' * 40), version, freeze:
            manifest = release.check_manifest(self.root)
            self.assertEqual(manifest['python_version'], sys.version.split()[0])
            self.assertIn('+00:00', manifest['built_at'])
            (backend / 'myflames-backend' / 'binary').write_bytes(b'stale binary')
            with self.assertRaisesRegex(ValueError, 'rebuild the backend with release-mac.sh'):
                release.check_manifest(self.root)

    def test_build_manifest_refuses_missing_extra_or_altered_bundled_chunks(self):
        for change in ('missing', 'extra', 'altered'):
            with self.subTest(change=change):
                backend = self.root / 'apps' / 'desktop' / 'build' / 'backend'
                if backend.exists():
                    backend.rename(backend.with_name('previous-' + change))
                with self.assertRaisesRegex(ValueError, 'bundled workspace assets.*' + change):
                    self.build_backend_manifest(asset_change=change)
                self.assertFalse((backend / 'build-manifest.json').exists())

    def test_resume_rechecks_bundled_chunks_even_if_backend_digest_is_updated(self):
        backend = self.build_backend_manifest()
        binary = backend / 'myflames-backend'
        chunk = binary / '_internal' / 'myflames' / 'ui_assets' / 'assets' / 'lazy.js'
        original = chunk.read_bytes()
        for change in ('missing', 'extra', 'altered'):
            with self.subTest(change=change):
                chunk.write_bytes(original)
                extra = chunk.with_name('extra.js')
                if extra.exists():
                    extra.unlink()
                if change == 'missing':
                    chunk.unlink()
                elif change == 'extra':
                    extra.write_bytes(b'extra chunk')
                else:
                    chunk.write_bytes(b'altered bytes')
                manifest = release.read_json(backend / 'build-manifest.json')
                manifest['backend_sha256'] = release.hash_path(binary)
                release.write_json(backend / 'build-manifest.json', manifest)
                version, freeze = self.build_venv()
                with mock.patch.object(release, 'git_head', return_value='a' * 40), version, freeze:
                    with self.assertRaisesRegex(ValueError, 'bundled workspace assets.*' + change):
                        release.check_manifest(self.root)

    def test_manifest_refuses_a_different_build_python_minor(self):
        self.build_backend_manifest()
        version, freeze = self.build_venv(python_version='%d.%d' % (sys.version_info[0],
                                                                    sys.version_info[1] + 1))
        with mock.patch.object(release, 'git_head', return_value='a' * 40), version, freeze:
            with self.assertRaisesRegex(ValueError, 'rebuild the backend with release-mac.sh'):
                release.check_manifest(self.root)

    def test_manifest_refuses_changed_build_dependencies(self):
        self.build_backend_manifest()
        version, freeze = self.build_venv(freeze=('PyInstaller==6.22.3', 'altgraph==0.17.4'))
        with mock.patch.object(release, 'git_head', return_value='a' * 40), version, freeze:
            with self.assertRaisesRegex(ValueError, 'rebuild the backend with release-mac.sh'):
                release.check_manifest(self.root)

    def test_manifest_without_freeze_provenance_is_refused(self):
        backend = self.build_backend_manifest()
        manifest = release.read_json(backend / 'build-manifest.json')
        for key in ('build_freeze', 'build_freeze_sha256'):
            manifest.pop(key)
        release.write_json(backend / 'build-manifest.json', manifest)
        version, freeze = self.build_venv()
        with mock.patch.object(release, 'git_head', return_value='a' * 40), version, freeze:
            with self.assertRaisesRegex(ValueError, 'rebuild the backend with release-mac.sh'):
                release.check_manifest(self.root)

    def test_venv_freeze_is_sorted_and_stripped(self):
        output = b'zope.interface==7.2\n\naltgraph==0.17.4\nPyInstaller==6.22.3\n'
        with mock.patch.object(release.subprocess, 'check_output', return_value=output) as run:
            freeze = release.venv_freeze('/fake/venv/bin/python')
        self.assertEqual(freeze, ['PyInstaller==6.22.3', 'altgraph==0.17.4', 'zope.interface==7.2'])
        self.assertEqual(run.call_args[0][0][1:], ['-m', 'pip', 'freeze'])
        self.assertEqual(release.freeze_digest(freeze), hashlib.sha256(
            b'PyInstaller==6.22.3\naltgraph==0.17.4\nzope.interface==7.2\n').hexdigest())

    def test_build_venv_python_follows_the_override_or_the_default(self):
        relative = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
        with mock.patch.dict(os.environ, {'MYFLAMES_DESKTOP_BUILD_VENV': str(self.root / 'elsewhere')}):
            self.assertEqual(release.build_venv_python(self.root), self.root / 'elsewhere' / relative)
        clean = {key: value for key, value in os.environ.items()
                 if key != 'MYFLAMES_DESKTOP_BUILD_VENV'}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertEqual(release.build_venv_python(self.root),
                             self.root / 'local' / 'desktop-build-venv' / relative)

    def test_changed_ui_during_build_refuses_manifest(self):
        desktop = self.root / 'apps' / 'desktop'
        desktop.mkdir(parents=True)
        (desktop / 'requirements-build.txt').write_bytes(b'requirements')
        assets = self.build_workspace()
        before = {'commit': 'a' * 40, 'ui_assets_sha256': release.ui_assets_digest(self.root),
                  'requirements_sha256': release.sha256_file(desktop / 'requirements-build.txt')}
        (assets / 'assets' / 'app.js').write_bytes(b'changed during backend build')
        with mock.patch.object(release, 'git_head', return_value='a' * 40):
            with self.assertRaisesRegex(ValueError, 'inputs changed while building'):
                release.build_manifest(self.root, before, [])
        self.assertFalse((desktop / 'build' / 'backend' / 'build-manifest.json').exists())

    def test_changed_sources_during_build_refuse_manifest(self):
        backend = self.build_backend_manifest()
        before = release.read_json(backend / 'build-manifest.json')
        before = {key: before[key] for key in ('commit', 'ui_assets_sha256', 'requirements_sha256')}
        (backend / 'build-manifest.json').unlink()
        (self.root / 'apps/workspace/src/main.ts').write_bytes(b'edited during build')
        with mock.patch.object(release, 'git_head', return_value='a' * 40):
            with self.assertRaisesRegex(ValueError, 'stale.*npm run build'):
                release.build_manifest(self.root, before, [])
        self.assertFalse((backend / 'build-manifest.json').exists())

    def test_resume_rejects_changed_sources_even_when_output_is_unchanged(self):
        self.build_backend_manifest()
        (self.root / 'apps/workspace/src/main.ts').write_bytes(b'unbuilt source edit')
        version, freeze = self.build_venv()
        with mock.patch.object(release, 'git_head', return_value='a' * 40), version, freeze:
            with self.assertRaisesRegex(ValueError, 'stale.*rebuild the backend'):
                release.check_manifest(self.root)

    def test_venv_key_uses_major_minor_and_requirement_bytes(self):
        requirements = self.root / 'requirements'
        requirements.write_bytes(b'PyInstaller==6.22.3\n')
        expected = {'python': '3.14', 'requirements_sha256': hashlib.sha256(b'PyInstaller==6.22.3\n').hexdigest()}
        self.assertEqual(release.venv_key('3.14.4', requirements), expected)
        self.assertEqual(release.venv_key('3.14.9', requirements), expected)
        self.assertNotEqual(release.venv_key('3.15.0', requirements), expected)
        requirements.write_bytes(b'changed')
        self.assertNotEqual(release.venv_key('3.14.4', requirements), expected)

    def test_venv_reuses_valid_stamp_and_recreates_stale_stamp(self):
        desktop = self.root / 'apps' / 'desktop'
        desktop.mkdir(parents=True)
        requirements = desktop / 'requirements-build.txt'
        requirements.write_bytes(b'pinned')
        venv = self.root / 'build-venv'
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        calls = []

        def fake_run(command, label):
            calls.append(command)
            if command[1:3] == ['-m', 'venv']:
                python.parent.mkdir(parents=True)
                python.write_bytes(b'fake Python')
                (venv / 'pyvenv.cfg').write_bytes(b'fake venv')

        with mock.patch.object(release, 'run_command', side_effect=fake_run):
            release.provision_venv(self.root, venv)
            self.assertEqual(len(calls), 2)
            current = ('%d.%d\n' % sys.version_info[:2]).encode('ascii')
            with mock.patch.object(release.subprocess, 'check_output', return_value=current):
                release.provision_venv(self.root, venv)
            self.assertEqual(len(calls), 2, 'valid venv must not reinstall')
            requirements.write_bytes(b'changed pin')
            release.provision_venv(self.root, venv)
            self.assertEqual(len(calls), 4, 'stale stamp must recreate and reinstall')
            self.assertEqual(calls[-1][-1], str(requirements))

    def test_venv_override_cannot_delete_an_unrelated_folder(self):
        desktop = self.root / 'apps' / 'desktop'
        desktop.mkdir(parents=True)
        (desktop / 'requirements-build.txt').write_bytes(b'pinned')
        venv = self.root / 'important'
        venv.mkdir()
        (venv / 'keep').write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'non-venv'):
            release.provision_venv(self.root, venv)
        self.assertEqual((venv / 'keep').read_bytes(), b'keep')


class NotarizationOriginTest(unittest.TestCase):
    FIND_IDENTITY = ('  1) 0123456789abcdef0123456789abcdef01234567 '
                     '"Developer ID Application: Example Name (AB12CD34EF)"\n')
    SPCTL = ('dist/myflames.app: accepted\n'
             'source=Notarized Developer ID\n'
             'origin=Developer ID Application: Example Name (AB12CD34EF)\n')

    def test_team_id_comes_from_the_matched_identity_name(self):
        self.assertEqual(release.identity_team_id(self.FIND_IDENTITY), 'AB12CD34EF')
        with self.assertRaisesRegex(ValueError, 'team ID'):
            release.identity_team_id('  1) 0123 "Developer ID Application: No Team"\n')
        with self.assertRaisesRegex(ValueError, 'team ID'):
            release.identity_team_id('  1) 0123 "Developer ID Application: Lower (ab12cd34ef)"\n')

    def test_notarized_origin_must_name_the_signing_team(self):
        release.check_notarized_origin(self.SPCTL, 'AB12CD34EF')
        other_team = self.SPCTL.replace('AB12CD34EF', 'ZZ99YY88XX')
        with self.assertRaisesRegex(ValueError, 'ZZ99YY88XX'):
            release.check_notarized_origin(other_team, 'AB12CD34EF')

    def test_a_missing_origin_is_not_trusted(self):
        without_origin = 'dist/myflames.app: accepted\nsource=Notarized Developer ID\n'
        with self.assertRaisesRegex(ValueError, 'origin'):
            release.check_notarized_origin(without_origin, 'AB12CD34EF')

    def test_the_notarized_source_is_still_required(self):
        signed_only = ('dist/myflames.app: accepted\nsource=Developer ID\n'
                       'origin=Developer ID Application: Example Name (AB12CD34EF)\n')
        with self.assertRaisesRegex(ValueError, 'Notarized Developer ID'):
            release.check_notarized_origin(signed_only, 'AB12CD34EF')


class NotaryRetryTest(unittest.TestCase):
    def test_notary_error_classification(self):
        for text, expected in (
                ('Error: HTTP status code: 403. A required agreement is missing or has expired.', 'agreement'),
                ('Error: HTTP status code: 401. Invalid credentials; timeout', 'credentials'),
                ('No Keychain password item found for profile', 'credentials'),
                ('{"status":"Invalid", "message":"timeout during validation"}', 'rejected'),
                ('{"status":"Rejected"}', 'rejected'),
                ('Error: Could not resolve host: appstoreconnect.apple.com', 'transport'),
                ('NSURLErrorDomain Code=-1003 A server with the specified hostname could not be found.', 'transport'),
                ('Connection reset by peer', 'transport'),
                ('The request timed out', 'transport'),
                ('Error: HTTP status code: 503. Service Unavailable', 'transport'),
                ('Error: HTTP status code: 400. Bad Request', 'fatal'),
                ('{"status":"Accepted"}', 'fatal')):
            with self.subTest(text=text):
                self.assertEqual(release.classify_notary_output(text), expected)

    def test_transport_retry_is_bounded_to_three_attempts(self):
        for operation in ('submit', 'wait', 'info'):
            runner = mock.Mock(side_effect=[(1, '', 'DNS resolution failed'),
                                             (1, '', 'HTTP status code: 503'),
                                             (0, '{"status":"Accepted"}', '')])
            sleep = mock.Mock()
            with contextlib.redirect_stderr(io.StringIO()):
                result = release.run_notary([operation, 'fake'], runner=runner, sleeper=sleep)
            self.assertEqual(result, '{"status":"Accepted"}')
            self.assertEqual(runner.call_count, 3)
            self.assertEqual(sleep.call_args_list, [mock.call(10), mock.call(30)])

    def test_exhausted_transport_retry_fails(self):
        runner = mock.Mock(return_value=(1, '', 'Connection reset by peer'))
        sleep = mock.Mock()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, 'wait failed'):
            release.run_notary(['wait', 'id'], runner=runner, sleeper=sleep)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_agreement_credentials_and_rejection_never_retry(self):
        for message, fix in (
                ('Error: HTTP status code: 403. A required agreement is missing or has expired.',
                 'accept the agreement at developer.apple.com'),
                ('Error: HTTP status code: 401. Invalid credentials', 'credentials were refused'),
                ('{"status":"Invalid"}', 'rejected')):
            runner = mock.Mock(return_value=(1, '', message))
            sleep = mock.Mock()
            output = io.StringIO()
            with contextlib.redirect_stderr(output), self.assertRaisesRegex(ValueError, fix):
                release.run_notary(['submit', 'fake'], runner=runner, sleeper=sleep)
            self.assertIn(message, output.getvalue(), 'preserve the exact Apple error')
            self.assertEqual(runner.call_count, 1)
            sleep.assert_not_called()

    def test_notary_diagnostics_redact_credentials(self):
        env = {'APPLE_API_KEY': '/private/fake-key.p8', 'APPLE_API_KEY_ID': 'FAKE-SECRET-ID',
               'APPLE_API_ISSUER': 'FAKE-SECRET-ISSUER', 'APPLE_KEYCHAIN_PROFILE': 'FAKE-SECRET-PROFILE'}
        runner = mock.Mock(return_value=(1, '', 'Invalid credentials: ' + ' '.join(env.values())))
        output = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(output):
            with self.assertRaises(ValueError):
                release.run_notary(['info', 'id'], runner=runner)
        for value in env.values():
            self.assertNotIn(value, output.getvalue())

    def test_submission_id_is_durable_before_wait_and_resume_uses_info(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as folder:
            path, dmg = Path(folder) / 'state.json', Path(folder) / 'test.dmg'
            dmg.write_bytes(b'signed')
            state = release.new_state({'commit': 'a' * 40, 'identity': 'B' * 40})
            release.complete_stage(state, 'app', [dmg])
            release.complete_stage(state, 'verify-app', [dmg])
            release.checkpoint(state, 'dmg-notarize', [dmg])
            release.write_json(path, state)
            submission = '12345678-1234-1234-1234-123456789abc'
            calls = []

            def late_failure(arguments):
                calls.append(arguments)
                if arguments[0] == 'submit':
                    return json.dumps({'id': submission, 'status': 'In Progress'})
                saved = release.read_json(path)
                self.assertEqual(saved['pending']['submission_id'], submission)
                raise ValueError('late transport failure')

            with mock.patch.object(release, 'run_notary', side_effect=late_failure):
                with self.assertRaisesRegex(ValueError, 'late transport failure'):
                    release.notarize_dmg(path, dmg)
            self.assertEqual([call[0] for call in calls], ['submit', 'wait'])
            self.assertEqual(release.resume_stage(release.read_json(path), state['inputs']), 'dmg-notarize')
            with mock.patch.object(release, 'run_notary', return_value='{"status":"Accepted"}') as notary:
                release.notarize_dmg(path, dmg)
            self.assertEqual(notary.call_args_list, [mock.call(['info', submission, '--output-format', 'json'])])
            self.assertEqual(release.read_json(path)['pending']['status'], 'Accepted')

    def test_notary_progress_checks_child_every_30_seconds(self):
        child = mock.Mock(returncode=0)
        child.communicate.side_effect = [subprocess.TimeoutExpired('fake', 30), (b'{"status":"Accepted"}', b'')]
        child.poll.return_value = 0
        output = io.StringIO()
        with mock.patch.object(release.subprocess, 'Popen', return_value=child), \
                mock.patch.object(release, '_notary_credentials', return_value=[]), \
                contextlib.redirect_stderr(output):
            release._notary_once(['wait', 'fake'])
        self.assertEqual(child.communicate.call_args_list, [mock.call(timeout=30), mock.call(timeout=30)])
        self.assertIn('wait still running (30 s)', output.getvalue())


class ClientRetryTest(unittest.TestCase):
    def test_download_retries_transport_only(self):
        dns = urllib.error.URLError(socket.gaierror(-2, 'Name or service not known'))
        unavailable = urllib.error.HTTPError('https://cdn.mysql.com/fake', 503, 'unavailable', {}, None)
        with mock.patch.object(fetcher, '_download_once', side_effect=[dns, unavailable, 'verified-later']) as download, \
                mock.patch.object(fetcher.time, 'sleep') as sleep, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fetcher.download('fake', 10, 'target'), 'verified-later')
        self.assertEqual(download.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(10), mock.call(30)])

    def test_download_4xx_and_integrity_failure_never_retry(self):
        for error in (urllib.error.HTTPError('https://cdn.mysql.com/fake', 403, 'forbidden', {}, None),
                      fetcher.Refused('larger than the lock allows')):
            with mock.patch.object(fetcher, '_download_once', side_effect=error) as download, \
                    mock.patch.object(fetcher.time, 'sleep') as sleep:
                with self.assertRaises(type(error)):
                    fetcher.download('fake', 10, 'target')
            self.assertEqual(download.call_count, 1)
            sleep.assert_not_called()

    def test_redownloaded_hash_mismatch_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as folder:
            target = Path(folder) / 'client.tar.gz'
            target.write_bytes(b'bad cache')
            entry = {'url': 'https://cdn.mysql.com/client.tar.gz', 'size': 4,
                     'sha256': hashlib.sha256(b'good').hexdigest()}

            def download(url, size, output):
                partial = Path(folder) / 'partial'
                partial.write_bytes(b'evil')
                return str(partial)

            with mock.patch.object(fetcher, 'download', side_effect=download) as retry, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(fetcher.Refused, 'refusing'):
                    fetcher.cached_archive(entry, folder)
            self.assertEqual(retry.call_count, 1)
            self.assertFalse(target.exists())
            self.assertFalse((Path(folder) / 'partial').exists())


if __name__ == '__main__':
    unittest.main()
