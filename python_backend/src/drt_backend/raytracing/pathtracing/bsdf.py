"""Versioned local-frame reflection BSDF; no lighting or transport integration."""
from dataclasses import dataclass
import math

from .contracts import PolicyDescriptor
from .pbr_material import PbrMaterialParameters
from .pbr_numeric import vector, scalar, dot, normalize
from .pbr_errors import InvalidBsdfDirection, InvalidBsdfParameter, InvalidBsdfPdf
from .fresnel import schlick_fresnel
from .microfacet import ggx_ndf, smith_g1_over_cos

GLTF_REFERENCE_BSDF_POLICY = PolicyDescriptor('drt.bsdf.gltf_mr_ggx.v1', (
    ('alpha_floor', 0.0001), ('delta', 'exact_zero_roughness'),
    ('diffuse_fresnel', 'half_vector_max'),
    ('lobe_probability', 'equal_active'), ('sampling', 'full_ggx_ndf'),
    ('shading_geometry', 'external_geometric_hemisphere_test'), ('smith', 'separable_exact')))
BSDF_POLICY = PolicyDescriptor('drt.bsdf.gltf_mr_ggx.budget.v1', (
    ('alpha_floor', 0.0001), ('delta', 'exact_zero_roughness'),
    ('diffuse_fresnel', 'symmetric_hemisphere_bound'),
    ('lobe_probability', 'equal_active'), ('sampling', 'full_ggx_ndf'),
    ('shading_geometry', 'external_geometric_hemisphere_test'), ('smith', 'separable_exact')))


def validate_bsdf_policy(policy: PolicyDescriptor, *, require_passive: bool = True) -> None:
    if type(policy) is not PolicyDescriptor or policy not in (BSDF_POLICY, GLTF_REFERENCE_BSDF_POLICY):
        raise InvalidBsdfParameter('Unsupported executable BSDF identity/parameters.')
    if type(require_passive) is not bool:
        raise InvalidBsdfParameter('Passivity requirement must be explicit bool.')
    if require_passive and policy != BSDF_POLICY:
        raise InvalidBsdfParameter('The exact glTF reference policy is diagnostic, not passivity-certified.')


def direction(value):
    return vector(value, 3, InvalidBsdfDirection, unit=True)


def reflection_half_vector(wi, wo):
    wi, wo = direction(wi), direction(wo)
    return normalize(tuple(a+b for a, b in zip(wi, wo)), InvalidBsdfDirection)


def lambert_brdf(reflectance):
    rho = vector(reflectance, 3, InvalidBsdfParameter, minimum=0, maximum=1)
    return tuple(c/math.pi for c in rho)


def alpha_for_material(material: PbrMaterialParameters, *, policy=BSDF_POLICY):
    validate_bsdf_policy(policy, require_passive=False)
    if type(material) is not PbrMaterialParameters:
        raise InvalidBsdfParameter('Expected resolved PbrMaterialParameters.')
    return 0.0 if material.roughness == 0 else max(material.roughness**2, 0.0001)


def active_lobes(material: PbrMaterialParameters):
    if type(material) is not PbrMaterialParameters:
        raise InvalidBsdfParameter('Expected resolved material.')
    diffuse = (material.metallic < 1 and max(material.base_color) > 0
               and max(material.dielectric_f0) < 1)
    # Black metallic F0 still has a Schlick grazing lobe: never remove support.
    specular = material.metallic > 0 or material.specular_strength > 0
    return diffuse, specular


def lobe_probability(material: PbrMaterialParameters) -> float:
    diffuse, specular = active_lobes(material)
    return 0.5 if diffuse and specular else float(specular)


def fresnel_terms(material, cosine):
    dielectric = schlick_fresnel(cosine, material.dielectric_f0, material.specular_strength)
    metal = schlick_fresnel(cosine, material.base_color)
    specular = tuple((1-material.metallic)*d+material.metallic*m for d, m in zip(dielectric, metal))
    diffuse = tuple((1-material.metallic)*(1-max(dielectric))*c/math.pi for c in material.base_color)
    return diffuse, specular


def dielectric_fresnel_bound(material, cosine):
    """Maximum dielectric Fresnel over all valid reflected hemisphere directions.

    For fixed wo.z=c>0, cos(wi,h) >= c/sqrt(2*(1+sqrt(1-c*c))).
    Schlick decreases with its cosine. This upper bound therefore also bounds
    dielectric specular hemispherical reflectance for a passive Smith GGX lobe.
    """
    c = scalar(cosine, InvalidBsdfParameter, minimum=0, maximum=1)
    minimum_half_cos = c/math.sqrt(2*(1+math.sqrt((1-c)*(1+c))))
    return max(schlick_fresnel(minimum_half_cos, material.dielectric_f0, material.specular_strength))


@dataclass(frozen=True, slots=True)
class BsdfEvaluation:
    diffuse: tuple[float, float, float]
    specular: tuple[float, float, float]
    has_delta: bool

    def __post_init__(self):
        for name in ('diffuse', 'specular'):
            object.__setattr__(self, name, vector(getattr(self, name), 3, InvalidBsdfParameter, minimum=0))
        if type(self.has_delta) is not bool or not all(math.isfinite(c) for c in self.value):
            raise InvalidBsdfParameter('Invalid/overflowing BRDF evaluation.')

    @property
    def value(self):
        return tuple(a+b for a, b in zip(self.diffuse, self.specular))


@dataclass(frozen=True, slots=True)
class BsdfPdf:
    """Full-mixture continuous solid-angle density (sr^-1), excluding deltas."""
    solid_angle_density: float

    def __post_init__(self):
        object.__setattr__(self, 'solid_angle_density', scalar(self.solid_angle_density, InvalidBsdfPdf, minimum=0))


def evaluate_bsdf(material: PbrMaterialParameters, wi, wo, *, policy=BSDF_POLICY) -> BsdfEvaluation:
    """wi/wo both leave the surface, local +Z=Ns; output is BRDF sr^-1.

    Opposite/zero shading cosines give physical zero. A delta specular lobe is
    excluded from finite-density evaluation and flagged by has_delta.
    """
    alpha = alpha_for_material(material, policy=policy)
    wi, wo = direction(wi), direction(wo)
    delta = alpha == 0 and lobe_probability(material) > 0
    if wi[2] <= 0 or wo[2] <= 0:
        return BsdfEvaluation((0.0,)*3, (0.0,)*3, delta)
    h = reflection_half_vector(wi, wo)
    # Clamp only internally computed unit-vector dot roundoff, never user data.
    cosine = min(1.0, max(0.0, dot(wi, h)))
    diffuse, fresnel = fresnel_terms(material, cosine)
    if policy == BSDF_POLICY:
        # Separate, explicitly authorized conservative model. Symmetry preserves
        # reciprocity. For each wo: diffuse energy <=1-bound(wo), while the
        # unchanged dielectric specular energy <=bound(wo). Metals are unchanged.
        remaining = 1-max(dielectric_fresnel_bound(material, wi[2]),
                          dielectric_fresnel_bound(material, wo[2]))
        diffuse = tuple((1-material.metallic)*remaining*c/math.pi for c in material.base_color)
    if alpha == 0:
        return BsdfEvaluation(diffuse, (0.0,)*3, delta)
    d = ggx_ndf(min(1.0, h[2]), alpha)
    scale = 0.25*d*smith_g1_over_cos(wi[2], alpha)*smith_g1_over_cos(wo[2], alpha)
    return BsdfEvaluation(diffuse, tuple(c*scale for c in fresnel), False)


def cosine_hemisphere_pdf(wi):
    return max(0.0, direction(wi)[2])/math.pi


def ggx_reflection_pdf(wi, wo, alpha):
    """Unconditioned full-NDF reflection density; null mass is not renormalized."""
    wi, wo = direction(wi), direction(wo)
    from .microfacet import _alpha
    alpha = _alpha(alpha)
    if wi[2] <= 0 or wo[2] <= 0:
        return 0.0
    h = reflection_half_vector(wi, wo)
    cosine = abs(dot(wo, h))
    if cosine == 0:
        return 0.0
    value = ggx_ndf(min(1.0, h[2]), alpha)*max(0.0, h[2])/(4*cosine)
    return BsdfPdf(value).solid_angle_density


def evaluate_pdf(material: PbrMaterialParameters, wi, wo, *, policy=BSDF_POLICY) -> BsdfPdf:
    alpha = alpha_for_material(material, policy=policy)
    wi, wo = direction(wi), direction(wo)
    if wo[2] <= 0 or not any(active_lobes(material)):
        return BsdfPdf(0.0)
    q = lobe_probability(material)
    specular = 0.0 if alpha == 0 else ggx_reflection_pdf(wi, wo, alpha)
    return BsdfPdf((1-q)*cosine_hemisphere_pdf(wi)+q*specular)


def delta_reflection(material: PbrMaterialParameters, wo, *, policy=BSDF_POLICY):
    """Return (mirror wi, RGB integrated coefficient, discrete lobe mass).

    Integrator weight is coefficient/mass; no cosine factor is applied again.
    """
    if alpha_for_material(material, policy=policy) != 0:
        raise InvalidBsdfParameter('Material has a continuous specular lobe.')
    wo = direction(wo)
    if wo[2] <= 0:
        raise InvalidBsdfDirection('Delta event requires a positive outgoing cosine.')
    _, coefficient = fresnel_terms(material, min(1.0, wo[2]))
    return (-wo[0], -wo[1], wo[2]), coefficient, lobe_probability(material)
