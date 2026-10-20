"""Camera instances with scale-free poses and finite/infinite projection metadata."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common._metadata import extensions, index, name, number, object_data, optional, retained
from drt_backend.raytracing.common.errors import MetadataValidationError, UnsupportedFeatureError
from drt_backend.raytracing.scene.scene import Scene
from drt_backend.raytracing.scene.transforms import numeric_array, validate_affine_matrix


@dataclass(frozen=True, slots=True)
class PerspectiveData:
    yfov: float
    znear: float
    zfar: float | None
    aspect_ratio: float | None


@dataclass(frozen=True, slots=True)
class OrthographicData:
    xmag: float
    ymag: float
    znear: float
    zfar: float


@dataclass(frozen=True, slots=True)
class CameraData:
    camera_index: int
    node_index: int
    name: str | None
    projection_type: str
    world_matrix: NDArray[np.float64]
    camera_pose: NDArray[np.float64]
    position: NDArray[np.float64]
    orientation: NDArray[np.float64]
    forward: NDArray[np.float64]
    up: NDArray[np.float64]
    perspective: PerspectiveData | None
    orthographic: OrthographicData | None
    source: Mapping[str, Any]
    extensions: Mapping[str, Any]


def rigid_orientation(world: NDArray[np.float64]) -> NDArray[np.float64]:
    """Normalize columns under a positive-scale convention; reject shear/reflections.

    Columns must be nonzero and orthogonal, with normalized determinant +1.
    Max-component pre-scaling avoids norm overflow/underflow. No SVD silently
    approximates a sheared pose, and singular orientation is never invented.
    """
    matrix = validate_affine_matrix(world)
    columns = matrix[:3, :3]
    magnitudes = np.max(np.abs(columns), axis=0)
    if np.any(magnitudes == 0):
        raise UnsupportedFeatureError("Singular camera/light orientation is not supported.")
    axes = columns / magnitudes
    axes = axes / np.linalg.norm(axes, axis=0)
    if not np.allclose(axes.T @ axes, np.eye(3), rtol=1e-6, atol=1e-6):
        raise UnsupportedFeatureError("Sheared camera/light orientation is not supported.")
    if not np.isclose(np.linalg.det(axes), 1.0, rtol=1e-6, atol=1e-6):
        raise UnsupportedFeatureError("Reflected camera/light orientation is not supported.")
    return numeric_array(axes, (3, 3), "Camera/light orientation")


def _projection(source: Mapping[str, Any]) -> tuple[PerspectiveData | None, OrthographicData | None]:
    kind = source.get("type")
    if not isinstance(kind, str) or kind not in {"perspective", "orthographic"}:
        raise MetadataValidationError("Camera type must be perspective or orthographic.")
    parameters = object_data(source.get(kind), f"Camera {kind}")
    other = "orthographic" if kind == "perspective" else "perspective"
    if other in source:
        raise MetadataValidationError("Camera projection blocks are mutually exclusive.")
    near = number(parameters.get("znear"), "Camera znear", 0, exclusive_minimum=kind == "perspective")
    far_value = optional(parameters, "zfar")
    far = number(far_value, "Camera zfar", near, exclusive_minimum=True) if far_value is not None else None
    if kind == "perspective":
        yfov = number(parameters.get("yfov"), "Camera yfov", 0, exclusive_minimum=True)
        if yfov >= math.pi:
            raise MetadataValidationError("Perspective yfov must be less than pi radians.")
        aspect_value = optional(parameters, "aspectRatio")
        aspect = number(aspect_value, "Camera aspectRatio", 0, exclusive_minimum=True) if aspect_value is not None else None
        return PerspectiveData(yfov, near, far, aspect), None
    if far is None:
        raise MetadataValidationError("Orthographic camera requires zfar.")
    xmag = number(parameters.get("xmag"), "Camera xmag")
    ymag = number(parameters.get("ymag"), "Camera ymag")
    if xmag == 0 or ymag == 0:
        raise MetadataValidationError("Orthographic magnifications cannot be zero.")
    return None, OrthographicData(xmag, ymag, near, far)


def camera_instances(scene: Scene, sources: Sequence[Mapping[str, Any]]) -> tuple[CameraData, ...]:
    projections = tuple(_projection(source) for source in sources)
    associations = tuple((name(source), extensions(source), retained(source)) for source in sources)
    result = []
    for node in scene.nodes.values():
        if node.camera_index is None:
            continue
        camera_index = index(node.camera_index, len(sources), "Camera index")
        perspective, orthographic = projections[camera_index]
        orientation = rigid_orientation(node.world_matrix)
        position = node.world_matrix[:3, 3]
        pose = np.eye(4)
        pose[:3, :3], pose[:3, 3] = orientation, position
        source_name, source_extensions, source = associations[camera_index]
        result.append(CameraData(
            camera_index, node.node_index, source_name, "perspective" if perspective else "orthographic",
            node.world_matrix, numeric_array(pose, (4, 4), "Camera pose"), position, orientation,
            numeric_array(-orientation[:, 2], (3,), "Camera forward"), orientation[:, 1],
            perspective, orthographic, source, source_extensions,
        ))
    return tuple(result)
