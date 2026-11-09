"""Native file chooser for Import CSV (stdlib, Python 3.7+).

Public API:
  choose_file() -> {'path': <absolute readable regular file> | None,
                    'cancelled': bool}
  check_file(path) -> the absolute, resolved path of a readable regular file
                      of at most MAX_BYTES (the typed-path fallback uses it too)

choose_file() takes no arguments: the helper executable, the script, the
prompt and the file types are fixed constants, never caller input (the same
rules as folder_picker.choose_directory, whose process-wide lock it shares,
so only one native dialog is open at a time). The dialog offers .csv, .tsv
and .txt files; the user can still pick another file where the platform
allows it, and check_file decides.

Failures raise ValueError with a message that points at the manual-entry
fallback: another chooser is open, the platform has no chooser, the helper
cannot start, times out (120 s; subprocess.run kills and waits the child),
exits non-zero, or returns a path with control characters, a relative path,
or something that is not a readable regular file of at most 4 GiB
(directories, devices, sockets and FIFOs are refused).

Nothing is created or written here, and the file is not read beyond a
permission check.
"""
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

from . import folder_picker

TIMEOUT = folder_picker.TIMEOUT
PROMPT = 'Choose a CSV file to import into myflames'
MAX_BYTES = 4 * 1024 * 1024 * 1024
_MANUAL_FALLBACK = ' Type the file path instead.'

_MACOS_OSASCRIPT = '/usr/bin/osascript'
# "of type" accepts uniform type identifiers and file name extensions.
_MACOS_SCRIPT = '''activate
try
    set chosenFile to choose file with prompt "Choose a CSV file to import into myflames" of type {"public.comma-separated-values-text", "public.tab-separated-values-text", "public.plain-text", "csv", "tsv", "txt"}
    return POSIX path of chosenFile
on error errorMessage number errorNumber
    if errorNumber is -128 then
        return ""
    end if
    error errorMessage number errorNumber
end try
'''

_WINDOWS_SCRIPT = """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Choose a CSV file to import into myflames'
$dialog.Filter = 'CSV, TSV and text files (*.csv;*.tsv;*.txt)|*.csv;*.tsv;*.txt|All files (*.*)|*.*'
$dialog.Multiselect = $false
$dialog.CheckFileExists = $true
$selected = $null
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $selected = $dialog.FileName
}
$dialog.Dispose()
if ($selected -ne $null) {
    Write-Output $selected
}
"""
_WINDOWS_FLAGS = ('-NoProfile', '-NonInteractive', '-STA', '-Command')

_ZENITY = 'zenity'
_ZENITY_ARGS = ('--file-selection', '--title=' + PROMPT,
                '--file-filter=CSV, TSV and text files | *.csv *.tsv *.txt',
                '--file-filter=All files | *')


# ---- SQL files (the Query editor's Open SQL file and Save as) ----------------
#
# A second fixed file-type set (.sql and .txt), never a caller-supplied
# pattern: the helper executable, the script and the filters are constants
# here, like the CSV set above. choose_save_file's only caller input is the
# suggested file name, sanitized before it reaches the dialog.

SQL_PROMPT = 'Choose a SQL file to open in myflames'
#: The editor reads at most this much of a chosen file.
SQL_MAX_BYTES = 5 * 1024 * 1024
SQL_EXTENSIONS = ('.sql', '.txt')

_MACOS_SQL_SCRIPT = '''activate
try
    set chosenFile to choose file with prompt "Choose a SQL file to open in myflames" of type {"public.plain-text", "sql", "txt"}
    return POSIX path of chosenFile
on error errorMessage number errorNumber
    if errorNumber is -128 then
        return ""
    end if
    error errorMessage number errorNumber
end try
'''

_WINDOWS_SQL_SCRIPT = """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Choose a SQL file to open in myflames'
$dialog.Filter = 'SQL and text files (*.sql;*.txt)|*.sql;*.txt|All files (*.*)|*.*'
$dialog.Multiselect = $false
$dialog.CheckFileExists = $true
$selected = $null
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $selected = $dialog.FileName
}
$dialog.Dispose()
if ($selected -ne $null) {
    Write-Output $selected
}
"""

_ZENITY_SQL_ARGS = ('--file-selection', '--title=' + SQL_PROMPT,
                    '--file-filter=SQL and text files | *.sql *.txt',
                    '--file-filter=All files | *')

SAVE_PROMPT = 'Save the query as a SQL file'

# macOS: "choose file name" is the save dialog; its default name is a bare
# file name (a path in it would be a scripting error, so it is sanitized).
# The name arrives in MYFLAMES_SAVE_NAME and is read with printenv:
# AppleScript's "system attribute" decodes the environment as MacRoman, which
# garbles any non-ASCII name, while "do shell script" output is UTF-8. The
# shell command is fixed text; the name is never script source.
_MACOS_SAVE_SCRIPT = '''activate
try
    set chosenFile to choose file name with prompt "Save the query as a SQL file" default name (do shell script "/usr/bin/printenv MYFLAMES_SAVE_NAME")
    return POSIX path of chosenFile
on error errorMessage number errorNumber
    if errorNumber is -128 then
        return ""
    end if
    error errorMessage number errorNumber
end try
'''

_WINDOWS_SAVE_SCRIPT = """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.Title = 'Save the query as a SQL file'
$dialog.Filter = 'SQL files (*.sql)|*.sql|Text files (*.txt)|*.txt|All files (*.*)|*.*'
$dialog.FileName = $env:MYFLAMES_SAVE_NAME
$dialog.AddExtension = $true
$dialog.DefaultExt = 'sql'
$dialog.OverwritePrompt = $true
$selected = $null
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $selected = $dialog.FileName
}
$dialog.Dispose()
if ($selected -ne $null) {
    Write-Output $selected
}
"""

_KDIALOG = 'kdialog'


def _clean_suggested_name(name):
    """A bare file name for the save dialog: no path, no control characters."""
    if not isinstance(name, str):
        name = ''
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    # A colon is a drive or an NTFS stream on Windows ("C:x.sql", "a:b").
    name = name.replace(':', '-')
    name = ''.join(c for c in name if ord(c) >= 0x20 and ord(c) != 0x7F).strip()
    if not name:
        name = 'query.sql'
    if not name.lower().endswith(SQL_EXTENSIONS):
        name += '.sql'
    return name[:255]


def choose_sql_file():
    """The native chooser for a .sql or .txt file to open; same answer shape,
    lock and errors as choose_file (the typed-path fallback also applies)."""
    if not folder_picker._DIALOG_LOCK.acquire(blocking=False):
        raise ValueError('A file or folder chooser is already open.' + _MANUAL_FALLBACK)
    try:
        raw = _launch_sql(sys.platform)
    finally:
        folder_picker._DIALOG_LOCK.release()
    if raw is None:
        return {'path': None, 'cancelled': True}
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError('The file chooser returned a path containing control characters.' + _MANUAL_FALLBACK)
    if not os.path.isabs(raw):
        raise ValueError('The file chooser returned a relative path.' + _MANUAL_FALLBACK)
    try:
        return {'path': check_file(raw), 'cancelled': False}
    except ValueError as exc:
        raise ValueError(str(exc).rstrip() + _MANUAL_FALLBACK)


def choose_save_file(default_name='query.sql'):
    """The native save dialog, answering {'path', 'cancelled'} like
    choose_file. The path is validated by the caller (check_save_path)
    before anything is written; there is no typed-path fallback for
    writing, so a platform without a save dialog is an error, not a prompt.
    """
    suggested = _clean_suggested_name(default_name)
    if not folder_picker._DIALOG_LOCK.acquire(blocking=False):
        raise ValueError('A file or folder chooser is already open. Try again after closing it.')
    try:
        raw = _launch_save(sys.platform, suggested)
    finally:
        folder_picker._DIALOG_LOCK.release()
    if raw is None:
        return {'path': None, 'cancelled': True}
    return {'path': raw, 'cancelled': False}


def check_save_path(path):
    """The canonical .sql/.txt destination returned by the native dialog."""
    return check_save_destination(path)[0]


def check_save_destination(path):
    """Bind the name to its resolved parent, without following a target link.

    Adding .sql must never authorize overwriting a different existing file
    from the one the native dialog showed. The writer also enforces this
    with a no-clobber operation when the extension was added here.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError('The save dialog returned no file name.')
    if len(path) > 4096 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise ValueError('The file path contains characters a file name cannot have.')
    if not os.path.isabs(path):
        raise ValueError('The save dialog returned a relative path.')
    _check_save_target(path)
    parent, base = os.path.split(path)
    if not base or base in ('.', '..'):
        raise ValueError('The save dialog returned a folder, not a file name.')
    stem, ext = os.path.splitext(base)
    if not ext:
        base += '.sql'
        path = os.path.join(parent, base)
    elif ext.lower() not in SQL_EXTENSIONS:
        raise ValueError('Save as a .sql or .txt file.')
    elif not stem:
        raise ValueError('The file name is only an extension.')
    parent = os.path.realpath(parent)
    path = os.path.join(parent, base)
    if not os.path.isdir(parent):
        raise ValueError('The folder of %s does not exist.' % path)
    parent_identity = file_parent_identity(path)
    exists = _check_save_target(path)
    if not ext and exists:
        raise ValueError(_EXTENSION_CONFLICT)
    _check_save_parent(path, parent_identity)
    return path, parent_identity, bool(ext)


_EXTENSION_CONFLICT = ('Adding .sql selects a file that already exists. '
                       'Use Save as and choose the full file name again to approve replacing it.')
_PARENT_CHANGED = 'The file\'s folder has changed. Use Save as to choose the file again.'
_OPEN_PARENT_CHANGED = 'The file\'s folder changed while it was being opened. Choose the file again.'
# os.replace has the same dir_fd support as os.rename, but is not itself
# listed in os.supports_dir_fd. A pinned directory keeps even a last-moment
# ancestor swap from redirecting creation, replacement or cleanup.
_SAVE_DIR_FD = (hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW') and
                all(fn in os.supports_dir_fd for fn in (os.open, os.stat, os.rename, os.link, os.unlink)))


def _check_save_target(path, dir_fd=None):
    try:
        info = os.lstat(path) if dir_fd is None else os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise ValueError('The save target is a symbolic link. Choose a regular file instead.')
    if stat.S_ISDIR(info.st_mode):
        raise ValueError('%s is a folder.' % path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('%s is not a regular file (a device, socket or pipe is never overwritten).' % path)
    return True


def _kept_mode(path, dir_fd=None):
    """(permission bits, gid) to keep from an existing target, read in one
    stat without following a link: only a regular file with one link that this
    user owns. None
    means the default permissions apply (0666 less the umask)."""
    try:
        info = os.lstat(path) if dir_fd is None else os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    # A second hard link may be another user's link to some other file.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
        return None
    return stat.S_IMODE(info.st_mode) & 0o777, info.st_gid


def _mode_for(kept, gid):
    """The kept bits; when the new file lands in another group, that group
    gets only what others had."""
    mode, kept_gid = kept
    if kept_gid != gid:
        mode = (mode & ~0o070) | ((mode & 0o007) << 3)
    return mode


def open_for_read(path, parent_identity):
    """Open *path* read-only through its pinned parent folder: neither the
    file nor its folder may be a link swapped in after the chooser, and a
    FIFO never blocks (the caller checks the descriptor is a regular file).
    Without dir_fd support the canonical parent is re-checked instead."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        if not _SAVE_DIR_FD:
            _check_save_parent(path, parent_identity)
            return os.open(path, flags)
        parent, name = os.path.split(path)
        try:
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise ValueError(_OPEN_PARENT_CHANGED)
        try:
            info = os.fstat(parent_fd)
            if (info.st_dev, info.st_ino) != parent_identity:
                raise ValueError(_OPEN_PARENT_CHANGED)
            return os.open(name, flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        # A link swapped in for the file (ELOOP), or it is gone or unreadable.
        raise ValueError('Could not open the file (%s). Choose it again.' % (exc.strerror or exc))


def file_parent_identity(path):
    """Device and inode of an already canonical destination's parent."""
    parent = os.path.dirname(path)
    try:
        info = os.lstat(parent)
    except OSError:
        raise ValueError(_PARENT_CHANGED)
    if not stat.S_ISDIR(info.st_mode) or os.path.realpath(parent) != parent:
        raise ValueError(_PARENT_CHANGED)
    return info.st_dev, info.st_ino


def _check_save_parent(path, identity):
    if file_parent_identity(path) != identity:
        raise ValueError(_PARENT_CHANGED)


def write_text_atomic(path, text, parent_identity=None, overwrite=True):
    """UTF-8, temporary file, fsync, then atomic publication.

    Tokens supply the parent identity captured at selection, never a fresh
    identity from a possibly replaced path. Platforms without dir_fd support
    re-stat the canonical parent at creation and immediately before publish.
    A replaced file this user owns keeps its permission bits; any other
    target gets the default ones (0666 less the umask; owner-only where dir_fd
    is unavailable).
    """
    if parent_identity is None:
        path, parent_identity, approved_name = check_save_destination(path)
        overwrite = overwrite and approved_name
    _check_save_parent(path, parent_identity)
    data = text.encode('utf-8')
    parent, name = os.path.split(path)
    parent_fd = None
    temporary = None
    fd = None
    try:
        if _SAVE_DIR_FD:
            try:
                parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                raise ValueError(_PARENT_CHANGED)
            info = os.fstat(parent_fd)
            if (info.st_dev, info.st_ino) != parent_identity:
                raise ValueError(_PARENT_CHANGED)
            _check_save_target(name, parent_fd)
            kept = _kept_mode(name, parent_fd)
            candidate = '.myflames-' + secrets.token_hex(16) + '.tmp'
            # 0666 lets the umask set a new file's permissions; a replaced
            # file's own bits are copied before any content is written.
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o666 if kept is None else 0o600, dir_fd=parent_fd)
            temporary = candidate
            if kept is not None:
                os.fchmod(fd, _mode_for(kept, os.fstat(fd).st_gid))
        else:
            _check_save_target(path)
            kept = _kept_mode(path)
            _check_save_parent(path, parent_identity)
            fd, temporary = tempfile.mkstemp(prefix='.myflames-', suffix='.tmp', dir=parent)
            _check_save_parent(path, parent_identity)
            if kept is not None:
                mode = _mode_for(kept, os.fstat(fd).st_gid)
                if hasattr(os, 'fchmod'):
                    os.fchmod(fd, mode)
                else:  # Windows before Python 3.13: only the read-only flag.
                    os.chmod(temporary, mode)
        with os.fdopen(fd, 'wb') as handle:
            fd = None  # The file object now owns it, also on write failure.
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        target = name if parent_fd is not None else path
        _check_save_target(target, parent_fd)
        _check_save_parent(path, parent_identity)
        options = {'src_dir_fd': parent_fd, 'dst_dir_fd': parent_fd} if parent_fd is not None else {}
        if overwrite:
            os.replace(temporary, target, **options)
            temporary = None
        else:
            # link publishes the complete file atomically and fails if a file
            # appeared after extension validation. replace would clobber it.
            try:
                os.link(temporary, target, **options)
            except FileExistsError:
                raise ValueError(_EXTENSION_CONFLICT)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                if parent_fd is not None:
                    os.unlink(temporary, dir_fd=parent_fd)
                else:
                    # Never clean up through a directory that was swapped.
                    _check_save_parent(path, parent_identity)
                    os.unlink(temporary)
            except (OSError, ValueError):
                pass
        if parent_fd is not None:
            os.close(parent_fd)


def _launch_sql(platform):
    if platform == 'darwin':
        return _run_dialog([_MACOS_OSASCRIPT], input_text=_MACOS_SQL_SCRIPT)
    if platform == 'win32':
        system_root = os.environ.get('SystemRoot') or os.environ.get('SYSTEMROOT') or r'C:\Windows'
        powershell = os.path.join(system_root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        return _run_dialog([powershell] + list(_WINDOWS_FLAGS) + [_WINDOWS_SQL_SCRIPT])
    if platform.startswith('linux'):
        return _run_dialog([_ZENITY] + list(_ZENITY_SQL_ARGS), cancel_codes=(1,))
    raise ValueError('No native file chooser is available on this platform (%s).%s' % (platform, _MANUAL_FALLBACK))


def _launch_save(platform, suggested):
    # Runtime text is data, never PowerShell or AppleScript source. In
    # particular, PowerShell treats typographic quotes as string delimiters.
    env = dict(os.environ, MYFLAMES_SAVE_NAME=suggested)
    if platform == 'darwin':
        return _run_dialog([_MACOS_OSASCRIPT], input_text=_MACOS_SAVE_SCRIPT, env=env)
    if platform == 'win32':
        system_root = os.environ.get('SystemRoot') or os.environ.get('SYSTEMROOT') or r'C:\Windows'
        powershell = os.path.join(system_root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        return _run_dialog([powershell] + list(_WINDOWS_FLAGS) + [_WINDOWS_SAVE_SCRIPT], env=env)
    if platform.startswith('linux'):
        # zenity cancels with exit code 1 and empty stdout; when neither
        # zenity nor kdialog exists there is no save dialog (writing has no
        # typed-path fallback).
        zenity = shutil.which(_ZENITY)
        if zenity:
            return _run_dialog([zenity, '--file-selection', '--save', '--confirm-overwrite',
                                '--title=' + SAVE_PROMPT, '--filename=' + suggested], cancel_codes=(1,))
        kdialog = shutil.which(_KDIALOG)
        if kdialog:
            return _run_dialog([kdialog, '--getsavefilename', os.path.join(os.path.expanduser('~'), suggested),
                                '*.sql *.txt |SQL and text files'], cancel_codes=(1,))
        raise ValueError('No native save dialog is available (zenity or kdialog is needed).')
    raise ValueError('No native save dialog is available on this platform (%s).' % platform)


def choose_file():
    """Show the native file chooser and return {'path', 'cancelled'}.

    Raises ValueError when the chooser cannot produce a usable file; the
    message always points at the typed-path fallback.
    """
    if not folder_picker._DIALOG_LOCK.acquire(blocking=False):
        raise ValueError('A file or folder chooser is already open.' + _MANUAL_FALLBACK)
    try:
        raw = _launch(sys.platform)
    finally:
        folder_picker._DIALOG_LOCK.release()
    if raw is None:
        return {'path': None, 'cancelled': True}
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError('The file chooser returned a path containing control characters.' + _MANUAL_FALLBACK)
    if not os.path.isabs(raw):
        raise ValueError('The file chooser returned a relative path.' + _MANUAL_FALLBACK)
    try:
        return {'path': check_file(raw), 'cancelled': False}
    except ValueError as exc:
        raise ValueError(str(exc).rstrip() + _MANUAL_FALLBACK)


def check_file(path):
    """The resolved absolute path of a readable regular file of at most 4 GiB.

    Used for the chooser's answer and for a typed path. Refuses a relative
    path, a missing file, a directory, a device, a socket or a FIFO, an
    unreadable file and a larger one (ValueError with the reason).
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError('Enter the full path of a CSV file.')
    if len(path) > 4096 or '\x00' in path or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise ValueError('The file path contains characters a file name cannot have.')
    # A pasted path often carries spaces around it; a file name may also end
    # in a space, so trim only when the path as typed does not exist.
    if path != path.strip() and not os.path.lexists(path) and os.path.lexists(path.strip()):
        path = path.strip()
    if not os.path.isabs(path):
        raise ValueError('Enter the full path of the file, starting at the root of the disk.')
    resolved = os.path.realpath(path)
    try:
        info = os.stat(resolved)
    except FileNotFoundError:
        raise ValueError('No file exists at %s.' % path)
    except OSError as exc:
        raise ValueError('The file cannot be opened (%s).' % (exc.strerror or exc))
    if stat.S_ISDIR(info.st_mode):
        raise ValueError('%s is a folder. Choose a CSV file.' % path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('%s is not a regular file (a device, socket or pipe cannot be imported).' % path)
    if info.st_size > MAX_BYTES:
        raise ValueError('The file is larger than 4 GiB. Split it into smaller files.')
    if not os.access(resolved, os.R_OK):
        raise ValueError('myflames cannot read %s. Check its permissions.' % path)
    return resolved


def _launch(platform):
    if platform == 'darwin':
        return _run_dialog([_MACOS_OSASCRIPT], input_text=_MACOS_SCRIPT)
    if platform == 'win32':
        system_root = os.environ.get('SystemRoot') or os.environ.get('SYSTEMROOT') or r'C:\Windows'
        powershell = os.path.join(system_root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        return _run_dialog([powershell] + list(_WINDOWS_FLAGS) + [_WINDOWS_SCRIPT])
    if platform.startswith('linux'):
        # zenity cancels with exit code 1 and empty stdout.
        return _run_dialog([_ZENITY] + list(_ZENITY_ARGS), cancel_codes=(1,))
    raise ValueError('No native file chooser is available on this platform (%s).%s' % (platform, _MANUAL_FALLBACK))


def _run_dialog(argv, input_text=None, cancel_codes=(), env=None):
    try:
        proc = subprocess.run(argv, input=input_text, capture_output=True, text=True, encoding='utf-8',
                              errors='strict', timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise ValueError('The file chooser timed out after %d s.%s' % (TIMEOUT, _MANUAL_FALLBACK))
    except OSError as exc:
        raise ValueError('Could not start the file chooser (%s: %s).%s' % (argv[0], exc, _MANUAL_FALLBACK))
    except UnicodeError:
        raise ValueError('The file chooser returned an unreadable path.' + _MANUAL_FALLBACK)
    if proc.returncode in cancel_codes and not (proc.stdout or '').strip():
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or '').strip()
        message = 'The file chooser exited with status %s' % proc.returncode
        if detail:
            message += ': %s' % detail[:200]
        raise ValueError(message + '.' + _MANUAL_FALLBACK)
    output = proc.stdout or ''
    if output.endswith('\r\n'):
        output = output[:-2]
    elif output.endswith('\n'):
        output = output[:-1]
    return output or None
