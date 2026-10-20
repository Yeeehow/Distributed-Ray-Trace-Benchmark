"""Stateless synthetic reduction of ALREADY centrally authorized contributions.

This routine cannot authenticate/commit attempts, retry, award contributions,
or replace TaskAcceptance. Coverage checks validate the numerical input set.
Future integration must supply only centrally committed logical task results.
"""
from dataclasses import dataclass
import math

from .contracts import WorkloadSpec, SampleTaskSpec, PbrRadianceBlockResultV1
from .errors import (InvalidWorkloadConfiguration, InvalidRadiancePayload,
                     WorkloadMismatch, CoverageError, AccumulationOverflow)


@dataclass(frozen=True, slots=True)
class RadianceReduction:
    """Row-major linear mean; missing preview pixels are None, never black."""
    workload_digest: str
    mean_rgb: tuple[tuple[float, float, float] | None, ...]
    sample_counts: tuple[int, ...]
    accepted_pixel_samples: int
    complete: bool


def reduce_authorized_blocks(workload: WorkloadSpec, results, *, require_complete: bool) -> RadianceReduction:
    """Validate set coverage, sort blocks by sample_start, fsum then divide.

    Equal canonical per-pixel block sums give equal output bits regardless of
    arrival order or pixel partition. Within-block per-sample reduction must also
    have a fixed order in a future integrator. This scalar reference retains all
    inputs; it is not a bounded production streaming/reorder implementation.
    """
    if type(workload) is not WorkloadSpec:
        raise InvalidWorkloadConfiguration('Reduction requires WorkloadSpec.')
    if type(require_complete) is not bool:
        raise InvalidRadiancePayload('require_complete must be an explicit boolean.')
    if workload.width > 7 or workload.height > 5 or workload.target_spp > 16:
        raise InvalidWorkloadConfiguration('M6.0 synthetic reduction is bounded to 7x5 and 16 SPP.')
    per_pixel = [{} for _ in range(workload.pixel_count)]
    accepted = 0
    for result in results:
        if type(result) is not PbrRadianceBlockResultV1:
            raise InvalidRadiancePayload('Reduction accepts only radiance schema 1.')
        if result.workload_digest != workload.workload_digest:
            raise WorkloadMismatch('Result belongs to another numerical workload.')
        task = SampleTaskSpec(workload, result.pixel_start, result.pixel_end,
                              result.sample_start, result.sample_end)
        result.validate_for(task)
        for offset, rgb in enumerate(result.radiance_sums):
            blocks = per_pixel[result.pixel_start + offset]
            if result.sample_start in blocks:
                raise CoverageError('Duplicate or overlapping logical pixel/sample block.')
            blocks[result.sample_start] = (result.sample_count, rgb)
        accepted += result.accepted_pixel_samples
    means, counts = [], []
    for blocks in per_pixel:
        ordered = [blocks[start] for start in sorted(blocks)]
        count = sum(samples for samples, _ in ordered)
        if require_complete and count != workload.target_spp:
            raise CoverageError('Finalization requires all canonical sample blocks for every pixel.')
        counts.append(count)
        if count == 0:
            means.append(None)
            continue
        try:
            rgb = tuple(math.fsum(rgb[channel] for _, rgb in ordered) / count for channel in range(3))
        except (OverflowError, ValueError) as error:
            raise AccumulationOverflow('Binary64 radiance sum overflow.') from error
        if not all(math.isfinite(c) for c in rgb):
            raise AccumulationOverflow('Binary64 mean radiance is not finite.')
        means.append(rgb)
    if accepted != sum(counts):
        raise InvalidRadiancePayload('Accepted sample accounting differs from per-pixel coverage.')
    return RadianceReduction(workload.workload_digest, tuple(means), tuple(counts), accepted,
                             all(count == workload.target_spp for count in counts))
