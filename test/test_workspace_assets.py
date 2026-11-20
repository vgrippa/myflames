"""Check workspace build inputs against the committed index.html stamp."""
import hashlib
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STALE_MESSAGE = 'myflames/ui_assets is stale: run `npm run build` in apps/workspace'


def sources_sha(root=REPO):
    """Same byte-based algorithm as apps/workspace/vite.config.ts:sourcesSha."""
    root = Path(root)
    paths = []
    for directory, folders, files in os.walk(str(root / 'apps' / 'workspace' / 'src')):
        folders[:] = [name for name in folders if not name.startswith('.')]
        for name in files:
            if not name.startswith('.'):
                paths.append((Path(directory) / name).relative_to(root).as_posix())
    paths.extend(('apps/workspace/index.html', 'apps/workspace/vite.config.ts',
                  'apps/workspace/tsconfig.json', 'apps/workspace/package-lock.json',
                  'docs/brand/myflames-app-logo.png'))
    digest = hashlib.sha256()
    for relative in sorted(paths, key=lambda path: path.encode('utf-8')):
        content = (root / relative).read_bytes().replace(b'\r\n', b'\n')
        digest.update(relative.encode('utf-8') + b'\0' + content + b'\0')
    return digest.hexdigest()


class _SourcesStampParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stamps = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'myflames-sources-sha256':
            self.stamps.append(attrs.get('content'))


class WorkspaceAssetsTests(unittest.TestCase):
    def test_committed_assets_match_sources(self):
        parser = _SourcesStampParser()
        parser.feed((REPO / 'myflames' / 'ui_assets' / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual(len(parser.stamps), 1, STALE_MESSAGE)
        self.assertEqual(parser.stamps[0], sources_sha(), STALE_MESSAGE)

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


if __name__ == '__main__':
    unittest.main()
