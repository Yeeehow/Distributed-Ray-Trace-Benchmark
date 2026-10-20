"""Explicit file, validation and unsupported-feature errors for raytracing input."""


class GlbError(ValueError):
    """Base error for this workload's input API."""


class GlbFileError(GlbError):
    """The requested file could not be read."""


class GlbValidationError(GlbError):
    """Container or document metadata is invalid."""


class UnsupportedFeatureError(GlbError):
    """A feature is deliberately outside the implemented subset."""


class AccessorDecodeError(GlbValidationError):
    """Accessor metadata cannot safely describe the requested array."""


class GeometryError(GlbValidationError):
    """Primitive attributes or indices are invalid."""


class SceneValidationError(GlbValidationError):
    """Scene references, hierarchy, transforms or bounds are invalid."""


class SingularTransformError(SceneValidationError):
    """A requested inverse/normal transform does not exist or is not finite."""


class MetadataValidationError(GlbValidationError):
    """Camera, light, material or texture resource metadata is invalid."""


class RenderValidationError(GlbValidationError):
    """Render configuration, strip, primary-ray input or ray data is invalid."""


def require_int(
    value: object,
    label: str,
    minimum: int = 0,
    error: type[GlbValidationError] = GlbValidationError,
) -> int:
    """Reject booleans, missing values, fractional values and negative offsets."""
    if type(value) is not int or value < minimum:
        raise error(f"{label} must be an integer >= {minimum}.")
    return value
