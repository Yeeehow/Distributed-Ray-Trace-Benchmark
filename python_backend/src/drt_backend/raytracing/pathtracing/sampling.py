"""Pure addressed SplitMix64 reference contract, with explicit 64-bit wrapping.

No mutable generator, call counter, random module, hash(), or execution identity.
Address collisions are possible: this is not a 256-to-64-bit injective mapping.
"""
from .errors import InvalidSamplingAddress

SAMPLER_VERSION = 'drt.splitmix64.counter.v1'
DIMENSION_LAYOUT_VERSION = 'drt.path_dimensions.v1'
MASK = 0xFFFFFFFFFFFFFFFF
C0 = 0x243F6A8885A308D3
C1 = 0x13198A2E03707344
C2 = 0xA4093822299F31D0
C3 = 0x082EFA98EC4E6C89

# Layout v1 reserves camera dimensions [0,16). At depth b >= 0 reserve
# [16+16*b, 32+16*b): BSDF offsets 0..2, light 3..5, alpha 6, RR 7,
# remaining offsets 8..15 reserved. No consumer or path algorithm exists yet.
# Repeated alpha events need an explicit later layout, never a mutable cursor.


def _uint64(value: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MASK:
        raise InvalidSamplingAddress(f'{label} requires a native uint64 integer.')
    return value


def mix64(value: int) -> int:
    """Specified unsigned SplitMix64 mixing steps; input is strictly uint64."""
    z = (_uint64(value, 'mix input') + 0x9E3779B97F4A7C15) & MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
    return (z ^ (z >> 31)) & MASK


def sampling_word(seed: int, pixel_id: int, sample_id: int, dimension_id: int) -> int:
    """Canonical word at (seed, y*width+x, sample, dimension)."""
    for value, label in ((seed, 'seed'), (pixel_id, 'pixel_id'),
                         (sample_id, 'sample_id'), (dimension_id, 'dimension_id')):
        _uint64(value, label)
    h = mix64(seed ^ C0)
    h = mix64(h ^ mix64(pixel_id ^ C1))
    h = mix64(h ^ mix64(sample_id ^ C2))
    return mix64(h ^ mix64(dimension_id ^ C3))


def uniform_sample(seed: int, pixel_id: int, sample_id: int, dimension_id: int) -> float:
    """Binary64 uniform value in [0,1), using the top 53 canonical word bits."""
    return float(sampling_word(seed, pixel_id, sample_id, dimension_id) >> 11) * 2**-53
