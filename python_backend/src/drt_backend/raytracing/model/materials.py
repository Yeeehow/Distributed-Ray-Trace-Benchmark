"""glTF metallic-roughness material data, including specular/emissive extensions."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from drt_backend.raytracing.common._metadata import extensions, name, number, object_data, optional, retained, vector
from drt_backend.raytracing.common.errors import MetadataValidationError
from drt_backend.raytracing.model.textures import TextureSlot, texture_slot


@dataclass(frozen=True, slots=True)
class MaterialData:
    material_index: int | None
    name: str | None
    base_color_factor: tuple[float, ...]
    metallic_factor: float
    roughness_factor: float
    base_color_texture: TextureSlot | None
    metallic_roughness_texture: TextureSlot | None
    normal_texture: TextureSlot | None
    occlusion_texture: TextureSlot | None
    emissive_texture: TextureSlot | None
    emissive_factor: tuple[float, ...]
    alpha_mode: str
    alpha_cutoff: float
    double_sided: bool
    specular_factor: float
    specular_texture: TextureSlot | None
    specular_color_factor: tuple[float, ...]
    specular_color_texture: TextureSlot | None
    emissive_strength: float
    source: Mapping[str, Any]
    extensions: Mapping[str, Any]

    @property
    def texture_slots(self) -> tuple[TextureSlot, ...]:
        return tuple(slot for slot in (
            self.base_color_texture, self.metallic_roughness_texture, self.normal_texture,
            self.occlusion_texture, self.emissive_texture, self.specular_texture, self.specular_color_texture,
        ) if slot is not None)


def parse_material(source: Mapping[str, Any], material_index: int | None, texture_count: int) -> MaterialData:
    pbr = object_data(source.get("pbrMetallicRoughness", {}), "pbrMetallicRoughness")
    source_extensions = extensions(source)
    specular = object_data(source_extensions.get("KHR_materials_specular", {}), "KHR_materials_specular")
    emission = object_data(source_extensions.get("KHR_materials_emissive_strength", {}), "KHR_materials_emissive_strength")
    alpha = source.get("alphaMode", "OPAQUE")
    if not isinstance(alpha, str) or alpha not in {"OPAQUE", "MASK", "BLEND"}:
        raise MetadataValidationError("Material alphaMode must be OPAQUE, MASK or BLEND.")
    double_sided = source.get("doubleSided", False)
    if type(double_sided) is not bool:
        raise MetadataValidationError("Material doubleSided must be a boolean.")

    def slot(owner: Mapping[str, Any], field: str, semantic: str) -> TextureSlot | None:
        return texture_slot(optional(owner, field), semantic, texture_count)

    return MaterialData(
        material_index, name(source), vector(pbr.get("baseColorFactor", [1, 1, 1, 1]), 4, "baseColorFactor"),
        number(pbr.get("metallicFactor", 1), "metallicFactor", 0, 1),
        number(pbr.get("roughnessFactor", 1), "roughnessFactor", 0, 1),
        slot(pbr, "baseColorTexture", "base_color"), slot(pbr, "metallicRoughnessTexture", "metallic_roughness"),
        slot(source, "normalTexture", "normal"), slot(source, "occlusionTexture", "occlusion"),
        slot(source, "emissiveTexture", "emissive"), vector(source.get("emissiveFactor", [0, 0, 0]), 3, "emissiveFactor"),
        alpha, number(source.get("alphaCutoff", 0.5), "alphaCutoff", 0), double_sided,
        number(specular.get("specularFactor", 1), "specularFactor", 0, 1),
        slot(specular, "specularTexture", "specular"),
        vector(specular.get("specularColorFactor", [1, 1, 1]), 3, "specularColorFactor"),
        slot(specular, "specularColorTexture", "specular_color"),
        number(emission.get("emissiveStrength", 1), "emissiveStrength", 0), retained(source), source_extensions,
    )
