"""Owned spawn processes and bounded point-to-point pipes, not an acceptance ledger.

One task per ready worker can be on a pipe. A coordinator window bounds collected
results. Pipes avoid Queue feeder threads and shared-writer corruption on forced
termination; every packet has a byte limit. No listener or retry exists.
"""
from dataclasses import dataclass
import math
import multiprocessing
import os
from time import perf_counter_ns, sleep

from drt_scheduler import LifecycleError
from . import codec


@dataclass(frozen=True, slots=True)
class ParallelOptions:
    workers: int = 2
    max_in_flight: int | None = None
    startup_seconds: float = 15
    task_seconds: float = 30
    shutdown_seconds: float = 3
    run_seconds: float = 60
    capacity_limit: int = 2

    def __post_init__(self):
        codec.integer(self.capacity_limit, 1, 64)
        codec.integer(self.workers, 1, self.capacity_limit)
        window = self.workers*2 if self.max_in_flight is None else self.max_in_flight
        codec.integer(window, self.workers, self.workers*2)
        object.__setattr__(self, 'max_in_flight', window)
        for name, cap in (('startup_seconds', 30), ('task_seconds', 60),
                          ('shutdown_seconds', 5), ('run_seconds', 75)):
            value = getattr(self, name)
            codec.require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= cap,
                          f'{name} must be positive and at most {cap}.')


class SpawnDispatcher:
    def __init__(self, initialization, job, options=ParallelOptions(), *, _worker_target=None, _codec=None):
        from drt_backend.pbr_execution import codec as pbr_codec
        from drt_backend.room_pbr import codec as room_codec
        if not isinstance(options, ParallelOptions):
            raise TypeError('ParallelOptions required.')
        limit=64 if _codec is room_codec else 2
        codec.require(_codec in (pbr_codec,room_codec) and options.workers<=limit and callable(_worker_target),
                      'Explicit registered PBR codec, Worker target and supported capacity required.')
        self.codec=_codec
        transport = self.codec
        transport.read_initialization(initialization)
        self.initialization, self.job, self.options = initialization, job, options
        self._target = _worker_target
        self.context = multiprocessing.get_context('spawn')
        self.cancellation = self.context.Event()
        self.handles = []
        self.closed = False
        self.started = False
        self.shutdown_report = None
        self.dispatch_nanoseconds = self.collection_nanoseconds = self.wait_nanoseconds = 0
        self.max_result_bytes = self.peak_active = 0
        self.arrival_ordinals = []
        self.started_ns = 0

    def _live(self):
        if self.closed:
            raise LifecycleError('SpawnDispatcher is closed.')

    def _read(self, handle):
        codec = self.codec
        before = perf_counter_ns()
        try:
            raw = handle['connection'].recv_bytes(codec.MAX_RESULT_BYTES)
            self.max_result_bytes = max(self.max_result_bytes, len(raw))
            message = codec.decode(raw)
            codec.check_header(message, self.job, handle['worker_id'], handle['pid'])
            kind = message['kind']
            if kind == 'ready':
                codec.require(set(message) == codec.HEADER_KEYS | {
                    'initialization_started_ns', 'ready_ns', 'scene_preparations', 'render_preparations',
                    'file_sha256', 'texture_bytes', 'blas_count', 'memory', 'child_processes', 'parent_pid'},
                    'Invalid ready fields.')
                codec.require(handle['ready'] is None and message['scene_preparations'] == 1
                              and message['render_preparations'] == 1 and message['child_processes'] == 0
                              and message['parent_pid'] == os.getpid(), 'Invalid/repeated worker initialization.')
                init, _ = codec.read_initialization(self.initialization)
                codec.require(message['file_sha256'] == init['file_sha256'], 'Ready file hash differs.')
                codec.integer(message['initialization_started_ns'], 1)
                codec.integer(message['ready_ns'], message['initialization_started_ns'])
                handle['ready'] = message | {'received_ns': perf_counter_ns()}
                if self.codec.VERSION=='drt.local_room_pbr_codec.v1':
                    from drt_backend.room_pbr.admission import ready
                else:
                    from drt_backend.pbr_execution.admission import ready
                ready(message)
            elif kind == 'result':
                task = handle['busy']
                codec.require(handle['ready'] is not None and handle['stopped'] is None
                              and task is not None and message.get('task') == codec.task_data(task),
                              'Unsolicited, duplicate or foreign result.')
                handle['busy'] = None
                self.arrival_ordinals.append(task.ordinal)
            elif kind == 'worker_error':
                codec.require(set(message) == codec.HEADER_KEYS | {'error'}, 'Invalid worker error fields.')
                raise codec.read_failure(message['error'])
            elif kind == 'stopped':
                codec.require(set(message) == codec.HEADER_KEYS | {
                    'cancellation_acknowledged', 'scene_preparations', 'render_preparations', 'scene_closes',
                    'texture_bytes_after_close', 'executed_tasks', 'reported_executed_rays', 'memory',
                    'cleanup_error', 'stopped_ns'}, 'Invalid stopped fields.')
                codec.require(handle['stopped'] is None and type(message['cancellation_acknowledged']) is bool,
                              'Repeated/invalid stop confirmation.')
                for name in ('scene_preparations', 'render_preparations', 'scene_closes'):
                    codec.integer(message[name], 0, 1)
                codec.require(message['scene_closes'] == message['scene_preparations']
                              and message['texture_bytes_after_close'] == 0, 'Worker resource release incomplete.')
                codec.integer(message['executed_tasks'], 0, 16384)
                rays = message['reported_executed_rays']
                codec.validate_rays(rays)
                if message['cleanup_error'] is not None:
                    raise codec.read_failure(message['cleanup_error'])
                handle['stopped'] = message
            else:
                raise codec.LocalProtocolError('Unknown worker event.')
            return message
        except (EOFError, OSError) as error:
            raise codec.WorkerFailure('WorkerPipeClosed', f"{handle['worker_id']} PID {handle['pid']}: {error}") from error
        finally:
            self.collection_nanoseconds += perf_counter_ns()-before

    def start(self, token, run_deadline):
        codec = self.codec
        self._live()
        if self.started:
            raise LifecycleError('Dispatcher is single-use.')
        self.started = True
        self.started_ns = perf_counter_ns()
        for index in range(self.options.workers):
            token.checkpoint()
            parent, child = self.context.Pipe(duplex=True)
            worker_id = f'worker-{index}'
            process = self.context.Process(target=self._target,
                args=(self.initialization, worker_id, child, self.cancellation),
                name=f'drt-{worker_id}', daemon=True)
            handle = dict(worker_id=worker_id, process=process, connection=parent, pid=None,
                          ready=None, stopped=None, busy=None, submitted_ns=0, assignments=[], forced=False)
            self.handles.append(handle)
            try:
                process.start()
                handle['pid'] = process.pid
            finally:
                child.close()
        deadline = min(run_deadline, self.started_ns+int(self.options.startup_seconds*1e9))
        while not all(h['ready'] is not None for h in self.handles):
            token.checkpoint()
            if perf_counter_ns() >= deadline:
                raise codec.WorkerFailure('WorkerStartupTimeout', 'Readiness deadline expired.')
            for handle in self.handles:
                if handle['ready'] is None and handle['connection'].poll():
                    message = self._read(handle)
                    codec.require(message['kind'] == 'ready', 'Worker stopped before readiness.')
                if not handle['process'].is_alive() and handle['ready'] is None:
                    raise codec.WorkerFailure('WorkerCrashed', f"{handle['worker_id']} exited during startup.")
            sleep(.005)
        return perf_counter_ns()-self.started_ns

    @property
    def idle_workers(self):
        self._live()
        return [h for h in self.handles if h['ready'] is not None and h['busy'] is None and h['stopped'] is None]

    def submit(self, handle, task):
        codec = self.codec
        self._live()
        codec.require(handle in self.idle_workers and task.job == self.job, 'Dispatch requires a ready idle owned worker.')
        before = perf_counter_ns()
        handle['connection'].send_bytes(codec.task_packet(task))
        handle['busy'], handle['submitted_ns'] = task, before
        handle['assignments'].append(task.ordinal)
        self.peak_active = max(self.peak_active, sum(h['busy'] is not None for h in self.handles))
        self.dispatch_nanoseconds += perf_counter_ns()-before

    def receive(self, token, run_deadline):
        codec = self.codec
        self._live()
        before = perf_counter_ns()
        try:
            while True:
                token.checkpoint()
                now = perf_counter_ns()
                if now >= run_deadline:
                    raise codec.WorkerFailure('RenderServiceTimeout', 'Service deadline expired.')
                for handle in self.handles:
                    if handle['connection'].poll():
                        message = self._read(handle)
                        codec.require(message['kind'] == 'result', 'Unexpected lifecycle event during execution.')
                        return handle, message
                    if not handle['process'].is_alive():
                        raise codec.WorkerFailure('WorkerCrashed', f"{handle['worker_id']} exited unexpectedly ({handle['process'].exitcode}).")
                    if handle['busy'] is not None and now-handle['submitted_ns'] > self.options.task_seconds*1e9:
                        raise codec.WorkerFailure('WorkerTaskTimeout', f"{handle['worker_id']} task deadline expired.")
                sleep(.005)
        finally:
            self.wait_nanoseconds += perf_counter_ns()-before

    def close(self, *, abort=False):
        codec = self.codec
        if self.closed:
            return self.shutdown_report
        self.closed = True
        before = perf_counter_ns()
        errors = []
        if abort:
            self.cancellation.set()
        for handle in self.handles:
            if handle['process'].pid is not None and handle['process'].is_alive():
                try:
                    handle['connection'].send_bytes(codec.encode({'version': codec.VERSION, 'kind': 'stop'}, codec.MAX_TASK_BYTES))
                except (OSError, EOFError) as error:
                    errors.append(str(error))
        deadline = before+int(self.options.shutdown_seconds*1e9)
        while perf_counter_ns() < deadline:
            for handle in self.handles:
                try:
                    if handle['stopped'] is None and handle['connection'].poll():
                        message = self._read(handle)
                        if not abort and message['kind'] != 'stopped':
                            errors.append('Unexpected/post-terminal result during normal shutdown.')
                except Exception as error:
                    # On an aborted run, the primary failure already explains a broken child.
                    if not abort:
                        errors.append(str(error))
                if handle['process'].pid is not None:
                    handle['process'].join(timeout=0)
            if all(not h['process'].is_alive() for h in self.handles):
                # One last read for a small stopped packet flushed immediately before exit.
                for handle in self.handles:
                    try:
                        if handle['stopped'] is None and handle['connection'].poll():
                            self._read(handle)
                    except Exception as error:
                        if not abort:
                            errors.append(str(error))
                break
            sleep(.005)
        for handle in self.handles:
            if handle['process'].is_alive():
                handle['forced'] = True
                handle['process'].terminate()
        reap_deadline = perf_counter_ns()+int(1e9)
        for handle in self.handles:
            process = handle['process']
            if process.pid is not None:
                process.join(timeout=max(0, (reap_deadline-perf_counter_ns())/1e9))
                if process.is_alive():
                    process.kill()
        # A single shared final deadline, independent of the configured process count.
        kill_deadline = perf_counter_ns()+int(.5e9)
        for handle in self.handles:
            process = handle['process']
            if process.pid is not None:
                process.join(timeout=max(0,(kill_deadline-perf_counter_ns())/1e9))
        remaining, workers = [], []
        for handle in self.handles:
            process = handle['process']
            if process.is_alive():
                remaining.append(handle['pid'])
            workers.append(dict(worker_id=handle['worker_id'], pid=handle['pid'], ready=handle['ready'],
                stopped=handle['stopped'], forced=handle['forced'], exit_code=process.exitcode,
                assigned_ordinals=handle['assignments']))
            handle['connection'].close()
            if not process.is_alive():
                process.close()
        self.shutdown_report = dict(workers=workers, remaining_owned_pids=remaining,
            forced=any(h['forced'] for h in self.handles), errors=errors,
            cancellation_confirmed=abort and not remaining and all(
                w['stopped'] is not None and w['stopped']['cancellation_acknowledged']
                and not w['forced'] and w['exit_code'] == 0
                for w in workers), shutdown_nanoseconds=perf_counter_ns()-before)
        return self.shutdown_report
