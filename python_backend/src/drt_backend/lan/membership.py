"""Run-scoped trusted membership. Only the coordinator owner mutates transitions."""
from collections import deque
from dataclasses import dataclass,field
from .protocol import require,identifier,integer,MAX_NODES

TRANSITIONS={
 'Configured':{'Authenticating','Stopped'},
 'Authenticating':{'Preparing','Unavailable','Failed'},
 'Unavailable':{'Authenticating','Stopped','Failed'},
 'Preparing':{'Ready','Draining','Lost','Failed'},
 'Ready':{'Busy','Draining','Suspect','Lost','Failed'},
 'Busy':{'Ready','Draining','Suspect','Lost','Failed'},
 'Draining':{'Stopped','Lost','Failed'},
 'Suspect':{'Lost','Failed'},
 'Lost':{'Rejoining','Failed'},
 'Rejoining':{'Preparing','Unavailable','Lost','Failed'},
 'Stopped':{'Rejoining'},
 'Failed':set(),
}

@dataclass
class Node:
    config:dict
    state:str='Configured'
    epoch:int=0
    instance:str|None=None
    channel:object=None
    workers:list=field(default_factory=list)
    preparing_slots:int=0
    sessions:list=field(default_factory=list)
    next_connect:float=0
    connects:int=0
    losses:int=0
    draining_until:float=0
    stop_sent:bool=False
    drain_terminal:str='drained'
    admission_enabled:bool=True
    automatic_max:int=1
    last_activity:float=0
    lost_at:float|None=None
    @property
    def id(self):return self.config['node_id']
    @property
    def eligible(self):return self.state in ('Ready','Busy')
    @property
    def reserved(self):
        # Unknown remote processes continue consuming the resource reservation.
        return max(len(self.workers),self.preparing_slots)+sum(s['reserved'] for s in self.sessions if not s['confirmed'])


class Membership:
    def __init__(self,agents,cap,*,maximum_nodes=MAX_NODES):
        integer(maximum_nodes,MAX_NODES,MAX_NODES+1)
        integer(cap,1,256);require(1<=len(agents)<=maximum_nodes,'Invalid trusted membership bound.')
        self.nodes={};self.cap=cap;self.events=deque(maxlen=256)
        for a in agents:
            identifier(a['node_id']);require(a['node_id'] not in self.nodes,'Duplicate trusted Node.')
            self.nodes[a['node_id']]=Node(a)
        self.peak_slots=0

    def transition(self,n,state,reason,now):
        if state==n.state:return
        require(state in TRANSITIONS[n.state],f'Illegal Node transition {n.state}->{state}.')
        self.events.append(dict(node=n.id,epoch=n.epoch,old=n.state,state=state,reason=reason,time=now))
        n.state=state

    def begin(self,n,now):
        require(n.connects<8,'Node reconnection budget exhausted.')
        n.epoch+=1;n.connects+=1;n.stop_sent=False
        self.transition(n,'Rejoining' if n.state in ('Lost','Stopped') else 'Authenticating','connect',now)

    def ready(self,n,workers,now):
        from .protocol import Registry
        registry=Registry(64);registry.register(n.id,n.channel.session,workers)
        require(len({w['pid'] for w in workers})==len(workers),'Duplicate process owns multiple live Worker slots.')
        require(len(workers)==n.preparing_slots and sum(v.reserved for v in self.nodes.values())<=self.cap,'Live/reserved capacity exceeds global ceiling.')
        n.workers=[w|dict(busy=None) for w in workers]
        n.preparing_slots=0
        n.last_activity=now
        self.peak_slots=max(self.peak_slots,sum(v.reserved for v in self.nodes.values()))
        self.transition(n,'Ready' if n.admission_enabled else 'Draining','verified readiness',now)

    def archive(self,n,confirmed,terminal=None):
        if n.channel is not None:
            require(len(n.sessions)<8,'Bounded session history exhausted.')
            n.sessions.append(dict(session=n.channel.session,epoch=n.epoch,instance=n.instance,
                workers=[dict(worker_id=w['worker_id'],pid=w['pid']) for w in n.workers],
                reserved=max(len(n.workers),n.preparing_slots),confirmed=confirmed,terminal=terminal,
                bytes_sent=getattr(n.channel,'bytes_sent',None),bytes_received=getattr(n.channel,'bytes_received',None),
                phase=getattr(n.channel,'phase',None),expected_kind=getattr(n.channel,'expected_kind',None),
                last_received_kind=getattr(n.channel,'last_kind',None),wire_version=getattr(n.channel,'version',None),
                sent_sequence=getattr(n.channel,'sent',None),received_sequence=getattr(n.channel,'received',None),
                read_deadline=getattr(n.channel,'read_deadline',None),write_deadline_seconds=3))
        n.workers=[];n.preparing_slots=0;n.channel=None

    def snapshot(self,n,accepted_rays,recovery):
        return dict(node_id=n.id,state=n.state,epoch=n.epoch,session=None if n.channel is None else n.channel.session,
            admitted=len(n.workers),active=sum(w['busy'] is not None for w in n.workers),
            eligible=n.eligible,accepted_rays=accepted_rays,recovery=recovery)
