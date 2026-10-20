"""Isotropic single-scattering GGX and exact separable Smith, not correlated G."""
import math
from .pbr_errors import InvalidBsdfParameter, DeltaMeasureError
from .pbr_numeric import scalar


def _alpha(alpha):
    alpha = scalar(alpha, InvalidBsdfParameter, minimum=0, maximum=1)
    if alpha == 0:
        raise DeltaMeasureError('Zero alpha has a discrete measure, not a GGX NDF/PDF.')
    if alpha < 0.0001:
        raise InvalidBsdfParameter('This version requires non-delta alpha >= 0.0001.')
    return alpha


def ggx_ndf(cos_half: float, alpha: float) -> float:
    alpha = _alpha(alpha)
    c = scalar(cos_half, InvalidBsdfParameter, minimum=0, maximum=1)
    a2 = alpha*alpha
    denominator = (1-c)*(1+c)+a2*c*c
    if denominator == 0 or a2 == 0:
        raise InvalidBsdfParameter('GGX alpha is too small for finite binary64 evaluation.')
    result = a2/(math.pi*denominator*denominator)
    if not math.isfinite(result):
        raise InvalidBsdfParameter('GGX NDF overflows binary64.')
    return result


def smith_g1_over_cos(cosine: float, alpha: float) -> float:
    alpha = _alpha(alpha)
    c = scalar(cosine, InvalidBsdfParameter, minimum=0, maximum=1)
    return 2/(c+math.hypot(alpha*math.sqrt((1-c)*(1+c)), c))


def smith_g1(cosine: float, alpha: float) -> float:
    return scalar(cosine, InvalidBsdfParameter, minimum=0, maximum=1)*smith_g1_over_cos(cosine, alpha)


def smith_g(cos_in: float, cos_out: float, alpha: float) -> float:
    return smith_g1(cos_in, alpha)*smith_g1(cos_out, alpha)
