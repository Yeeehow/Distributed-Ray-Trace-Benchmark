"""M4.1 base factors and bindings; no texture pixels or alpha acceptance."""

from dataclasses import dataclass

from drt_backend.raytracing.common._metadata import number, vector
from drt_backend.raytracing.common.errors import MetadataValidationError, RenderValidationError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.scene.geometry import PrimitiveGeometry
from drt_backend.raytracing.model.materials import MaterialData
from drt_backend.raytracing.resources.shaded_context import ShadedContext
from drt_backend.raytracing.model.textures import TextureSlot


@dataclass(frozen=True, slots=True)
class BaseMaterial:
    """Borrow the actual material; expose its factors without inventing texels."""

    source: MaterialData

    @property
    def base_color_factor(self) -> tuple[float, ...]:
        """Linear RGB factor and linear alpha factor, with no transfer applied."""
        return self.source.base_color_factor

    @property
    def alpha_mode(self) -> str:
        return self.source.alpha_mode

    @property
    def alpha_cutoff(self) -> float:
        return self.source.alpha_cutoff

    @property
    def double_sided(self) -> bool:
        return self.source.double_sided

    @property
    def base_color_texture(self) -> TextureSlot | None:
        return self.source.base_color_texture

    @property
    def texture_pending(self) -> bool:
        return self.base_color_texture is not None

    @property
    def untextured_base_color(self) -> tuple[float, ...] | None:
        """Factor RGBA only for untextured inputs; None means sampling is pending.

        This is not alpha-mode-adjusted coverage: even OPAQUE keeps its factor A.
        """
        return None if self.texture_pending else self.base_color_factor


def evaluate_base_material(context: ShadedContext, primitive: PrimitiveGeometry) -> BaseMaterial:
    """Resolve the existing default or explicit material, without shading it."""
    if not isinstance(context, ShadedContext):
        raise RenderValidationError("Base material evaluation requires a ShadedContext.")
    context.require_primitive(primitive)
    material = context.metadata.material_for_primitive(primitive)
    if not isinstance(material, MaterialData) or material.material_index != primitive.material_index:
        raise MetadataValidationError("Primitive material identity does not match its metadata resource.")
    vector(material.base_color_factor, 4, "Base color factor")
    number(material.alpha_cutoff, "Alpha cutoff", 0)
    if material.alpha_mode not in ("OPAQUE", "MASK", "BLEND") or type(material.double_sided) is not bool:
        raise MetadataValidationError("Invalid base material alpha/sidedness metadata.")
    slot = material.base_color_texture
    if slot is not None:
        if not isinstance(slot, TextureSlot) or slot.semantic != "base_color":
            raise MetadataValidationError("Base color binding must be a base_color TextureSlot.")
        require_int(slot.texcoord, "Base color texcoord", error=MetadataValidationError)
        if slot.texcoord != 0:
            raise UnsupportedFeatureError("Base color binding requires TEXCOORD_0.")
        index = require_int(slot.texture_index, "Base color texture index", error=MetadataValidationError)
        if index >= len(context.metadata.textures):
            raise MetadataValidationError("Base color texture index is out of range.")
        texture = context.metadata.textures[index]
        image = require_int(texture.source_image_index, "Base color image index", error=MetadataValidationError)
        if texture.texture_index != index or image >= len(context.metadata.images):
            raise MetadataValidationError("Base color texture/image identity is invalid.")
        if texture.sampler_index is not None:
            sampler = require_int(texture.sampler_index, "Base color sampler index", error=MetadataValidationError)
            if sampler >= len(context.metadata.samplers):
                raise MetadataValidationError("Base color sampler index is out of range.")
        if primitive.texcoord_0 is None:
            raise UnsupportedFeatureError("Base color texture binding has no TEXCOORD_0.")
    return BaseMaterial(material)
