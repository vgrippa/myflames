"""Import non-secret MySQL connection fields from two known tool formats.

Only fixed default files or a native chooser's result can be read. No caller
path, credentials file, password store, JDBC URL or key file is ever opened.
"""
import json
import os
import stat
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
MAX_CONNECTIONS = 1000
FILENAMES = {'workbench': 'connections.xml', 'dbeaver': 'data-sources.json'}
_DIALOG_LOCK = threading.Lock()


def _text(value, limit=1024):
    if value is None:
        return ''
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError('A connection field has an invalid type.')
    value = str(value)
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('A connection field is too long or contains control characters.')
    return value


def _port(value, default):
    text = _text(value).strip()
    try:
        port = int(text or default)
    except ValueError:
        raise ValueError('An imported connection has an invalid port.') from None
    if not 1 <= port <= 65535:
        raise ValueError('An imported connection has an invalid port.')
    return port


def validate_entry(result):
    from .live import connection_options
    from .workspace_store import _check_safety
    if not isinstance(result, dict):
        raise ValueError('A connection must be an object.')
    options = connection_options(result)
    name = result.get('name')
    if isinstance(name, str):
        name = name.strip()
    name = name or options['host']
    if not isinstance(name, str) or len(name) > 200:
        raise ValueError('Connection names must be text of at most 200 characters.')
    _check_safety(result.get('type'), result.get('read_only'))
    result['name'] = name
    result['host'] = options['host']
    result['port'] = options['port']
    result['user'] = options.get('user', '')
    result['database'] = options.get('database', '')
    if 'ssh_host' in options:
        result['ssh_host'] = options['ssh_host']
        result['ssh_user'] = options.get('ssh_user', '')
        result['ssh_port'] = options.get('ssh_port', 22)
    return result


def save_imported(store, items):
    validated = []
    for item in items:
        if isinstance(item, dict) and item.get('error'):
            raise ValueError(str(item['error']))
        entry = dict(item) if isinstance(item, dict) else {}
        validate_entry(entry)
        validated.append(entry)
    return [store.save('profile', entry, remember=False) for entry in validated]


def _entry(name, host, port, user, database, ssh=None):
    host = _text(host).strip()
    if not host:
        raise ValueError('A connection has no host field; URL-only connections cannot be imported.')
    user = _text(user)
    result = {'name': _text(name).strip() or '%s@%s' % (user, host), 'host': host,
              'port': _port(port, 3306), 'user': user, 'database': _text(database),
              'type': 'production'}
    if ssh:
        ssh_host = _text(ssh.get('host'))
        ssh_port = ssh.get('port')
        # Workbench stores the SSH port in sshHost, including [IPv6]:port.
        if ssh_host.startswith('[') and ']:' in ssh_host:
            ssh_host, embedded = ssh_host[1:].rsplit(']:', 1)
            ssh_port = ssh_port or embedded
        elif ssh_host.count(':') == 1:
            ssh_host, embedded = ssh_host.rsplit(':', 1)
            ssh_port = ssh_port or embedded
        if not ssh_host:
            raise ValueError('An SSH connection has no SSH host; its tunnel cannot be imported.')
        result.update(ssh_host=ssh_host, ssh_user=_text(ssh.get('user')),
                      ssh_port=_port(ssh_port, 22))
    try:
        validate_entry(result)
    except ValueError as exc:
        result['error'] = str(exc)
    return result


def _bounded(data):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ValueError('Connection files must be at most 2 MB.')
    return data


def parse_workbench(data):
    data = _bounded(data)
    # Require UTF-8 so UTF-16/32 cannot bypass the declaration check. ET has
    # no network resolver, but reject all DTD/entity declarations regardless.
    try:
        text = data.decode('utf-8-sig')
        if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
            raise ValueError('DTD and entity declarations are not allowed in connection files.')
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError):
        raise ValueError('Could not parse the Workbench connections XML.') from None
    items = []
    for obj in root.iter('value'):
        if obj.get('struct-name') != 'db.mgmt.Connection':
            continue
        fields = {child.get('key'): child for child in obj if child.get('key')}
        driver = fields.get('driver')
        driver_id = (driver.text or '').strip().lower() if driver is not None else ''
        if not driver_id.startswith(('com.mysql.rdbms.mysql.driver.', 'com.mysql.rdbms.mariadb.driver.')):
            continue
        if driver_id.rsplit('.', 1)[-1] not in ('native', 'native_sshtun'):
            raise ValueError('Only Workbench TCP/IP and TCP/IP over SSH connections can be imported.')
        params = fields.get('parameterValues')
        values = {child.get('key'): child.text or '' for child in params} if params is not None else {}
        name = fields.get('name')
        ssh = None
        if 'sshtun' in driver_id:
            ssh = {'host': values.get('sshHost') or values.get('sshHostName'), 'user': values.get('sshUserName'), 'port': values.get('sshPort')}
        items.append(_entry(name.text if name is not None else '', values.get('hostName'), values.get('port'),
                            values.get('userName'), values.get('schema'), ssh))
        if len(items) > MAX_CONNECTIONS:
            raise ValueError('Import at most 1,000 connections at a time.')
    return items


def parse_dbeaver(data):
    try:
        document = json.loads(_bounded(data).decode('utf-8-sig'))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('Could not parse the DBeaver data sources JSON (maximum 2 MB).') from None
    if not isinstance(document, dict) or not isinstance(document.get('connections', {}), dict):
        raise ValueError('DBeaver connections must be an object.')
    items = []
    for connection in document.get('connections', {}).values():
        if not isinstance(connection, dict):
            raise ValueError('A DBeaver connection must be an object.')
        provider = _text(connection.get('provider')).strip().lower()
        driver = _text(connection.get('driver')).strip().lower()
        if provider not in ('mysql', 'mariadb') or driver not in ('mysql', 'mysql8', 'mariadb'):
            continue
        config = connection.get('configuration') or {}
        if not isinstance(config, dict):
            raise ValueError('A DBeaver configuration must be an object.')
        if str(config.get('configurationType', 'MANUAL')).upper() == 'URL':
            raise ValueError('URL-only DBeaver connections cannot be imported; use host and port fields.')
        auth = config.get('auth-properties') or {}
        if not isinstance(auth, dict):
            raise ValueError('DBeaver authentication properties must be an object.')
        handlers = config.get('handlers', config.get('network-handlers', connection.get('network-handlers', {}))) or {}
        if not isinstance(handlers, dict):
            raise ValueError('DBeaver network handlers must be an object.')
        tunnel = handlers.get('ssh_tunnel') or {}
        if not isinstance(tunnel, dict):
            raise ValueError('A DBeaver SSH handler must be an object.')
        for key, handler in handlers.items():
            if isinstance(handler, dict) and handler.get('enabled') is True and handler.get('type') in ('TUNNEL', 'PROXY') and key != 'ssh_tunnel':
                raise ValueError('Only direct connections and basic SSH tunnels can be imported.')
        ssh = None
        if tunnel.get('enabled') is True:
            props = tunnel.get('properties') or {}
            if not isinstance(props, dict):
                raise ValueError('DBeaver SSH properties must be an object.')
            if props.get('jumpServer.count') not in (None, 0, '0', '') or props.get('remoteHost') or props.get('remotePort'):
                raise ValueError('SSH jump chains and custom forwarding must be configured manually.')
            ssh = {'host': props.get('host'), 'port': props.get('port'), 'user': tunnel.get('user')}
        items.append(_entry(connection.get('name'), config.get('host'), config.get('port'),
                            config.get('user') or auth.get('userName'), config.get('database'), ssh))
        if len(items) > MAX_CONNECTIONS:
            raise ValueError('Import at most 1,000 connections at a time.')
    return items


def default_path(tool):
    filename = FILENAMES[tool]
    if sys.platform == 'darwin':
        base = Path.home() / 'Library'
        return (base / 'Application Support/MySQL/Workbench' / filename if tool == 'workbench'
                else base / 'DBeaverData/workspace6/General/.dbeaver' / filename)
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA')
        if not appdata or not os.path.isabs(appdata):
            return None
        return (Path(appdata) / 'MySQL/Workbench' / filename if tool == 'workbench'
                else Path(appdata) / 'DBeaverData/workspace6/General/.dbeaver' / filename)
    return None


def choose_file():
    from . import folder_picker
    if not _DIALOG_LOCK.acquire(blocking=False):
        raise ValueError('A connection file chooser is already open.')
    try:
        if sys.platform == 'darwin':
            script = '''activate
try
  return POSIX path of (choose file with prompt "Choose connections.xml or data-sources.json")
on error number -128
  return ""
end try
'''
            return folder_picker._run_dialog(['/usr/bin/osascript'], input_text=script)
        if sys.platform == 'win32':
            script = '''$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Filter = 'Connection files (connections.xml;data-sources.json)|connections.xml;data-sources.json'
try { if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.FileName } } finally { $d.Dispose() }
'''
            system_root = os.environ.get('SystemRoot') or r'C:\Windows'
            executable = os.path.join(system_root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
            return folder_picker._run_dialog([executable, '-NoProfile', '-NonInteractive', '-STA', '-Command', script])
        if sys.platform.startswith('linux'):
            return folder_picker._run_dialog(['zenity', '--file-selection', '--title=Choose a connection file',
                                             '--file-filter=Connection files | connections.xml data-sources.json'], cancel_codes=(1,))
        raise ValueError('No native connection file chooser is available on this platform.')
    except ValueError as exc:
        message = str(exc).replace('directory chooser', 'file chooser')
        message = message.replace(' Type the directory path manually instead.', ' Retry choosing a connection file.')
        raise ValueError(message) from None
    finally:
        _DIALOG_LOCK.release()


def read_file(path, tool):
    raw = str(path)
    if not os.path.isabs(raw) or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError('Choose an absolute connection file path without control characters.')
    path = Path(raw)
    if path.name != FILENAMES[tool] or path.is_symlink():
        raise ValueError('Choose %s, not a password store or a symbolic link.' % FILENAMES[tool])
    resolved = path.resolve()
    try:
        fd = os.open(str(resolved), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Choose a regular connection file.')
            if info.st_size > MAX_BYTES:
                raise ValueError('Connection files must be at most 2 MB.')
            data = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ValueError('Could not read the selected connection file.') from None
    return (parse_workbench if tool == 'workbench' else parse_dbeaver)(data)


def response(payload):
    if set(payload) - {'tool', 'choose'}:
        raise ValueError('Only a tool and the native file chooser flag are accepted.')
    tool = payload.get('tool')
    if not isinstance(tool, str) or tool not in FILENAMES:
        raise ValueError('Choose MySQL Workbench or DBeaver.')
    if not isinstance(payload.get('choose', False), bool):
        raise ValueError('The file chooser flag must be a boolean.')
    if payload.get('choose'):
        path = choose_file()
        if path is None:
            return {'items': [], 'cancelled': True, 'found': False}
    else:
        path = default_path(tool)
        if path is None or not path.exists():
            return {'items': [], 'cancelled': False, 'found': False}
    return {'items': read_file(path, tool), 'cancelled': False, 'found': True}
