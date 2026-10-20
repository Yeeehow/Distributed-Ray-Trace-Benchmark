"""Original-room frontend v5; bounded events and verified RGB24 transfers."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from time import perf_counter_ns
from uuid import UUID
from drt_scheduler import JobState,LifecycleObservation,ProgressObservation,CancellationRequested
from . import codec
from .policy import make_workload,RAW
from .service import execute
from .frame_transfer import messages
from .topology import load_agents

SCENE='shogi-match-room-v3'


def configuration(w):
    return dict(workloadKind=w.workload_kind,presetId='original-room-camera5-v1',resourceSha256=RAW,
        workloadDigest=w.workload_digest,targetSpp=w.target_spp,seed=w.seed,maxDepth=w.max_path_depth,
        outputPolicy=w.color_output_policy.identity)


def validate_request(request):
    codec.exact(request,('protocolVersion','runId','sceneId','mode','width','height','sceneVersion','pbr'))
    codec.require(type(request['protocolVersion']) is int and request['protocolVersion']==5
        and type(request['width']) is int and type(request['height']) is int
        and request['mode'] in ('cpuSerialPreview','localAllCore','distributedAllCore')
        and request['sceneVersion'] is None and request['sceneId']==SCENE,'Unsupported room request.')
    w=make_workload(width=request['width'],height=request['height'])
    codec.require(codec.encode(request['pbr'])==codec.encode(configuration(w)),'Room numerical identities differ.')
    run=UUID(request['runId']);codec.require(str(run)==request['runId'] and run.int!=0,'Invalid room RunId.')
    return run


def render(host,request):
    started=perf_counter_ns();accepted=0;contributors={};nodes={};execution=None;terminal='failed';error=None
    counters=linear_hash=rgb_hash=None;render_ns=None;last_percent=-1;events=[];frames=[]
    pending_progress=False;pending_preview=None
    w=make_workload(width=request['width'],height=request['height']);config=configuration(w);total=w.pixel_count*w.target_spp

    def event(phase,frame_id=None):
        elapsed=(render_ns if render_ns is not None else perf_counter_ns()-started)//1_000_000
        data=dict(schemaVersion=2,configuration=config,phase=phase,width=w.width,height=w.height,
            acceptedSamples=accepted,totalSamples=total,frameId=frame_id,counters=counters,
            contributions=list(contributors.values()),nodes=list(nodes.values()),linearSha256=linear_hash,rgb8Sha256=rgb_hash,
            elapsedMilliseconds=elapsed,renderNanoseconds=render_ns,error=error,coverageComplete=phase in ('frame','completed'),
            isBenchmarkValid=False)
        host.event('roomEvent',**data)
        events.append(dict(sequence=host.sequence,**data));codec.require(len(events)<=256,'Room event registry exceeds bound.')

    def send_frame(rgb,purpose):
        frame_id=None;first=host.sequence+1;count=0
        for m in messages(w,rgb,purpose=purpose):
            frame_id=m['frameId'];host.event('roomFrame',transfer=m);count+=1
        frames.append(dict(frame_id=frame_id,purpose=purpose,first_sequence=first,last_sequence=host.sequence,
            messages=count,bytes=len(rgb),sha256=sha256(rgb).hexdigest(),accepted_samples=accepted))
        return frame_id

    def observe(item):
        nonlocal accepted,last_percent,pending_progress
        if isinstance(item,LifecycleObservation) and item.state is JobState.RUNNING:event('rendering')
        if isinstance(item,ProgressObservation):
            accepted=item.completed_units;pct=accepted*100//total
            if pct!=last_percent:
                last_percent=pct;pending_progress=True
                if accepted==0:event('progress');pending_progress=False

    def contribute(worker,pid,primary,shadow,secondary,samples):
        nonlocal pending_progress,pending_preview
        # Each accepted Worker remains distinct, including across Agent epochs.
        contributors[worker]=dict(id=worker,pid=pid,acceptedSamples=samples,primaryRays=primary,
            secondaryRays=secondary,shadowRays=shadow)
        # Generic acceptance notifies progress just before its contribution
        # callback. Publish only when both describe that same committed prefix.
        if sum(c['acceptedSamples'] for c in contributors.values())==accepted:
            if pending_progress:event('progress');pending_progress=False
            if pending_preview is not None:
                rgb,pending_preview=pending_preview,None
                event('preview',send_frame(rgb,'preview'))

    def node(data):
        live=data['session'] is not None and data['state'] in ('Preparing','Ready','Busy','Draining')
        value=dict(id=data['node_id'],state=data['state'],session=data['session'],generation=data['epoch'],
            admitted=data['admitted'],active=data['active'],capabilityConfirmed=live,authenticated=live)
        # Busy/Ready changes accompany individual tasks. Emit only changes in
        # connection ownership/readiness; current counters ride progress events.
        old=nodes.get(data['node_id']);nodes[data['node_id']]=value
        if old is None or any(old[k]!=value[k] for k in ('session','generation','authenticated')):event('node')

    def preview(width,height,units,rgb):
        nonlocal pending_preview
        codec.require((width,height,units)==(w.width,w.height,accepted),'Room preview authority differs.')
        codec.require(pending_preview is None,'Unpublished room preview overlap.')
        pending_preview=rgb

    host.sequence=0
    try:
        event('preparing');validate_request(request)
        codec.require(host.room_scene is not None and host.room_root is not None,'Original room has not been provisioned.')
        agents=load_agents(host.room_agents) if request['mode']=='distributedAllCore' else None
        before=perf_counter_ns()
        execution=execute(host.room_scene,w,request['mode'],resource_root=host.room_root,run_id=host.active,
            cancellation=host.token,observer=observe,preview_observer=preview,contribution_observer=contribute,
            node_observer=node,agents=agents,pixels_per_task=32)
        render_ns=perf_counter_ns()-before;outcome=execution.outcome
        # Read the same central committed record when an observer interrupted a
        # callback. Received/physical effort never becomes accepted contribution.
        for worker,t in execution.diagnostics.get('per_worker_acceptance',{}).items():
            contribute(worker,t['pid'],t['accepted_primary'],t['accepted_shadow'],t['accepted_secondary'],t['accepted_samples'])
        outcome.raise_for_failure()
        if outcome.state is JobState.CANCELLED:raise CancellationRequested('Room cleanup confirmed cancellation.')
        f=outcome.result
        codec.require(f is not None and f.reduction.complete and accepted==total,'Incomplete room coverage.')
        c=f.counters;counters=dict(primaryRays=c.primary_rays,secondaryRays=c.secondary_rays,shadowRays=c.shadow_rays,
            pathCount=c.path_count,surfaceInteractions=c.surface_interactions,schemaVersion=c.schema_version)
        if request['mode']=='cpuSerialPreview':contribute('serial/worker-0',os.getpid(),c.primary_rays,c.shadow_rays,c.secondary_rays,total)
        linear_hash=sha256(f.linear_float64_le).hexdigest();rgb_hash=sha256(f.display_rgb8).hexdigest()
        host.output.mkdir(parents=True,exist_ok=True)
        def publish():
            from drt_backend.raytracing.output.png_output import save_rgb_png
            import numpy as np
            for suffix,data in (('.f64',f.linear_float64_le),('.rgb8',f.display_rgb8)):
                with (host.output/f'{host.active}{suffix}').open('xb') as file:file.write(data)
            save_rgb_png(np.frombuffer(f.display_rgb8,dtype=np.uint8).reshape(w.height,w.width,3),host.output/f'{host.active}.png')
            event('frame',send_frame(f.display_rgb8,'final'))
        host.token.commit(publish);terminal='completed'
    except CancellationRequested:
        terminal='cancelled';event('cancelling')
    except Exception as e:
        error=dict(code=str(getattr(e,'code',type(e).__name__))[:128],message=(str(e) or type(e).__name__)[:4096])
    event(terminal)
    host.output.mkdir(parents=True,exist_ok=True)
    record=dict(schema='drt.room_frontend_record.v1',request=request,runId=str(host.active),configuration=config,
        jobIdentity=None if execution is None else execution.outcome.job.job_identity,
        configurationIdentity=None if execution is None else execution.outcome.job.configuration_identity,
        resourceIdentity=None if execution is None else execution.outcome.job.resource_identity,
        events=events,frames=frames,state=terminal,render_nanoseconds=render_ns,frontend_nanoseconds=perf_counter_ns()-started,
        timing_definition='Contiguous execute invocation including source/capacity preparation, numerical work, acceptance, finalization and confirmed cleanup; excludes final output publication and frame transfer.',
        diagnostics=None if execution is None else execution.diagnostics,acceptedSamples=accepted,counters=counters,
        linearSha256=linear_hash,rgb8Sha256=rgb_hash,isBenchmarkValid=False)
    encoded=json.dumps(record,indent=2,allow_nan=False);codec.require(len(encoded.encode())<=16*1024*1024,'Room record exceeds bound.')
    with (host.output/f'{host.active}.room.json').open('x',encoding='utf-8') as file:file.write(encoded)


def probe(scene,agents,*,width=160,height=90):
    from .service import create_submission
    from .protocol import connect
    w=make_workload(width=width,height=height);sub=create_submission(scene,w,resource_root=Path(scene).parent,workers=1)
    nodes=[]
    for a in agents['agents']:
        channel=None
        try:
            channel,hello=connect(agents,a,sub.definition.run_id,1,sub.definition)
            channel.send('probe',dict(file_sha256=RAW,job=codec.job_data(sub.definition)))
            r=codec.exact(channel.expect('capability'),('node','file_sha256','resource_identity','configuration_identity','available_workers','pid','wire_version'))
            codec.require(r['node']==a['node_id'] and r['file_sha256']==RAW and type(r['wire_version']) is int and r['wire_version']==4
                and r['resource_identity']==sub.definition.resource_identity and r['configuration_identity']==sub.definition.configuration_identity,
                'Room capability probe differs.')
            codec.integer(r['pid'],1,2**32-1);codec.integer(r['available_workers'],1,64)
            nodes.append(dict(node_id=a['node_id'],available_workers=r['available_workers'],agent_pid=r['pid'],authenticated=True,resource_verified=True))
            channel.send('bye',{});codec.require(channel.expect('bye')=={},'Room probe close differs.')
        except (ConnectionRefusedError,TimeoutError):pass
        finally:
            if channel:channel.close()
    return dict(wireVersion=4,nodes=nodes,configuredNodes=[a['node_id'] for a in agents['agents']])


def main():
    p=argparse.ArgumentParser();p.add_argument('--probe',action='store_true',required=True)
    p.add_argument('--scene',type=Path,required=True);p.add_argument('--agents',type=Path,required=True)
    p.add_argument('--width',type=int,default=160);p.add_argument('--height',type=int,default=90)
    a=p.parse_args();print(json.dumps(probe(a.scene,load_agents(a.agents),width=a.width,height=a.height)),flush=True)


if __name__=='__main__':main()
