"""Local client inventory and pure, version-aware role selection (Python 3.7+).

Only version probes execute programs. Common-folder discoveries reuse the
connector's ownership/mode checks. Its Windows name allowlist must grow to
include non-client programs before later briefs check selected copies there.
Unavailable probes and refused/limited candidates appear in ``skipped``;
mysqlpump is presence-only, deprecated, and never a selection candidate.
"""
import collections
import copy
import os
import re
import subprocess
import sys
import tempfile
import threading

from . import connector, server_info

PROGRAMS = ('mysql', 'mariadb', 'mysqldump', 'mariadb-dump', 'mariadb-backup',
            'mariadb-upgrade', 'mysqlpump', 'mysqlsh')
_PROBE_CACHE = collections.OrderedDict()
_PROBE_LOCK = threading.Lock()
_PROBE_FLIGHTS = {}
_PROBE_LIMIT = 64
_FACTS = collections.OrderedDict()
_FACTS_LOCK = threading.Lock()


def _probe_key(name, binary):
    try:
        info = os.stat(binary)
        return (name, os.path.realpath(binary), info.st_dev, info.st_ino,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        return None


def _banner_version(banner, name):
    # mysql-server: client/mysql.cc:2104, client/mysqldump.cc:1081 call
    # mysys/print_version.cc:54-58, which prefixes the banner with argv[0].
    # mariadb-server: client/mysql.cc:1998-2003 and client/mysqldump.cc:960
    # (include/welcome_copyright_notice.h:32-36) print the distribution first.
    # mysql-server tag mysql-5.7.44: client/mysqldump.c:656-659 prints
    # "Ver <client version> Distrib <server version>"; Distrib wins over Ver.
    for pattern in (r'\bDistrib\s+(\d+\.\d+\.\d+)',
                    r'\b(\d+\.\d+\.\d+)-MariaDB',
                    r'\bVer\s+(\d+\.\d+\.\d+)'):
        match = re.search(pattern, banner, re.I)
        if match:
            return tuple(int(n) for n in match.group(1).split('.'))
    # Shell and backup/upgrade also use a bare program-name version banner.
    match = re.search(r'\b' + re.escape(name) + r'(?:\.exe)?\s+(?:version\s+)?'
                      r'(\d+\.\d+\.\d+)', banner, re.I)
    return tuple(int(n) for n in match.group(1).split('.')) if match else (0, 0, 0)


def probe(name, binary, refresh=False, job=None):
    """Return independent lifecycle-compatible banner fields; successes use LRU.

    Concurrent requests for an identity share its in-flight result, including
    failures, but failures are never retained for subsequent calls. No child
    runs under the global lock. An identity changed during probing is not cached.
    """
    from . import lifecycle
    if job:
        job.check_cancel()
    key = _probe_key(name, binary)
    with _PROBE_LOCK:
        if key is not None and not refresh and key in _PROBE_CACHE:
            _PROBE_CACHE.move_to_end(key)
            return copy.deepcopy(_PROBE_CACHE[key])
        flight = _PROBE_FLIGHTS.get(key) if key is not None else None
        owner = flight is None
        if owner:
            flight = {'done': threading.Event(), 'result': None}
            if key is not None:
                _PROBE_FLIGHTS[key] = flight
    if not owner:
        while not flight['done'].wait(0.1):
            if job:
                job.check_cancel()
        if flight['result'] is None:
            raise RuntimeError('Version probe was interrupted.')
        return copy.deepcopy(flight['result'])
    result = {'available': False}
    try:
        try:
            with tempfile.TemporaryDirectory(prefix='myflames-probe-') as home:
                child = subprocess.run([os.path.abspath(binary), '--version'],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       timeout=8, env=lifecycle._clean_env(home))
            banner = child.stdout.decode('utf-8', 'replace').strip()[:4096]
            # Preserve lifecycle's historical tuple semantics here. Discovery
            # derives its distribution version from this cached banner instead.
            version = server_info.tool_version_tuple(banner)
            result.update(version=banner, version_tuple=list(version),
                          engine=server_info.detect_flavor(banner),
                          available=child.returncode == 0 and version != (0, 0, 0))
            if not result['available']:
                result['error'] = 'Version probe failed or returned an unrecognized version.'
        except (OSError, subprocess.TimeoutExpired) as exc:
            result['error'] = type(exc).__name__
        with _PROBE_LOCK:
            if (result['available'] and _banner_version(result['version'], name) != (0, 0, 0)
                    and key is not None and _probe_key(name, binary) == key):
                _PROBE_CACHE[key] = copy.deepcopy(result)
                _PROBE_CACHE.move_to_end(key)
                while len(_PROBE_CACHE) > _PROBE_LIMIT:
                    _PROBE_CACHE.popitem(last=False)
            flight['result'] = copy.deepcopy(result)
        return result
    finally:
        with _PROBE_LOCK:
            if key is not None:
                _PROBE_FLIGHTS.pop(key, None)
            flight['done'].set()


def snapshot(path=None, common_dirs=None, bundled=None):
    """Return stat-only candidates [{name,path,realpath,identity,found_in,
    bundled,order,source}]. Aliases share one entry, preferring MariaDB names.
    No trust check or subprocess runs here; this is not execution authority.
    """
    from . import lifecycle
    bundle = connector.bundled_client_dir() if bundled is None else bundled
    folders = []
    for folder in (os.environ.get('PATH', '') if path is None else path).split(os.pathsep):
        if folder and not (bundle and connector._same_dir(folder, bundle)):
            folders.append((folder, 'path'))
    if os.name != 'nt':
        folders.extend((folder, 'common') for folder in
                       (lifecycle.COMMON_BIN_DIRS if common_dirs is None else common_dirs)
                       if not (bundle and connector._same_dir(folder, bundle)))
    if bundle:
        folders.append((bundle, 'bundled'))
    copies = {}
    for folder, source in folders:
        for name in PROGRAMS:
            binary = os.path.abspath(os.path.join(folder, name + ('.exe' if os.name == 'nt' else '')))
            if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
                continue
            identity = _probe_key(name, binary)
            if identity is None:
                continue
            real = identity[1]
            existing = copies.get(real)
            if existing is not None:
                # Keep the earliest source's trust status and ordering, while
                # retaining the MariaDB spelling of compatibility symlinks.
                if name.startswith('mariadb') and not existing['name'].startswith('mariadb'):
                    existing.update(name=name, path=binary, identity=identity, found_in=folder)
                continue
            copies[real] = dict(name=name, path=binary, realpath=real, identity=identity,
                                found_in=folder, bundled=bool(bundle and connector._same_dir(
                                    os.path.dirname(real), bundle)),
                                order=len(copies), source=source)
    return sorted(copies.values(), key=lambda c: (c['order'], c['realpath'], c['name']))


def _origin(folder, bundled):
    if bundled:
        return 'bundled', 'Bundled with myflames'
    normalized = folder.replace('\\', '/')
    brew = re.search(r'/Cellar/([^/]+)/', normalized)
    if brew:
        return 'homebrew', 'Homebrew ' + brew.group(1)
    table = ((r'^/opt/local(?:/|$)', 'macports', 'MacPorts'),
             (r'^/usr/local/mysql(?:[-/]|$)', 'mysql-installer', 'MySQL installer'),
             (r'/MySQL/MySQL Workbench(?:[^/]*)(?:/|$)', 'workbench', 'MySQL Workbench'),
             (r'/MySQL/MySQL Server(?:[^/]*)(?:/|$)', 'mysql-installer', 'MySQL installer'),
             (r'^[A-Za-z]:/.*/MariaDB[^/]*(?:/|$)', 'mariadb-installer', 'MariaDB'))
    for pattern, origin, label in table:
        if re.search(pattern, normalized, re.I):
            return origin, label
    if sys.platform.startswith('linux') and normalized in ('/usr/bin', '/usr/sbin'):
        return 'system', 'System package'
    return 'path', 'On PATH'


# Verified names: mysql-server libmysql/authentication_ldap/CMakeLists.txt:77,
# authentication_kerberos/CMakeLists.txt:76, authentication_oci_client/CMakeLists.txt:39,
# fido_client/authentication_webauthn/webauthn_client_plugin.cc; mariadb-server
# plugin/auth_ed25519/CMakeLists.txt:31, auth_gssapi/CMakeLists.txt:59,
# auth_parsec/server_parsec.cc:297. libmariadb is an empty submodule in this checkout.
_AUTH_PLUGINS = frozenset(('authentication_ldap_sasl_client',
                           'authentication_kerberos_client', 'authentication_oci_client',
                           'authentication_webauthn_client',
                           'client_ed25519', 'auth_gssapi_client', 'parsec'))


def _has_auth_plugins(folder):
    for relative in ('../lib/plugin', '../lib/mysql/plugin', '../lib64/mysql/plugin'):
        directory = os.path.join(folder, relative)
        try:
            for name in os.listdir(directory):
                if (os.path.splitext(name)[0] in _AUTH_PLUGINS and
                        os.path.splitext(name)[1].lower() in ('.so', '.dll', '.dylib') and
                        os.path.isfile(os.path.join(directory, name))):
                    return True
        except OSError:
            pass
    return False


def discover(path=None, common_dirs=None, bundled=None, refresh=False, job=None):
    """Return {installations: [...], shells: [...], skipped: [...]}.

    At most 32 version-probe requests per call, including cached requests.
    mysqlpump is stat-only. Skipped records contain candidate metadata plus
    available=False and error (also reason); presence records are deprecated.
    """
    groups, shells, skipped = {}, [], []
    probes = 0
    for candidate in snapshot(path, common_dirs, bundled):
        if job:
            job.check_cancel()
        name, binary = candidate['name'], candidate['path']
        try:
            if candidate['source'] == 'common':
                connector._check_client_trust(binary)
        except ValueError as exc:
            skipped.append(dict(candidate, available=False, error=str(exc), reason=str(exc)))
            continue
        if name == 'mysqlpump':
            fields = dict(available=True, version_tuple=[0, 0, 0], engine='mysql')
        else:
            if probes >= 32:
                skipped.append(dict(candidate, available=False, error='probe limit', reason='probe limit'))
                continue
            probes += 1
            fields = probe(name, binary, refresh=refresh, job=job)
            if fields['available']:
                fields['version_tuple'] = list(_banner_version(fields['version'], name))
                if fields['version_tuple'] == [0, 0, 0]:
                    fields.update(available=False,
                                  error='Version probe failed or returned an unrecognized version.')
        if not fields['available']:
            skipped.append(dict(candidate, reason=fields.get('error'), **fields))
            continue
        version_tuple = tuple(fields['version_tuple'])
        version = '.'.join(str(n) for n in version_tuple) if any(version_tuple) else None
        folder = os.path.dirname(candidate['realpath'])
        origin, label = _origin(folder, candidate['bundled'])
        if name == 'mysqlsh':
            shells.append(dict(path=binary, realpath=candidate['realpath'], version=version,
                               version_tuple=version_tuple, origin=origin, origin_label=label,
                               order=candidate['order']))
            continue
        key = fields['engine'] + ':' + folder
        if key not in groups:
            groups[key] = dict(key=key, engine=fields['engine'], folder=folder,
                               found_in=candidate['found_in'], origin=origin, origin_label=label,
                               bundled=candidate['bundled'], has_auth_plugins=_has_auth_plugins(folder),
                               order=candidate['order'], programs={})
        program = dict(path=binary, realpath=candidate['realpath'], version=version)
        if name == 'mysqlpump':
            program['deprecated'] = True
        groups[key]['programs'][name] = program
    for group in groups.values():
        preferred = next((group['programs'][name] for name in
                          ('mariadb', 'mysql', 'mariadb-dump', 'mysqldump')
                          if name in group['programs']), {})
        group['version'] = preferred.get('version')
        group['version_tuple'] = server_info.parse_version_tuple(group['version'])
        group['series'] = '%s.%s' % group['version_tuple'][:2]
    return dict(installations=sorted(groups.values(), key=lambda g: (g['order'], g['key'])),
                shells=sorted(shells, key=lambda s: (tuple(-v for v in s['version_tuple']),
                                                    s['order'], s['realpath'], s['path'])), skipped=skipped)


def _server(server):
    if not server or server.get('engine') not in ('mysql', 'mariadb'):
        return None
    version = server_info.parse_version_tuple(server.get('version'), leading=True)
    if version[0] < 5:
        return None
    return dict(engine=server['engine'], version=server['version'])


def _program(group, role):
    names = {'client': ('mariadb', 'mysql') if group['engine'] == 'mariadb' else ('mysql',),
             'dump': ('mariadb-dump', 'mysqldump') if group['engine'] == 'mariadb' else ('mysqldump',),
             'backup': ('mariadb-backup',)}[role]
    return next((name for name in names if name in group['programs']), None)


def _use(group, role, program, reason, warning=None):
    item = group['programs'][program]
    return dict(role=role, program=program, path=item['path'], realpath=item['realpath'],
                version=item['version'], version_tuple=server_info.parse_version_tuple(item['version']),
                engine=group['engine'], installation=group['key'], origin=group['origin'],
                origin_label=group['origin_label'], bundled=group['bundled'], reason=reason, warning=warning)


def select(discovered, server=None, choice=None):
    """Pure selection: {server,client,dump,load,backup,shell,missing:[],notes:[]}.

    Program versions drive each role's compatibility, even in mixed-version
    folders. Final tie-breaks include stable keys/paths, independent of list order.
    """
    server = _server(server)
    result = dict(server=server, client=None, dump=None, load=None, backup=None,
                  shell=None, missing=[], notes=[])
    groups = sorted(discovered.get('installations', []), key=lambda g: (g['order'], g['key']))

    def fallback(reason):
        candidates = [(g, name) for g in groups for name in ('mysql', 'mariadb')
                      if name in g['programs'] and (not g['bundled'] or name == 'mysql')]
        candidates.sort(key=lambda pair: (pair[0]['bundled'], pair[1] != 'mysql',
                                           pair[0]['order'], pair[0]['key'], pair[1]))
        return _use(candidates[0][0], 'client', candidates[0][1], reason) if candidates else None

    def missing(role, program, chosen=False):
        text = ('The installation chosen in Settings > Client tools has no %s.' % program
                if chosen else 'No usable %s was found.' % program)
        result['missing'].append(dict(role=role, program=program, text=text))

    if server is None:
        result['client'] = fallback('unknown_server')
        return result
    engine = server['engine']
    own = [g for g in groups if g['engine'] == engine]
    chosen_key = (choice or {}).get(engine)
    chosen = next((g for g in own if g['key'] == chosen_key), None)
    roles = ('client', 'dump', 'backup') if engine == 'mariadb' else ('client', 'dump')
    defaults = {'client': 'mariadb' if engine == 'mariadb' else 'mysql',
                'dump': 'mariadb-dump' if engine == 'mariadb' else 'mysqldump',
                'backup': 'mariadb-backup', 'load': 'mariadb' if engine == 'mariadb' else 'mysql'}
    if not own and not chosen_key:
        result['client'] = fallback('other_engine')
        for role in roles[1:] + ('load',):
            missing(role, defaults[role])
        if engine == 'mariadb':
            result['notes'].append('No MariaDB client tools were found; the MySQL client reads this server. '
                                   'Dumps and restores need MariaDB client tools.')
    else:
        series = server_info.parse_version_tuple(server['version'], leading=True)[:2]
        for role in roles:
            candidates = [(g, _program(g, role)) for g in own]
            candidates = [(g, name) for g, name in candidates if name]
            reason = 'chosen'
            if chosen_key:
                candidates = [(chosen, _program(chosen, role))] if chosen and _program(chosen, role) else []
            elif candidates:
                def rank(pair):
                    g, name = pair
                    v = server_info.parse_version_tuple(g['programs'][name]['version'])
                    s = v[:2]
                    band = 0 if s == series else (1 if s > series else 2)
                    target = s if band == 1 else tuple(-n for n in s)
                    return (band, target, not g['has_auth_plugins'], tuple(-n for n in v),
                            g['bundled'], g['order'], g['key'], name, g['programs'][name]['realpath'])
                candidates.sort(key=rank)
                version = server_info.parse_version_tuple(candidates[0][0]['programs'][candidates[0][1]]['version'])
                reason = 'series' if version[:2] == series else ('newer' if version[:2] > series else 'older')
            if not candidates:
                missing(role, defaults[role], bool(chosen_key))
                continue
            group, name = candidates[0]
            warning = None
            if reason == 'older':
                label = 'MySQL' if engine == 'mysql' else 'MariaDB'
                warning = '%s %s is older than this server (%s %s.%s).' % (
                    name, group['programs'][name]['version'], label, series[0], series[1])
                if role == 'dump':
                    warning += ' Install %s %s.%s client tools for a complete dump.' % (label, series[0], series[1])
            result[role] = _use(group, role, name, reason, warning)
        dump = result['dump']
        source = next((g for g in own if dump and g['key'] == dump['installation']), None)
        if source:
            name = _program(source, 'client')
            if name:
                result['load'] = _use(source, 'load', name, dump['reason'])
            elif result['client']:
                result['load'] = dict(result['client'], role='load')
            else:
                missing('load', defaults['load'], bool(chosen_key))
        elif result['client']:
            result['load'] = dict(result['client'], role='load')
        else:
            missing('load', defaults['load'], bool(chosen_key))
    if engine == 'mysql' and discovered.get('shells'):
        shell = min(discovered['shells'], key=lambda s: (tuple(-n for n in s['version_tuple']),
                                                       s['order'], s['realpath'], s['path']))
        result['shell'] = dict(shell, role='shell', program='mysqlsh', engine='mysql',
                               installation='mysql:' + os.path.dirname(shell['realpath']),
                               bundled=shell['origin'] == 'bundled', reason='newest', warning=None)
        result['shell'].pop('order', None)
    return result


def endpoint_key(options):
    """Endpoint identity (host,port,socket,ssh_host,ssh_user,ssh_port).

    Missing host/ports normalize to localhost/3306/22; text is otherwise kept,
    so localhost and 127.0.0.1 never alias. User/TLS/password are not identity.
    """
    options = options or {}
    return (str(options.get('host') or 'localhost'), str(options.get('port') or 3306),
            str(options.get('socket') or ''), str(options.get('ssh_host') or ''),
            str(options.get('ssh_user') or ''), str(options.get('ssh_port') or 22))


def remember_server(key, engine, version):
    """Remember valid server facts in a thread-safe 256-endpoint LRU; return None."""
    facts = _server(dict(engine=engine, version=version))
    if facts is None:
        return
    with _FACTS_LOCK:
        _FACTS[key] = facts
        _FACTS.move_to_end(key)
        while len(_FACTS) > 256:
            _FACTS.popitem(last=False)


def server_facts(key):
    """Return an independent {engine,version} or None and mark the key recently used."""
    with _FACTS_LOCK:
        facts = _FACTS.get(key)
        if facts is not None:
            _FACTS.move_to_end(key)
            return dict(facts)
    return None
