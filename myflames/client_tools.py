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
    copies = []
    probes = 0
    for candidate in snapshot(path, common_dirs, bundled):
        if job:
            job.check_cancel()
        name, binary = candidate['name'], candidate['path']
        folder = os.path.dirname(candidate['realpath'])
        origin, label = _origin(folder, candidate['bundled'])
        entry = dict(name=name, path=binary, realpath=candidate['realpath'],
                     installation=None, origin=origin, origin_label=label, reason=None,
                     warning=None, available=False, deprecated=name == 'mysqlpump')
        copies.append(entry)
        try:
            if candidate['source'] == 'common':
                connector._check_client_trust(binary)
        except ValueError as exc:
            entry['error'] = str(exc)
            skipped.append(dict(candidate, available=False, error=str(exc), reason=str(exc)))
            continue
        if name == 'mysqlpump':
            fields = dict(available=True, version_tuple=[0, 0, 0], engine='mysql')
        else:
            if probes >= 32:
                entry['error'] = 'probe limit'
                skipped.append(dict(candidate, available=False, error='probe limit', reason='probe limit'))
                continue
            probes += 1
            fields = probe(name, binary, refresh=refresh, job=job)
            if fields['available']:
                fields['version_tuple'] = list(_banner_version(fields['version'], name))
                if fields['version_tuple'] == [0, 0, 0]:
                    fields.update(available=False,
                                  error='Version probe failed or returned an unrecognized version.')
        entry.update(copy.deepcopy(fields))
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
        entry['installation'] = key
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
                                                    s['order'], s['realpath'], s['path'])), skipped=skipped, copies=copies)


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
    # Loading must stay with the selected dump installation. An incomplete
    # installation is a missing load, never authority to use another client.
    if result['dump'] and source and not _program(source, 'client'):
        result['load'] = None
        if not any(m['role'] == 'load' for m in result['missing']):
            missing('load', defaults['load'], bool(chosen_key))
    if result['load']:
        load = result['load']
        if load['reason'] != 'chosen':
            load_series = server_info.parse_version_tuple(load['version'])[:2]
            load['reason'] = ('series' if load_series == series else
                              'newer' if load_series > series else 'older')
        if load['reason'] == 'older':
            load['warning'] = '%s %s is older than this server (%s %s.%s).' % (
                load['program'], load['version'], 'MySQL' if engine == 'mysql' else 'MariaDB',
                series[0], series[1])
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


def choice_keys(discovered, choices):
    """Resolve stored spellings without silently replacing unavailable choices."""
    keys = dict(mysql=None, mariadb=None)
    errors = dict(mysql=None, mariadb=None)
    for engine in keys:
        value = (choices or {}).get(engine)
        if value is None:
            continue
        if os.name == 'nt' and value.replace('/', '\\').startswith('\\\\'):
            errors[engine] = 'Choose a local client named mysql.exe or mariadb.exe.'
            continue
        folder = None if value == 'bundled' else os.path.realpath(
            value if os.path.isdir(value) else os.path.dirname(os.path.realpath(value)))
        group = next((g for g in discovered.get('installations', [])
                      if g['engine'] == engine and
                      (g['bundled'] if value == 'bundled' else connector._same_dir(g['folder'], folder))
                      and _program(g, 'client')), None)
        try:
            if group is None:
                raise ValueError('The installation is unavailable.')
            program = (group['programs'][_program(group, 'client')]['path'] if value == 'bundled'
                       else _choice_program(engine, value))
            connector._check_client_trust(program)
            connector._check_client_trust(os.path.realpath(program))
        except (OSError, ValueError):
            errors[engine] = ('The client tools chosen in Settings > Client tools are no longer at %s.' % value)
        else:
            keys[engine] = group['key']
    return keys, errors


def _choice_program(engine, value):
    """Find one client in a stored folder without splitting PATH separators."""
    if (not isinstance(value, str) or not os.path.isabs(value) or
            any(c in value for c in '\0\r\n')):
        raise ValueError('Choose an absolute path to the client tools.')
    if os.name == 'nt' and value.replace('/', '\\').startswith('\\\\'):
        raise ValueError('Choose a local client named mysql.exe or mariadb.exe.')
    program = value
    if os.path.isdir(value):
        names = ('mariadb', 'mysql') if engine == 'mariadb' else ('mysql', 'mariadb')
        program = next((os.path.join(value, n + ('.exe' if os.name == 'nt' else ''))
                        for n in names if os.path.isfile(os.path.join(
                            value, n + ('.exe' if os.name == 'nt' else '')))), None)
        if program is None:
            raise ValueError('Choose a folder holding a mysql or mariadb client program.')
    name = os.path.basename(program)
    allowed = ('mysql.exe', 'mariadb.exe') if os.name == 'nt' else ('mysql', 'mariadb')
    if (name.lower() if os.name == 'nt' else name) not in allowed:
        raise ValueError('Choose a mysql or mariadb client program.')
    return program


def validate_choice(engine, path):
    """Validate a client or its folder before it can receive server credentials."""
    if engine not in ('mysql', 'mariadb'):
        raise ValueError('Choose engine mysql or mariadb.')
    if path is None:
        return None
    bundled = path == 'bundled'
    value = connector.bundled_client_dir() if bundled else path
    if (not isinstance(value, str) or not value or len(value) > 4096 or
            any(c in value for c in '\0\r\n') or not os.path.isabs(value)):
        raise ValueError('Choose an absolute path to the client tools.')
    # Refuse network/device paths before any stat can contact a remote host.
    if os.name == 'nt' and value.replace('/', '\\').startswith('\\\\'):
        raise ValueError('Choose a local client named mysql.exe or mariadb.exe.')
    program = _choice_program(engine, value)
    connector._check_client_trust(program)
    connector._check_client_trust(os.path.realpath(program))
    banner = connector.probe_client(os.path.realpath(program), chosen=program)['version']
    if server_info.detect_flavor(banner) != engine:
        raise ValueError('The chosen client tools are for %s, not %s.' % (
            server_info.detect_flavor(banner), engine))
    if connector.bundled_client_dir() and connector._same_dir(
            os.path.dirname(program), connector.bundled_client_dir()):
        return 'bundled'
    # Keep the person's folder or stable symlink spelling across upgrades.
    return path


def discover_choices(choices, refresh=False):
    """Include trusted Settings folders even when they are outside PATH.

    A stale or newly untrusted selection is retained by choice_keys as an error,
    and is never executed merely to describe Settings.
    """
    discovered = discover(refresh=refresh)
    for engine in ('mysql', 'mariadb'):
        value = (choices or {}).get(engine)
        if not value or value == 'bundled':
            continue
        try:
            program = _choice_program(engine, value)
            connector._check_client_trust(program)
            connector._check_client_trust(os.path.realpath(program))
        except (ValueError, OSError):
            continue
        folder = value if os.path.isdir(value) else os.path.dirname(value)
        extra = (discover(path='', common_dirs=(folder,), bundled=False, refresh=refresh)
                 if os.name != 'nt' else discover(path='', common_dirs=(), bundled=folder, refresh=refresh))
        for group in extra['installations']:
            if not any(g['key'] == group['key'] for g in discovered['installations']):
                group['order'] += len(discovered['installations'])
                group['bundled'] = False
                group['origin'], group['origin_label'] = _origin(group['folder'], False)
                if group['origin'] == 'path':
                    group.update(origin='folder', origin_label='Chosen folder')
                discovered['installations'].append(group)
        for shell in extra['shells']:
            if not any(s['realpath'] == shell['realpath'] for s in discovered['shells']):
                discovered['shells'].append(shell)
        for skipped in extra['skipped']:
            if not any(s['path'] == skipped['path'] for s in discovered['skipped']):
                discovered['skipped'].append(skipped)
    return discovered


def discover_lifecycle(path=None, common_dirs=None, choices=None, refresh=False, job=None):
    """Lifecycle inventory, including trusted Settings folders outside PATH.

    Explicit search paths retain the historical PATH-only test/caller seam.
    Copies keep search order and probe banners; installations drive selection.
    """
    discovered = discover(path=path, common_dirs=common_dirs,
                          bundled=None if path is None else False, refresh=refresh, job=job)
    for engine in ('mysql', 'mariadb'):
        value = (choices or {}).get(engine)
        if not value or value == 'bundled':
            continue
        try:
            program = _choice_program(engine, value)
            connector._check_client_trust(program)
            connector._check_client_trust(os.path.realpath(program))
        except (ValueError, OSError):
            continue
        folder = value if os.path.isdir(value) else os.path.dirname(value)
        # Avoid probing known files a second time, particularly on refresh.
        positions = {c['realpath']: i for i, c in enumerate(discovered['copies'])}
        known = {c['realpath'] for c in discovered['copies'] if c.get('error') != 'probe limit'}
        candidates = snapshot(path='', common_dirs=(folder,), bundled=False) if os.name != 'nt' else (
            snapshot(path='', common_dirs=(), bundled=folder))
        if all(c['realpath'] in known for c in candidates):
            continue
        extra = (discover(path='', common_dirs=(folder,), bundled=False, refresh=refresh, job=job)
                 if os.name != 'nt' else discover(path='', common_dirs=(), bundled=folder, refresh=refresh, job=job))
        for group in extra['installations']:
            old = next((g for g in discovered['installations'] if g['key'] == group['key']), None)
            if old:
                old['programs'].update({n: p for n, p in group['programs'].items() if n not in old['programs']})
            else:
                group['order'] += len(discovered['copies'])
                group['bundled'] = False
                group['origin'], group['origin_label'] = _origin(group['folder'], False)
                if group['origin'] == 'path':
                    group.update(origin='folder', origin_label='Chosen folder')
                discovered['installations'].append(group)
        for entry in extra['copies']:
            if entry['realpath'] not in known:
                group = next((g for g in discovered['installations'] if g['key'] == entry['installation']), None)
                if group:
                    entry.update(origin=group['origin'], origin_label=group['origin_label'])
                else:
                    origin, label = _origin(os.path.dirname(entry['realpath']), False)
                    entry.update(origin='folder' if origin == 'path' else origin,
                                 origin_label='Chosen folder' if origin == 'path' else label)
                position = positions.get(entry['realpath'])
                if position is None:
                    discovered['copies'].append(entry)
                else:
                    discovered['copies'][position] = entry
                    discovered['skipped'] = [s for s in discovered['skipped']
                                             if s['realpath'] != entry['realpath']]
                known.add(entry['realpath'])
        for shell in extra['shells']:
            if not any(s['realpath'] == shell['realpath'] for s in discovered['shells']):
                origin, label = _origin(os.path.dirname(shell['realpath']), False)
                shell.update(origin='folder' if origin == 'path' else origin,
                             origin_label='Chosen folder' if origin == 'path' else label)
                discovered['shells'].append(shell)
        for skipped in extra['skipped']:
            if not any(s['path'] == skipped['path'] for s in discovered['skipped']):
                discovered['skipped'].append(skipped)
    return discovered


def select_program(discovered, server, name, choice=None):
    """Rank an auxiliary program with the same engine/series/choice policy.

    Reuse the dump role on an independent projection, so future ranking fixes
    also apply to maintenance-only programs such as mariadb-upgrade.
    """
    projected = dict(installations=[], shells=[])
    program = 'mariadb-dump' if server['engine'] == 'mariadb' else 'mysqldump'
    for group in discovered.get('installations', []):
        projected['installations'].append(dict(group, programs=(
            {program: group['programs'][name]} if name in group['programs'] else {})))
    used = select(projected, server, choice)['dump']
    if used:
        used['program'] = name
        if used.get('warning'):
            used['warning'] = used['warning'].replace(program, name, 1).split(' Install ', 1)[0]
    return used
