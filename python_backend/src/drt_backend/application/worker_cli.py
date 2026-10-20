"""Documented standalone deployment helpers. No command implicitly renders."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
from uuid import uuid4


def bundled_scene():
    if not getattr(sys,'frozen',False):
        return Path(__file__).resolve().parents[5]/'assets/PyRenderAssets/shogi_match_room_v3.glb'
    directory=Path(sys.executable).resolve().parent
    # The Windows integrated backend lives one level below the application assets.
    if sys.platform=='win32' and directory.name=='backend':directory=directory.parent
    return directory/'assets/shogi_match_room_v3.glb'


def read_agent_config(path, parser):
    from drt_backend.lan.protocol import strict_json
    allowed={'listen','port','coordinator','coordinator_id','coordinator_fingerprint','node_id','certificate','private_key',
        'trust','scene','resource_root','workers','diagnostics','lifetime_seconds','max_sessions','allow_remote_shutdown','wire_version'}
    try:
        path=path.resolve()
        if not path.is_file() or path.stat().st_size>32768:raise ValueError('Config must be a bounded JSON file.')
        data=strict_json(path.read_bytes())
        if type(data) is not dict or set(data)-allowed:raise ValueError('Unknown agent config fields.')
        for name in ('certificate','private_key','trust','scene','resource_root','diagnostics'):
            if name in data:
                if type(data[name]) is not str or not data[name]:raise ValueError('Config paths must be nonempty strings.')
                data[name]=str((path.parent/data[name]).resolve())
        for name in ('port','workers','lifetime_seconds','max_sessions','wire_version'):
            if name in data and type(data[name]) is not int:raise ValueError(f'{name} must be an integer.')
        if 'allow_remote_shutdown' in data and type(data['allow_remote_shutdown']) is not bool:raise ValueError('allow_remote_shutdown must be boolean.')
        for name in ('listen','coordinator','coordinator_id','coordinator_fingerprint','node_id'):
            if name in data and type(data[name]) is not str:raise ValueError(f'{name} must be a string.')
        return data
    except Exception as e:parser.error('Invalid --config: '+str(e))


def certificate_pin(path):
    text=Path(path).read_text(encoding='ascii')
    if len(text)>65536:raise ValueError('Certificate file exceeds the limit.')
    return sha256(ssl.PEM_cert_to_DER_cert(text)).hexdigest()


def _save(path, value):
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,indent=2);stream.write('\n')


def _system_program_environment():
    """Undo the frozen loader's library override only for external system programs.

    Keep our own process/Worker environment intact. A distro's OpenSSL executable
    must load its matching system libcrypto rather than this bundle's TLS library.
    """
    environment=os.environ.copy()
    if sys.platform=='linux' and getattr(sys,'frozen',False):
        original=environment.get('LD_LIBRARY_PATH_ORIG')
        if original is None:environment.pop('LD_LIBRARY_PATH',None)
        else:environment['LD_LIBRARY_PATH']=original
    return environment


def _openssl(command, *, environment):
    try:
        return subprocess.run(command,check=True,capture_output=True,text=True,encoding='utf-8',errors='replace',
            timeout=30,env=environment)
    except subprocess.CalledProcessError as error:
        detail=(error.stderr or error.stdout or 'No diagnostic output.').strip()[-4096:]
        raise RuntimeError(f'System OpenSSL failed (exit {error.returncode}): {detail}') from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('System OpenSSL did not finish within 30 seconds.') from error


def setup_pairing(args):
    """Create new testing identities at the user's explicit destination, never in a release."""
    import ipaddress
    from drt_backend.lan.protocol import identifier,integer
    identifier(args.node_id);identifier(args.coordinator_id);integer(args.port,1,65535)
    if args.node_id==args.coordinator_id:raise ValueError('Use different node and coordinator identities.')
    for value in (args.node_host,args.coordinator_host):
        if ipaddress.ip_address(value).version!=4:raise ValueError('Use explicit IPv4 addresses.')
    openssl=shutil.which('openssl')
    if openssl is None:raise ValueError('setup-pairing requires the OpenSSL CLI. Install openssl or provision certificates with your PKI. Agent runtime does not require the openssl command.')
    root=args.output.resolve()
    if getattr(sys,'frozen',False) and root.is_relative_to(Path(sys.executable).resolve().parent):
        raise ValueError('Pairing identities must be outside the immutable release; use $HOME/drt-pairing.')
    if root.exists() or root.is_symlink():raise FileExistsError('Choose a new pairing output directory; existing data is never overwritten.')
    environment=_system_program_environment()
    # Check loader compatibility before creating the user's identity directories.
    _openssl([openssl,'version'],environment=environment)
    root.mkdir(mode=0o700)
    for role,identity in (('node',args.node_id),('coordinator',args.coordinator_id)):
        folder=root/role;folder.mkdir(mode=0o700)
        try:_openssl([openssl,'req','-x509','-newkey','rsa:2048','-sha256','-nodes','-days','30',
            '-keyout',str(folder/(role+'.key')),'-out',str(folder/(role+'.crt')),'-subj','/CN='+identity,
            '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign',
            '-addext','extendedKeyUsage=serverAuth,clientAuth'],environment=environment)
        except RuntimeError as error:
            # Never recursively remove a failed pairing tree: a partial key may exist.
            raise RuntimeError(f'{error}\nPairing incomplete at {root}. Do not import it; choose a new --output directory after resolving the error.') from error
        (folder/(role+'.key')).chmod(0o600)
    shutil.copyfile(root/'node/node.crt',root/'coordinator/node.crt')
    shutil.copyfile(root/'coordinator/coordinator.crt',root/'node/coordinator.crt')
    _save(root/'node/agent.json',dict(listen='127.0.0.1' if args.node_host=='127.0.0.1' else '0.0.0.0',port=args.port,
        coordinator=args.coordinator_host,coordinator_id=args.coordinator_id,coordinator_fingerprint=certificate_pin(root/'coordinator/coordinator.crt'),
        node_id=args.node_id,certificate='node.crt',private_key='node.key',trust='coordinator.crt',diagnostics='logs',wire_version=4))
    _save(root/'coordinator/topology.template.json',dict(wire_version=4,coordinator_id=args.coordinator_id,
        certificate='coordinator.crt',private_key='coordinator.key',global_workers='auto',agents=[dict(
            node_id=args.node_id,host=args.node_host,port=args.port,workers='auto',trust='node.crt',fingerprint=certificate_pin(root/'node/node.crt'))]))
    (root/'coordinator/Configure-Coordinator.ps1').write_text('''$ErrorActionPreference = 'Stop'
$template = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'topology.template.json') -Raw | ConvertFrom-Json
$template.certificate = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $template.certificate))
$template.private_key = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $template.private_key))
foreach ($agent in $template.agents) { $agent.trust = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $agent.trust)) }
$target = Join-Path $PSScriptRoot 'trusted.json'
$bytes = [Text.UTF8Encoding]::new($false).GetBytes(($template | ConvertTo-Json -Depth 8))
$file = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $file.Write($bytes, 0, $bytes.Length) } finally { $file.Dispose() }
Write-Host "Import this topology in Remote: $target"
''',encoding='utf-8')
    print(json.dumps(dict(status='created',directory=str(root),node_config=str(root/'node/agent.json'),
        coordinator_template=str(root/'coordinator/topology.template.json'),private_keys_shipped_in_release=False,
        next='Keep node/ on the Agent. Transfer coordinator/ securely to the desktop, run Configure-Coordinator.ps1 there, and import trusted.json. Test certificates expire in 30 days.')))
    return 0


def configure_coordinator(args):
    from drt_backend.lan.protocol import strict_json
    from drt_backend.room_pbr.topology import load_agents
    template=args.template.resolve();data=strict_json(template.read_bytes())
    for name in ('certificate','private_key'):data[name]=str((template.parent/data[name]).resolve())
    for peer in data['agents']:peer['trust']=str((template.parent/peer['trust']).resolve())
    output=args.output.resolve() if args.output else template.with_name('trusted.json')
    # Validate before committing the user-visible output. Never overwrite a prior trusted topology.
    temporary=output.with_name('.'+output.name+'.'+uuid4().hex)
    try:
        _save(temporary,data);load_agents(temporary)
        with output.open('x',encoding='utf-8') as stream:stream.write(temporary.read_text())
    finally:temporary.unlink(missing_ok=True)
    print(json.dumps(dict(status='configured',topology=str(output))));return 0


def probe(args):
    """Real mutual TLS + capability + idle heartbeat + orderly close, zero numerical Workers."""
    import time
    from drt_backend.room_pbr.topology import load_agents
    from drt_backend.room_pbr import codec,protocol
    from drt_backend.room_pbr.policy import make_workload,RAW
    from drt_backend.room_pbr.scene import RoomSource
    from drt_backend.room_pbr.adapter import RoomAdapter
    config=load_agents(args.agents.resolve());scene=args.scene.resolve();w=make_workload()
    source=RoomSource.load(scene,w);rows=[]
    for peer in config['agents']:
        channel=None
        try:
            job=RoomAdapter(source,w,uuid4(),32).job
            channel,hello=protocol.connect(config,peer,job.run_id,1,job)
            channel.send('monitor',dict(schema='drt.room_idle_monitor.v1',file_sha256=RAW,job=codec.job_data(job)))
            state=channel.expect('monitor_state')
            codec.require(state['node']==peer['node_id'] and state['numerical_workers']==0
                and state['job_identity']==job.job_identity and state['resource_identity']==job.resource_identity,'Foreign monitor state.')
            binding=dict(schema='drt.room_idle_monitor.v1',run_id=str(job.run_id),epoch=1)
            for _ in range(args.heartbeats):
                channel.send('heartbeat',binding);codec.require(channel.expect('heartbeat')==binding,'Foreign heartbeat.');time.sleep(.1)
            channel.send('bye',{});codec.require(channel.expect('bye')=={},'Monitor close differs.')
            rows.append(dict(node=peer['node_id'],status='connected',authenticated=True,compatible=True,
                available_workers=hello['workers'],agent_pid=state['pid'],heartbeats=args.heartbeats,numerical_workers=0))
        finally:
            if channel:channel.close()
    print(json.dumps(dict(status='PASS',nodes=rows,numerical_workers=0,numerical_rays=0)));return 0


def add_commands(commands):
    commands.add_parser('agent',help='Start a Linux/Windows TLS Worker listener; use agent --help',add_help=False)
    setup=commands.add_parser('setup-pairing',help='Create fresh 30-day test identities and separate node/coordinator folders (requires openssl)')
    setup.add_argument('--output',type=Path,required=True,help='New private directory outside the release; parent must exist')
    setup.add_argument('--node-host',required=True,help='Node IPv4 reachable from the coordinator (127.0.0.1 for same-OS loopback)')
    setup.add_argument('--coordinator-host',required=True,help='Coordinator source IPv4 as observed by the node; WSL NAT may differ from localhost')
    setup.add_argument('--node-id',default='node-a',help='Unique node ID (default: node-a)')
    setup.add_argument('--coordinator-id',default='coordinator',help='Coordinator ID (default: coordinator)')
    setup.add_argument('--port',type=int,default=45831,help='Agent TCP port (default: 45831)')
    setup.set_defaults(worker_action=setup_pairing)
    configure=commands.add_parser('configure-coordinator',help='Resolve a copied topology template to local absolute paths; never overwrites output')
    configure.add_argument('--template',type=Path,required=True,help='Copied coordinator/topology.template.json')
    configure.add_argument('--output',type=Path,help='New trusted topology (default: trusted.json beside template)')
    configure.set_defaults(worker_action=configure_coordinator)
    pin=commands.add_parser('fingerprint',help='Print the DER SHA256 pin of a public PEM certificate')
    pin.add_argument('--certificate',type=Path,required=True,help='Public PEM certificate; never pass a private key')
    pin.set_defaults(worker_action=lambda a:print(certificate_pin(a.certificate)) or 0)
    check=commands.add_parser('probe',help='Check TLS, room identity and idle heartbeats, then disconnect; zero numerical Workers/rays')
    check.add_argument('--agents',type=Path,required=True,help='Local trusted.json with local absolute credential paths')
    check.add_argument('--scene',type=Path,default=bundled_scene(),help='Original room GLB (default: bundled scene)')
    check.add_argument('--heartbeats',type=int,choices=range(1,6),default=2,help='Bounded health exchanges, 1..5 (default: 2)')
    check.set_defaults(worker_action=probe)
