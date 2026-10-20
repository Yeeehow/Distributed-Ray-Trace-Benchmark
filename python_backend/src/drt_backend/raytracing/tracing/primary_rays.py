"""Deterministic 1D strips and perspective pixel-centre world-space primary rays."""

from collections.abc import Iterator
from dataclasses import dataclass
import math

import numpy as np

from drt_backend.raytracing.common._metadata import number
from drt_backend.raytracing.model.camera import CameraData, PerspectiveData
from drt_backend.raytracing.common.errors import RenderValidationError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.model.rays import RayBatch
from drt_backend.raytracing.model.render_config import RenderConfig
from drt_backend.raytracing.scene.render_metadata import RenderMetadata
from drt_backend.raytracing.scene.transforms import numeric_array, validate_affine_matrix


@dataclass(frozen=True, slots=True)
class StripRegion:
    """Horizontal: fixed_index=y/start=x. Vertical: fixed_index=x/start=y.

    Descriptors contain only real pixels. Viewport bounds, orientation agreement
    and nominal-length limits are checked by generate_primary_rays().
    """

    orientation: str
    fixed_index: int
    start: int
    length: int

    def __post_init__(self) -> None:
        if not isinstance(self.orientation, str) or self.orientation not in ("horizontal", "vertical"):
            raise RenderValidationError("Strip orientation must be horizontal or vertical.")
        require_int(self.fixed_index, "Strip fixed_index", error=RenderValidationError)
        require_int(self.start, "Strip start", error=RenderValidationError)
        require_int(self.length, "Strip length", 1, RenderValidationError)


def _perspective(camera: CameraData) -> PerspectiveData:
    if camera.projection_type != "perspective":
        raise UnsupportedFeatureError(f"M3.0 primary rays do not support {camera.projection_type!r} projection.")
    if not isinstance(camera.perspective, PerspectiveData) or camera.orthographic is not None:
        raise RenderValidationError("Perspective CameraData requires only perspective projection data.")
    return camera.perspective


def select_camera(metadata: RenderMetadata, camera_node_index: int | None = None) -> CameraData:
    """Select by active node identity; all multi-camera scenes require a choice."""
    if camera_node_index is not None:
        require_int(camera_node_index, "camera_node_index", error=RenderValidationError)
        matches = tuple(camera for camera in metadata.cameras if camera.node_index == camera_node_index)
        if not matches:
            raise RenderValidationError(f"No active camera at node {camera_node_index}.")
        if len(matches) != 1:
            raise RenderValidationError(f"Ambiguous active camera node {camera_node_index}.")
        camera = matches[0]
    else:
        if not metadata.cameras:
            raise RenderValidationError("No active camera is available.")
        if len(metadata.cameras) != 1:
            raise RenderValidationError("Multiple active cameras require camera_node_index.")
        camera = metadata.cameras[0]
    _perspective(camera)
    return camera


def iter_strips(config: RenderConfig) -> Iterator[StripRegion]:
    """Lazy row-major horizontal or column-major vertical strip enumeration.

    The nested bounded ranges cover each axis exactly once without padding.
    No pixel array or image-sized list is constructed by this iterator.
    """
    horizontal = config.strip_orientation == "horizontal"
    fixed_count, extent = (config.height, config.width) if horizontal else (config.width, config.height)
    nominal = config.nominal_strip_length
    for fixed in range(fixed_count):
        for start in range(0, extent, nominal):
            yield StripRegion(config.strip_orientation, fixed, start, min(nominal, extent - start))


def generate_primary_rays(camera: CameraData, config: RenderConfig, strip: StripRegion) -> RayBatch:
    """Generate only this strip; camera_pose is the existing M2 scale-free pose.

    Top-left image origin, X right, Y down; local camera forward is -Z. Explicit
    camera aspect must match W/H with rel_tol=1e-9, abs_tol=1e-12. Rigid pose
    checks use M2's 1e-6 orthogonality/determinant tolerance; position agreement
    uses rtol=atol=1e-12. No pose extraction/decomposition is performed here.
    """
    if config.camera_node_index is not None and camera.node_index != config.camera_node_index:
        raise RenderValidationError("Camera node does not match RenderConfig.camera_node_index.")
    if strip.orientation != config.strip_orientation:
        raise RenderValidationError("Strip orientation does not match RenderConfig.")
    horizontal = strip.orientation == "horizontal"
    fixed_count, extent = (config.height, config.width) if horizontal else (config.width, config.height)
    if (strip.fixed_index >= fixed_count or strip.start >= extent or strip.start + strip.length > extent
            or strip.length > config.nominal_strip_length):
        raise RenderValidationError("Strip is out of viewport bounds or exceeds nominal_strip_length.")

    projection = _perspective(camera)
    # Reuse existing numeric validation for manually constructed CameraData too.
    yfov = number(projection.yfov, "Primary camera yfov", 0, exclusive_minimum=True)
    near = number(projection.znear, "Primary camera znear", 0, exclusive_minimum=True)
    far = None if projection.zfar is None else number(projection.zfar, "Primary camera zfar", near,
                                                     exclusive_minimum=True)
    if yfov >= math.pi:
        raise RenderValidationError("Primary camera yfov must be less than pi radians.")
    aspect = config.width / config.height
    if projection.aspect_ratio is not None:
        explicit = number(projection.aspect_ratio, "Primary camera aspect_ratio", 0, exclusive_minimum=True)
        if not math.isclose(aspect, explicit, rel_tol=1e-9, abs_tol=1e-12):
            raise RenderValidationError("Viewport aspect ratio does not match the camera aspect_ratio.")
        aspect = explicit
    pose = validate_affine_matrix(camera.camera_pose)
    position = numeric_array(camera.position, (3,), "Primary camera position")
    rotation = pose[:3, :3]
    with np.errstate(over="ignore", invalid="ignore"):
        rigid = (np.allclose(rotation.T @ rotation, np.eye(3), rtol=1e-6, atol=1e-6)
                 and np.isclose(np.linalg.det(rotation), 1.0, rtol=1e-6, atol=1e-6))
    if not rigid:
        raise RenderValidationError("Primary camera_pose must contain a proper scale-free rotation.")
    if not np.allclose(pose[:3, 3], position, rtol=1e-12, atol=1e-12):
        raise RenderValidationError("Primary camera_pose translation disagrees with camera.position.")

    offsets = np.arange(strip.start, strip.start + strip.length, dtype=np.int64)
    x, y = (offsets, strip.fixed_index) if horizontal else (strip.fixed_index, offsets)
    pixel_indices = np.asarray(y * config.width + x, dtype=np.int64)
    u = 2.0 * (x + 0.5) / config.width - 1.0
    v = 1.0 - 2.0 * (y + 0.5) / config.height
    scale = math.tan(yfov / 2.0)
    directions_camera = np.empty((strip.length, 3), dtype=np.float64)
    directions_camera[:, 0] = u * aspect * scale
    directions_camera[:, 1] = v * scale
    directions_camera[:, 2] = -1.0
    directions_camera /= np.linalg.norm(directions_camera, axis=1, keepdims=True)
    directions_world = directions_camera @ rotation.T
    directions_world /= np.linalg.norm(directions_world, axis=1, keepdims=True)
    # Near/far are camera-space planes; normalized world-ray t is distance.
    forward_components = -directions_camera[:, 2]
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        t_min = near / forward_components
        t_max = np.full(strip.length, np.inf) if far is None else far / forward_components
    if not np.isfinite(t_min).all() or (far is not None and not np.isfinite(t_max).all()):
        raise RenderValidationError("Primary ray clipping distances overflow float64.")
    return RayBatch(np.broadcast_to(pose[:3, 3], (strip.length, 3)), directions_world,
                    t_min, t_max, pixel_indices, pixel_count=config.width * config.height)
