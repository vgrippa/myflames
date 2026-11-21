"""Check optional workspace output and the required packaging guards."""
import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Load the checker by file path: a top-level `scripts` package elsewhere on
# sys.path must not shadow it.
_spec = importlib.util.spec_from_file_location('check_ui_assets', str(REPO / 'scripts' / 'check_ui_assets.py'))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
check_ui_assets = _module.check_ui_assets
sources_sha = _module.sources_sha


class WorkspaceAssetsTests(unittest.TestCase):
    def test_built_assets_match_sources(self):
        output = REPO / 'myflames' / 'ui_assets'
        if ((not output.exists() and not output.is_symlink()) or
                (output.is_dir() and not any(output.iterdir()))):
            self.skipTest('Workspace assets are not built; run `npm ci` and `npm run build` in apps/workspace')
        check_ui_assets(REPO / 'myflames')

    def test_completely_absent_build_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(globals(), {'REPO': Path(directory)}):
            with self.assertRaises(unittest.SkipTest):
                self.test_built_assets_match_sources()
            (Path(directory) / 'myflames' / 'ui_assets').mkdir(parents=True)
            with self.assertRaises(unittest.SkipTest):
                self.test_built_assets_match_sources()

    def test_partial_build_is_checked_instead_of_skipped(self):
        for component in ('index.html', 'assets', 'empty-assets', 'other.txt', '.hidden',
                          'output-file', 'broken-output-link', 'broken-index-link'):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / 'myflames' / 'ui_assets'
                output.parent.mkdir(parents=True)
                if component == 'output-file':
                    output.write_bytes(b'not a directory')
                elif component == 'broken-output-link':
                    output.symlink_to(root / 'missing-build', target_is_directory=True)
                else:
                    output.mkdir()
                if component == 'index.html':
                    (output / component).write_bytes(b'<html></html>')
                elif component == 'assets':
                    (output / component).mkdir()
                    (output / component / 'app.js').write_bytes(b'js')
                elif component == 'empty-assets':
                    (output / 'assets').mkdir()
                elif component in ('other.txt', '.hidden'):
                    (output / component).write_bytes(b'partial build')
                elif component == 'broken-index-link':
                    (output / 'index.html').symlink_to(root / 'missing-index')
                with mock.patch.dict(globals(), {'REPO': root}), self.assertRaisesRegex(ValueError, 'missing'):
                    self.test_built_assets_match_sources()

    def test_sources_sha_uses_sorted_utf8_paths_and_nul_delimiters(self):
        # Explicit canonical order, including names that JavaScript's default
        # UTF-16 sort puts in the opposite order to Python's Unicode sort.
        records = (
            ('apps/workspace/index.html', b'index'),
            ('apps/workspace/package-lock.json', b'lock'),
            ('apps/workspace/src/nested/z.txt', b'nested'),
            ('apps/workspace/src/\ue000.txt', b'\xff'),
            ('apps/workspace/src/\U00010000.txt', b'\xfe'),
            ('apps/workspace/tsconfig.json', b'types'),
            ('apps/workspace/vite.config.ts', b'vite'),
            ('docs/brand/myflames-app-logo.png', b'\x89PNG\xff'),
        )
        expected = hashlib.sha256(b''.join(
            path.encode('utf-8') + b'\0' + content + b'\0'
            for path, content in records)).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, content in reversed(records):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            self.assertEqual(sources_sha(root), expected)

    def test_sources_sha_normalizes_crlf_and_hashes_binary_without_hidden_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'apps' / 'workspace'
            (workspace / 'src' / 'nested').mkdir(parents=True)
            for name in ('index.html', 'vite.config.ts', 'tsconfig.json',
                         'package-lock.json'):
                (workspace / name).write_bytes(b'build input\n')
            logo = root / 'docs' / 'brand' / 'myflames-app-logo.png'
            logo.parent.mkdir(parents=True)
            logo.write_bytes(b'\x89PNG\xff\x00\r\n')
            text = workspace / 'src' / 'nested' / 'text.txt'
            text.write_bytes(b'first\nsecond\nlast\r')
            binary = workspace / 'src' / 'logo.png'
            binary.write_bytes(b'\x89PNG\xff\xfe\x80\x00\r\n\r')
            original = sources_sha(root)
            self.assertRegex(original, r'^[0-9a-f]{64}$')

            text.write_bytes(b'first\r\nsecond\r\nlast\r')
            self.assertEqual(sources_sha(root), original)
            binary.write_bytes(b'\x89PNG\xff\xfe\x80\x00\n\r')
            self.assertEqual(sources_sha(root), original)
            logo.write_bytes(b'\x89PNG\xff\x00\n')
            self.assertEqual(sources_sha(root), original)
            logo.write_bytes(logo.read_bytes() + b'\xff')
            self.assertNotEqual(sources_sha(root), original)
            logo.write_bytes(logo.read_bytes()[:-1])
            (workspace / 'package.json').write_bytes(b'test script changed')
            self.assertEqual(sources_sha(root), original)
            text.write_bytes(b'first\nsecond\nlast')
            self.assertNotEqual(sources_sha(root), original)
            text.write_bytes(b'first\nsecond\nlast\r')

            (workspace / 'src' / '.ignored').write_bytes(b'ignored\r\n\xff')
            (workspace / 'src' / '.hidden' / 'nested').mkdir(parents=True)
            (workspace / 'src' / '.hidden' / 'nested' / 'visible.txt').write_bytes(b'ignored')
            self.assertEqual(sources_sha(root), original)

            binary.write_bytes(binary.read_bytes() + b'\xff')
            self.assertNotEqual(sources_sha(root), original)
            binary.write_bytes(binary.read_bytes()[:-1])
            text.rename(text.with_name('renamed.txt'))
            self.assertNotEqual(sources_sha(root), original)


class PackageAssetsTests(unittest.TestCase):
    """Synthetic archives exercise shipping guards without building a package."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('src/main.ts', 'index.html', 'vite.config.ts', 'tsconfig.json', 'package-lock.json'):
            path = self.root / 'apps' / 'workspace' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'source\n')
        logo = self.root / 'docs' / 'brand' / 'myflames-app-logo.png'
        logo.parent.mkdir(parents=True)
        logo.write_bytes(b'\x89PNG\xff')
        self.stamp = sources_sha(self.root)
        self.index = ('<meta name="myflames-sources-sha256" content="%s">' % self.stamp).encode('utf-8')

    def target(self, kind, index=None, js=True, extra_files=None):
        files = {} if index is None else {'myflames/ui_assets/index.html': index}
        if js:
            files['myflames/ui_assets/assets/app.js'] = b'console.log("fixture");'
        files.update(('myflames/ui_assets/' + name, content)
                     for name, content in (extra_files or {}).items())
        if kind == 'directory':
            for name, content in files.items():
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (self.root / 'myflames').mkdir(exist_ok=True)
            return self.root / 'myflames'
        if kind == 'wheel':
            target = self.root / 'fixture.whl'
            with zipfile.ZipFile(str(target), 'w') as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
            return target
        target = self.root / 'fixture.tar.gz'
        with tarfile.open(str(target), 'w:gz') as archive:
            for name, content in files.items():
                entry = tarfile.TarInfo('myflames-1.0/' + name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return target

    def test_accepts_current_directory_wheel_and_sdist(self):
        for kind in ('directory', 'wheel', 'sdist'):
            with self.subTest(kind=kind):
                self.assertEqual(check_ui_assets(self.target(kind, self.index), self.root), self.stamp)

    def test_missing_index_is_a_build_error(self):
        for kind in ('directory', 'wheel', 'sdist'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'index.html.*npm run build'):
                check_ui_assets(self.target(kind), self.root)

    def test_missing_javascript_is_a_build_error(self):
        for kind in ('directory', 'wheel', 'sdist'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, r'assets/\*\.js.*npm run build'):
                check_ui_assets(self.target(kind, self.index, js=False), self.root)

    def test_stale_missing_and_duplicate_stamps_fail_even_with_current_checkout_assets(self):
        self.target('directory', self.index)
        for kind in ('wheel', 'sdist', 'directory'):
            for index in (b'<html></html>', self.index.replace(self.stamp.encode(), b'0' * 64),
                          self.index + self.index):
                with self.subTest(kind=kind, index=index), self.assertRaisesRegex(ValueError, 'stale.*npm run build'):
                    check_ui_assets(self.target(kind, index), self.root)

    def test_changed_source_invalidates_each_format(self):
        targets = [self.target(kind, self.index) for kind in ('directory', 'wheel', 'sdist')]
        (self.root / 'apps/workspace/src/main.ts').write_bytes(b'changed source')
        for target in targets:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'stale'):
                check_ui_assets(target, self.root)

    def test_every_index_asset_reference_must_exist_even_with_other_javascript(self):
        references = (
            '<script src="/assets/missing.js"></script>',
            '<script src="/missing.js"></script>',
            '<script src="./assets/missing.js?v=1#entry"></script>',
            '<link rel="stylesheet" href="assets/missing.css">',
            '<link rel="stylesheet" href="/missing.css">',
            '<link rel="icon" href="/assets/missing.png?v=4">',
            '<link rel="modulepreload" href="/assets/missing.js">',
        )
        for kind in ('directory', 'wheel', 'sdist'):
            for reference in references:
                with self.subTest(kind=kind, reference=reference):
                    target = self.target(kind, self.index + reference.encode('utf-8'))
                    with self.assertRaisesRegex(ValueError, 'missing referenced workspace asset .*missing'):
                        check_ui_assets(target, self.root)

    def test_accepts_existing_script_stylesheet_and_icon_references(self):
        index = self.index + (
            '<script src="/assets/app.js?v=1#entry"></script>'
            '<link rel="stylesheet" href="./assets/style.css">'
            '<link rel="icon" href="assets/icon.png?v=4&amp;x=1"/>'
            '<link rel="modulepreload" href="assets/lazy.js">'
            '<link rel="stylesheet" href="https://example.invalid/external.css">'
        ).encode('utf-8')
        extra = {'assets/style.css': b'body {}', 'assets/icon.png': b'icon',
                 'assets/lazy.js': b'lazy'}
        for kind in ('directory', 'wheel', 'sdist'):
            with self.subTest(kind=kind):
                self.assertEqual(check_ui_assets(self.target(kind, index, extra_files=extra), self.root),
                                 self.stamp)

    def reference_build(self):
        reference = self.root / 'reference'
        files = {'index.html': self.index, 'assets/app.js': b'console.log("fixture");',
                 'assets/lazy.js': b'lazy chunk', 'assets/style.css': b'body {}',
                 'assets/nested/icon.png': b'\x89PNG\xff'}
        for name, content in files.items():
            path = reference / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return reference, files

    def test_reference_build_matches_every_file_including_unreferenced_chunks(self):
        reference, files = self.reference_build()
        for kind in ('directory', 'wheel', 'sdist'):
            with self.subTest(kind=kind):
                target = self.target(kind, self.index, extra_files=files)
                self.assertEqual(check_ui_assets(target, self.root, reference=reference), self.stamp)

    def test_reference_build_rejects_missing_extra_and_altered_files(self):
        reference, files = self.reference_build()
        for kind in ('directory', 'wheel', 'sdist'):
            for change in ('missing', 'extra', 'altered'):
                changed = dict(files)
                if change == 'missing':
                    del changed['assets/lazy.js']
                elif change == 'extra':
                    changed['assets/extra.js'] = b'unexpected'
                else:
                    changed['assets/lazy.js'] = b'altered bytes'
                with self.subTest(kind=kind, change=change):
                    # A directory fixture needs a fresh tree for every case.
                    package = self.root / 'myflames'
                    if kind == 'directory' and package.exists():
                        package.rename(self.root / ('previous-' + change))
                    target = self.target(kind, self.index, extra_files=changed)
                    with self.assertRaisesRegex(ValueError, 'reference build.*' + change):
                        check_ui_assets(target, self.root, reference=reference)

    def test_asset_references_allow_served_queries_fragments_and_root(self):
        index = self.index + (
            '<script src="./assets/app.js?v=1&amp;x=2#entry"></script>'
            '<link rel="icon" href="assets/icon.png?v=4" />'
            '<link rel="preload" href="/?v=1#entry">'
            '<link rel="stylesheet" href="//example.invalid/assets/external.css">'
        ).encode('utf-8')
        files = {'assets/icon.png': b'png'}
        for kind in ('directory', 'wheel', 'sdist'):
            with self.subTest(kind=kind):
                self.assertEqual(check_ui_assets(self.target(kind, index, extra_files=files), self.root), self.stamp)

    def test_existing_references_must_be_servable_by_the_page_handler(self):
        # Existing files must still fail when their URL cannot reach them.
        paths = ('/outside.js', '/index.html', '/assets/sub/x.js',
                 '/assets/icon%20name.png', '/assets/%61pp.js',
                 '/assets/../assets/app.js', '/assets/sub\\app.js',
                 '/assets/app.txt')
        files = {'outside.js': b'js', 'assets/sub/x.js': b'js',
                 'assets/icon name.png': b'png', 'assets/icon%20name.png': b'png',
                 'assets/%61pp.js': b'js', 'assets/sub\\app.js': b'js',
                 'assets/app.txt': b'js'}
        for kind in ('directory', 'wheel', 'sdist'):
            for tag in ('<script src="{}"></script>', '<link href="{}">'):
                for path in paths:
                    with self.subTest(kind=kind, tag=tag, path=path):
                        index = self.index + tag.format(path).encode('utf-8')
                        target = self.target(kind, index, extra_files=files)
                        with self.assertRaisesRegex(ValueError, 'unservable referenced workspace asset'):
                            check_ui_assets(target, self.root)

    def test_external_scripts_are_rejected(self):
        for kind in ('directory', 'wheel', 'sdist'):
            for src in ('https://example.invalid/app.js', '//example.invalid/app.js',
                        'data:text/javascript,alert(1)'):
                with self.subTest(kind=kind, src=src):
                    index = self.index + ('<script src="{}"></script>'.format(src)).encode('utf-8')
                    with self.assertRaisesRegex(ValueError, 'external workspace script'):
                        check_ui_assets(self.target(kind, index), self.root)

    def test_wheel_requires_real_package_paths_and_flat_js_assets(self):
        target = self.root / 'fixture.whl'
        for prefix, js in (('other/', 'app.js'), ('', 'nested/app.js')):
            with zipfile.ZipFile(str(target), 'w') as archive:
                archive.writestr(prefix + 'myflames/ui_assets/index.html', self.index)
                archive.writestr(prefix + 'myflames/ui_assets/assets/' + js, b'js')
            with self.subTest(prefix=prefix, js=js), self.assertRaises(ValueError):
                check_ui_assets(target, self.root)

    def test_sdist_does_not_accept_a_link_as_javascript(self):
        target = self.target('sdist', self.index, js=False)
        # tarfile cannot append compressed files, so create the two members anew.
        with tarfile.open(str(target), 'w:gz') as archive:
            index = tarfile.TarInfo('myflames-1.0/myflames/ui_assets/index.html')
            index.size = len(self.index)
            archive.addfile(index, io.BytesIO(self.index))
            link = tarfile.TarInfo('myflames-1.0/myflames/ui_assets/assets/app.js')
            link.type, link.linkname = tarfile.SYMTYPE, '/outside/app.js'
            archive.addfile(link)
        with self.assertRaisesRegex(ValueError, r'assets/\*\.js'):
            check_ui_assets(target, self.root)

    def test_sdist_does_not_ignore_extra_links_when_matching_a_reference(self):
        reference, files = self.reference_build()
        target = self.root / 'linked.tar.gz'
        with tarfile.open(str(target), 'w:gz') as archive:
            for name, content in files.items():
                entry = tarfile.TarInfo('myflames-1.0/myflames/ui_assets/' + name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            link = tarfile.TarInfo('myflames-1.0/myflames/ui_assets/assets/extra.js')
            link.type, link.linkname = tarfile.SYMTYPE, '/outside/app.js'
            archive.addfile(link)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            check_ui_assets(target, self.root, reference=reference)


if __name__ == '__main__':
    unittest.main()
