"""Display conversion only after canonical Float64 radiance reduction."""
import math
import struct
from .color_space import linear_to_srgb
from .pbr_numeric import vector
from .transport_errors import InvalidTransport, UnsupportedTransport
from .transport_policy import COLOR_POLICY


def packed_linear(mean_rgb):
    rows = tuple(vector(row, 3, InvalidTransport, minimum=0) for row in mean_rgb)
    return b''.join(struct.pack('<3d', *row) for row in rows)


def rgb8(mean_rgb, *, policy=COLOR_POLICY):
    if policy != COLOR_POLICY:
        raise UnsupportedTransport('Unsupported display transform.')
    rows = tuple(vector(row, 3, InvalidTransport, minimum=0) for row in mean_rgb)
    return bytes(math.floor(255*linear_to_srgb(min(1.0, c))+0.5) for row in rows for c in row)
