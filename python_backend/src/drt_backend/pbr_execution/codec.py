"""Strict bounded PBR local codec using shared job/task/byte validation."""
import base64
from dataclasses import asdict, fields
from hashlib import sha256
import json
from pathlib import Path
import struct

from drt_scheduler import TaskResult, ExecutionStatus
from drt_backend.local_parallel import codec as shared
from drt_backend.raytracing.pathtracing.contracts import (WorkloadSpec, PolicyDescriptor,
    PbrRadianceBlockResultV1, RadianceCountersV1)
from drt_backend.raytracing.pathtracing.multibounce_policy import validate_workload, MIS_POLICY
from drt_backend.raytracing.pathtracing.multibounce_adapter import MultiSerialAdapter
from drt_backend.raytracing.pathtracing.multibounce_scene import load_source

VERSION = 'drt.local_pbr_codec.v1'
MAX_INIT_BYTES, MAX_TASK_BYTES, MAX_RESULT_BYTES = 32768, 4096, 16384
require, integer = shared.require, shared.integer
LocalProtocolError, WorkerFailure = shared.LocalProtocolError, shared.WorkerFailure
encode, job_data, read_job, task_data = shared.encode, shared.job_data, shared.read_job, shared.task_data
failure_data, read_failure = shared.failure_data, shared.read_failure
HEADER_KEYS = shared.HEADER_KEYS | {'workload_digest', 'capability_digest'}
RESULT_KEYS = HEADER_KEYS | {'task', 'status', 'candidate_units', 'radiance', 'rays', 'started_ns', 'ended_ns', 'error'}


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys), 'Invalid PBR field set.')
    return value


def decode(raw, maximum=MAX_RESULT_BYTES):
    # Reuse duplicate-key/ASCII/NaN/size checks, with a distinct discriminator.
    require(type(raw) is bytes and 0 < len(raw) <= maximum, 'Invalid PBR frame size.')
    from drt_backend.lan.protocol import strict_json
    value = strict_json(raw)
    require(type(value) is dict and value.get('version') == VERSION, 'Unsupported PBR local codec.')
    return value


def read_workload(value):
    exact(value, (f.name for f in fields(WorkloadSpec)))
    data = dict(value)
    for name in ('termination_policy', 'material_model', 'bsdf', 'integrator', 'alpha_policy',
                 'lighting_policy', 'rendering_policy', 'color_output_policy'):
        p = exact(data[name], ('identity', 'parameters'))
        require(type(p['parameters']) is dict, 'Invalid policy parameters.')
        def scalar(v):
            if type(v) is dict:
                exact(v, ('float64',))
                require(type(v['float64']) is str, 'Invalid Float64 tag.')
                f = float.fromhex(v['float64'])
                require(f.hex() == v['float64'], 'Noncanonical Float64 policy.')
                return f
            return v
        data[name] = PolicyDescriptor(p['identity'], tuple((k, scalar(v)) for k, v in sorted(p['parameters'].items())))
    w = WorkloadSpec(**data)
    validate_workload(w)
    require(json.loads(w.canonical_bytes()) == value, 'Noncanonical numerical workload.')
    return w


def workload_from_job(job):
    from drt_backend.lan.protocol import strict_json
    d = exact(strict_json(job.description), ('schema', 'workload_digest', 'workload', 'pixels_per_task'))
    require(type(d['schema']) is int and d['schema'] == 1 and job.workload_id == 'drt.pbr_pathtracer.v1'
            and job.workload_version == '1', 'Legacy/PBR job crossover.')
    w = read_workload(d['workload'])
    integer(d['pixels_per_task'], 1, min(32, w.pixel_count))
    require(d['workload_digest'] == w.workload_digest and job.expected_units == w.pixel_count*w.target_spp,
            'Job numerical digest/units differ.')
    return w, d['pixels_per_task']


def capability(w):
    validate_workload(w)
    return dict(schema='drt.pbr_capability.v1', workload=json.loads(w.canonical_bytes()),
        mis=MIS_POLICY.canonical_data(), radiance='drt.pbr_radiance_block.v1', counters='RadianceCountersV1',
        precision='float64', endian='little', channels=3, sample_block_size=8,
        dimensions=[[4, 3], [7, 5]], max_spp=8, max_depth=3, max_workers=2,
        max_task_pixels=32, alpha='OPAQUE', material_subset='untextured_mr_specular_no_normal_map',
        resource='provisioned_synthetic_glb_raw_sha256_ecs',
        max_init_bytes=MAX_INIT_BYTES, max_task_bytes=MAX_TASK_BYTES, max_result_bytes=MAX_RESULT_BYTES,
        timing_policy='drt.pbr_tiny_cpu_deadlines.v1')


def match_capability(value, w):
    # Complete typed canonical equality, including all policy parameters and limits.
    require(encode(value, MAX_INIT_BYTES) == encode(capability(w), MAX_INIT_BYTES), 'Unsupported PBR capability/configuration.')


def capability_digest(w):
    return sha256(encode(capability(w), MAX_INIT_BYTES)).hexdigest()


def initialization(scene, file_sha256, job):
    w, _ = workload_from_job(job)
    require(Path(scene).is_absolute() and len(str(scene)) <= 4096, 'Absolute provisioned PBR resource required.')
    require(file_sha256 == w.scene_sha256, 'Wrong raw PBR source digest.')
    return encode(dict(version=VERSION, kind='initialize', scene=str(scene), file_sha256=file_sha256,
                       job=job_data(job), capability=capability(w)), MAX_INIT_BYTES)


def read_initialization(raw):
    d = exact(decode(raw, MAX_INIT_BYTES), ('version', 'kind', 'scene', 'file_sha256', 'job', 'capability'))
    require(d['kind'] == 'initialize' and type(d['scene']) is str and Path(d['scene']).is_absolute()
            and len(d['scene']) <= 4096, 'Invalid provisioned PBR resource.')
    job = read_job(d['job'])
    w, _ = workload_from_job(job)
    match_capability(d['capability'], w)
    require(d['file_sha256'] == w.scene_sha256, 'PBR resource digest differs.')
    return d, job


def describe(scene, job):
    w, size = workload_from_job(job)
    source = load_source(Path(scene), w)
    adapter = MultiSerialAdapter(source, w, job.run_id, size)
    require(adapter.job == job, 'Raw/ECS/camera/configuration identity differs.')
    return adapter


def task_packet(task):
    return encode(dict(version=VERSION, kind='task', task=task_data(task)), MAX_TASK_BYTES)


def read_task(message, job):
    exact(message, ('version', 'kind', 'task'))
    require(message['version'] == VERSION and message['kind'] == 'task', 'Legacy/PBR task crossover.')
    task = shared.read_task(dict(version=1, kind='task', task=message['task']), job)
    # The unchanged adapter independently checks canonical intervals/ordinal/units.
    w, size = workload_from_job(job)
    d = json.loads(task.payload)
    from drt_backend.raytracing.pathtracing.contracts import SampleTaskSpec, iter_sample_tasks
    exact(d, ('schema_version', 'workload_digest', 'pixel_start', 'pixel_end', 'sample_start', 'sample_end'))
    spec = SampleTaskSpec(w, *(d[k] for k in ('pixel_start', 'pixel_end', 'sample_start', 'sample_end')))
    require(spec.canonical_bytes() == task.payload and spec.pixel_count*spec.sample_count == task.expected_units
            and task.ordinal < (w.pixel_count+size-1)//size
            and tuple(iter_sample_tasks(w, size))[task.ordinal] == spec, 'Noncanonical PBR task.')
    return task


def header(kind, job, worker_id, pid):
    w, _ = workload_from_job(job)
    return shared.header(kind, job, worker_id, pid) | dict(version=VERSION, workload_digest=w.workload_digest,
                                                        capability_digest=capability_digest(w))


def check_header(message, job, worker_id, pid):
    require(all(message.get(k) == v and type(message.get(k)) is type(v)
                for k, v in header(message.get('kind'), job, worker_id, pid).items()),
            'PBR Worker/job/resource/capability identity differs.')


def ray_counts(primary, shadow, secondary=0):
    for v in (primary, secondary, shadow, primary+secondary+shadow):
        integer(v)
    return dict(primary=primary, secondary=secondary, shadow=shadow, total=primary+secondary+shadow)


def validate_rays(value):
    exact(value, ('primary', 'secondary', 'shadow', 'total'))
    require(value == ray_counts(value['primary'], value['shadow'], value['secondary'])
            and type(value['total']) is int, 'Invalid PBR ray counters.')
    return value


def pack_block(block):
    raw = b''.join(struct.pack('<3d', *rgb) for rgb in block.radiance_sums)
    d = asdict(block)
    d['radiance_sums'] = base64.b64encode(raw).decode('ascii')
    return dict(schema='drt.pbr_radiance_block.v1', precision='float64', endian='little', channels=3, block=d)


def unpack_block(value, spec, *, maximum_shadow_lights=2):
    require(type(maximum_shadow_lights) is int and maximum_shadow_lights in (2,5)
            and (maximum_shadow_lights==2 or spec.workload.integrator.identity=='drt.integrator.room_multibounce_nee_mis_rr.v1'),
            'Unsupported shadow-light counter policy.')
    exact(value, ('schema', 'precision', 'endian', 'channels', 'block'))
    require((value['schema'], value['precision'], value['endian'], value['channels']) ==
            ('drt.pbr_radiance_block.v1', 'float64', 'little', 3) and type(value['channels']) is int, 'Invalid radiance encoding.')
    d = dict(exact(value['block'], (f.name for f in fields(PbrRadianceBlockResultV1))))
    text = d['radiance_sums']; n = spec.pixel_count*24
    require(type(text) is str and len(text) == 4*((n+2)//3), 'Invalid Float64 encoded length.')
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise LocalProtocolError('Invalid Float64 base64.') from e
    require(len(raw) == n and base64.b64encode(raw).decode('ascii') == text, 'Noncanonical Float64 buffer.')
    d['radiance_sums'] = tuple(struct.iter_unpack('<3d', raw))
    exact(d['counters'], (f.name for f in fields(RadianceCountersV1)))
    d['counters'] = RadianceCountersV1(**d['counters'])
    block = PbrRadianceBlockResultV1(**d)
    block.validate_for(spec)
    c, u, depth = block.counters, spec.pixel_count*spec.sample_count, spec.workload.max_path_depth
    require(c.primary_rays == u and c.path_count == u and c.secondary_rays <= u*depth
            and c.shadow_rays <= u*depth*maximum_shadow_lights and c.surface_interactions <= u*(depth+1), 'Invalid bounded PBR effort.')
    return block


def result_packet(job, worker_id, pid, task, status, counters, started, ended, payload=None, error=None):
    return encode(header('result', job, worker_id, pid) | dict(task=task_data(task), status=status,
        candidate_units=task.expected_units if status == 'completed' else 0,
        radiance=None if payload is None else pack_block(payload),
        rays=ray_counts(counters.primary_rays, counters.shadow_rays, counters.secondary_rays),
        started_ns=started, ended_ns=ended, error=None if error is None else failure_data(error)))


def translate_result(message, adapter, task, context, worker_id, pid):
    exact(message, RESULT_KEYS)
    check_header(message, task.job, worker_id, pid)
    require(read_task(dict(version=VERSION, kind='task', task=message['task']), task.job) == task,
            'Foreign PBR result task.')
    integer(message['started_ns'], 1); integer(message['ended_ns'], message['started_ns'])
    rays = validate_rays(message['rays'])
    integer(message['candidate_units'])
    status = ExecutionStatus(message['status'])
    elapsed = (message['ended_ns']-message['started_ns'])/1e9
    if status is not ExecutionStatus.COMPLETED:
        require(message['radiance'] is None and message['candidate_units'] == 0, 'Failed PBR task carries radiance.')
        error = read_failure(message['error'])
        if status is ExecutionStatus.CANCELLED:
            require(message['error']['code'] == 'CancellationRequested', 'Cancellation checkpoint not confirmed.')
        return TaskResult.for_task(task, status, executor_id=worker_id, elapsed_seconds=elapsed, failure=error), rays
    block = unpack_block(message['radiance'], adapter.task_spec(task))
    c = block.counters
    require(message['candidate_units'] == task.expected_units and message['error'] is None
            and rays == ray_counts(c.primary_rays, c.shadow_rays, c.secondary_rays), 'PBR result counters differ.')
    return TaskResult.for_task(task, status, executor_id=worker_id, elapsed_seconds=elapsed, payload=block), rays
