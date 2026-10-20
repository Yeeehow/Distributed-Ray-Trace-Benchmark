"""Bounded Schlick interpolation with an explicit grazing reflectance."""
from .pbr_numeric import scalar, vector
from .pbr_errors import InvalidFresnelState


def schlick_fresnel(cosine, f0, f90=1.0):
    c = scalar(cosine, InvalidFresnelState, minimum=0, maximum=1)
    initial = vector(f0, 3, InvalidFresnelState, minimum=0, maximum=1)
    grazing = scalar(f90, InvalidFresnelState, minimum=0, maximum=1)
    if any(component > grazing for component in initial):
        raise InvalidFresnelState('F0 must not exceed the selected grazing reflectance.')
    weight = (1-c)**5
    return tuple(component+(grazing-component)*weight for component in initial)
