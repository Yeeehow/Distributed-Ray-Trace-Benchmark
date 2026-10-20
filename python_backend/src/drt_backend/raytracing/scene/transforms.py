"""glTF column-vector transforms in float64; no geometry arrays are modified."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
from numpy.typing import NDArray
from pygltflib import Node

from drt_backend.raytracing.common.errors import SceneValidationError, SingularTransformError


def numeric_array(value: object, shape: tuple[int, ...] | None, label: str) -> NDArray[np.float64]:
    """Validate real numeric input and return an independent read-only float64 array."""
    try:
        source = np.asarray(value)
        if source.dtype.kind not in "iuf" or (shape is not None and source.shape != shape):
            raise SceneValidationError(f"{label} must be numeric with shape {shape}.")
        with np.errstate(over="ignore", invalid="ignore"):
            array = np.array(source, dtype=np.float64, copy=True)
    except (ValueError, TypeError, OverflowError) as error:
        raise SceneValidationError(f"Invalid numeric data for {label}.") from error
    if not np.isfinite(array).all():
        raise SceneValidationError(f"{label} must contain only finite values.")
    array.setflags(write=False)
    return array


def validate_affine_matrix(value: object) -> NDArray[np.float64]:
    """Internal matrix validation; world matrices may include inherited shear."""
    matrix = numeric_array(value, (4, 4), "Affine matrix")
    if not np.array_equal(matrix[3], [0.0, 0.0, 0.0, 1.0]):
        raise SceneValidationError("Transform must be affine with last row [0,0,0,1].")
    return matrix


def node_local_matrix(node: Node | Mapping[str, object]) -> NDArray[np.float64]:
    """Read glTF XYZW TRS or a column-major 16-value matrix; they are exclusive.

    Raw metadata mappings preserve field presence/types before binding coercion.
    Quaternions within 1e-6 of unit length are normalized; others are rejected.
    Finite negative and zero scales are retained, including singular transforms.
    """
    def present(name: str) -> bool:
        return name in node if isinstance(node, Mapping) else getattr(node, name, None) is not None

    def value(name: str, default: object) -> object:
        if isinstance(node, Mapping):
            return node.get(name, default)
        result = getattr(node, name, None)
        return default if result is None else result

    if present("matrix"):
        if any(present(name) for name in ("translation", "rotation", "scale")):
            raise SceneValidationError("Node matrix and TRS properties are mutually exclusive.")
        serialized = numeric_array(value("matrix", None), (16,), "glTF matrix")
        matrix = validate_affine_matrix(serialized.reshape(4, 4, order="F"))
        columns = matrix[:3, :3]
        magnitudes = np.max(np.abs(columns), axis=0)
        nonzero = magnitudes > 0
        axes = columns[:, nonzero] / magnitudes[nonzero]
        if axes.shape[1]:
            axes = axes / np.linalg.norm(axes, axis=0)
            if not np.allclose(axes.T @ axes, np.eye(axes.shape[1]), rtol=1e-6, atol=1e-6):
                raise SceneValidationError("Explicit node matrix contains shear, not a glTF TRS transform.")
        return matrix
    translation = numeric_array(value("translation", [0, 0, 0]), (3,), "Translation")
    scale = numeric_array(value("scale", [1, 1, 1]), (3,), "Scale")
    quaternion = numeric_array(value("rotation", [0, 0, 0, 1]), (4,), "Quaternion XYZW")
    with np.errstate(over="ignore", invalid="ignore"):
        length = np.linalg.norm(quaternion)
    if not np.isfinite(length) or abs(length - 1.0) > 1e-6:
        raise SceneValidationError("Quaternion must have unit length within 1e-6.")
    x, y, z, w = quaternion / length
    rotation = np.array([
        [1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
        [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
        [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)],
    ])
    matrix = np.eye(4)
    with np.errstate(over="ignore", invalid="ignore"):
        matrix[:3, :3] = rotation * scale[np.newaxis, :]
    matrix[:3, 3] = translation
    return validate_affine_matrix(matrix)  # T @ R @ S, with column vectors.


def compose_world(parent: object, local: object) -> NDArray[np.float64]:
    """Compute parent @ local and reject non-finite products."""
    parent_matrix = validate_affine_matrix(parent)
    local_matrix = validate_affine_matrix(local)
    with np.errstate(over="ignore", invalid="ignore"):
        result = parent_matrix @ local_matrix
    return validate_affine_matrix(result)


def orientation_sign(world: object) -> int:
    """Return -1, +1 or 0 for a reflection, positive orientation or singular map."""
    matrix = validate_affine_matrix(world)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        sign, _ = np.linalg.slogdet(matrix[:3, :3])
    if not np.isfinite(sign):
        raise SceneValidationError("Transform orientation could not be determined finitely.")
    return int(sign)


def normal_matrix(world: object) -> NDArray[np.float64]:
    """Compute inverse(world[:3,:3]).T on request; never return an invalid inverse."""
    matrix = validate_affine_matrix(world)
    try:
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            inverse = np.linalg.inv(matrix[:3, :3]).T
    except np.linalg.LinAlgError as error:
        raise SingularTransformError("Normal transform requires an invertible world transform.") from error
    if not np.isfinite(inverse).all():
        raise SingularTransformError("Normal transform inverse is not finite.")
    return numeric_array(inverse, (3, 3), "Normal matrix")


def transform_normals(normals: object, world: object, *, normalize: bool = True) -> NDArray[np.float64]:
    """Apply the inverse-transpose to a (3,) normal or (N,3) batch, without mutation."""
    vectors = numeric_array(normals, None, "Normals")
    if vectors.shape != (3,) and (vectors.ndim != 2 or vectors.shape[1] != 3):
        raise SceneValidationError("Normals must have shape (3,) or (N,3).")
    with np.errstate(over="ignore", invalid="ignore"):
        result = vectors @ normal_matrix(world).T
    if not np.isfinite(result).all():
        raise SceneValidationError("Transformed normals are not finite.")
    if normalize:
        magnitudes = np.max(np.abs(result), axis=-1, keepdims=True)
        if np.any(magnitudes == 0):
            raise SceneValidationError("A zero normal cannot be normalized.")
        result = result / magnitudes
        result = result / np.linalg.norm(result, axis=-1, keepdims=True)
    return numeric_array(result, vectors.shape, "Transformed normals")
