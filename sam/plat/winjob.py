"""winjob.py - stdlib-only (ctypes) Windows Job Object + LockFileEx helpers. Experiment code,
written so it can later be lifted into SAM's Windows platform adapter."""
import ctypes
import msvcrt
from ctypes import wintypes as wt

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
HANDLE = ctypes.c_void_p
k32.CreateJobObjectW.restype = HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
k32.OpenJobObjectW.restype = HANDLE
k32.OpenJobObjectW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
k32.TerminateJobObject.argtypes = [HANDLE, wt.UINT]
k32.GetCurrentProcess.restype = HANDLE
k32.IsProcessInJob.argtypes = [HANDLE, HANDLE, ctypes.POINTER(wt.BOOL)]
k32.QueryInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
k32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
k32.CloseHandle.argtypes = [HANDLE]
k32.OpenProcess.restype = HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

JOB_ALL = 0x1F001F
KILL_ON_JOB_CLOSE = 0x2000
BREAKAWAY_OK = 0x800
SILENT_BREAKAWAY_OK = 0x1000


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class EXT_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class BASIC_ACCT(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]


class ACCT_IO(ctypes.Structure):
    _fields_ = [("BasicInfo", BASIC_ACCT), ("IoInfo", IO_COUNTERS)]


def _err(what):
    return OSError("%s failed: WinError %d %s" % (what, ctypes.get_last_error(),
                                                  ctypes.FormatError(ctypes.get_last_error())))


def create(name=None, kill_on_close=False, breakaway_ok=False):
    h = k32.CreateJobObjectW(None, name)
    if not h:
        raise _err("CreateJobObjectW")
    flags = (KILL_ON_JOB_CLOSE if kill_on_close else 0) | (BREAKAWAY_OK if breakaway_ok else 0)
    if flags:
        info = EXT_LIMIT()
        info.BasicLimitInformation.LimitFlags = flags
        if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise _err("SetInformationJobObject")
    return h


def open_job(name):
    h = k32.OpenJobObjectW(JOB_ALL, False, name)
    if not h:
        raise _err("OpenJobObjectW(%s)" % name)
    return h


def assign_self(h):
    if not k32.AssignProcessToJobObject(h, k32.GetCurrentProcess()):
        raise _err("AssignProcessToJobObject(self)")


def assign_pid(h, pid):
    hp = k32.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if not hp:
        raise _err("OpenProcess(%d)" % pid)
    try:
        if not k32.AssignProcessToJobObject(h, hp):
            raise _err("AssignProcessToJobObject(%d)" % pid)
    finally:
        k32.CloseHandle(hp)


def terminate(h, code=137):
    if not k32.TerminateJobObject(h, code):
        raise _err("TerminateJobObject")


def accounting(h):
    a = ACCT_IO()
    if not k32.QueryInformationJobObject(h, 8, ctypes.byref(a), ctypes.sizeof(a), None):
        raise _err("QueryInformationJobObject(acct)")
    b = a.BasicInfo
    return {"TotalProcesses": b.TotalProcesses, "ActiveProcesses": b.ActiveProcesses,
            "cpu_100ns": b.TotalUserTime + b.TotalKernelTime,
            "io_read": a.IoInfo.ReadTransferCount, "io_write": a.IoInfo.WriteTransferCount}


def pids(h, cap=512):
    class PIDLIST(ctypes.Structure):
        _fields_ = [("NumberOfAssignedProcesses", wt.DWORD), ("NumberOfProcessIdsInList", wt.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * cap)]
    p = PIDLIST()
    if not k32.QueryInformationJobObject(h, 3, ctypes.byref(p), ctypes.sizeof(p), None):
        raise _err("QueryInformationJobObject(pids)")
    return [int(p.ProcessIdList[i]) for i in range(p.NumberOfProcessIdsInList)]


def self_in_job():
    b = wt.BOOL()
    if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(b)):
        raise _err("IsProcessInJob")
    return bool(b.value)


def self_job_limit_flags():
    info = EXT_LIMIT()
    if not k32.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        raise _err("QueryInformationJobObject(self limits)")
    f = info.BasicLimitInformation.LimitFlags
    return {"LimitFlags": hex(f), "KILL_ON_JOB_CLOSE": bool(f & KILL_ON_JOB_CLOSE),
            "BREAKAWAY_OK": bool(f & BREAKAWAY_OK), "SILENT_BREAKAWAY_OK": bool(f & SILENT_BREAKAWAY_OK)}


def close(h):
    k32.CloseHandle(h)


# ---- LockFileEx (supports shared + exclusive, like flock) ----
class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", HANDLE)]


k32.LockFileEx.argtypes = [HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(OVERLAPPED)]
k32.UnlockFileEx.argtypes = [HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(OVERLAPPED)]


def lockfileex(fd, exclusive=True, blocking=False):
    """Returns True if acquired, False if would block."""
    flags = (0x2 if exclusive else 0) | (0 if blocking else 0x1)
    ov = OVERLAPPED()
    ok = k32.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(ov))
    if ok:
        return True
    if ctypes.get_last_error() == 33:  # ERROR_LOCK_VIOLATION
        return False
    raise _err("LockFileEx")


def unlockfileex(fd):
    ov = OVERLAPPED()
    k32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(ov))
