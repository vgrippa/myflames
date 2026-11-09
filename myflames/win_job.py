"""Windows: end child processes with the process that started them.

Standard library only (ctypes and the documented kernel32 API), Python 3.7+.

On POSIX an SSH tunnel's ssh is tied to the process that opened it by
ssh_watchdog.py. On Windows a child process simply outlives its parent, so
ssh_tunnel.py starts each ssh.exe suspended (CREATE_SUSPENDED), assigns it to
this process's job object, and only then lets it run. The job is created
once per process with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, its handle is not
inheritable (no child holds it open) and this process never closes it: when
the process ends in any way (a normal exit, a crash, ``taskkill /F``),
Windows closes the handle and ends every process in the job, with the
processes they started (a ProxyCommand). A process that starts suspended
runs nothing before it is in the job.

Nothing here runs at import; the kernel32 functions load on first use.
"""
import ctypes
import threading

#: Process creation flag: the first thread waits until resumed.
CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_HANDLE_FLAG_INHERIT = 0x00000001
_RESUME_FAILED = 0xFFFFFFFF

# Plain ctypes types (not ctypes.wintypes, which older Pythons cannot import
# outside Windows) so the layouts can be checked on any system:
# DWORD c_uint32, LONG c_int32, LARGE_INTEGER c_int64, ULONGLONG c_uint64,
# SIZE_T and ULONG_PTR c_size_t.


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [('ReadOperationCount', ctypes.c_uint64), ('WriteOperationCount', ctypes.c_uint64),
                ('OtherOperationCount', ctypes.c_uint64), ('ReadTransferCount', ctypes.c_uint64),
                ('WriteTransferCount', ctypes.c_uint64), ('OtherTransferCount', ctypes.c_uint64)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', ctypes.c_uint32), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', ctypes.c_uint32),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', ctypes.c_uint32),
                ('SchedulingClass', ctypes.c_uint32)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_uint32), ('cntUsage', ctypes.c_uint32), ('th32ThreadID', ctypes.c_uint32),
                ('th32OwnerProcessID', ctypes.c_uint32), ('tpBasePri', ctypes.c_int32),
                ('tpDeltaPri', ctypes.c_int32), ('dwFlags', ctypes.c_uint32)]


class Kernel32(object):
    """The few kernel32 calls the job needs. Each raises OSError (with the
    Windows error number and text) when the call fails."""

    def __init__(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        handle, dword, boolean, pointer = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p

        def function(name, restype, *argtypes):
            item = getattr(kernel, name)
            item.restype, item.argtypes = restype, list(argtypes)
            return item
        self._create = function('CreateJobObjectW', handle, pointer, ctypes.c_wchar_p)
        self._set = function('SetInformationJobObject', boolean, handle, ctypes.c_int, pointer, dword)
        self._flags = function('SetHandleInformation', boolean, handle, dword, dword)
        self._assign = function('AssignProcessToJobObject', boolean, handle, handle)
        self._snapshot = function('CreateToolhelp32Snapshot', handle, dword, dword)
        self._first = function('Thread32First', boolean, handle, ctypes.POINTER(THREADENTRY32))
        self._next = function('Thread32Next', boolean, handle, ctypes.POINTER(THREADENTRY32))
        self._open_thread = function('OpenThread', handle, dword, boolean, dword)
        self._resume = function('ResumeThread', dword, handle)
        self._close = function('CloseHandle', boolean, handle)

    @staticmethod
    def _error():
        return ctypes.WinError(ctypes.get_last_error())

    def create_job(self):
        """A new job; no security attributes, so its handle is not inheritable."""
        job = self._create(None, None)
        if not job:
            raise self._error()
        return job

    def kill_on_close(self, job):
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._set(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            raise self._error()

    def not_inheritable(self, handle):
        if not self._flags(handle, _HANDLE_FLAG_INHERIT, 0):
            raise self._error()

    def assign(self, job, process):
        if not self._assign(job, process):
            raise self._error()

    def resume(self, pid):
        """Resume every thread of process *pid* (one, for a process that was
        started suspended). Raises OSError when none could be resumed."""
        snapshot = self._snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            raise self._error()
        resumed = 0
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            found = self._first(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == pid:
                    thread = self._open_thread(_THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID)
                    if not thread:
                        raise self._error()
                    try:
                        if self._resume(thread) == _RESUME_FAILED:
                            raise self._error()
                        resumed += 1
                    finally:
                        self._close(thread)
                entry.dwSize = ctypes.sizeof(entry)
                found = self._next(snapshot, ctypes.byref(entry))
        finally:
            self._close(snapshot)
        if not resumed:
            raise OSError('no thread of process %d was found to resume' % pid)

    def close(self, handle):
        self._close(handle)


class KillOnCloseJob(object):
    """This process's job object (see the module docstring). *api* replaces
    the kernel32 calls (tests)."""

    def __init__(self, api=None):
        self._api = api
        self._handle = None
        self._lock = threading.Lock()

    def _kernel(self):
        with self._lock:
            if self._api is None:
                self._api = Kernel32()
            return self._api

    def handle(self):
        """The job's handle, created on first use. A failed creation is not
        kept: the next call tries again."""
        api = self._kernel()
        with self._lock:
            if self._handle is None:
                job = api.create_job()
                try:
                    api.kill_on_close(job)
                    api.not_inheritable(job)
                except BaseException:
                    api.close(job)  # Empty: closing it ends nothing.
                    raise
                self._handle = job
            return self._handle

    def adopt(self, process):
        """Assign *process* (a subprocess.Popen started with CREATE_SUSPENDED)
        to the job, then resume it. Raises OSError; the caller then stops the
        process, which never ran outside the job."""
        handle = getattr(process, '_handle', None)
        if handle is None:
            raise OSError('the process handle is not available')
        job = self.handle()
        api = self._kernel()
        api.assign(job, int(handle))
        api.resume(process.pid)


#: The job of this process, shared by every SSH tunnel it opens.
JOB = KillOnCloseJob()
