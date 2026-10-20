"""Versioned presentation copy of centrally committed Float64 sample blocks."""
from time import perf_counter_ns
from drt_scheduler import ProgressObservation
from drt_backend.raytracing.pathtracing.accumulation import reduce_authorized_blocks
from drt_backend.raytracing.pathtracing.color_pipeline import rgb8


class AcceptedSamplePreview:
    schema = 'drt.pbr_accepted_preview.v1'

    def __init__(self, adapter, publish=None):
        self.adapter, self.publish = adapter, publish
        self.blocks, self.pending = [], None
        self.last = 0
        self.diagnostics = dict(schema=self.schema, snapshots=[], accepted_updates=0)

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def validate_and_accept(self, task, result, context):
        self.adapter.validate_and_accept(task, result, context)
        self.pending = result.payload

    def snapshot(self):
        r = reduce_authorized_blocks(self.workload, self.blocks, require_complete=False)
        image = b''.join(bytes((32, 32, 32)) if c == 0 else rgb8((v,))
                         for c, v in zip(r.sample_counts, r.mean_rgb))
        return r, image

    def committed(self, item):
        if not isinstance(item, ProgressObservation) or self.pending is None:
            return
        block, self.pending = self.pending, None
        self.blocks.append(block)
        r, image = self.snapshot()
        if r.accepted_pixel_samples != item.completed_units:
            raise ValueError('Preview differs from centrally committed sample count.')
        self.diagnostics['accepted_updates'] += 1
        now = perf_counter_ns()
        if self.publish and (now-self.last >= 200_000_000 or r.complete):
            self.last = now
            self.publish(self.workload.width, self.workload.height, item.completed_units, image)
            self.diagnostics['snapshots'].append(dict(accepted_samples=item.completed_units,
                sample_counts=list(r.sample_counts), complete=r.complete, monotonic_ns=now))


def attach(submission, observer, publish):
    preview = AcceptedSamplePreview(submission.adapter, publish)
    submission.adapter = preview
    def observed(item):
        if observer:
            observer(item)
        preview.committed(item)
    return submission, observed, preview
