"""Normalized world-space rays: P(t) = origin + t * direction, t in world units.

Local directions transformed by a later intersection consumer must NOT be
renormalized or reconstructed through these normalized-world-ray constructors.
"""

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import RenderValidationError, require_int


def _float_array(value: object, shape: tuple[int, ...], label: str) -> NDArray[np.float64]:
    """Validate real numeric shape; callers check finiteness/interval semantics."""
    try:
        source = np.asarray(value)
        if source.dtype.kind not in "iuf" or source.shape != shape:
            raise RenderValidationError(f"{label} must be real numeric data with shape {shape}.")
        with np.errstate(over="ignore", invalid="ignore"):
            return np.asarray(source, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise RenderValidationError(f"Invalid numeric data for {label}; expected shape {shape}.") from error


def _readonly(array: NDArray) -> NDArray:
    """One owned bytes copy, C-contiguous views; no writable ndarray owner/alias.

    Unlike just clearing WRITEABLE on an owning ndarray, immutable bytes backing
    also prevents callers from re-enabling writing on the exposed base chain.
    """
    return np.frombuffer(array.tobytes(order="C"), dtype=array.dtype).reshape(array.shape)


def _validate_rays(origins: NDArray, directions: NDArray, t_min: NDArray, t_max: NDArray) -> None:
    if not np.isfinite(origins).all() or not np.isfinite(directions).all():
        raise RenderValidationError("Ray origins and directions must be finite.")
    with np.errstate(over="ignore", invalid="ignore"):
        lengths = np.linalg.norm(directions, axis=-1)
    if not np.all(np.isclose(lengths, 1.0, rtol=1e-12, atol=1e-12)):
        raise RenderValidationError("World-space ray directions must be normalized (tolerance 2e-12).")
    if not np.isfinite(t_min).all() or np.any(t_min < 0):
        raise RenderValidationError("Ray t_min must be finite and nonnegative.")
    if not np.all(np.isfinite(t_max) | np.isposinf(t_max)) or np.any(t_max <= t_min):
        raise RenderValidationError("Ray t_max must exceed t_min; only positive infinity is allowed.")


@dataclass(frozen=True, slots=True, eq=False)
class Ray:
    """One normalized world ray; independent immutable float64 vector storage."""

    origin: NDArray[np.float64]
    direction: NDArray[np.float64]
    t_min: float
    t_max: float

    def __post_init__(self) -> None:
        origin = _float_array(self.origin, (3,), "Ray origin")
        direction = _float_array(self.direction, (3,), "Ray direction")
        near = _float_array(self.t_min, (), "Ray t_min")
        far = _float_array(self.t_max, (), "Ray t_max")
        _validate_rays(origin, direction, near, far)
        object.__setattr__(self, "origin", _readonly(origin))
        object.__setattr__(self, "direction", _readonly(direction))
        object.__setattr__(self, "t_min", float(near))
        object.__setattr__(self, "t_max", float(far))


@dataclass(frozen=True, slots=True, eq=False)
class RayBatch:
    """N normalized world rays with row-major global pixel identities.

    pixel_count is the viewport's width*height, required to validate the upper
    pixel-index bound without storing RenderConfig or renderer-specific state.
    N may be zero or a partial strip; arrays are independent immutable C-order
    copies, with no writable aliases retained from constructor arguments.
    """

    origins: NDArray[np.float64]
    directions: NDArray[np.float64]
    t_min: NDArray[np.float64]
    t_max: NDArray[np.float64]
    pixel_indices: NDArray[np.int64]
    pixel_count: int = field(kw_only=True)

    def __post_init__(self) -> None:
        count = require_int(self.pixel_count, "pixel_count", 1, RenderValidationError)
        if count > np.iinfo(np.int64).max:
            raise RenderValidationError("pixel_count exceeds the int64 pixel identity range.")
        try:
            indices = np.asarray(self.pixel_indices)
        except (TypeError, ValueError, OverflowError) as error:
            raise RenderValidationError("Invalid RayBatch pixel_indices.") from error
        if indices.ndim != 1 or indices.dtype.kind not in "iu":
            raise RenderValidationError("RayBatch pixel_indices must be a 1D integer array.")
        if np.any(indices < 0) or np.any(indices >= count):
            raise RenderValidationError("RayBatch pixel_indices must be in [0, pixel_count).")
        n = len(indices)
        arrays = (
            _float_array(self.origins, (n, 3), "RayBatch origins"),
            _float_array(self.directions, (n, 3), "RayBatch directions"),
            _float_array(self.t_min, (n,), "RayBatch t_min"),
            _float_array(self.t_max, (n,), "RayBatch t_max"),
        )
        _validate_rays(*arrays)
        for label, array in zip(("origins", "directions", "t_min", "t_max"), arrays, strict=True):
            object.__setattr__(self, label, _readonly(array))
        object.__setattr__(self, "pixel_indices", _readonly(np.asarray(indices, dtype=np.int64)))

    def __len__(self) -> int:
        return len(self.pixel_indices)

    def ray(self, index: int) -> Ray:
        """Extract one scalar ray for numerical/reference intersection consumers."""
        require_int(index, "RayBatch ray index", error=RenderValidationError)
        if index >= len(self):
            raise RenderValidationError("RayBatch ray index is out of range.")
        return Ray(self.origins[index], self.directions[index], self.t_min[index], self.t_max[index])
