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
from urllib.parse import urlsplit
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
        self.references = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'myflames-sources-sha256':
            self.stamps.append(attrs.get('content'))
        # Include preload links as well as stylesheets and icons, so all local
        # assets that the index asks the browser to load must exist.
        reference = None
        if tag == 'script':
            reference = attrs.get('src')
        elif tag == 'link':
            reference = attrs.get('href')
        if reference is not None:
            self.references.append((tag, reference))


def _check_stamp(index, root, names):
    parser = _SourcesStampParser()
    parser.feed(index.decode('utf-8'))
    expected = sources_sha(root)
    if parser.stamps != [expected]:
        raise ValueError('myflames/ui_assets is stale or has no valid source stamp: ' + BUILD_HINT)
    for tag, reference in parser.references:
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            if tag == 'script':
                raise ValueError('external workspace script is not allowed: {}'.format(reference))
            continue
        # ui.py ignores queries and fragments, but does not decode URL paths.
        # Only the root index and flat assets with known suffixes are served.
        if '%' in url.path or '\\' in url.path or '..' in url.path.split('/'):
            raise ValueError('unservable referenced workspace asset {}: {}'.format(reference, BUILD_HINT))
        path = url.path.lstrip('/')
        while path.startswith('./'):
            path = path[2:]
        name = path or 'index.html'
        if name not in names:
            raise ValueError('missing referenced workspace asset {}: {}'.format(name, BUILD_HINT))
        if path and not re.fullmatch(r'assets/[^/]+\.(js|css|json|jpeg|png|svg|woff2)', path):
            raise ValueError('unservable referenced workspace asset {}: {}'.format(reference, BUILD_HINT))
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


def _directory_files(assets):
    """Read file contents only; packaging may legitimately change file modes."""
    return {path.relative_to(assets).as_posix(): path.read_bytes()
            for path in assets.rglob('*') if path.is_file()}


def _check_files(files, root):
    if 'index.html' not in files:
        raise ValueError('missing myflames/ui_assets/index.html: ' + BUILD_HINT)
    if not any(re.fullmatch(r'assets/[^/]+\.js', name) for name in files):
        raise ValueError('missing myflames/ui_assets/assets/*.js: ' + BUILD_HINT)
    return _check_stamp(files['index.html'], root, files)


def _check_reference(files, reference, root):
    expected = _directory_files(Path(reference))
    _check_files(expected, root)
    actual_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    expected_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in expected.items()}
    missing = sorted(set(expected_hashes) - set(actual_hashes))
    extra = sorted(set(actual_hashes) - set(expected_hashes))
    altered = sorted(name for name in set(actual_hashes) & set(expected_hashes)
                     if actual_hashes[name] != expected_hashes[name])
    differences = [('missing', missing), ('extra', extra), ('altered', altered)]
    details = '; '.join('{}: {}'.format(label, ', '.join(names))
                        for label, names in differences if names)
    if details:
        raise ValueError('workspace assets differ from reference build ({}): {}'.format(details, reference))


def check_ui_assets(target, root=REPO, reference=None):
    """Validate index references and optionally match an entire ui_assets tree.

    reference names the freshly built ui_assets directory, not its package.
    Compare relative file names and SHA-256, independent of archive metadata.
    """
    target = Path(target)
    if target.is_dir():
        files = _directory_files(target / 'ui_assets')
    elif target.suffix == '.whl':
        with zipfile.ZipFile(str(target)) as archive:
            names = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
            prefix = _archive_index(names)[:-len('index.html')]
            members = [name for name in names if name.startswith(prefix)]
            if len(members) != len(set(members)):
                raise ValueError('duplicate workspace asset in archive')
            files = {name[len(prefix):]: archive.read(name) for name in members}
    elif target.name.endswith('.tar.gz'):
        with tarfile.open(str(target), 'r:gz') as archive:
            entries = archive.getmembers()
            names = [entry.name for entry in entries if entry.isfile()]
            prefix = _archive_index(names, sdist=True)[:-len('index.html')]
            if any(entry.name.startswith(prefix) and not entry.isfile() and not entry.isdir()
                   for entry in entries):
                raise ValueError('workspace asset in archive is not a regular file')
            members = [name for name in names if name.startswith(prefix)]
            if len(members) != len(set(members)):
                raise ValueError('duplicate workspace asset in archive')
            files = {}
            for name in members:
                with archive.extractfile(name) as handle:
                    files[name[len(prefix):]] = handle.read()
    else:
        raise ValueError('expected a package directory, .whl or .tar.gz: {}'.format(target))
    stamp = _check_files(files, root)
    if reference is not None:
        _check_reference(files, reference, root)
    return stamp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('targets', nargs='+', help='package directory (myflames), wheel or sdist')
    parser.add_argument('--reference', help='ui_assets directory whose file names and SHA-256 hashes must match')
    args = parser.parse_args(argv)
    for target in args.targets:
        try:
            check_ui_assets(target, reference=args.reference)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            print('check_ui_assets: {}: {}'.format(target, exc), file=sys.stderr)
            return 1
        print('{}: workspace assets match sources'.format(target))
    return 0


if __name__ == '__main__':
    sys.exit(main())
