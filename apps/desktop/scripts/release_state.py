"""Local release provenance, resumable stages and bounded notarytool retries.

Standard library only, Python 3.7+. State contains artifact hashes and Apple's
submission ID, never credentials. It belongs to one worktree, not the shared
download cache. A hash is a resume guard, not a substitute for signature checks.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse


STAGES = ('app', 'verify-app', 'dmg-notarize', 'staple', 'verify', 'size', 'checksums')
RETRY_BACKOFF = (10, 30, 90)
MAX_ATTEMPTS = 3  # Three total attempts: only the first two delays are needed.
NOTARY_HOST = 'appstoreconnect.apple.com'
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
SMOKES = ('editor-settings', 'preferences')
SCHEMA = 'myflames-release-state-1'

# The only (stage, artifact) pairs allowed to change a consumed path's bytes:
# signing and stapling rewrite the DMG in place. 'app' is the first stage, so
# it consumes nothing; verify, size and checksums must leave every input
# untouched, or the .sha256 files would bless bytes nobody verified.
DECLARED_WRITERS = {'dmg-notarize': ('.dmg',), 'staple': ('.dmg',)}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def hash_path(path):
    """Hash contents, names, modes and symlink targets without following links.

    Framework symlinks are part of a macOS app. Their targets are hashed as
    names; the regular files elsewhere in the same tree are hashed once each.
    Timestamps are deliberately excluded (mounting/extraction can change them).
    """
    path = Path(path)
    info = path.lstat()
    if stat.S_ISREG(info.st_mode):
        return sha256_file(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('artifact is not a regular file or directory: {}'.format(path))
    digest = hashlib.sha256()
    digest.update(('directory-mode:{}\n'.format(stat.S_IMODE(info.st_mode))).encode('ascii'))

    def visit(folder):
        for child in sorted(folder.iterdir(), key=lambda item: item.name):
            entry = child.lstat()
            relative = child.relative_to(path).as_posix()
            if stat.S_ISLNK(entry.st_mode):
                kind, value = 'link', os.readlink(str(child))
            elif stat.S_ISDIR(entry.st_mode):
                kind, value = 'directory', ''
            elif stat.S_ISREG(entry.st_mode):
                kind, value = 'file', sha256_file(child)
            else:
                raise ValueError('artifact contains a special file: {}'.format(child))
            record = [relative, kind, stat.S_IMODE(entry.st_mode), value]
            digest.update(json.dumps(record, ensure_ascii=True, separators=(',', ':')).encode('ascii'))
            digest.update(b'\n')
            if kind == 'directory':
                visit(child)

    visit(path)
    return digest.hexdigest()


def ui_assets_digest(root):
    return hash_path(Path(root) / 'myflames' / 'ui_assets')


def git_head(root):
    try:
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'],
                                       stderr=subprocess.DEVNULL).decode('ascii').strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def write_json(path, value):
    """Atomic private-file replacement; a failed write leaves the old checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('refusing a symlink at {}'.format(path))
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    with open(str(path), 'r') as handle:
        return json.load(handle)


def validate_manifest(manifest, commit, ui_digest, requirements_digest=None):
    if not commit or manifest.get('commit') != commit:
        raise ValueError('backend manifest commit does not match HEAD')
    if manifest.get('ui_assets_sha256') != ui_digest:
        raise ValueError('backend manifest ui_assets digest does not match the tree')
    if requirements_digest is not None and manifest.get('requirements_sha256') != requirements_digest:
        raise ValueError('backend manifest build requirements do not match the tree')
    return manifest


def check_manifest(root):
    root = Path(root)
    backend = root / 'apps' / 'desktop' / 'build' / 'backend'
    try:
        manifest = read_json(backend / 'build-manifest.json')
        validate_manifest(manifest, git_head(root), ui_assets_digest(root),
                          sha256_file(root / 'apps' / 'desktop' / 'requirements-build.txt'))
        if manifest.get('backend_sha256') != hash_path(backend / 'myflames-backend'):
            raise ValueError('backend files do not match the build manifest')
        if manifest.get('python_license_sha256') != sha256_file(backend / 'Python-LICENSE.txt'):
            raise ValueError('backend Python license does not match the build manifest')
        python = build_venv_python(root)
        built = '.'.join(str(manifest.get('python_version') or '').split('.')[:2])
        current = venv_python_version(python)
        if built != current:
            raise ValueError('backend was built with Python {} but the build venv runs {}'.format(
                built or 'an unknown version', current))
        if manifest.get('build_freeze_sha256') != freeze_digest(venv_freeze(python)):
            raise ValueError('build venv dependencies differ from the build manifest')
        return manifest
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        raise ValueError('{}; rebuild the backend with release-mac.sh'.format(exc))


def build_manifest(root, before, freeze):
    """Publish provenance only after a successful build of unchanged inputs.

    freeze is the sorted pip freeze of the build venv: requirements-build.txt
    pins only the direct dependencies, so the manifest records the resolved set
    and check_manifest refuses a backend built from anything else.
    """
    root = Path(root)
    desktop = root / 'apps' / 'desktop'
    current = {'commit': git_head(root), 'ui_assets_sha256': ui_assets_digest(root),
               'requirements_sha256': sha256_file(desktop / 'requirements-build.txt')}
    if before != current:
        raise ValueError('build inputs changed while building; rebuild the backend with release-mac.sh')
    current.update({'schema_version': 'myflames-backend-build-1',
                    'python_version': sys.version.split()[0], 'built_at': utc_now(),
                    'backend_sha256': hash_path(desktop / 'build' / 'backend' / 'myflames-backend'),
                    'python_license_sha256': sha256_file(desktop / 'build' / 'backend' / 'Python-LICENSE.txt'),
                    'build_freeze': list(freeze), 'build_freeze_sha256': freeze_digest(freeze)})
    write_json(desktop / 'build' / 'backend' / 'build-manifest.json', current)


def venv_key(python_version, requirements_path):
    parts = str(python_version).split('.')
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        raise ValueError('Python version must start with major.minor')
    return {'python': '.'.join(parts[:2]), 'requirements_sha256': sha256_file(requirements_path)}


def build_venv_python(root):
    """The interpreter whose packages built the backend (see release-mac.sh)."""
    override = os.environ.get('MYFLAMES_DESKTOP_BUILD_VENV')
    venv = (Path(override).expanduser().absolute() if override
            else Path(root) / 'local' / 'desktop-build-venv')
    return venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def venv_python_version(python):
    return subprocess.check_output(
        [str(python), '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'],
        stderr=subprocess.DEVNULL, timeout=10).decode('ascii').strip()


def venv_freeze(python):
    """The build venv's resolved dependencies as sorted pip freeze lines."""
    output = subprocess.check_output([str(python), '-m', 'pip', 'freeze'],
                                     stderr=subprocess.DEVNULL, timeout=60).decode('utf-8', 'replace')
    return sorted(line.strip() for line in output.splitlines() if line.strip())


def freeze_digest(lines):
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def provision_venv(root, venv):
    venv = Path(venv).absolute()
    requirements = Path(root) / 'apps' / 'desktop' / 'requirements-build.txt'
    key = venv_key(sys.version.split()[0], requirements)
    stamp = venv / '.myflames-build-key.json'
    python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if venv.is_symlink():
        raise ValueError('build venv must not be a symlink')
    try:
        if read_json(stamp) == key and python.is_file():
            actual = subprocess.check_output([str(python), '-c',
                'import sys, PyInstaller; print("%d.%d" % sys.version_info[:2])'],
                stderr=subprocess.DEVNULL, timeout=10).decode('ascii').strip()
            if actual == key['python']:
                return
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if venv.exists():
        # A typo in the override must never recursively delete an unrelated folder.
        if not (venv / 'pyvenv.cfg').is_file():
            raise ValueError('refusing to recreate a non-venv folder: {}'.format(venv))
        shutil.rmtree(str(venv))
    venv.parent.mkdir(parents=True, exist_ok=True)
    run_command([sys.executable, '-m', 'venv', str(venv)], 'creating the build venv')
    run_command([str(python), '-m', 'pip', 'install', '-r', str(requirements)],
                'installing the pinned build requirements')
    write_json(stamp, key)


def new_state(inputs):
    return {'schema_version': SCHEMA, 'inputs': inputs, 'stages': {}, 'smokes': {}}


def _outputs(paths):
    return {os.path.abspath(str(path)): hash_path(path) for path in paths}


def _next(state):
    return next((stage for stage in STAGES if stage not in state.get('stages', {})), None)


def _effective_outputs(state):
    latest = {}
    for stage in STAGES:
        record = state.get('stages', {}).get(stage)
        if record is None:
            break
        latest.update(record['outputs'])
    pending = state.get('pending', {})
    if pending.get('stage') == _next(state) and pending.get('inputs') == state['inputs']:
        latest.update(pending.get('outputs', {}))
    return latest


def checkpoint(state, stage, paths, submission_id=None):
    if stage != _next(state) or stage not in ('dmg-notarize', 'staple'):
        raise ValueError('cannot checkpoint stage {}'.format(stage))
    outputs = _outputs(paths)
    pending = state.get('pending', {})
    if pending.get('stage') != stage or pending.get('outputs') != outputs:
        pending = {'stage': stage, 'inputs': state['inputs'], 'outputs': outputs}
    if submission_id is not None:
        pending['submission_id'] = submission_id
    state['pending'] = pending


def complete_stage(state, stage, paths):
    if stage != _next(state):
        raise ValueError('stage {} is out of order; next is {}'.format(stage, _next(state)))
    consumed = _effective_outputs(state)
    writers = DECLARED_WRITERS.get(stage, ())
    for consumed_path, expected in consumed.items():
        if any(consumed_path.endswith(suffix) for suffix in writers):
            continue
        try:
            current = hash_path(consumed_path)
        except (OSError, ValueError):
            current = None
        if current != expected:
            raise ValueError('stage {} cannot complete: consumed artifact {} changed since it was '
                             'recorded; rerun with --resume'.format(stage, consumed_path))
    record = {'inputs': state['inputs'], 'consumed': consumed,
              'outputs': _outputs(paths), 'completed_at': utc_now()}
    if not record['outputs']:
        raise ValueError('stage outputs must not be empty')
    pending = state.get('pending', {})
    if pending.get('stage') == stage:
        for key in ('submission_id', 'status'):
            if key in pending:
                record[key] = pending[key]
    state['stages'][stage] = record
    state.pop('pending', None)


def resume_stage(state, inputs):
    """Find the first unsafe/incomplete stage, including all earlier artifacts.

    Signing and stapling legitimately change the DMG. The latest completed or
    checkpointed writer supplies its current expected hash; earlier hashes stay
    in the history. An unexpected change restarts the earliest producer, since
    re-signing a corrupt input would only bless the corruption.
    """
    if state.get('schema_version') != SCHEMA or state.get('inputs') != inputs:
        return STAGES[0]
    records, earliest, latest = [], {}, {}
    incomplete = None
    for stage in STAGES:
        record = state.get('stages', {}).get(stage)
        if record is None:
            incomplete = stage
            break
        if record.get('inputs') != inputs or not isinstance(record.get('outputs'), dict) or not record['outputs']:
            incomplete = stage
            break
        records.append(stage)
        for path, digest in record['outputs'].items():
            earliest.setdefault(path, stage)
            latest[path] = digest
    pending = state.get('pending', {})
    if pending.get('stage') == incomplete and pending.get('inputs') == inputs:
        for path, digest in pending.get('outputs', {}).items():
            earliest.setdefault(path, incomplete)
            latest[path] = digest
    failures = []
    for path, expected in latest.items():
        try:
            if hash_path(path) != expected:
                failures.append(earliest[path])
        except (OSError, ValueError):
            failures.append(earliest[path])
    if failures:
        return min(failures, key=STAGES.index)
    return incomplete


def state_inputs(root, identity):
    manifest = check_manifest(root)
    path = Path(root) / 'apps' / 'desktop' / 'build' / 'backend' / 'build-manifest.json'
    return {'commit': manifest['commit'], 'ui_assets_sha256': manifest['ui_assets_sha256'],
            'manifest_sha256': sha256_file(path), 'identity': identity.upper()}


def smoke_matches(state, name, app):
    record = state.get('smokes', {}).get(name, {})
    return (record.get('inputs') == state['inputs'] and record.get('status') == 'passed'
            and record.get('app_sha256') == hash_path(app))


def prepare_state(path, inputs, resume, app):
    try:
        state = read_json(path) if resume else new_state(inputs)
        stage = resume_stage(state, inputs)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        state, stage = new_state(inputs), STAGES[0]
    if stage == 'app':
        state = new_state(inputs)
    if stage is None or STAGES.index(stage) > STAGES.index('verify'):
        if not all(smoke_matches(state, name, app) for name in SMOKES):
            stage = 'verify'
    if stage is not None:
        for later in STAGES[STAGES.index(stage):]:
            state['stages'].pop(later, None)
        if state.get('pending', {}).get('stage') != stage:
            state.pop('pending', None)
    write_json(path, state)
    return stage


TEAM_ID_PATTERN = r'\(([0-9A-Z]{10})\)'


def identity_team_id(find_identity_output):
    """Team ID of the signing identity, from `security find-identity` output.

    The identity name ends with "(TEAMID)": ten uppercase letters or digits.
    It is derived from the matched identity, never hard-coded, so a renewed or
    replaced certificate cannot leave a stale team behind.
    """
    match = re.search(TEAM_ID_PATTERN + r'"?\s*$', find_identity_output)
    if match is None:
        raise ValueError("the signing identity's name does not end with a team ID")
    return match.group(1)


def check_notarized_origin(assessment, team_id):
    """Refuse a notarization that is not this build's own team's.

    spctl accepts any notarized Developer ID artifact; only its origin line
    names the team. A missing origin or another team means the artifact was
    notarized by someone else and must not ship as this build.
    """
    if 'source=Notarized Developer ID' not in assessment:
        raise ValueError('Gatekeeper does not report a Notarized Developer ID source')
    origin = next((line for line in assessment.splitlines() if line.startswith('origin=')), None)
    match = re.search(TEAM_ID_PATTERN + r'\s*$', origin) if origin is not None else None
    if match is None:
        raise ValueError('spctl reported no notarization origin; refusing to trust the assessment')
    if match.group(1) != team_id:
        raise ValueError('artifact is notarized under team {}, but this build signed with team {}'.format(
            match.group(1), team_id))


def classify_notary_output(text):
    lower = text.lower()
    # Fatal classifications take precedence over incidental words like timeout.
    if 'required agreement' in lower or re.search(r'\b403\b', lower):
        return 'agreement'
    if (re.search(r'\b401\b', lower) or any(word in lower for word in (
            'unauthorized', 'invalid credentials', 'invalid credential', 'authentication failed',
            'invalid api key', 'no keychain', 'could not find a keychain', 'not authorized'))):
        return 'credentials'
    if re.search(r'"status"\s*:\s*"(?:invalid|rejected)"', lower) or 'status: invalid' in lower or 'status: rejected' in lower:
        return 'rejected'
    if (re.search(r'(?:http(?: status)?(?: code)?|status code|statuscode)\s*[:=]?\s*5\d\d\b', lower)
            or any(word in lower for word in (
                'could not resolve', 'couldn\'t resolve', 'name or service not known',
                'nodename nor servname', 'temporary failure in name resolution', 'dns',
                'connection reset', 'connection timed out', 'timed out', 'timeout',
                'network connection was lost', 'could not connect', 'couldn\'t connect',
                'nsurlerrordomain code=-1001', 'nsurlerrordomain code=-1003',
                'nsurlerrordomain code=-1004', 'nsurlerrordomain code=-1005'))):
        return 'transport'
    return 'fatal'


def _redact(text):
    for name in ('APPLE_API_KEY', 'APPLE_API_KEY_ID', 'APPLE_API_ISSUER',
                 'APPLE_KEYCHAIN_PROFILE', 'APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD'):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, '<redacted>')
    return text


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_command(command, label):
    """Keep child output visible and check its status at least every 30 seconds."""
    process = subprocess.Popen(command)
    try:
        elapsed = 0
        while True:
            try:
                status = process.wait(timeout=30)
                if status:
                    raise ValueError('{} failed (exit {})'.format(label, status))
                return
            except subprocess.TimeoutExpired:
                elapsed += 30
                sys.stderr.write('release-mac: {} is still running ({} s)\n'.format(label, elapsed))
    finally:
        _stop(process)


def run_notary(arguments, runner=None, sleeper=time.sleep):
    """Capture Apple's response without ever logging the command/credentials."""
    runner = runner or _notary_once
    attempts = MAX_ATTEMPTS if arguments[0] in ('submit', 'wait', 'info') else 1
    for attempt in range(attempts):
        status, stdout, stderr = runner(arguments)
        combined = stdout + '\n' + stderr
        category = classify_notary_output(combined)
        if status == 0 and category not in ('agreement', 'credentials', 'rejected'):
            if stderr:
                sys.stderr.write(_redact(stderr))
            return stdout
        sys.stderr.write(_redact(combined).rstrip() + '\n')
        if category == 'agreement':
            raise ValueError('accept the agreement at developer.apple.com')
        if category == 'credentials':
            raise ValueError('notarytool credentials were refused; check the configured notary sign-in')
        if category == 'rejected':
            raise ValueError('notarization was rejected; xcrun notarytool log <id> names the files')
        if category != 'transport' or attempt + 1 == attempts:
            raise ValueError('notarytool {} failed (output above)'.format(arguments[0]))
        delay = RETRY_BACKOFF[attempt]
        sys.stderr.write('release-mac: notarytool {} transport error; attempt {}/{} in {} s\n'.format(
            arguments[0], attempt + 2, attempts, delay))
        sleeper(delay)
    raise AssertionError('unreachable')


def _notary_credentials():
    # Shell validates and normalizes exactly one set, then passes it by env.
    if os.environ.get('APPLE_API_KEY'):
        return ['--key', os.environ['APPLE_API_KEY'], '--key-id', os.environ['APPLE_API_KEY_ID'],
                '--issuer', os.environ['APPLE_API_ISSUER']]
    return ['--keychain-profile', os.environ['APPLE_KEYCHAIN_PROFILE']]


def _notary_once(arguments):
    process = subprocess.Popen(['xcrun', 'notarytool'] + arguments + _notary_credentials(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        elapsed = 0
        while True:
            try:
                # Preflight must fail in seconds. Waiting for Apple remains
                # long-lived, with an observed process-status line every 30 s.
                stdout, stderr = process.communicate(timeout=10 if arguments[0] == 'history' else 30)
                return process.returncode, stdout.decode('utf-8', 'replace'), stderr.decode('utf-8', 'replace')
            except subprocess.TimeoutExpired:
                if arguments[0] == 'history':
                    _stop(process)
                    return 1, '', 'notarytool sign-in preflight timed out after 10 s'
                elapsed += 30
                sys.stderr.write('release-mac: notarytool {} still running ({} s)\n'.format(arguments[0], elapsed))
    finally:
        _stop(process)


def _notary_status(response):
    result = json.loads(response)
    if not isinstance(result, dict):
        raise ValueError('notarytool returned a non-object JSON response')
    return result


def notarize_dmg(path, dmg):
    state = read_json(path)
    pending = state.get('pending', {})
    if pending.get('stage') != 'dmg-notarize' or pending.get('outputs') != _outputs([dmg]):
        raise ValueError('DMG changed before notarization; rerun with --resume')
    submission = pending.get('submission_id')
    if not submission:
        response = run_notary(['submit', str(dmg), '--output-format', 'json'])
        result = _notary_status(response)
        submission = result.get('id')
        if not isinstance(submission, str) or not re.match(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$', submission):
            raise ValueError('notarytool submit did not return a submission ID')
        # Persist the ID and signed bytes BEFORE any wait. A late failure will
        # query this submission, rather than upload the DMG for a second time.
        checkpoint(state, 'dmg-notarize', [dmg], submission)
        write_json(path, state)
    else:
        response = run_notary(['info', submission, '--output-format', 'json'])
        result = _notary_status(response)
    if result.get('status') != 'Accepted':
        if result.get('status') not in (None, 'In Progress', 'InProgress', 'Uploaded'):
            sys.stderr.write(_redact(response).rstrip() + '\n')
            raise ValueError('notarization ended with "{}"; xcrun notarytool log <id> names the files'.format(result.get('status')))
        response = run_notary(['wait', submission, '--output-format', 'json'])
        result = _notary_status(response)
    if result.get('status') != 'Accepted':
        sys.stderr.write(_redact(response).rstrip() + '\n')
        raise ValueError('notarization ended with "{}"; xcrun notarytool log <id> names the files'.format(result.get('status')))
    state = read_json(path)
    state['pending']['status'] = 'Accepted'
    write_json(path, state)


def preflight(root, commit, python, venv, cache):
    root = Path(root)
    if not re.match(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$', commit) or git_head(root) != commit:
        raise ValueError('first argument must be the explicit full commit SHA equal to HEAD')
    dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'])
    if dirty.strip():
        raise ValueError('git tree must be clean (git status --porcelain must be empty)')
    for command in ('node', 'npm', python):
        if not shutil.which(command):
            raise ValueError('{} is required before building'.format(command))
    version = subprocess.check_output(['node', '--version'], timeout=5).decode('ascii').strip().lstrip('v')
    if tuple(int(part) for part in version.split('.')[:2]) < (22, 12):
        raise ValueError('Node.js 22.12 or newer is required')
    subprocess.check_output(['npm', '--version'], timeout=5)
    subprocess.check_output([python, '-c', 'import venv, sys; assert sys.version_info >= (3, 7)'], timeout=5)
    lock = read_json(root / 'apps' / 'desktop' / 'mysql-client.lock.json')
    url = urlparse(lock['archives']['darwin-arm64']['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('client lock must name an HTTPS download host without credentials')
    for host in (NOTARY_HOST, url.hostname):
        try:
            subprocess.check_call([sys.executable, str(Path(__file__).resolve()), 'resolve', host],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('DNS resolution failed for {}'.format(host))
    # Check each potentially separate volume before creating anything.
    for location in (root, Path(venv), Path(cache)):
        existing = location.absolute()
        while not existing.exists():
            existing = existing.parent
        if shutil.disk_usage(str(existing)).free < MIN_FREE_BYTES:
            raise ValueError('at least 5 GiB of free disk space is required at {}'.format(location))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('command', choices=('manifest-check', 'preflight', 'resolve', 'venv', 'prepare',
                        'next', 'complete', 'checkpoint', 'pending', 'notary', 'notarize-dmg',
                        'team-id', 'notarized-origin', 'smoke-needed', 'smoke-complete', 'run'))
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument('--state')
    parser.add_argument('--identity')
    parser.add_argument('--commit')
    parser.add_argument('--python')
    parser.add_argument('--venv')
    parser.add_argument('--cache')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--app', default='dist/mac-arm64/myflames.app')
    parser.add_argument('--label', default='release command')
    args, rest = parser.parse_known_args(argv)
    if rest[:1] == ['--']:
        rest = rest[1:]
    path = args.state or str(Path(args.root) / 'apps' / 'desktop' / 'build' / 'release-state.json')
    if args.command == 'manifest-check':
        check_manifest(args.root)
    elif args.command == 'preflight':
        preflight(args.root, args.commit or '', args.python, args.venv, args.cache)
    elif args.command == 'resolve':
        socket.getaddrinfo(rest[0], 443, type=socket.SOCK_STREAM)
    elif args.command == 'venv':
        provision_venv(args.root, args.venv)
    elif args.command == 'notary':
        sys.stdout.write(run_notary(rest))
    elif args.command == 'run':
        run_command(rest, args.label)
    elif args.command == 'team-id':
        sys.stdout.write(identity_team_id(sys.stdin.read()) + '\n')
    elif args.command == 'notarized-origin':
        if not rest:
            raise ValueError('notarized-origin needs the expected team ID')
        check_notarized_origin(sys.stdin.read(), rest[0])
    elif args.command == 'prepare':
        print(prepare_state(path, state_inputs(args.root, args.identity), args.resume, args.app) or 'done')
    elif args.command == 'notarize-dmg':
        notarize_dmg(path, rest[0])
    else:
        state = read_json(path)
        if args.command == 'next':
            print(_next(state) or 'done')
        elif args.command == 'pending':
            return 0 if state.get('pending', {}).get('stage') == rest[0] else 1
        elif args.command == 'checkpoint':
            checkpoint(state, rest[0], rest[1:])
            write_json(path, state)
        elif args.command == 'complete':
            if rest[0] == 'dmg-notarize' and state.get('pending', {}).get('status') != 'Accepted':
                raise ValueError('DMG notarization has not been accepted')
            if rest[0] == 'verify' and not all(smoke_matches(state, name, args.app) for name in SMOKES):
                raise ValueError('both packaged settings smokes must pass before verify')
            complete_stage(state, rest[0], rest[1:])
            write_json(path, state)
        elif args.command == 'smoke-needed':
            return 1 if smoke_matches(state, rest[0], args.app) else 0
        elif args.command == 'smoke-complete':
            if rest[0] not in SMOKES:
                raise ValueError('unknown packaged smoke')
            state['smokes'][rest[0]] = {'inputs': state['inputs'], 'status': 'passed',
                                       'app_sha256': hash_path(args.app), 'completed_at': utc_now()}
            write_json(path, state)
    return 0


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.stderr.write('release-mac: {}\n'.format(_redact(str(error))))
        sys.exit(2)
    except KeyboardInterrupt:
        sys.stderr.write('release-mac: interrupted\n')
        sys.exit(130)
