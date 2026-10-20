"""Static scene inputs, ray/BVH queries and world geometric-normal PNG output."""

from drt_backend.raytracing.scene.accessors import decode_accessor
from drt_backend.raytracing.scene.bounds import AABB
from drt_backend.raytracing.common.errors import (
    AccessorDecodeError,
    GeometryError,
    GlbError,
    GlbFileError,
    GlbValidationError,
    MetadataValidationError,
    RenderValidationError,
    SceneValidationError,
    SingularTransformError,
    UnsupportedFeatureError,
)
from drt_backend.raytracing.scene.geometry import GeometryStats, PrimitiveGeometry, extract_primitive, get_geometry_stats
from drt_backend.raytracing.scene.glb_loader import GlbAsset, load_glb
from drt_backend.raytracing.scene.scene import MeshDefinition, MeshInstance, Scene, SceneNode, SceneStats, build_scene
from drt_backend.raytracing.model.camera import CameraData, OrthographicData, PerspectiveData
from drt_backend.raytracing.model.lights import LightData
from drt_backend.raytracing.model.materials import MaterialData
from drt_backend.raytracing.scene.render_metadata import RenderMetadata, RenderMetadataStats, ResourceUsage, build_render_metadata
from drt_backend.raytracing.model.textures import EmbeddedImageData, SamplerData, TextureData, TextureSlot
from drt_backend.raytracing.scene.inspection import SceneInspection, ValidationIssue, inspect_scene
from drt_backend.raytracing.scene.transforms import compose_world, node_local_matrix, normal_matrix, orientation_sign, transform_normals
from drt_backend.raytracing.model.render_config import RenderConfig
from drt_backend.raytracing.model.rays import Ray, RayBatch
from drt_backend.raytracing.tracing.primary_rays import StripRegion, generate_primary_rays, iter_strips, select_camera
from drt_backend.raytracing.tracing.intersection import (
    HitRecord, LocalRay, PreparedInstance, brute_force_closest_hit, intersect_triangle, transform_ray,
)
from drt_backend.raytracing.tracing.bvh import SceneAcceleration, build_acceleration, trace_closest
from drt_backend.raytracing.rendering.normal_visualization import encode_normal_rgb, geometric_normal_world, render_normal_image
from drt_backend.raytracing.output.png_output import save_rgb_png

__all__ = [
    "GlbAsset", "load_glb", "decode_accessor", "PrimitiveGeometry", "GeometryStats",
    "extract_primitive", "get_geometry_stats", "GlbError", "GlbFileError",
    "GlbValidationError", "AccessorDecodeError", "GeometryError", "UnsupportedFeatureError",
    "Scene", "SceneNode", "SceneStats", "MeshDefinition", "MeshInstance", "AABB", "build_scene",
    "node_local_matrix", "compose_world", "normal_matrix", "orientation_sign", "transform_normals",
    "SceneValidationError", "SingularTransformError",
    "MetadataValidationError", "CameraData", "PerspectiveData", "OrthographicData", "LightData",
    "MaterialData", "TextureData", "TextureSlot", "EmbeddedImageData", "SamplerData",
    "RenderMetadata", "RenderMetadataStats", "ResourceUsage", "build_render_metadata",
    "SceneInspection", "ValidationIssue", "inspect_scene",
    "RenderValidationError", "RenderConfig", "Ray", "RayBatch", "StripRegion",
    "select_camera", "iter_strips", "generate_primary_rays",
    "HitRecord", "LocalRay", "PreparedInstance", "transform_ray", "intersect_triangle", "brute_force_closest_hit",
    "SceneAcceleration", "build_acceleration", "trace_closest",
    "geometric_normal_world", "encode_normal_rgb", "render_normal_image", "save_rgb_png",
]

# New shaded resources remain lazy so normal/inspection imports do not require Pillow.
_MX3_EXPORTS = {
    "EntityNamespace": "ecs.components", "SceneComponents": "ecs.components", "build_components": "ecs.components",
    "PreparedScene": "resources.preparation", "prepare_scene": "resources.preparation",
}
__all__ += list(_MX3_EXPORTS)


def __getattr__(name):
    if name not in _MX3_EXPORTS:
        raise AttributeError(name)
    from importlib import import_module
    value = getattr(import_module("." + _MX3_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
