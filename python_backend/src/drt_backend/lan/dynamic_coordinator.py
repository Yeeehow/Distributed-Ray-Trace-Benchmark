"""Dynamic orchestration around ONE unchanged TaskAcceptance and workload-owned Float64 assembly.

All state/expiry/result decisions run on this owner thread. Transport threads only
enqueue immutable generation-tagged events. Received valid results are fenced
before ordered acceptance; task failure attempts never enter the logical ledger.
"""
from dataclasses import asdict
import queue
from time import monotonic,perf_counter_ns
from drt_scheduler import (CancellationRequested,ExecutionContext,ExecutionStatus,JobState,JobOutcome,JobTimings,TaskState,
    LifecycleObservation,ProgressObservation)
from drt_scheduler.attempts import AttemptManager,AttemptPolicy,RetryExhausted,retryable_failure
from drt_scheduler.dispatch_policy import AdaptiveDispatch
from drt_backend.application.parallel_service import OrderedAcceptance
from drt_backend.local_parallel import codec
from .dynamic_protocol import binding,transient
from .protocol import exact,require,integer
from .membership import Membership
from .session_manager import SessionManager
from .attempt_evidence import AttemptEvidence


class MembershipControl:
    """Bounded operator requests for configured identities only, no endpoint upload."""
    def __init__(self):self.pending=queue.Queue(maxsize=16)
    def request(self,operation,node=None):
        require(operation in ('refresh','drain'),'Unsupported membership operation.')
        if node is not None:
            from .protocol import identifier
            identifier(node)
        try:self.pending.put_nowait((operation,node))
        except queue.Full:raise ValueError('Membership request backlog is full.')




def validate_receipt(body,n,entry,job,acceptance,expected=None):
    codec=acceptance.codec
    exact(body,'node run_id epoch instance session state shutdown error')
    require((body['node'],body['run_id'],body['epoch'],body['session'],body['instance'])==
        (n.id,str(job.run_id),entry['epoch'],entry['session'],entry['instance']),'Foreign shutdown receipt.')
    require(body['state'] in ('completed','drained','cancelled','failed'),'Invalid remote terminal state.')
    if body['error'] is not None:codec.read_failure(body['error'])
    if body['state'] in ('completed','drained'):require(body['error'] is None,'Successful shutdown reported an error.')
    if expected is not None:require(body['state']==expected,'Remote stop was not confirmed in the requested state.')
    s=exact(body['shutdown'],'forced remaining_owned_pids cancellation_confirmed errors shutdown_nanoseconds workers')
    require(s['forced'] is False and s['remaining_owned_pids']==[] and s['errors']==[],'Remote termination forced or unverified.')
    integer(s['shutdown_nanoseconds'],0);require(type(s['cancellation_confirmed']) is bool,'Invalid cancellation proof.')
    if expected=='cancelled':require(s['cancellation_confirmed'],'Remote cancellation unconfirmed.')
    require(type(s['workers']) is list and len(s['workers'])==entry['reserved'],'Cleanup roster count differs.')
    roster={w['worker_id']:w['pid'] for w in entry['workers']};seen=set()
    for w in s['workers']:
        exact(w,'worker_id pid exit_code forced stopped executed_tasks executed_rays')
        require(w['worker_id'] in {f'worker-{i}' for i in range(entry['reserved'])} and w['worker_id'] not in seen
            and (not roster or roster.get(w['worker_id'])==w['pid'])
            and type(w['exit_code']) is int and w['exit_code']==0 and w['forced'] is False and w['stopped'] is True,'Worker natural shutdown is not confirmed.')
        seen.add(w['worker_id']);integer(w['pid'],1,2**32-1);integer(w['executed_tasks'],0,16384)
        rays=exact(w['executed_rays'],'primary secondary shadow total')
        integer(rays['total'],0)
        codec.validate_rays(rays)
        accepted=acceptance.workers.get(f'{n.id}/e{entry["epoch"]}/{w["worker_id"]}',{})
        require(rays['primary']>=accepted.get('accepted_primary',0) and rays['shadow']>=accepted.get('accepted_shadow',0)
                and w['executed_tasks']>=accepted.get('accepted_tasks',0),'Executed effort contradicts acceptance.')
        require(rays['secondary']>=accepted.get('accepted_secondary',0),'PBR secondary effort contradicts acceptance.')
    return s

def require_retryable_receipt(body):
    if body['error'] is not None:
        error=codec.read_failure(body['error'])
        if body['error']['code'] not in {'EOFError','TimeoutError','ConnectionResetError','ConnectionAbortedError','BrokenPipeError',
                'SSLEOFError','SSLZeroReturnError','CancellationRequested'} and not retryable_failure(error):raise error


def execute_dynamic(scene,options,*,config,run_id,cancellation,observer=None,contribution_observer=None,
        preview_observer=None,node_observer=None,membership_control=None,_policy=None,_profile=None):
    require(_profile is not None and _profile.wire_version in (3,4),'Explicit PBR coordinator profile required.')
    codec=_profile.codec
    policy=_policy or AttemptPolicy(max_active=config['global_workers'],max_pending=min(512,config['global_workers']*2))
    require(policy.max_active<=config['global_workers'],'Attempt limit exceeds global admission.')
    before=perf_counter_ns();deadline=monotonic()+policy.run_seconds
    submission=_profile.submission
    submission,observed,preview=_profile.attach(submission,observer,preview_observer)
    job,adapter=submission.definition,submission.adapter
    context=ExecutionContext(job,cancellation)
    acceptance=OrderedAcceptance(adapter,context,policy.max_pending,observed,contribution_observer,
        _codec=codec)
    attempts=AttemptManager(policy);membership=Membership(config['agents'],config['global_workers'],
        maximum_nodes=getattr(_profile,'maximum_nodes',16))
    effort=AttemptEvidence(_codec=codec)
    transport=SessionManager(config,job,submission.file_sha256,_profile=_profile);scheduler=AdaptiveDispatch()
    control=membership_control or MembershipControl()
    result=failure=None;errors=[];state=JobState.FAILED;exhausted=False
    task_count=sum(1 for _ in adapter.enumerate_tasks(job));require(task_count<=16384,'Task descriptor bound exceeded.')
    iterator=iter(adapter.enumerate_tasks(job));notifications={};last_heartbeat=monotonic()
    preparation_ns=execution_ns=finalization_ns=0;running_at=None
    metrics=dict(initial_ready_seconds=None,reconnections=[],disconnects=0,stale_session_events=0,
                 discarded_known_primary=0,discarded_known_shadow=0,unknown_sessions=0,transport_errors=[])
    hybrid=any(n.config.get('owned_local') for n in membership.nodes.values())
    initially_ready=set();initial_deadline=monotonic()+getattr(_profile,'ready_seconds',30)+5
    def notify(item):
        if observed:observed(item)
    def accepted_node(n):
        return sum(v['accepted_primary']+v['accepted_shadow']+v.get('accepted_secondary',0)
                   for k,v in acceptance.workers.items() if k.split('/')[0]==n.id)
    def publish(n,force=False):
        data=membership.snapshot(n,accepted_node(n),any(s for s in n.sessions if not s['confirmed']))
        prior=notifications.get(n.id);now=monotonic()
        if node_observer and (force or prior is None or data!=prior[0] and now-prior[1]>=.2):
            node_observer(data);notifications[n.id]=(data,now)
    def lose(n,reason):
        now=monotonic();session=n.channel.session if n.channel else None
        if n.state in ('Ready','Busy'):membership.transition(n,'Suspect',reason,now)
        membership.transition(n,'Lost',reason,now)
        transport.close_node(n);membership.archive(n,False)
        n.losses+=1;n.lost_at=now;n.next_connect=now+min(2,.25*2**min(n.losses,3));metrics['disconnects']+=1
        if session:attempts.retire_session(n.id,session,reason,now)
        publish(n,True)
    def ready(n,data):
        b=exact(data['body'],'node run_id epoch instance file_sha256 job_identity configuration_identity resource_identity workers')
        integer(b['epoch'],1,8)
        require((b['node'],b['run_id'],b['epoch'],b['instance'],b['file_sha256'],b['job_identity'],b['configuration_identity'],b['resource_identity'])==
            (n.id,str(run_id),n.epoch,n.instance,submission.file_sha256,job.job_identity,job.configuration_identity,job.resource_identity)
            and len(b['workers'])==data['count'],'Readiness identity/capacity mismatch.')
        membership.ready(n,b['workers'],monotonic())
        initially_ready.add(n.id)
        if not n.admission_enabled:n.draining_until=min(deadline,monotonic()+5)
        if metrics['initial_ready_seconds'] is None:metrics['initial_ready_seconds']=(perf_counter_ns()-before)/1e9
        if n.lost_at is not None:
            metrics['reconnections'].append(dict(node=n.id,epoch=n.epoch,seconds=monotonic()-n.lost_at));n.lost_at=None
        publish(n,True)
    def validate_stale(n,epoch,session,data):
        exact(data,'attempt packet');a=binding(data['attempt'])
        require((a['node'],a['epoch'],a['session'],a['run_id'])==(n.id,epoch,session,str(run_id)),'Foreign stale result scope.')
        record=attempts.records.get(a['task_id']);require(record is not None,'Foreign stale canonical task.')
        task=record['task']
        require((a['ordinal'],a['job_identity'],a['configuration_identity'],a['resource_identity'])==
            (task.ordinal,job.job_identity,job.configuration_identity,job.resource_identity),'Stale canonical identity differs.')
        roster=n.workers if n.epoch==epoch and n.channel and n.channel.session==session else next(
            (s['workers'] for s in n.sessions if s['epoch']==epoch and s['session']==session),[])
        worker=next((w for w in roster if w['worker_id']==a['worker_id']),None)
        require(worker is not None,'Unregistered stale Worker.')
        translated,rays=codec.translate_result(data['packet'],adapter,task,context,worker['worker_id'],worker['pid'])
        effort.report(a,data['packet'],rays,current=False)
        if translated.status==ExecutionStatus.FAILED and not retryable_failure(translated.failure):raise translated.failure
    def terminal(n,b,expected=None):
        entry=dict(session=n.channel.session,epoch=n.epoch,instance=n.instance,
            workers=[dict(worker_id=w['worker_id'],pid=w['pid']) for w in n.workers],
            reserved=max(len(n.workers),n.preparing_slots))
        validate_receipt(b,n,entry,job,acceptance,expected)
        transport.close_node(n);membership.archive(n,True,b)
        if n.state=='Draining':membership.transition(n,'Stopped','confirmed graceful drain',monotonic())
        elif n.state in ('Ready','Busy'):
            membership.transition(n,'Draining','confirmed run stop',monotonic());publish(n,True)
            membership.transition(n,'Stopped','confirmed run stop',monotonic())
        else:
            membership.transition(n,'Lost','confirmed stopped startup',monotonic())
        publish(n,True)
    try:
        notify(LifecycleObservation(run_id,JobState.PREPARING));adapter.prepare(context)
        for n in membership.nodes.values():publish(n,True)
        # Dynamic preparation may overlap the first Node's numerical work.
        notify(LifecycleObservation(run_id,JobState.RUNNING));notify(ProgressObservation(run_id,0,job.expected_units,0))
        running_at=perf_counter_ns();preparation_ns=running_at-before
        while (acceptance.ledger.completed_units<job.expected_units
               or any(not s['confirmed'] for n in membership.nodes.values() for s in n.sessions)):
            cancellation.checkpoint();now=monotonic()
            if now>=deadline:raise TimeoutError('Dynamic whole-run deadline exhausted.')
            if hybrid and len(initially_ready)<len(membership.nodes) and now>=initial_deadline:
                raise TimeoutError('Local and every selected remote Node must become ready before hybrid computation.')
            while not exhausted and len(acceptance.pending)<acceptance.window:
                try:task=next(iterator)
                except StopIteration:exhausted=True;break
                acceptance.register(task);attempts.register(task)
            while True:
                try:operation,node_id=control.pending.get_nowait()
                except queue.Empty:break
                require(node_id is None or node_id in membership.nodes,'Operator requested an unconfigured Node.')
                selected=membership.nodes.values() if node_id is None else [membership.nodes[node_id]]
                for n in selected:
                    if operation=='refresh' and n.state in ('Configured','Unavailable','Lost','Stopped'):
                        n.admission_enabled=True;n.next_connect=0
                    if operation=='drain':
                        n.admission_enabled=False
                        if n.eligible:
                            n.drain_terminal='drained'
                            membership.transition(n,'Draining','operator drain',now);n.draining_until=min(deadline,now+5)
                            transport.send(n,'drain',dict(run_id=str(run_id),epoch=n.epoch));publish(n,True)
                        elif n.state=='Preparing':
                            n.drain_terminal='cancelled'
                            transport.send(n,'cancel',dict(run_id=str(run_id),epoch=n.epoch),priority=True)
                        elif n.state in ('Configured','Unavailable'):
                            membership.transition(n,'Stopped','operator stopped admission',now);n.next_connect=float('inf');publish(n,True)
            for n in membership.nodes.values():
                if n.state in ('Configured','Unavailable','Lost') and now>=n.next_connect and n.connects<8:
                    membership.begin(n,now);transport.open(n);publish(n,True)
                elif n.state=='Stopped' and n.next_connect==0 and n.connects<8:
                    membership.begin(n,now);transport.open(n);publish(n,True)
                if n.state=='Draining' and now>=n.draining_until:lose(n,'DrainDeadline')
            for a in attempts.expired(now):
                n=membership.nodes[a.node]
                if n.channel and n.channel.session==a.session:lose(n,'LeaseExpired')
            if now-last_heartbeat>=1:
                for n in membership.nodes.values():
                    if n.state in ('Ready','Busy','Draining'):transport.send(n,'heartbeat',dict(run_id=str(run_id),epoch=n.epoch))
                last_heartbeat=now
            # Never silently execute a Distributed workload locally while its selected
            # remote Nodes have not even established their initial Worker authority.
            for task in (attempts.eligible(now) if not hybrid or len(initially_ready)==len(membership.nodes) else ()):
                candidates=[(n.id,len(n.workers),sum(w['busy'] is not None for w in n.workers),
                    [w['worker_id'] for w in n.workers if w['busy'] is None]) for n in membership.nodes.values()
                    if n.eligible and any(w['busy'] is None for w in n.workers)]
                choice=scheduler.choose(candidates,task.expected_units,attempts.records[task.task_id]['count']>0)
                if choice is None or attempts.active>=policy.max_active:break
                n=membership.nodes[choice[0]];w=next(w for w in n.workers if w['worker_id']==choice[1])
                a=attempts.assign(task,n.id,w['worker_id'],n.channel.session,n.epoch,now);w['busy']=a
                membership.transition(n,'Busy','dispatch',now)
                transport.send(n,'assign',dict(attempt=a.binding,task=codec.task_data(task),lease_ms=int(policy.lease_seconds*1000)))
            try:node_id,epoch,session,kind,data=transport.events.get(timeout=.01)
            except queue.Empty:
                for n in membership.nodes.values():publish(n)
                continue
            n=membership.nodes[node_id]
            if epoch!=n.epoch or kind not in ('connected','connect_error') and session is not None and (n.channel is None or session!=n.channel.session):
                metrics['stale_session_events']+=1
                if kind=='connected':data['channel'].close()
                elif kind=='result':validate_stale(n,epoch,session,data)
                elif kind=='terminal':
                    entry=next((s for s in n.sessions if s['epoch']==epoch and s['session']==session),None)
                    require(entry is not None,'Foreign retired terminal session.')
                    validate_receipt(data,n,entry,job,acceptance);entry.update(confirmed=True,terminal=data)
                    require_retryable_receipt(data)
                continue
            if kind=='connect_error':
                if not transient(data):raise data
                membership.transition(n,'Unavailable','transient endpoint unavailable',monotonic())
                n.next_connect=monotonic()+min(2,.25*2**min(n.connects-1,3));publish(n,True)
            elif kind=='connected':
                n.channel=data['channel'];hello=data['hello'];n.instance=hello['instance']
                for response in data['receipts']:
                    entry=next(s for s in n.sessions if s['session']==response['session'])
                    require(response['receipt'] is not None,'Prior remote cleanup remains unknown; refusing new numerical authority.')
                    validate_receipt(response['receipt'],n,entry,job,acceptance)
                    entry.update(confirmed=True,terminal=response['receipt'])
                    require_retryable_receipt(response['receipt'])
                require(all(s['confirmed'] for s in n.sessions),'Outstanding remote cleanup uncertainty.')
                if acceptance.ledger.completed_units==job.expected_units or not n.admission_enabled:
                    # This authenticated connection has never initialized a pool.
                    # Retired numerical ownership was proved by the actual receipts.
                    n.channel.send('bye',{});n.channel.close();n.channel=None
                    membership.transition(n,'Unavailable','cleanup reconciled; admission stopped or no remaining work',monotonic())
                    n.next_connect=float('inf');publish(n,True);continue
                free=config['global_workers']-sum(v.reserved for v in membership.nodes.values())
                if getattr(_profile,'reserve_node_slots',False):
                    # A fast local hello must not consume a small global ceiling before remote
                    # authentication finishes. Keep one slot per other pending participant.
                    waiting=sum(v.id!=n.id and v.admission_enabled and v.state in ('Configured','Authenticating','Rejoining')
                        and v.reserved==0 for v in membership.nodes.values())
                    free=max(1,free-min(waiting,max(0,free-1))) if free>0 else 0
                count=min(hello['workers'],64,policy.max_per_node,free,task_count)
                if n.config['workers']!='auto':count=min(count,n.config['workers'])
                if count<=0:
                    n.channel.close();n.channel=None
                    membership.transition(n,'Unavailable','global capacity reserved; admission deferred',monotonic())
                    n.next_connect=monotonic()+1;publish(n,True);continue
                n.automatic_max=hello['workers'];n.preparing_slots=count
                membership.transition(n,'Preparing','TLS/session/resource initialization',monotonic())
                transport.prepare(n,count);publish(n,True)
            elif kind=='ready':ready(n,data)
            elif kind=='error':
                metrics['transport_errors'].append(dict(node=n.id,epoch=epoch,phase=n.channel.phase if n.channel else 'closed',
                    error=type(data).__name__,expected=None if n.channel is None else n.channel.expected_kind))
                metrics['transport_errors']=metrics['transport_errors'][-64:]
                if not transient(data,authenticated=True):raise data
                if n.channel:lose(n,'TransportDisconnected')
            elif kind=='heartbeat':
                require(data==dict(run_id=str(run_id),epoch=n.epoch),'Foreign health heartbeat.')
                n.last_activity=monotonic() # Never renews attempt deadline.
            elif kind=='terminal':
                if n.state=='Draining':terminal(n,data,n.drain_terminal);n.next_connect=float('inf')
                else:
                    # A worker/Agent failure is retryable only through the explicit allowlist.
                    error=codec.read_failure(data['error']) if data.get('error') else RuntimeError('Unexpected remote terminal.')
                    current=n.channel.session
                    terminal(n,data)
                    if not retryable_failure(error):raise error
                    attempts.retire_session(n.id,current,'WorkerFailure',monotonic())
                    if n.state=='Stopped':n.next_connect=0
            elif kind=='result':
                exact(data,'attempt packet');identity=binding(data['attempt'])
                a=attempts.authorize(identity,n.id,session,epoch,monotonic())
                if a is None:
                    validate_stale(n,epoch,session,data)
                    if attempts.records[identity['task_id']]['state']=='Pending' and n.channel:lose(n,'LeaseExpired')
                    continue
                require(n.eligible or n.state=='Draining','Result from ineligible session.')
                w=next((w for w in n.workers if w['worker_id']==a.worker_id),None)
                require(w is not None and w['busy']==a,'Attempt Worker ownership differs.')
                # Complete codec validation occurs before any ledger or preview mutation.
                translated,rays=codec.translate_result(data['packet'],adapter,a.task,context,w['worker_id'],w['pid'])
                effort.report(identity,data['packet'],rays,current=True)
                acceptance.ledger.validate_result(a.task,translated)
                if translated.status is not ExecutionStatus.COMPLETED:
                    if translated.status is ExecutionStatus.CANCELLED:
                        require(cancellation.requested,'Unsolicited current-attempt cancellation.')
                        raise CancellationRequested('Run cancellation was requested.')
                    if retryable_failure(translated.failure):lose(n,'WorkerFailure');continue
                    acceptance.ledger.stop_task(a.task,TaskState.FAILED)
                    raise translated.failure or RuntimeError('Unsolicited attempt cancellation.')
                attempts.received(a,monotonic());w['busy']=None
                scheduler.observe(n.id,monotonic()-a.dispatched,a.task.expected_units)
                acceptance.offer(data['packet'],w['worker_id'],w['pid'],identity=f'{n.id}/e{epoch}/{w["worker_id"]}')
                for committed in attempts.committed_through(acceptance.next_ordinal):
                    scheduler.accepted(committed.node,committed.task.expected_units);effort.committed(committed)
                if n.state=='Busy' and not any(w['busy'] for w in n.workers):membership.transition(n,'Ready','results received',monotonic())
            for n in membership.nodes.values():publish(n)
        acceptance.require_complete();execution_ns=perf_counter_ns()-running_at
        notify(LifecycleObservation(run_id,JobState.FINALIZING));start=perf_counter_ns()
        result=cancellation.commit(lambda:adapter.finalize(context));finalization_ns=perf_counter_ns()-start
        state=JobState.COMPLETED
    except CancellationRequested:state=JobState.CANCELLED
    except RetryExhausted as error:
        failure=error
        task=acceptance.pending.get(error.task_id)
        if task is not None:acceptance.ledger.stop_task(task,TaskState.FAILED)
    except BaseException as error:failure=error
    finally:
        cleanup_start=perf_counter_ns();attempts.stop(monotonic())
        if running_at and not execution_ns:execution_ns=cleanup_start-running_at
        aborted=state!=JobState.COMPLETED;stop_deadline=monotonic()+6
        for n in membership.nodes.values():
            if n.channel and n.state in ('Ready','Busy','Preparing','Draining'):
                try:
                    if aborted or n.state!='Draining':transport.send(n,'cancel' if aborted else 'finish',dict(run_id=str(run_id),epoch=n.epoch),priority=aborted)
                    n.stop_sent=True
                except BaseException as error:errors.append(error)
        while any(n.channel and n.preparing_slots or n.channel and n.workers for n in membership.nodes.values()) and monotonic()<stop_deadline:
            try:node_id,epoch,session,kind,data=transport.events.get(timeout=.05)
            except queue.Empty:continue
            n=membership.nodes[node_id]
            if epoch!=n.epoch or n.channel is None or session!=n.channel.session:continue
            try:
                if kind=='ready':ready(n,data)
                elif kind=='terminal':terminal(n,data,('cancelled' if state==JobState.CANCELLED else None) if aborted else n.drain_terminal if n.state=='Draining' else 'completed')
                elif kind=='error':raise data
                # Results after abort are executed effort only, never acceptance.
            except BaseException as error:errors.append(error);transport.close_node(n);membership.archive(n,False)
        for n in membership.nodes.values():
            if n.workers or n.preparing_slots:errors.append(RuntimeError('Current remote shutdown unconfirmed.'))
            for entry in n.sessions:
                if not entry['confirmed']:metrics['unknown_sessions']+=1
                else:
                    try:
                        s=validate_receipt(entry['terminal'],n,entry,job,acceptance)
                        for w in s['workers']:
                            accepted=acceptance.workers.get(f'{n.id}/e{entry["epoch"]}/{w["worker_id"]}',{})
                            metrics['discarded_known_primary']+=w['executed_rays']['primary']-accepted.get('accepted_primary',0)
                            metrics['discarded_known_shadow']+=w['executed_rays']['shadow']-accepted.get('accepted_shadow',0)
                    except BaseException as error:errors.append(error)
        if metrics['unknown_sessions']:errors.append(RuntimeError('Retired remote shutdown remains uncertain.'))
        living_threads=transport.close()
        if living_threads:errors.append(RuntimeError('Transport owners did not terminate: '+','.join(living_threads)))
        try:adapter.release(context)
        except BaseException as error:errors.append(error)
        acceptance.finish();cleanup_ns=perf_counter_ns()-cleanup_start
    if errors:state=JobState.FAILED;failure=failure or errors[0]
    if state!=JobState.COMPLETED:result=None
    total=perf_counter_ns()-before
    outcome=JobOutcome(job,state,acceptance.ledger.completed_units,acceptance.ledger.records,
        JobTimings(preparation_ns/1e9,max(0,execution_ns-acceptance.acceptance_ns)/1e9,
            acceptance.acceptance_ns/1e9,finalization_ns/1e9,cleanup_ns/1e9,total/1e9),result,failure,tuple(errors),())
    notify(LifecycleObservation(run_id,state))
    for n in membership.nodes.values():publish(n,True)
    rays=acceptance.accepted_rays
    diagnostics=dict(schema_version=1,kind='drt.dynamic_lan',wire_version=2,run_id=str(run_id),policy=asdict(policy),
        coordinator_render_workers=max((s['reserved'] for n in membership.nodes.values() if n.config.get('owned_local')
            for s in n.sessions),default=0),accepted_samples=outcome.completed_units,accepted_tasks=acceptance.ledger.accepted_tasks,
        accepted_rays=rays,per_worker_acceptance=acceptance.workers,task_events=acceptance.events,
        attempts=attempts.diagnostics(),membership_events=list(membership.events),scheduler=scheduler.stats,
        per_attempt_effort=effort.summary(attempts,acceptance),
        logical_task_states=[dict(ordinal=r.task.ordinal,state=r.state.value) for r in outcome.tasks],
        nodes=[dict(node_id=n.id,state=n.state,epoch=n.epoch,instance=n.instance,sessions=n.sessions) for n in membership.nodes.values()],
        recovery=metrics,logical_coverage_complete=outcome.completed_units==job.expected_units,
        remote_cleanup_confirmed=not errors and metrics['unknown_sessions']==0,
        peaks=dict(reserved_slots=membership.peak_slots,outstanding=acceptance.peak_pending,
            buffered=acceptance.peak_buffer,transport_events=transport.peak_events),
        preview=None if preview is None else preview.diagnostics,service_nanoseconds=total,
        timing_nanoseconds=dict(parent_preparation=preparation_ns,dispatch_readiness_execution_collection=execution_ns,
            original_acceptance=acceptance.acceptance_ns,finalization=finalization_ns,cleanup=cleanup_ns,
            note='Acceptance overlaps dispatch/readiness/execution/collection; do not sum overlapping stages.'),
        error=None if failure is None else codec.failure_data(failure),cleanup_errors=[codec.failure_data(e) for e in errors[:16]],
        physical_lan='DEFERRED / NOT TESTED; same-host loopback verification is separate',
        timing_definition='Contiguous coordinator creation/preparation, TLS/readiness, dispatch, execution, network, retries/backoff/reconnection, original acceptance/finalization and required confirmed shutdown; no recovery subtraction.')
    from drt_backend.pbr_execution.service import PbrExecutionResult
    diagnostics.update(schema_version=2 if _profile.wire_version==3 else 3,
                       kind='drt.pbr_dynamic_lan.v1' if _profile.wire_version==3 else 'drt.room_dynamic_lan.v1',
                       wire_version=_profile.wire_version,
                       is_benchmark_valid=False,workload_digest=adapter.workload.workload_digest)
    return PbrExecutionResult(outcome,diagnostics)
