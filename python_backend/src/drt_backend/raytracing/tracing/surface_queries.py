"""Camera-contributing closest surfaces over the unchanged M3 geometry kernel."""

from collections.abc import Iterator
from dataclasses import dataclass
import math
from numbers import Real

from drt_backend.raytracing.tracing.bvh import SceneAcceleration, _leaf_references
from drt_backend.raytracing.common.errors import RenderValidationError, require_int
from drt_backend.raytracing.resources.image_decoding import TextureContext
from drt_backend.raytracing.tracing.intersection import HitRecord, intersect_triangle, transform_ray
from drt_backend.raytracing.shading.material_evaluation import BaseMaterial, evaluate_base_material
from drt_backend.raytracing.model.materials import MaterialData
from drt_backend.raytracing.model.rays import Ray
from drt_backend.raytracing.resources.shaded_context import ShadedContext
from drt_backend.raytracing.tracing.surface_evaluation import SurfaceAttributes, evaluate_surface
from drt_backend.raytracing.shading.texture_sampling import SamplingPolicy
from drt_backend.raytracing.shading.textured_material_evaluation import EffectiveBaseColor, evaluate_effective_base_color
from drt_backend.raytracing.tracing.contact_certification import certified_coplanar_departure


_MAX_RAW_CANDIDATES = 65_536


class CandidateLimitError(RenderValidationError):
    """Successful raw geometric candidates exceeded the configured query cap."""


@dataclass(frozen=True, slots=True, eq=False)
class AcceptedSurface:
    """Obtain through trace_filtered_closest; retain the evaluated winner once.

    Source scene/material references follow ShadedContext's borrowed, read-only
    lifetime. Derived surface and RGBA arrays retain M4.1/M4.2 immutable backing.
    Coverage is camera contribution, not composited color or shadow visibility.
    """

    evaluated: EffectiveBaseColor
    coverage_alpha: float

    @property
    def hit(self) -> HitRecord:
        return self.evaluated.surface.hit

    @property
    def surface(self) -> SurfaceAttributes:
        return self.evaluated.surface

    @property
    def material(self) -> MaterialData:
        return self.evaluated.base.source

    @property
    def alpha_mode(self) -> str:
        return self.evaluated.base.alpha_mode

    @property
    def front_face(self) -> bool:
        return self.hit.front_face


def _iter_geometric_candidates(acceleration: SceneAcceleration, ray: Ray, *,
                               max_candidates: int = _MAX_RAW_CANDIDATES) -> Iterator[HitRecord]:
    """Borrow TLAS/BLAS, yielding original hits over the full original interval.

    Count every successful kernel hit before material rejection. The cap bounds
    successful candidates, not node visits or kernel attempts. This DFS stream
    is unordered; exhausting it is necessary before returning a closest winner.
    Limits may be lowered, not raised beyond the approved default maximum.
    """
    if not isinstance(acceleration, SceneAcceleration) or not isinstance(ray, Ray):
        raise RenderValidationError("Surface traversal requires SceneAcceleration and a world Ray.")
    require_int(max_candidates, "max_candidates", 1, CandidateLimitError)
    if max_candidates > _MAX_RAW_CANDIDATES:
        raise CandidateLimitError("max_candidates exceeds the approved 65,536 raw-hit maximum.")
    count = 0
    for instance_index in _leaf_references(acceleration.tlas, ray):
        prepared = acceleration.prepared_instances[instance_index]
        local = transform_ray(ray, prepared)
        blas = acceleration.blas_by_mesh[prepared.instance.mesh_index]
        for reference in _leaf_references(blas.bvh, local):
            primitive_index = int(blas.triangle_primitive_indices[reference])
            triangle_index = int(blas.triangle_indices[reference])
            primitive = blas.definition.primitives[primitive_index]
            vertices = primitive.positions[primitive.triangle_indices[triangle_index]]
            hit = intersect_triangle(local, vertices, instance_index=instance_index,
                                     primitive_index=primitive_index, triangle_index=triangle_index)
            if hit is not None:
                count += 1
                if count > max_candidates:
                    raise CandidateLimitError(f"Raw geometric candidates exceed max_candidates={max_candidates}.")
                yield hit


def _camera_coverage(base: BaseMaterial, alpha: float) -> float | None:
    """None means material rejection; all comparisons use validated alpha."""
    if not math.isfinite(alpha):
        raise RenderValidationError("Effective surface alpha must be finite.")
    if base.alpha_mode == "OPAQUE":
        return 1.0
    if base.alpha_mode == "MASK":
        return None if alpha < base.alpha_cutoff else 1.0
    return None if alpha == 0 else alpha  # Validated BLEND; cutoff is irrelevant.


def _evaluate_camera_candidate(context: TextureContext, ray: Ray, hit: HitRecord,
                                sampling_policy: SamplingPolicy) -> AcceptedSurface | None:
    """Shared M4.3 camera acceptance; retain one authoritative evaluation."""
    _, primitive, _ = context.shaded.resolve_hit(hit)
    base = evaluate_base_material(context.shaded, primitive)
    if not base.double_sided and not hit.front_face:
        return None
    # Extension-bearing materials go through the authoritative evaluator so
    # unsupported semantics cannot be hidden by the cheap alpha shortcut.
    if base.base_color_texture is None and not base.source.extensions:
        if _camera_coverage(base, float(base.base_color_factor[3])) is None:
            return None
    surface = evaluate_surface(context.shaded, ray, hit)
    evaluated = evaluate_effective_base_color(context, surface, sampling_policy)
    coverage = _camera_coverage(evaluated.base, float(evaluated.effective_rgba[3]))
    return None if coverage is None else AcceptedSurface(evaluated, coverage)


def trace_filtered_closest(context: TextureContext, ray: Ray,
                           sampling_policy: SamplingPolicy = SamplingPolicy(), *,
                           max_candidates: int = _MAX_RAW_CANDIDATES) -> AcceptedSurface | None:
    """Accept independently, then choose the accepted global-minimum 8-ULP band.

    Explicit TextureContext.prepare() must precede any needed sampling; this
    query never decodes. Back faces and known untextured alpha rejections avoid
    surface interpolation. Evaluation errors propagate, including for accepted
    OPAQUE textures whose raw alpha does not affect camera coverage.
    """
    if (not isinstance(context, TextureContext) or not isinstance(ray, Ray)
            or not isinstance(sampling_policy, SamplingPolicy)):
        raise RenderValidationError("Filtered closest requires TextureContext, world Ray and SamplingPolicy.")
    context.require_open()
    accepted: list[AcceptedSurface] = []
    for hit in _iter_geometric_candidates(context.shaded.acceleration, ray, max_candidates=max_candidates):
        candidate = _evaluate_camera_candidate(context, ray, hit, sampling_policy)
        if candidate is not None:
            accepted.append(candidate)
    if not accepted:
        return None
    minimum = min(item.hit.t for item in accepted)
    tied = (item for item in accepted
            if abs(item.hit.t - minimum) <= 8 * max(math.ulp(item.hit.t), math.ulp(minimum)))
    return min(tied, key=lambda item: (item.hit.instance_index, item.hit.primitive_index, item.hit.triangle_index))


@dataclass(frozen=True, slots=True, eq=False)
class OrderedSurfaceGroup:
    """Minimum anchor separate from unchanged, identity-ordered hit depths."""

    anchor_t: float
    surfaces: tuple[AcceptedSurface, ...]

    def __post_init__(self) -> None:
        if (isinstance(self.anchor_t, bool) or not isinstance(self.anchor_t, Real)
                or not math.isfinite(self.anchor_t) or self.anchor_t < 0):
            raise RenderValidationError("Ordered group anchor must be finite and nonnegative.")
        if (not isinstance(self.surfaces, tuple) or not self.surfaces
                or any(not isinstance(item, AcceptedSurface) for item in self.surfaces)):
            raise RenderValidationError("Ordered group requires a nonempty tuple of AcceptedSurface objects.")


@dataclass(frozen=True, slots=True, eq=False)
class OrderedSurfaceResult:
    """Borrow source context/ray; own only immutable tuples and small wrappers.

    No images or geometry are copied. Flattened original depths can decrease
    inside a group, and across an exponent-boundary eligibility gap. Consumers
    must use group order followed by identity order, never re-sort the layers.
    """

    context: ShadedContext
    ray: Ray
    groups: tuple[OrderedSurfaceGroup, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.context, ShadedContext) or not isinstance(self.ray, Ray)
                or not isinstance(self.groups, tuple)
                or any(not isinstance(group, OrderedSurfaceGroup) for group in self.groups)):
            raise RenderValidationError("Ordered result requires ShadedContext, world Ray and immutable groups.")


def _group_accepted(surfaces: list[AcceptedSurface]) -> tuple[OrderedSurfaceGroup, ...]:
    """Exact-depth sort, then partition ALL remaining hits against each minimum.

    A failed predicate is not a safe scan stop: the ULP can double at the next
    exponent boundary, making a later hit eligible across an ineligible gap.
    Full partitioning prioritizes this contract over a contiguous-prefix shortcut.
    """
    remaining = sorted(surfaces, key=lambda item: item.hit.t)
    groups = []
    while remaining:
        anchor = remaining[0].hit.t
        members, excluded = [], []
        for item in remaining:
            if abs(item.hit.t-anchor) <= 8 * max(math.ulp(item.hit.t), math.ulp(anchor)):
                members.append(item)
            else:
                excluded.append(item)
        members.sort(key=lambda item: (item.hit.instance_index, item.hit.primitive_index, item.hit.triangle_index))
        groups.append(OrderedSurfaceGroup(anchor, tuple(members)))
        remaining = excluded
    return tuple(groups)


def ordered_surface_hits(context: TextureContext, ray: Ray,
                         sampling_policy: SamplingPolicy = SamplingPolicy(), *,
                         max_candidates: int = _MAX_RAW_CANDIDATES) -> OrderedSurfaceResult:
    """Complete raw collection before acceptance/grouping; no partial result.

    All distinct original triangle identities are layers, including coincidences
    and shared edges. No compositor termination can hide a raw-candidate overflow.
    """
    if (not isinstance(context, TextureContext) or not isinstance(ray, Ray)
            or not isinstance(sampling_policy, SamplingPolicy)):
        raise RenderValidationError("Ordered hits require TextureContext, world Ray and SamplingPolicy.")
    context.require_open()
    raw = tuple(_iter_geometric_candidates(context.shaded.acceleration, ray, max_candidates=max_candidates))
    accepted = []
    for hit in raw:
        candidate = _evaluate_camera_candidate(context, ray, hit, sampling_policy)
        if candidate is not None:
            accepted.append(candidate)
    return OrderedSurfaceResult(context.shaded, ray, _group_accepted(accepted))


class ShadowNumericalError(RenderValidationError):
    """Shadow spawning/contact geometry is outside the supported numerical domain."""


@dataclass(frozen=True, slots=True, eq=False)
class SourceHitGuard:
    """Obtain from spawn_shadow_ray; bind one exact source, context and Ray.

    The builder verifies the exact source plane lies behind this outgoing ray.
    Only that identity within near_t can be a numerical self-hit. A contact
    segment checks geometry crossed by the offset. Distinct contacts still fail
    unless exact affine geometry proves a common plane and strict departure
    from that entire plane; adjacency or an approximate plane never suffices.
    """

    context: ShadedContext
    ray: Ray
    source_hit: HitRecord
    near_t: float
    contact_ray: Ray

    def __post_init__(self) -> None:
        if (not isinstance(self.context, ShadedContext) or not isinstance(self.ray, Ray)
                or not isinstance(self.source_hit, HitRecord) or not isinstance(self.contact_ray, Ray)
                or isinstance(self.near_t, bool) or not isinstance(self.near_t, Real)
                or not math.isfinite(self.near_t) or self.near_t <= 0):
            raise ShadowNumericalError("Invalid source-hit guard representation.")
        _, primitive, _ = self.context.resolve_hit(self.source_hit)
        if self.source_hit.triangle_index >= len(primitive.triangle_indices):
            raise ShadowNumericalError("Source guard triangle is out of range.")

    def same_identity(self, hit: HitRecord) -> bool:
        return (hit.instance_index, hit.primitive_index, hit.triangle_index) == (
            self.source_hit.instance_index, self.source_hit.primitive_index, self.source_hit.triangle_index)

    def excludes(self, hit: HitRecord) -> bool:
        return self.same_identity(hit) and 0 <= hit.t <= self.near_t


def _shadow_geometric_candidates(context: TextureContext, ray: Ray, guard: SourceHitGuard | None,
                                  *, max_candidates: int) -> Iterator[HitRecord]:
    """Shared lazy raw stream; contact and main traversals share one hit cap.

    A contact segment is a numerical support check, not material occlusion.
    It intentionally rejects unresolved distinct contacts even if transparent.
    Each traversal still uses the unmodified BLAS/TLAS and triangle kernel.
    """
    if not isinstance(context, TextureContext) or not isinstance(ray, Ray):
        raise RenderValidationError("Shadow queries require TextureContext and world Ray.")
    context.require_open()
    require_int(max_candidates, "max_candidates", 1, CandidateLimitError)
    if max_candidates > _MAX_RAW_CANDIDATES:
        raise CandidateLimitError("max_candidates exceeds 65,536.")
    if guard is not None and (not isinstance(guard, SourceHitGuard)
                              or guard.context is not context.shaded or guard.ray is not ray):
        raise ShadowNumericalError("Source guard is not bound to this exact context and Ray.")
    encountered = 0
    if guard is not None:
        for hit in _iter_geometric_candidates(context.shaded.acceleration, guard.contact_ray,
                                               max_candidates=max_candidates):
            encountered += 1
            if not guard.same_identity(hit):
                if not certified_coplanar_departure(context.shaded, guard, hit):
                    raise ShadowNumericalError("Distinct triangle intersects the source uncertainty band.")
    for hit in _iter_geometric_candidates(context.shaded.acceleration, ray, max_candidates=max_candidates):
        encountered += 1
        if encountered > max_candidates:
            raise CandidateLimitError("Contact plus shadow candidates exceed max_candidates.")
        if guard is not None:
            if guard.excludes(hit):
                continue
            if hit.t <= guard.near_t and certified_coplanar_departure(context.shaded, guard, hit):
                continue  # Exact world half-ray miss, not an identity/adjacency skip.
        yield hit


def trace_occluded(context: TextureContext, shadow_ray: Ray, source_guard: SourceHitGuard | None = None,
                   sampling_policy: SamplingPolicy = SamplingPolicy(), *,
                   max_candidates: int = _MAX_RAW_CANDIDATES) -> bool:
    """Lazy material-aware Boolean Any-Hit; partial BLEND is not a full blocker.

    Evaluate only encountered candidates. A legitimate full blocker ends the
    generator immediately; later materials/errors/hits are not inspected.
    This does not sort or call either closest/ordered public query.
    """
    if not isinstance(sampling_policy, SamplingPolicy):
        raise RenderValidationError("Shadow query requires SamplingPolicy.")
    for hit in _shadow_geometric_candidates(context, shadow_ray, source_guard, max_candidates=max_candidates):
        accepted = _evaluate_camera_candidate(context, shadow_ray, hit, sampling_policy)
        if accepted is not None and (accepted.alpha_mode != "BLEND" or accepted.coverage_alpha == 1):
            return True
    return False
