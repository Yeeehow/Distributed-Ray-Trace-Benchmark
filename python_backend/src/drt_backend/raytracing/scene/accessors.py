"""Dense scalar/vector accessor decoding with explicit offsets and strides."""

from typing import Any

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import AccessorDecodeError, GlbValidationError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.scene.glb_loader import GlbAsset


_DTYPES = {5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def decode_accessor(asset: GlbAsset, index: int) -> NDArray[Any]:
    """Return a read-only (count,) scalar or (count, components) vector ndarray.

    Dense views retain immutable bytes through their base objects. Normalized
    integers and implicit-zero accessors own their arrays; those are read-only too.
    Matrix and sparse accessors are explicitly unsupported, never silently flattened.
    """
    index = require_int(index, "Accessor index", error=AccessorDecodeError)
    if index >= len(asset.document.accessors):
        raise AccessorDecodeError(f"Accessor index {index} is out of range.")
    accessor = asset.document.accessors[index]
    if accessor.sparse is not None:
        raise UnsupportedFeatureError(f"Accessor {index}: sparse decoding is not implemented.")
    if not isinstance(accessor.type, str):
        raise AccessorDecodeError("Accessor type must be a SCALAR/VEC element-type string.")
    if accessor.type in {"MAT2", "MAT3", "MAT4"}:
        raise UnsupportedFeatureError("Matrix accessor column padding is outside M2.1.")
    if accessor.type not in _WIDTHS:
        raise AccessorDecodeError(f"Accessor {index}: invalid element type.")
    component = require_int(accessor.componentType, "Component type", error=AccessorDecodeError)
    if component not in _DTYPES:
        raise AccessorDecodeError(f"Accessor {index}: invalid component type {component}.")
    count = require_int(accessor.count, "Accessor count", 1, AccessorDecodeError)
    offset = require_int(accessor.byteOffset, "Accessor byteOffset", error=AccessorDecodeError)
    if type(accessor.normalized) is not bool:
        raise AccessorDecodeError("normalized must be a boolean.")
    if accessor.normalized and component not in {5120, 5121, 5122, 5123}:
        raise AccessorDecodeError("Only BYTE/UNSIGNED_BYTE/SHORT/UNSIGNED_SHORT may be normalized.")
    dtype = np.dtype(_DTYPES[component])
    width = _WIDTHS[accessor.type]
    element_size = width * dtype.itemsize
    if count > np.iinfo(np.intp).max // element_size:
        raise AccessorDecodeError("Accessor shape exceeds addressable array size.")
    shape = (count,) if accessor.type == "SCALAR" else (count, width)
    if offset % dtype.itemsize:
        raise AccessorDecodeError("Accessor byteOffset is not aligned to its component size.")
    if accessor.bufferView is None:
        if offset:
            raise AccessorDecodeError("An accessor without a BufferView cannot have a byteOffset.")
        # glTF defines missing base data as zeros, even in the absence of sparse values.
        array = np.zeros(shape, dtype=dtype)
    else:
        view_index = require_int(accessor.bufferView, "Accessor BufferView", error=AccessorDecodeError)
        try:
            buffer = asset.buffer_view(view_index)
        except GlbValidationError as error:
            raise AccessorDecodeError(f"Accessor {index}: {error}") from error
        view = asset.document.bufferViews[view_index]
        if (view.byteOffset + offset) % dtype.itemsize:
            raise AccessorDecodeError("Combined BufferView/accessor offset is misaligned.")
        stride = element_size if view.byteStride is None else view.byteStride
        if stride < element_size or stride % dtype.itemsize:
            raise AccessorDecodeError("byteStride is smaller than an element or misaligned.")
        required = offset + (count - 1) * stride + element_size
        if required > len(buffer):
            raise AccessorDecodeError(f"Accessor {index} exceeds its BufferView length.")
        strides = (stride,) if accessor.type == "SCALAR" else (stride, dtype.itemsize)
        array = np.ndarray(shape, dtype=dtype, buffer=buffer, offset=offset, strides=strides)
    if component == 5126 and not np.isfinite(array).all():
        raise AccessorDecodeError("FLOAT accessor contains NaN or infinity.")
    if accessor.normalized:
        maximum = np.iinfo(dtype).max
        array = array.astype(np.float32) / np.float32(maximum)
        if dtype.kind == "i":
            np.maximum(array, np.float32(-1), out=array)
    array.setflags(write=False)
    return array
