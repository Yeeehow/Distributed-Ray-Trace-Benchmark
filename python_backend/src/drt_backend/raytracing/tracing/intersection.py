"""Scalar, double-sided mesh-local intersections and a small-scene reference.

World rays use M3.0 Ray. LocalRay is derived instance-local data whose direction
is deliberately NOT normalized; both rays retain the same world-distance t.
"""

from dataclasses import dataclass, field
import math
from numbers import Real

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, SingularTransformError, require_int
from drt_backend.raytracing.model.rays import Ray
from drt_backend.raytracing.scene.scene import MeshInstance, Scene
from drt_backend.raytracing.scene.transforms import numeric_array, validate_affine_matrix


# Angular/relative thresholds, independently applied to area and parallelism.
_DEGENERACY_REL = 64 * np.finfo(np.float64).eps
_PARALLEL_REL = 64 * np.finfo(np.float64).eps
_TIE_ULPS = 8


def _number(value: object, label: str, *, infinite: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise RenderValidationError(f"{label} must be a real number.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise RenderValidationError(f"{label} is outside float64 range.") from error
    if not math.isfinite(result) and not (infinite and result == math.inf):
        raise RenderValidationError(f"{label} must be finite; only t_max may be positive infinity.")
    return result


def _owned_array(value: object, shape: tuple[int, ...], label: str) -> NDArray[np.float64]:
    array = numeric_array(value, shape, label)
    return np.frombuffer(array.tobytes(order="C"), dtype=np.float64).reshape(shape)


@dataclass(frozen=True, slots=True, eq=False)
class LocalRay:
    """Instance-local origin/direction, preserving the input world ray's t units.

    Direction must be finite and nonzero, with no unit-length requirement.
    Arrays have owned immutable bytes backing, independent of caller aliases.
    """

    origin: NDArray[np.float64]
    direction: NDArray[np.float64]
    t_min: float
    t_max: float

    def __post_init__(self) -> None:
        origin = _owned_array(self.origin, (3,), "Local ray origin")
        direction = _owned_array(self.direction, (3,), "Local ray direction")
        if not np.any(direction):
            raise RenderValidationError("Local ray direction must be nonzero.")
        near = _number(self.t_min, "Local ray t_min")
        far = _number(self.t_max, "Local ray t_max", infinite=True)
        if near < 0 or far <= near:
            raise RenderValidationError("Local ray interval requires 0 <= t_min < t_max.")
        for label, value in (("origin", origin), ("direction", direction), ("t_min", near), ("t_max", far)):
            object.__setattr__(self, label, value)


@dataclass(frozen=True, slots=True)
class HitRecord:
    """Triangle identity: scene instance tuple, definition primitive, local triangle.

    Vertex weights are (1-u-v, u, v). Normals/positions/materials are not stored.
    """

    t: float
    instance_index: int
    primitive_index: int
    triangle_index: int
    u: float
    v: float
    front_face: bool

    def __post_init__(self) -> None:
        for label in ("t", "u", "v"):
            object.__setattr__(self, label, _number(getattr(self, label), f"Hit {label}"))
        if self.t < 0 or self.u < 0 or self.v < 0 or self.u + self.v > 1:
            raise RenderValidationError("Hit requires t >= 0 and barycentrics inside the closed triangle.")
        for label in ("instance_index", "primitive_index", "triangle_index"):
            require_int(getattr(self, label), f"Hit {label}", error=RenderValidationError)
        if type(self.front_face) is not bool:
            raise RenderValidationError("Hit front_face must be bool.")


@dataclass(frozen=True, slots=True, eq=False)
class PreparedInstance:
    """M3-owned inverse preparation; retains the shared M2 instance/definition.

    Uses the existing lazy inverse-transpose exactly once during preparation.
    No inverse is computed per triangle, and no cache is attached to M2 objects.
    """

    instance_index: int
    instance: MeshInstance = field(repr=False)
    inverse_linear: NDArray[np.float64] = field(init=False, repr=False)
    translation: NDArray[np.float64] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        require_int(self.instance_index, "Prepared instance_index", error=RenderValidationError)
        matrix = validate_affine_matrix(self.instance.world_matrix)
        if self.instance.is_singular:
            raise SingularTransformError("Renderable instance requires an invertible world transform.")
        inverse = self.instance.normal_matrix.T
        object.__setattr__(self, "inverse_linear", _owned_array(inverse, (3, 3), "Instance inverse linear"))
        object.__setattr__(self, "translation", _owned_array(matrix[:3, 3], (3,), "Instance translation"))


def transform_ray(ray: Ray, prepared: PreparedInstance) -> LocalRay:
    """Apply inverse(A) to (o-translation) and d, without normalizing local d."""
    if not isinstance(ray, Ray):
        raise RenderValidationError("Instance conversion requires an M3.0 world Ray.")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="raise"):
            origin = prepared.inverse_linear @ (ray.origin - prepared.translation)
            direction = prepared.inverse_linear @ ray.direction
    except FloatingPointError as error:
        raise RenderValidationError("Instance-local ray computation exceeds float64 range.") from error
    return LocalRay(origin, direction, ray.t_min, ray.t_max)


def intersect_triangle(
    ray: Ray | LocalRay,
    vertices: object,
    *,
    instance_index: int = 0,
    primitive_index: int = 0,
    triangle_index: int = 0,
) -> HitRecord | None:
    """Scalar Moller-Trumbore, inclusive computed barycentric and t boundaries.

    vertices is (3,3) in the ray's space. Positive edge rescaling factors out
    triangle size without changing t or direction; u/v compensate those factors.
    Degenerate area and near-parallel incidence use distinct relative checks at
    64*float64 epsilon. No barycentric clamp, t expansion or backface cull occurs.
    Unrepresentable float64 arithmetic raises rather than inventing a hit/miss.
    """
    if not isinstance(ray, (Ray, LocalRay)):
        raise RenderValidationError("Triangle intersection requires Ray or derived LocalRay.")
    for label, value in (("instance_index", instance_index), ("primitive_index", primitive_index),
                         ("triangle_index", triangle_index)):
        require_int(value, label, error=RenderValidationError)
    points = numeric_array(vertices, (3, 3), "Triangle vertices")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="raise"):
            e1, e2 = points[1] - points[0], points[2] - points[0]
            scale1, scale2 = float(np.max(np.abs(e1))), float(np.max(np.abs(e2)))
            if scale1 == 0 or scale2 == 0:
                return None
            a, b = e1 / scale1, e2 / scale2
            normal = np.cross(a, b)
            area = math.hypot(*normal)
            if area <= _DEGENERACY_REL * math.hypot(*a) * math.hypot(*b):
                return None
            # Direction scaling is ONLY for the dimensionless incidence check.
            # The actual MT equations below retain the original local direction.
            angular_direction = ray.direction / np.max(np.abs(ray.direction))
            angular_det = float(np.dot(a, np.cross(angular_direction, b)))
            if abs(angular_det) <= _PARALLEL_REL * area * math.hypot(*angular_direction):
                return None
            p = np.cross(ray.direction, b)
            det = float(np.dot(a, p))
            if det == 0 or not math.isfinite(det):
                raise RenderValidationError("Triangle determinant exceeds float64 range.")
            delta = ray.origin - points[0]
            u = float(np.dot(delta, p) / det / scale1)
            q = np.cross(delta, a)
            v = float(np.dot(ray.direction, q) / det / scale2)
            t = float(np.dot(b, q) / det)
    except FloatingPointError as error:
        raise RenderValidationError("Triangle intersection arithmetic exceeds float64 range.") from error
    if not all(math.isfinite(value) for value in (u, v, t)):
        raise RenderValidationError("Triangle intersection produced non-finite parameters.")
    if u < 0 or v < 0 or u + v > 1 or t < ray.t_min or t > ray.t_max:
        return None
    # det > 0 iff d_local . normal_local < 0. Since d_local=A^-1 d_world,
    # its sign equals d_world . (A^-T normal_local), even for a reflection.
    # Applying orientation_sign again would invert mirrored front-side semantics.
    return HitRecord(t, instance_index, primitive_index, triangle_index, u, v, bool(det > 0))


def brute_force_closest_hit(scene: Scene, ray: Ray) -> HitRecord | None:
    """Small-fixture oracle; indexed local geometry, no BVH or world triangles.

    Tie candidates lie within 8 ULPs of the GLOBAL minimum t, with tolerance
    8*max(ulp(candidate.t), ulp(minimum)). Among them choose lexicographically by
    (instance_index, primitive_index, triangle_index). Anchoring to the global
    minimum prevents chained pairwise ties from drifting to farther hits.
    """
    if not isinstance(ray, Ray):
        raise RenderValidationError("Scene reference intersection requires an M3.0 world Ray.")
    # Prepare every renderable instance first, so singular geometry cannot be
    # silently skipped because an earlier instance already yielded a hit.
    prepared = [PreparedInstance(index, instance) for index, instance in enumerate(scene.mesh_instances)
                if any(len(primitive.indices) for primitive in instance.definition.primitives)]
    hits: list[HitRecord] = []
    for item in prepared:
        local = transform_ray(ray, item)
        for primitive_index, primitive in enumerate(item.instance.definition.primitives):
            positions, indices = np.asarray(primitive.positions), np.asarray(primitive.indices)
            if (primitive.mode != 4 or positions.ndim != 2 or positions.shape[1] != 3
                    or positions.dtype.kind not in "iuf" or not np.isfinite(positions).all()
                    or indices.ndim != 1 or indices.dtype.kind not in "iu" or len(indices) % 3):
                raise GeometryError("Reference intersection requires finite indexed TRIANGLES geometry.")
            if np.any(indices < 0) or np.any(indices >= len(positions)):
                raise GeometryError("Reference triangle vertex index is out of range.")
            for triangle_index, vertex_indices in enumerate(indices.reshape(-1, 3)):
                hit = intersect_triangle(local, positions[vertex_indices], instance_index=item.instance_index,
                                         primitive_index=primitive_index, triangle_index=triangle_index)
                if hit is not None:
                    hits.append(hit)
    if not hits:
        return None
    minimum = min(hit.t for hit in hits)
    tied = (hit for hit in hits if abs(hit.t - minimum) <= _TIE_ULPS * max(math.ulp(hit.t), math.ulp(minimum)))
    return min(tied, key=lambda hit: (hit.instance_index, hit.primitive_index, hit.triangle_index))
