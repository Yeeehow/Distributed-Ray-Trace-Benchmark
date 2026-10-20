"""KHR_lights_punctual metadata; no radiometry conversion or light evaluation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from drt_backend.raytracing.common._metadata import extensions, index, name, number, object_data, optional, retained, vector
from drt_backend.raytracing.model.camera import rigid_orientation
from drt_backend.raytracing.common.errors import MetadataValidationError
from drt_backend.raytracing.scene.scene import Scene
from drt_backend.raytracing.scene.transforms import numeric_array


@dataclass(frozen=True, slots=True)
class LightData:
    light_index: int
    node_index: int
    name: str | None
    type: str
    color: tuple[float, ...]
    intensity: float
    intensity_unit: str
    range: float | None
    world_matrix: NDArray[np.float64]
    world_position: NDArray[np.float64] | None
    world_direction: NDArray[np.float64] | None
    inner_cone_angle: float | None
    outer_cone_angle: float | None
    source: Mapping[str, Any]
    extensions: Mapping[str, Any]


def _properties(source: Mapping[str, Any]) -> dict[str, Any]:
    kind = source.get("type")
    if not isinstance(kind, str) or kind not in {"point", "spot", "directional"}:
        raise MetadataValidationError("Light type must be point, spot or directional.")
    range_value = optional(source, "range")
    light_range = number(range_value, "Light range", 0, exclusive_minimum=True) if range_value is not None else None
    inner = outer = None
    if kind == "spot":
        spot = object_data(source.get("spot"), "Spot light cone")
        inner = number(spot.get("innerConeAngle", 0), "Spot innerConeAngle", 0)
        outer = number(spot.get("outerConeAngle", math.pi / 4), "Spot outerConeAngle", 0, math.pi / 2,
                       exclusive_minimum=True)
        if inner >= outer:
            raise MetadataValidationError("Spot innerConeAngle must be less than outerConeAngle.")
    elif "spot" in source:
        raise MetadataValidationError("Only a spot light may have spot cone metadata.")
    return dict(
        name=name(source), type=kind, color=vector(source.get("color", [1, 1, 1]), 3, "Light color"),
        intensity=number(source.get("intensity", 1), "Light intensity", 0),
        intensity_unit="lux" if kind == "directional" else "candela", range=light_range,
        inner_cone_angle=inner, outer_cone_angle=outer, source=retained(source), extensions=extensions(source),
    )


def light_instances(scene: Scene, sources: Sequence[Mapping[str, Any]]) -> tuple[LightData, ...]:
    definitions = tuple(_properties(source) for source in sources)
    result = []
    for node in scene.nodes.values():
        if node.light_index is None:
            continue
        light_index = index(node.light_index, len(definitions), "Light index")
        properties = definitions[light_index]
        kind = properties["type"]
        direction = None
        if kind != "point":
            orientation = rigid_orientation(node.world_matrix)
            direction = numeric_array(-orientation[:, 2], (3,), "Light direction")
        result.append(LightData(
            light_index=light_index, node_index=node.node_index, world_matrix=node.world_matrix,
            world_position=node.world_matrix[:3, 3] if kind != "directional" else None,
            world_direction=direction, **properties,
        ))
    return tuple(result)
