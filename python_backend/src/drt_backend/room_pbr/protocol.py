"""Explicit authenticated room wire 4 / drt-worker/4, without downgrade."""
import socket
import ssl
from uuid import uuid4
from drt_backend.lan.dynamic_protocol import Channel as DynamicChannel,AuthenticationFailure,uuid
from drt_backend.lan.protocol import tls_context,verify_peer,exact,require,integer,fingerprint
from drt_backend.pbr_execution.codec import read_workload
from . import codec
from .admission import select,LOCAL_WORKER_LIMIT


class Channel(DynamicChannel):
    version=4
    kinds=DynamicChannel.kinds|{'monitor','monitor_state'}


def connect(config,agent,run_id,epoch,job):
    require(config['wire_version']==4,'Room topology version 4 required.')
    stream=socket.create_connection((agent['host'],agent['port']),timeout=3);channel=None
    try:
        context=tls_context(config['certificate'],config['private_key'],agent['trust'])
        context.set_alpn_protocols(['drt-worker/4'])
        try:
            stream=context.wrap_socket(stream,server_hostname=agent['host'])
            verify_peer(stream,agent['fingerprint'])
        except (ssl.SSLError,ValueError,codec.LocalProtocolError) as e:
            raise AuthenticationFailure('Room mutual TLS/pin authentication failed.') from e
        require(stream.selected_alpn_protocol()=='drt-worker/4','Room ALPN required.')
        channel=Channel(stream,str(uuid4()));w,_=codec.workload_from_job(job)
        channel.send('hello',dict(coordinator=config['coordinator_id'],node=agent['node_id'],run_id=str(run_id),
            epoch=epoch,job_identity=job.job_identity,capability=codec.capability(w)))
        hello=exact(channel.expect('hello'),'node coordinator workers instance wire_version job_identity capability')
        require(hello['node']==agent['node_id'] and hello['coordinator']==config['coordinator_id']
            and type(hello['wire_version']) is int and hello['wire_version']==4
            and hello['job_identity']==job.job_identity,'Room authenticated identity/job/version differs.')
        codec.match_capability(hello['capability'],w);integer(hello['workers'],1,LOCAL_WORKER_LIMIT);uuid(hello['instance'])
        return channel,hello
    except BaseException:
        if channel:channel.close()
        else:stream.close()
        raise


def accept_hello(channel,args):
    hello=exact(channel.expect('hello'),'coordinator node run_id epoch job_identity capability')
    require(hello['coordinator']==args.coordinator_id and hello['node']==args.node_id,'Unauthorized room identities.')
    uuid(hello['run_id']);integer(hello['epoch'],1,8);fingerprint(hello['job_identity'])
    require(type(hello['capability']) is dict,'Room capability object required.')
    w=read_workload(hello['capability'].get('workload'));codec.match_capability(hello['capability'],w)
    require(channel.session not in args.session_ids and hello['epoch']>args.run_epochs.get(hello['run_id'],0)
        and len(args.session_ids)<256 and (hello['run_id'] in args.run_epochs or len(args.run_epochs)<256),
        'Replayed/excess room session/run epoch.')
    args.session_ids.add(channel.session);args.run_epochs[hello['run_id']]=hello['epoch'];args.room_hello=hello
    available=select((w.pixel_count+31)//32,args.workers)['workers']
    channel.send('hello',dict(node=args.node_id,coordinator=args.coordinator_id,workers=available,
        instance=args.instance_id,wire_version=4,job_identity=hello['job_identity'],capability=codec.capability(w)))
    return hello,available
