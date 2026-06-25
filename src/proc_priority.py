"""Lower the launcher's OWN browser processes to below-normal CPU priority
during heavy work (the bulk pass/fail scroll-scan) so it doesn't starve the
rest of the machine.

Windows-only and dependency-free on purpose — psutil isn't bundled (keep the
build lean per the project's distribution goals), so we walk the process table
with the Win32 Toolhelp API via ctypes. Everything here is best-effort: any
failure path is a no-op, so a priority tweak can never break the scrape.

Scope matters: we lower ONLY the Edge/Chromium processes that are descendants
of THIS process (the launcher's controlled browser), never the user's other
browser windows.
"""
import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_SET_INFORMATION = 0x0200
NORMAL_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
# The launcher drives Edge (channel="msedge") with a bundled-Chromium fallback.
_BROWSER_EXES = {"msedge.exe", "chrome.exe"}


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _kernel32():
    """kernel32 with the handle-returning signatures pinned so 64-bit handles
    aren't truncated to int. Returns None on non-Windows."""
    try:
        k = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return k


def _snapshot(k):
    """List of (pid, ppid, exe_lower) for every process. [] on failure."""
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return []
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if not k.Process32First(snap, ctypes.byref(entry)):
            return []
        out = []
        while True:
            try:
                exe = entry.szExeFile.decode("ascii", "ignore").lower()
            except Exception:
                exe = ""
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, exe))
            if not k.Process32Next(snap, ctypes.byref(entry)):
                break
        return out
    finally:
        k.CloseHandle(snap)


def find_browser_pids(root_pid=None):
    """PIDs of Edge/Chromium processes that are DESCENDANTS of `root_pid`
    (default: this process) — i.e. only the launcher's own controlled browser,
    not the user's other browser windows. [] on any failure."""
    k = _kernel32()
    if k is None:
        return []
    if root_pid is None:
        root_pid = os.getpid()
    procs = _snapshot(k)
    if not procs:
        return []
    children: dict = {}
    exe_by_pid: dict = {}
    for pid, ppid, exe in procs:
        children.setdefault(ppid, []).append(pid)
        exe_by_pid[pid] = exe
    descendants = []
    seen = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        stack.extend(children.get(pid, []))
    return [pid for pid in descendants
            if exe_by_pid.get(pid, "") in _BROWSER_EXES]


def _set_priority(pids, prio) -> int:
    k = _kernel32()
    if k is None:
        return 0
    ok = 0
    for pid in pids:
        h = k.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if not h:
            continue
        try:
            if k.SetPriorityClass(h, prio):
                ok += 1
        finally:
            k.CloseHandle(h)
    return ok


@contextmanager
def browser_low_priority(root_pid=None):
    """Lower the launcher's browser processes to BELOW_NORMAL for the duration
    of the block, restoring NORMAL on exit. Never raises and is a no-op if the
    process walk or the priority call fails (e.g. non-Windows, access denied)."""
    pids = []
    try:
        pids = find_browser_pids(root_pid)
        if pids:
            _set_priority(pids, BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pids = []
    try:
        yield pids   # caller can log how many processes were de-prioritized
    finally:
        try:
            if pids:
                _set_priority(pids, NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
