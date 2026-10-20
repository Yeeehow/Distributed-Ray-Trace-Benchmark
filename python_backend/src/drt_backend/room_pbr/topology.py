"""Explicit selected room topology; credentials are local absolute paths."""
from pathlib import Path
from drt_backend.lan.protocol import strict_json,identifier,fingerprint
from . import codec
from .admission import LOCAL_WORKER_LIMIT,DISTRIBUTED_WORKER_LIMIT,MAX_AGENTS,LOCAL_NODE_ID


def load_agents(path):
    p=Path(path)
    codec.require(p.is_absolute() and p.is_file() and p.stat().st_size<=32768,'Absolute bounded room topology required.')
    c=strict_json(p.read_bytes())
    codec.require(type(c) is dict,'Room topology object required.')
    c=dict(global_workers='auto')|c
    c=codec.exact(c,('wire_version','coordinator_id','certificate','private_key','global_workers','agents'))
    codec.require(type(c['wire_version']) is int and c['wire_version']==4,'Room requires Worker configuration 4.')
    identifier(c['coordinator_id'])
    if c['global_workers']=='auto':c['global_workers']=DISTRIBUTED_WORKER_LIMIT
    codec.integer(c['global_workers'],1,DISTRIBUTED_WORKER_LIMIT)
    codec.require(type(c['agents']) is list and 1<=len(c['agents'])<=MAX_AGENTS,'Room Node bound exceeded.')
    identities=set();pins=set();endpoints=set()
    for index,a in enumerate(c['agents']):
        codec.require(type(a) is dict,'Room Agent object required.')
        a=dict(workers='auto')|a;c['agents'][index]=a
        codec.exact(a,('node_id','host','port','workers','trust','fingerprint'))
        identifier(a['node_id']);fingerprint(a['fingerprint']);codec.integer(a['port'],1,65535)
        codec.require(a['node_id']!=LOCAL_NODE_ID,'drt-local is reserved for the owned coordinator participant.')
        if a['workers']!='auto':codec.integer(a['workers'],1,LOCAL_WORKER_LIMIT)
        codec.require(type(a['host']) is str and 0<len(a['host'])<=253 and Path(a['trust']).is_absolute(),'Invalid room endpoint/trust.')
        codec.require(a['node_id'] not in identities and a['fingerprint'] not in pins and (a['host'],a['port']) not in endpoints,'Duplicate room Node identity.')
        identities.add(a['node_id']);pins.add(a['fingerprint']);endpoints.add((a['host'],a['port']))
    codec.require(all(Path(c[k]).is_absolute() for k in ('certificate','private_key')),'Absolute room credential paths required.')
    return c
