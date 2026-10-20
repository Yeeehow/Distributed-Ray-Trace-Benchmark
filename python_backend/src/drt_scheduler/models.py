"""Immutable data-only definitions and opaque local execution results.

Identity encoding v1: ASCII JSON, sorted string keys, compact separators, native
JSON scalar types, finite floats using Python's JSON representation. Descriptor
bytes must already be canonical. Tuples encode as arrays. No pickle or live
resources occur in JobDefinition/TaskDefinition. UUIDs encode as lower-case text.
"""
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import math
from uuid import UUID

from .errors import DefinitionError


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum or value > 2**63-1:
        raise DefinitionError(f'{label} must be an integer in [{minimum}, 2**63-1].')


def _text(value, label):
    if type(value) is not str or not value or len(value) > 256:
        raise DefinitionError(f'{label} must be nonempty text of at most 256 characters.')


def canonical_data(value) -> bytes:
    def check(item):
        if type(item) in (str, int, bool, type(None)):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) in (list, tuple):
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise DefinitionError('Descriptors require finite native JSON data only.')
    try:
        check(value)
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(',', ':')).encode('ascii')
        if len(encoded) > 65_536:
            raise DefinitionError('Descriptor exceeds 65,536 bytes.')
        return encoded
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise DefinitionError(str(error)) from error


def _descriptor(value):
    if type(value) is not bytes or not value or len(value) > 65_536:
        raise DefinitionError('Descriptor must be canonical immutable JSON bytes.')
    try:
        if canonical_data(json.loads(value)) != value:
            raise DefinitionError('Descriptor is not canonically encoded.')
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DefinitionError('Descriptor is not canonical JSON.') from error


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class JobDefinition:
    run_id: UUID
    workload_id: str
    workload_version: str
    resource_identity: str
    description: bytes
    expected_units: int
    configuration_identity: str = field(init=False)
    job_identity: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.run_id, UUID):
            raise DefinitionError('run_id requires UUID.')
        for label in ('workload_id', 'workload_version', 'resource_identity'):
            _text(getattr(self, label), label)
        _integer(self.expected_units, 'expected_units', 1)
        _descriptor(self.description)
        configuration = digest(self.description)
        identity = digest(canonical_data(dict(codec=1, run_id=str(self.run_id), workload_id=self.workload_id,
            workload_version=self.workload_version, resource_identity=self.resource_identity,
            configuration_identity=configuration, expected_units=self.expected_units)))
        object.__setattr__(self, 'configuration_identity', configuration)
        object.__setattr__(self, 'job_identity', identity)


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    job: JobDefinition
    ordinal: int
    expected_units: int
    payload: bytes
    payload_identity: str = field(init=False)
    task_id: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.job, JobDefinition):
            raise DefinitionError('Task requires JobDefinition.')
        _integer(self.ordinal, 'ordinal')
        _integer(self.expected_units, 'task expected_units', 1)
        if self.expected_units > self.job.expected_units:
            raise DefinitionError('Task exceeds job units.')
        _descriptor(self.payload)
        payload_identity = digest(self.payload)
        identity = digest(canonical_data(dict(codec=1, job_identity=self.job.job_identity,
            ordinal=self.ordinal, expected_units=self.expected_units, payload_identity=payload_identity)))
        object.__setattr__(self, 'payload_identity', payload_identity)
        object.__setattr__(self, 'task_id', identity)


class ExecutionStatus(str, Enum):
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class TaskState(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    ACCEPTED = 'accepted'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class JobState(str, Enum):
    PREPARING = 'preparing'
    RUNNING = 'running'
    FINALIZING = 'finalizing'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


@dataclass(frozen=True, slots=True, eq=False)
class TaskResult:
    run_id: UUID
    task_id: str
    job_identity: str
    configuration_identity: str
    resource_identity: str
    payload_identity: str
    status: ExecutionStatus
    completed_units: int
    payload: object = None
    executor_id: str | None = None
    elapsed_seconds: float | None = None
    failure: Exception | None = None

    @classmethod
    def for_task(cls, task, status, *, payload=None, failure=None, executor_id=None, elapsed_seconds=None):
        job = task.job
        return cls(job.run_id, task.task_id, job.job_identity, job.configuration_identity,
            job.resource_identity, task.payload_identity, status,
            task.expected_units if status is ExecutionStatus.COMPLETED else 0,
            payload, executor_id, elapsed_seconds, failure)


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task: TaskDefinition
    state: TaskState
    executor_id: str | None = None
    elapsed_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class LifecycleObservation:
    run_id: UUID
    state: JobState


@dataclass(frozen=True, slots=True)
class ProgressObservation:
    run_id: UUID
    completed_units: int
    expected_units: int
    accepted_tasks: int


@dataclass(frozen=True, slots=True)
class JobTimings:
    preparation: float
    execution: float
    acceptance: float
    finalization: float
    cleanup: float
    total: float


@dataclass(frozen=True, slots=True, eq=False)
class JobOutcome:
    job: JobDefinition
    state: JobState
    completed_units: int
    tasks: tuple[TaskRecord, ...]
    timings: JobTimings
    result: object = None
    failure: Exception | None = None
    cleanup_errors: tuple[Exception, ...] = ()
    observer_errors: tuple[Exception, ...] = ()

    def raise_for_failure(self):
        if self.state is JobState.FAILED:
            raise self.failure
