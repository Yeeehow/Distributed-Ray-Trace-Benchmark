"""Explicit frontend v4 mapping into the existing IPC Host and PBR strategies."""
import argparse
import base64
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4
from PIL import Image
from drt_scheduler import JobState, LifecycleObservation, ProgressObservation, CancellationRequested
from drt_backend.raytracing.pathtracing.multibounce_policy import make_workload
from . import codec
from .service import execute, create_submission

SCENE = 'm64-area-ggx-v1'
RAW = 'f8863be7127d7bb04edc7beabcffb65b90162ad12bab46f69f33d03f15226159'
DIGEST = 'e9c0c2c8bda87735328c51fbc73a0bc82ab143b61c0708af2d571d1d2994edf7'


def configuration():
    return dict(workloadKind='drt.pbr_pathtracer.v1', presetId=SCENE, resourceSha256=RAW,
        workloadDigest=DIGEST, targetSpp=8, seed=19, maxDepth=1, outputPolicy='drt.color.linear_srgb_clamp_ev0.v1')


def workload():
    w = make_workload(RAW, max_path_depth=1, environment=(.2,.3,.4), emitter_ids=((1,0,0),(1,0,1)))
    codec.require(w.workload_digest == DIGEST, 'PBR preset implementation identity changed.')
    return w


def validate_request(request):
    codec.exact(request, ('protocolVersion','runId','sceneId','mode','width','height','sceneVersion','pbr'))
    codec.require(type(request['protocolVersion']) is int and request['protocolVersion']==4
        and (request['width'],request['height'])==(4,3) and type(request['width']) is int and type(request['height']) is int
        and request['mode'] in ('cpuSerialPreview','localAllCore','distributedAllCore')
        and request['sceneVersion'] is None and type(request['sceneId']) is str and len(request['sceneId'])<=128,
        'Unsupported explicit PBR request.')
    codec.require(codec.encode(request['pbr']) == codec.encode(configuration()), 'Unsupported PBR configuration/policy identity.')
    run=UUID(request['runId'])
    codec.require(str(run)==request['runId'] and run.int!=0, 'Invalid PBR RunId.')
    return run


def load_agents(path):
    # Separate historical tiny-PBR v3 configuration; validate its explicit topology
    # after explicit version selection. This does not negotiate a v2 connection.
    from drt_backend.lan.protocol import strict_json, identifier, fingerprint, integer
    p=Path(path)
    codec.require(p.is_absolute() and p.stat().st_size<=32768,'Absolute bounded PBR topology required.')
    c=codec.exact(strict_json(p.read_bytes()),('wire_version','coordinator_id','certificate','private_key','global_workers','agents'))
    codec.require(type(c['wire_version']) is int and c['wire_version']==3,'PBR requires Worker configuration 3; no downgrade.')
    identifier(c['coordinator_id']);integer(c['global_workers'],1,2)
    codec.require(type(c['agents']) is list and 1<=len(c['agents'])<=2,'PBR Node bound exceeded.')
    identities=set();pins=set();endpoints=set()
    for a in c['agents']:
        codec.exact(a,('node_id','host','port','workers','trust','fingerprint'))
        identifier(a['node_id']);fingerprint(a['fingerprint']);integer(a['port'],1,65535);integer(a['workers'],1,1)
        codec.require(type(a['host']) is str and 0<len(a['host'])<=253 and Path(a['trust']).is_absolute(), 'Invalid PBR endpoint/trust.')
        codec.require(a['node_id'] not in identities and a['fingerprint'] not in pins and (a['host'],a['port']) not in endpoints,'Duplicate PBR Node identity.')
        identities.add(a['node_id']);pins.add(a['fingerprint']);endpoints.add((a['host'],a['port']))
    codec.require(all(Path(c[k]).is_absolute() for k in ('certificate','private_key')),'Absolute PBR credential paths required.')
    return c


def probe(scene, root, agents):
    from .protocol import connect
    sub=create_submission(scene,workload(),resource_root=root)
    nodes=[]
    for a in agents['agents']:
        channel=None
        try:
            channel,hello=connect(agents,a,sub.definition.run_id,1,sub.definition)
            channel.send('probe',dict(file_sha256=RAW,job=codec.job_data(sub.definition)))
            reply=channel.expect('capability')
            codec.require(reply['wire_version']==3 and reply['node']==a['node_id']
                and reply['resource_identity']==sub.definition.resource_identity
                and reply['configuration_identity']==sub.definition.configuration_identity, 'PBR probe binding differs.')
            nodes.append(dict(node_id=a['node_id'],available_workers=hello['workers'],agent_pid=reply['pid'],authenticated=True,resource_verified=True))
            channel.send('bye',{});codec.require(channel.expect('bye')=={},'PBR probe close differs.')
        finally:
            if channel:channel.close()
    return dict(wireVersion=3,nodes=nodes,configuredNodes=[a['node_id'] for a in agents['agents']])


def render(host, request):
    started=perf_counter();events=[];accepted=0;contributors={};nodes={};execution=None;frame=None
    terminal='failed';error=None;counters=None;linear_hash=rgb_hash=None
    config=configuration()

    def event(phase, *, png=None, complete=False):
        data=dict(schemaVersion=1,configuration=config,phase=phase,width=4,height=3,acceptedSamples=accepted,
            totalSamples=96,pngBytes=png,counters=counters,contributions=list(contributors.values()),nodes=list(nodes.values()),
            linearSha256=linear_hash,rgb8Sha256=rgb_hash,elapsedMilliseconds=int((perf_counter()-started)*1000),
            error=error,coverageComplete=complete,isBenchmarkValid=False)
        host.event('pbrEvent',**data);events.append(dict(sequence=host.sequence,**data))

    def observe(item):
        nonlocal accepted
        if isinstance(item,LifecycleObservation) and item.state is JobState.RUNNING:event('rendering')
        if isinstance(item,ProgressObservation):
            accepted=item.completed_units;event('progress')
            if accepted==24 and os.environ.get('DRT_M64_CANCEL_VERIFICATION_GATE')=='1':
                # Explicit bounded verification pause AFTER a real ledger commit.
                # Numerical execution, result bytes and normal runs are unchanged.
                from time import sleep
                until=perf_counter()+10
                while perf_counter()<until:
                    host.token.checkpoint();sleep(.01)

    worker_totals={}
    def contribute(worker,pid,primary,shadow,secondary,samples):
        distributed=request['mode']=='distributedAllCore'
        identity=worker.split('/')[0] if distributed else f'{worker}/PID-{pid}'
        worker_totals[worker]=(samples,primary,secondary,shadow)
        values=tuple(sum(v[i] for k,v in worker_totals.items() if k.split('/')[0]==identity) for i in range(4)) if distributed else (samples,primary,secondary,shadow)
        contributors[identity]=dict(id=identity,pid=0 if distributed else pid,acceptedSamples=values[0],
            primaryRays=values[1],secondaryRays=values[2],shadowRays=values[3])
        event('contribution')

    def node(data):
        prior=nodes.get(data['node_id'])
        confirmed=(prior is not None and prior['capabilityConfirmed']) or data['state'] in ('Ready','Busy','Draining','Stopped') and data['session'] is not None
        nodes[data['node_id']]=dict(id=data['node_id'],state=data['state'],session=data['session'],generation=data['epoch'],
            admitted=data['admitted'],active=data['active'],capabilityConfirmed=confirmed)
        event('node')

    def preview(width,height,units,image):
        stream=BytesIO()
        with Image.frombytes('RGB',(width,height),image) as im:im.save(stream,format='PNG')
        event('preview',png=base64.b64encode(stream.getvalue()).decode('ascii'))

    host.sequence=0
    try:
        event('preparing')
        validate_request(request)
        codec.require(request['sceneId']==SCENE,'UnsupportedPbrScene: only the provisioned synthetic preset is supported.')
        codec.require(host.pbr_scene is not None and host.pbr_root is not None,'PBR fixture has not been provisioned.')
        agents=None if request['mode']!='distributedAllCore' else load_agents(host.pbr_agents)
        if request['mode']=='localAllCore':
            from drt_backend.ipc.host import configure_source_workers
            configure_source_workers()
        execution=execute(host.pbr_scene,workload(),request['mode'],resource_root=host.pbr_root,
            run_id=host.active,cancellation=host.token,observer=observe,preview_observer=preview,
            contribution_observer=contribute,node_observer=node,agents=agents,pixels_per_task=3)
        outcome=execution.outcome
        if outcome.state is not JobState.COMPLETED:
            # An observer can cancel after central commitment but before the
            # normal contribution callback. Read that same committed accounting;
            # never infer accepted effort from received or executed attempts.
            for worker, totals in execution.diagnostics.get('per_worker_acceptance',{}).items():
                committed=(totals['accepted_samples'],totals['accepted_primary'],
                           totals['accepted_secondary'],totals['accepted_shadow'])
                if worker_totals.get(worker)!=committed:
                    contribute(worker,totals['pid'],committed[1],committed[3],committed[2],committed[0])
        outcome.raise_for_failure()
        if outcome.state is JobState.CANCELLED:raise CancellationRequested('PBR cleanup confirmed cancellation.')
        frame=outcome.result
        codec.require(frame is not None and frame.reduction.complete and accepted==96,'Incomplete PBR coverage.')
        c=frame.counters;counters=dict(primaryRays=c.primary_rays,secondaryRays=c.secondary_rays,shadowRays=c.shadow_rays,
            pathCount=c.path_count,surfaceInteractions=c.surface_interactions,schemaVersion=c.schema_version)
        linear_hash=sha256(frame.linear_float64_le).hexdigest();rgb_hash=sha256(frame.display_rgb8).hexdigest()
        host.output.mkdir(parents=True,exist_ok=True)
        from drt_backend.raytracing.output.png_output import save_rgb_png
        import numpy as np
        destination=host.output/f'{host.active}.png'
        save_rgb_png(np.frombuffer(frame.display_rgb8,dtype=np.uint8).reshape(3,4,3),destination)
        for suffix,data in (('.f64',frame.linear_float64_le),('.rgb8',frame.display_rgb8)):
            with (host.output/f'{host.active}{suffix}').open('xb') as f:f.write(data)
        event('frame',png=base64.b64encode(destination.read_bytes()).decode('ascii'),complete=True)
        terminal='completed'
    except CancellationRequested:
        terminal='cancelled';event('cancelling')
    except Exception as e:
        error=dict(code='UnsupportedPbrScene' if 'UnsupportedPbrScene' in str(e) else str(getattr(e,'code',type(e).__name__))[:128],
            message=(str(e) or type(e).__name__)[:4096])
    event(terminal,complete=terminal=='completed')
    host.output.mkdir(parents=True,exist_ok=True)
    record=dict(schema='drt.pbr_frontend_record.v1',request=request,runId=str(host.active),
        configuration=config,events=events,state=terminal,elapsed_seconds=perf_counter()-started,
        diagnostics=None if execution is None else execution.diagnostics,
        jobIdentity=None if execution is None else execution.outcome.job.job_identity,
        ecsSha256=None if execution is None else execution.outcome.job.resource_identity,
        acceptedSamples=accepted,counters=counters,linearSha256=linear_hash,rgb8Sha256=rgb_hash,isBenchmarkValid=False,
        cancellation_verification_gate=os.environ.get('DRT_M64_CANCEL_VERIFICATION_GATE')=='1')
    encoded=json.dumps(record,indent=2,allow_nan=False)
    codec.require(len(encoded.encode())<=4*1024*1024,'PBR diagnostic record exceeds bound.')
    with (host.output/f'{host.active}.pbr.json').open('x',encoding='utf-8') as f:f.write(encoded)


def main():
    p=argparse.ArgumentParser();p.add_argument('--probe',action='store_true',required=True)
    p.add_argument('--scene',type=Path,required=True);p.add_argument('--agents',type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(probe(a.scene,a.scene.parent,load_agents(a.agents))),flush=True)


if __name__=='__main__':main()
