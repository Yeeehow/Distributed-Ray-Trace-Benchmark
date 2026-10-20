"""Immutable numerical viewport and explicit float64 strip partition settings."""

from dataclasses import dataclass

from drt_backend.raytracing.common.errors import RenderValidationError, require_int


# Match the existing protocol's allocation bounds, without its 16:9 restriction.
MAX_DIMENSION = 16_384
MAX_PIXELS = 67_108_864


@dataclass(frozen=True, slots=True)
class RenderConfig:
    """M3.0 settings only; SIMD widths model partition sizes, not CPU execution.

    Dimensions are Python integers (not bool), at most 16384 per axis and
    67108864 pixels total. No image-sized ray storage is allocated here.
    Large positive vector_groups are safe: effective strips are axis-bounded,
    and nominal lengths are computed with Python's non-overflowing integers.
    """

    width: int
    height: int
    camera_node_index: int | None = None
    strip_orientation: str = "horizontal"
    simd_register_bits: int = 128
    vector_groups: int = 16

    def __post_init__(self) -> None:
        for label in ("width", "height"):
            value = require_int(getattr(self, label), label, 1, RenderValidationError)
            if value > MAX_DIMENSION:
                raise RenderValidationError(f"{label} must be <= {MAX_DIMENSION}.")
        if self.width * self.height > MAX_PIXELS:
            raise RenderValidationError(f"Viewport must contain <= {MAX_PIXELS} pixels.")
        if self.camera_node_index is not None:
            require_int(self.camera_node_index, "camera_node_index", error=RenderValidationError)
        if not isinstance(self.strip_orientation, str) or self.strip_orientation not in ("horizontal", "vertical"):
            raise RenderValidationError("strip_orientation must be horizontal or vertical.")
        bits = require_int(self.simd_register_bits, "simd_register_bits", 1, RenderValidationError)
        if bits not in (128, 256, 512) or bits % 64:
            raise RenderValidationError("simd_register_bits must be 128, 256 or 512, divisible by 64.")
        require_int(self.vector_groups, "vector_groups", 1, RenderValidationError)

    @property
    def vector_lanes(self) -> int:
        return self.simd_register_bits // 64

    @property
    def nominal_strip_length(self) -> int:
        return self.vector_lanes * self.vector_groups
