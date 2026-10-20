"""Worker v2 session execution. The standalone Agent remains the TLS listener.

No retries or final acceptance live here. On abandoned sessions the independent
watchdog cancels its owned pool; a later authenticated session can retrieve the
actual compact cleanup receipt, never a fabricated shutdown acknowledgement.
"""
from collections import deque
from hashlib import sha256
import json
import os
import queue
from threading import Thread,Event
from time import monotonic,perf_counter_ns
from drt_scheduler import CancellationToken,CancellationRequested
from drt_backend.application.worker_policy import select_workers
from drt_backend.local_parallel import codec
from drt_backend.local_parallel.dispatcher import SpawnDispatcher,ParallelOptions
from .agent import summary
from .dynamic_protocol import binding,uuid
from .protocol import exact,require,integer
from .worker_supervisor import SupervisedWorker


def session(channel,args,*,worker_target=None,_profile=None):
    require(_profile is not None and _profile.wire_version in (3,4),'Explicit PBR execution profile required.')
    codec=_profile.codec
    verify=lambda scene,body:_profile.verify_resource(scene,body,args)
    hello,available=_profile.hello(channel,args)
    return _execute_session(channel,args,hello,available,verify,codec,worker_target or _profile.worker_target,_profile)


def _execute_session(channel,args,hello,available,verify,codec,worker_target,_profile):
    run_id,epoch=hello['run_id'],hello['epoch']
    wire_version=_profile.wire_version
    recoveries=0
    while True:
        kind,body=channel.receive(5)
        if kind!='recover':break
        exact(body,'session run_id epoch');uuid(body['session']);uuid(body['run_id']);integer(body['epoch'],1,8)
        recoveries+=1;require(recoveries<=8,'Recovery request bound exceeded.')
        receipt=next((r for r in args.receipts if (r['session'],r['run_id'],r['epoch'])==
                     (body['session'],body['run_id'],body['epoch'])),None)
        channel.send('recovered',dict(session=body['session'],receipt=receipt))
    if kind=='shutdown':
        require(args.allow_remote_shutdown and body=={},'Remote Agent shutdown is not enabled.')
        channel.send('bye',{});return 'shutdown'
    if kind=='bye':
        require(body=={},'Invalid idle reconciliation close.');channel.send('bye',{});return None
    if kind=='monitor' and hasattr(_profile,'monitor'):
        return _profile.monitor(channel,args,hello,body)
    require(kind in ('probe','initialize'),'Dynamic readiness required.')
    if kind=='probe':
        submission=verify(args.scene,body)
        channel.send('capability',dict(node=args.node_id,file_sha256=body['file_sha256'],
            resource_identity=submission.definition.resource_identity,configuration_identity=submission.definition.configuration_identity,
            available_workers=available,pid=os.getpid(),wire_version=wire_version))
        require(channel.expect('bye')=={},'Invalid probe close.');channel.send('bye',{});return None
    exact(body,'file_sha256 job workers epoch')
    integer(body['epoch'],1,8)
    require(body['epoch']==epoch and body['job']['run_id']==run_id,'Foreign initialization epoch/run.')
    submission=verify(args.scene,{k:body[k] for k in ('file_sha256','job')})
    count=sum(1 for _ in submission.adapter.enumerate_tasks(submission.definition))
    if hasattr(_profile,'worker_allocation'):
        allocation=_profile.worker_allocation(count,args.workers)
    else:
        allocation=dict(selected_workers=min(2,args.workers,count),policy='explicit PBR profile')
        from drt_backend.pbr_execution.admission import admit
        allocation.update(admit(allocation['selected_workers']))
    require(body['workers']<=available,'Requested Worker count exceeds the authenticated advertisement.')
    integer(body['workers'],1,allocation['selected_workers'])
    from drt_backend.ipc.host import configure_source_workers
    configure_source_workers()
    options=(_profile.worker_options(body['workers']) if hasattr(_profile,'worker_options') else
             ParallelOptions(workers=body['workers'],capacity_limit=2,task_seconds=15,run_seconds=45))
    pool=SpawnDispatcher(codec.initialization(args.scene,body['file_sha256'],submission.definition),submission.definition,
        options,_worker_target=SupervisedWorker(worker_target,getattr(_profile,'worker_supervision_seconds',85)),
            _codec=codec)
    token=CancellationToken();stopping=Event();commands=queue.Queue(maxsize=128)
    reader_errors=[];reader=None;failure=None;terminal='failed';shutdown=None
    deadline=perf_counter_ns()+int(options.run_seconds*1e9);last_heartbeat=monotonic()
    assigned={};attempt_ids=set();task_generations={};draining=False
    def controls():
        try:
            channel.expected_kind='heartbeat|assign|finish|drain|cancel'
            while not stopping.is_set():
                k,b=channel.receive(5)
                if k=='heartbeat':require(b==dict(run_id=run_id,epoch=epoch),'Foreign heartbeat.')
                elif k=='cancel':
                    require(b==dict(run_id=run_id,epoch=epoch),'Foreign cancellation.');token.request();return
                else:
                    require(k in ('assign','finish','drain'),'Unexpected dynamic control.')
                    commands.put_nowait((k,b))
        except BaseException as error:
            if not stopping.is_set():reader_errors.append(error);token.request()
    try:
        channel.phase='worker-startup'
        pool.start(token,deadline)
        channel.send('ready',dict(node=args.node_id,run_id=run_id,epoch=epoch,instance=args.instance_id,
            file_sha256=body['file_sha256'],job_identity=submission.definition.job_identity,
            configuration_identity=submission.definition.configuration_identity,resource_identity=submission.definition.resource_identity,
            workers=[dict(worker_id=h['worker_id'],pid=h['pid']) for h in pool.handles]))
        channel.phase='executing'
        reader=Thread(target=controls,name='v2-authenticated-controls',daemon=True);reader.start()
        while True:
            token.checkpoint()
            if perf_counter_ns()>=deadline:raise TimeoutError('Agent absolute run watchdog expired.')
            try:k,b=commands.get(timeout=.005)
            except queue.Empty:k=None
            if k in ('finish','drain'):
                require(b==dict(run_id=run_id,epoch=epoch),'Foreign stop epoch/run.')
                draining=True
                if k=='finish':
                    require(not assigned,'Finish before assigned tasks returned.');terminal='completed';break
            if k=='assign':
                require(not draining,'Assignment after drain.')
                exact(b,'attempt task lease_ms');a=binding(b['attempt']);integer(b['lease_ms'],50,30000)
                task=codec.read_task(dict(version=codec.VERSION,kind='task',task=b['task']),submission.definition)
                require(a['node']==args.node_id and a['session']==channel.session and a['epoch']==epoch
                    and a['run_id']==run_id and a['task_id']==task.task_id and a['ordinal']==task.ordinal
                    and a['job_identity']==task.job.job_identity and a['configuration_identity']==task.job.configuration_identity
                    and a['resource_identity']==task.job.resource_identity,'Assignment authority differs.')
                require(a['attempt_id'] not in attempt_ids and a['generation']>task_generations.get(task.task_id,0)
                    and len(attempt_ids)<20480,'Duplicate/excess attempt registry.')
                handle=next((h for h in pool.idle_workers if h['worker_id']==a['worker_id']),None)
                require(handle is not None,'Exclusive Worker slot unavailable.')
                attempt_ids.add(a['attempt_id']);task_generations[task.task_id]=a['generation']
                assigned[a['worker_id']]=a;pool.submit(handle,task)
            if any(h['connection'].poll() for h in pool.handles):
                handle,message=pool.receive(token,deadline)
                a=assigned.pop(handle['worker_id'])
                channel.send('result',dict(attempt=a,packet=message))
                if message['status']!='completed':
                    failure=codec.read_failure(message['error']);terminal='failed';break
            now=perf_counter_ns()
            for h in pool.handles:
                if not h['process'].is_alive():
                    raise codec.WorkerFailure('WorkerCrashed','Agent Worker exited without a pending result.')
                if h['busy'] is not None and now-h['submitted_ns']>=30e9:
                    raise codec.WorkerFailure('WorkerTaskTimeout','Independent Agent task watchdog expired.')
            if draining and not assigned:terminal='drained';break
            if monotonic()-last_heartbeat>=1:
                channel.send('heartbeat',dict(run_id=run_id,epoch=epoch));last_heartbeat=monotonic()
    except CancellationRequested:
        failure=reader_errors[0] if reader_errors else None
        terminal='failed' if failure else 'cancelled'
    except BaseException as error:failure=error
    finally:
        stopping.set();channel.phase='worker-cleanup'
        shutdown=pool.close(abort=terminal not in ('completed','drained'))
        if (shutdown['forced'] or shutdown['remaining_owned_pids'] or shutdown['errors']
                or any(w['stopped'] is None or w['exit_code']!=0 for w in shutdown['workers'])):
            terminal='failed';failure=RuntimeError('Agent forced or unverified Worker cleanup.')
        if terminal=='cancelled' and not shutdown['cancellation_confirmed']:
            terminal='failed';failure=RuntimeError('Agent cancellation unconfirmed.')
        response=dict(node=args.node_id,run_id=run_id,epoch=epoch,instance=args.instance_id,session=channel.session,
            state=terminal,shutdown=summary(shutdown),error=None if failure is None else codec.failure_data(failure))
        args.receipts.append(response)
        if args.diagnostics:
            args.diagnostics.mkdir(parents=True,exist_ok=True)
            with (args.diagnostics/f'{channel.session}.json').open('x',encoding='utf-8') as stream:
                json.dump(dict(wire_version=wire_version,pid=os.getpid(),receipt=response,allocation=allocation,
                    shutdown=shutdown,phase=channel.phase,expected_kind=channel.expected_kind,
                    read_deadline=channel.read_deadline,write_deadline_seconds=3),stream,indent=2)
        try:channel.send('terminal',response)
        except (EOFError,OSError):pass  # Actual receipt survives for authenticated reconciliation.
        finally:
            channel.close()
            if reader:reader.join(timeout=1)
    return response
