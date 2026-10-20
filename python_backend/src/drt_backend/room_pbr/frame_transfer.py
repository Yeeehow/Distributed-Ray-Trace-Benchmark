"""Bounded RGB24 chunks. Only the hash-verified end can publish a frame.

Each message is an existing ordered IPC event. A frame occupies one contiguous
sequence interval, with no interleaved events, no paths and no object decoding.
"""
import base64
import binascii
from hashlib import sha256
from uuid import UUID,uuid4
from . import codec

SCHEMA='drt.room_rgb24_chunks.v1'
CHUNK_BYTES=49152
MAX_FRAME_BYTES=640*360*3
FIELDS=('schema','frameId','workloadDigest','width','height','pixelFormat','contentLength','chunkCount','sha256','purpose')


def messages(w,rgb,*,purpose):
    codec.require(type(rgb) is bytes and len(rgb)==w.width*w.height*3 and len(rgb)<=MAX_FRAME_BYTES
        and purpose in ('preview','final'),'Invalid bounded room frame.')
    common=dict(schema=SCHEMA,frameId=str(uuid4()),workloadDigest=w.workload_digest,width=w.width,height=w.height,
        pixelFormat='rgb24',contentLength=len(rgb),chunkCount=(len(rgb)+CHUNK_BYTES-1)//CHUNK_BYTES,
        sha256=sha256(rgb).hexdigest(),purpose=purpose)
    yield common|dict(stage='begin')
    for i,start in enumerate(range(0,len(rgb),CHUNK_BYTES)):
        yield common|dict(stage='chunk',index=i,offset=start,data=base64.b64encode(rgb[start:start+CHUNK_BYTES]).decode('ascii'))
    yield common|dict(stage='end')


class Assembler:
    """One transfer, exact job/dimension/sequence binding, at most 691,200 bytes."""
    def __init__(self,run_id,w):
        self.run_id=str(run_id);self.w=w;self.active=None;self.buffer=None;self.next_sequence=None;self.index=0
        self.completed=set()

    def accept(self,event):
        codec.exact(event,('type','protocolVersion','runId','sequence','transfer'))
        codec.require(event['type']=='roomFrame' and type(event['protocolVersion']) is int and event['protocolVersion']==5
            and event['runId']==self.run_id,'Foreign frame session.')
        codec.integer(event['sequence'],1);d=event['transfer'];codec.require(type(d) is dict,'Frame transfer object required.')
        stage=d.get('stage');codec.exact(d,(*FIELDS,'stage',*(('index','offset','data') if stage=='chunk' else ())))
        codec.require(d['schema']==SCHEMA and d['workloadDigest']==self.w.workload_digest and d['pixelFormat']=='rgb24'
            and type(d['width']) is int and type(d['height']) is int and (d['width'],d['height'])==(self.w.width,self.w.height)
            and type(d['contentLength']) is int and d['contentLength']==self.w.pixel_count*3
            and type(d['chunkCount']) is int and d['chunkCount']==(d['contentLength']+CHUNK_BYTES-1)//CHUNK_BYTES
            and d['purpose'] in ('preview','final'),'Frame workload/format/length differs.')
        codec.require(type(d['sha256']) is str and len(d['sha256'])==64 and all(c in '0123456789abcdef' for c in d['sha256']),
            'Invalid frame content hash.')
        codec.require(type(d['frameId']) is str and str(UUID(d['frameId']))==d['frameId'] and UUID(d['frameId']).int!=0,'Invalid frame identity.')
        common={k:d[k] for k in FIELDS}
        if stage=='begin':
            codec.require(self.active is None and d['frameId'] not in self.completed and len(self.completed)<21,'Duplicate/excess/overlapping frame.')
            self.active=common;self.buffer=bytearray();self.index=0;self.next_sequence=event['sequence']+1;return None
        codec.require(self.active==common and self.next_sequence==event['sequence'],'Missing/reordered/foreign frame chunk.')
        if stage=='chunk':
            codec.require(type(d['index']) is int and d['index']==self.index and type(d['offset']) is int
                and d['offset']==len(self.buffer) and type(d['data']) is str and len(d['data'])<=65536,'Invalid chunk interval.')
            try:raw=base64.b64decode(d['data'],validate=True)
            except (binascii.Error,ValueError) as e:raise codec.LocalProtocolError('Malformed frame chunk.') from e
            codec.require(len(raw)==min(CHUNK_BYTES,d['contentLength']-len(self.buffer)) and len(raw)>0
                and base64.b64encode(raw).decode('ascii')==d['data'],'Invalid/noncanonical chunk size.')
            self.buffer.extend(raw);self.index+=1;self.next_sequence+=1;return None
        codec.require(stage=='end' and self.index==d['chunkCount'] and len(self.buffer)==d['contentLength']
            and sha256(self.buffer).hexdigest()==d['sha256'],'Incomplete or corrupt room frame.')
        raw=bytes(self.buffer);self.completed.add(d['frameId']);self.active=self.buffer=None;self.next_sequence=None
        return d['purpose'],raw
