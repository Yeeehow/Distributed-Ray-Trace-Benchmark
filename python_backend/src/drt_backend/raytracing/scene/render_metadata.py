"""Compose validated resource metadata with an existing selected static Scene."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from drt_backend.raytracing.common._metadata import extensions, index, object_data, resources
from drt_backend.raytracing.model.camera import CameraData, camera_instances
from drt_backend.raytracing.common.errors import MetadataValidationError, UnsupportedFeatureError
from drt_backend.raytracing.scene.geometry import PrimitiveGeometry
from drt_backend.raytracing.model.lights import LightData, light_instances
from drt_backend.raytracing.model.materials import MaterialData, parse_material
from drt_backend.raytracing.scene.scene import Scene
from drt_backend.raytracing.model.textures import EmbeddedImageData, SamplerData, TextureData, parse_images, parse_sampler, parse_textures


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """Unique indices reachable through active mesh materials, not document totals."""
    material_indices: tuple[int, ...]
    texture_indices: tuple[int, ...]
    image_indices: tuple[int, ...]
    sampler_indices: tuple[int, ...]
    uses_default_material: bool
    uses_default_sampler: bool


@dataclass(frozen=True, slots=True)
class RenderMetadataStats:
    document_cameras: int
    document_lights: int
    document_materials: int
    document_textures: int
    document_images: int
    document_samplers: int
    active_camera_instances: int
    active_light_instances: int
    referenced_materials: int
    referenced_textures: int
    referenced_images: int
    referenced_samplers: int


@dataclass(frozen=True, slots=True)
class RenderMetadata:
    scene: Scene
    cameras: tuple[CameraData, ...]
    lights: tuple[LightData, ...]
    materials: tuple[MaterialData, ...]
    textures: tuple[TextureData, ...]
    images: tuple[EmbeddedImageData, ...]
    samplers: tuple[SamplerData, ...]
    default_material: MaterialData
    default_sampler: SamplerData
    usage: ResourceUsage
    stats: RenderMetadataStats
    extensions: Mapping[str, Any]

    def material_for_primitive(self, primitive: PrimitiveGeometry) -> MaterialData:
        if primitive.material_index is None:
            return self.default_material
        return self.materials[index(primitive.material_index, len(self.materials), "Primitive material index")]

    def sampler_for_texture(self, texture_index: int) -> SamplerData:
        texture = self.textures[index(texture_index, len(self.textures), "Texture index")]
        return self.default_sampler if texture.sampler_index is None else self.samplers[texture.sampler_index]


def _document_extensions(scene: Scene) -> Mapping[str, Any]:
    source = object_data(scene.asset.raw_document if scene.asset.raw_document is not None else scene.asset.document,
                         "glTF document")
    required, used = source.get("extensionsRequired", []), source.get("extensionsUsed", [])
    for label, value in (("extensionsRequired", required), ("extensionsUsed", used)):
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise MetadataValidationError(f"{label} must be an array of extension names.")
    if not set(required).issubset(used):
        raise MetadataValidationError("Required extensions must also appear in extensionsUsed.")
    unsupported = set(required) - {"KHR_lights_punctual", "KHR_materials_specular", "KHR_materials_emissive_strength"}
    if unsupported:
        raise UnsupportedFeatureError(f"Unsupported required metadata extensions: {', '.join(sorted(unsupported))}.")
    return extensions(source)


def build_render_metadata(scene: Scene) -> RenderMetadata:
    """Validate document resources, then attach active camera/light node instances.

    Scene transforms/geometry are reused, never decoded/recomposed. Unknown optional
    extensions remain available but are not interpreted. All core resource chains
    are checked; UV availability is checked for active primitive/material pairs.
    """
    source_extensions = _document_extensions(scene)
    light_extension = object_data(source_extensions.get("KHR_lights_punctual", {}), "KHR_lights_punctual")
    light_values = light_extension.get("lights", [])
    if not isinstance(light_values, (list, tuple)):
        raise MetadataValidationError("Punctual lights must be an array.")
    light_sources = tuple(object_data(value, "Punctual light") for value in light_values)
    cameras = camera_instances(scene, resources(scene.asset, "cameras"))
    lights = light_instances(scene, light_sources)
    samplers = tuple(parse_sampler(source, i) for i, source in enumerate(resources(scene.asset, "samplers")))
    images = parse_images(scene.asset, resources(scene.asset, "images"))
    textures = parse_textures(resources(scene.asset, "textures"), len(images), len(samplers))
    materials = tuple(parse_material(source, i, len(textures)) for i, source in enumerate(resources(scene.asset, "materials")))
    default_material, default_sampler = parse_material({}, None, len(textures)), parse_sampler({}, None)
    used_materials: set[int] = set()
    uses_default_material = False
    for mesh in scene.mesh_definitions.values():
        for primitive in mesh.primitives:
            if primitive.material_index is None:
                material = default_material
                uses_default_material = True
            else:
                material_index = index(primitive.material_index, len(materials), "Primitive material index")
                used_materials.add(material_index)
                material = materials[material_index]
            if material.texture_slots and primitive.texcoord_0 is None:
                raise UnsupportedFeatureError(
                    f"Mesh {primitive.mesh_index} primitive {primitive.primitive_index} uses textures without TEXCOORD_0."
                )
    used_textures = {slot.texture_index for i in used_materials for slot in materials[i].texture_slots}
    used_images = {textures[i].source_image_index for i in used_textures}
    used_samplers = {textures[i].sampler_index for i in used_textures if textures[i].sampler_index is not None}
    usage = ResourceUsage(tuple(sorted(used_materials)), tuple(sorted(used_textures)), tuple(sorted(used_images)),
                          tuple(sorted(used_samplers)), uses_default_material,
                          any(textures[i].sampler_index is None for i in used_textures))
    stats = RenderMetadataStats(len(scene.asset.document.cameras), len(light_sources), len(materials), len(textures),
                                len(images), len(samplers), len(cameras), len(lights), len(used_materials),
                                len(used_textures), len(used_images), len(used_samplers))
    return RenderMetadata(scene, cameras, lights, materials, textures, images, samplers,
                          default_material, default_sampler, usage, stats, source_extensions)
