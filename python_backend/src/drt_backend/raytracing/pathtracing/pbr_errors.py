"""M6.1 failures; the accepted M6.0 and Legacy error modules stay unchanged."""
from .errors import NumericalContractError


class InvalidPbrMaterial(NumericalContractError):
    """Invalid material factors, samples or binding."""


class UnsupportedPbrFeature(InvalidPbrMaterial):
    """A source feature needs a separately implemented interpretation."""


class InvalidSurfaceFrame(NumericalContractError):
    """Invalid normal, transform or tangent frame."""


class InvalidBsdfDirection(NumericalContractError):
    """A BSDF direction is not a finite unit vector."""


class InvalidBsdfParameter(NumericalContractError):
    """Unsupported BSDF policy or numerical domain."""


class InvalidBsdfPdf(NumericalContractError):
    """Invalid solid-angle density or discrete probability mass."""


class InvalidBsdfSample(NumericalContractError):
    """Invalid uniforms or inconsistent sample event/measure."""


class InvalidFresnelState(NumericalContractError):
    """Fresnel arguments are outside their finite physical domain."""


class DeltaMeasureError(InvalidBsdfParameter):
    """A discrete reflection cannot be evaluated as a finite GGX density."""
