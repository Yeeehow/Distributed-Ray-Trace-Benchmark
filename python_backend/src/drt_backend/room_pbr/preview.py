"""Bounded incremental accepted-sample presentation, maximum twenty previews."""
from time import perf_counter
import numpy as np
from drt_scheduler import ProgressObservation
from .display import room_rgb8
from drt_backend.raytracing.pathtracing.contracts import RadianceCountersV1
from drt_backend.raytracing.pathtracing.integrator import add_counters


class AcceptedRoomPreview:
    def __init__(self,adapter,publish=None):
        self.adapter=adapter;self.publish=publish;self.pending=None
        w=adapter.workload
        self.sums=np.zeros((w.pixel_count,3),dtype=np.float64)
        self.counts=np.zeros(w.pixel_count,dtype=np.uint8)
        self.coverage=0;self.last_bucket=0;self.last_time=0
        self.accepted_counters=RadianceCountersV1(0,0,0,0,0,1)
        self.diagnostics=dict(schema='drt.room_accepted_preview.v1',updates=0,published=[],
            bounded_state_bytes=self.sums.nbytes+self.counts.nbytes,maximum_frames=20)

    def __getattr__(self,key):return getattr(self.adapter,key)

    def validate_and_accept(self,task,result,context):
        self.adapter.validate_and_accept(task,result,context)
        self.pending=result.payload

    def committed(self,item):
        if not isinstance(item,ProgressObservation) or self.pending is None:return
        b,self.pending=self.pending,None
        start,end=b.pixel_start,b.pixel_end
        if np.any(self.counts[start:end]):raise ValueError('Duplicate committed preview pixels.')
        self.sums[start:end]=b.radiance_sums;self.counts[start:end]=b.sample_count
        self.coverage+=b.accepted_pixel_samples;self.diagnostics['updates']+=1
        self.accepted_counters=add_counters((self.accepted_counters,b.counters))
        if self.coverage!=item.completed_units:raise ValueError('Preview/central commitment diverged.')
        w=self.workload;bucket=self.coverage*20//(w.pixel_count*w.target_spp);now=perf_counter()
        if self.publish and bucket>self.last_bucket and (now-self.last_time>=.2 or bucket==20):
            means=self.sums/np.maximum(self.counts,1)[:,None]
            raw=bytearray(room_rgb8(means,w.color_output_policy))
            for p in np.flatnonzero(self.counts==0):raw[int(p)*3:int(p)*3+3]=bytes((32,32,32))
            self.publish(w.width,w.height,self.coverage,bytes(raw))
            self.last_bucket=bucket;self.last_time=now;self.diagnostics['published'].append(self.coverage)


def attach(submission,observer,publish):
    preview=AcceptedRoomPreview(submission.adapter,publish);submission.adapter=preview
    def observed(item):
        if observer:observer(item)
        preview.committed(item)
    return submission,observed,preview
