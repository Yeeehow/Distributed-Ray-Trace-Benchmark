"""Original-room adapter uses the existing generic Serial acceptance authority."""
from dataclasses import dataclass
import json
import math
from drt_scheduler import JobDefinition,canonical_data
from drt_backend.raytracing.pathtracing.multibounce_adapter import MultiSerialAdapter
from drt_backend.raytracing.pathtracing.task_adapter import _Lease,PbrSerialFrame
from drt_backend.raytracing.pathtracing.accumulation import RadianceReduction
from drt_backend.raytracing.pathtracing.contracts import SampleTaskSpec,PbrRadianceBlockResultV1
from drt_backend.raytracing.pathtracing.integrator import add_counters
from drt_backend.raytracing.pathtracing.color_pipeline import packed_linear,rgb8
from .policy import validate
from .scene import RoomSource,RoomScene
from .display import room_rgb8


@dataclass(frozen=True,slots=True,eq=False)
class RoomAdapter(MultiSerialAdapter):
    def __post_init__(self):
        validate(self.workload)
        if type(self.source) is not RoomSource or self.source.raw_sha256!=self.workload.scene_sha256:
            raise ValueError('Original-room source does not bind workload.')
        if type(self.pixels_per_task) is not int or not 1<=self.pixels_per_task<=32:
            raise ValueError('Room task must contain 1..32 pixels.')
        description=canonical_data(dict(schema=1,workload_digest=self.workload.workload_digest,
            workload=json.loads(self.workload.canonical_bytes()),pixels_per_task=self.pixels_per_task))
        object.__setattr__(self,'job',JobDefinition(self.run_id,self.workload.workload_kind,'1',
            self.source.identity(0),description,self.workload.pixel_count*self.workload.target_spp))

    def prepare(self,context):
        self._job(context.job);context.cancellation.checkpoint();lease=_Lease();context.resources=lease
        lease.scene=RoomScene.prepare(self.source,self.workload,checkpoint=context.cancellation.checkpoint)
        lease.definitions=iter(self.enumerate_tasks(self.job));lease.expected=next(lease.definitions,None)

    def task_spec(self,task):
        self._job(task.job);w=self.workload;start=task.ordinal*self.pixels_per_task
        if not 0<=start<w.pixel_count:raise ValueError('Unplanned room task ordinal.')
        spec=SampleTaskSpec(w,start,min(start+self.pixels_per_task,w.pixel_count),0,w.target_spp)
        if task.payload!=spec.canonical_bytes() or task.expected_units!=spec.pixel_count*spec.sample_count:
            raise ValueError('Room task canonical identity differs.')
        return spec

    def validate_and_accept(self,task,result,context):
        lease=self._lease(context);spec=self.task_spec(task);payload=result.payload
        if task!=lease.expected or type(payload) is not PbrRadianceBlockResultV1:raise ValueError('Unexpected room task/result.')
        payload.validate_for(spec);c=payload.counters;u=task.expected_units;d=self.workload.max_path_depth
        if (c is None or c.primary_rays!=u or c.path_count!=u or c.secondary_rays>d*u
                or c.shadow_rays>5*d*u or c.surface_interactions>(d+1)*u):
            raise ValueError('Original-room ray counters exceed physical bounds.')
        following=next(lease.definitions,None);lease.blocks.append(payload);lease.expected=following

    def finalize(self,context):
        lease=self._lease(context);w=self.workload
        if lease.expected is not None:raise ValueError('Incomplete original-room tasks.')
        # SPP<=8: exactly one canonical block per pixel; one bounded linear pass.
        sums=[None]*w.pixel_count
        for b in lease.blocks:
            b.validate_for(SampleTaskSpec(w,b.pixel_start,b.pixel_end,b.sample_start,b.sample_end))
            for offset,value in enumerate(b.radiance_sums):
                p=b.pixel_start+offset
                if sums[p] is not None:raise ValueError('Duplicate accepted room block.')
                sums[p]=tuple(math.fsum((v,))/w.target_spp for v in value)
        if any(v is None for v in sums):raise ValueError('Missing accepted room pixels.')
        reduction=RadianceReduction(w.workload_digest,tuple(sums),(w.target_spp,)*w.pixel_count,
                                    w.pixel_count*w.target_spp,True)
        return PbrSerialFrame(reduction,add_counters(b.counters for b in lease.blocks),
                              packed_linear(reduction.mean_rgb),room_rgb8(reduction.mean_rgb,w.color_output_policy))
