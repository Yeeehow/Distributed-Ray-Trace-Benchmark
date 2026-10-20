"""Shared TLS, pins, registry and bounded data-only framing for explicit PBR schemas."""
import hashlib
import json
from pathlib import Path
import re
import socket
import ssl
import struct
from time import monotonic
from uuid import UUID, uuid4
from threading import Lock
from drt_backend.local_parallel.codec import require, integer, LocalProtocolError

VERSION=None
MAX_CONTROL=32768
MAX_FRAME=65536
MAX_NODES=16
MAX_NODE_WORKERS=64
MAX_GLOBAL_WORKERS=256
KINDS={'hello','probe','capability','initialize','ready','assign','result','heartbeat','cancel','finish','terminal','bye','shutdown'}

def exact(value, fields):
    require(type(value) is dict and set(value)==set(fields.split()),'Unexpected message fields.')
    return value

def identifier(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',value) is not None,'Invalid Node/Worker identity.')
    return value

def fingerprint(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None,'Invalid certificate/resource SHA256.')
    return value

def strict_json(raw):
    def pairs(items):
        v={}
        for k,x in items:
            require(k not in v,'Duplicate network JSON property.')
            v[k]=x
        return v
    try:
        result=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
            parse_constant=lambda _:require(False,'Nonfinite network JSON.'))
    except (ValueError,UnicodeError,RecursionError) as e:
        raise LocalProtocolError('Malformed bounded network JSON.') from e
    return result

def tls_context(cert, key, trust, *, server=False):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    if not server: context.check_hostname=False  # Explicit SHA256 identity pin is mandatory below.
    context.verify_mode=ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(trust))
    context.load_cert_chain(str(cert),str(key))
    return context

def verify_peer(stream,pin):
    fingerprint(pin)
    require(hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()==pin,'TLS peer pin mismatch.')

class Channel:
    """One reader and one writer; one bounded pending write, no arbitrary pickle."""
    version=VERSION
    kinds=KINDS
    def __init__(self,stream,session=None):
        require(self.version in (3,4),'An explicit PBR Channel schema is required.')
        self.stream,self.session=stream,session
        self.sent=self.received=0
        self.bytes_sent=self.bytes_received=0
        self.write_lock=Lock()
        self.phase='authenticated'
        self.expected_kind=None
        self.last_kind=None
        self.read_deadline=None
        self.stream.settimeout(3)

    def send(self,kind,body):
        with self.write_lock:
            require(kind in self.kinds and self.session is not None,'Unknown outbound kind/session.')
            self.sent+=1
            raw=json.dumps(dict(wireVersion=self.version,session=self.session,sequence=self.sent,kind=kind,body=body),
                ensure_ascii=True,allow_nan=False,separators=(',',':')).encode('ascii')
            require(len(raw)<=(MAX_FRAME if kind=='result' else MAX_CONTROL),'Network write exceeds bound.')
            self.stream.sendall(struct.pack('!I',len(raw))+raw)
            self.bytes_sent+=len(raw)+4

    def receive(self,seconds=5,*,tick=None):
        deadline=monotonic()+seconds
        self.read_deadline=deadline
        def read(n):
            data=bytearray()
            while len(data)<n:
                left=deadline-monotonic()
                if left<=0: raise TimeoutError('Network frame deadline expired.')
                self.stream.settimeout(min(.25 if tick else 3,left))
                try:chunk=self.stream.recv(n-len(data))
                except socket.timeout:
                    if monotonic()>=deadline:raise TimeoutError('Network frame deadline expired.')
                    if tick:tick()
                    continue
                if not chunk: raise EOFError('Authenticated peer disconnected.')
                data.extend(chunk)
            return bytes(data)
        size=struct.unpack('!I',read(4))[0]
        require(0<size<=MAX_FRAME,'Oversized network frame.')
        raw=read(size)
        value=exact(strict_json(raw),'wireVersion session sequence kind body')
        require(type(value['wireVersion']) is int and value['wireVersion']==self.version,'Unsupported Worker protocol.')
        require(type(value['session']) is str and str(UUID(value['session']))==value['session'] and UUID(value['session']).int!=0,'Invalid session.')
        if self.session is None: self.session=value['session']
        require(value['session']==self.session,'Stale/foreign connection session.')
        require(type(value['sequence']) is int and value['sequence']==self.received+1,'Network sequence mismatch.')
        require(value['kind'] in self.kinds and type(value['body']) is dict,'Unknown kind/body.')
        require(size<=(MAX_FRAME if value['kind']=='result' else MAX_CONTROL),'Control message exceeds bound.')
        self.received+=1;self.bytes_received+=size+4;self.last_kind=value['kind']
        return value['kind'],value['body']

    def expect(self,kind,seconds=5,*,tick=None):
        self.expected_kind=kind
        actual,body=self.receive(seconds,tick=tick)
        require(actual==kind,f'Unexpected protocol transition: expected {kind}, received {actual}.')
        return body

    def close(self):
        try:self.stream.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.stream.close()



class Registry:
    def __init__(self,cap):
        integer(cap,1,256);self.cap=cap;self.nodes={};self.slots=set()
    def register(self,node,session,workers):
        identifier(node)
        require(type(session) is str and str(UUID(session))==session and UUID(session).int!=0,'Invalid registry session.')
        require(node not in self.nodes and len(self.nodes)<MAX_NODES,'Duplicate/excess registered Node.')
        require(type(workers) is list and 1<=len(workers)<=64,'Invalid per-node slots.')
        require(len(self.slots)+len(workers)<=self.cap,'Global Worker capacity exceeded.')
        local=set()
        for w in workers:
            exact(w,'worker_id pid');identifier(w['worker_id']);integer(w['pid'],1,2**32-1)
            require(re.fullmatch(r'worker-([0-9]|[1-5][0-9]|6[0-3])',w['worker_id']) is not None,'Invalid bounded Worker slot.')
            require(w['worker_id'] not in local,'Duplicate registered Worker.')
            local.add(w['worker_id'])
        self.nodes[node]=dict(session=session,workers=workers)
        self.slots.update((node,session,w['worker_id']) for w in workers)
