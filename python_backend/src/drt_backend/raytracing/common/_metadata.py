"""Shared metadata validation; raw JSON takes precedence over binding defaults."""

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
import math
from numbers import Real
from types import MappingProxyType
from typing import Any

from drt_backend.raytracing.common.errors import MetadataValidationError, require_int
from drt_backend.raytracing.scene.glb_loader import GlbAsset


def object_data(value: object, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in fields(value) if getattr(value, f.name) is not None}
    raise MetadataValidationError(f"{label} must be an object.")


def resources(asset: GlbAsset, name: str) -> tuple[Mapping[str, Any], ...]:
    parsed = getattr(asset.document, name, [])
    if not isinstance(parsed, (list, tuple)):
        raise MetadataValidationError(f"Document {name} must be an array.")
    values = asset.raw_document.get(name, []) if asset.raw_document is not None else parsed
    if not isinstance(values, (list, tuple)) or len(values) != len(parsed):
        raise MetadataValidationError(f"Raw and parsed {name} metadata are inconsistent.")
    return tuple(object_data(value, f"{name}[{i}]") for i, value in enumerate(values))


def optional(source: Mapping[str, Any], key: str) -> Any:
    """Absent optional values stay absent; explicit JSON null is invalid."""
    if key in source and source[key] is None:
        raise MetadataValidationError(f"{key} cannot be null.")
    return source.get(key)


def index(value: object, count: int, label: str) -> int:
    result = require_int(value, label, error=MetadataValidationError)
    if result >= count:
        raise MetadataValidationError(f"{label} is out of range.")
    return result


def number(value: object, label: str, minimum: float | None = None,
           maximum: float | None = None, *, exclusive_minimum: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MetadataValidationError(f"{label} must be a finite number.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise MetadataValidationError(f"{label} must be a finite number.") from error
    if not math.isfinite(result):
        raise MetadataValidationError(f"{label} must be a finite number.")
    if minimum is not None and (result < minimum or (exclusive_minimum and result == minimum)):
        raise MetadataValidationError(f"{label} is below its allowed range.")
    if maximum is not None and result > maximum:
        raise MetadataValidationError(f"{label} exceeds its allowed range.")
    return result


def vector(value: object, size: int, label: str, minimum: float = 0,
           maximum: float = 1) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != size:
        raise MetadataValidationError(f"{label} must have {size} numeric components.")
    return tuple(number(component, label, minimum, maximum) for component in value)


def name(source: Mapping[str, Any]) -> str | None:
    value = optional(source, "name")
    if value is not None and not isinstance(value, str):
        raise MetadataValidationError("Resource name must be a string.")
    return value


def extensions(source: Mapping[str, Any]) -> Mapping[str, Any]:
    value = object_data(source.get("extensions", {}), "extensions")
    if any(not isinstance(key, str) or not isinstance(item, Mapping) for key, item in value.items()):
        raise MetadataValidationError("Extension names must map to metadata objects.")
    return MappingProxyType(dict(value))


def retained(source: Mapping[str, Any]) -> Mapping[str, Any]:
    """Protect the top-level association; nested source metadata remains asset-owned."""
    return MappingProxyType(dict(source))
