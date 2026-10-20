"""Canonical M6.0 Float64 sum blocks for the separately versioned path loop."""
import math
from .contracts import SampleTaskSpec, PbrRadianceBlockResultV1
from .integrator import TraceEffort
from .multibounce_integrator import sample_path
from .errors import WorkloadMismatch
from .transport_errors import InvalidTransport


def execute_block(scene,task,*,effort=None,checkpoint=lambda:None):
    if type(task) is not SampleTaskSpec or task.workload!=scene.workload:
        raise WorkloadMismatch('Task does not bind to the multi-bounce prepared workload.')
    scene.prepared.require_open()
    meter=TraceEffort() if effort is None else effort
    if any(meter.values.values()): raise InvalidTransport('Block needs a fresh effort meter.')
    sums=[]
    for p in range(task.pixel_start,task.pixel_end):
        checkpoint()
        samples=[sample_path(scene,p,s,meter,checkpoint=checkpoint).radiance for s in range(task.sample_start,task.sample_end)]
        try: sums.append(tuple(math.fsum(v[c] for v in samples) for c in range(3)))
        except (OverflowError,ValueError) as error: raise InvalidTransport('Float64 block sum overflow.') from error
    return PbrRadianceBlockResultV1.for_task(task,tuple(sums),counters=meter.snapshot())
