"""Canonical ascending per-pixel sample blocks; no scheduler or acceptance here."""
import math
from .contracts import SampleTaskSpec, PbrRadianceBlockResultV1
from .errors import WorkloadMismatch
from .integrator import TraceEffort, sample_radiance
from .transport_errors import InvalidTransport


def execute_block(scene, task, *, effort=None, checkpoint=lambda: None):
    if type(task) is not SampleTaskSpec or task.workload != scene.workload:
        raise WorkloadMismatch('Sample task does not bind to this prepared scene workload.')
    scene.prepared.require_open()
    meter = TraceEffort() if effort is None else effort
    if any(meter.snapshot().__getattribute__(n) for n in meter.values):
        raise InvalidTransport('Block requires a fresh executed-effort meter.')
    sums = []
    for pixel in range(task.pixel_start, task.pixel_end):
        checkpoint()
        samples = [sample_radiance(scene, pixel, sample, meter, checkpoint=checkpoint).radiance
                   for sample in range(task.sample_start, task.sample_end)]
        try:
            sums.append(tuple(math.fsum(rgb[c] for rgb in samples) for c in range(3)))
        except (OverflowError, ValueError) as error:
            raise InvalidTransport('Block sum exceeds finite Float64.') from error
    return PbrRadianceBlockResultV1.for_task(task, tuple(sums), counters=meter.snapshot())
