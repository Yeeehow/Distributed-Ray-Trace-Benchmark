"""Effective base-color RGBA only; no alpha acceptance or lighting decisions."""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, UnsupportedFeatureError
from drt_backend.raytracing.resources.image_decoding import TextureContext
from drt_backend.raytracing.shading.material_evaluation import BaseMaterial, evaluate_base_material
from drt_backend.raytracing.tracing.surface_evaluation import SurfaceAttributes
from drt_backend.raytracing.shading.texture_sampling import SamplingPolicy, sample_base_color_texture


@dataclass(frozen=True, slots=True, eq=False)
class EffectiveBaseColor:
    surface: SurfaceAttributes
    base: BaseMaterial
    sampled_rgba: NDArray[np.float64] | None
    effective_rgba: NDArray[np.float64]

    @property
    def texture_evaluated(self) -> bool:
        return self.sampled_rgba is not None

    @property
    def texture_pending(self) -> bool:
        return False


def evaluate_effective_base_color(context: TextureContext, surface: SurfaceAttributes,
                                  policy: SamplingPolicy = SamplingPolicy()) -> EffectiveBaseColor:
    """Preserve factor/slot identity; multiply a genuine linear sample once."""
    if not isinstance(context, TextureContext) or not isinstance(surface, SurfaceAttributes):
        raise RenderValidationError("Effective base color requires TextureContext and SurfaceAttributes.")
    context.require_open()
    if surface.scene is not context.shaded.scene:
        raise GeometryError("Surface does not belong to the texture context scene.")
    instance, primitive, _ = context.shaded.resolve_hit(surface.hit)
    if instance is not surface.instance or primitive is not surface.primitive:
        raise GeometryError("Surface source identity does not match its context.")
    base = evaluate_base_material(context.shaded, primitive)
    if set(base.source.extensions) - {"KHR_materials_specular", "KHR_materials_emissive_strength"}:
        raise UnsupportedFeatureError("Material extension semantics are outside core base-color evaluation.")
    sampled = None
    if base.base_color_texture is not None:
        if surface.texcoord_0 is None:
            raise GeometryError("Textured surface has no TEXCOORD_0; no default UV is invented.")
        sampled = sample_base_color_texture(context, base.base_color_texture, surface.texcoord_0, policy)
    rgba = np.asarray(base.base_color_factor, dtype=np.float64)
    if sampled is not None:
        rgba = rgba * sampled
    if not np.isfinite(rgba).all():
        raise RenderValidationError("Effective base color must be finite.")
    return EffectiveBaseColor(surface, base, sampled, np.frombuffer(rgba.tobytes(), dtype=np.float64))
