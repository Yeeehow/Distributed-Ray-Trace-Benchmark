"""Owned PBR spawn target; one M6.3 scene/BVH, bounded byte-only transport."""
from multiprocessing import active_children
import os
from time import perf_counter_ns

from drt_scheduler import ExecutionContext, CancellationRequested
from drt_backend.local_parallel.worker import ProcessCheckpoint, process_memory
from drt_backend.raytracing.pathtracing.integrator import add_counters
from . import codec


class EventToken:
    def __init__(self, event):
        self.checkpoint = ProcessCheckpoint(event)


def worker_main(raw, worker_id, connection, cancellation, *, _codec=None):
    codec=globals()['codec'] if _codec is None else _codec
    if _codec is not None:
        from drt_backend.room_pbr import codec as room_codec
        if _codec is not room_codec:raise ValueError('Unregistered PBR worker codec.')
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    init, job = codec.read_initialization(raw)
    pid = os.getpid(); start = perf_counter_ns()
    token = EventToken(cancellation)
    context = ExecutionContext(job, token)
    adapter = None; prepares = closed = executed = 0

    def send(kind, **data):
        connection.send_bytes(codec.encode(codec.header(kind, job, worker_id, pid) | data))

    try:
        token.checkpoint()
        adapter = codec.describe(init['scene'], job)
        adapter.prepare(context); prepares = 1
        token.checkpoint()
        scene = context.resources.scene.prepared
        send('ready', initialization_started_ns=start, ready_ns=perf_counter_ns(), scene_preparations=1,
             render_preparations=1, file_sha256=init['file_sha256'], texture_bytes=scene.textures.retained_bytes,
             blas_count=len(scene.acceleration.blas_by_mesh), memory=process_memory(),
             child_processes=len(active_children()), parent_pid=os.getppid())
        seen = set()
        while True:
            token.checkpoint()
            if not connection.poll(.05):
                continue
            packet = codec.decode(connection.recv_bytes(codec.MAX_TASK_BYTES), codec.MAX_TASK_BYTES)
            if packet == dict(version=codec.VERSION, kind='stop'):
                break
            task = codec.read_task(packet, job)
            codec.require(task.task_id not in seen, 'Duplicate PBR task in Worker.')
            seen.add(task.task_id)
            before = perf_counter_ns(); offset = len(adapter._executed)
            status, payload, error = 'completed', None, None
            try:
                payload = adapter.execute(task, context)
                token.checkpoint()
            except CancellationRequested as e:
                status, error = 'cancelled', e
            except Exception as e:
                status, error = 'failed', e
            counters = add_counters(adapter._executed[offset:]); executed += 1
            connection.send_bytes(codec.result_packet(job, worker_id, pid, task, status, counters,
                before, perf_counter_ns(), payload if status == 'completed' else None, error))
            if status != 'completed':
                break
    except CancellationRequested:
        pass
    except Exception as e:
        try:
            send('worker_error', error=codec.failure_data(e))
        except (OSError, EOFError):
            pass
    finally:
        error = None
        if adapter is not None:
            try:
                adapter.release(context)
                closed = prepares
            except Exception as e:
                error = codec.failure_data(e)
        counters = add_counters(() if adapter is None else adapter._executed)
        try:
            send('stopped', cancellation_acknowledged=cancellation.is_set(), scene_preparations=prepares,
                 render_preparations=prepares, scene_closes=closed, texture_bytes_after_close=0,
                 executed_tasks=executed, reported_executed_rays=codec.ray_counts(counters.primary_rays,
                     counters.shadow_rays, counters.secondary_rays), memory=process_memory(),
                 cleanup_error=error, stopped_ns=perf_counter_ns())
        finally:
            connection.close()
