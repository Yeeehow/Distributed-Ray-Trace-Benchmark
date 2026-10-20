"""World-space flat geometric normals and deterministic strip-based RGB8 output."""

import math

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.tracing.bvh import SceneAcceleration, trace_closest
from drt_backend.raytracing.model.camera import CameraData
from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, SingularTransformError
from drt_backend.raytracing.tracing.intersection import HitRecord
from drt_backend.raytracing.tracing.primary_rays import generate_primary_rays, iter_strips
from drt_backend.raytracing.model.render_config import RenderConfig
from drt_backend.raytracing.scene.transforms import numeric_array


def _unit(vector: NDArray[np.float64], label: str) -> NDArray[np.float64]:
    if not np.isfinite(vector).all():
        raise GeometryError(f"{label} must be finite.")
    scale = float(np.max(np.abs(vector)))
    if scale == 0:
        raise GeometryError(f"{label} must have nonzero length.")
    scaled = vector / scale
    return scaled / math.hypot(*scaled)


def geometric_normal_world(acceleration: SceneAcceleration, hit: HitRecord) -> NDArray[np.float64]:
    """Resolve the indexed winding and apply the cached inverse transpose.

    Returns independent bytes-backed read-only float64 (3,) data. No vertex NORMAL,
    material, camera orientation, orientation_sign factor or front-face flip is used.
    """
    if not isinstance(hit, HitRecord):
        raise RenderValidationError("Normal reconstruction requires a HitRecord.")
    if hit.instance_index >= len(acceleration.scene.mesh_instances):
        raise GeometryError("Normal hit instance_index is out of range.")
    instance = acceleration.scene.mesh_instances[hit.instance_index]
    if instance.is_singular:
        raise SingularTransformError("Geometric normals require an invertible instance transform.")
    prepared = acceleration.prepared_instances.get(hit.instance_index)
    if prepared is None or prepared.instance is not instance:
        raise GeometryError("Normal hit has no matching prepared renderable instance.")
    if hit.primitive_index >= len(instance.definition.primitives):
        raise GeometryError("Normal hit primitive_index is out of range.")
    primitive = instance.definition.primitives[hit.primitive_index]
    triangles = primitive.triangle_indices
    if hit.triangle_index >= len(triangles):
        raise GeometryError("Normal hit triangle_index is out of range.")
    indices = triangles[hit.triangle_index]
    positions = np.asarray(primitive.positions)
    if (positions.ndim != 2 or positions.shape[1] != 3 or indices.dtype.kind not in "iu"
            or np.any(indices < 0) or np.any(indices >= len(positions))):
        raise GeometryError("Normal hit refers to invalid indexed positions.")
    vertices = numeric_array(positions[indices], (3, 3), "Geometric normal triangle")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="raise"):
            e1, e2 = vertices[1] - vertices[0], vertices[2] - vertices[0]
            # Positive edge rescaling preserves the normalized cross product and
            # winding while avoiding overflow/underflow in raw edge products.
            scale1, scale2 = float(np.max(np.abs(e1))), float(np.max(np.abs(e2)))
            if scale1 == 0 or scale2 == 0:
                raise GeometryError("Degenerate triangle cannot produce a geometric normal.")
            a, b = e1 / scale1, e2 / scale2
            normal_raw = np.cross(a, b)
            if math.hypot(*normal_raw) <= 64 * np.finfo(np.float64).eps * math.hypot(*a) * math.hypot(*b):
                raise GeometryError("Degenerate triangle cannot produce a geometric normal.")
            local = _unit(normal_raw, "Local geometric normal")
            world = _unit(prepared.inverse_linear.T @ local, "World geometric normal")
    except FloatingPointError as error:
        raise GeometryError("Geometric normal computation exceeds float64 range.") from error
    return np.frombuffer(world.tobytes(), dtype=np.float64)


def encode_normal_rgb(normal_world: NDArray[np.float64]) -> NDArray[np.uint8]:
    """Direct diagnostic bytes: floor(clamp((n+1)/2,0,1)*255+0.5).

    Input must already be a finite unit vector (rtol=atol=1e-12). Clamping controls
    floating drift only; no normalization, gamma, exposure or color transform.
    """
    normal = numeric_array(normal_world, (3,), "RGB world normal")
    if not math.isclose(math.hypot(*normal), 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise RenderValidationError("RGB encoding requires a unit-length world normal.")
    return np.floor(np.clip((normal + 1.0) * 0.5, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def render_normal_image(
    acceleration: SceneAcceleration, camera: CameraData, config: RenderConfig,
) -> NDArray[np.uint8]:
    """One SPP, black misses, original BVH and bounded M3.0 ray strips.

    Returns a newly owned writable C-order uint8 (H,W,3) image. Only this output
    buffer and bounded per-strip/per-ray data are allocated; source data is borrowed
    read-only. The accepted strip iterator guarantees disjoint complete coverage.
    """
    image = np.empty((config.height, config.width, 3), dtype=np.uint8, order="C")
    flat = image.reshape(-1, 3)
    written = 0
    for strip in iter_strips(config):
        batch = generate_primary_rays(camera, config, strip)
        for index, pixel in enumerate(batch.pixel_indices):
            hit = trace_closest(acceleration, batch.ray(index))
            flat[int(pixel)] = (0, 0, 0) if hit is None else encode_normal_rgb(geometric_normal_world(acceleration, hit))
        written += len(batch)
    if written != config.width * config.height:
        raise RenderValidationError("Normal render did not cover the configured pixel count.")
    return image
