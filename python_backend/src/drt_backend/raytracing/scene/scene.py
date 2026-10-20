"""Selected static glTF scenes with shared mesh geometry and per-node instances."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.scene.bounds import AABB
from drt_backend.raytracing.common.errors import SceneValidationError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.scene.geometry import PrimitiveGeometry, extract_primitive
from drt_backend.raytracing.scene.glb_loader import GlbAsset
from drt_backend.raytracing.scene.transforms import compose_world, node_local_matrix, normal_matrix, orientation_sign


@dataclass(frozen=True, slots=True)
class SceneNode:
    node_index: int
    name: str | None
    parent_index: int | None
    children: tuple[int, ...]
    local_matrix: NDArray[np.float64]
    world_matrix: NDArray[np.float64]
    mesh_index: int | None
    camera_index: int | None
    light_index: int | None
    extensions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MeshDefinition:
    mesh_index: int
    name: str | None
    primitives: tuple[PrimitiveGeometry, ...]
    primitive_bounds: tuple[AABB, ...]
    local_bounds: AABB
    triangle_count: int


@dataclass(frozen=True, slots=True)
class MeshInstance:
    node_index: int
    definition: MeshDefinition
    world_matrix: NDArray[np.float64]
    world_bounds: AABB
    orientation_sign: int

    @property
    def mesh_index(self) -> int:
        return self.definition.mesh_index

    @property
    def local_bounds(self) -> AABB:
        return self.definition.local_bounds

    @property
    def triangle_count(self) -> int:
        return self.definition.triangle_count

    @property
    def is_singular(self) -> bool:
        return self.orientation_sign == 0

    @property
    def normal_matrix(self) -> NDArray[np.float64]:
        """Lazy inverse-transpose: singular instances load but fail this request."""
        return normal_matrix(self.world_matrix)


@dataclass(frozen=True, slots=True)
class SceneStats:
    document_scenes: int
    document_nodes: int
    active_nodes: int
    root_nodes: int
    child_edges: int
    document_meshes: int
    mesh_definitions: int
    mesh_primitives: int
    mesh_instances: int
    geometry_triangles: int
    instanced_triangles: int
    camera_nodes: int
    light_nodes: int
    singular_instances: int


@dataclass(frozen=True, slots=True)
class Scene:
    asset: GlbAsset
    scene_index: int
    root_node_indices: tuple[int, ...]
    nodes: Mapping[int, SceneNode]
    mesh_definitions: Mapping[int, MeshDefinition]
    mesh_instances: tuple[MeshInstance, ...]
    stats: SceneStats
    bounds: AABB

    @property
    def active_node_indices(self) -> tuple[int, ...]:
        return tuple(self.nodes)

    @property
    def inactive_node_indices(self) -> tuple[int, ...]:
        return tuple(index for index in range(self.stats.document_nodes) if index not in self.nodes)


def _field(source: object, name: str, default: object = None) -> Any:
    return source.get(name, default) if isinstance(source, Mapping) else getattr(source, name, default)


def _objects(asset: GlbAsset, name: str, parsed: Sequence[Any]) -> Sequence[Any]:
    """Use retained raw metadata to avoid integer/vector coercion by a binding."""
    if asset.raw_document is None:
        return parsed
    raw = asset.raw_document.get(name, [])
    if not isinstance(raw, list) or len(raw) != len(parsed) or any(not isinstance(item, dict) for item in raw):
        raise SceneValidationError(f"Raw and parsed {name} metadata are inconsistent.")
    return raw


def _index(value: object, count: int, label: str) -> int:
    index = require_int(value, label, error=SceneValidationError)
    if index >= count:
        raise SceneValidationError(f"{label} is out of range.")
    return index


def _optional_index(source: object, name: str, count: int, label: str) -> int | None:
    value = _field(source, name)
    if value is None:
        if isinstance(source, Mapping) and name in source:
            raise SceneValidationError(f"{label} cannot be null.")
        return None
    return _index(value, count, label)


def _node_indices(value: object, count: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise SceneValidationError(f"{label} must be an array of node indices.")
    indices = tuple(_index(index, count, label) for index in value)
    if len(set(indices)) != len(indices):
        raise SceneValidationError(f"Duplicate reference in {label}.")
    return indices


def build_scene(asset: GlbAsset, scene_index: int | None = None) -> Scene:
    """Build the selected default static pose; decode each active mesh exactly once.

    Global references/transforms and the strict node forest are validated. Only
    selected reachable nodes contribute instances, and only their mesh definitions
    are decoded. Inactive skin/morph/instancing features are not evaluated.
    No-default policy: select a sole scene; require an explicit choice if ambiguous.
    A document with no scenes is a resource library, not an implicit merged scene.
    """
    document = asset.document
    for name in ("nodes", "scenes", "meshes", "cameras"):
        if not isinstance(getattr(document, name), (list, tuple)):
            raise SceneValidationError(f"Document {name} must be an array.")
    node_sources = _objects(asset, "nodes", document.nodes)
    scene_sources = _objects(asset, "scenes", document.scenes)
    node_count = len(node_sources)
    if not scene_sources:
        raise SceneValidationError("The document has no scenes; explicit scene definitions are required.")
    default = (asset.raw_document.get("scene") if asset.raw_document is not None else document.scene)
    if asset.raw_document is not None and "scene" in asset.raw_document and default is None:
        raise SceneValidationError("Default scene index cannot be null.")
    if default is not None:
        default = _index(default, len(scene_sources), "Default scene index")
    if scene_index is None:
        if default is not None:
            scene_index = default
        elif len(scene_sources) == 1:
            scene_index = 0
        else:
            raise SceneValidationError("Multiple scenes without a default require an explicit scene index.")
    scene_index = _index(scene_index, len(scene_sources), "Scene index")

    light_extension = (document.extensions or {}).get("KHR_lights_punctual", {})
    if not isinstance(light_extension, dict) or not isinstance(light_extension.get("lights", []), list):
        raise SceneValidationError("Invalid punctual-light reference metadata.")
    light_count = len(light_extension.get("lights", []))
    children: list[tuple[int, ...]] = []
    parents: list[int | None] = [None] * node_count
    locals_: list[NDArray[np.float64]] = []
    mesh_indices: list[int | None] = []
    camera_indices: list[int | None] = []
    light_indices: list[int | None] = []
    extensions: list[Mapping[str, Any]] = []
    for index, source in enumerate(node_sources):
        child_values = _field(source, "children", [])
        if child_values is None and not isinstance(source, Mapping):
            child_values = []
        child_indices = _node_indices(child_values, node_count, f"Node {index} children")
        children.append(child_indices)
        for child in child_indices:
            if parents[child] is not None:
                raise SceneValidationError(f"Node {child} has a duplicate parent.")
            parents[child] = index
        mesh_indices.append(_optional_index(source, "mesh", len(document.meshes), f"Node {index} mesh index"))
        camera_indices.append(_optional_index(source, "camera", len(document.cameras), f"Node {index} camera index"))
        locals_.append(node_local_matrix(source))
        node_extensions = _field(source, "extensions", {})
        if node_extensions is None and not isinstance(source, Mapping):
            node_extensions = {}
        if not isinstance(node_extensions, dict):
            raise SceneValidationError(f"Node {index} extensions must be an object.")
        light_index = None
        if "KHR_lights_punctual" in node_extensions:
            light = node_extensions["KHR_lights_punctual"]
            if not isinstance(light, dict):
                raise SceneValidationError("Node light reference must be an object.")
            light_index = _index(light.get("light"), light_count, f"Node {index} light index")
        light_indices.append(light_index)
        extensions.append(MappingProxyType(dict(node_extensions)))

    # Kahn traversal validates arbitrarily deep forests without Python recursion.
    indegree = [int(parent is not None) for parent in parents]
    queue = deque(index for index, degree in enumerate(indegree) if degree == 0)
    visited = 0
    while queue:
        index = queue.popleft()
        visited += 1
        for child in children[index]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != node_count:
        raise SceneValidationError("Node hierarchy contains a cycle.")
    scene_roots = []
    for index, source in enumerate(scene_sources):
        values = _field(source, "nodes", [])
        if values is None and not isinstance(source, Mapping):
            values = []
        roots = _node_indices(values, node_count, f"Scene {index} roots")
        if any(parents[root] is not None for root in roots):
            raise SceneValidationError("Scene roots cannot be children of another node.")
        scene_roots.append(roots)
    roots = scene_roots[scene_index]

    active: dict[int, SceneNode] = {}
    stack = list(reversed(roots))
    while stack:
        index = stack.pop()
        if index in active:
            raise SceneValidationError("Active scene contains a repeated node or parent.")
        source = node_sources[index]
        if _field(source, "skin") is not None or _field(source, "weights"):
            raise UnsupportedFeatureError(f"Active node {index}: skinning/morph weights are not supported.")
        if "EXT_mesh_gpu_instancing" in extensions[index]:
            raise UnsupportedFeatureError("Active EXT_mesh_gpu_instancing is not supported.")
        mesh_index = mesh_indices[index]
        if mesh_index is not None and document.meshes[mesh_index].weights:
            raise UnsupportedFeatureError("Active mesh morph weights are not supported.")
        parent = parents[index]
        world = locals_[index] if parent is None else compose_world(active[parent].world_matrix, locals_[index])
        active[index] = SceneNode(
            index, document.nodes[index].name, parent, children[index], locals_[index], world,
            mesh_index, camera_indices[index], light_indices[index], extensions[index],
        )
        stack.extend(reversed(children[index]))

    mesh_definitions: dict[int, MeshDefinition] = {}
    for index in sorted({node.mesh_index for node in active.values() if node.mesh_index is not None}):
        mesh = document.meshes[index]
        primitives = tuple(extract_primitive(asset, index, primitive) for primitive in range(len(mesh.primitives)))
        primitive_bounds = tuple(AABB.from_points(primitive.positions) for primitive in primitives)
        local_bounds = AABB.empty()
        for bound in primitive_bounds:
            local_bounds = local_bounds.union(bound)
        mesh_definitions[index] = MeshDefinition(
            index, mesh.name, primitives, primitive_bounds, local_bounds,
            sum(len(primitive.indices) // 3 for primitive in primitives),
        )
    instances = tuple(
        MeshInstance(node.node_index, mesh_definitions[node.mesh_index], node.world_matrix,
                     mesh_definitions[node.mesh_index].local_bounds.transform(node.world_matrix),
                     orientation_sign(node.world_matrix))
        for node in active.values() if node.mesh_index is not None
    )
    bounds = AABB.empty()
    for instance in instances:
        bounds = bounds.union(instance.world_bounds)
    stats = SceneStats(
        len(document.scenes), node_count, len(active), len(roots),
        sum(len(node.children) for node in active.values()), len(document.meshes),
        len(mesh_definitions), sum(len(mesh.primitives) for mesh in mesh_definitions.values()),
        len(instances), sum(mesh.triangle_count for mesh in mesh_definitions.values()),
        sum(instance.triangle_count for instance in instances),
        sum(node.camera_index is not None for node in active.values()),
        sum(node.light_index is not None for node in active.values()),
        sum(instance.is_singular for instance in instances),
    )
    return Scene(asset, scene_index, roots, MappingProxyType(active), MappingProxyType(mesh_definitions), instances, stats, bounds)
