"""Typed failures for the isolated numerical foundation, never Legacy failures."""


class NumericalContractError(ValueError):
    """Base class for invalid M6 numerical data."""


class InvalidSamplingAddress(NumericalContractError):
    """A sampling coordinate is not a native unsigned 64-bit integer."""


class InvalidWorkloadConfiguration(NumericalContractError):
    """A workload or versioned descriptor is malformed or unsupported."""


class InvalidTaskInterval(NumericalContractError):
    """A task violates pixel bounds or the canonical sample-block policy."""


class InvalidRadiancePayload(NumericalContractError):
    """A result has invalid shape, precision, counts, or task binding."""


class NonFiniteRadiance(InvalidRadiancePayload):
    """A radiance component is NaN or infinite."""


class WorkloadMismatch(NumericalContractError):
    """A result belongs to another numerical workload."""


class CoverageError(NumericalContractError):
    """Authorized contributions overlap, repeat, or lack required coverage."""


class AccumulationOverflow(NumericalContractError):
    """Finite inputs cannot be reduced to finite binary64 radiance."""
