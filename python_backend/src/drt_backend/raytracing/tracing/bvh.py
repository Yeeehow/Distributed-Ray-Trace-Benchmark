"""Deterministic mesh-local BLAS/world-instance TLAS and scalar traversal."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, require_int
from drt_backend.raytracing.tracing.intersection import HitRecord, LocalRay, PreparedInstance, intersect_triangle, transform_ray
from drt_backend.raytracing.model.rays import Ray
from drt_backend.raytracing.scene.scene import MeshDefinition, Scene
from drt_backend.raytracing.scene.transforms import numeric_array


def _freeze(value: object, dtype: object, shape: tuple[int, ...] | None = None) -> NDArray:
    array = np.asarray(value, dtype=dtype)
    if shape is not None:
        array = array.reshape(shape)
    return np.frombuffer(array.tobytes(order="C"), dtype=array.dtype).reshape(array.shape)


@dataclass(frozen=True, slots=True, eq=False)
class _BVH:
    """Root 0, or no nodes. Internal: count=0/start=-1, two valid children.

    Leaf: count>0/start>=0, both children=-1. References form disjoint spans.
    All indices/counts/references are int64; bounds are float64 (node_count,3).
    Obtain these immutable buffers through the builder, not manual assembly.
    """

    bounds_min: NDArray[np.float64]
    bounds_max: NDArray[np.float64]
    left_child: NDArray[np.int64]
    right_child: NDArray[np.int64]
    leaf_start: NDArray[np.int64]
    leaf_count: NDArray[np.int64]
    references: NDArray[np.int64]


@dataclass(frozen=True, slots=True, eq=False)
class _MeshBLAS:
    definition: MeshDefinition
    bvh: _BVH
    triangle_primitive_indices: NDArray[np.int64]
    triangle_indices: NDArray[np.int64]

    @property
    def mesh_index(self) -> int:
        return self.definition.mesh_index


@dataclass(frozen=True, slots=True, eq=False)
class SceneAcceleration:
    """Build with build_acceleration(); scene owns the shared source geometry.

    BLAS keys are stable mesh indices; prepared-instance keys are original scene
    tuple ordinals. Each mapping owns a private read-only copy of its association.
    """

    scene: Scene
    blas_by_mesh: Mapping[int, _MeshBLAS]
    tlas: _BVH
    prepared_instances: Mapping[int, PreparedInstance]

    def __post_init__(self) -> None:
        object.__setattr__(self, "blas_by_mesh", MappingProxyType(dict(self.blas_by_mesh)))
        object.__setattr__(self, "prepared_instances", MappingProxyType(dict(self.prepared_instances)))


def _build_bvh(lower: NDArray, upper: NDArray, reference_ids: NDArray, leaf_size: int) -> _BVH:
    count = len(reference_ids)
    limit = min(np.iinfo(np.int64).max, np.iinfo(np.intp).max)
    if count > (limit + 1) // 2:
        raise GeometryError("BVH worst-case node count exceeds the signed index/address range.")
    # Half-sums and half-extents avoid overflow for finite large coordinates.
    centroids = lower * 0.5 + upper * 0.5
    minimum, maximum, left, right, starts, counts, ordered = [], [], [], [], [], [], []

    def build(items: NDArray) -> int:
        node = len(left)
        minimum.append(np.min(lower[items], axis=0))
        maximum.append(np.max(upper[items], axis=0))
        left.append(-1)
        right.append(-1)
        starts.append(-1)
        counts.append(0)
        if len(items) <= leaf_size:
            starts[node], counts[node] = len(ordered), len(items)
            ordered.extend(reference_ids[items].tolist())
        else:
            half_extent = np.max(centroids[items], axis=0) * 0.5 - np.min(centroids[items], axis=0) * 0.5
            axis = int(np.argmax(half_extent))  # First/lowest axis wins equal extents.
            order = np.lexsort((reference_ids[items], centroids[items, axis]))
            sorted_items = items[order]
            midpoint = len(items) // 2
            left[node] = build(sorted_items[:midpoint])
            right[node] = build(sorted_items[midpoint:])
        return node

    if count:
        build(np.arange(count, dtype=np.int64))
    return _BVH(_freeze(minimum, np.float64, (-1, 3)), _freeze(maximum, np.float64, (-1, 3)),
                _freeze(left, np.int64), _freeze(right, np.int64), _freeze(starts, np.int64),
                _freeze(counts, np.int64), _freeze(ordered, np.int64))


def _build_blas(definition: MeshDefinition, leaf_size: int) -> _MeshBLAS | None:
    lowers, uppers, primitive_ids, triangle_ids = [], [], [], []
    for ordinal, primitive in enumerate(definition.primitives):
        positions, indices = np.asarray(primitive.positions), np.asarray(primitive.indices)
        if (primitive.mode != 4 or positions.ndim != 2 or positions.shape[1] != 3
                or positions.dtype.kind not in "iuf" or not np.isfinite(positions).all()
                or indices.ndim != 1 or indices.dtype.kind not in "iu" or len(indices) % 3):
            raise GeometryError("BLAS requires finite indexed TRIANGLES geometry.")
        if np.any(indices < 0) or np.any(indices >= len(positions)):
            raise GeometryError("BLAS triangle vertex index is out of range.")
        if not len(indices):
            continue
        # Temporary local float64 gathering only, never retained or transformed.
        vertices = np.asarray(positions, dtype=np.float64)[primitive.triangle_indices]
        count = len(vertices)
        lowers.append(np.min(vertices, axis=1))
        uppers.append(np.max(vertices, axis=1))
        primitive_ids.append(np.full(count, ordinal, dtype=np.int64))
        triangle_ids.append(np.arange(count, dtype=np.int64))
    if not triangle_ids:
        return None
    lower, upper = np.concatenate(lowers), np.concatenate(uppers)
    references = np.arange(len(lower), dtype=np.int64)
    return _MeshBLAS(definition, _build_bvh(lower, upper, references, leaf_size),
                     _freeze(np.concatenate(primitive_ids), np.int64),
                     _freeze(np.concatenate(triangle_ids), np.int64))


def build_acceleration(scene: Scene, *, blas_leaf_size: int = 4, tlas_leaf_size: int = 2) -> SceneAcceleration:
    """One BLAS per nonempty shared definition; one cached inverse per instance.

    Stable longest-centroid-axis median splits; thresholds are provisional explicit
    configuration, not measured optima. Singular renderable instances fail here.
    """
    require_int(blas_leaf_size, "blas_leaf_size", 1, RenderValidationError)
    require_int(tlas_leaf_size, "tlas_leaf_size", 1, RenderValidationError)
    blas_by_mesh = {}
    for mesh_index, definition in sorted(scene.mesh_definitions.items()):
        if mesh_index != definition.mesh_index:
            raise GeometryError("Scene mesh key does not match its shared definition.")
        blas = _build_blas(definition, blas_leaf_size)
        if blas is not None:
            blas_by_mesh[mesh_index] = blas
    prepared, lower, upper, instance_ids = {}, [], [], []
    for index, instance in enumerate(scene.mesh_instances):
        if scene.mesh_definitions.get(instance.mesh_index) is not instance.definition:
            raise GeometryError("Instance must reference the Scene's shared MeshDefinition.")
        if instance.mesh_index not in blas_by_mesh:
            continue
        prepared[index] = PreparedInstance(index, instance)
        if instance.world_bounds.is_empty:
            raise GeometryError("Renderable instance cannot have empty world bounds.")
        minimum = numeric_array(instance.world_bounds.minimum, (3,), "TLAS instance minimum")
        maximum = numeric_array(instance.world_bounds.maximum, (3,), "TLAS instance maximum")
        if np.any(minimum > maximum):
            raise GeometryError("TLAS instance bounds are reversed.")
        lower.append(minimum)
        upper.append(maximum)
        instance_ids.append(index)
    tlas = _build_bvh(np.asarray(lower, dtype=np.float64).reshape(-1, 3),
                      np.asarray(upper, dtype=np.float64).reshape(-1, 3),
                      np.asarray(instance_ids, dtype=np.int64), tlas_leaf_size)
    return SceneAcceleration(scene, blas_by_mesh, tlas, prepared)


def _ray_aabb(ray: Ray | LocalRay, minimum: object, maximum: object) -> tuple[float, float] | None:
    """Conservative inclusive slabs for world or unnormalized local rays.

    Finite bounds expand by one representable value outward. Subtraction and
    division endpoints each expand by one nextafter step, enclosing rounding and
    finite-input overflow/underflow. No absolute scene-unit epsilon is applied.
    Zero (including signed zero) is tested explicitly without reciprocal/NaN tricks.
    """
    if not isinstance(ray, (Ray, LocalRay)):
        raise RenderValidationError("Ray/AABB requires Ray or LocalRay.")
    lower = numeric_array(minimum, (3,), "BVH minimum")
    upper = numeric_array(maximum, (3,), "BVH maximum")
    if np.any(lower > upper):
        raise RenderValidationError("BVH minimum exceeds maximum.")
    enter, exit = ray.t_min, ray.t_max
    with np.errstate(over="ignore", under="ignore", invalid="raise", divide="raise"):
        for axis in range(3):
            low = np.nextafter(lower[axis], -np.inf)
            high = np.nextafter(upper[axis], np.inf)
            origin, direction = ray.origin[axis], ray.direction[axis]
            if direction == 0:
                if origin < low or origin > high:
                    return None
                continue
            low_delta = np.nextafter(low - origin, -np.inf)
            high_delta = np.nextafter(high - origin, np.inf)
            if direction > 0:
                axis_enter = np.nextafter(low_delta / direction, -np.inf)
                axis_exit = np.nextafter(high_delta / direction, np.inf)
            else:
                axis_enter = np.nextafter(high_delta / direction, -np.inf)
                axis_exit = np.nextafter(low_delta / direction, np.inf)
            enter, exit = max(enter, float(axis_enter)), min(exit, float(axis_exit))
            if enter > exit:
                return None
    return enter, exit


def _leaf_references(bvh: _BVH, ray: Ray | LocalRay) -> Iterator[int]:
    if not len(bvh.left_child):
        return
    stack = [0]
    while stack:
        node = stack.pop()
        if _ray_aabb(ray, bvh.bounds_min[node], bvh.bounds_max[node]) is None:
            continue
        count = int(bvh.leaf_count[node])
        if count:
            start = int(bvh.leaf_start[node])
            for reference in bvh.references[start:start + count]:
                yield int(reference)
        else:
            stack.append(int(bvh.right_child[node]))
            stack.append(int(bvh.left_child[node]))


def trace_closest(acceleration: SceneAcceleration, ray: Ray) -> HitRecord | None:
    """Iterative TLAS/BLAS traversal; reuse the accepted M3.1 scalar kernel.

    Keep the original ray interval throughout, with spatial pruning only. This
    retains slightly farther identity winners in the global-minimum eight-ULP
    band. Temporary HitRecords are bounded by candidate scene triangle count.
    """
    if not isinstance(ray, Ray):
        raise RenderValidationError("Scene BVH traversal requires an M3.0 world Ray.")
    hits: list[HitRecord] = []
    for instance_index in _leaf_references(acceleration.tlas, ray):
        prepared = acceleration.prepared_instances[instance_index]
        local = transform_ray(ray, prepared)
        blas = acceleration.blas_by_mesh[prepared.instance.mesh_index]
        for reference in _leaf_references(blas.bvh, local):
            primitive_index = int(blas.triangle_primitive_indices[reference])
            triangle_index = int(blas.triangle_indices[reference])
            primitive = blas.definition.primitives[primitive_index]
            vertices = primitive.positions[primitive.triangle_indices[triangle_index]]
            hit = intersect_triangle(local, vertices, instance_index=instance_index,
                                     primitive_index=primitive_index, triangle_index=triangle_index)
            if hit is not None:
                hits.append(hit)
    if not hits:
        return None
    minimum = min(hit.t for hit in hits)
    tied = (hit for hit in hits if abs(hit.t - minimum) <= 8 * max(math.ulp(hit.t), math.ulp(minimum)))
    return min(tied, key=lambda hit: (hit.instance_index, hit.primitive_index, hit.triangle_index))
