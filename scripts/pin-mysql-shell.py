#!/usr/bin/env python3
"""Download and authenticate Oracle archives before proposing a Shell lock.

Requires gpg with Oracle's release key already in the maintainer's keyring.
Archive contents are never executed. JSON goes to stdout; progress and the
informational macOS codesign coverage report go to stderr. Without --write the
packaged lock is untouched. Python 3.7+, standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from myflames import managed_shell as shell  # noqa: E402
from myflames import tls  # noqa: E402

LOCK_PATH = ROOT / 'myflames' / 'mysql-shell.lock.json'


def valid_gpg_status(output):
    """Trust only machine-readable status records, never gpg's prose."""
    valid = False
    failures = {'BADSIG', 'ERRSIG', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG',
                'NO_PUBKEY', 'FAILURE', 'NODATA', 'BADARMOR'}
    for line in output.splitlines():
        if not line.startswith('[GNUPG:] '):
            continue
        fields = line[len('[GNUPG:] '):].split()
        if not fields:
            continue
        if fields[0] in failures:
            return False
        if fields[0] == 'VALIDSIG':
            if len(fields) < 10 or fields[1] != shell.SIGNING_KEY:
                return False
            valid = True
    return valid


def render_lock(version, archives):
    """Render a complete lock only when every platform passed verification."""
    lock = {'schema_version': 'mysql-shell-lock-1.0', 'product': 'MySQL Shell',
            'version': version, 'license': 'GPL-2.0-only WITH Universal-FOSS-exception-1.0',
            'license_url': 'https://www.mysql.com/about/legal/licensing/oem/',
            'downloads_url': 'https://dev.mysql.com/downloads/shell/%s.html' % '.'.join(version.split('.')[:2]),
            'signing_key': shell.SIGNING_KEY, 'archives': archives}
    shell.validate_lock(lock)
    return json.dumps(lock, indent=2) + '\n'


def archive_roots(version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('Use a version with three numbers, such as 9.7.0.')
    return {key: 'mysql-shell-%s-%s' % (version, suffix) for key, suffix in (
        ('darwin-arm64', 'macos15-arm64'), ('darwin-x64', 'macos15-x86-64bit'),
        ('win32-x64', 'windows-x86-64bit'))}


def _length(response, maximum):
    values = response.headers.get_all('Content-Length', [])
    if len(values) != 1 or not re.fullmatch(r'[0-9]+', values[0]):
        raise ValueError('Oracle did not publish a valid Content-Length.')
    size = int(values[0])
    if not 0 < size <= maximum:
        raise ValueError('The published size exceeds the download limit.')
    return size


def fetch(url, destination, maximum, expected=None, head=False):
    """No proxies, unsafe redirects, unbounded bodies, or partial files."""
    deadline = time.monotonic() + shell.DOWNLOAD_DEADLINE
    watch = tls.Watchdog(deadline)
    temporary = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            tls.WatchedHTTPSHandler(watch, tls.https_context()), shell._ShellRedirectHandler())
        request = urllib.request.Request(shell._validate_url(url), method='HEAD' if head else 'GET',
                                         headers={'Accept-Encoding': 'identity'})
        with opener.open(request, timeout=shell.REQUEST_TIMEOUT) as response:
            shell._validate_url(response.geturl())
            if response.status != 200:
                raise ValueError('Oracle did not return the requested file.')
            size = _length(response, maximum)
            if expected is not None and size != expected:
                raise ValueError('The download differs from Oracle’s published size.')
            if head:
                return size
            fd, temporary = tempfile.mkstemp(prefix='.mysqlsh-pin-', dir=str(destination.parent))
            with os.fdopen(fd, 'wb') as output:
                received = 0
                while True:
                    if watch.expired or time.monotonic() >= deadline:
                        raise ValueError('The download deadline expired.')
                    chunk = response.read(min(shell.CHUNK, size - received + 1))
                    if watch.expired or time.monotonic() >= deadline:
                        raise ValueError('The download deadline expired.')
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size or received > maximum:
                        raise ValueError('The download exceeded its published size.')
                    output.write(chunk)
                if received != size or (getattr(response, 'length', None) or 0) > 0:
                    raise ValueError('The download ended early.')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, str(destination))
            temporary = None
            return size
    finally:
        watch.stop()
        if temporary:
            os.unlink(temporary)


def file_digest(path, algorithm):
    digest = hashlib.new(algorithm)
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(shell.CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def codesign_coverage(archive):
    """Read each Mach-O with codesign on macOS, without unsafe tar extraction.

    This report is informational: the pinned tarballs are ad hoc signed and
    have no Oracle Developer ID. PGP plus the packaged SHA-256 establish trust.
    """
    if sys.platform != 'darwin':
        print('%s: codesign coverage unverified (requires macOS).' % archive.name, file=sys.stderr)
        return
    magic = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
             b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce'}
    counts = {'Mach-O': 0, 'ad hoc': 0, 'Oracle Developer ID': 0, 'other or unsigned': 0, 'unverified': 0}
    total, entries = 0, 0
    with tempfile.TemporaryDirectory(prefix='.mysqlsh-codesign-', dir=str(archive.parent)) as temp:
        path = os.path.join(temp, 'binary')
        with tarfile.open(str(archive), 'r:gz') as contents:
            for member in contents:
                entries += 1
                if entries > shell.MAX_EXTRACTED_FILES:
                    raise ValueError('The archive contains too many entries to inspect.')
                if not member.isfile():
                    continue
                total += member.size
                if member.size < 0 or total > shell.MAX_EXTRACTED_BYTES:
                    raise ValueError('The archive exceeds the inspection size limit.')
                with contents.extractfile(member) as source:
                    prefix = source.read(4)
                    if prefix not in magic:
                        continue
                    counts['Mach-O'] += 1
                    with open(path, 'wb') as target:
                        target.write(prefix)
                        shutil.copyfileobj(source, target, shell.CHUNK)
                try:
                    result = subprocess.run(['/usr/bin/codesign', '-dvv', path], stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env={'LC_ALL': 'C'})
                except (OSError, subprocess.TimeoutExpired):
                    counts['unverified'] += 1
                    continue  # Informational only; PGP and SHA-256 bind the archive.
                report = result.stderr.decode('utf-8', 'replace')
                if result.returncode == 0 and 'TeamIdentifier=VB5E2TV963' in report and 'Signature=adhoc' not in report:
                    counts['Oracle Developer ID'] += 1
                elif result.returncode == 0 and 'Signature=adhoc' in report:
                    counts['ad hoc'] += 1
                else:
                    counts['other or unsigned'] += 1
    print('%s: codesign coverage: %s.' % (archive.name, ', '.join('%d %s' % (v, k) for k, v in counts.items())), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', help='Oracle version, for example 9.7.0 (macOS 15 archives)')
    parser.add_argument('output_folder', type=Path, help='Folder retaining the verified archives, signatures and MD5 files')
    parser.add_argument('--write', action='store_true', help='Also atomically replace myflames/mysql-shell.lock.json')
    args = parser.parse_args(argv)
    try:
        roots = archive_roots(args.version)
        args.output_folder.mkdir(parents=True, exist_ok=True)
        archives = {}
        for key, root in roots.items():
            name = root + ('.zip' if key == 'win32-x64' else '.tar.gz')
            url = shell.DOWNLOAD_PREFIX + name
            path = args.output_folder / name
            print('Downloading and verifying %s.' % name, file=sys.stderr)
            size = fetch(url, path, shell.MAX_SHELL_ARCHIVE_BYTES, head=True)
            fetch(url, path, shell.MAX_SHELL_ARCHIVE_BYTES, expected=size)
            signature = args.output_folder / (name + '.asc')
            md5_path = args.output_folder / (name + '.md5')
            fetch(url + '.asc', signature, 1024 * 1024)
            fetch(url + '.md5', md5_path, 8192)
            published = md5_path.read_text(encoding='ascii').strip().split()
            if not published or not re.fullmatch('[0-9a-fA-F]{32}', published[0]):
                raise ValueError('Oracle returned an invalid MD5 checksum.')
            md5 = file_digest(path, 'md5')
            if md5 != published[0].lower():
                raise ValueError('The archive did not match Oracle’s published MD5 checksum.')
            checked = subprocess.run(['gpg', '--batch', '--no-auto-key-retrieve', '--status-fd', '1', '--verify',
                                      str(signature), str(path)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if checked.returncode != 0 or not valid_gpg_status(checked.stdout.decode('ascii', 'replace')):
                raise ValueError('Oracle’s PGP signature did not verify with the pinned release key.')
            sha256 = file_digest(path, 'sha256')
            if key.startswith('darwin-'):
                codesign_coverage(path)
            archives[key] = dict(version=args.version, url=url, size=size, md5=md5, sha256=sha256, root=root)
        rendered = render_lock(args.version, archives)
        if args.write:
            fd, temporary = tempfile.mkstemp(prefix='.mysql-shell-lock-', dir=str(LOCK_PATH.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as output:
                    output.write(rendered)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, str(LOCK_PATH))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        print(rendered, end='')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print('Could not pin MySQL Shell: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
