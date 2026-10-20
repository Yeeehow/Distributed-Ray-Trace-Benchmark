"""Explicit original-room Float64 codec; historical PBR/Direct schemas unchanged."""
from hashlib import sha256
import json
from pathlib import Path
from drt_scheduler import TaskResult,ExecutionStatus
from drt_backend.local_parallel import codec as generic
from drt_backend.pbr_execution import codec as tiny
from drt_backend.lan.protocol import strict_json
from drt_backend.raytracing.pathtracing.contracts import SampleTaskSpec
from .policy import validate,NORMAL_POLICY,LIGHTING_POLICY
from .admission import LOCAL_WORKER_LIMIT,DISTRIBUTED_WORKER_LIMIT,MAX_AGENTS

VERSION='drt.local_room_pbr_codec.v1'
MAX_INIT_BYTES,MAX_TASK_BYTES,MAX_RESULT_BYTES=32768,4096,16384
require,integer,exact=generic.require,generic.integer,tiny.exact
LocalProtocolError,WorkerFailure=generic.LocalProtocolError,generic.WorkerFailure
encode,job_data,read_job,task_data=generic.encode,generic.job_data,generic.read_job,generic.task_data
failure_data,read_failure=generic.failure_data,generic.read_failure
ray_counts,validate_rays,pack_block=tiny.ray_counts,tiny.validate_rays,tiny.pack_block
HEADER_KEYS=tiny.HEADER_KEYS
RESULT_KEYS=tiny.RESULT_KEYS


def workload_from_job(job):
    data=exact(strict_json(job.description),('schema','workload_digest','workload','pixels_per_task'))
    w=tiny.read_workload(data['workload']);validate(w)
    integer(data['schema'],1,1);integer(data['pixels_per_task'],1,32)
    require(job.workload_id==w.workload_kind and job.workload_version=='1'
        and data['workload_digest']==w.workload_digest and job.expected_units==w.pixel_count*w.target_spp,
        'Room job/configuration identity differs.')
    return w,data['pixels_per_task']


def capability(w):
    validate(w)
    return dict(schema='drt.room_pbr_capability.v2',workload=json.loads(w.canonical_bytes()),
        normal_transport=NORMAL_POLICY.canonical_data(),lighting=LIGHTING_POLICY.canonical_data(),
        radiance='drt.pbr_radiance_block.v1',counters='RadianceCountersV1',precision='float64',endian='little',channels=3,
        sample_block_size=8,dimensions=[[160,90],[640,360]],max_spp=8,max_depth=3,max_workers=LOCAL_WORKER_LIMIT,
        max_distributed_workers=DISTRIBUTED_WORKER_LIMIT,max_agents=MAX_AGENTS,worker_selection='automatic_available_logical_cpus',max_task_pixels=32,
        resource='exact_original_glb_sha256_ecs_camera5',max_resource_bytes=16*1024*1024,
        max_init_bytes=32768,max_task_bytes=4096,max_result_bytes=16384,
        timing=dict(schema='drt.room_cpu_deadlines.v1',startup=30,task=30,lease=30,shutdown=5,run=10800),
        memory=dict(per_worker_bytes=512*1024*1024,parent_headroom_bytes=1024*1024*1024))


def match_capability(value,w):require(encode(value,32768)==encode(capability(w),32768),'Unsupported complete room capability.')
def capability_digest(w):return sha256(encode(capability(w),32768)).hexdigest()


def decode(raw,maximum=MAX_RESULT_BYTES):
    require(type(raw) is bytes and 0<len(raw)<=maximum,'Invalid room frame size.')
    data=strict_json(raw);require(type(data) is dict and data.get('version')==VERSION,'Room codec discriminator required.')
    return data


def initialization(scene,file_sha256,job):
    w,_=workload_from_job(job)
    require(Path(scene).is_absolute() and file_sha256==w.scene_sha256,'Room resource initialization differs.')
    return encode(dict(version=VERSION,kind='initialize',scene=str(scene),file_sha256=file_sha256,
                       job=job_data(job),capability=capability(w)),32768)


def read_initialization(raw):
    d=exact(decode(raw,32768),('version','kind','scene','file_sha256','job','capability'))
    require(d['kind']=='initialize' and type(d['scene']) is str and Path(d['scene']).is_absolute()
        and len(d['scene'])<=4096,'Invalid provisioned room path.')
    job=read_job(d['job']);w,_=workload_from_job(job);match_capability(d['capability'],w)
    require(d['file_sha256']==w.scene_sha256,'Wrong room source SHA256.');return d,job


def describe(scene,job):
    from .adapter import RoomAdapter
    from .scene import RoomSource
    w,size=workload_from_job(job);source=RoomSource.load(scene,w)
    adapter=RoomAdapter(source,w,job.run_id,size);require(adapter.job==job,'Room source/ECS/job differs.');return adapter


def task_packet(task):return encode(dict(version=VERSION,kind='task',task=task_data(task)),4096)


def read_task(message,job):
    exact(message,('version','kind','task'));require(message['version']==VERSION and message['kind']=='task','Wrong room task discriminator.')
    task=generic.read_task(dict(version=1,kind='task',task=message['task']),job)
    w,size=workload_from_job(job);start=task.ordinal*size;end=min(start+size,w.pixel_count)
    require(0<=start<end<=w.pixel_count,'Room task ordinal outside planned partition.')
    spec=SampleTaskSpec(w,start,end,0,w.target_spp)
    require(task.payload==spec.canonical_bytes() and task.expected_units==spec.pixel_count*spec.sample_count,
            'Room task is not the exact canonical interval.')
    return task


def header(kind,job,worker_id,pid):
    w,_=workload_from_job(job)
    return generic.header(kind,job,worker_id,pid)|dict(version=VERSION,workload_digest=w.workload_digest,
        capability_digest=capability_digest(w))


def check_header(message,job,worker_id,pid):
    require(all(message.get(k)==v and type(message.get(k)) is type(v) for k,v in header(message.get('kind'),job,worker_id,pid).items()),
            'Room Worker/job/resource/capability binding differs.')


def result_packet(job,worker_id,pid,task,status,counters,started,ended,payload=None,error=None):
    return encode(header('result',job,worker_id,pid)|dict(task=task_data(task),status=status,
        candidate_units=task.expected_units if status=='completed' else 0,
        radiance=None if payload is None else pack_block(payload),rays=ray_counts(counters.primary_rays,counters.shadow_rays,counters.secondary_rays),
        started_ns=started,ended_ns=ended,error=None if error is None else failure_data(error)))


def translate_result(message,adapter,task,context,worker_id,pid):
    exact(message,RESULT_KEYS);check_header(message,task.job,worker_id,pid)
    require(read_task(dict(version=VERSION,kind='task',task=message['task']),task.job)==task,'Foreign room result task.')
    integer(message['started_ns'],1);integer(message['ended_ns'],message['started_ns']);integer(message['candidate_units'])
    rays=validate_rays(message['rays']);status=ExecutionStatus(message['status']);elapsed=(message['ended_ns']-message['started_ns'])/1e9
    if status is not ExecutionStatus.COMPLETED:
        require(message['radiance'] is None and message['candidate_units']==0,'Failed room result carries radiance.')
        failure=read_failure(message['error'])
        if status is ExecutionStatus.CANCELLED:require(message['error']['code']=='CancellationRequested','Unconfirmed room cancellation.')
        return TaskResult.for_task(task,status,executor_id=worker_id,elapsed_seconds=elapsed,failure=failure),rays
    block=tiny.unpack_block(message['radiance'],adapter.task_spec(task),maximum_shadow_lights=5);c=block.counters
    require(message['candidate_units']==task.expected_units and message['error'] is None
        and rays==ray_counts(c.primary_rays,c.shadow_rays,c.secondary_rays),'Room result effort differs.')
    return TaskResult.for_task(task,status,executor_id=worker_id,elapsed_seconds=elapsed,payload=block),rays
