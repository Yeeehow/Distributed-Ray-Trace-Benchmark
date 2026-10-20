"""One camera path, at most one BSDF event, real certified continuation/visibility."""
from dataclasses import dataclass
import math

from drt_backend.raytracing.tracing.surface_queries import trace_occluded
from .contracts import RadianceCountersV1
from .pbr_numeric import vector
from .bsdf import evaluate_bsdf
from .bsdf_sampling import sample_bsdf_addressed
from .transport_policy import validate_workload
from .transport_errors import InvalidTransport

COUNTER_NAMES = ('primary_rays', 'secondary_rays', 'shadow_rays', 'path_count', 'surface_interactions')


def add_counters(counters):
    rows = tuple(counters)
    return RadianceCountersV1(*(sum(getattr(row, name) for row in rows) for name in COUNTER_NAMES), 1)


class TraceEffort:
    """Executed work, independent of logical acceptance. Query starts count once.

    Optional observer runs in finally after a real query was initiated, including
    exceptional queries. It is diagnostic and must not mutate scene resources.
    """
    def __init__(self, on_trace=None):
        self.values = dict.fromkeys(COUNTER_NAMES, 0)
        self.on_trace = on_trace

    def snapshot(self):
        return RadianceCountersV1(*(self.values[n] for n in COUNTER_NAMES), 1)

    def query(self, category, operation):
        self.values[category] += 1
        self.snapshot()  # signed-int64 validation before entering geometry
        try:
            return operation()
        finally:
            if self.on_trace is not None:
                self.on_trace(category)


@dataclass(frozen=True, slots=True)
class PbrSampleResult:
    radiance: tuple[float, float, float]
    event: str
    endpoint: str

    def __post_init__(self):
        object.__setattr__(self, 'radiance', vector(self.radiance, 3, InvalidTransport, minimum=0))


def sample_radiance(scene, pixel_id, sample_id, effort, *, checkpoint=lambda: None):
    w = scene.workload
    environment = validate_workload(w)
    if type(sample_id) is not int or not 0 <= sample_id < w.target_spp:
        raise InvalidTransport('SampleId outside admitted SPP.')
    checkpoint()
    ray = scene.camera.addressed(w.seed, pixel_id, sample_id)
    effort.values['path_count'] += 1
    surface = effort.query('primary_rays', lambda: scene.closest(ray))
    if surface is not None:
        effort.values['surface_interactions'] += 1
    checkpoint()
    if surface is None:
        return PbrSampleResult(environment, 'none', 'primary_environment')
    frame, material = surface.frame, surface.material
    wo_world = tuple(-float(c) for c in ray.direction)
    wo = frame.to_local(wo_world)
    contributions = [material.emission]
    for light in scene.point_samples(surface):
        checkpoint()
        wi_world = tuple(float(c) for c in light.incoming_direction)
        if not frame.valid_reflection(wi_world, wo_world):
            continue
        wi = frame.to_local(wi_world)
        value = evaluate_bsdf(material, wi, wo, policy=w.bsdf).value
        if not any(value):
            continue
        spawned = scene.spawn_shadow(ray, surface, light)
        blocked = effort.query('shadow_rays', lambda: trace_occluded(
            scene.prepared.textures, spawned.ray, spawned.guard))
        if not blocked:
            contributions.append(tuple(value[c]*wi[2]*float(light.incident_rgb[c]) for c in range(3)))
    checkpoint()
    sample = sample_bsdf_addressed(material, wo, seed=w.seed, pixel_id=pixel_id,
                                  sample_id=sample_id, bounce=0, policy=w.bsdf)
    endpoint = 'no_continuation'
    if sample.can_contribute:
        incoming = frame.to_world(sample.wi)
        if not frame.valid_reflection(incoming, wo_world):
            raise InvalidTransport('Sampled continuation violates the geometric reflection hemisphere.')
        spawned = scene.spawn_continuation(ray, surface, incoming)
        checkpoint()
        secondary = effort.query('secondary_rays', lambda: scene.closest(spawned.ray, spawned.guard))
        if secondary is not None:
            effort.values['surface_interactions'] += 1
        checkpoint()
        if secondary is None:
            incident, endpoint = environment, 'secondary_environment'
        else:
            incident, endpoint = secondary.material.emission, 'secondary_surface_depth_limit'
        contributions.append(tuple(sample.throughput_weight[c]*incident[c] for c in range(3)))
    try:
        radiance = tuple(math.fsum(row[c] for row in contributions) for c in range(3))
    except (OverflowError, ValueError) as error:
        raise InvalidTransport('Radiance summation exceeds finite Float64.') from error
    return PbrSampleResult(radiance, sample.event, endpoint)
