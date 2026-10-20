"""Workload-side Serial specialization; all generic scheduling/acceptance is inherited."""
from dataclasses import dataclass
import json
from drt_scheduler import JobDefinition, ResultRejectedError, canonical_data
from .task_adapter import PbrSerialWorkloadAdapter, _Lease
from .scene_adapter import PbrSource
from .multibounce_scene import MultiScene
from .multibounce_policy import validate_workload
from .multibounce_serial import execute_block
from .integrator import TraceEffort
from .contracts import PbrRadianceBlockResultV1
from .transport_errors import InvalidTransport


@dataclass(frozen=True,slots=True,eq=False)
class MultiSerialAdapter(PbrSerialWorkloadAdapter):
    def __post_init__(self):
        validate_workload(self.workload)
        if type(self.source) is not PbrSource or self.source.raw_sha256!=self.workload.scene_sha256:
            raise InvalidTransport('Source does not bind multi-bounce workload.')
        if type(self.pixels_per_task) is not int or not 1<=self.pixels_per_task<=self.workload.pixel_count:
            raise InvalidTransport('Invalid row-major partition.')
        description=canonical_data(dict(schema=1,workload_digest=self.workload.workload_digest,
            workload=json.loads(self.workload.canonical_bytes()),pixels_per_task=self.pixels_per_task))
        object.__setattr__(self,'job',JobDefinition(self.run_id,self.workload.workload_kind,'1',
            self.source.identity(self.workload.scene_index),description,self.workload.pixel_count*self.workload.target_spp))

    def prepare(self,context):
        self._job(context.job);context.cancellation.checkpoint()
        lease=_Lease();context.resources=lease
        lease.scene=MultiScene.prepare(self.source,self.workload,checkpoint=context.cancellation.checkpoint)
        if lease.scene.source.identity(self.workload.scene_index)!=self.job.resource_identity:
            raise ResultRejectedError('Prepared resource identity differs.')
        lease.definitions=iter(self.enumerate_tasks(self.job));lease.expected=next(lease.definitions,None)

    def execute(self,task,context):
        spec=self.task_spec(task);lease=self._lease(context);meter=TraceEffort(self.on_trace)
        try: return execute_block(lease.scene,spec,effort=meter,checkpoint=context.cancellation.checkpoint)
        finally: self._executed.append(meter.snapshot())

    def validate_and_accept(self,task,result,context):
        lease=self._lease(context);spec=self.task_spec(task);payload=result.payload
        if task!=lease.expected or type(payload) is not PbrRadianceBlockResultV1:
            raise ResultRejectedError('Unexpected Serial multi-bounce task/result.')
        payload.validate_for(spec)
        c,units,depth=payload.counters,task.expected_units,self.workload.max_path_depth
        if (c is None or c.primary_rays!=units or c.path_count!=units or c.secondary_rays>depth*units
                or c.shadow_rays>2*depth*units or c.surface_interactions>(depth+1)*units):
            raise ResultRejectedError('Counters exceed the bounded multi-bounce transport contract.')
        following=next(lease.definitions,None)
        lease.blocks.append(payload);lease.expected=following
