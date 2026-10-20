"""Local leases and transport-neutral protocols; no numerical/GUI imports."""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Event, RLock
from typing import Protocol

from .errors import CancellationRequested
from .models import JobDefinition, LifecycleObservation, ProgressObservation, TaskDefinition, TaskResult


class CancellationToken:
    """Request is intent. The scheduler confirms stop only after release.

    commit() linearizes acceptance/finalization against request(). A request
    arriving during an already-started final commit waits; completion may win.
    Neither the token nor its lock preempts a blocked numerical operation.
    """
    def __init__(self):
        self._requested = Event()
        self._gate = RLock()

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def request(self):
        with self._gate:
            self._requested.set()

    def checkpoint(self):
        if self.requested:
            raise CancellationRequested('Cooperative cancellation requested.')

    def commit(self, operation: Callable):
        with self._gate:
            self.checkpoint()
            return operation()


@dataclass(slots=True)
class ExecutionContext:
    job: JobDefinition
    cancellation: CancellationToken
    resources: object = None  # Local-only adapter lease; never a task descriptor.


class WorkloadAdapter(Protocol):
    def prepare(self, context: ExecutionContext) -> None: ...
    def enumerate_tasks(self, job: JobDefinition) -> Iterable[TaskDefinition]: ...
    def execute(self, task: TaskDefinition, context: ExecutionContext) -> object: ...
    def validate_and_accept(self, task: TaskDefinition, result: TaskResult, context: ExecutionContext) -> None: ...
    def finalize(self, context: ExecutionContext) -> object: ...
    def release(self, context: ExecutionContext) -> None: ...


class TaskExecutor(Protocol):
    def execute(self, task: TaskDefinition, context: ExecutionContext) -> TaskResult: ...


class Observer(Protocol):
    def __call__(self, observation: LifecycleObservation | ProgressObservation) -> None: ...
