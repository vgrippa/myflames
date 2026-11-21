"""Verify the workspace build in a package directory, wheel or sdist.

Standard library only, Python 3.7+. Archives are read without extracting them.
The source checkout containing this script supplies the expected build stamp.
"""
import argparse
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import sys
import tarfile
import zipfile


REPO = Path(__file__).resolve().parents[1]
BUILD_HINT = 'run `npm ci` and `npm run build` in apps/workspace'


def sources_sha(root=REPO):
    """Same byte-based algorithm as apps/workspace/vite.config.ts:sourcesSha."""
    root = Path(root)
    source = root / 'apps' / 'workspace' / 'src'
    if not source.is_dir():
        raise ValueError('workspace sources are missing: {}'.format(source))
    paths = []
    for directory, folders, files in os.walk(str(source)):
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


def _check_stamp(index, root):
    parser = _SourcesStampParser()
    parser.feed(index.decode('utf-8'))
    expected = sources_sha(root)
    if parser.stamps != [expected]:
        raise ValueError('myflames/ui_assets is stale or has no valid source stamp: ' + BUILD_HINT)
    return expected


def _archive_index(names, sdist=False):
    """Require the index and a flat JS asset in the same package tree."""
    pattern = (r'[^/]+/' if sdist else '') + r'myflames/ui_assets/index\.html'
    indexes = [name for name in names if re.fullmatch(pattern, name)]
    if len(indexes) != 1:
        raise ValueError('expected one myflames/ui_assets/index.html: ' + BUILD_HINT)
    prefix = indexes[0][:-len('index.html')] + 'assets/'
    if not any(re.fullmatch(re.escape(prefix) + r'[^/]+\.js', name) for name in names):
        raise ValueError('missing myflames/ui_assets/assets/*.js: ' + BUILD_HINT)
    return indexes[0]


def check_ui_assets(target, root=REPO):
    """Return the source digest only for a complete, current workspace build."""
    target = Path(target)
    if target.is_dir():
        assets = target / 'ui_assets'
        index = assets / 'index.html'
        if not index.is_file():
            raise ValueError('missing myflames/ui_assets/index.html: ' + BUILD_HINT)
        if not any(path.is_file() for path in (assets / 'assets').glob('*.js')):
            raise ValueError('missing myflames/ui_assets/assets/*.js: ' + BUILD_HINT)
        content = index.read_bytes()
    elif target.suffix == '.whl':
        with zipfile.ZipFile(str(target)) as archive:
            names = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
            content = archive.read(_archive_index(names))
    elif target.name.endswith('.tar.gz'):
        with tarfile.open(str(target), 'r:gz') as archive:
            names = [entry.name for entry in archive.getmembers() if entry.isfile()]
            with archive.extractfile(_archive_index(names, sdist=True)) as handle:
                content = handle.read()
    else:
        raise ValueError('expected a package directory, .whl or .tar.gz: {}'.format(target))
    return _check_stamp(content, root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('targets', nargs='+', help='package directory (myflames), wheel or sdist')
    args = parser.parse_args(argv)
    for target in args.targets:
        try:
            check_ui_assets(target)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            print('check_ui_assets: {}: {}'.format(target, exc), file=sys.stderr)
            return 1
        print('{}: workspace assets match sources'.format(target))
    return 0


if __name__ == '__main__':
    sys.exit(main())
