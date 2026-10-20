"""Scene-linear composite to display RGB8; independent of normal diagnostics."""

import math
from numbers import Real

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import RenderValidationError


def linear_rgb(value: object) -> NDArray[np.float64]:
    """Validate a real finite nonnegative triple without clipping HDR."""
    try:
        source = np.asarray(value)
        if source.shape != (3,) or source.dtype.kind not in "iuf":
            raise RenderValidationError("Linear RGB requires three real components.")
        with np.errstate(over="raise", invalid="raise"):
            result = np.asarray(source, dtype=np.float64)
        if not np.isfinite(result).all() or np.any(result < 0):
            raise RenderValidationError("Linear RGB must be finite and nonnegative.")
    except (ValueError, TypeError, OverflowError, FloatingPointError) as error:
        raise RenderValidationError("Invalid finite nonnegative linear RGB.") from error
    return result


def exposure_scale(exposure_ev: float, tone_mapping: str) -> float:
    """Reject overflow and an unrepresentable zero exposure multiplier."""
    if not isinstance(tone_mapping, str) or tone_mapping not in ("clamp", "reinhard"):
        raise RenderValidationError("Tone mapping must be clamp or reinhard.")
    if isinstance(exposure_ev, bool) or not isinstance(exposure_ev, Real):
        raise RenderValidationError("Exposure EV must be finite and real.")
    try:
        ev = float(exposure_ev)
        scale = 2.0 ** ev
    except (ValueError, OverflowError) as error:
        raise RenderValidationError("Exposure exceeds float64 range.") from error
    if not math.isfinite(ev) or not math.isfinite(scale) or scale == 0:
        raise RenderValidationError("Exposure multiplier must be finite and positive.")
    return scale


def map_linear_rgb(color: object, *, exposure_ev: float = 0,
                   tone_mapping: str = "clamp") -> NDArray[np.uint8]:
    """Expose once, map once, transfer once, then half-up quantize three bytes."""
    rgb = linear_rgb(color)
    scale = exposure_scale(exposure_ev, tone_mapping)
    result = np.empty(3, dtype=np.uint8)
    for index, channel in enumerate(rgb):
        exposed = float(channel) * scale
        if not math.isfinite(exposed) or (channel > 0 and exposed == 0):
            raise RenderValidationError("Exposed radiance exceeds float64 range.")
        if tone_mapping == "clamp":
            mapped = min(exposed, 1.0)
        else:
            # The reciprocal form cannot overflow 1+x at large HDR magnitudes.
            mapped = exposed / (1.0 + exposed) if exposed <= 1 else 1.0 / (1.0 + 1.0 / exposed)
        srgb = 12.92 * mapped if mapped <= 0.0031308 else 1.055 * mapped ** (1.0 / 2.4) - 0.055
        result[index] = math.floor(min(1.0, max(0.0, srgb)) * 255.0 + 0.5)
    return result
