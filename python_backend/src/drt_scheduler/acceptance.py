"""Serialized authoritative acceptance ledger, independent of workload payloads."""
from dataclasses import replace
import math

from .errors import DefinitionError, LifecycleError, ResultRejectedError
from .models import ExecutionStatus, JobDefinition, TaskDefinition, TaskRecord, TaskResult, TaskState


class TaskAcceptance:
    """One owner serializes register/start/accept/finish calls, also for future dispatch.

    Retains descriptors/states, never result payloads. Metadata storage is O(tasks).
    A validator must fully validate its payload before committing assembly writes.
    """
    def __init__(self, job: JobDefinition):
        if not isinstance(job, JobDefinition):
            raise DefinitionError('Acceptance requires JobDefinition.')
        self.job = job
        self._records = {}
        self._registered_units = 0
        self._completed_units = 0
        self._accepted_tasks = 0
        self._closed = False
        self._committing = False

    @property
    def completed_units(self):
        return self._completed_units

    @property
    def accepted_tasks(self):
        return self._accepted_tasks

    @property
    def records(self):
        return tuple(self._records.values())

    def _open(self):
        if self._closed:
            raise LifecycleError('Job acceptance is terminal.')
        if self._committing:
            raise LifecycleError('Acceptance callbacks cannot reenter the ledger.')

    def _record(self, task):
        self._open()
        if not isinstance(task, TaskDefinition) or task.job != self.job:
            raise ResultRejectedError('Foreign task/job definition.')
        record = self._records.get(task.task_id)
        if record is None or record.task != task:
            raise ResultRejectedError('Task is not the registered definition.')
        return record

    def register(self, task):
        self._open()
        if not isinstance(task, TaskDefinition) or task.job != self.job:
            raise ResultRejectedError('Foreign task/job definition.')
        if task.task_id in self._records or task.ordinal != len(self._records):
            raise ResultRejectedError('Duplicate task or nondeterministic ordinal.')
        if self._registered_units + task.expected_units > self.job.expected_units:
            raise ResultRejectedError('Task definitions exceed expected work.')
        self._records[task.task_id] = TaskRecord(task, TaskState.PENDING)
        self._registered_units += task.expected_units

    def start(self, task):
        record = self._record(task)
        if record.state is not TaskState.PENDING:
            raise LifecycleError('Only pending tasks may start.')
        self._records[task.task_id] = replace(record, state=TaskState.RUNNING)

    def validate_result(self, task, result):
        record = self._record(task)
        if record.state is not TaskState.RUNNING:
            raise ResultRejectedError('Result is stale, duplicate or not running.')
        if not isinstance(result, TaskResult):
            raise ResultRejectedError('Executor did not return TaskResult.')
        job = self.job
        expected = (job.run_id, task.task_id, job.job_identity, job.configuration_identity,
                    job.resource_identity, task.payload_identity)
        actual = (result.run_id, result.task_id, result.job_identity, result.configuration_identity,
                  result.resource_identity, result.payload_identity)
        if actual != expected:
            raise ResultRejectedError('Result correlation does not match the registered task.')
        if not isinstance(result.status, ExecutionStatus) or type(result.completed_units) is not int:
            raise ResultRejectedError('Invalid status or logical unit representation.')
        if result.status is ExecutionStatus.COMPLETED:
            if result.completed_units != task.expected_units or result.failure is not None:
                raise ResultRejectedError('Completed work has wrong units or an error.')
        elif result.completed_units != 0 or result.payload is not None:
            raise ResultRejectedError('Unaccepted execution cannot report partial work.')
        if result.status is ExecutionStatus.FAILED and not isinstance(result.failure, Exception):
            raise ResultRejectedError('Failed execution requires the actual failure.')
        if result.executor_id is not None and (type(result.executor_id) is not str or not result.executor_id):
            raise ResultRejectedError('Invalid executor identity.')
        if result.elapsed_seconds is not None and (type(result.elapsed_seconds) not in (int, float)
                or not math.isfinite(result.elapsed_seconds) or result.elapsed_seconds < 0):
            raise ResultRejectedError('Invalid measured elapsed time.')

    def accept(self, task, result, validate_and_commit):
        self.validate_result(task, result)
        if result.status is not ExecutionStatus.COMPLETED:
            raise ResultRejectedError('Only complete results may be accepted.')
        self._committing = True
        try:
            validate_and_commit()
        finally:
            self._committing = False
        self._records[task.task_id] = TaskRecord(task, TaskState.ACCEPTED, result.executor_id, result.elapsed_seconds)
        self._completed_units += task.expected_units
        self._accepted_tasks += 1

    def stop_task(self, task, state):
        record = self._record(task)
        if record.state not in (TaskState.PENDING, TaskState.RUNNING) or state not in (TaskState.FAILED, TaskState.CANCELLED):
            raise LifecycleError('Illegal task stop transition.')
        self._records[task.task_id] = replace(record, state=state)

    def require_complete(self):
        self._open()
        if self._registered_units != self.job.expected_units or self.completed_units != self.job.expected_units:
            raise ResultRejectedError('Task stream did not cover all expected logical work.')

    def finish(self):
        self._open()
        for key, record in self._records.items():
            if record.state in (TaskState.PENDING, TaskState.RUNNING):
                self._records[key] = replace(record, state=TaskState.CANCELLED)
        self._closed = True
