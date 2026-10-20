"""Addressed three-variate BSDF sampling with explicit continuous/delta/null events."""
from dataclasses import dataclass
import math

from .pbr_numeric import scalar, vector, dot, normalize
from .pbr_errors import InvalidBsdfSample, InvalidBsdfPdf, InvalidBsdfParameter
from .sampling import uniform_sample, MASK
from .bsdf import (BSDF_POLICY, BsdfPdf, direction, active_lobes, lobe_probability,
                   alpha_for_material, evaluate_bsdf, evaluate_pdf, delta_reflection)


def _uniform(value):
    return scalar(value, InvalidBsdfSample, minimum=0, maximum=math.nextafter(1.0, 0.0))


def sample_cosine_hemisphere(u1, u2):
    a, b = _uniform(u1), _uniform(u2)
    radius = math.sqrt(a)
    angle = 2*math.pi*b
    return (radius*math.cos(angle), radius*math.sin(angle), math.sqrt(1-a))


def sample_ggx_half_vector(u1, u2, alpha):
    from .microfacet import _alpha
    a, b, alpha = _uniform(u1), _uniform(u2), _alpha(alpha)
    tangent2 = alpha*alpha*a/(1-a)
    cosine = 1/math.sqrt(1+tangent2)
    sine = math.sqrt(tangent2/(1+tangent2))
    angle = 2*math.pi*b
    return (sine*math.cos(angle), sine*math.sin(angle), cosine)


@dataclass(frozen=True, slots=True)
class BsdfSample:
    event: str  # continuous, delta, null
    lobe: str  # diffuse, specular, none
    wi: tuple[float, float, float] | None
    pdf: BsdfPdf | None  # full continuous mixture, sr^-1, ONLY continuous event
    discrete_mass: float  # ONLY delta event, dimensionless lobe selection mass
    bsdf_value: tuple[float, float, float] | None  # ONLY continuous, sr^-1
    delta_coefficient: tuple[float, float, float] | None  # ONLY delta, dimensionless
    throughput_weight: tuple[float, float, float]  # numerical component, no propagation
    reason: str | None

    def __post_init__(self):
        weight = vector(self.throughput_weight, 3, InvalidBsdfSample, minimum=0)
        object.__setattr__(self, 'throughput_weight', weight)
        mass = scalar(self.discrete_mass, InvalidBsdfPdf, minimum=0, maximum=1)
        object.__setattr__(self, 'discrete_mass', mass)
        if self.event not in ('continuous', 'delta', 'null') or self.lobe not in ('diffuse', 'specular', 'none'):
            raise InvalidBsdfSample('Unknown sample event/lobe.')
        if self.event == 'null':
            if (self.wi is not None or self.pdf is not None or mass != 0 or self.bsdf_value is not None
                    or self.delta_coefficient is not None or any(weight)
                    or type(self.reason) is not str or not self.reason):
                raise InvalidBsdfSample('A rejected draw has no direction/density/weight, and needs a reason.')
            return
        wi = direction(self.wi)
        object.__setattr__(self, 'wi', wi)
        if wi[2] <= 0 or self.reason is not None or self.lobe == 'none':
            raise InvalidBsdfSample('A contributing event requires positive shading hemisphere.')
        if self.event == 'continuous':
            if type(self.pdf) is not BsdfPdf or self.pdf.solid_angle_density <= 0 or mass != 0 or self.delta_coefficient is not None:
                raise InvalidBsdfSample('Continuous event requires a positive solid-angle density, no mass.')
            value = vector(self.bsdf_value, 3, InvalidBsdfSample, minimum=0)
            object.__setattr__(self, 'bsdf_value', value)
            expected = tuple(c*wi[2]/self.pdf.solid_angle_density for c in value)
        else:
            if self.pdf is not None or mass <= 0 or self.bsdf_value is not None or self.lobe != 'specular':
                raise InvalidBsdfSample('Delta event requires discrete mass, no finite-density BRDF/PDF.')
            coefficient = vector(self.delta_coefficient, 3, InvalidBsdfSample, minimum=0, maximum=1)
            object.__setattr__(self, 'delta_coefficient', coefficient)
            expected = tuple(c/mass for c in coefficient)
        if not all(math.isfinite(c) and math.isclose(c, w, rel_tol=2e-12, abs_tol=1e-14) for c, w in zip(expected, weight)):
            raise InvalidBsdfSample('Weight is inconsistent with the declared probability measure.')

    @property
    def can_contribute(self):
        return self.event != 'null' and any(c > 0 for c in self.throughput_weight)


def _null(lobe, reason):
    # This records one rejected draw, NOT an alleged pointwise null PDF/mass.
    # Aggregate null probability is 1-integral(valid density)-delta mass.
    return BsdfSample('null', lobe, None, None, 0.0, None, None, (0.0,)*3, reason)


def sample_bsdf(material, wo, uniforms, *, policy=BSDF_POLICY) -> BsdfSample:
    alpha = alpha_for_material(material, policy=policy)
    wo = direction(wo)
    values = vector(uniforms, 3, InvalidBsdfSample)
    u_lobe, u1, u2 = tuple(_uniform(u) for u in values)
    if wo[2] <= 0:
        return _null('none', 'outgoing_outside_shading_hemisphere')
    if not any(active_lobes(material)):
        return _null('none', 'zero_bsdf')
    q = lobe_probability(material)
    if u_lobe < q:
        if alpha == 0:
            wi, coefficient, mass = delta_reflection(material, wo, policy=policy)
            return BsdfSample('delta', 'specular', wi, None, mass, None, coefficient,
                              tuple(c/mass for c in coefficient), None)
        h = sample_ggx_half_vector(u1, u2, alpha)
        cosine = dot(wo, h)
        if cosine <= 0:
            return _null('specular', 'microfacet_faces_away')
        wi = normalize(tuple(2*cosine*h[i]-wo[i] for i in range(3)), InvalidBsdfSample)
        if wi[2] <= 0:
            return _null('specular', 'reflection_below_shading_hemisphere')
        lobe = 'specular'
    else:
        wi = sample_cosine_hemisphere(u1, u2)
        lobe = 'diffuse'
    pdf = evaluate_pdf(material, wi, wo, policy=policy)
    if pdf.solid_angle_density <= 0:
        raise InvalidBsdfPdf('Valid sampled direction lacks full-mixture probability support.')
    value = evaluate_bsdf(material, wi, wo, policy=policy).value
    return BsdfSample('continuous', lobe, wi, pdf, 0.0, value, None,
                      tuple(c*wi[2]/pdf.solid_angle_density for c in value), None)


def addressed_bsdf_uniforms(seed: int, pixel_id: int, sample_id: int, bounce: int):
    if type(bounce) is not int or not 0 <= bounce <= (MASK-18)//16:
        raise InvalidBsdfSample('Bounce cannot address its three uint64 dimensions.')
    base = 16+16*bounce
    return tuple(uniform_sample(seed, pixel_id, sample_id, base+offset) for offset in range(3))


def sample_bsdf_addressed(material, wo, *, seed: int, pixel_id: int, sample_id: int,
                          bounce: int, policy=BSDF_POLICY) -> BsdfSample:
    return sample_bsdf(material, wo, addressed_bsdf_uniforms(seed, pixel_id, sample_id, bounce), policy=policy)
