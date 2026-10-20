"""Pure front-to-back coverage composition of caller-supplied linear radiance."""

from collections.abc import Callable
from dataclasses import dataclass
import math
from numbers import Real

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, require_int
from drt_backend.raytracing.tracing.surface_evaluation import SurfaceAttributes
from drt_backend.raytracing.tracing.surface_queries import AcceptedSurface, OrderedSurfaceResult, _camera_coverage, _group_accepted
from drt_backend.raytracing.shading.textured_material_evaluation import EffectiveBaseColor
from drt_backend.raytracing.shading.material_evaluation import BaseMaterial


class BlendLayerLimitError(RenderValidationError):
    """Another positive-alpha BLEND layer is required beyond the selected cap."""


@dataclass(frozen=True, slots=True, eq=False)
class CompositeResult:
    """Obtain from composite_ordered_surfaces; RGB has immutable bytes backing."""

    linear_rgb: NDArray[np.float64]
    transmittance: float
    consumed_layers: int
    blend_layers: int


def _linear_rgb(value: object, label: str) -> NDArray[np.float64]:
    try:
        source = np.asarray(value)
        if source.shape != (3,) or source.dtype.kind not in "iuf":
            raise RenderValidationError(f"{label} requires three real linear RGB components.")
        with np.errstate(over="raise", invalid="raise", under="raise"):
            rgb = np.asarray(source, dtype=np.float64)
        if not np.isfinite(rgb).all() or np.any(rgb < 0):
            raise RenderValidationError(f"{label} must be finite and nonnegative.")
    except (ValueError, TypeError, OverflowError, FloatingPointError) as error:
        raise RenderValidationError(f"Invalid {label}; expected finite nonnegative linear RGB.") from error
    return rgb


def _validated_surfaces(ordered: OrderedSurfaceResult) -> tuple[AcceptedSurface, ...]:
    """Check source ownership, coverage and canonical groups without resampling.

    This validates retained representations, not the completeness of a fabricated
    result or a proof that an arbitrary user-created HitRecord intersected a ray.
    Sources remain read-only by the established context lifetime convention.
    """
    if not isinstance(ordered, OrderedSurfaceResult):
        raise RenderValidationError("Compositing requires an OrderedSurfaceResult.")
    surfaces = tuple(item for group in ordered.groups for item in group.surfaces)
    identities = set()
    for item in surfaces:
        if (not isinstance(item.evaluated, EffectiveBaseColor)
                or not isinstance(item.surface, SurfaceAttributes)
                or not isinstance(item.evaluated.base, BaseMaterial)):
            raise RenderValidationError("Accepted surface has an unsupported evaluation representation.")
        surface, hit = item.surface, item.hit
        instance, primitive, _ = ordered.context.resolve_hit(hit)
        if (surface.scene is not ordered.context.scene or surface.instance is not instance
                or surface.primitive is not primitive
                or item.material is not ordered.context.metadata.material_for_primitive(primitive)):
            raise GeometryError("Accepted surface source/material ownership does not match the ordered context.")
        if (not ordered.ray.t_min <= hit.t <= ordered.ray.t_max
                or hit.triangle_index >= len(primitive.triangle_indices)):
            raise GeometryError("Accepted surface hit interval/triangle identity is invalid.")
        if (surface.vertex_indices != tuple(int(i) for i in primitive.triangle_indices[hit.triangle_index])
                or surface.barycentric_weights != (1-hit.u-hit.v, hit.u, hit.v)):
            raise GeometryError("Accepted surface indexed/barycentric identity is inconsistent.")
        identity = (hit.instance_index, hit.primitive_index, hit.triangle_index)
        if identity in identities:
            raise GeometryError("An ordered result cannot repeat the same original triangle identity.")
        identities.add(identity)
        rgba = np.asarray(item.evaluated.effective_rgba)
        if (rgba.shape != (4,) or rgba.dtype.kind not in "iuf" or not np.isfinite(rgba).all() or np.any(rgba < 0)
                or not 0 <= rgba[3] <= 1):
            raise RenderValidationError("Accepted effective RGBA/alpha is invalid.")
        if (isinstance(item.coverage_alpha, bool) or not isinstance(item.coverage_alpha, Real)
                or not math.isfinite(item.coverage_alpha) or not 0 < item.coverage_alpha <= 1
                or item.alpha_mode not in ("OPAQUE", "MASK", "BLEND")
                or (not item.material.double_sided and not hit.front_face)
                or _camera_coverage(item.evaluated.base, float(rgba[3])) != item.coverage_alpha):
            raise RenderValidationError("Accepted camera coverage is invalid or inconsistent with its material.")
    expected = _group_accepted(list(surfaces))
    if (len(expected) != len(ordered.groups)
            or any(a.anchor_t != b.anchor_t or len(a.surfaces) != len(b.surfaces)
                   or any(x is not y for x, y in zip(a.surfaces, b.surfaces, strict=True))
                   for a, b in zip(expected, ordered.groups, strict=True))):
        raise RenderValidationError("Ordered groups do not follow minimum-anchored eight-ULP identity ordering.")
    return surfaces


def composite_ordered_surfaces(ordered: OrderedSurfaceResult,
                               color_provider: Callable[[AcceptedSurface], object], *,
                               background_linear: object = (0, 0, 0),
                               max_blend_layers: int = 64) -> CompositeResult:
    """Compose supplied, unpremultiplied linear RGB; perform no lighting/display.

    Validate the retained sequence first. Invoke the provider only for consumed
    layers, passing the exact source-owned AcceptedSurface. Every positive-alpha
    BLEND, including alpha one, counts; OPAQUE/MASK do not count against the cap.
    Stop only at exactly zero T. Underflow fails rather than fabricating opacity.
    A provider should be deterministic and read-only; its exceptions propagate.
    """
    require_int(max_blend_layers, "max_blend_layers", 1, BlendLayerLimitError)
    if max_blend_layers > 64:
        raise BlendLayerLimitError("max_blend_layers exceeds the approved maximum of 64.")
    if not callable(color_provider):
        raise RenderValidationError("Compositing requires a callable linear color provider.")
    background = _linear_rgb(background_linear, "background")
    surfaces = _validated_surfaces(ordered)
    color, transmittance = np.zeros(3, dtype=np.float64), np.float64(1)
    consumed = blends = 0
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="raise"):
            for surface in surfaces:
                if transmittance == 0:
                    break
                if surface.alpha_mode == "BLEND":
                    if blends == max_blend_layers:
                        raise BlendLayerLimitError(f"Contributing BLEND layers exceed max_blend_layers={max_blend_layers}.")
                    blends += 1
                rgb = _linear_rgb(color_provider(surface), "layer radiance")
                alpha = np.float64(surface.coverage_alpha)
                color = color + (transmittance * alpha) * rgb
                transmittance = transmittance * (1-alpha)
                consumed += 1
            color = color + transmittance * background
    except FloatingPointError as error:
        raise RenderValidationError("Alpha composition exceeds the supported float64 numerical range.") from error
    if not np.isfinite(color).all() or not math.isfinite(transmittance):
        raise RenderValidationError("Alpha composition produced nonfinite RGB/transmittance.")
    return CompositeResult(np.frombuffer(color.tobytes(), dtype=np.float64), float(transmittance), consumed, blends)
