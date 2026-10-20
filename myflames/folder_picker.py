"""Native directory chooser (stdlib, Python 3.7+).

Public API: choose_directory() -> {'path': <absolute existing directory> | None,
'cancelled': bool}. The function takes no arguments: the helper executable, the
script and the starting location are all fixed constants, never caller input.

Wire contract:
  result.path      absolute path of an existing directory, or None when the
                   user cancelled the dialog.
  result.cancelled True exactly when the user cancelled (path is then None).

Failures raise ValueError with a message that tells the caller to offer a
manual-entry fallback instead. ValueError is raised when another chooser is
already open (a process-global nonblocking lock), the platform is unsupported,
the helper cannot be launched, the helper times out (120 s; subprocess.run
kills and waits the child before raising TimeoutExpired), the helper exits
non-zero, or the returned path contains ASCII control characters, is relative
or does not name an existing directory. Spaces, unicode and quotes in a valid
path are preserved; only the trailing newline terminator is stripped.

No directory is created, nothing is written, and no backup is started here.
This module only asks the user to pick an existing directory.
"""
import os
import subprocess
import sys
import threading

TIMEOUT = 120
PROMPT = 'Choose a directory for myflames'
_MANUAL_FALLBACK = ' Type the directory path manually instead.'

_MACOS_OSASCRIPT = '/usr/bin/osascript'
_MACOS_SCRIPT = '''activate
try
    set chosenFolder to choose folder with prompt "Choose a directory for myflames"
    return POSIX path of chosenFolder
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
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose a directory for myflames'
$selected = $null
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $selected = $dialog.SelectedPath
}
$dialog.Dispose()
if ($selected -ne $null) {
    Write-Output $selected
}
"""
_WINDOWS_FLAGS = ('-NoProfile', '-NonInteractive', '-STA', '-Command')

_ZENITY = 'zenity'
_ZENITY_ARGS = ('--file-selection', '--directory', '--title=' + PROMPT)

# One dialog per process; a second caller while a dialog is up gets ValueError.
_DIALOG_LOCK = threading.Lock()


def choose_directory():
    """Show the native directory chooser and return {'path', 'cancelled'}.

    Raises ValueError when the chooser cannot produce a usable path; the
    message always points at the manual-entry fallback.
    """
    if not _DIALOG_LOCK.acquire(blocking=False):
        raise ValueError('A directory chooser is already open.' + _MANUAL_FALLBACK)
    try:
        raw = _launch(sys.platform)
    finally:
        _DIALOG_LOCK.release()
    if raw is None:
        return {'path': None, 'cancelled': True}
    return {'path': _validate(raw), 'cancelled': False}


def _launch(platform):
    if platform == 'darwin':
        return _run_macos()
    if platform == 'win32':
        return _run_windows()
    if platform.startswith('linux'):
        return _run_linux()
    raise ValueError('unsupported platform (%s): no native directory chooser '
                     'is available.%s' % (platform, _MANUAL_FALLBACK))


def _run_macos():
    return _run_dialog([_MACOS_OSASCRIPT], input_text=_MACOS_SCRIPT)


def _run_windows():
    system_root = os.environ.get('SystemRoot') or os.environ.get('SYSTEMROOT') or r'C:\Windows'
    powershell = os.path.join(system_root, 'System32', 'WindowsPowerShell',
                              'v1.0', 'powershell.exe')
    return _run_dialog([powershell] + list(_WINDOWS_FLAGS) + [_WINDOWS_SCRIPT])


def _run_linux():
    # zenity cancels with exit code 1 and empty stdout.
    return _run_dialog([_ZENITY] + list(_ZENITY_ARGS), cancel_codes=(1,))


def _run_dialog(argv, input_text=None, cancel_codes=()):
    try:
        proc = subprocess.run(argv, input=input_text, capture_output=True,
                              text=True, encoding='utf-8', errors='strict',
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError('The directory chooser timed out after %d s.%s'
                         % (TIMEOUT, _MANUAL_FALLBACK))
    except OSError as exc:
        raise ValueError('Could not start the directory chooser (%s: %s).%s'
                         % (argv[0], exc, _MANUAL_FALLBACK))
    except UnicodeError:
        raise ValueError('The directory chooser returned an unreadable path.' + _MANUAL_FALLBACK)
    if proc.returncode in cancel_codes and not (proc.stdout or '').strip():
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or '').strip()
        message = 'The directory chooser exited with status %s' % proc.returncode
        if detail:
            message += ': %s' % detail[:200]
        raise ValueError(message + '.' + _MANUAL_FALLBACK)
    output = proc.stdout or ''
    if output.endswith('\r\n'):
        output = output[:-2]
    elif output.endswith('\n'):
        output = output[:-1]
    if not output:
        return None
    return output


def _validate(raw):
    for char in raw:
        code = ord(char)
        if code < 0x20 or code == 0x7F:
            raise ValueError('The directory chooser returned a path containing '
                             'control characters.%s' % _MANUAL_FALLBACK)
    if not os.path.isabs(raw):
        raise ValueError('The directory chooser returned a relative path: %s.%s'
                         % (raw, _MANUAL_FALLBACK))
    if not os.path.isdir(raw):
        raise ValueError('The directory chooser returned a directory that does '
                         'not exist: %s.%s' % (raw, _MANUAL_FALLBACK))
    return raw
