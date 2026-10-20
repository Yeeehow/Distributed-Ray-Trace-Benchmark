"""Shared process measurements and cooperative numerical checkpoints."""
import os
import sys
from drt_scheduler import CancellationRequested

def process_memory():
    """Platform process RSS measurement; never part of the ray kernel."""
    if sys.platform == 'linux':
        from pathlib import Path
        import resource
        status = dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        return dict(status='MEASURED', pid=os.getpid(), working_set_bytes=int(status['VmRSS'].split()[0])*1024,
            peak_working_set_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            pagefile_usage_bytes=int(status.get('VmSwap','0 kB').split()[0])*1024,
            definition='Linux per-process RSS and ru_maxrss; RSS can include shared pages')
    if os.name != 'nt':
        return {'status': 'UNMEASURED', 'reason': 'Windows metrics adapter only'}
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('peak', 'working', 'paged_peak', 'paged',
                                                'nonpaged_peak', 'nonpaged', 'pagefile', 'pagefile_peak')]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    data = Counters()
    data.cb = ctypes.sizeof(data)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(data), data.cb):
        return {'status': 'UNMEASURED', 'error': ctypes.get_last_error()}
    return dict(status='MEASURED', pid=os.getpid(), working_set_bytes=data.working,
                peak_working_set_bytes=data.peak, pagefile_usage_bytes=data.pagefile,
                definition='Per-process Windows counters; working sets can include shared OS pages')


class ProcessCheckpoint:
    """Only the spawn-safe process Event crosses the boundary, not a thread token."""
    def __init__(self, event):
        self.event = event

    def __call__(self):
        if self.event.is_set():
            raise CancellationRequested('Process cancellation observed at a numerical checkpoint.')


