"""Shared punctual-light preparation and photometric RGB sampling.

Prepared constants borrow metadata identity and own only small immutable vectors.
No traversal, texture sampling, alpha multiplication or node transforms occur here.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import math
from numbers import Real

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.rendering.color_output import linear_rgb
from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, UnsupportedFeatureError
from drt_backend.raytracing.model.lights import LightData
from drt_backend.raytracing.resources.shaded_context import ShadedContext
from drt_backend.raytracing.tracing.surface_queries import AcceptedSurface


class LightingNumericalError(RenderValidationError):
    """A photometric light sample cannot be represented in float64."""


def _vector(value: object, *, unit: bool = False) -> NDArray[np.float64]:
    try:
        source = np.asarray(value)
        if source.shape != (3,) or source.dtype.kind not in "iuf":
            raise RenderValidationError("Light/surface vector requires three real components.")
        vector = np.asarray(source, dtype=np.float64)
        if not np.isfinite(vector).all():
            raise RenderValidationError("Light/surface vector must be finite.")
        if unit and not math.isclose(math.hypot(*vector), 1, rel_tol=1e-12, abs_tol=1e-12):
            raise RenderValidationError("Light/surface direction must be normalized.")
        return np.frombuffer(vector.tobytes(), dtype=np.float64)
    except (ValueError, TypeError, OverflowError) as error:
        raise RenderValidationError("Invalid light/surface vector.") from error


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise RenderValidationError(f"{label} must be finite and real.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise RenderValidationError(f"Invalid {label}.") from error
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise RenderValidationError(f"{label} must be finite and nonnegative (positive when required).")
    return result


@dataclass(frozen=True, slots=True, eq=False)
class PreparedLight:
    """Obtain from PreparedLighting; world vectors are copied, never transformed."""

    source: LightData
    intensity_rgb: NDArray[np.float64]
    position: NDArray[np.float64] | None
    emission_direction: NDArray[np.float64] | None
    range: float | None
    cos_inner: float | None
    cos_outer: float | None


def _prepare(light: LightData) -> PreparedLight:
    if not isinstance(light, LightData):
        raise RenderValidationError("Active lights must be LightData.")
    if not isinstance(light.type, str) or light.type not in ("directional", "point", "spot"):
        raise UnsupportedFeatureError("Unsupported active light type.")
    if light.extensions:
        raise UnsupportedFeatureError("Active light extensions are not evaluated.")
    if light.intensity_unit != ("lux" if light.type == "directional" else "candela"):
        raise RenderValidationError("Light intensity unit disagrees with its type.")
    color = linear_rgb(light.color)
    if np.any(color > 1):
        raise RenderValidationError("Light color must be within [0,1].")
    intensity = _number(light.intensity, "Light intensity")
    position = None if light.type == "directional" else _vector(light.world_position)
    direction = None if light.type == "point" else _vector(light.world_direction, unit=True)
    if ((light.type == "directional" and (light.world_position is not None or light.range is not None))
            or (light.type == "point" and light.world_direction is not None)):
        raise RenderValidationError("Light position/direction/range disagrees with its type.")
    light_range = None if light.range is None else _number(light.range, "Light range", positive=True)
    inner = outer = None
    if light.type == "spot":
        a = _number(light.inner_cone_angle, "Spot inner angle")
        b = _number(light.outer_cone_angle, "Spot outer angle", positive=True)
        if not a < b <= math.pi / 2:
            raise RenderValidationError("Spot angles require 0 <= inner < outer <= pi/2.")
        inner, outer = math.cos(a), math.cos(b)
        if not inner > outer:
            raise LightingNumericalError("Spot cosine difference is not representable.")
    elif light.inner_cone_angle is not None or light.outer_cone_angle is not None:
        raise RenderValidationError("Only spot lights have cone angles.")
    try:
        with np.errstate(over="raise", invalid="raise", under="raise"):
            weighted = color * intensity
    except FloatingPointError as error:
        raise LightingNumericalError("Light RGB intensity exceeds float64 range.") from error
    return PreparedLight(light, np.frombuffer(weighted.tobytes(), dtype=np.float64),
                         position, direction, light_range, inner, outer)


@dataclass(frozen=True, slots=True, eq=False)
class PreparedLighting:
    """Reusable, scene-bound light constants in deterministic metadata order.

    Source scene/metadata must remain read-only for this object's lifetime.
    Visibility defaults to one; the evaluator can accept a scoped provider.
    """

    context: ShadedContext
    lights: tuple[PreparedLight, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context, ShadedContext):
            raise RenderValidationError("PreparedLighting requires ShadedContext.")
        object.__setattr__(self, "lights", tuple(_prepare(light) for light in self.context.metadata.lights))


@dataclass(frozen=True, slots=True, eq=False)
class LightSample:
    """A contributing incoming direction, distance (infinite directional), and E.

    These are illumination data, not a validated secondary-ray origin/interval.
    """

    light: PreparedLight
    incoming_direction: NDArray[np.float64]
    distance: float
    incident_rgb: NDArray[np.float64]


def sample_light(light: PreparedLight, position_world: object) -> LightSample | None:
    """Return None for zero intensity/range/cone; never issue a visibility query."""
    if not isinstance(light, PreparedLight):
        raise RenderValidationError("Light sampling requires PreparedLight.")
    position = _vector(position_world)
    if not np.any(light.intensity_rgb):
        return None
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="raise"):
            distance = math.inf
            attenuation = 1.0
            if light.source.type == "directional":
                incoming = -light.emission_direction
            else:
                delta = light.position - position
                distance = math.hypot(*delta)
                if distance == 0 or not math.isfinite(distance):
                    raise LightingNumericalError("Surface-light separation is zero or unrepresentable.")
                if light.range is not None and distance >= light.range:
                    return None
                incoming = delta / distance
                squared = distance * distance
                if squared == 0 or not math.isfinite(squared):
                    raise LightingNumericalError("Squared surface-light separation is unrepresentable.")
                attenuation = 1.0 / squared
                if not math.isfinite(attenuation) or attenuation == 0:
                    raise LightingNumericalError("Inverse-square attenuation is unrepresentable.")
                if light.range is not None:
                    attenuation *= max(1.0 - (distance / light.range) ** 4, 0.0)
                if light.source.type == "spot":
                    cosine = float(np.dot(light.emission_direction, -incoming))
                    q = min(1.0, max(0.0, (cosine-light.cos_outer)/(light.cos_inner-light.cos_outer)))
                    if q == 0:
                        return None
                    attenuation *= q * q
            incident = light.intensity_rgb * attenuation
    except FloatingPointError as error:
        raise LightingNumericalError("Light sample exceeds float64 range.") from error
    if not np.isfinite(incident).all():
        raise LightingNumericalError("Light sample produced nonfinite incident RGB.")
    return LightSample(light, _vector(incoming, unit=True), distance,
                       np.frombuffer(incident.tobytes(), dtype=np.float64))


