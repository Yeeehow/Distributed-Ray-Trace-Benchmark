"""Conservative Windows admission policy, distinct from CPU/core attribution."""
from dataclasses import asdict, dataclass
import ctypes
from ctypes import wintypes
import os
import sys
from pathlib import Path

MAX_WORKERS = 64
MIB = 1024 * 1024
# M5.1 original-scene largest observed worker peak WS = 134,082,560 bytes.
# 512 MiB is an admission reservation with margin, NOT a universal memory estimate.
PROFILES = {'553b9bd35d9234a319b2d8424fb8c0e95a0af722327ddd6103485d7d809ff13b': 512*MIB}


@dataclass(frozen=True)
class Capacity:
    physical_cores: int
    logical_processors: int
    available_cpu_capacity: int
    available_memory_bytes: int


def discover_capacity():
    if sys.platform == 'linux':
        return _linux_capacity()
    if os.name != 'nt':
        raise RuntimeError('CPU admission supports Windows and Linux only.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(process_mask), ctypes.byref(system_mask)):
        raise OSError('Cannot discover process CPU availability.')
    # Ex enumerates physical-core records across all processor groups. Admission
    # deliberately uses the inherited primary-group affinity mask below; it never
    # claims that other groups or unverified CPU sets are available to children.
    length = wintypes.DWORD()
    kernel.GetLogicalProcessorInformationEx(0xffff,None,ctypes.byref(length))
    if not 0 < length.value <= 1024*1024:
        raise OSError('Cannot discover CPU topology.')
    buffer = ctypes.create_string_buffer(length.value)
    if not kernel.GetLogicalProcessorInformationEx(0xffff,buffer,ctypes.byref(length)):
        raise OSError('Cannot read CPU topology.')
    import struct
    physical=offset=0
    while offset<length.value:
        relationship,size=struct.unpack_from('<II',buffer,offset)
        if size<8 or offset+size>length.value:raise OSError('Invalid processor-group topology record.')
        physical+=relationship==0;offset+=size
    class Memory(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in ('total_physical','available_physical','total_page',
                'available_page','total_virtual','available_virtual','available_extended')]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError('Cannot discover available memory.')
    return Capacity(physical, os.cpu_count() or 1, process_mask.value.bit_count(), memory.available_physical)


def _linux_capacity():
    """Linux affinity, available RAM and cgroup v2 ceilings; no CPU placement claim."""
    affinity = os.sched_getaffinity(0)
    logical = os.cpu_count() or len(affinity)
    available = len(affinity)
    info = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
    memory = int(info['MemAvailable'].strip().split()[0]) * 1024
    physical = set()
    for cpu in affinity:
        topology = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
        try: physical.add(((topology/'physical_package_id').read_text().strip(), (topology/'core_id').read_text().strip()))
        except OSError: physical.add(('unknown', str(cpu)))
    # Walk the visible unified cgroup and its ancestors, including a namespace-root mount.
    group = next((line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')), '/')
    base = Path('/sys/fs/cgroup')
    current = base / group.lstrip('/')
    if '..' in Path(group).parts: raise RuntimeError('Invalid cgroup path.')
    if not current.is_dir(): current = base
    while current.is_relative_to(base):
        try:
            quota, period = (current/'cpu.max').read_text().split()
            if quota != 'max': available = min(available, max(1, int(quota)//int(period)))
        except FileNotFoundError: pass
        try:
            limit = (current/'memory.max').read_text().strip()
            if limit != 'max': memory = min(memory, max(0, int(limit)-int((current/'memory.current').read_text())))
        except FileNotFoundError: pass
        if current == base: break
        current = current.parent
    return Capacity(len(physical), logical, available, memory)


def select_workers(file_sha256, task_count, configured_cap=None, *, capacity=None):
    if configured_cap is not None and (type(configured_cap) is not int or not 1 <= configured_cap <= MAX_WORKERS):
        raise ValueError('Configured local worker cap must be 1..64.')
    if type(task_count) is not int or not 1 <= task_count <= 16384:
        raise ValueError('Invalid independent task count.')
    if file_sha256 not in PROFILES:
        raise ValueError('No measured resource admission profile for this scene; Local execution unavailable.')
    facts = discover_capacity() if capacity is None else capacity
    if any(type(v) is not int or v <= 0 for v in asdict(facts).values()):
        raise ValueError('Invalid resource discovery.')
    requested_ceiling=MAX_WORKERS if configured_cap is None else configured_cap
    automatic_max=min(requested_ceiling,facts.available_cpu_capacity,facts.logical_processors)
    reserve = 1024*MIB  # Leaves headroom for the parent, OS and other applications.
    memory_limit = max(0, (facts.available_memory_bytes-reserve)//PROFILES[file_sha256])
    selected = min(automatic_max, task_count, memory_limit)
    if selected < 1:
        raise MemoryError('Insufficient available scene memory reservation for a Local worker.')
    return dict(**asdict(facts), independent_tasks=task_count, configured_cap=automatic_max,
        requested_ceiling=requested_ceiling, admission_policy='automatic_available_logical_cpus',
        memory_worker_limit=memory_limit, per_worker_reservation_bytes=PROFILES[file_sha256],
        parent_os_reserve_bytes=reserve, selected_workers=selected,
        limitation=('Bounded by Linux CPU affinity, cgroup v2 CPU/memory ceilings, available RAM, tasks and cap; no per-core placement claim'
            if sys.platform=='linux' else 'Bounded by inherited primary-group process affinity mask, logical processors, tasks, cap and scene memory reservation. Physical topology uses all-group Ex enumeration. Other processor groups/CPU sets are conservatively excluded; no per-core placement claim'))
