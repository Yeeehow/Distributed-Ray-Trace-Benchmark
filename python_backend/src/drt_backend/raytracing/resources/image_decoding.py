"""Bounded embedded PNG/JPEG decoding and scene-local immutable pixel storage."""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from io import BytesIO
import warnings

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageFile

from drt_backend.raytracing.common.errors import MetadataValidationError, RenderValidationError, UnsupportedFeatureError, require_int
from drt_backend.raytracing.shading.material_evaluation import evaluate_base_material
from drt_backend.raytracing.resources.shaded_context import ShadedContext
from drt_backend.raytracing.model.textures import EmbeddedImageData


class ImageDecodeError(MetadataValidationError):
    """An encoded image cannot produce a complete supported RGBA8 image."""


class ImageResourceLimitError(ImageDecodeError):
    """Encoded size, dimensions, pixels or retained cache exceeds its bound."""


@dataclass(frozen=True, slots=True)
class ImageLimits:
    """Limits may be lowered, but never raised above the approved M4.2 caps."""

    max_encoded_bytes: int = 64 * 1024 * 1024
    max_dimension: int = 8192
    max_pixels: int = 16_777_216
    max_cache_bytes: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        caps = (64 * 1024 * 1024, 8192, 16_777_216, 128 * 1024 * 1024)
        for item, cap in zip(fields(self), caps, strict=True):
            value = require_int(getattr(self, item.name), item.name, 1, ImageResourceLimitError)
            if value > cap:
                raise ImageResourceLimitError(f"{item.name} exceeds the approved limit.")


@dataclass(frozen=True, slots=True)
class _ImageHeader:
    width: int
    height: int
    format: str
    mode: str

    @property
    def rgba_bytes(self) -> int:
        return 4 * self.width * self.height


@dataclass(frozen=True, slots=True, eq=False)
class DecodedImage:
    """Obtain from TextureContext: pixels retain independent immutable bytes."""

    image_index: int
    width: int
    height: int
    pixels: NDArray[np.uint8]


_DECODER_ERRORS = (OSError, ValueError, SyntaxError, EOFError,
                   Image.DecompressionBombWarning, Image.DecompressionBombError)


def _validate_resource(resource: EmbeddedImageData, limits: ImageLimits) -> None:
    if not isinstance(resource, EmbeddedImageData):
        raise ImageDecodeError("Decoder requires an EmbeddedImageData resource.")
    if (not isinstance(resource.data, memoryview) or not resource.data.readonly
            or resource.data.ndim != 1 or resource.data.itemsize != 1
            or resource.byte_length != resource.data.nbytes):
        raise ImageDecodeError("Embedded image must reference matching read-only encoded bytes.")
    if not 0 < resource.byte_length <= limits.max_encoded_bytes:
        raise ImageResourceLimitError("Embedded image encoded byte count exceeds its limit.")
    if resource.mime_type not in ("image/png", "image/jpeg"):
        raise UnsupportedFeatureError("Only embedded PNG and JPEG pixels are supported.")
    if resource.extensions:
        raise UnsupportedFeatureError("Image extension semantics are not evaluated.")
    if Image.MAX_IMAGE_PIXELS is None or ImageFile.LOAD_TRUNCATED_IMAGES:
        raise ImageDecodeError("Pillow safety settings must retain bomb checks and strict truncation handling.")


def _inspect_open_image(image: Image.Image, resource: EmbeddedImageData, limits: ImageLimits) -> _ImageHeader:
    expected = "PNG" if resource.mime_type == "image/png" else "JPEG"
    if image.format != expected:
        raise ImageDecodeError("Embedded image MIME does not match its decoded format.")
    width, height = image.size
    if (not 0 < width <= limits.max_dimension or not 0 < height <= limits.max_dimension
            or width * height > limits.max_pixels):
        raise ImageResourceLimitError("Embedded image dimensions/pixel count exceed their limits.")
    if 4 * width * height > limits.max_cache_bytes:
        raise ImageResourceLimitError("Image RGBA8 storage exceeds the retained cache limit.")
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
        raise UnsupportedFeatureError("Animated images are not supported.")
    # Pillow can expose 16-bit RGB PNG as RGB while downconverting on load.
    # Read only the standard IHDR precision field; Pillow still owns all decoding.
    if expected == "PNG" and (len(resource.data) <= 24 or resource.data[24] > 8):
        raise UnsupportedFeatureError("PNG precision above eight bits is not supported.")
    allowed = {"1", "L", "LA", "P", "RGB", "RGBA"} if expected == "PNG" else {"L", "RGB"}
    if image.mode not in allowed or (expected == "JPEG" and getattr(image, "bits", 8) != 8):
        raise UnsupportedFeatureError("Image color mode/precision is outside the RGBA8 subset.")
    return _ImageHeader(width, height, expected, image.mode)


def _inspect_image(resource: EmbeddedImageData, limits: ImageLimits) -> _ImageHeader:
    """Validate header/projected allocation and container integrity without pixel load."""
    _validate_resource(resource, limits)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with BytesIO(resource.data) as stream, Image.open(stream, formats=("PNG", "JPEG")) as image:
                header = _inspect_open_image(image, resource, limits)
                image.verify()
                return header
    except (ImageDecodeError, UnsupportedFeatureError):
        raise
    except _DECODER_ERRORS as error:
        raise ImageDecodeError(f"Embedded image {resource.image_index} header/integrity failure.") from error


def _decode_image(resource: EmbeddedImageData, header: _ImageHeader, limits: ImageLimits) -> DecodedImage:
    """Load all pixels before publishing; no metadata-driven rotation or transfer."""
    _validate_resource(resource, limits)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with BytesIO(resource.data) as stream, Image.open(stream, formats=("PNG", "JPEG")) as image:
                if _inspect_open_image(image, resource, limits) != header:
                    raise ImageDecodeError("Embedded image header changed during preparation.")
                image.load()
                with image.convert("RGBA") as rgba:
                    encoded_pixels = rgba.tobytes()
        if len(encoded_pixels) != header.rgba_bytes:
            raise ImageDecodeError("Decoded RGBA8 buffer has an invalid length.")
        pixels = np.frombuffer(encoded_pixels, dtype=np.uint8).reshape(header.height, header.width, 4)
        return DecodedImage(resource.image_index, header.width, header.height, pixels)
    except (ImageDecodeError, UnsupportedFeatureError):
        raise
    except _DECODER_ERRORS as error:
        raise ImageDecodeError(f"Embedded image {resource.image_index} pixel decode failure.") from error


class TextureContext:
    """Per-ShadedContext cache. Explicit prepare precedes all sampling.

    Preparation publishes a batch only after all new decodes succeed. Sampling
    reads image() and never calls the decoder. close() releases this owner's
    references; externally retained immutable images can outlive the context.
    No locking, global cache or renderer/acceleration ownership is introduced.
    """

    __slots__ = ("_shaded", "_limits", "_images", "_retained_bytes", "_closed")

    def __init__(self, shaded: ShadedContext, limits: ImageLimits = ImageLimits()) -> None:
        if not isinstance(shaded, ShadedContext) or not isinstance(limits, ImageLimits):
            raise RenderValidationError("TextureContext requires a ShadedContext and ImageLimits.")
        self._shaded, self._limits = shaded, limits
        self._images: dict[int, DecodedImage] = {}
        self._retained_bytes, self._closed = 0, False

    @property
    def shaded(self) -> ShadedContext:
        return self._shaded

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def loaded_image_indices(self) -> tuple[int, ...]:
        return tuple(sorted(self._images))

    def require_open(self) -> None:
        if self._closed:
            raise RenderValidationError("TextureContext is closed.")

    def _resource(self, image_index: int) -> EmbeddedImageData:
        self.require_open()
        require_int(image_index, "Image index", error=ImageDecodeError)
        images = self.shaded.metadata.images
        if image_index >= len(images) or images[image_index].image_index != image_index:
            raise ImageDecodeError("Image index does not match its metadata resource.")
        return images[image_index]

    def prepare(self, image_indices: Iterable[int] | None = None) -> None:
        """Default prepares active base-color images only; repeat indices share pixels."""
        self.require_open()
        if image_indices is None:
            image_indices = []
            for definition in self.shaded.scene.mesh_definitions.values():
                for primitive in definition.primitives:
                    slot = evaluate_base_material(self.shaded, primitive).base_color_texture
                    if slot is not None:
                        image_indices.append(self.shaded.metadata.textures[slot.texture_index].source_image_index)
        requested = set()
        for index in image_indices:
            self._resource(index)
            requested.add(index)
        pending = sorted(requested - self._images.keys())
        headers = {index: _inspect_image(self._resource(index), self._limits) for index in pending}
        projected = self._retained_bytes + sum(header.rgba_bytes for header in headers.values())
        if projected > self._limits.max_cache_bytes:
            raise ImageResourceLimitError("Aggregate retained RGBA8 cache exceeds its limit.")
        decoded = {index: _decode_image(self._resource(index), headers[index], self._limits) for index in pending}
        self._images.update(decoded)
        self._retained_bytes = projected

    def image(self, image_index: int) -> DecodedImage:
        self._resource(image_index)
        if image_index not in self._images:
            raise RenderValidationError("Texture image is not prepared; sampling never decodes implicitly.")
        return self._images[image_index]

    def close(self) -> None:
        self._images.clear()
        self._retained_bytes, self._closed = 0, True

    def __enter__(self) -> "TextureContext":
        self.require_open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
