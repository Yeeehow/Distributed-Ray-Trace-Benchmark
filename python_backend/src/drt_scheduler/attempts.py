"""Bounded execution authority around canonical tasks; never a logical acceptance ledger.

One coordinator thread owns this object. Its result authorization and the original
TaskAcceptance commit are serialized with expiry on that same thread.
"""
from collections import deque
from dataclasses import dataclass
import math
from uuid import uuid4


class AttemptError(RuntimeError):
    pass


class RetryExhausted(AttemptError):
    def __init__(self,message,task_id=None):
        super().__init__(message);self.task_id=task_id


@dataclass(frozen=True, slots=True)
class AttemptPolicy:
    max_attempts: int = 3
    max_active: int = 256
    max_per_node: int = 64
    max_pending: int = 512
    max_total_retries: int = 512
    lease_seconds: float = 10
    run_seconds: float = 60
    backoff_seconds: float = .1

    def __post_init__(self):
        for name, low, high in (('max_attempts',1,5),('max_active',1,256),
                ('max_per_node',1,64),('max_pending',1,512),('max_total_retries',0,4096)):
            value = getattr(self,name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'Invalid {name}.')
        for name, low, high in (('lease_seconds',.05,30),('run_seconds',1,75),('backoff_seconds',.01,1)):
            value = getattr(self,name)
            if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid {name}.')


@dataclass(frozen=True, slots=True)
class Attempt:
    task: object
    attempt_id: str
    generation: int
    node: str
    worker_id: str
    session: str
    epoch: int
    dispatched: float
    deadline: float

    @property
    def binding(self):
        job = self.task.job
        return dict(run_id=str(job.run_id),job_identity=job.job_identity,
            configuration_identity=job.configuration_identity,resource_identity=job.resource_identity,
            task_id=self.task.task_id,ordinal=self.task.ordinal,attempt_id=self.attempt_id,
            generation=self.generation,node=self.node,worker_id=self.worker_id,session=self.session,epoch=self.epoch)


class AttemptManager:
    def __init__(self,policy=AttemptPolicy()):
        self.policy=policy
        self.records={}
        self.events=deque(maxlen=512)
        self.retired=deque(maxlen=1024)
        self.active=self.retries=self.expirations=self.stale=self.duplicates=0
        self.peak_active=self.peak_retry=0

    def register(self,task):
        if task.task_id in self.records or len(self.records)>=16384:
            raise AttemptError('Duplicate/excess canonical task.')
        self.records[task.task_id]=dict(task=task,state='Pending',attempt=None,count=0,ready=0)

    def assign(self,task,node,worker_id,session,epoch,now):
        r=self.records[task.task_id]
        if r['state']!='Pending' or now<r['ready'] or self.active>=self.policy.max_active:
            raise AttemptError('Task/attempt capacity is not eligible.')
        if sum(v['state']=='Assigned' and v['attempt'].node==node for v in self.records.values())>=self.policy.max_per_node:
            raise AttemptError('Per-node attempt capacity exceeded.')
        if r['count']>=self.policy.max_attempts or r['count'] and self.retries>=self.policy.max_total_retries:
            r['state']='Failed'
            raise RetryExhausted('Finite execution attempt budget exhausted.',task.task_id)
        if r['count']:self.retries+=1
        r['count']+=1
        a=Attempt(task,str(uuid4()),r['count'],node,worker_id,session,epoch,now,now+self.policy.lease_seconds)
        r.update(state='Assigned',attempt=a)
        self.active+=1;self.peak_active=max(self.peak_active,self.active)
        self.events.append(dict(event='assigned',time=now,lease_deadline=a.deadline,**a.binding))
        return a

    def authorize(self,binding,node,session,epoch,now):
        if not isinstance(binding,dict):raise AttemptError('Missing attempt binding.')
        r=self.records.get(binding.get('task_id'))
        if r is None:raise AttemptError('Foreign canonical TaskId.')
        a=r['attempt']
        if a is not None and binding==a.binding:
            if (a.node,a.session,a.epoch)!=(node,session,epoch):raise AttemptError('Foreign attempt owner.')
            if r['state'] in ('Received','Accepted'):
                self.duplicates+=1;return None
            if r['state']=='Assigned':
                # Expiry wins at equality. Receipt on the sole owner thread is the
                # linearization point, never a peer's clock or reader timestamp.
                if now>=a.deadline:
                    self.retire(a,'LeaseExpired',now);self.stale+=1;return None
                return a
        if binding in self.retired:
            if (binding['node'],binding['session'],binding['epoch'])!=(node,session,epoch):
                raise AttemptError('Retired attempt owner differs.')
            self.stale+=1;return None
        raise AttemptError('Wrong or superseded attempt authority.')

    def received(self,a,now):
        r=self.records[a.task.task_id]
        if r['state']!='Assigned' or r['attempt']!=a:raise AttemptError('Attempt is no longer assigned.')
        r['state']='Received';self.active-=1
        self.events.append(dict(event='validated',time=now,**a.binding))

    def committed_through(self,next_ordinal):
        committed=[]
        for r in self.records.values():
            if r['state']=='Received' and r['task'].ordinal<next_ordinal:
                r['state']='Accepted';committed.append(r['attempt'])
        return committed

    def retire(self,a,reason,now):
        r=self.records[a.task.task_id]
        if r['state']!='Assigned' or r['attempt']!=a:return
        self.retired.append(a.binding)
        r.update(state='Pending',attempt=None,ready=now+min(1,self.policy.backoff_seconds*2**(r['count']-1))
                 +(a.task.ordinal%7)*.01)
        self.active-=1
        if reason=='LeaseExpired' and now>=a.deadline:self.expirations+=1
        self.events.append(dict(event='retired',reason=reason,time=now,**a.binding))
        pending=sum(v['state']=='Pending' and v['count']>0 for v in self.records.values())
        self.peak_retry=max(self.peak_retry,pending)
        if pending>self.policy.max_pending:
            r['state']='Failed';raise RetryExhausted('Retry backlog bound exceeded.',a.task.task_id)
        if r['count']>=self.policy.max_attempts:
            r['state']='Failed';raise RetryExhausted('Per-task attempt budget exhausted.',a.task.task_id)

    def retire_session(self,node,session,reason,now):
        for r in self.records.values():
            a=r['attempt']
            if r['state']=='Assigned' and a.node==node and a.session==session:self.retire(a,reason,now)

    def eligible(self,now):
        return [r['task'] for r in sorted(self.records.values(),key=lambda r:(not bool(r['count']),r['task'].ordinal))
                if r['state']=='Pending' and r['ready']<=now]

    def expired(self,now):
        return [r['attempt'] for r in self.records.values() if r['state']=='Assigned' and now>=r['attempt'].deadline]

    def stop(self,now):
        for r in self.records.values():
            if r['state']=='Assigned':
                a=r['attempt'];self.retired.append(a.binding);self.active-=1;r['state']='Retired'
                self.events.append(dict(event='retired',reason='RunTerminal',time=now,**a.binding))

    def diagnostics(self):
        assigned=sum(r['count'] for r in self.records.values())
        validated=sum(r['state'] in ('Received','Accepted') for r in self.records.values())
        return dict(assigned_attempts=assigned,validated_complete_attempts=validated,
            retired_or_unaccepted_attempts=assigned-validated,retries=self.retries,lease_expirations=self.expirations,stale_results=self.stale,
            duplicate_results=self.duplicates,peak_active=self.peak_active,peak_retry=self.peak_retry,
            event_history=list(self.events),retained_retired=len(self.retired),
            tasks=[dict(task_id=k,ordinal=r['task'].ordinal,attempts=r['count'],state=r['state'],
                        final_attempt=None if r['attempt'] is None else r['attempt'].binding) for k,r in self.records.items()])


def retryable_failure(error):
    """Allowlist infrastructure failures; all numerical/protocol failures default fatal."""
    return getattr(error,'code',None) in {'WorkerCrashed','WorkerPipeClosed','WorkerTaskTimeout',
        'WorkerStartupTimeout','RenderServiceTimeout','TransportDisconnected'}
