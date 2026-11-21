"""Check optional workspace output and the required packaging guards."""
import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
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
        if not (REPO / 'myflames' / 'ui_assets' / 'index.html').is_file():
            self.skipTest('Workspace assets are not built; run `npm ci` and `npm run build` in apps/workspace')
        check_ui_assets(REPO / 'myflames')

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

    def target(self, kind, index=None, js=True):
        files = {} if index is None else {'myflames/ui_assets/index.html': index}
        if js:
            files['myflames/ui_assets/assets/app.js'] = b'console.log("fixture");'
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


if __name__ == '__main__':
    unittest.main()
