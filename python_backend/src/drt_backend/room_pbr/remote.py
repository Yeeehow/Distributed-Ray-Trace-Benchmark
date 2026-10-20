"""Paired metadata and actual idle authenticated connections; never Workers/rays.

Idle monitor connections are closed before numerical Distributed sessions. The
existing Agent remains a sequential session owner; no second execution pool.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import Event,Thread
from time import monotonic
from uuid import UUID,uuid4
from . import codec,protocol
from .topology import load_agents
from .policy import make_workload,RAW
from .scene import RoomSource
from .adapter import RoomAdapter


def metadata(path):
    c=load_agents(path)
    return dict(schema='drt.room_paired_topology.v1',sha256=sha256(Path(path).read_bytes()).hexdigest(),
        nodes=[dict(id=a['node_id'],host=a['host'],port=a['port'],fingerprint=a['fingerprint']) for a in c['agents']])


def monitor(a):
    cfg=load_agents(a.agents);w=make_workload(width=a.width,height=a.height)
    source=RoomSource.load(a.scene,w);stop=Event();channels={};states={};sequence=0
    def write():
        nonlocal sequence
        sequence+=1
        value=dict(type='remoteState',schema='drt.room_monitor_frontend.v1',generation=a.generation,sequence=sequence,nodes=list(states.values()))
        raw=codec.encode(value,32768);sys.stdout.buffer.write(raw+b'\n');sys.stdout.buffer.flush()
    def controls():
        try:
            raw=sys.stdin.buffer.readline(4097)
            if not raw:return
            from drt_backend.lan.protocol import strict_json
            codec.require(len(raw)<=4096 and raw.endswith(b'\n') and strict_json(raw)==dict(command='shutdown'),'Invalid monitor control.')
        finally:stop.set()
    Thread(target=controls,name='room-monitor-input',daemon=True).start()
    try:
        for peer in cfg['agents']:
            if stop.is_set():break
            ch=None;node=peer['node_id']
            try:
                job=RoomAdapter(source,w,uuid4(),32).job;ch,hello=protocol.connect(cfg,peer,job.run_id,1,job)
                ch.send('monitor',dict(schema='drt.room_idle_monitor.v1',file_sha256=RAW,job=codec.job_data(job)))
                r=codec.exact(ch.expect('monitor_state'),('schema','run_id','epoch','node','state','pid','numerical_workers','job_identity','resource_identity'))
                codec.require(r['schema']=='drt.room_idle_monitor.v1' and r['run_id']==str(job.run_id) and r['epoch']==1
                    and r['node']==node and r['state']=='connected' and r['numerical_workers']==0
                    and r['job_identity']==job.job_identity and r['resource_identity']==job.resource_identity,'Idle monitor ownership differs.')
                codec.integer(r['pid'],1,2**32-1)
                codec.integer(r['epoch'],1,1);codec.integer(r['numerical_workers'],0,0)
                channels[node]=(ch,dict(schema='drt.room_idle_monitor.v1',run_id=str(job.run_id),epoch=1))
                states[node]=dict(id=node,state='Connected',session=ch.session,authenticated=True,compatible=True,available_workers=hello['workers'],agent_pid=r['pid'])
            except Exception as e:
                if ch:ch.close()
                states[node]=dict(id=node,state='Disconnected',session=None,authenticated=False,compatible=False,available_workers=0,agent_pid=0)
            write()
        deadline=monotonic()+a.lifetime_seconds
        while not stop.wait(.5) and monotonic()<deadline:
            for node,(ch,binding) in list(channels.items()):
                try:
                    ch.send('heartbeat',binding);codec.require(ch.expect('heartbeat',3)==binding,'Foreign monitor heartbeat.')
                except Exception:
                    ch.close();del channels[node]
                    states[node]=states[node]|dict(state='Disconnected',session=None,authenticated=False,available_workers=0);write()
    finally:
        for node,(ch,binding) in channels.items():
            try:ch.send('bye',{});codec.require(ch.expect('bye',2)=={},'Monitor close differs.')
            except (OSError,EOFError,ValueError,codec.LocalProtocolError):pass
            finally:ch.close()
        for node in states:states[node]=states[node]|dict(state='Disconnected',session=None,authenticated=False,available_workers=0)
        write()


def main():
    p=argparse.ArgumentParser();mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--list',action='store_true');mode.add_argument('--monitor',action='store_true')
    p.add_argument('--agents',type=Path,required=True);p.add_argument('--scene',type=Path);p.add_argument('--generation')
    p.add_argument('--width',type=int,default=160);p.add_argument('--height',type=int,default=90);p.add_argument('--lifetime-seconds',type=int,default=21600)
    a=p.parse_args()
    if a.list:print(json.dumps(metadata(a.agents)),flush=True);return
    codec.require(a.scene is not None and a.scene.is_absolute() and a.generation is not None
        and str(UUID(a.generation))==a.generation and UUID(a.generation).int!=0,'Explicit monitor authority required.')
    codec.integer(a.lifetime_seconds,1,21600);monitor(a)


if __name__=='__main__':main()
