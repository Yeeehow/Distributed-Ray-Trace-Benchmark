"""Immutable future-workload identity and synthetic linear-radiance contracts.

Descriptors identify proposed algorithms; they do not implement or advertise
them. Canonical JSON is sorted compact UTF-8; finite float parameters use exact
float.hex() strings under a type tag. Digest domains are distinct NUL-terminated
ASCII prefixes. No scheduler identifiers, resources, or codecs are replaced.
"""
from dataclasses import dataclass, fields
from hashlib import sha256
import json
import math
import re

from .errors import (InvalidWorkloadConfiguration, InvalidTaskInterval,
                     InvalidRadiancePayload, NonFiniteRadiance, WorkloadMismatch)
from .sampling import SAMPLER_VERSION, DIMENSION_LAYOUT_VERSION, MASK

WORKLOAD_KIND = 'drt.pbr_pathtracer.v1'
WORKLOAD_DOMAIN = b'drt.workload_spec.v1\x00'
TASK_DOMAIN = b'drt.sample_task_content.v1\x00'
INT64_MAX = (1 << 63) - 1
BLOCK_SIZE = 8


def _integer(value, label, minimum, maximum, error):
    if type(value) is not int or not minimum <= value <= maximum:
        raise error(f'{label} requires an integer in [{minimum},{maximum}].')


def _digest(value, error):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise error('Digest requires 64 lower-case SHA256 hex digits.')


def _text(value):
    if type(value) is not str or not value or len(value) > 256:
        raise InvalidWorkloadConfiguration('Descriptor text requires 1..256 characters.')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeError as error:
        raise InvalidWorkloadConfiguration('Descriptor text must be valid UTF-8.') from error


def _scalar(value):
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        _text(value)
        return value
    if type(value) is int:
        _integer(value, 'descriptor integer', -INT64_MAX-1, MASK, InvalidWorkloadConfiguration)
        return value
    if type(value) is float and math.isfinite(value):
        return {'float64': value.hex()}
    raise InvalidWorkloadConfiguration('Parameters require finite native immutable scalars.')


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


@dataclass(frozen=True, slots=True)
class PolicyDescriptor:
    """Explicit versioned identity and sorted immutable scalar parameters.

    Every actual algorithm must later validate its own complete parameter schema
    before execution; M6.0 descriptors confer no execution capability.
    """
    identity: str
    parameters: tuple[tuple[str, object], ...]

    def __post_init__(self):
        if (type(self.identity) is not str or len(self.identity) > 128
                or re.fullmatch(r'drt\.[a-z0-9_.]+\.v[1-9][0-9]*', self.identity) is None):
            raise InvalidWorkloadConfiguration('Policy requires an explicit drt.*.vN identity.')
        if type(self.parameters) is not tuple or len(self.parameters) > 64:
            raise InvalidWorkloadConfiguration('Policy parameters require a bounded immutable tuple.')
        keys = []
        for pair in self.parameters:
            if type(pair) is not tuple or len(pair) != 2:
                raise InvalidWorkloadConfiguration('Each parameter requires (name, scalar).')
            key, value = pair
            _text(key)
            _scalar(value)
            keys.append(key)
        if keys != sorted(set(keys)):
            raise InvalidWorkloadConfiguration('Parameter keys must be unique and sorted.')

    def canonical_data(self) -> dict:
        return {'identity': self.identity, 'parameters': {k: _scalar(v) for k, v in self.parameters}}


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    """Job-level numerical identity; all configuration must be supplied explicitly."""
    workload_kind: str
    schema_version: int
    scene_sha256: str
    scene_index: int
    camera_node_index: int
    width: int
    height: int
    sampler_version: str
    dimension_layout_version: str
    seed: int
    target_spp: int
    sample_block_size: int
    max_path_depth: int
    termination_policy: PolicyDescriptor
    material_model: PolicyDescriptor
    bsdf: PolicyDescriptor
    integrator: PolicyDescriptor
    alpha_policy: PolicyDescriptor
    lighting_policy: PolicyDescriptor
    rendering_policy: PolicyDescriptor
    color_output_policy: PolicyDescriptor

    def __post_init__(self):
        if type(self.workload_kind) is not str or self.workload_kind != WORKLOAD_KIND:
            raise InvalidWorkloadConfiguration('Unsupported workload kind.')
        _integer(self.schema_version, 'schema_version', 1, 1, InvalidWorkloadConfiguration)
        _digest(self.scene_sha256, InvalidWorkloadConfiguration)
        for label in ('scene_index', 'camera_node_index'):
            _integer(getattr(self, label), label, 0, INT64_MAX, InvalidWorkloadConfiguration)
        for label in ('width', 'height'):
            _integer(getattr(self, label), label, 1, 16384, InvalidWorkloadConfiguration)
        if self.width * self.height > 67_108_864:
            raise InvalidWorkloadConfiguration('Image exceeds the existing dimension contract.')
        if (type(self.sampler_version) is not str or self.sampler_version != SAMPLER_VERSION
                or type(self.dimension_layout_version) is not str
                or self.dimension_layout_version != DIMENSION_LAYOUT_VERSION):
            raise InvalidWorkloadConfiguration('Unsupported sampler or dimension layout.')
        _integer(self.seed, 'seed', 0, MASK, InvalidWorkloadConfiguration)
        _integer(self.target_spp, 'target_spp', 1, MASK, InvalidWorkloadConfiguration)
        _integer(self.sample_block_size, 'sample_block_size', BLOCK_SIZE, BLOCK_SIZE,
                 InvalidWorkloadConfiguration)
        # Native Python integer arithmetic cannot wrap. Bound aggregate units to
        # the existing generic scheduler's signed 64-bit expected_units contract.
        if self.pixel_count * self.target_spp > INT64_MAX:
            raise InvalidWorkloadConfiguration('Pixel-sample count exceeds signed 64-bit units.')
        _integer(self.max_path_depth, 'max_path_depth', 1, 1024, InvalidWorkloadConfiguration)
        for label in ('termination_policy', 'material_model', 'bsdf', 'integrator',
                      'alpha_policy', 'lighting_policy', 'rendering_policy', 'color_output_policy'):
            if type(getattr(self, label)) is not PolicyDescriptor:
                raise InvalidWorkloadConfiguration(f'{label} requires a PolicyDescriptor.')

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    def canonical_bytes(self) -> bytes:
        data = {}
        for field in fields(self):
            value = getattr(self, field.name)
            data[field.name] = value.canonical_data() if type(value) is PolicyDescriptor else value
        return _canonical(data)

    @property
    def workload_digest(self) -> str:
        return sha256(WORKLOAD_DOMAIN + self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class SampleTaskSpec:
    """Global row-major pixel range and exactly one canonical sample block.

    TaskContentDigest is numerical content only, never scheduler TaskId/RunId.
    Pixel partitions may vary without changing any pixel's sample-block sums.
    """
    workload: WorkloadSpec
    pixel_start: int
    pixel_end: int
    sample_start: int
    sample_end: int

    def __post_init__(self):
        if type(self.workload) is not WorkloadSpec:
            raise InvalidTaskInterval('Task requires WorkloadSpec.')
        for label in ('pixel_start', 'pixel_end', 'sample_start', 'sample_end'):
            _integer(getattr(self, label), label, 0, MASK, InvalidTaskInterval)
        if not 0 <= self.pixel_start < self.pixel_end <= self.workload.pixel_count:
            raise InvalidTaskInterval('Pixel range is empty or out of bounds.')
        if (not 0 <= self.sample_start < self.sample_end <= self.workload.target_spp
                or self.sample_start % BLOCK_SIZE != 0
                or self.sample_end != min(self.sample_start + BLOCK_SIZE, self.workload.target_spp)):
            raise InvalidTaskInterval('Sample interval must be one canonical block, including its final tail.')

    @property
    def workload_digest(self) -> str:
        return self.workload.workload_digest

    @property
    def pixel_count(self) -> int:
        return self.pixel_end - self.pixel_start

    @property
    def sample_count(self) -> int:
        return self.sample_end - self.sample_start

    def canonical_bytes(self) -> bytes:
        return _canonical({'schema_version': 1, 'workload_digest': self.workload_digest,
                           'pixel_start': self.pixel_start, 'pixel_end': self.pixel_end,
                           'sample_start': self.sample_start, 'sample_end': self.sample_end})

    @property
    def task_content_digest(self) -> str:
        return sha256(TASK_DOMAIN + self.canonical_bytes()).hexdigest()


def iter_sample_tasks(workload: WorkloadSpec, pixels_per_task: int):
    """Synthetic grid iterator, not a scheduler or a production workload adapter."""
    if type(workload) is not WorkloadSpec:
        raise InvalidTaskInterval('Grid requires WorkloadSpec.')
    _integer(pixels_per_task, 'pixels_per_task', 1, workload.pixel_count, InvalidTaskInterval)
    for start in range(0, workload.pixel_count, pixels_per_task):
        for sample in range(0, workload.target_spp, BLOCK_SIZE):
            yield SampleTaskSpec(workload, start, min(start + pixels_per_task, workload.pixel_count),
                                 sample, min(sample + BLOCK_SIZE, workload.target_spp))


@dataclass(frozen=True, slots=True)
class RadianceCountersV1:
    """Actual traced effort for this block; None at result level means unknown.

    These become accepted contributions only downstream of central acceptance.
    BVH visits/triangle tests are separate diagnostics, never implicit rays.
    """
    primary_rays: int
    secondary_rays: int
    shadow_rays: int
    path_count: int
    surface_interactions: int
    schema_version: int

    def __post_init__(self):
        for field in fields(self):
            _integer(getattr(self, field.name), field.name, 0, INT64_MAX, InvalidRadiancePayload)
        if self.schema_version != 1 or self.traced_ray_total > INT64_MAX:
            raise InvalidRadiancePayload('Unsupported counter schema or overflowing ray total.')

    @property
    def traced_ray_total(self) -> int:
        return self.primary_rays + self.secondary_rays + self.shadow_rays


@dataclass(frozen=True, slots=True)
class PbrRadianceBlockResultV1:
    """Immutable (pixel_count,3) binary64 RGB block sums, increasing PixelId.

    Native Python floats are required, without dtype conversion. No per-sample
    colors, tone mapping, indices buffer, unknown fields, or live resources.
    sample_count is per pixel; accepted_pixel_samples is a separate derived unit.
    This in-process schema is NOT an IPC/LAN serialization or capability.
    """
    workload_digest: str
    task_content_digest: str
    pixel_start: int
    pixel_end: int
    sample_start: int
    sample_end: int
    sample_count: int
    radiance_sums: tuple[tuple[float, float, float], ...]
    counters: RadianceCountersV1 | None
    schema_version: int

    def __post_init__(self):
        _digest(self.workload_digest, InvalidRadiancePayload)
        _digest(self.task_content_digest, InvalidRadiancePayload)
        _integer(self.schema_version, 'result schema', 1, 1, InvalidRadiancePayload)
        for label in ('pixel_start', 'pixel_end', 'sample_start', 'sample_end', 'sample_count'):
            _integer(getattr(self, label), label, 0, INT64_MAX, InvalidRadiancePayload)
        if (self.pixel_start >= self.pixel_end or self.sample_start >= self.sample_end
                or self.sample_count != self.sample_end - self.sample_start
                or self.accepted_pixel_samples > INT64_MAX):
            raise InvalidRadiancePayload('Invalid result intervals or sample count.')
        if (type(self.radiance_sums) is not tuple
                or len(self.radiance_sums) != self.pixel_end - self.pixel_start):
            raise InvalidRadiancePayload('Radiance must have exactly one tuple per global pixel.')
        for rgb in self.radiance_sums:
            if type(rgb) is not tuple or len(rgb) != 3 or any(type(c) is not float for c in rgb):
                raise InvalidRadiancePayload('Radiance shape/precision must be (N,3) native binary64.')
            if any(not math.isfinite(c) for c in rgb):
                raise NonFiniteRadiance('Radiance must be finite.')
            if any(c < 0 for c in rgb):
                raise InvalidRadiancePayload('Radiance must be nonnegative.')
        if self.counters is not None and type(self.counters) is not RadianceCountersV1:
            raise InvalidRadiancePayload('Counters require schema 1 or explicit unknown None.')

    @property
    def accepted_pixel_samples(self) -> int:
        return (self.pixel_end - self.pixel_start) * self.sample_count

    @classmethod
    def for_task(cls, task: SampleTaskSpec, radiance_sums, *, counters):
        if type(task) is not SampleTaskSpec:
            raise InvalidRadiancePayload('Result requires SampleTaskSpec.')
        return cls(task.workload_digest, task.task_content_digest, task.pixel_start, task.pixel_end,
                   task.sample_start, task.sample_end, task.sample_count, radiance_sums, counters, 1)

    def validate_for(self, task: SampleTaskSpec) -> None:
        if type(task) is not SampleTaskSpec:
            raise InvalidRadiancePayload('Binding requires SampleTaskSpec.')
        if self.workload_digest != task.workload_digest:
            raise WorkloadMismatch('Result WorkloadDigest differs from task.')
        if (self.task_content_digest != task.task_content_digest
                or (self.pixel_start, self.pixel_end, self.sample_start, self.sample_end,
                    self.sample_count) != (task.pixel_start, task.pixel_end, task.sample_start,
                                          task.sample_end, task.sample_count)):
            raise InvalidRadiancePayload('Result TaskContentDigest, intervals, or count differs from task.')
