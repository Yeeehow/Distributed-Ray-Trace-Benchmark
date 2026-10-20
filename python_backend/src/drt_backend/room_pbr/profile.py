"""Room capability hooks into the existing authenticated dynamic scheduler."""
from dataclasses import dataclass,field
from types import SimpleNamespace
from . import codec,protocol
from .preview import attach
from .worker import worker_main
from .admission import RoomOptions,RoomAttemptPolicy,admit,select,LOCAL_WORKER_LIMIT,DISTRIBUTED_WORKER_LIMIT,MAX_AGENTS,LOCAL_NODE_ID


@dataclass
class Profile:
    submission:object=None
    local_sessions:list=field(default_factory=list)
    maximum_nodes=MAX_AGENTS+1
    reserve_node_slots=True
    codec=codec
    wire_version=4
    ready_seconds=30
    worker_supervision_seconds=10840
    worker_target=staticmethod(worker_main)
    attach=staticmethod(attach)
    worker_admission=staticmethod(admit)
    worker_options=staticmethod(lambda count:RoomOptions(workers=count))
    worker_allocation=staticmethod(lambda count,cap:select(count,cap))

    def connect(self,config,agent,run_id,epoch):
        if agent.get('owned_local') is True:
            from .local_node import connect
            return connect(self,config,run_id,epoch)
        return protocol.connect(config,agent,run_id,epoch,self.submission.definition)

    def close_local_sessions(self):
        from time import monotonic
        deadline=monotonic()+6
        for thread in self.local_sessions:thread.join(max(0,deadline-monotonic()))
        return [thread.name for thread in self.local_sessions if thread.is_alive()]

    hello=staticmethod(protocol.accept_hello)

    @staticmethod
    def verify_resource(scene,body,args):
        from .service import provisioned
        codec.exact(body,('file_sha256','job'))
        job=codec.read_job(body['job']);w,_=codec.workload_from_job(job);hello=args.room_hello
        codec.require(job.job_identity==hello['job_identity'] and str(job.run_id)==hello['run_id']
            and body['file_sha256']==w.scene_sha256,'Room negotiated job/resource differs.')
        codec.match_capability(hello['capability'],w);scene=provisioned(scene,args.resource_root)
        return SimpleNamespace(definition=job,adapter=codec.describe(scene,job))

    @staticmethod
    def monitor(channel,args,hello,body):
        from time import monotonic
        import os
        codec.exact(body,('schema','file_sha256','job'));codec.require(body['schema']=='drt.room_idle_monitor.v1','Room monitor schema required.')
        sub=Profile.verify_resource(args.scene,{k:body[k] for k in ('file_sha256','job')},args)
        binding=dict(schema='drt.room_idle_monitor.v1',run_id=hello['run_id'],epoch=hello['epoch'])
        channel.send('monitor_state',binding|dict(node=args.node_id,state='connected',pid=os.getpid(),numerical_workers=0,
            job_identity=sub.definition.job_identity,resource_identity=sub.definition.resource_identity))
        deadline=monotonic()+21600
        while monotonic()<deadline:
            kind,value=channel.receive(5)
            if kind=='bye':
                codec.require(value=={},'Invalid monitor close.');channel.send('bye',{});return None
            codec.require(kind=='heartbeat' and value==binding,'Foreign monitor health message.')
            channel.send('heartbeat',binding)
        raise TimeoutError('Bounded idle monitor lifetime expired.')


def execute_distributed(sub,w,agents,token,observer,preview,contribute,node):
    from drt_backend.lan.dynamic_coordinator import execute_dynamic
    from drt_backend.pbr_execution.service import PbrExecutionResult
    codec.require(agents is not None and type(agents['wire_version']) is int and agents['wire_version']==4
        and 1<=len(agents['agents'])<=MAX_AGENTS and type(agents['global_workers']) is int
        and 1<=agents['global_workers']<=DISTRIBUTED_WORKER_LIMIT,
        'Room requires selected authenticated wire 4 Agents within the 256-Worker limit.')
    codec.require(not any(a['node_id']==LOCAL_NODE_ID for a in agents['agents']),'Owned local Node identity is reserved.')
    codec.require(agents['global_workers']>=len(agents['agents'])+1,
        'Local plus selected remote Nodes require at least one global Worker slot per participant.')
    config=dict(agents,agents=[dict(node_id=LOCAL_NODE_ID,workers='auto',owned_local=True),*agents['agents']])
    result=execute_dynamic(sub.source_path,w,config=config,run_id=sub.definition.run_id,cancellation=token,
        observer=observer,preview_observer=preview,contribution_observer=contribute,node_observer=node,
        _policy=RoomAttemptPolicy(max_active=agents['global_workers'],max_pending=2*agents['global_workers']),_profile=Profile(sub))
    return PbrExecutionResult(result.outcome,result.diagnostics|dict(resource_profile=codec.capability(w)['memory']|dict(
        workers=agents['global_workers'],max_per_agent=LOCAL_WORKER_LIMIT),timing_policy='drt.room_cpu_deadlines.v1'))
