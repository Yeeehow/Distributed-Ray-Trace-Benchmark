"""Worker wire 3 / drt-worker/3: authenticated PBR capability, no downgrade."""
import socket
import ssl
from uuid import uuid4
from drt_backend.lan.dynamic_protocol import Channel as DynamicChannel, AuthenticationFailure, uuid
from drt_backend.lan.protocol import tls_context, verify_peer, exact, require, integer
from . import codec


class Channel(DynamicChannel):
    version = 3


def connect(config, agent, run_id, epoch, job):
    stream = socket.create_connection((agent['host'], agent['port']), timeout=3)
    channel = None
    try:
        context = tls_context(config['certificate'], config['private_key'], agent['trust'])
        context.set_alpn_protocols(['drt-worker/3'])
        try:
            stream = context.wrap_socket(stream, server_hostname=agent['host'])
            verify_peer(stream, agent['fingerprint'])
        except (ssl.SSLError, ValueError, codec.LocalProtocolError) as e:
            raise AuthenticationFailure('PBR mutual TLS/pin authentication failed.') from e
        require(stream.selected_alpn_protocol() == 'drt-worker/3', 'PBR ALPN required; no Legacy downgrade.')
        channel = Channel(stream, str(uuid4()))
        w, _ = codec.workload_from_job(job)
        channel.send('hello', dict(coordinator=config['coordinator_id'], node=agent['node_id'], run_id=str(run_id),
            epoch=epoch, job_identity=job.job_identity, capability=codec.capability(w)))
        hello = exact(channel.expect('hello'), 'node coordinator workers instance wire_version job_identity capability')
        require(hello['node'] == agent['node_id'] and hello['coordinator'] == config['coordinator_id']
                and type(hello['wire_version']) is int and hello['wire_version'] == 3
                and hello['job_identity'] == job.job_identity, 'PBR authenticated identity/job/version differs.')
        codec.match_capability(hello['capability'], w)
        integer(hello['workers'], 1, 2); uuid(hello['instance'])
        return channel, hello
    except BaseException:
        if channel:
            channel.close()
        else:
            stream.close()
        raise


def accept_hello(channel, args):
    hello = exact(channel.expect('hello'), 'coordinator node run_id epoch job_identity capability')
    require(hello['coordinator'] == args.coordinator_id and hello['node'] == args.node_id,
            'Unauthorized PBR Coordinator/Node identity.')
    uuid(hello['run_id']); integer(hello['epoch'], 1, 8)
    w = codec.read_workload(hello['capability'].get('workload'))
    codec.match_capability(hello['capability'], w)
    from drt_backend.lan.protocol import fingerprint
    fingerprint(hello['job_identity'])
    require(channel.session not in args.session_ids and hello['epoch'] > args.run_epochs.get(hello['run_id'], 0)
            and len(args.session_ids) < 256 and (hello['run_id'] in args.run_epochs or len(args.run_epochs) < 256),
            'Replayed/excess PBR session/run epoch.')
    args.session_ids.add(channel.session); args.run_epochs[hello['run_id']] = hello['epoch']
    args.pbr_hello = hello
    integer(args.workers, 1, 2)
    channel.send('hello', dict(node=args.node_id, coordinator=args.coordinator_id, workers=args.workers,
        instance=args.instance_id, wire_version=3, job_identity=hello['job_identity'], capability=codec.capability(w)))
    return hello, args.workers
