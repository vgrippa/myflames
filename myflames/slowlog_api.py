"""Workspace endpoints for slow query log files (Query digest's Slow log file source).

``/api/slow-log/choose`` shows the native file chooser (the same helper and
process-wide dialog lock as Import CSV's, file_picker.py, with a prompt and
file types of its own), ``/api/slow-log/open-path`` takes a typed path; both
check the file (slowlog.check_path) and keep it in this workspace instance's
memory under a random ``file_id`` (never saved, never a path from the page
after that). ``/api/slow-log/read`` reads the whole file into a
``slow-log-1.0`` report with the range, filters and ranking asked for,
``/api/slow-log/progress`` says how far a read with a ``progress_id`` is
(``slow-log-progress-1.0``) and ``/api/slow-log/stop`` stops it. Nothing
connects to a server; files are only read.
"""
import collections
import re
import sys
import threading
import time

from . import slowlog

PATHS = ("/api/slow-log/choose", "/api/slow-log/open-path", "/api/slow-log/read", "/api/slow-log/progress",
         "/api/slow-log/stop")
PROGRESS_SCHEMA = "slow-log-progress-1.0"
FILE_LIMIT = 16
FILE_TTL = 6 * 3600
PROGRESS_LIMIT = 32
PROGRESS_TTL = 3600
#: Reads running at once in one workspace.
RUNNING_LIMIT = 2
_PROGRESS_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_CREATE_LOCK = threading.Lock()

PROMPT = "Choose a slow query log to open in myflames"
_MACOS_SCRIPT = '''activate
try
    set chosenFile to choose file with prompt "Choose a slow query log to open in myflames"
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
$dialog.Title = 'Choose a slow query log to open in myflames'
$dialog.Filter = 'Log files (*.log;*.txt)|*.log;*.txt|All files (*.*)|*.*'
$dialog.FilterIndex = 2
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
_ZENITY_ARGS = ("--file-selection", "--title=" + PROMPT)
_MANUAL = " Type the file path instead."


def choose_file(platform=None):
    """Show the native chooser: ``{"path", "cancelled"}``. ValueError when it
    cannot produce a usable file (the message points at the typed path)."""
    import os
    from . import file_picker, folder_picker
    if not folder_picker._DIALOG_LOCK.acquire(blocking=False):
        raise ValueError("A file or folder chooser is already open." + _MANUAL)
    try:
        platform = platform or sys.platform
        if platform == "darwin":
            raw = file_picker._run_dialog([file_picker._MACOS_OSASCRIPT], input_text=_MACOS_SCRIPT)
        elif platform == "win32":
            root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
            powershell = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            raw = file_picker._run_dialog([powershell] + list(file_picker._WINDOWS_FLAGS) + [_WINDOWS_SCRIPT])
        elif platform.startswith("linux"):
            raw = file_picker._run_dialog([file_picker._ZENITY] + list(_ZENITY_ARGS), cancel_codes=(1,))
        else:
            raise ValueError("No native file chooser is available on this platform (%s).%s" % (platform, _MANUAL))
    finally:
        folder_picker._DIALOG_LOCK.release()
    if raw is None:
        return {"path": None, "cancelled": True}
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw) or not os.path.isabs(raw):
        raise ValueError("The file chooser returned an unusable path." + _MANUAL)
    try:
        return {"path": slowlog.check_path(raw), "cancelled": False}
    except ValueError as exc:
        raise ValueError(str(exc).rstrip() + _MANUAL)


class Progress(object):
    """How far one read is (bytes only: no statement text, no path)."""

    def __init__(self, total):
        self.lock = threading.Lock()
        self.total = total
        self.read = 0
        self.phase = "reading"
        self.started = time.monotonic()
        self.ended = None
        self.stop_requested = False

    def update(self, read):
        with self.lock:
            self.read = read

    def finish(self, phase):
        with self.lock:
            self.phase = phase
            self.ended = time.monotonic()

    def snapshot(self):
        with self.lock:
            end = self.ended if self.ended is not None else time.monotonic()
            percent = None
            if self.total:
                percent = min(100.0, round(self.read * 100.0 / self.total, 1))
            return {"schema_version": PROGRESS_SCHEMA, "phase": self.phase, "bytes_read": self.read,
                    "bytes_total": self.total, "percent": percent, "elapsed_s": round(end - self.started, 1),
                    "stop_requested": self.stop_requested}


class SlowLogFiles(object):
    """Files chosen and reads running in this workspace instance (memory only)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.files = collections.OrderedDict()
        self.progress = collections.OrderedDict()
        self.running = 0

    def add_file(self, path):
        import secrets
        source = slowlog.file_source(path)
        entry = {"file_id": secrets.token_urlsafe(18), "path": path, "name": source["name"],
                 "size": source["size_bytes"], "modified": source["modified"], "added": time.monotonic()}
        with self.lock:
            self._prune()
            while len(self.files) >= FILE_LIMIT:
                self.files.popitem(last=False)
            self.files[entry["file_id"]] = entry
        return {"file_id": entry["file_id"], "name": entry["name"], "size": entry["size"],
                "modified": entry["modified"]}

    def file(self, file_id):
        with self.lock:
            self._prune()
            entry = self.files.get(file_id) if isinstance(file_id, str) else None
        if entry is None:
            raise ValueError("This slow log is no longer open in myflames. Choose the file again.")
        return entry

    def start(self, progress_id, total):
        with self.lock:
            if self.running >= RUNNING_LIMIT:
                raise ValueError("Two slow logs are being read already. Wait for one to finish, or stop it.")
            self.running += 1
            progress = Progress(total)
            if progress_id:
                self.progress.pop(progress_id, None)
                while len(self.progress) >= PROGRESS_LIMIT:
                    self.progress.popitem(last=False)
                self.progress[progress_id] = progress
            return progress

    def done(self):
        with self.lock:
            self.running = max(0, self.running - 1)

    def get_progress(self, progress_id):
        with self.lock:
            return self.progress.get(progress_id) if isinstance(progress_id, str) else None

    def _prune(self):
        now = time.monotonic()
        for key in [k for k, v in self.files.items() if now - v["added"] > FILE_TTL]:
            del self.files[key]
        for key in [k for k, v in self.progress.items() if v.ended is not None and now - v.ended > PROGRESS_TTL]:
            del self.progress[key]


def registry(workspace):
    with _CREATE_LOCK:
        files = getattr(workspace, "slow_log_files", None)
        if files is None:
            files = workspace.slow_log_files = SlowLogFiles()
        return files


def _progress_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _PROGRESS_ID.match(value):
        raise ValueError("progress_id must be 8 to 64 letters, digits, - or _.")
    return value


def _limit(value):
    if value is None:
        return slowlog.DEFAULT_LIMIT
    return value


def response(path, payload, workspace):
    files = registry(workspace)
    if path == "/api/slow-log/choose":
        chosen = choose_file()
        if chosen["cancelled"]:
            return {"cancelled": True}
        return files.add_file(chosen["path"])
    if path == "/api/slow-log/open-path":
        return files.add_file(slowlog.check_path(payload.get("path")))
    if path == "/api/slow-log/read":
        order = payload.get("order") or "total"
        limit = _limit(payload.get("limit"))
        user = payload.get("user")
        database = payload.get("database")
        slowlog.check_options(order, limit, user, database)
        slowlog.check_range(payload.get("from"), payload.get("to"))
        progress_id = _progress_id(payload.get("progress_id"))
        entry = files.file(payload.get("file_id"))
        progress = files.start(progress_id, entry["size"])
        phase = "failed"
        try:
            report = slowlog.read_file(entry["path"], payload.get("from"), payload.get("to"), user, database,
                                       order, limit, progress=progress.update,
                                       stop=lambda: progress.stop_requested, label=entry["name"])
            report["source"]["file_id"] = entry["file_id"]
            report["source"]["name"] = entry["name"]
            phase = "done"
            return report
        except slowlog.Stopped:
            phase = "stopped"
            raise ValueError("Stopped reading the slow log.")
        finally:
            progress.finish(phase)
            files.done()
    if path == "/api/slow-log/progress":
        progress = files.get_progress(payload.get("progress_id"))
        if progress is None:
            return {"schema_version": PROGRESS_SCHEMA, "phase": "unknown", "bytes_read": 0, "bytes_total": None,
                    "percent": None, "elapsed_s": None, "stop_requested": False}
        return progress.snapshot()
    if path == "/api/slow-log/stop":
        progress = files.get_progress(payload.get("progress_id"))
        if progress is None or progress.ended is not None:
            return {"stop_requested": False, "message": "No slow log read with this id is running."}
        with progress.lock:
            progress.stop_requested = True
        return {"stop_requested": True, "message": "Stopping the read."}
    raise ValueError("Unknown workspace operation.")
