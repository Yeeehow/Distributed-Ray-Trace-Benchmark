"""Explicit presentation-only exposure; saved Float64 radiance stays unscaled."""
from drt_backend.raytracing.pathtracing.color_pipeline import rgb8
from .policy import COLOR_POLICY


def room_rgb8(means,policy):
    if policy!=COLOR_POLICY:raise ValueError('Unknown room output policy.')
    ev=dict(policy.parameters)['exposure_ev']
    return rgb8(tuple(tuple(c*2.**ev for c in v) for v in means))
