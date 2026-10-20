"""Texture reference chains and read-only encoded image bytes; no pixel decoding."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from drt_backend.raytracing.common._metadata import extensions, index, name, number, object_data, optional, retained
from drt_backend.raytracing.common.errors import MetadataValidationError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.scene.glb_loader import GlbAsset


@dataclass(frozen=True, slots=True)
class SamplerData:
    sampler_index: int | None
    name: str | None
    mag_filter: int | None
    min_filter: int | None
    wrap_s: int
    wrap_t: int
    source: Mapping[str, Any]
    extensions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EmbeddedImageData:
    image_index: int
    name: str | None
    mime_type: str
    buffer_view_index: int
    byte_length: int
    data: memoryview
    source: Mapping[str, Any]
    extensions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TextureData:
    texture_index: int
    name: str | None
    source_image_index: int
    sampler_index: int | None
    source: Mapping[str, Any]
    extensions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TextureSlot:
    texture_index: int
    texcoord: int
    semantic: str
    channels: Mapping[str, str]
    transfer: Mapping[str, str]
    normal_scale: float | None
    occlusion_strength: float | None
    source: Mapping[str, Any]
    extensions: Mapping[str, Any]


# A transfer function belongs to a slot/channel, not to the shared image resource.
_SLOT_SEMANTICS = {
    "base_color": ({"RGB": "base_color", "A": "opacity"}, {"RGB": "sRGB", "A": "linear"}),
    "metallic_roughness": ({"G": "roughness", "B": "metallic"}, {"G": "linear", "B": "linear"}),
    "normal": ({"RGB": "tangent_space_normal"}, {"RGB": "linear"}),
    "occlusion": ({"R": "occlusion"}, {"R": "linear"}),
    "emissive": ({"RGB": "emissive"}, {"RGB": "sRGB"}),
    "specular": ({"A": "specular_strength"}, {"A": "linear"}),
    "specular_color": ({"RGB": "specular_color"}, {"RGB": "sRGB"}),
}


def parse_sampler(source: Mapping[str, Any], sampler_index: int | None) -> SamplerData:
    def enum(key: str, choices: set[int], default: int | None = None) -> int | None:
        value = source.get(key, default)
        if value is None and key not in source:
            return None
        result = require_int(value, f"Sampler {key}", error=MetadataValidationError)
        if result not in choices:
            raise MetadataValidationError(f"Sampler {key} has an invalid enum value.")
        return result

    return SamplerData(
        sampler_index, name(source), enum("magFilter", {9728, 9729}),
        enum("minFilter", {9728, 9729, 9984, 9985, 9986, 9987}),
        enum("wrapS", {33071, 33648, 10497}, 10497), enum("wrapT", {33071, 33648, 10497}, 10497),
        retained(source), extensions(source),
    )


def parse_images(asset: GlbAsset, sources: Sequence[Mapping[str, Any]]) -> tuple[EmbeddedImageData, ...]:
    result = []
    for image_index, source in enumerate(sources):
        view_value, uri = optional(source, "bufferView"), optional(source, "uri")
        if uri is not None:
            if view_value is not None:
                raise MetadataValidationError("Image uri and bufferView are mutually exclusive.")
            raise UnsupportedFeatureError("Only bufferView-embedded images are supported; URI images are deferred.")
        view_index = index(view_value, len(asset.document.bufferViews), "Image bufferView index")
        mime = source.get("mimeType")
        if not isinstance(mime, str) or mime not in {"image/png", "image/jpeg"}:
            raise MetadataValidationError("Embedded image mimeType must be image/png or image/jpeg.")
        view = asset.document.bufferViews[view_index]
        if view.byteStride is not None or view.target is not None:
            raise MetadataValidationError("Image bufferView cannot have vertex/index target or byteStride.")
        data = asset.buffer_view(view_index)
        # MIME/signature consistency only; chunks, entropy data and pixels are not decoded.
        if mime == "image/png":
            signature_matches = bytes(data[:8]) == b"\x89PNG\r\n\x1a\n"
        else:
            marker = 0
            while marker < len(data) and data[marker] == 0xFF:
                marker += 1  # JPEG permits FF fill bytes before a marker.
            signature_matches = 0 < marker < len(data) and data[marker] == 0xD8
        if not signature_matches:
            raise MetadataValidationError(f"Embedded image {image_index} signature does not match its MIME type.")
        result.append(EmbeddedImageData(image_index, name(source), mime, view_index, len(data), data,
                                        retained(source), extensions(source)))
    return tuple(result)


def parse_textures(sources: Sequence[Mapping[str, Any]], image_count: int,
                   sampler_count: int) -> tuple[TextureData, ...]:
    result = []
    for texture_index, source in enumerate(sources):
        image_value = optional(source, "source")
        if image_value is None:
            raise UnsupportedFeatureError("A texture without a core image source is not supported.")
        image_index = index(image_value, image_count, "Texture image index")
        sampler_value = optional(source, "sampler")
        sampler_index = index(sampler_value, sampler_count, "Texture sampler index") if sampler_value is not None else None
        result.append(TextureData(texture_index, name(source), image_index, sampler_index,
                                  retained(source), extensions(source)))
    return tuple(result)


def texture_slot(value: object, semantic: str, texture_count: int) -> TextureSlot | None:
    if value is None:
        return None
    source = object_data(value, f"{semantic} texture slot")
    texture_index = index(source.get("index"), texture_count, f"{semantic} texture index")
    texcoord = require_int(source.get("texCoord", 0), "Texture texCoord", error=MetadataValidationError)
    if texcoord != 0:
        raise UnsupportedFeatureError(f"Texture TEXCOORD_{texcoord} is not exposed by the geometry API.")
    channels, transfer = _SLOT_SEMANTICS[semantic]
    scale = number(source.get("scale", 1), "Normal texture scale") if semantic == "normal" else None
    strength = number(source.get("strength", 1), "Occlusion strength", 0, 1) if semantic == "occlusion" else None
    return TextureSlot(texture_index, texcoord, semantic, MappingProxyType(channels), MappingProxyType(transfer),
                       scale, strength, retained(source), extensions(source))
