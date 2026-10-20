"""Headless local parallel service. One original acceptance ledger, no new scheduler hierarchy."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from threading import Lock
from time import perf_counter_ns

from drt_scheduler import (CancellationToken, CancellationRequested, ExecutionContext,
    TaskAcceptance, ExecutionStatus, JobState, JobOutcome, JobTimings, TaskState,
    LifecycleError, LifecycleObservation, ProgressObservation)
from drt_backend.local_parallel import codec
from drt_backend.local_parallel.dispatcher import ParallelOptions, SpawnDispatcher
from drt_backend.local_parallel.worker import process_memory

MAX_JOB_TASKS = 16384


class OrderedAcceptance:
    """Renderer translation/reorder buffer around the sole TaskAcceptance ledger.

    pending includes running and buffered results, so a slow early range cannot
    permit unlimited later dispatch. Only ledger.accept's callback copies pixels.
    """
    def __init__(self, adapter, context, window, observer=None, contribution_observer=None, *, _codec=None):
        codec.require(_codec is not None and hasattr(_codec,'validate_rays'),'Explicit PBR acceptance codec required.')
        self.codec = _codec
        self.adapter, self.context, self.window, self.observer = adapter, context, window, observer
        self.ledger = TaskAcceptance(context.job)
        self.pending, self.buffer, self.workers = {}, {}, {}
        self.events = []
        self.next_ordinal = self.peak_pending = self.peak_buffer = 0
        self.acceptance_ns = 0
        self.closed = False
        self.contribution_observer = contribution_observer

    def register(self, task):
        codec = self.codec
        if self.closed:
            raise LifecycleError('Parallel acceptance is terminal.')
        codec.require(len(self.pending) < self.window, 'Outstanding task window exceeded.')
        self.ledger.register(task)
        self.ledger.start(task)
        self.pending[task.task_id] = task
        self.peak_pending = max(self.peak_pending, len(self.pending))

    def offer(self, message, worker_id, pid, *, identity=None):
        codec = self.codec
        if self.closed:
            raise LifecycleError('Parallel acceptance is terminal.')
        data = message.get('task', {})
        task = self.pending.get(data.get('task_id')) if isinstance(data, dict) else None
        codec.require(task is not None and task.ordinal not in self.buffer,
                      'Duplicate/stale/foreign/unregistered result.')
        result, rays = codec.translate_result(message, self.adapter, task, self.context, worker_id, pid)
        self.ledger.validate_result(task, result)
        worker_key = worker_id if identity is None else identity
        worker = self.workers.setdefault(worker_key, dict(pid=pid, received_tasks=0, accepted_tasks=0,
            accepted_primary=0, accepted_shadow=0, reported_result_primary=0, reported_result_shadow=0))
        if 'secondary' in rays:
            worker.setdefault('accepted_secondary', 0)
            worker.setdefault('accepted_samples', 0)
            worker.setdefault('reported_result_secondary', 0)
            worker['reported_result_secondary'] += rays['secondary']
        codec.require(worker['pid'] == pid, 'Worker PID changed within a run.')
        worker['received_tasks'] += 1
        worker['reported_result_primary'] += rays['primary']
        worker['reported_result_shadow'] += rays['shadow']
        event = dict(worker_id=worker_key, pid=pid, ordinal=task.ordinal, task_id=task.task_id,
            start=message['started_ns'], end=message['ended_ns'], rays=rays, status=result.status.value,
            accepted=False)
        self.events.append(event)
        if result.status is not ExecutionStatus.COMPLETED:
            self.ledger.stop_task(task, TaskState.FAILED if result.status is ExecutionStatus.FAILED else TaskState.CANCELLED)
            del self.pending[task.task_id]
            if result.status is ExecutionStatus.FAILED:
                raise result.failure
            codec.require(self.context.cancellation.requested, 'Unsolicited worker cancellation.')
            raise CancellationRequested('Worker cancellation result observed.')
        self.buffer[task.ordinal] = task, result, rays, worker, event
        self.peak_buffer = max(self.peak_buffer, len(self.buffer))
        while self.next_ordinal in self.buffer:
            task, result, rays, worker, event = self.buffer[self.next_ordinal]
            codec.integer(self.accepted_rays['total']+rays['total'])
            before = perf_counter_ns()
            self.context.cancellation.commit(lambda: self.ledger.accept(task, result,
                lambda: self.adapter.validate_and_accept(task, result, self.context)))
            self.acceptance_ns += perf_counter_ns()-before
            # Contribution exists only after the original ledger/assembler commit.
            worker['accepted_tasks'] += 1
            worker['accepted_primary'] += rays['primary']
            worker['accepted_shadow'] += rays['shadow']
            if 'secondary' in rays:
                worker['accepted_secondary'] += rays['secondary']
                worker['accepted_samples'] += task.expected_units
            event['accepted'] = True
            del self.buffer[self.next_ordinal]
            del self.pending[task.task_id]
            self.next_ordinal += 1
            if self.observer:
                self.observer(ProgressObservation(self.context.job.run_id, self.ledger.completed_units,
                              self.context.job.expected_units, self.ledger.accepted_tasks))
            if self.contribution_observer:
                self.contribution_observer(event['worker_id'],worker['pid'],worker['accepted_primary'],
                    worker['accepted_shadow'],worker['accepted_secondary'],worker['accepted_samples'])

    @property
    def accepted_rays(self):
        return self.codec.ray_counts(sum(w['accepted_primary'] for w in self.workers.values()),
            sum(w['accepted_shadow'] for w in self.workers.values()),
            sum(w.get('accepted_secondary',0) for w in self.workers.values()))

    def require_complete(self):
        self.ledger.require_complete()
        codec.require(not self.pending and not self.buffer, 'Uncommitted task/result remains.')

    def finish(self):
        if not self.closed:
            self.ledger.finish()
            self.buffer.clear()
            self.pending.clear()
            self.closed = True


@dataclass(frozen=True, slots=True)
class ParallelRenderOutcome:
    outcome: JobOutcome
    diagnostics: dict


@dataclass(slots=True)
class ParallelSubmission:
    source_path: Path
    file_sha256: str
    adapter: object
    options: ParallelOptions
    description_seconds: float
    transport_codec: object = None
    _used: bool = field(default=False, init=False, repr=False)
    _gate: object = field(default_factory=Lock, init=False, repr=False)

    @property
    def definition(self):
        return self.adapter.job

    def run(self, *, cancellation=None, observer=None, contribution_observer=None, _worker_target=None):
        codec = self.transport_codec
        if codec is None:raise ValueError('Explicit PBR transport codec required.')
        token = cancellation if cancellation is not None else CancellationToken()
        if not isinstance(token, CancellationToken):
            raise TypeError('CancellationToken required.')
        with self._gate:
            if self._used:
                raise LifecycleError('ParallelSubmission is single-use.')
            self._used = True
        started = perf_counter_ns()
        deadline = started+int(self.options.run_seconds*1e9)
        context = ExecutionContext(self.definition, token)
        errors, observer_errors = [], []

        def notify(item):
            if observer:
                try:
                    observer(item)
                except Exception as error:
                    observer_errors.append(error)
                    raise

        acceptance = OrderedAcceptance(self.adapter, context, self.options.max_in_flight, notify, contribution_observer,
                                       _codec=self.transport_codec)
        pool = SpawnDispatcher(codec.initialization(self.source_path, self.file_sha256, self.definition),
                               self.definition, self.options, _worker_target=_worker_target, _codec=self.transport_codec)
        state, result, failure = JobState.FAILED, None, None
        preparation_ns = execution_ns = finalization_ns = startup_ns = 0
        iterator = None
        loop_started = 0
        parent_memory = {'before': process_memory()}
        shutdown = None
        try:
            token.checkpoint()
            notify(LifecycleObservation(self.definition.run_id, JobState.PREPARING))
            before = perf_counter_ns()
            try:
                self.adapter.prepare(context)
            finally:
                preparation_ns = perf_counter_ns()-before
            before = perf_counter_ns()
            try:
                pool.start(token, deadline)
            finally:
                startup_ns = perf_counter_ns()-before
            notify(LifecycleObservation(self.definition.run_id, JobState.RUNNING))
            if contribution_observer:
                for handle in pool.handles:
                    contribution_observer(handle['worker_id'],handle['pid'],0,0,0,0)
            notify(ProgressObservation(self.definition.run_id, 0, self.definition.expected_units, 0))
            iterator = iter(self.adapter.enumerate_tasks(self.definition))
            exhausted = False
            before = perf_counter_ns()
            loop_started = before
            while not exhausted or acceptance.pending:
                token.checkpoint()
                if perf_counter_ns() >= deadline:
                    raise codec.WorkerFailure('RenderServiceTimeout', 'Service deadline expired.')
                for handle in pool.idle_workers:
                    if exhausted or len(acceptance.pending) >= self.options.max_in_flight:
                        break
                    try:
                        task = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    acceptance.register(task)
                    pool.submit(handle, task)
                if acceptance.pending:
                    handle, message = pool.receive(token, deadline)
                    acceptance.offer(message, handle['worker_id'], handle['pid'])
            execution_ns = perf_counter_ns()-before
            acceptance.require_complete()
            notify(LifecycleObservation(self.definition.run_id, JobState.FINALIZING))
            before = perf_counter_ns()
            result = token.commit(lambda: self.adapter.finalize(context))
            finalization_ns = perf_counter_ns()-before
            state = JobState.COMPLETED
        except CancellationRequested:
            state = JobState.CANCELLED
        except Exception as error:
            failure = error
        finally:
            cleanup_started = perf_counter_ns()
            if loop_started and not execution_ns:
                execution_ns = cleanup_started-loop_started
            try:
                shutdown = pool.close(abort=state is not JobState.COMPLETED)
                if shutdown['forced'] or shutdown['remaining_owned_pids'] or shutdown['errors']:
                    forced_pids = [w['pid'] for w in shutdown['workers'] if w['forced']]
                    raise codec.WorkerFailure('ForcedOrUnverifiedShutdown',
                        f"forced PIDs={forced_pids}; remaining PIDs={shutdown['remaining_owned_pids']}; "
                        f"shutdown errors={shutdown['errors'][:3]}")
                if state is JobState.CANCELLED and not shutdown['cancellation_confirmed']:
                    raise codec.WorkerFailure('CancellationUnconfirmed', 'Workers did not confirm stopped cancellation.')
                if state is JobState.COMPLETED and any(w['stopped'] is None or w['exit_code'] != 0 for w in shutdown['workers']):
                    raise codec.WorkerFailure('WorkerShutdownFailure', 'Completion requires stopped resources and natural exit0.')
                for worker in shutdown['workers']:
                    stopped = worker['stopped']
                    if stopped is None:
                        continue  # Crash/forced effort remains explicitly unreported.
                    accepted = acceptance.workers.get(worker['worker_id'], {})
                    primary, shadow = accepted.get('accepted_primary', 0), accepted.get('accepted_shadow', 0)
                    executed = stopped['reported_executed_rays']
                    if self.transport_codec is not None:
                        codec.require(executed['secondary'] >= accepted.get('accepted_secondary', 0),
                                      'PBR executed secondary effort contradicts acceptance.')
                    codec.require(executed['primary'] >= primary and executed['shadow'] >= shadow
                                  and stopped['executed_tasks'] <= len(worker['assigned_ordinals']),
                                  'Final execution diagnostics contradict accepted work.')
                    if state is JobState.COMPLETED:
                        expected_rays = codec.ray_counts(primary,shadow,accepted.get('accepted_secondary',0))
                        codec.require(executed == expected_rays
                                      and stopped['executed_tasks'] == accepted.get('accepted_tasks', 0),
                                      'Completed worker totals differ from central accepted results.')
            except Exception as error:
                errors.append(error)
            if iterator is not None and hasattr(iterator, 'close'):
                try:
                    iterator.close()
                except Exception as error:
                    errors.append(error)
            try:
                self.adapter.release(context)
            except Exception as error:
                errors.append(error)
            acceptance.finish()
            cleanup_ns = perf_counter_ns()-cleanup_started
        if errors:
            state = JobState.FAILED
            failure = failure or errors[0]
        if state is not JobState.COMPLETED:
            result = None
        try:
            notify(LifecycleObservation(self.definition.run_id, state))
        except Exception:
            pass  # One terminal outcome after cleanup; observer error is retained.
        parent_memory['after'] = process_memory()
        total_ns = perf_counter_ns()-started
        timings = JobTimings(preparation=(preparation_ns+startup_ns)/1e9,
            execution=max(0, execution_ns-acceptance.acceptance_ns)/1e9,
            acceptance=acceptance.acceptance_ns/1e9, finalization=finalization_ns/1e9,
            cleanup=cleanup_ns/1e9, total=total_ns/1e9)
        outcome = JobOutcome(self.definition, state, acceptance.ledger.completed_units, acceptance.ledger.records,
                             timings, result, failure, tuple(errors), tuple(observer_errors))
        diagnostics = dict(schema_version=1, kind='drt.local_parallel_diagnostics', parent_pid=os.getpid(),
            start_method='spawn', run_id=str(self.definition.run_id), file_sha256=self.file_sha256,
            job_identity=self.definition.job_identity, resource_identity=self.definition.resource_identity,
            configuration_identity=self.definition.configuration_identity,
            service_started_ns=started, service_ended_ns=started+total_ns,
            description_seconds=self.description_seconds, service_nanoseconds=total_ns,
            parent_preparation_nanoseconds=preparation_ns, startup_nanoseconds=startup_ns,
            dispatch_send_nanoseconds=pool.dispatch_nanoseconds, collection_decode_nanoseconds=pool.collection_nanoseconds,
            collection_wait_nanoseconds=pool.wait_nanoseconds, ordered_acceptance_nanoseconds=acceptance.acceptance_ns,
            task_events=acceptance.events, arrival_ordinals=pool.arrival_ordinals,
            accepted_samples=outcome.completed_units, accepted_tasks=acceptance.ledger.accepted_tasks,
            task_count=len(outcome.tasks), accepted_rays=acceptance.accepted_rays,
            per_worker_acceptance=acceptance.workers, workers=[] if shutdown is None else shutdown['workers'],
            shutdown=shutdown, limits=dict(workers=self.options.workers, max_in_flight=self.options.max_in_flight,
                per_worker_outstanding=1, max_result_bytes=codec.MAX_RESULT_BYTES, max_task_bytes=codec.MAX_TASK_BYTES,
                max_job_tasks=MAX_JOB_TASKS), peaks=dict(outstanding=acceptance.peak_pending,
                buffered_results=acceptance.peak_buffer, active_workers=pool.peak_active,
                result_bytes=pool.max_result_bytes), parent_memory=parent_memory,
            timing_definition='Service includes parent preparation, spawn/readiness, concurrent dispatch/collection/central acceptance, finalization and shutdown. Excludes description/input snapshot and later PNG publication. Diagnostic only, not a v3 benchmark score.',
            clock='perf_counter_ns; Windows QueryPerformanceCounter common host clock; no CPU affinity assertion',
            terminal_state=state.value, error=None if failure is None else codec.failure_data(failure))
        return ParallelRenderOutcome(outcome, diagnostics)




