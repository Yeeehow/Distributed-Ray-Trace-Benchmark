"""Two conditional solid-angle techniques; discrete masses never enter this helper."""
from .pbr_numeric import scalar
from .transport_errors import InvalidTransport


class InvalidMis(InvalidTransport):
    """Incompatible or undefined MIS probability state."""


def power_weight(selected_density, competing_density):
    a=scalar(selected_density,InvalidMis,minimum=0)
    b=scalar(competing_density,InvalidMis,minimum=0)
    scale=max(a,b)
    if scale==0: raise InvalidMis('Two zero densities do not define an MIS weight.')
    a,b=a/scale,b/scale
    return a*a/(a*a+b*b)
