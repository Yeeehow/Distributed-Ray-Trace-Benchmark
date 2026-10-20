"""Room strategies retain the existing process owners and single acceptance."""
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from drt_scheduler import CancellationToken,SerialScheduler,ProgressObservation
from drt_backend.application.parallel_service import ParallelSubmission
from drt_backend.pbr_execution.service import PbrExecutionResult
from .policy import validate,RAW
from .scene import RoomSource
from .adapter import RoomAdapter
from .admission import RoomOptions,select
from .preview import attach
from .worker import worker_main
from . import codec


def provisioned(scene,root):
    scene,root=Path(scene),Path(root)
    codec.require(scene.is_absolute() and root.is_absolute() and scene.is_file()
        and scene.resolve().is_relative_to(root.resolve()) and scene.suffix=='.glb'
        and scene.stat().st_size<=16*1024*1024,'Unprovisioned room GLB.')
    for p in (scene,*scene.parents):
        if p==root.parent:break
        codec.require(not p.is_symlink() and not p.is_junction(),'Reparse room resource path.')
    return scene.resolve()


def create_submission(scene,w,*,resource_root,run_id=None,pixels_per_task=32,workers=None):
    before=perf_counter();validate(w);scene=provisioned(scene,resource_root)
    source=RoomSource.load(scene,w);adapter=RoomAdapter(source,w,run_id or uuid4(),pixels_per_task)
    allocation=select((w.pixel_count+pixels_per_task-1)//pixels_per_task,workers)
    return ParallelSubmission(scene,RAW,adapter,RoomOptions(workers=allocation['workers']),perf_counter()-before,codec)


def execute(scene,w,mode,*,resource_root,run_id=None,cancellation=None,observer=None,preview_observer=None,
            contribution_observer=None,node_observer=None,agents=None,workers=None,pixels_per_task=32):
    token=cancellation or CancellationToken()
    sub=create_submission(scene,w,resource_root=resource_root,run_id=run_id,
        workers=1 if mode=='cpuSerialPreview' else workers,pixels_per_task=pixels_per_task)
    if mode=='distributedAllCore':
        from .profile import execute_distributed
        return execute_distributed(sub,w,agents,token,observer,preview_observer,contribution_observer,node_observer)
    sub,observe,preview=attach(sub,observer,preview_observer)
    if mode=='localAllCore':
        from drt_backend.ipc.host import configure_source_workers
        configure_source_workers()
        measured=sub.run(cancellation=token,observer=observe,contribution_observer=contribution_observer,_worker_target=worker_main)
        return PbrExecutionResult(measured.outcome,measured.diagnostics|dict(schema_version=3,kind='drt.room_local.v1',
            is_benchmark_valid=False,preview=preview.diagnostics,resource_profile=codec.capability(w)['memory']|dict(workers=sub.options.workers),
            timing_policy='drt.room_cpu_deadlines.v1'))
    codec.require(mode=='cpuSerialPreview','Room strategy unsupported; no Direct fallback.')
    def serial_observe(item):
        observe(item)
        if isinstance(item,ProgressObservation) and item.completed_units and contribution_observer:
            import os
            c=preview.accepted_counters
            contribution_observer('serial/worker-0',os.getpid(),c.primary_rays,c.shadow_rays,c.secondary_rays,item.completed_units)
    result=SerialScheduler().run(sub.definition,sub.adapter,cancellation=token,observer=serial_observe)
    c=sub.adapter.executed_counters
    return PbrExecutionResult(result,dict(schema_version=3,kind='drt.room_serial.v1',
        physical_rays=codec.ray_counts(c.primary_rays,c.shadow_rays,c.secondary_rays),resources_closed=sub.adapter.resources_closed,
        is_benchmark_valid=False,preview=preview.diagnostics,resource_profile=codec.capability(w)['memory']|dict(workers=1),
        timing_policy='drt.room_cpu_deadlines.v1'))
