"""Owned local participant for the existing single distributed acceptance ledger.

Private socketpair only: no listener, credentials or network trust bypass. Numerical
children inherit the coordinator's process ownership and use the existing Agent
session/Worker lifecycle and byte protocol. Remote Agents are unchanged.
"""
from collections import deque
import socket
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4
from . import codec,protocol
from .admission import LOCAL_NODE_ID


def connect(profile,config,run_id,epoch):
    from drt_backend.lan.dynamic_agent import session
    from .profile import Profile
    left,right=socket.socketpair()
    server=protocol.Channel(right)
    args=SimpleNamespace(node_id=LOCAL_NODE_ID,coordinator_id=config['coordinator_id'],workers=profile.submission.options.workers,
        instance_id=str(uuid4()),receipts=deque(maxlen=16),session_ids=set(),run_epochs={},
        scene=profile.submission.source_path,resource_root=profile.submission.source_path.parent,
        diagnostics=None,allow_remote_shutdown=False)
    def serve():
        try:session(server,args,_profile=Profile())
        except BaseException:pass # The peer receives EOF; the coordinator remains the sole failure authority.
        finally:server.close()
    thread=Thread(target=serve,name='room-owned-local-session',daemon=True)
    profile.local_sessions.append(thread);thread.start()
    client=protocol.Channel(left,str(uuid4()));job=profile.submission.definition;w,_=codec.workload_from_job(job)
    try:
        client.send('hello',dict(coordinator=config['coordinator_id'],node=LOCAL_NODE_ID,run_id=str(run_id),epoch=epoch,
            job_identity=job.job_identity,capability=codec.capability(w)))
        hello=protocol.exact(client.expect('hello'),'node coordinator workers instance wire_version job_identity capability')
        codec.require(hello['node']==LOCAL_NODE_ID and hello['coordinator']==config['coordinator_id']
            and hello['wire_version']==4 and hello['job_identity']==job.job_identity,'Owned local session identity differs.')
        codec.match_capability(hello['capability'],w);codec.integer(hello['workers'],1,64)
        return client,hello
    except BaseException:client.close();raise
