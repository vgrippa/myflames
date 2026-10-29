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
import stat
import subprocess
import sys

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


def _run_dialog(argv, input_text=None, cancel_codes=()):
    try:
        proc = subprocess.run(argv, input=input_text, capture_output=True, text=True, encoding='utf-8',
                              errors='strict', timeout=TIMEOUT)
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
