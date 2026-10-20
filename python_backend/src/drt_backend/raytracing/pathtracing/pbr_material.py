"""glTF material resolution from the existing MaterialData source of truth.

Explicit slot-bound texels only: no image decoding, loading or hit execution.
Already-linear samples are tagged to prevent a second sRGB conversion.
"""
from dataclasses import dataclass, replace
from collections.abc import Mapping
import math

from drt_backend.raytracing.model.materials import MaterialData, parse_material
from drt_backend.raytracing.common.errors import MetadataValidationError, UnsupportedFeatureError
from drt_backend.raytracing.common._metadata import retained, extensions
from .contracts import PolicyDescriptor
from .color_space import srgb_to_linear
from .pbr_numeric import scalar, vector
from .pbr_errors import InvalidPbrMaterial, UnsupportedPbrFeature

MATERIAL_POLICY = PolicyDescriptor('drt.material.gltf_mr.v1', (
    ('color', 'srgb_piecewise_or_tagged_linear'), ('emission', 'unitless_strength'),
    ('occlusion', 'unsupported'), ('specular', 'khr_max_channel_attenuation'),
    ('uv', 'resolved_texcoord0'), ('vertex_color', 'explicit_linear_multiplier')))
_EXTENSIONS = {'KHR_materials_specular': {'specularFactor', 'specularTexture',
                'specularColorFactor', 'specularColorTexture'},
               'KHR_materials_emissive_strength': {'emissiveStrength'}}


def validate_material_policy(descriptor: PolicyDescriptor) -> None:
    if type(descriptor) is not PolicyDescriptor or descriptor != MATERIAL_POLICY:
        raise UnsupportedPbrFeature('Unsupported executable material identity/parameters.')


def parse_pbr_source(source: Mapping, material_index: int | None, texture_count: int) -> MaterialData:
    """Additive adapter to Legacy parsing, including valid specular colors >1.

    The Legacy parser bounds specularColorFactor to 1. Parse its other fields
    using a neutral temporary color, then restore the validated actual factor
    and original raw source/extension associations. No invalid value is clipped.
    The returned existing MaterialData remains the only material description.
    """
    if not isinstance(source, Mapping):
        raise InvalidPbrMaterial('Material source must be a mapping.')
    try:
        raw_extensions = source.get('extensions', {})
        if not isinstance(raw_extensions, Mapping):
            raise InvalidPbrMaterial('Material extensions must be a mapping.')
        spec = raw_extensions.get('KHR_materials_specular', {})
        if not isinstance(spec, Mapping):
            raise InvalidPbrMaterial('Specular extension must be a mapping.')
        color = vector(spec.get('specularColorFactor', (1, 1, 1)), 3, InvalidPbrMaterial, minimum=0)
        adjusted = dict(source)
        adjusted['extensions'] = dict(raw_extensions)
        if 'KHR_materials_specular' in raw_extensions:
            adjusted['extensions']['KHR_materials_specular'] = dict(spec, specularColorFactor=(1, 1, 1))
        material = parse_material(adjusted, material_index, texture_count)
        return replace(material, specular_color_factor=color, source=retained(source), extensions=extensions(source))
    except UnsupportedFeatureError as cause:
        raise UnsupportedPbrFeature(str(cause)) from cause
    except MetadataValidationError as cause:
        raise InvalidPbrMaterial(str(cause)) from cause


@dataclass(frozen=True, slots=True)
class TextureSample:
    texture_index: int
    rgba: tuple[float, float, float, float]
    encoding: str  # srgb, linear (already decoded color), or data

    def __post_init__(self):
        if type(self.texture_index) is not int or self.texture_index < 0:
            raise InvalidPbrMaterial('Sample requires a nonnegative texture index.')
        if self.encoding not in ('srgb', 'linear', 'data'):
            raise InvalidPbrMaterial('Sample requires an explicit transfer tag.')
        object.__setattr__(self, 'rgba', vector(self.rgba, 4, InvalidPbrMaterial, minimum=0, maximum=1))


@dataclass(frozen=True, slots=True)
class ResolvedTextureSamples:
    base_color: TextureSample | None = None
    metallic_roughness: TextureSample | None = None
    normal: TextureSample | None = None
    emissive: TextureSample | None = None
    specular: TextureSample | None = None
    specular_color: TextureSample | None = None

    def __post_init__(self):
        for name in self.__slots__:
            item = getattr(self, name)
            if item is not None and type(item) is not TextureSample:
                raise InvalidPbrMaterial('Resolved slots require TextureSample or None.')


@dataclass(frozen=True, slots=True)
class PbrMaterialParameters:
    base_color: tuple[float, float, float]
    metallic: float
    roughness: float
    specular_strength: float
    dielectric_f0: tuple[float, float, float]
    emission: tuple[float, float, float]
    alpha: float
    alpha_mode: str
    alpha_cutoff: float
    double_sided: bool
    normal_rgb: tuple[float, float, float] | None
    normal_scale: float
    source_material: MaterialData

    def __post_init__(self):
        for name in ('base_color', 'dielectric_f0'):
            object.__setattr__(self, name, vector(getattr(self, name), 3, InvalidPbrMaterial, minimum=0, maximum=1))
        object.__setattr__(self, 'emission', vector(self.emission, 3, InvalidPbrMaterial, minimum=0))
        for name in ('metallic', 'roughness', 'specular_strength', 'alpha'):
            object.__setattr__(self, name, scalar(getattr(self, name), InvalidPbrMaterial, minimum=0, maximum=1))
        if any(c > self.specular_strength for c in self.dielectric_f0):
            raise InvalidPbrMaterial('Dielectric F0 cannot exceed specular grazing reflectance.')
        object.__setattr__(self, 'alpha_cutoff', scalar(self.alpha_cutoff, InvalidPbrMaterial, minimum=0))
        object.__setattr__(self, 'normal_scale', scalar(self.normal_scale, InvalidPbrMaterial))
        if self.normal_rgb is not None:
            object.__setattr__(self, 'normal_rgb', vector(self.normal_rgb, 3, InvalidPbrMaterial, minimum=0, maximum=1))
        if self.alpha_mode not in ('OPAQUE', 'MASK', 'BLEND') or type(self.double_sided) is not bool:
            raise InvalidPbrMaterial('Invalid alpha/sidedness state.')
        if type(self.source_material) is not MaterialData:
            raise InvalidPbrMaterial('Material values must retain their MaterialData source.')

    @property
    def coverage_alpha(self) -> float:
        if self.alpha_mode == 'OPAQUE':
            return 1.0
        return float(self.alpha >= self.alpha_cutoff) if self.alpha_mode == 'MASK' else self.alpha


def _texel(slot, sample, *, color):
    if slot is None:
        if sample is not None:
            raise InvalidPbrMaterial('Unexpected texel for an absent material slot.')
        return (1.0, 1.0, 1.0, 1.0)
    if slot.texcoord != 0 or slot.extensions:
        raise UnsupportedPbrFeature('Only resolved TEXCOORD_0 without texture transforms is supported.')
    if type(sample) is not TextureSample or sample.texture_index != slot.texture_index:
        raise InvalidPbrMaterial('Missing texel or texture-slot binding mismatch.')
    if color:
        if sample.encoding not in ('srgb', 'linear'):
            raise InvalidPbrMaterial('Color texels must be tagged srgb or already linear.')
        rgb = tuple(srgb_to_linear(c) for c in sample.rgba[:3]) if sample.encoding == 'srgb' else sample.rgba[:3]
        return (*rgb, sample.rgba[3])
    if sample.encoding != 'data':
        raise InvalidPbrMaterial('Non-color channels require linear data, never an sRGB transform.')
    return sample.rgba


def resolve_material(material: MaterialData, samples: ResolvedTextureSamples, *,
                     vertex_color=None, vertex_color_required: bool = False,
                     policy: PolicyDescriptor = MATERIAL_POLICY) -> PbrMaterialParameters:
    validate_material_policy(policy)
    if type(material) is not MaterialData or type(samples) is not ResolvedTextureSamples:
        raise InvalidPbrMaterial('Resolution requires MaterialData and explicit resolved samples.')
    if type(vertex_color_required) is not bool or vertex_color_required and vertex_color is None:
        raise UnsupportedPbrFeature('A required COLOR_0 multiplier must be explicitly supplied.')
    for name, value in material.extensions.items():
        if name not in _EXTENSIONS or not isinstance(value, Mapping) or set(value)-_EXTENSIONS[name]:
            raise UnsupportedPbrFeature('Unsupported material extension/configuration.')
    if material.occlusion_texture is not None:
        raise UnsupportedPbrFeature('Occlusion transport is deferred, not silently baked into this BSDF.')
    factor = vector(material.base_color_factor, 4, InvalidPbrMaterial, minimum=0, maximum=1)
    vertex = (1.0,)*4 if vertex_color is None else vector(vertex_color, 4, InvalidPbrMaterial, minimum=0, maximum=1)
    base = _texel(material.base_color_texture, samples.base_color, color=True)
    mr = _texel(material.metallic_roughness_texture, samples.metallic_roughness, color=False)
    emission = _texel(material.emissive_texture, samples.emissive, color=True)
    strength = _texel(material.specular_texture, samples.specular, color=False)
    specular_color = _texel(material.specular_color_texture, samples.specular_color, color=True)
    normal = _texel(material.normal_texture, samples.normal, color=False)
    metallic = scalar(material.metallic_factor, InvalidPbrMaterial, minimum=0, maximum=1)*mr[2]
    roughness = scalar(material.roughness_factor, InvalidPbrMaterial, minimum=0, maximum=1)*mr[1]
    weight = scalar(material.specular_factor, InvalidPbrMaterial, minimum=0, maximum=1)*strength[3]
    spec_factor = vector(material.specular_color_factor, 3, InvalidPbrMaterial, minimum=0)
    # The min is the SPECIFIED extension F0 bound for valid high color factors,
    # not a repair of invalid source data. Multiply by 0.04 first to avoid overflow.
    f0 = tuple(min(0.04*c*t, 1.0)*weight for c, t in zip(spec_factor, specular_color[:3]))
    emissive_factor = vector(material.emissive_factor, 3, InvalidPbrMaterial, minimum=0, maximum=1)
    emissive_strength = scalar(material.emissive_strength, InvalidPbrMaterial, minimum=0)
    emitted = tuple(f*t*emissive_strength for f, t in zip(emissive_factor, emission[:3]))
    if not all(math.isfinite(c) for c in emitted):
        raise InvalidPbrMaterial('Emission multiplication overflows binary64.')
    scale = 1.0 if material.normal_texture is None else material.normal_texture.normal_scale
    return PbrMaterialParameters(tuple(factor[c]*base[c]*vertex[c] for c in range(3)),
        metallic, roughness, weight, f0, emitted, factor[3]*base[3]*vertex[3],
        material.alpha_mode, material.alpha_cutoff, material.double_sided,
        None if material.normal_texture is None else normal[:3], scale, material)


def resolve_surface_material(material, samples, surface, *, vertex_color=None, policy=MATERIAL_POLICY):
    """Borrow existing hit/material identity and enforce a required COLOR_0 input."""
    from drt_backend.raytracing.tracing.surface_evaluation import SurfaceAttributes
    if (type(surface) is not SurfaceAttributes or type(material) is not MaterialData
            or material.material_index != surface.primitive.material_index):
        raise InvalidPbrMaterial('Material/surface primitive association differs.')
    return resolve_material(material, samples, vertex_color=vertex_color,
        vertex_color_required='COLOR_0' in surface.primitive.attribute_accessors, policy=policy)
