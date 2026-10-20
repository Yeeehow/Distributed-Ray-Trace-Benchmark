"""Typed first-scattering admission/transport failures; no numerical repair."""
from .errors import NumericalContractError


class UnsupportedTransport(NumericalContractError):
    """The resource or policy needs transport semantics outside M6.2."""


class InvalidTransport(NumericalContractError):
    """Invalid ownership, frame, interval or finite-radiance state."""
