"""Bounded-domain secondary-ray policy and scalar material shadow visibility.

Exact rational reconstruction certifies algebra relative to the stored float
geometry/world matrix and input Ray, not the M3 kernel's universal accuracy.
The declared domain and real-kernel/contact fixtures remain empirical gates.
No PBRT kernel coefficients or fixed scene-unit epsilon are used.
"""

from dataclasses import dataclass
from fractions import Fraction as F
import math

import numpy as np

from drt_backend.raytracing.shading.alpha_compositing import BlendLayerLimitError, composite_ordered_surfaces
from drt_backend.raytracing.shading.direct_lighting import LightSample, _vector
from drt_backend.raytracing.common.errors import GeometryError, RenderValidationError, require_int
from drt_backend.raytracing.resources.image_decoding import TextureContext
from drt_backend.raytracing.model.rays import Ray
from drt_backend.raytracing.resources.shaded_context import ShadedContext
from drt_backend.raytracing.tracing.surface_queries import (AcceptedSurface, OrderedSurfaceResult, ShadowNumericalError, SourceHitGuard,
                              _evaluate_camera_candidate, _group_accepted, _shadow_geometric_candidates,
                              trace_occluded)
from drt_backend.raytracing.shading.texture_sampling import SamplingPolicy


def _q(value):
    return F(float(value))


def _dot(a, b):
    return sum((x*y for x, y in zip(a, b, strict=True)), F(0))


def _sub(a, b):
    return tuple(x-y for x, y in zip(a, b, strict=True))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _rounded(value: F, upward: bool) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ShadowNumericalError("Secondary-ray arithmetic exceeds float64.")
    if (upward and F(result) < value) or (not upward and F(result) > value):
        result = math.nextafter(result, math.inf if upward else -math.inf)
    if not math.isfinite(result):
        raise ShadowNumericalError("Secondary-ray outward rounding overflowed.")
    return result


@dataclass(frozen=True, slots=True, eq=False)
class SpawnedShadow:
    ray: Ray
    guard: SourceHitGuard
    position_error_l1: float
    offset_distance: float
    endpoint_margin: float


def spawn_shadow_ray(context: ShadedContext, primary_ray: Ray, accepted: AcceptedSurface,
                     sample: LightSample) -> SpawnedShadow:
    """Derive an offset, contact certificate and finite/infinite outgoing Ray.

    Support gates: edge L1 extents [2^-40,2^40], coordinate/edge <=2^40,
    ||A||inf*||cached A^-1||inf <=2^20, primary/outgoing |n.d|>=2^-20,
    reconstruction envelope/edge <=2^-20. These are declared dimensionless
    conditioning/precision gates, not hidden offsets. Distinct contact geometry
    requires an exact common-plane departure certificate or is rejected.
    A contact line alone is not a 3D clearance proof.
    """
    if (not isinstance(context, ShadedContext) or not isinstance(primary_ray, Ray)
            or not isinstance(accepted, AcceptedSurface) or not isinstance(sample, LightSample)):
        raise ShadowNumericalError("Spawn requires context, primary Ray, AcceptedSurface and LightSample.")
    surface, hit = accepted.surface, accepted.hit
    instance, primitive, prepared = context.resolve_hit(hit)
    if (surface.scene is not context.scene or surface.instance is not instance or surface.primitive is not primitive
            or accepted.material is not context.metadata.material_for_primitive(primitive)):
        raise GeometryError("Shadow source ownership disagrees with context.")
    if (hit.triangle_index >= len(primitive.triangle_indices) or not primary_ray.t_min <= hit.t <= primary_ray.t_max
            or surface.vertex_indices != tuple(int(i) for i in primitive.triangle_indices[hit.triangle_index])
            or surface.barycentric_weights != (1-hit.u-hit.v, hit.u, hit.v)):
        raise ShadowNumericalError("Shadow source hit/index/interval is inconsistent.")
    if not any(sample.light.source is source for source in context.metadata.lights):
        raise GeometryError("Shadow light does not belong to context metadata.")
    try:
        return _spawn(context, primary_ray, accepted, sample, instance, primitive, prepared)
    except (OverflowError, ZeroDivisionError, FloatingPointError) as error:
        raise ShadowNumericalError("Secondary-ray numerical policy exceeded its domain.") from error


def _spawn(context, primary, accepted, sample, instance, primitive, prepared):
    surface, hit = accepted.surface, accepted.hit
    vertices = [tuple(_q(x) for x in row) for row in primitive.positions[list(surface.vertex_indices)]]
    matrix = instance.world_matrix
    a = [tuple(_q(x) for x in row) for row in matrix[:3, :3]]
    translation = tuple(_q(x) for x in matrix[:3, 3])
    norm_a = max(sum(abs(x) for x in row) for row in a)
    norm_inv = max(sum(abs(_q(x)) for x in row) for row in prepared.inverse_linear)
    if norm_a*norm_inv > 2**20:
        raise ShadowNumericalError("Instance conditioning exceeds shadow support domain.")
    e1, e2 = _sub(vertices[1], vertices[0]), _sub(vertices[2], vertices[0])
    edge1, edge2 = tuple(_dot(row, e1) for row in a), tuple(_dot(row, e2) for row in a)
    extents = [sum(abs(x) for x in e) for e in (edge1, edge2)]
    extent, smallest = max(extents), min(extents)
    if smallest < F(1, 2**40) or extent > 2**40:
        raise ShadowNumericalError("Triangle extent exceeds shadow support domain.")
    w0 = tuple(_dot(row, vertices[0])+t for row, t in zip(a, translation, strict=True))
    plane = _cross(edge1, edge2)
    o, d = tuple(map(_q, primary.origin)), tuple(map(_q, primary.direction))
    n = tuple(map(_q, _vector(surface.geometric_normal_world, unit=True)))
    outgoing = tuple(map(_q, _vector(sample.incoming_direction, unit=True)))
    incidence, hemisphere = _dot(n, d), _dot(n, outgoing)
    if abs(incidence) < F(1, 2**20) or abs(hemisphere) < F(1, 2**20):
        raise ShadowNumericalError("Grazing primary/outgoing incidence is numerically unsupported.")
    if (accepted.front_face and hemisphere <= 0) or (not accepted.front_face and hemisphere >= 0):
        raise ShadowNumericalError("Outgoing ray is outside the facing geometric hemisphere.")
    if not any(plane) or _dot(plane, d) == 0:
        raise ShadowNumericalError("Exact source plane is degenerate or parallel.")
    tau = _dot(plane, _sub(w0, o))/_dot(plane, d)
    exact_point = tuple(x+tau*y for x, y in zip(o, d, strict=True))
    if tau < _q(primary.t_min) or (math.isfinite(primary.t_max) and tau > _q(primary.t_max)):
        raise ShadowNumericalError("Exact source intersection is outside the primary interval.")
    # Exact containment on the dominant plane; reject marginal false MT hits.
    axis = max(range(3), key=lambda k: abs(plane[k]))
    indices = [i for i in range(3) if i != axis]
    i, j = indices
    offset = _sub(exact_point, w0)
    determinant = edge1[i]*edge2[j]-edge1[j]*edge2[i]
    u = (offset[i]*edge2[j]-offset[j]*edge2[i])/determinant
    v = (edge1[i]*offset[j]-edge1[j]*offset[i])/determinant
    if u < 0 or v < 0 or u+v > 1:
        raise ShadowNumericalError("Exact plane intersection is outside the source triangle.")
    p = tuple(map(_q, _vector(surface.position_world)))
    ray_position = tuple(map(_q, _vector(surface.position_ray_world)))
    weights = (F(1)-_q(hit.u)-_q(hit.v), _q(hit.u), _q(hit.v))
    local = tuple(sum(weights[k]*vertices[k][j] for k in range(3)) for j in range(3))
    bary_world = tuple(_dot(row, local)+t for row, t in zip(a, translation, strict=True))
    ray_exact_t = tuple(x+_q(hit.t)*y for x, y in zip(o, d, strict=True))
    # u=2^-53 unit roundoff; gamma5 bounds 3 products+2 adds, gamma6
    # bounds 3 products+3 adds. Exact-plane/bary/ray differences separately
    # include this actual hit's kernel discrepancy, not only p_world-p_ray.
    unit = F(1, 2**53)
    g5, g6 = 5*unit/(1-5*unit), 6*unit/(1-6*unit)
    local_error = tuple(g5*sum(abs(weights[k]*vertices[k][j]) for k in range(3)) for j in range(3))
    errors = []
    for j in range(3):
        propagation = (1+g6)*sum(abs(a[j][k])*local_error[k] for k in range(3))
        affine_error = g6*(sum(abs(a[j][k]*local[k]) for k in range(3))+abs(translation[j]))
        observed = max(abs(candidate[j]-p[j]) for candidate in (exact_point, bary_world, ray_exact_t, ray_position))
        errors.append(observed+propagation+affine_error)
    # An isotropic reconstruction reserve based on edge arithmetic, retaining
    # nonzero clearance even on coordinate planes or at an exact zero vertex.
    error_l1 = sum(errors)+g5*extent
    if max(abs(x) for x in p)/smallest > 2**40 or error_l1/smallest > F(1, 2**20):
        raise ShadowNumericalError("Coordinate/reconstruction uncertainty exceeds supported feature scale.")
    normal_plane = _dot(plane, n)
    if 2*abs(normal_plane) < sum(abs(x) for x in plane):
        raise ShadowNumericalError("Geometric normal does not align with the exact source plane.")
    sign = 1 if hemisphere > 0 else -1
    # |N.n| >= ||N||1/2 above: moving 2*error_l1 along n clears
    # every component error in this envelope. Verify actual representable side.
    delta = 2*error_l1
    shifted = []
    for coordinate, normal in zip(p, n, strict=True):
        target = coordinate+sign*delta*normal
        moved = _rounded(target, sign*normal > 0)
        if normal:
            moved = math.nextafter(moved, math.inf if sign*normal > 0 else -math.inf)
        shifted.append(moved)
    shifted_q = tuple(map(_q, shifted))
    signed_distance = _dot(plane, _sub(shifted_q, w0))
    if signed_distance*sign*normal_plane <= 0:
        raise ShadowNumericalError("Rounded shadow origin did not leave the source plane.")
    endpoint_margin = F(0)
    if sample.light.position is None:
        direction, t_max = sample.incoming_direction, math.inf
    else:
        light_position = tuple(map(_q, sample.light.position))
        separation = _sub(light_position, shifted_q)
        delta_float = np.array([float(x) for x in separation])
        distance = math.hypot(*delta_float)
        if not 0 < distance < math.inf:
            raise ShadowNumericalError("Offset light segment has no representable length.")
        direction = delta_float/distance
        q_direction = tuple(map(_q, direction))
        closest = _dot(separation, q_direction)/_dot(q_direction, q_direction)
        # Endpoint error includes source envelope, exact transverse miss of
        # the computed direction and representability of multiply/add at both
        # endpoints. ||d||>1/2, so 2*L1 spatial radius bounds parameter radius.
        miss = _sub(separation, tuple(closest*x for x in q_direction))
        endpoint_error = error_l1+sum(abs(x) for x in miss)+sum(
            _q(math.ulp(float(lp)))+_q(math.ulp(float(sp))) for lp, sp in zip(light_position, shifted_q, strict=True))
        endpoint_margin = 2*endpoint_error
        t_max = math.nextafter(_rounded(closest-endpoint_margin, False), 0)
        if t_max <= 0 or closest <= endpoint_margin:
            raise ShadowNumericalError("Uncertainty consumes the finite light segment.")
    q_direction = tuple(map(_q, direction))
    if abs(_dot(n, q_direction)) < F(1, 2**20):
        raise ShadowNumericalError("Recomputed offset direction is grazing.")
    if _dot(plane, q_direction)*signed_distance <= 0:
        raise ShadowNumericalError("Offset outgoing segment does not leave the exact source plane.")
    ray = Ray(shifted, direction, 0, t_max)
    displacement = sum(abs(x-y) for x, y in zip(shifted_q, exact_point, strict=True))
    near = _rounded(2*(displacement+error_l1)/abs(_dot(n, q_direction)), True)
    contact_max = _rounded(2*(displacement+error_l1)/_dot(n, n), True)
    contact = Ray(shifted, np.array([-sign*float(x) for x in n]), 0, contact_max)
    guard = SourceHitGuard(context, ray, hit, near, contact)
    return SpawnedShadow(ray, guard, _rounded(error_l1, True), _rounded(delta, True),
                         _rounded(endpoint_margin, True))


def trace_shadow_transmittance(context: TextureContext, shadow_ray: Ray,
                               source_guard: SourceHitGuard | None = None,
                               sampling_policy: SamplingPolicy = SamplingPolicy(), *,
                               max_candidates: int = 65_536, max_blend_layers: int = 64) -> float:
    """Complete guarded raw collection, existing acceptance/grouping, then T.

    Reusing the M4.4 compositor with zero supplied RGB preserves its exact
    positive-BLEND counting, termination, underflow and limit semantics.
    """
    if not isinstance(sampling_policy, SamplingPolicy):
        raise RenderValidationError("Weighted visibility requires SamplingPolicy.")
    require_int(max_blend_layers, "max_blend_layers", 1, BlendLayerLimitError)
    if max_blend_layers > 64:
        raise BlendLayerLimitError("max_blend_layers exceeds 64.")
    raw = tuple(_shadow_geometric_candidates(context, shadow_ray, source_guard, max_candidates=max_candidates))
    accepted = []
    for hit in raw:
        item = _evaluate_camera_candidate(context, shadow_ray, hit, sampling_policy)
        if item is not None:
            accepted.append(item)
    ordered = OrderedSurfaceResult(context.shaded, shadow_ray, _group_accepted(accepted))
    return composite_ordered_surfaces(ordered, lambda item: (0, 0, 0),
                                     max_blend_layers=max_blend_layers).transmittance


def make_visibility_provider(context: TextureContext, primary_ray: Ray, mode: str, *,
                              sampling_policy: SamplingPolicy = SamplingPolicy(),
                              max_candidates: int = 65_536, max_blend_layers: int = 64,
                              counters=None):
    """One primary-ray scoped callback; original LightSample radiance is reused."""
    if (not isinstance(context, TextureContext) or not isinstance(primary_ray, Ray)
            or mode not in ("binary", "alpha_weighted")):
        raise RenderValidationError("Visibility provider requires context, primary Ray and explicit shadow mode.")
    context.require_open()

    def visibility(accepted, sample):
        spawned = spawn_shadow_ray(context.shaded, primary_ray, accepted, sample)
        # One logical secondary visibility trace. Contact certification and all
        # internal BVH/triangle operations remain part of this same ray.
        if counters is not None:
            counters.shadow()
        if mode == "binary":
            return 0.0 if trace_occluded(context, spawned.ray, spawned.guard, sampling_policy,
                                         max_candidates=max_candidates) else 1.0
        return trace_shadow_transmittance(context, spawned.ray, spawned.guard, sampling_policy,
                                         max_candidates=max_candidates, max_blend_layers=max_blend_layers)

    return visibility
