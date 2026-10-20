"""Slot-specific scalar transfer functions; no image decoder or tone mapping."""
from .pbr_errors import InvalidPbrMaterial
from .pbr_numeric import scalar


def srgb_to_linear(value: float) -> float:
    value = scalar(value, InvalidPbrMaterial, minimum=0, maximum=1)
    return value/12.92 if value <= 0.04045 else ((value+0.055)/1.055)**2.4


def linear_to_srgb(value: float) -> float:
    value = scalar(value, InvalidPbrMaterial, minimum=0, maximum=1)
    return 12.92*value if value <= 0.0031308 else 1.055*value**(1/2.4)-0.055
