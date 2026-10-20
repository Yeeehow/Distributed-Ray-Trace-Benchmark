"""In-process Serial WorkloadAdapter behind the existing central TaskAcceptance."""
from dataclasses import dataclass, field
import json
from uuid import UUID

from drt_scheduler import JobDefinition, TaskDefinition, ResultRejectedError, canonical_data
from .contracts import SampleTaskSpec, PbrRadianceBlockResultV1, iter_sample_tasks
from .accumulation import reduce_authorized_blocks
from .color_pipeline import packed_linear, rgb8
from .integrator import TraceEffort, add_counters
from .scene_adapter import PbrSource, PbrSceneAdapter
from .serial_rendering import execute_block
from .transport_policy import validate_workload
from .transport_errors import InvalidTransport


@dataclass(frozen=True, slots=True)
class PbrSerialFrame:
    reduction: object
    counters: object
    linear_float64_le: bytes
    display_rgb8: bytes
    is_benchmark_valid: bool = field(default=False, init=False)


@dataclass(slots=True)
class _Lease:
    scene: PbrSceneAdapter | None = None
    blocks: list = field(default_factory=list)
    definitions: object = None
    expected: object = None


@dataclass(frozen=True, slots=True, eq=False)
class PbrSerialWorkloadAdapter:
    source: PbrSource
    workload: object
    run_id: UUID
    pixels_per_task: int = 4
    on_trace: object = field(default=None, repr=False)
    job: JobDefinition = field(init=False)
    _executed: list = field(default_factory=list, init=False, repr=False)
    resources_closed: bool = field(default=False, init=False)

    def __post_init__(self):
        validate_workload(self.workload)
        if type(self.source) is not PbrSource or self.source.raw_sha256 != self.workload.scene_sha256:
            raise InvalidTransport('Adapter source does not bind raw workload identity.')
        if type(self.pixels_per_task) is not int or not 1 <= self.pixels_per_task <= self.workload.pixel_count:
            raise InvalidTransport('Invalid row-major task partition size.')
        description = canonical_data(dict(schema=1, workload_digest=self.workload.workload_digest,
            workload=json.loads(self.workload.canonical_bytes()), pixels_per_task=self.pixels_per_task))
        object.__setattr__(self, 'job', JobDefinition(self.run_id, self.workload.workload_kind, '1',
            self.source.identity(self.workload.scene_index), description,
            self.workload.pixel_count*self.workload.target_spp))

    @property
    def executed_counters(self):
        return add_counters(self._executed)

    def _job(self, job):
        if job != self.job:
            raise ResultRejectedError('PBR adapter does not own this job.')

    def _lease(self, context):
        self._job(context.job)
        if not isinstance(context.resources, _Lease) or context.resources.scene is None:
            raise InvalidTransport('PBR resources are not prepared.')
        context.resources.scene.prepared.require_open()
        return context.resources

    def prepare(self, context):
        self._job(context.job)
        context.cancellation.checkpoint()
        lease = _Lease()
        context.resources = lease
        lease.scene = PbrSceneAdapter.prepare(self.source, self.workload, checkpoint=context.cancellation.checkpoint)
        if lease.scene.source.identity(self.workload.scene_index) != self.job.resource_identity:
            raise ResultRejectedError('Prepared resource identity differs from the job.')
        lease.definitions = iter(self.enumerate_tasks(self.job))
        lease.expected = next(lease.definitions, None)

    def sample_tasks(self):
        return iter_sample_tasks(self.workload, self.pixels_per_task)

    def enumerate_tasks(self, job):
        self._job(job)
        for ordinal, spec in enumerate(self.sample_tasks()):
            yield TaskDefinition(job, ordinal, spec.pixel_count*spec.sample_count, spec.canonical_bytes())

    def task_spec(self, task):
        self._job(task.job)
        try:
            data = json.loads(task.payload)
            if (set(data) != {'schema_version', 'workload_digest', 'pixel_start', 'pixel_end', 'sample_start', 'sample_end'}
                    or type(data['schema_version']) is not int or data['schema_version'] != 1
                    or data['workload_digest'] != self.workload.workload_digest):
                raise ResultRejectedError('Invalid PBR task schema/workload binding.')
            spec = SampleTaskSpec(self.workload, data['pixel_start'], data['pixel_end'], data['sample_start'], data['sample_end'])
            if spec.canonical_bytes() != task.payload or task.expected_units != spec.pixel_count*spec.sample_count:
                raise ResultRejectedError('Task canonical payload or expected pixel-sample units differ.')
            planned = next((candidate for candidate in self.enumerate_tasks(self.job) if candidate.ordinal == task.ordinal), None)
            if task != planned:
                raise ResultRejectedError('Task is outside the immutable planned partition.')
            return spec
        except (KeyError, TypeError, ValueError) as error:
            raise ResultRejectedError('Invalid PBR task payload.') from error

    def execute(self, task, context):
        spec = self.task_spec(task)  # validate before numerical work
        lease = self._lease(context)
        meter = TraceEffort(self.on_trace)
        try:
            return execute_block(lease.scene, spec, effort=meter, checkpoint=context.cancellation.checkpoint)
        finally:
            self._executed.append(meter.snapshot())

    def validate_and_accept(self, task, result, context):
        """Called inside TaskAcceptance.accept; no independent logical ledger."""
        lease = self._lease(context)
        spec = self.task_spec(task)
        payload = result.payload
        if task != lease.expected or type(payload) is not PbrRadianceBlockResultV1:
            raise ResultRejectedError('Result differs from the expected Serial task/type.')
        payload.validate_for(spec)
        c, units = payload.counters, task.expected_units
        if (c is None or c.primary_rays != units or c.path_count != units or c.secondary_rays > units
                or c.shadow_rays > units or c.surface_interactions > 2*units):
            raise ResultRejectedError('Counters are inconsistent with the depth-one point-light workload.')
        # All fallible validation and iterator advance precede the commit write.
        following = next(lease.definitions, None)
        lease.blocks.append(payload)
        lease.expected = following

    def finalize(self, context):
        lease = self._lease(context)
        if lease.expected is not None:
            raise ResultRejectedError('Missing planned Serial contributions.')
        reduction = reduce_authorized_blocks(self.workload, lease.blocks, require_complete=True)
        counters = add_counters(block.counters for block in lease.blocks)
        return PbrSerialFrame(reduction, counters, packed_linear(reduction.mean_rgb), rgb8(reduction.mean_rgb))

    def release(self, context):
        lease = context.resources
        if not isinstance(lease, _Lease):
            return
        try:
            try:
                if lease.definitions is not None and hasattr(lease.definitions, 'close'):
                    lease.definitions.close()
            finally:
                if lease.scene is not None:
                    lease.scene.close()
        finally:
            lease.blocks.clear()
            context.resources = None
            object.__setattr__(self, 'resources_closed', lease.scene is None or lease.scene.prepared._closed)
