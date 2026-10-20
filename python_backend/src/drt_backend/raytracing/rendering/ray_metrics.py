"""Per-job logical execution counters, independent of accepted pixel units."""
from dataclasses import dataclass
from uuid import UUID

from drt_backend.raytracing.common.errors import RenderValidationError

MAX_INT64 = (1 << 63) - 1


@dataclass(slots=True)
class RayCounters:
    run_id: UUID
    primary_ray_count: int = 0
    shadow_ray_count: int = 0

    def primary(self):
        if self.total_ray_count >= MAX_INT64:
            raise RenderValidationError('Logical ray count exceeds int64.')
        self.primary_ray_count += 1

    def shadow(self):
        if self.total_ray_count >= MAX_INT64:
            raise RenderValidationError('Logical ray count exceeds int64.')
        self.shadow_ray_count += 1

    @property
    def total_ray_count(self):
        return self.primary_ray_count + self.shadow_ray_count

    def completed_metrics(self, render_nanoseconds):
        if any(type(value) is not int or value < 0 for value in (self.primary_ray_count,self.shadow_ray_count)) or self.total_ray_count>MAX_INT64:
            raise RenderValidationError('Completed logical ray counts must be nonnegative int64 values.')
        if type(render_nanoseconds) is not int or not 0 < render_nanoseconds <= MAX_INT64:
            raise RenderValidationError('A positive int64 monotonic render duration is required.')
        score = self.total_ray_count * 1_000_000_000 // render_nanoseconds
        if score > MAX_INT64:
            raise RenderValidationError('Measured throughput exceeds int64.')
        return dict(primaryRayCount=self.primary_ray_count, shadowRayCount=self.shadow_ray_count,
                    totalRayCount=self.total_ray_count, renderNanoseconds=render_nanoseconds,
                    raysPerSecond=score)
