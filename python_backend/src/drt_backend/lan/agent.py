"""Independent Windows/Linux Python Agent. Provision identities before listening."""
import argparse
from collections import deque
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import socket
import signal
import sys
import ipaddress
from threading import Thread, Event
from time import monotonic, perf_counter_ns
from uuid import uuid4
from drt_scheduler import CancellationToken, CancellationRequested
from drt_backend.local_parallel import codec
from .protocol import Channel, exact, identifier, integer, require, tls_context, verify_peer


def summary(shutdown):
    return dict(forced=shutdown['forced'],remaining_owned_pids=shutdown['remaining_owned_pids'],
        cancellation_confirmed=shutdown['cancellation_confirmed'],errors=shutdown['errors'][:3],
        shutdown_nanoseconds=shutdown['shutdown_nanoseconds'],workers=[dict(worker_id=w['worker_id'],pid=w['pid'],
            exit_code=w['exit_code'],forced=w['forced'],stopped=w['stopped'] is not None,
            executed_tasks=0 if w['stopped'] is None else w['stopped']['executed_tasks'],
            executed_rays=None if w['stopped'] is None else w['stopped']['reported_executed_rays']) for w in shutdown['workers']])


def main(argv=None):
    from drt_backend.application.worker_cli import bundled_scene, read_agent_config
    argv=list(sys.argv[1:] if argv is None else argv)
    pre=argparse.ArgumentParser(add_help=False);pre.add_argument('--config',type=Path)
    preliminary,_=pre.parse_known_args(argv)
    defaults={} if preliminary.config is None or '--help' in argv or '-h' in argv else read_agent_config(preliminary.config,pre)
    parser=argparse.ArgumentParser(description='Independent mutually authenticated CPU PBR Agent for Linux/Windows (room wire 4).',
        formatter_class=argparse.RawDescriptionHelpFormatter,epilog='''Quick start:
  drt-agent setup-pairing --output ./pairing --node-host 127.0.0.1 --coordinator-host 127.0.0.1
  drt-agent agent --config ./pairing/node/agent.json

Keep node private keys on the node. Transfer only the coordinator/ role folder
to the trusted desktop. Run its Configure-Coordinator.ps1, import trusted.json
in Remote, select the node and choose Discover / connect selected.
Linux package: ./drt-agent agent --config "$HOME/drt-node/agent.json"
Relative config paths resolve beside agent.json; relative CLI paths use cwd.
An idle authenticated connection creates no numerical Workers and renders no rays.
Ctrl+C / SIGTERM stops the Agent; active sessions clean up their owned Workers.
See README.md and docs/USER_GUIDE.md for loopback, LAN, WSL and certificate setup.''')
    parser.add_argument('--config',type=Path,help='JSON startup configuration; explicit flags override its values')
    parser.add_argument('--listen',default='127.0.0.1',help='Local IPv4 bind address (default: loopback); use the node LAN IP or 0.0.0.0 for LAN')
    parser.add_argument('--port',type=int,required='port' not in defaults,help='TCP listener port, 1..65535 (example: 45831)')
    parser.add_argument('--coordinator',required='coordinator' not in defaults,help='Only this source IPv4 address may connect; use the source as seen by the node')
    parser.add_argument('--coordinator-id',required='coordinator_id' not in defaults,help='Trusted coordinator ID; must match the imported topology')
    parser.add_argument('--coordinator-fingerprint',required='coordinator_fingerprint' not in defaults,help='64 lowercase hex SHA256 of the coordinator DER certificate, not PEM text')
    parser.add_argument('--node-id',required='node_id' not in defaults,help='Unique node ID: 1..64 ASCII letters/digits/underscore/dot/hyphen')
    parser.add_argument('--certificate',type=Path,required='certificate' not in defaults,help='This node public PEM certificate')
    parser.add_argument('--private-key',type=Path,required='private_key' not in defaults,help='This node PEM private key; keep private (Linux chmod 600)')
    parser.add_argument('--trust',type=Path,required='trust' not in defaults,help='Trusted coordinator certificate or issuing CA, PEM')
    parser.add_argument('--scene',type=Path,default=bundled_scene(),help='Provisioned original room GLB (default: this package assets/shogi_match_room_v3.glb)')
    parser.add_argument('--workers',type=int,metavar='1..64',help='Administrative ceiling; default all available logical CPUs, bounded by memory, affinity and 64')
    parser.add_argument('--diagnostics',type=Path,help='Writable session receipt directory outside the release (optional)')
    parser.add_argument('--lifetime-seconds',type=int,default=3600,help='Listener lifetime, 1..86400 seconds (default: 3600); not an active-session timeout')
    parser.add_argument('--max-sessions',type=int,default=64,help='Accepted connection limit, 1..256 (default: 64); probes/monitor connections also count')
    parser.add_argument('--allow-remote-shutdown',action=argparse.BooleanOptionalAction,default=False,help='Allow the pinned Coordinator to stop this idle Agent (default: disabled)')
    parser.add_argument('--wire-version',type=int,choices=(4,) if getattr(sys,'frozen',False) or sys.platform=='linux' else (3,4),default=4,help='Room PBR protocol version (default: 4)')
    parser.add_argument('--resource-root',type=Path,help='Provisioned asset directory (room default: parent of --scene)')
    parser.set_defaults(**defaults)
    args=parser.parse_args(argv)
    if type(args.wire_version) is not int or args.wire_version not in (3,4):parser.error('Supported --wire-version values are 3 and 4.')
    if getattr(sys,'frozen',False) and args.wire_version!=4:parser.error('Packaged Agent requires room wire 4.')
    if args.workers is None:args.workers=64 if args.wire_version==4 else 2
    if os.name!='nt' and sys.platform!='linux':parser.error('Agent supports Windows and Linux only.')
    if sys.platform=='linux' and args.wire_version!=4:parser.error('Linux Worker requires current room --wire-version 4.')
    try:
        integer(args.port,1,65535);integer(args.lifetime_seconds,1,86400);integer(args.max_sessions,1,256)
        identifier(args.node_id);identifier(args.coordinator_id)
        require(ipaddress.ip_address(args.listen).version==4 and ipaddress.ip_address(args.coordinator).version==4,'Use explicit IPv4 addresses.')
        from .protocol import fingerprint
        fingerprint(args.coordinator_fingerprint)
        for name in ('certificate','private_key','trust','scene','resource_root','diagnostics'):
            value=getattr(args,name)
            if value is not None:setattr(args,name,Path(value).resolve())
        if args.wire_version==4 and args.resource_root is None:args.resource_root=args.scene.parent
        integer(args.workers,1,64 if args.wire_version==4 else 2)
        for name in ('certificate','private_key','trust'):
            require(getattr(args,name).is_file(),f'Provision a readable --{name.replace("_","-")} file.')
        if sys.platform=='linux':require(args.private_key.stat().st_mode&0o077==0,'Private key permissions must be owner-only: chmod 600 <key>.')
    except (ValueError,OSError,codec.LocalProtocolError) as e:parser.error(str(e))
    args.instance_id=str(uuid4());args.receipts=deque(maxlen=16)
    args.session_ids=set();args.run_epochs={}
    try:
        require(args.scene.is_absolute() and args.scene.is_file(),'Provision a local GLB before starting the Agent.')
        if args.wire_version==3:
            from drt_backend.pbr_execution.service import provisioned
            require(args.resource_root is not None,'PBR requires an explicit fixture allowlist directory.')
            provisioned(args.scene,args.resource_root);integer(args.workers,1,2)
        elif args.wire_version==4:
            from drt_backend.room_pbr.service import provisioned
            provisioned(args.scene,args.resource_root);integer(args.workers,1,64)
        context=tls_context(args.certificate,args.private_key,args.trust,server=True)
        context.set_alpn_protocols([f'drt-worker/{args.wire_version}'])
    except Exception as e:parser.error(str(e))
    stopping=Event();previous={}
    def stop(signum,frame):
        stopping.set();raise KeyboardInterrupt
    try:
        for sig in (signal.SIGINT,signal.SIGTERM):previous[sig]=signal.signal(sig,stop)
        return _serve(args,context,stopping)
    except KeyboardInterrupt:
        print(json.dumps(dict(status='stopped',node=args.node_id,reason='signal')),flush=True);return 0
    except OSError as e:
        print('AGENT_START_ERROR: '+str(e),file=sys.stderr);return 2
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)


def _serve(args,context,stopping):
    end=monotonic()+args.lifetime_seconds
    with socket.socket() as server:
        server.bind((args.listen,args.port));server.listen(4);server.settimeout(.2)
        print(json.dumps(dict(status='listening',node=args.node_id,port=args.port,pid=os.getpid(),wireVersion=args.wire_version)),flush=True)
        count=0
        while not stopping.is_set() and count<args.max_sessions and monotonic()<end:
            try:stream,address=server.accept()
            except socket.timeout:continue
            count+=1;channel=None
            try:
                require(address[0]==args.coordinator,'Unauthorized Coordinator address.')
                stream.settimeout(5);stream=context.wrap_socket(stream,server_side=True)
                verify_peer(stream,args.coordinator_fingerprint)
                if args.wire_version==4:
                    from drt_backend.room_pbr.protocol import Channel as RoomChannel
                    from drt_backend.room_pbr.profile import Profile
                    from .dynamic_agent import session as dynamic_session
                    require(stream.selected_alpn_protocol()=='drt-worker/4','Required room ALPN was not negotiated.')
                    channel=RoomChannel(stream)
                    record=dynamic_session(channel,args,_profile=Profile())
                elif args.wire_version==3:
                    from drt_backend.pbr_execution.protocol import Channel as PbrChannel
                    from drt_backend.pbr_execution.profile import Profile
                    from .dynamic_agent import session as dynamic_session
                    require(stream.selected_alpn_protocol()=='drt-worker/3','Required PBR ALPN was not negotiated.')
                    channel=PbrChannel(stream)
                    record=dynamic_session(channel,args,_profile=Profile())
                if record=='shutdown':return 0
            except Exception as e:
                # Deliberately omit peer-controlled text and all key material.
                print(json.dumps(dict(status='rejected',error=type(e).__name__,phase='tls-authentication' if channel is None else channel.phase,
                    session=None if channel is None else channel.session,wire_version=args.wire_version,
                    expected_kind=None if channel is None else channel.expected_kind)),flush=True)
            finally:
                if channel:channel.close()
                else:stream.close()
    return 0

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
