"""One synchronous executor call at a time; no worker/transport runtime."""
from time import perf_counter
from threading import Lock

from .acceptance import TaskAcceptance
from .errors import CancellationRequested, DefinitionError, LifecycleError
from .interfaces import CancellationToken, ExecutionContext, Observer, TaskExecutor, WorkloadAdapter
from .models import (ExecutionStatus, JobDefinition, JobOutcome, JobState, JobTimings,
                     LifecycleObservation, ProgressObservation, TaskResult, TaskState)


class LocalSerialExecutor:
    def __init__(self, adapter: WorkloadAdapter, executor_id: str = 'local-serial'):
        self.adapter = adapter
        self.executor_id = executor_id

    def execute(self, task, context):
        start = perf_counter()
        try:
            context.cancellation.checkpoint()
            payload = self.adapter.execute(task, context)
            context.cancellation.checkpoint()
        except CancellationRequested as error:
            return TaskResult.for_task(task, ExecutionStatus.CANCELLED, failure=error,
                executor_id=self.executor_id, elapsed_seconds=perf_counter()-start)
        except Exception as error:
            return TaskResult.for_task(task, ExecutionStatus.FAILED, failure=error,
                executor_id=self.executor_id, elapsed_seconds=perf_counter()-start)
        return TaskResult.for_task(task, ExecutionStatus.COMPLETED, payload=payload,
            executor_id=self.executor_id, elapsed_seconds=perf_counter()-start)


class SerialScheduler:
    """run() owns one isolated ledger/context; it starts no threads or processes.

    Operational/observer failures before finalization produce Failed and preserve
    the actual exception. Finalization is the completion commit and can win a late
    cancellation race. Cleanup failure always produces Failed. Terminal observer
    delivery is attempted once, after cleanup; delivery errors are recorded without
    changing or re-emitting that committed terminal state. Control-flow exceptions
    outside Exception still unwind through release, then propagate.
    """
    def __init__(self):
        self._gate = Lock()
        self._used = False

    def run(self, job: JobDefinition, adapter: WorkloadAdapter, *,
            cancellation: CancellationToken | None = None,
            observer: Observer | None = None, executor: TaskExecutor | None = None) -> JobOutcome:
        if not isinstance(job, JobDefinition):
            raise DefinitionError('run requires JobDefinition.')
        token = cancellation if cancellation is not None else CancellationToken()
        if not isinstance(token, CancellationToken):
            raise DefinitionError('Cancellation requires CancellationToken.')
        with self._gate:
            if self._used:
                raise LifecycleError('SerialScheduler is a single-run acceptance authority.')
            self._used = True
        start = perf_counter()
        context = ExecutionContext(job, token)
        ledger = TaskAcceptance(job)
        execution = executor if executor is not None else LocalSerialExecutor(adapter)
        stage_times = dict(preparation=0., execution=0., acceptance=0., finalization=0., cleanup=0.)
        state, result, failure, active, iterator = JobState.FAILED, None, None, None, None
        cleanup_errors, observer_errors = [], []

        def notify(observation):
            if observer is not None:
                try:
                    observer(observation)
                except Exception as error:
                    observer_errors.append(error)
                    raise

        def measured(stage, operation):
            before = perf_counter()
            try:
                return operation()
            finally:
                stage_times[stage] += perf_counter()-before

        try:
            token.checkpoint()
            notify(LifecycleObservation(job.run_id, JobState.PREPARING))
            token.checkpoint()
            measured('preparation', lambda: adapter.prepare(context))
            token.checkpoint()
            notify(LifecycleObservation(job.run_id, JobState.RUNNING))
            notify(ProgressObservation(job.run_id, 0, job.expected_units, 0))
            iterator = iter(adapter.enumerate_tasks(job))
            while True:
                token.checkpoint()
                try:
                    task = next(iterator)
                except StopIteration:
                    break
                ledger.register(task)
                active = task
                token.checkpoint()
                ledger.start(task)
                response = measured('execution', lambda: execution.execute(task, context))
                ledger.validate_result(task, response)
                if response.status is ExecutionStatus.FAILED:
                    ledger.stop_task(task, TaskState.FAILED)
                    active = None
                    raise response.failure
                if response.status is ExecutionStatus.CANCELLED:
                    ledger.stop_task(task, TaskState.CANCELLED)
                    active = None
                    token.request()
                    raise CancellationRequested('Executor confirmed task cancellation.')
                token.checkpoint()
                measured('acceptance', lambda: token.commit(lambda: ledger.accept(task, response,
                    lambda: adapter.validate_and_accept(task, response, context))))
                active = None
                # Do not retain a large payload across task boundaries.
                response = None
                notify(ProgressObservation(job.run_id, ledger.completed_units, job.expected_units, ledger.accepted_tasks))
            ledger.require_complete()
            notify(LifecycleObservation(job.run_id, JobState.FINALIZING))
            result = measured('finalization', lambda: token.commit(lambda: adapter.finalize(context)))
            state = JobState.COMPLETED
        except CancellationRequested:
            state = JobState.CANCELLED
            if active is not None:
                ledger.stop_task(active, TaskState.CANCELLED)
        except Exception as error:
            failure, state = error, JobState.FAILED
            if active is not None:
                ledger.stop_task(active, TaskState.FAILED)
        finally:
            before = perf_counter()
            if iterator is not None and hasattr(iterator, 'close'):
                try:
                    iterator.close()
                except Exception as error:
                    cleanup_errors.append(error)
            try:
                adapter.release(context)
            except Exception as error:
                cleanup_errors.append(error)
            ledger.finish()
            stage_times['cleanup'] = perf_counter()-before
        if cleanup_errors:
            state = JobState.FAILED
            if failure is None:
                failure = cleanup_errors[0]
        if state is not JobState.COMPLETED:
            result = None
        try:
            notify(LifecycleObservation(job.run_id, state))
        except Exception:
            pass  # Already recorded; no second terminal observation/outcome.
        return JobOutcome(job, state, ledger.completed_units, ledger.records,
            JobTimings(**stage_times, total=perf_counter()-start), result, failure,
            tuple(cleanup_errors), tuple(observer_errors))
