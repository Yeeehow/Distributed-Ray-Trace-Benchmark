"""Small immutable-by-convention AABBs, with an explicit empty sentinel."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import SceneValidationError
from drt_backend.raytracing.scene.transforms import numeric_array, validate_affine_matrix


@dataclass(frozen=True, slots=True)
class AABB:
    """Empty bounds have both extrema None; invalid extrema raise rather than persist."""

    minimum: NDArray[np.float64] | None = None
    maximum: NDArray[np.float64] | None = None

    def __post_init__(self) -> None:
        if self.minimum is None and self.maximum is None:
            return
        if self.minimum is None or self.maximum is None:
            raise SceneValidationError("AABB must have both extrema or be empty.")
        minimum = numeric_array(self.minimum, (3,), "AABB minimum")
        maximum = numeric_array(self.maximum, (3,), "AABB maximum")
        if np.any(minimum > maximum):
            raise SceneValidationError("AABB minimum exceeds maximum.")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    @classmethod
    def empty(cls) -> AABB:
        return cls()

    @property
    def is_empty(self) -> bool:
        return self.minimum is None

    @classmethod
    def from_points(cls, points: object) -> AABB:
        """Use actual local positions; no accessor extrema or stored vertex copies."""
        try:
            values = np.asarray(points)
        except (TypeError, ValueError) as error:
            raise SceneValidationError("Invalid AABB points.") from error
        if values.ndim != 2 or values.shape[1] != 3 or values.dtype.kind not in "iuf":
            raise SceneValidationError("AABB points must be numeric with shape (N,3).")
        if not np.isfinite(values).all():
            raise SceneValidationError("AABB points must be finite.")
        if len(values) == 0:
            return cls.empty()
        return cls(values.min(axis=0), values.max(axis=0))

    def union(self, other: AABB) -> AABB:
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        return AABB(np.minimum(self.minimum, other.minimum), np.maximum(self.maximum, other.maximum))

    def transform(self, world: object) -> AABB:
        """Transform all eight local corners, including rotations/reflections/shear."""
        matrix = validate_affine_matrix(world)
        if self.is_empty:
            return self
        corners = np.array(list(product(*zip(self.minimum, self.maximum))), dtype=np.float64)
        with np.errstate(over="ignore", invalid="ignore"):
            transformed = corners @ matrix[:3, :3].T + matrix[:3, 3]
        return AABB.from_points(transformed)
