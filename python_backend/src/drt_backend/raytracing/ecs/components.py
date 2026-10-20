"""Typed static scene components; node entities borrow the original mesh resources.

Component rows are not GLB identities or instance ordinals. Geometry, matrices,
material definitions and metadata are borrowed from the existing authoritative
Scene/RenderMetadata. Newly owned integer columns are backed by immutable bytes.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from collections.abc import Mapping

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import SceneValidationError, require_int
from drt_backend.raytracing.scene.geometry import PrimitiveGeometry
from drt_backend.raytracing.scene.glb_loader import GlbAsset
from drt_backend.raytracing.scene.render_metadata import RenderMetadata
from drt_backend.raytracing.scene.scene import MeshDefinition, Scene


def _column(values) -> NDArray[np.int64]:
    array = np.asarray(tuple(values), dtype=np.int64)
    return np.frombuffer(array.tobytes(), dtype=np.int64)


@dataclass(frozen=True, slots=True)
class EntityNamespace:
    asset_sha256: str
    scene_index: int


@dataclass(frozen=True, slots=True, eq=False)
class TransformComponents:
    entity_ids: NDArray[np.int64]
    parent_entities: NDArray[np.int64]  # -1 denotes no parent.
    local_matrices: tuple[NDArray[np.float64], ...]
    world_matrices: tuple[NDArray[np.float64], ...]
    entity_to_row: Mapping[int, int]

    def row_for(self, entity: int) -> int:
        require_int(entity, "Entity ID", error=SceneValidationError)
        try:
            return self.entity_to_row[entity]
        except KeyError as error:
            raise SceneValidationError("Entity is not active in this prepared scene.") from error


@dataclass(frozen=True, slots=True, eq=False)
class MeshReferenceComponents:
    entity_ids: NDArray[np.int64]  # This table follows the original instance tuple.
    mesh_indices: NDArray[np.int64]
    resource_rows: NDArray[np.int64]
    instance_ordinals: NDArray[np.int64]
    entity_to_instance: Mapping[int, int]


@dataclass(frozen=True, slots=True, eq=False)
class ResourceReferenceComponents:
    entity_ids: NDArray[np.int64]
    definition_indices: NDArray[np.int64]
    resource_rows: NDArray[np.int64]  # Original metadata camera/light tuple order.


@dataclass(frozen=True, slots=True, eq=False)
class PrimitiveBindings:
    mesh_indices: NDArray[np.int64]
    primitive_indices: NDArray[np.int64]
    material_indices: NDArray[np.int64]  # -1 is the existing default material.
    resources: tuple[PrimitiveGeometry, ...]


@dataclass(frozen=True, slots=True, eq=False)
class SceneComponents:
    namespace: EntityNamespace
    transforms: TransformComponents
    meshes: MeshReferenceComponents
    cameras: ResourceReferenceComponents
    lights: ResourceReferenceComponents
    primitives: PrimitiveBindings
    mesh_resources: tuple[MeshDefinition, ...]
    mesh_to_resource_row: Mapping[int, int]


def asset_fingerprint(asset: GlbAsset) -> str:
    """Existing ECS namespace encoding, shared with task configuration identity."""
    raw = asset.raw_document
    if raw is None:
        raw = json.loads(asset.document.to_json())
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    identity = sha256()
    identity.update(len(encoded).to_bytes(8, "little"))
    identity.update(encoded)
    identity.update(asset.binary)
    return identity.hexdigest()


def build_components(scene: Scene, metadata: RenderMetadata) -> SceneComponents:
    """Project original identities; never decode, transform or copy geometry.

    The fingerprint uses canonical retained JSON plus immutable BIN bytes, not
    Python hash() or object addresses. Nested source metadata remain read-only
    by convention; this is not a claim of deep immutability.
    """
    if metadata.scene is not scene:
        raise SceneValidationError("Components and metadata must borrow the exact same Scene.")
    fingerprint = asset_fingerprint(scene.asset)
    nodes = tuple(scene.nodes.values())  # Preserve active traversal, including transform-only parents.
    rows = MappingProxyType({node.node_index: row for row, node in enumerate(nodes)})
    transforms = TransformComponents(
        _column(node.node_index for node in nodes),
        _column(-1 if node.parent_index is None else node.parent_index for node in nodes),
        tuple(node.local_matrix for node in nodes), tuple(node.world_matrix for node in nodes), rows)
    resources = tuple(scene.mesh_definitions.values())
    resource_rows = MappingProxyType({mesh.mesh_index: row for row, mesh in enumerate(resources)})
    instances = scene.mesh_instances
    meshes = MeshReferenceComponents(
        _column(instance.node_index for instance in instances),
        _column(instance.mesh_index for instance in instances),
        _column(resource_rows[instance.mesh_index] for instance in instances),
        _column(range(len(instances))),
        MappingProxyType({instance.node_index: ordinal for ordinal, instance in enumerate(instances)}))

    def references(items, definition_name):
        return ResourceReferenceComponents(
            _column(item.node_index for item in items),
            _column(getattr(item, definition_name) for item in items), _column(range(len(items))))

    primitives = tuple(primitive for mesh in resources for primitive in mesh.primitives)
    return SceneComponents(
        EntityNamespace(fingerprint, scene.scene_index), transforms, meshes,
        references(metadata.cameras, "camera_index"), references(metadata.lights, "light_index"),
        PrimitiveBindings(_column(p.mesh_index for p in primitives),
            _column(p.primitive_index for p in primitives),
            _column(-1 if p.material_index is None else p.material_index for p in primitives), primitives),
        resources, resource_rows)
