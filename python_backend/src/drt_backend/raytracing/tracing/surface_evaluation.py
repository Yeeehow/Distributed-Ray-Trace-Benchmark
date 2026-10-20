"""Indexed M4.1 surface attributes, isolated from M3 geometric diagnostics."""

from dataclasses import dataclass
import math
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError
from drt_backend.raytracing.scene.geometry import PrimitiveGeometry
from drt_backend.raytracing.tracing.intersection import HitRecord
from drt_backend.raytracing.shading.material_evaluation import evaluate_base_material
from drt_backend.raytracing.rendering.normal_visualization import geometric_normal_world
from drt_backend.raytracing.model.rays import Ray
from drt_backend.raytracing.scene.scene import MeshDefinition, MeshInstance, Scene
from drt_backend.raytracing.resources.shaded_context import ShadedContext
from drt_backend.raytracing.scene.transforms import numeric_array, validate_affine_matrix


def _freeze(vector: NDArray[np.float64]) -> NDArray[np.float64]:
    if not np.isfinite(vector).all():
        raise GeometryError("Calculated surface attribute must be finite.")
    return np.frombuffer(vector.tobytes(order="C"), dtype=np.float64).reshape(vector.shape)


def _unit(vector: NDArray[np.float64]) -> NDArray[np.float64]:
    if not np.isfinite(vector).all():
        raise GeometryError("Shading normal must be finite.")
    scale = float(np.max(np.abs(vector)))
    if scale == 0:
        raise GeometryError("Transformed shading normal cannot be normalized.")
    scaled = vector / scale
    return scaled / math.hypot(*scaled)


@dataclass(frozen=True, slots=True, eq=False)
class SurfaceAttributes:
    """Obtain through evaluate_surface; source references remain borrowed.

    Only small derived vectors are owned immutable bytes-backed float64 copies.
    No error envelope, visibility, material acceptance or texture sample is implied.
    """

    scene: Scene
    hit: HitRecord
    instance: MeshInstance
    primitive: PrimitiveGeometry
    vertex_indices: tuple[int, int, int]
    barycentric_weights: tuple[float, float, float]
    position_local: NDArray[np.float64]
    position_world: NDArray[np.float64]
    position_ray_world: NDArray[np.float64]
    texcoord_0: NDArray[np.float64] | None
    geometric_normal_world: NDArray[np.float64]
    vertex_normal_local: NDArray[np.float64] | None
    shading_normal_world: NDArray[np.float64]
    normal_source: Literal["smooth", "flat_missing", "flat_cancelled", "flat_tangent"]

    @property
    def definition(self) -> MeshDefinition:
        return self.instance.definition


def evaluate_surface(context: ShadedContext, ray: Ray, hit: HitRecord) -> SurfaceAttributes:
    """Interpolate source attributes and derive the material's view-side normal.

    Geometric normals stay unflipped. Smooth normals align to their geometric
    hemisphere; exact tangency falls back to flat. Only double-sided back hits
    flip the derived shading normal. Single-sided back hits remain evaluable.
    Ray and reconstructed positions are retained independently, without imposing
    a new epsilon or claiming that a HitRecord proves ray/triangle consistency.
    """
    if not isinstance(context, ShadedContext) or not isinstance(ray, Ray):
        raise RenderValidationError("Surface evaluation requires a ShadedContext and world Ray.")
    instance, primitive, prepared = context.resolve_hit(hit)
    if not ray.t_min <= hit.t <= ray.t_max:
        raise RenderValidationError("Surface hit is outside the supplied ray interval.")
    positions, indices = np.asarray(primitive.positions), np.asarray(primitive.indices)
    if (primitive.mode != 4 or positions.ndim != 2 or positions.shape[1] != 3
            or positions.dtype.kind not in "iuf" or indices.ndim != 1
            or indices.dtype.kind not in "iu" or len(indices) % 3):
        raise GeometryError("Surface evaluation requires indexed TRIANGLES geometry.")
    if hit.triangle_index >= len(indices) // 3:
        raise GeometryError("Surface hit triangle_index is out of range.")
    selected = primitive.triangle_indices[hit.triangle_index]
    if np.any(selected < 0) or np.any(selected >= len(positions)):
        raise GeometryError("Surface triangle vertex index is out of range.")
    vertex_indices = tuple(int(index) for index in selected)
    vertices = numeric_array(positions[selected], (3, 3), "Surface triangle positions")
    weights = (1.0 - hit.u - hit.v, hit.u, hit.v)
    matrix = validate_affine_matrix(instance.world_matrix)
    base = evaluate_base_material(context, primitive)
    geometric = geometric_normal_world(context.acceleration, hit)

    def interpolate(attribute: object, width: int, label: str) -> NDArray[np.float64]:
        source = np.asarray(attribute)
        if source.shape != (len(positions), width):
            raise GeometryError(f"{label} shape must match the POSITION vertex count.")
        return np.asarray(weights) @ numeric_array(source[selected], (3, width), label)

    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="raise"):
            local = np.asarray(weights) @ vertices
            world = matrix[:3, :3] @ local + matrix[:3, 3]
            ray_position = ray.origin + hit.t * ray.direction
            uv = None if primitive.texcoord_0 is None else interpolate(primitive.texcoord_0, 2, "TEXCOORD_0")
            vertex_normal = None
            shading = geometric
            normal_source = "flat_missing"
            if primitive.normals is not None:
                vertex_normal = interpolate(primitive.normals, 3, "Vertex NORMAL")
                if not np.isfinite(vertex_normal).all():
                    raise GeometryError("Interpolated Vertex NORMAL must be finite.")
                normal_source = "flat_cancelled"
                if np.any(vertex_normal):
                    shading = _unit(prepared.inverse_linear.T @ vertex_normal)
                    hemisphere = float(np.dot(shading, geometric))
                    normal_source = "smooth"
                    if hemisphere < 0:
                        shading = -shading
                    elif hemisphere == 0:
                        shading, normal_source = geometric, "flat_tangent"
            if base.double_sided and not hit.front_face:
                shading = -shading
    except FloatingPointError as error:
        raise GeometryError("Surface attribute computation exceeds float64 range.") from error
    return SurfaceAttributes(
        context.scene, hit, instance, primitive, vertex_indices, weights,
        _freeze(local), _freeze(world), _freeze(ray_position),
        None if uv is None else _freeze(uv), geometric,
        None if vertex_normal is None else _freeze(vertex_normal), _freeze(shading), normal_source,
    )
