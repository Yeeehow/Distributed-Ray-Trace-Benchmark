"""Compose the validated static input chain into a compact inspection summary."""

from dataclasses import asdict, dataclass
from os import PathLike
from typing import Any, Literal

import numpy as np

from drt_backend.raytracing.common.errors import SingularTransformError
from drt_backend.raytracing.scene.glb_loader import load_glb
from drt_backend.raytracing.scene.render_metadata import RenderMetadata, build_render_metadata
from drt_backend.raytracing.scene.scene import Scene, build_scene


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    source: str | None = None
    index: int | None = None
    blocks_m3: bool = False


@dataclass(frozen=True, slots=True)
class SceneInspection:
    """References to accepted models plus structural readiness, never render output.

    Obtain instances with inspect_scene(). As with Scene/GlbAsset, callers must
    treat nested source metadata and arrays as read-only after construction.
    """
    scene: Scene
    metadata: RenderMetadata
    issues: tuple[ValidationIssue, ...]

    @property
    def m3_input_ready(self) -> bool:
        """At least one camera and instanced triangle; all instance inverses finite."""
        return not any(issue.blocks_m3 for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        """Return fresh JSON values, excluding source names/paths, arrays and bytes.

        schema_version belongs only to drt.scene_inspection, not Render Protocol.
        Counts retain document/active/referenced and unique/instanced distinctions.
        """
        scene_stats, metadata_stats = self.scene.stats, self.metadata.stats
        return {
            "schema_version": 1,
            "kind": "drt.scene_inspection",
            "scene_index": self.scene.scene_index,
            "root_node_indices": list(self.scene.root_node_indices),
            "counts": {
                "document": {
                    "scenes": scene_stats.document_scenes,
                    "nodes": scene_stats.document_nodes,
                    "meshes": scene_stats.document_meshes,
                    "cameras": metadata_stats.document_cameras,
                    "lights": metadata_stats.document_lights,
                    "materials": metadata_stats.document_materials,
                    "textures": metadata_stats.document_textures,
                    "images": metadata_stats.document_images,
                    "samplers": metadata_stats.document_samplers,
                },
                "active": {
                    "nodes": scene_stats.active_nodes,
                    "root_nodes": scene_stats.root_nodes,
                    "mesh_definitions": scene_stats.mesh_definitions,
                    "mesh_instances": scene_stats.mesh_instances,
                    "primitives": scene_stats.mesh_primitives,
                    "geometry_triangles": scene_stats.geometry_triangles,
                    "instanced_triangles": scene_stats.instanced_triangles,
                    "camera_instances": metadata_stats.active_camera_instances,
                    "light_instances": metadata_stats.active_light_instances,
                    "point_lights": sum(light.type == "point" for light in self.metadata.lights),
                    "spot_lights": sum(light.type == "spot" for light in self.metadata.lights),
                    "directional_lights": sum(light.type == "directional" for light in self.metadata.lights),
                },
                "referenced": {
                    "materials": metadata_stats.referenced_materials,
                    "textures": metadata_stats.referenced_textures,
                    "images": metadata_stats.referenced_images,
                    "samplers": metadata_stats.referenced_samplers,
                },
            },
            "camera_node_indices": [camera.node_index for camera in self.metadata.cameras],
            "camera_selection_required": len(self.metadata.cameras) > 1,
            "light_node_indices": {
                kind: [light.node_index for light in self.metadata.lights if light.type == kind]
                for kind in ("point", "spot", "directional")
            },
            "bounds": {
                "empty": self.scene.bounds.is_empty,
                "minimum": None if self.scene.bounds.is_empty else self.scene.bounds.minimum.tolist(),
                "maximum": None if self.scene.bounds.is_empty else self.scene.bounds.maximum.tolist(),
            },
            "issues": [asdict(issue) for issue in self.issues],
            "m3_input_ready": self.m3_input_ready,
            "readiness_scope": "static_inputs_only",
        }


def _readiness_issues(scene: Scene, metadata: RenderMetadata) -> tuple[ValidationIssue, ...]:
    """Add only downstream limitations not already validated by the three builders."""
    issues = []
    if not metadata.cameras:
        issues.append(ValidationIssue("NO_CAMERA", "warning", "No active camera candidate is available.",
                                      "scene", scene.scene_index, True))
    elif len(metadata.cameras) > 1:
        issues.append(ValidationIssue("MULTIPLE_CAMERAS", "info", "Choose a camera node explicitly before ray generation.",
                                      "scene", scene.scene_index))
    for camera in metadata.cameras:
        if camera.perspective is not None and camera.perspective.aspect_ratio is None:
            issues.append(ValidationIssue("VIEWPORT_ASPECT_REQUIRED", "info", "Camera aspect ratio must come from the target viewport.",
                                          "node", camera.node_index))
    if scene.stats.instanced_triangles == 0:
        issues.append(ValidationIssue("NO_TRIANGLE_GEOMETRY", "warning", "No active instanced triangle geometry is available.",
                                      "scene", scene.scene_index, True))
    for instance in scene.mesh_instances:
        if instance.is_singular:
            issues.append(ValidationIssue("SINGULAR_INSTANCE", "warning", "Singular mesh instance has no inverse transform.",
                                          "node", instance.node_index, True))
            continue
        try:
            # Reuse the existing inverse-transpose; no geometry is copied/transformed.
            inverse_linear = instance.normal_matrix.T
        except SingularTransformError:
            issues.append(ValidationIssue("INSTANCE_INVERSE_UNAVAILABLE", "error", "Mesh instance has no finite representable inverse.",
                                          "node", instance.node_index, True))
            continue
        with np.errstate(over="ignore", invalid="ignore"):
            inverse_translation = -(inverse_linear @ instance.world_matrix[:3, 3])
        if not np.isfinite(inverse_translation).all():
            issues.append(ValidationIssue("INSTANCE_INVERSE_UNAVAILABLE", "error", "Mesh inverse translation is not finite in float64.",
                                          "node", instance.node_index, True))
    return tuple(issues)


def inspect_scene(path: str | PathLike[str], scene_index: int | None = None) -> SceneInspection:
    """Load -> select/build Scene -> build RenderMetadata -> assess M3 input limits.

    Existing input/unsupported-feature exceptions propagate unchanged. Successful
    loading may have blocking readiness issues, e.g. no camera or a zero-scale
    mesh. Multiple valid cameras remain candidates; none is selected implicitly.
    This is not a validator for caller-forged Scene/RenderMetadata dataclasses.
    """
    scene = build_scene(load_glb(path), scene_index)
    metadata = build_render_metadata(scene)
    return SceneInspection(scene, metadata, _readiness_issues(scene, metadata))
