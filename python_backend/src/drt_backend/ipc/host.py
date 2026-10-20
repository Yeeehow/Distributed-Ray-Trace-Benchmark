"""Bounded PBR-only IPC process/control owner; explicit frontend v4/v5."""
import argparse
import json
import os
from pathlib import Path
import queue
import sys
import threading
from drt_scheduler import CancellationToken

VERSION=5
SCENE='shogi-match-room-v3'
MAX_COMMAND=8192
MAX_EVENT=262144


def configure_source_workers():
    if os.name=='nt':
        import multiprocessing
        worker_python=(Path(sys.executable).with_name('drt-worker.exe') if getattr(sys,'frozen',False)
                       else Path(sys._base_executable).with_name('pythonw.exe'))
        if not worker_python.is_file():raise RuntimeError('Matching windowless Worker executable required.')
        multiprocessing.set_executable(str(worker_python))


def strict_json(data):
    def pairs(items):
        value={}
        for k,v in items:
            if k in value:raise ValueError('Duplicate JSON property.')
            value[k]=v
        return value
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON.')))


class Host:
    def __init__(self,scene,output,*,pbr_scene=None,pbr_root=None,pbr_agents=None,room_scene=None,room_root=None,room_agents=None):
        self.scene,self.output=scene,output
        self.pbr_scene,self.pbr_root,self.pbr_agents=pbr_scene,pbr_root,pbr_agents
        self.room_scene,self.room_root,self.room_agents=room_scene,room_root,room_agents
        self.version=VERSION;self.writer=threading.Lock();self.gate=threading.Lock()
        self.pending=queue.Queue(maxsize=1);self.stop=threading.Event();self.active=None;self.token=None
        self.fatal=None;self.used=set();self.sequence=0

    def write(self,message):
        encoded=json.dumps(message,separators=(',',':'),allow_nan=False).encode('utf-8')
        if len(encoded)>MAX_EVENT:raise ValueError('Event exceeds transport bound.')
        with self.writer:sys.stdout.buffer.write(encoded+b'\n');sys.stdout.buffer.flush()

    def event(self,kind,**values):
        self.sequence+=1
        self.write(dict(type=kind,protocolVersion=self.version,runId=str(self.active),sequence=self.sequence,**values))

    def controls(self):
        hello=False
        try:
            while not self.stop.is_set():
                raw=sys.stdin.buffer.readline(MAX_COMMAND+1)
                if not raw:raise EOFError('Control stream disconnected.')
                if len(raw)>MAX_COMMAND or not raw.endswith(b'\n'):raise ValueError('Oversized/incomplete control frame.')
                message=strict_json(raw.decode('utf-8',errors='strict'))
                if type(message) is not dict or type(message.get('protocolVersion')) is not int or message['protocolVersion'] not in (4,5) or hello and message['protocolVersion']!=self.version:
                    raise ValueError('Explicit PBR frontend protocol required.')
                command=message.get('command')
                if command=='hello' and set(message)=={'command','protocolVersion'} and not hello:
                    self.version=message['protocolVersion'];hello=True
                    if self.version==5:
                        if self.room_scene is None:raise ValueError('Original room is not provisioned.')
                        scene=SCENE;agents=self.room_agents
                    else:
                        from drt_backend.pbr_execution.frontend import SCENE as scene
                        if self.pbr_scene is None:raise ValueError('Explicit historical PBR fixture is not provisioned.')
                        agents=self.pbr_agents
                    self.write(dict(type='ready',protocolVersion=self.version,pid=os.getpid(),sceneId=scene,
                        modes=['cpuSerialPreview','localAllCore']+(['distributedAllCore'] if agents else [])))
                elif not hello:raise ValueError('Readiness handshake required.')
                elif command=='start' and set(message)=={'command','protocolVersion','request'}:
                    if self.version==5:
                        from drt_backend.room_pbr.frontend import validate_request
                        agents=self.room_agents
                    else:
                        from drt_backend.pbr_execution.frontend import validate_request
                        agents=self.pbr_agents
                    run=validate_request(message['request'])
                    if message['request']['mode']=='distributedAllCore' and not agents:raise ValueError('Explicit authenticated Agents required; no Local fallback.')
                    with self.gate:
                        if self.active is not None or run in self.used:raise ValueError('Run already active or reused.')
                        self.active,self.token=run,CancellationToken();self.used.add(run)
                    self.pending.put_nowait(message['request'])
                elif command=='cancel' and set(message)=={'command','protocolVersion','runId'}:
                    with self.gate:
                        if self.active is None or str(self.active)!=message['runId']:raise ValueError('Cancellation RunId mismatch.')
                        self.token.request()
                elif command=='shutdown' and set(message)=={'command','protocolVersion'}:
                    self.stop.set()
                    with self.gate:
                        if self.token is not None:self.token.request()
                else:raise ValueError('Unknown PBR control command/properties.')
        except Exception as error:
            self.fatal=error;self.stop.set()
            with self.gate:
                if self.token is not None:self.token.request()

    def run(self):
        threading.Thread(target=self.controls,name='ipc-control',daemon=True).start()
        while not self.stop.is_set():
            try:request=self.pending.get(timeout=.05)
            except queue.Empty:continue
            self.render(request)
        if self.fatal is not None and self.active is None:
            self.write(dict(type='protocolError',protocolVersion=self.version,message=str(self.fatal)[:4096]))

    def render(self,request):
        if self.version==5:from drt_backend.room_pbr.frontend import render
        else:from drt_backend.pbr_execution.frontend import render
        return render(self,request)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--scene',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    for name in ('pbr-scene','pbr-resource-root','pbr-agents','room-scene','room-resource-root','room-agents'):parser.add_argument('--'+name,type=Path)
    args=parser.parse_args()
    if not args.scene.is_absolute() or not args.output.is_absolute():parser.error('Absolute scene/output paths required.')
    Host(args.scene,args.output,pbr_scene=args.pbr_scene,pbr_root=args.pbr_resource_root,pbr_agents=args.pbr_agents,
        room_scene=args.room_scene,room_root=args.room_resource_root,room_agents=args.room_agents).run()


if __name__=='__main__':main()
