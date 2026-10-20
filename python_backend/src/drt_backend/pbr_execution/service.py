"""Experimental tiny CPU PBR strategies using the existing scheduler owners."""
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from drt_scheduler import SerialScheduler, CancellationToken
from drt_scheduler.attempts import AttemptPolicy
from drt_backend.application.parallel_service import ParallelSubmission
from drt_backend.local_parallel.dispatcher import ParallelOptions
from drt_backend.raytracing.pathtracing.multibounce_adapter import MultiSerialAdapter
from drt_backend.raytracing.pathtracing.multibounce_scene import load_source
from . import codec
from .worker import worker_main
from .preview import attach


@dataclass(frozen=True)
class PbrExecutionResult:
    outcome: object
    diagnostics: dict


def provisioned(scene, root):
    scene, root = Path(scene), Path(root)
    codec.require(scene.is_absolute() and root.is_absolute() and scene.is_file()
                  and scene.resolve().is_relative_to(root.resolve()) and scene.suffix == '.glb'
                  and scene.stat().st_size <= 2_097_152, 'Unsupported/unprovisioned PBR resource.')
    for p in (scene, *scene.parents):
        if p == root.parent:
            break
        codec.require(not p.is_symlink() and not p.is_junction(), 'PBR resource reparse paths are forbidden.')
    return scene.resolve()


def create_submission(scene, workload, *, run_id=None, pixels_per_task=4, workers=2, resource_root):
    before = perf_counter()
    scene = provisioned(scene, resource_root)
    codec.capability(workload)
    source = load_source(scene, workload)
    adapter = MultiSerialAdapter(source, workload, run_id or uuid4(), pixels_per_task)
    codec.workload_from_job(adapter.job)
    from .admission import admit
    admit(workers)
    return ParallelSubmission(scene, source.raw_sha256, adapter,
        ParallelOptions(workers=workers, capacity_limit=2, startup_seconds=15, task_seconds=15,
                        shutdown_seconds=3, run_seconds=45), perf_counter()-before, codec)


def execute(scene, workload, mode, *, resource_root, run_id=None, cancellation=None, observer=None,
            preview_observer=None, contribution_observer=None, node_observer=None, agents=None,
            pixels_per_task=4, workers=2):
    token = cancellation or CancellationToken()
    submission = create_submission(scene, workload, run_id=run_id, pixels_per_task=pixels_per_task,
                                   workers=workers, resource_root=resource_root)
    if mode == 'distributedAllCore':
        codec.require(agents is not None and 1 <= len(agents['agents']) <= 2
                      and 1 <= agents['global_workers'] <= 2, 'PBR needs one or two authenticated provisioned Agents.')
        from .profile import Profile
        from drt_backend.lan.dynamic_coordinator import execute_dynamic
        profile = Profile(submission)
        measured = execute_dynamic(scene, workload, config=agents, run_id=submission.definition.run_id,
            cancellation=token, observer=observer, preview_observer=preview_observer,
            contribution_observer=contribution_observer, node_observer=node_observer,
            _policy=AttemptPolicy(max_active=agents['global_workers'], max_per_node=1,
                max_pending=4, max_attempts=1, max_total_retries=0, lease_seconds=15, run_seconds=45),
            _profile=profile)
        return PbrExecutionResult(measured.outcome, measured.diagnostics)
    submission, observed, preview = attach(submission, observer, preview_observer)
    if mode == 'localAllCore':
        measured = submission.run(cancellation=token, observer=observed,
            contribution_observer=contribution_observer, _worker_target=worker_main)
        return PbrExecutionResult(measured.outcome, measured.diagnostics | dict(schema_version=2,
            kind='drt.pbr_local.v1', is_benchmark_valid=False, preview=preview.diagnostics))
    codec.require(mode == 'cpuSerialPreview', 'Unsupported PBR mode; no fallback.')
    outcome = SerialScheduler().run(submission.definition, submission.adapter, cancellation=token, observer=observed)
    c = submission.adapter.executed_counters
    return PbrExecutionResult(outcome, dict(kind='drt.pbr_serial.v1', physical_rays=codec.ray_counts(
        c.primary_rays, c.shadow_rays, c.secondary_rays), preview=preview.diagnostics,
        resources_closed=submission.adapter.resources_closed, is_benchmark_valid=False))
