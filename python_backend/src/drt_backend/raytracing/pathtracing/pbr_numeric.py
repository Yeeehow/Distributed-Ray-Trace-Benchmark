"""Small shared finite binary64/vector checks, with no geometry or tracing."""
import math
from numbers import Real

import numpy as np

UNIT_TOLERANCE = 2e-12


def scalar(value, error, *, minimum=None, maximum=None):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise error('Expected a finite real scalar, not a coercible string/bool.')
    try:
        result = float(value)
    except (OverflowError, ValueError, TypeError) as cause:
        raise error('Scalar cannot be represented in binary64.') from cause
    if (not math.isfinite(result) or minimum is not None and result < minimum
            or maximum is not None and result > maximum):
        raise error('Scalar is outside its finite allowed range.')
    return result


def vector(value, size, error, *, minimum=None, maximum=None, unit=False):
    if isinstance(value, (list, tuple)) and any(isinstance(c, (bool, np.bool_)) for c in value):
        raise error('Boolean vector components are not real numerical inputs.')
    try:
        array = np.asarray(value)
    except (TypeError, ValueError, OverflowError) as cause:
        raise error('Invalid numeric vector.') from cause
    if array.shape != (size,) or array.dtype.kind not in 'iuf':
        raise error(f'Expected a real vector with shape ({size},).')
    result = tuple(scalar(c, error, minimum=minimum, maximum=maximum) for c in array)
    if unit and not math.isclose(math.hypot(*result), 1.0, rel_tol=0, abs_tol=UNIT_TOLERANCE):
        raise error('Direction/basis must already have unit length.')
    return result


def dot(a, b):
    return math.fsum(x*y for x, y in zip(a, b, strict=True))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def normalize(value, error):
    value = vector(value, 3, error)
    scale = max(abs(c) for c in value)
    if scale < 1e-14:
        raise error('Vector is zero or below the declared 1e-14 normalization threshold.')
    scaled = tuple(c/scale for c in value)
    length = math.hypot(*scaled)
    return tuple(c/length for c in scaled)
