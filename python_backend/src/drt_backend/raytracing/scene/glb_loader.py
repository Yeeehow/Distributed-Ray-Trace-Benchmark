"""Validate GLB framing and delegate glTF document parsing to pygltflib."""

from dataclasses import dataclass
import json
from os import PathLike
from pathlib import Path
import struct
from typing import Any, NoReturn

from pygltflib import GLTF2

from drt_backend.raytracing.common.errors import GlbFileError, GlbValidationError, UnsupportedFeatureError, require_int


# These extensions do not change the geometry byte layout. Semantics remain deferred.
PRESERVED_REQUIRED_EXTENSIONS = frozenset({
    "KHR_lights_punctual", "KHR_materials_specular", "KHR_materials_emissive_strength",
})


@dataclass(frozen=True, slots=True)
class GlbAsset:
    """Parsed document and owned immutable bytes; arrays may safely outlive this wrapper.

    Document/raw_document are metadata, not a complete immutable scene graph.
    Callers should treat them as read-only. Every byte-view request rechecks bounds.
    """

    document: GLTF2
    binary: bytes
    source_path: Path | None = None
    raw_document: dict[str, Any] | None = None
    file_size: int = 0
    json_chunk_length: int = 0
    bin_chunk_length: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.binary, bytes):
            raise TypeError("GlbAsset.binary must be owned immutable bytes.")

    def buffer_view(self, index: int) -> memoryview:
        """Return a read-only slice, bounded by the declared buffer, excluding padding."""
        views = self.document.bufferViews
        index = require_int(index, "BufferView index")
        if index >= len(views):
            raise GlbValidationError(f"BufferView index {index} is out of range.")
        view = views[index]
        buffer_index = require_int(view.buffer, f"BufferView {index} buffer")
        if buffer_index >= len(self.document.buffers):
            raise GlbValidationError(f"BufferView {index} refers to a missing buffer.")
        buffer = self.document.buffers[buffer_index]
        if buffer_index != 0 or buffer.uri is not None:
            raise UnsupportedFeatureError("Only the embedded GLB buffer is supported.")
        if "EXT_meshopt_compression" in (view.extensions or {}):
            raise UnsupportedFeatureError("Compressed BufferViews are not decoded.")
        if view.target is not None:
            require_int(view.target, "BufferView target")
        if view.target not in {None, 34962, 34963}:
            raise GlbValidationError("BufferView target must be ARRAY_BUFFER or ELEMENT_ARRAY_BUFFER.")
        buffer_length = require_int(buffer.byteLength, "Buffer byteLength", 1)
        offset = require_int(view.byteOffset, f"BufferView {index} byteOffset")
        length = require_int(view.byteLength, f"BufferView {index} byteLength", 1)
        if buffer_length > len(self.binary) or offset + length > buffer_length:
            raise GlbValidationError(f"BufferView {index} exceeds its declared buffer.")
        if view.byteStride is not None:
            stride = require_int(view.byteStride, f"BufferView {index} byteStride", 4)
            if stride > 252 or stride % 4:
                raise GlbValidationError("byteStride must be a multiple of 4 in [4, 252].")
        return memoryview(self.binary)[offset:offset + length]


def _container(data: bytes) -> tuple[bytes, bytes]:
    if len(data) < 20:
        raise GlbValidationError("Truncated GLB header or JSON chunk header.")
    magic, version, length = struct.unpack_from("<4sII", data)
    if magic != b"glTF":
        raise GlbValidationError("Invalid GLB magic.")
    if version != 2:
        raise UnsupportedFeatureError(f"GLB version {version} is not supported; expected 2.")
    if length != len(data):
        raise GlbValidationError("GLB declared file length differs from actual bytes.")
    position = 12
    json_chunk = None
    binary = b""
    bin_seen = False
    chunk_index = 0
    while position < length:
        if length - position < 8:
            raise GlbValidationError("Truncated GLB chunk header.")
        chunk_length, chunk_type = struct.unpack_from("<II", data, position)
        position += 8
        if chunk_length % 4 or chunk_length > length - position:
            raise GlbValidationError("GLB chunk length/alignment exceeds the container.")
        payload = data[position:position + chunk_length]
        if chunk_index == 0 and chunk_type != 0x4E4F534A:
            raise GlbValidationError("The first GLB chunk must be JSON.")
        if chunk_type == 0x4E4F534A:
            if chunk_index != 0 or json_chunk is not None:
                raise GlbValidationError("A GLB must have exactly one leading JSON chunk.")
            json_chunk = payload
        elif chunk_type == 0x004E4942:
            if chunk_index != 1 or bin_seen:
                raise GlbValidationError("The optional BIN chunk must occur once, second.")
            binary, bin_seen = payload, True
        # Unknown chunk types are ignored as required by the GLB format.
        position += chunk_length
        chunk_index += 1
    if not json_chunk:
        raise GlbValidationError("Missing JSON content.")
    return json_chunk, binary


def _reject_constant(value: str) -> NoReturn:
    raise GlbValidationError(f"Non-finite JSON constant {value} is invalid.")


def _validate_json_metadata(raw: dict[str, Any]) -> None:
    """Check byte/index integers before a binding library can coerce their types."""
    sections = {
        "buffers": ({"byteLength"}, {"byteLength"}),
        "bufferViews": ({"buffer", "byteLength"}, {"buffer", "byteOffset", "byteLength", "byteStride"}),
        "accessors": ({"componentType", "count", "type"}, {"bufferView", "byteOffset", "componentType", "count"}),
    }
    for section, (required, integer_fields) in sections.items():
        entries = raw.get(section, [])
        if not isinstance(entries, list):
            raise GlbValidationError(f"{section} must be an array.")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not required.issubset(entry):
                raise GlbValidationError(f"{section}[{index}] lacks required metadata.")
            for field in integer_fields & entry.keys():
                minimum = 1 if field in {"count", "byteLength"} else 0
                require_int(entry[field], f"{section}[{index}].{field}", minimum)
            if "normalized" in entry and type(entry["normalized"]) is not bool:
                raise GlbValidationError("Accessor normalized must be a boolean.")
    meshes = raw.get("meshes", [])
    if not isinstance(meshes, list):
        raise GlbValidationError("meshes must be an array.")
    for mesh in meshes:
        if not isinstance(mesh, dict) or not isinstance(mesh.get("primitives"), list) or not mesh["primitives"]:
            raise GlbValidationError("A mesh must contain a nonempty primitives array.")
        for primitive in mesh["primitives"]:
            if not isinstance(primitive, dict) or not isinstance(primitive.get("attributes"), dict) or not primitive["attributes"]:
                raise GlbValidationError("A primitive must contain an attributes object.")
            for field in {"indices", "material", "mode"} & primitive.keys():
                require_int(primitive[field], f"Primitive {field}")
            for name, value in primitive["attributes"].items():
                require_int(value, f"Attribute {name} accessor")
    required_extensions = raw.get("extensionsRequired", [])
    used_extensions = raw.get("extensionsUsed", [])
    for label, extensions in (("extensionsRequired", required_extensions), ("extensionsUsed", used_extensions)):
        if not isinstance(extensions, list) or any(not isinstance(e, str) for e in extensions):
            raise GlbValidationError(f"{label} must be an array of extension names.")
    if not set(required_extensions).issubset(used_extensions):
        raise GlbValidationError("Required extensions must also appear in extensionsUsed.")
    unsupported = set(required_extensions) - PRESERVED_REQUIRED_EXTENSIONS
    if unsupported:
        raise UnsupportedFeatureError(f"Unsupported required extensions: {', '.join(sorted(unsupported))}.")


def load_glb(path: str | PathLike[str]) -> GlbAsset:
    """Read a GLB 2.0 file without resolving external resources or benchmark identity.

    Missing/unreadable input raises GlbFileError; invalid bytes raise
    GlbValidationError; unsupported versions/buffers/required extensions raise
    UnsupportedFeatureError. No fixed asset path or scene hash is imposed here.
    """
    try:
        source = Path(path).resolve(strict=True)
        data = source.read_bytes()
    except OSError as error:
        raise GlbFileError(f"Cannot read GLB: {error.strerror or type(error).__name__}.") from error
    json_chunk, binary = _container(data)
    try:
        text = json_chunk.decode("utf-8")
        raw = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GlbValidationError("GLB JSON chunk is not a valid UTF-8 JSON document.") from error
    if not isinstance(raw, dict):
        raise GlbValidationError("The glTF JSON root must be an object.")
    _validate_json_metadata(raw)
    metadata = raw.get("asset")
    if not isinstance(metadata, dict) or metadata.get("version") != "2.0":
        raise UnsupportedFeatureError("Only glTF asset.version 2.0 is supported.")
    if metadata.get("minVersion", "2.0") != "2.0":
        raise UnsupportedFeatureError("The glTF minimum version is outside the supported 2.0 subset.")
    try:
        # pygltflib owns document/schema binding; our framing checks avoid its lax GLB bounds.
        document = GLTF2.from_json(text, infer_missing=True)
        document.set_binary_blob(binary)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise GlbValidationError("pygltflib could not parse the glTF document.") from error
    if len(document.buffers) > 1 or any(b.uri is not None for b in document.buffers):
        raise UnsupportedFeatureError("External/data-URI/multiple buffers are outside M2.1.")
    if document.buffers:
        declared = require_int(document.buffers[0].byteLength, "Buffer byteLength", 1)
        if declared > len(binary) or len(binary) - declared > 3:
            raise GlbValidationError("BIN length does not match buffer byteLength plus up to 3 padding bytes.")
        if any(binary[declared:]):
            raise GlbValidationError("BIN padding must contain zero bytes.")
    elif binary:
        raise GlbValidationError("BIN payload has no corresponding buffer declaration.")
    asset = GlbAsset(document, binary, source, raw, len(data), len(json_chunk), len(binary))
    for index in range(len(document.bufferViews)):
        asset.buffer_view(index)
    return asset
