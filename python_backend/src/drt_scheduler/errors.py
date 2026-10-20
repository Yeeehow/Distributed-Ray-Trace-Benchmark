"""Typed contract/lifecycle errors; workload exceptions retain their own types."""


class SchedulerError(ValueError):
    pass


class DefinitionError(SchedulerError):
    pass


class ResultRejectedError(SchedulerError):
    pass


class LifecycleError(SchedulerError):
    pass


class CancellationRequested(SchedulerError):
    """Cooperative stop signal, not proof that resources are already released."""
