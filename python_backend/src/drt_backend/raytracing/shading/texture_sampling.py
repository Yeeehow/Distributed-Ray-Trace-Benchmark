"""Core TEXCOORD_0 base-color sampling in linear RGB and linear alpha."""

from collections.abc import Mapping
from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common._metadata import number
from drt_backend.raytracing.common.errors import MetadataValidationError, RenderValidationError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.resources.image_decoding import DecodedImage, TextureContext
from drt_backend.raytracing.model.textures import SamplerData, TextureData, TextureSlot


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    intent: str = "minification"
    filter_override: str | None = None

    def __post_init__(self) -> None:
        if self.intent not in ("minification", "magnification"):
            raise RenderValidationError("Sampling intent must be minification or magnification.")
        if self.filter_override not in (None, "nearest", "bilinear"):
            raise RenderValidationError("Texture filter override must be nearest or bilinear.")


def _validate_slot_extensions(slot: TextureSlot) -> None:
    if set(slot.extensions) - {"KHR_texture_transform"}:
        raise UnsupportedFeatureError("Base-color texture slot extension is unsupported.")
    if "KHR_texture_transform" not in slot.extensions:
        return
    transform = slot.extensions["KHR_texture_transform"]
    if not isinstance(transform, Mapping) or set(transform) - {"offset", "scale", "rotation", "texCoord"}:
        raise UnsupportedFeatureError("Unsupported texture-transform metadata.")
    coordinate = require_int(transform.get("texCoord", 0), "Texture transform texCoord", error=MetadataValidationError)
    pairs = []
    for key, default in (("offset", (0, 0)), ("scale", (1, 1))):
        value = transform.get(key, default)
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise MetadataValidationError(f"Texture transform {key} must have two components.")
        pairs.append(tuple(number(component, f"Texture transform {key}") for component in value))
    if coordinate != 0 or pairs != [(0, 0), (1, 1)] or number(transform.get("rotation", 0), "Texture rotation") != 0:
        raise UnsupportedFeatureError("Nonidentity texture transforms/alternate UV sets are not evaluated.")


def resolve_base_color_slot(context: TextureContext, slot: TextureSlot) -> tuple[TextureData, SamplerData]:
    if not isinstance(context, TextureContext) or not isinstance(slot, TextureSlot):
        raise RenderValidationError("Texture sampling requires TextureContext and TextureSlot.")
    context.require_open()
    if slot.semantic != "base_color" or slot.transfer.get("RGB") != "sRGB" or slot.transfer.get("A") != "linear":
        raise UnsupportedFeatureError("Only sRGB base-color RGB with linear alpha is evaluated.")
    require_int(slot.texcoord, "Texture texcoord", error=MetadataValidationError)
    if slot.texcoord != 0:
        raise UnsupportedFeatureError("Only TEXCOORD_0 is sampled.")
    _validate_slot_extensions(slot)
    index = require_int(slot.texture_index, "Texture index", error=MetadataValidationError)
    metadata = context.shaded.metadata
    if index >= len(metadata.textures):
        raise MetadataValidationError("Texture index is out of range.")
    texture = metadata.textures[index]
    if texture.texture_index != index or texture.extensions:
        raise UnsupportedFeatureError("Texture identity/extension semantics are unsupported.")
    image_index = require_int(texture.source_image_index, "Texture image index", error=MetadataValidationError)
    if image_index >= len(metadata.images) or metadata.images[image_index].image_index != image_index:
        raise MetadataValidationError("Texture image reference is invalid.")
    if texture.sampler_index is not None:
        sampler_index = require_int(texture.sampler_index, "Texture sampler index", error=MetadataValidationError)
        if sampler_index >= len(metadata.samplers) or metadata.samplers[sampler_index].sampler_index != sampler_index:
            raise MetadataValidationError("Texture sampler reference is invalid.")
    sampler = metadata.sampler_for_texture(index)
    if sampler.extensions:
        raise UnsupportedFeatureError("Sampler extension semantics are unsupported.")
    return texture, sampler


def effective_filter(sampler: SamplerData, policy: SamplingPolicy) -> str:
    """Fixed sampling intent, level-zero fallback; no footprint or mipmap LOD."""
    if not isinstance(policy, SamplingPolicy) or not isinstance(sampler, SamplerData):
        raise RenderValidationError("Filter selection requires SamplerData and SamplingPolicy.")
    for label, value, allowed in (("magFilter", sampler.mag_filter, {9728, 9729}),
                                  ("minFilter", sampler.min_filter, {9728, 9729, 9984, 9985, 9986, 9987})):
        if value is not None:
            require_int(value, label, error=MetadataValidationError)
            if value not in allowed:
                raise MetadataValidationError(f"Invalid sampler {label}.")
    for value in (sampler.wrap_s, sampler.wrap_t):
        require_int(value, "Texture wrap", error=MetadataValidationError)
        if value not in (10497, 33071, 33648):
            raise MetadataValidationError("Invalid sampler wrap mode.")
    if policy.filter_override is not None:
        return policy.filter_override
    selected = sampler.min_filter if policy.intent == "minification" else sampler.mag_filter
    return "nearest" if selected in (9728, 9984, 9986) else "bilinear"


def _address(index: int, size: int, wrap: int) -> int:
    if wrap == 10497:
        return index % size
    if wrap == 33071:
        return min(size - 1, max(0, index))
    index %= 2 * size
    return index if index < size else 2 * size - 1 - index


def _linear_texel(image: DecodedImage, x: int, y: int, sampler: SamplerData) -> NDArray[np.float64]:
    rgba = image.pixels[_address(y, image.height, sampler.wrap_t), _address(x, image.width, sampler.wrap_s)].astype(np.float64) / 255.0
    rgb = rgba[:3]
    rgba[:3] = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return rgba


def sample_base_color_texture(context: TextureContext, slot: TextureSlot, uv: object,
                              policy: SamplingPolicy = SamplingPolicy()) -> NDArray[np.float64]:
    """Sample a prepared image; return independent read-only linear RGBA."""
    texture, sampler = resolve_base_color_slot(context, slot)
    filtering = effective_filter(sampler, policy)
    try:
        source = np.asarray(uv)
        if source.shape != (2,) or source.dtype.kind not in "iuf":
            raise RenderValidationError("Texture UV must be a finite numeric pair.")
        with np.errstate(over="raise", invalid="raise"):
            coordinates = np.asarray(source, dtype=np.float64)
        if not np.isfinite(coordinates).all():
            raise RenderValidationError("Texture UV must be finite.")
    except (ValueError, TypeError, OverflowError, FloatingPointError) as error:
        raise RenderValidationError("Texture UV cannot be represented in float64.") from error
    image = context.image(texture.source_image_index)
    scaled = (float(coordinates[0]) * image.width, float(coordinates[1]) * image.height)
    if any(not math.isfinite(value) or abs(value) > 2**51 for value in scaled):
        raise RenderValidationError("UV texel coordinates exceed float64 half-texel addressing resolution.")
    if filtering == "nearest":
        result = _linear_texel(image, math.floor(scaled[0]), math.floor(scaled[1]), sampler)
    else:
        x, y = scaled[0] - 0.5, scaled[1] - 0.5
        ix, iy = math.floor(x), math.floor(y)
        fx, fy = x - ix, y - iy
        a = _linear_texel(image, ix, iy, sampler)
        b = _linear_texel(image, ix + 1, iy, sampler)
        c = _linear_texel(image, ix, iy + 1, sampler)
        d = _linear_texel(image, ix + 1, iy + 1, sampler)
        result = (1-fx)*(1-fy)*a + fx*(1-fy)*b + (1-fx)*fy*c + fx*fy*d
    if not np.isfinite(result).all():
        raise RenderValidationError("Texture sampling produced nonfinite RGBA.")
    return np.frombuffer(result.tobytes(), dtype=np.float64)
