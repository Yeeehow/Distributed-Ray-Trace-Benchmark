"""Local-space TRIANGLES geometry, with original mesh/primitive associations."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.scene.accessors import decode_accessor
from drt_backend.raytracing.common.errors import GeometryError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.scene.glb_loader import GlbAsset


@dataclass(frozen=True, slots=True)
class PrimitiveGeometry:
    mesh_index: int
    primitive_index: int
    positions: NDArray[np.float32]
    normals: NDArray[np.float32] | None
    texcoord_0: NDArray[np.float32] | None
    tangents: NDArray[np.float32] | None
    indices: NDArray[Any]
    mode: int
    material_index: int | None
    index_accessor: int | None
    attribute_accessors: Mapping[str, int]

    @property
    def triangle_indices(self) -> NDArray[Any]:
        """Read-only (triangle_count, 3) view; no winding/transform conversion."""
        return self.indices.reshape(-1, 3)


@dataclass(frozen=True, slots=True)
class GeometryStats:
    meshes: int
    primitives: int
    triangles: int
    primitive_vertices: int
    indexed_primitives: int
    with_normals: int
    with_texcoord_0: int
    with_tangents: int


def extract_primitive(asset: GlbAsset, mesh_index: int, primitive_index: int) -> PrimitiveGeometry:
    """Decode one mesh primitive, without nodes, instancing, transforms or shading.

    POSITION is mandatory; NORMAL/UV/TANGENT may be absent. Non-indexed TRIANGLES
    receive sequential uint32 indices, with index_accessor=None recording this fact.
    Other topologies, compressed primitives and morph targets are explicitly deferred.
    """
    mesh_index = require_int(mesh_index, "Mesh index", error=GeometryError)
    if mesh_index >= len(asset.document.meshes):
        raise GeometryError("Mesh index is out of range.")
    mesh = asset.document.meshes[mesh_index]
    primitive_index = require_int(primitive_index, "Primitive index", error=GeometryError)
    if primitive_index >= len(mesh.primitives):
        raise GeometryError("Primitive index is out of range.")
    primitive = mesh.primitives[primitive_index]
    mode = 4 if primitive.mode is None else require_int(primitive.mode, "Primitive mode", error=GeometryError)
    if mode > 6:
        raise GeometryError("Primitive mode is invalid.")
    if mode != 4:
        raise UnsupportedFeatureError("Only TRIANGLES primitives are extracted in M2.1.")
    if primitive.targets:
        raise UnsupportedFeatureError("Morph targets are not applied by M2.1.")
    if "KHR_draco_mesh_compression" in (primitive.extensions or {}):
        raise UnsupportedFeatureError("Draco-compressed primitives are not decoded.")
    attributes = {key: value for key, value in vars(primitive.attributes).items() if value is not None}
    if "POSITION" not in attributes:
        raise GeometryError("Primitive is missing POSITION.")
    for name, index in attributes.items():
        require_int(index, f"Attribute {name} accessor", error=GeometryError)
        if index >= len(asset.document.accessors):
            raise GeometryError(f"Attribute {name} refers to a missing accessor.")

    def attribute(name: str, width: int, required: bool = False) -> NDArray[np.float32] | None:
        index = attributes.get(name)
        if index is None:
            if required:
                raise GeometryError(f"Missing {name}.")
            return None
        accessor = asset.document.accessors[index]
        is_float = accessor.componentType == 5126 and not accessor.normalized
        is_uv_integer = name == "TEXCOORD_0" and accessor.componentType in {5121, 5123} and accessor.normalized
        if accessor.type != f"VEC{width}" or not (is_float or is_uv_integer):
            raise GeometryError(f"{name} has an unsupported core glTF attribute format.")
        array = decode_accessor(asset, index)
        if accessor.bufferView is not None:
            view = asset.document.bufferViews[accessor.bufferView]
            if (view.byteOffset + accessor.byteOffset) % 4 or array.strides[0] % 4:
                raise GeometryError(f"{name} vertex data must be aligned to 4-byte boundaries.")
            if view.target not in {None, 34962}:
                raise GeometryError(f"{name} refers to an indices-only BufferView.")
        return array

    positions = attribute("POSITION", 3, required=True)
    assert positions is not None
    vertex_count = len(positions)
    normals = attribute("NORMAL", 3)
    uv = attribute("TEXCOORD_0", 2)
    tangents = attribute("TANGENT", 4)
    for name, index in attributes.items():
        if asset.document.accessors[index].count != vertex_count:
            raise GeometryError(f"Attribute {name} count differs from POSITION.")
    if primitive.indices is None:
        indices = np.arange(vertex_count, dtype=np.uint32)
        indices.setflags(write=False)
        index_accessor = None
    else:
        index_accessor = require_int(primitive.indices, "Indices accessor", error=GeometryError)
        if index_accessor >= len(asset.document.accessors):
            raise GeometryError("Indices accessor is out of range.")
        accessor = asset.document.accessors[index_accessor]
        if accessor.type != "SCALAR" or accessor.componentType not in {5121, 5123, 5125} or accessor.normalized:
            raise GeometryError("Indices must use an unnormalized unsigned SCALAR accessor.")
        if accessor.bufferView is not None:
            asset.buffer_view(accessor.bufferView)
            if asset.document.bufferViews[accessor.bufferView].byteStride is not None:
                raise GeometryError("Indices BufferView cannot define byteStride.")
            if asset.document.bufferViews[accessor.bufferView].target not in {None, 34963}:
                raise GeometryError("Indices refer to a vertex-only BufferView.")
        indices = decode_accessor(asset, index_accessor)
    if len(indices) == 0 or len(indices) % 3:
        raise GeometryError("TRIANGLES indices count must be positive and divisible by 3.")
    if int(indices.max()) >= vertex_count:
        raise GeometryError("Triangle index exceeds POSITION vertex count.")
    if np.any(indices == np.iinfo(indices.dtype).max):
        raise GeometryError("Primitive-restart sentinel indices are forbidden by glTF.")
    material_index = primitive.material
    if material_index is not None:
        material_index = require_int(material_index, "Material index", error=GeometryError)
        if material_index >= len(asset.document.materials):
            raise GeometryError("Material index is out of range.")
    return PrimitiveGeometry(
        mesh_index, primitive_index, positions, normals, uv, tangents, indices, mode,
        material_index, index_accessor, MappingProxyType(attributes),
    )


def get_geometry_stats(asset: GlbAsset) -> GeometryStats:
    """Count mesh primitives once, not node instances; vertices are summed per primitive."""
    primitives = triangles = vertices = indexed = normals = uv = tangents = 0
    for mesh_index, mesh in enumerate(asset.document.meshes):
        for primitive_index in range(len(mesh.primitives)):
            geometry = extract_primitive(asset, mesh_index, primitive_index)
            primitives += 1
            triangles += len(geometry.indices) // 3
            vertices += len(geometry.positions)
            indexed += geometry.index_accessor is not None
            normals += geometry.normals is not None
            uv += geometry.texcoord_0 is not None
            tangents += geometry.tangents is not None
    return GeometryStats(len(asset.document.meshes), primitives, triangles, vertices, indexed, normals, uv, tangents)
