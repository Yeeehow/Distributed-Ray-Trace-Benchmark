"""Bounded transport owners. Never mutate membership, attempts or acceptance.

One initialization/reader thread and one bounded writer per Node session. Every
event carries the captured generation; the coordinator fences older generations.
"""
import queue
from threading import Thread,Event,Lock
from time import monotonic
from drt_backend.local_parallel import codec
from .protocol import exact,require


class SessionManager:
    def __init__(self,config,job,file_sha256,*,_profile=None):
        require(_profile is not None and _profile.wire_version in (3,4),'Explicit PBR session profile required.')
        self.profile=_profile
        self.config,self.job,self.file_sha256=config,job,file_sha256
        self.events=queue.Queue(maxsize=512)
        self.owners={};self.threads=[];self.stopping=Event()
        self.peak_events=0
        self.thread_gate=Lock();self.channels=[]

    def _emit(self,n,epoch,session,kind,body):
        if self.stopping.is_set():return
        self.events.put((n.id,epoch,session,kind,body),timeout=1)
        self.peak_events=max(self.peak_events,self.events.qsize())

    def _thread(self,target,name):
        # Reap completed thread objects rather than retaining every connection.
        with self.thread_gate:
            self.threads=[t for t in self.threads if t.is_alive()]
            require(len(self.threads)<3*max(16,len(self.config['agents'])),'Transport thread bound exceeded.')
            t=Thread(target=target,name=name,daemon=True);self.threads.append(t);t.start()

    def open(self,n):
        epoch=n.epoch
        old=[dict(session=s['session'],run_id=str(self.job.run_id),epoch=s['epoch'])
             for s in n.sessions if not s['confirmed']]
        def run():
            channel=None
            try:
                connector=self.profile.connect
                channel,hello=connector(self.config,n.config,self.job.run_id,epoch)
                with self.thread_gate:
                    self.channels=[c for c in self.channels if c.stream.fileno()!=-1]
                    self.channels.append(channel)
                receipts=[];channel.phase='reconcile'
                for item in old:
                    channel.send('recover',item)
                    response=exact(channel.expect('recovered'),'session receipt')
                    require(response['session']==item['session'],'Wrong cleanup reconciliation session.')
                    receipts.append(response)
                if self.stopping.is_set():channel.close();return
                self._emit(n,epoch,channel.session,'connected',dict(channel=channel,hello=hello,receipts=receipts))
            except BaseException as error:
                if channel:channel.close()
                self._emit(n,epoch,None,'connect_error',error)
        self._thread(run,'v2-connect-'+n.id)

    def prepare(self,n,count):
        codec=self.profile.codec
        channel=n.channel;epoch=n.epoch;session=channel.session
        owner=dict(channel=channel,queue=queue.Queue(maxsize=128),stop=Event())
        self.owners[(n.id,epoch)]=owner
        def tick():
            if self.stopping.is_set() or owner['stop'].is_set():raise InterruptedError('Preparation owner stopped.')
        def write():
            try:
                while not owner['stop'].is_set() and not self.stopping.is_set():
                    try:kind,body=owner['queue'].get(timeout=.05)
                    except queue.Empty:continue
                    channel.send(kind,body)
            except BaseException as error:self._emit(n,epoch,session,'error',error)
        def read():
            try:
                channel.phase='preparing'
                channel.send('initialize',dict(file_sha256=self.file_sha256,job=codec.job_data(self.job),workers=count,epoch=epoch))
                ready=channel.expect('ready',getattr(self.profile,'ready_seconds',20),tick=tick)
                self._emit(n,epoch,session,'ready',dict(body=ready,count=count))
                self._thread(write,'v2-writer-'+n.id)
                channel.phase='executing'
                channel.expected_kind='result|terminal|heartbeat'
                while not self.stopping.is_set() and not owner['stop'].is_set():
                    kind,body=channel.receive(5)
                    require(kind in ('result','terminal','heartbeat'),'Unexpected dynamic execution message.')
                    if kind=='terminal':channel.phase='terminal-received'
                    self._emit(n,epoch,session,kind,body)
                    if kind=='terminal':return
            except BaseException as error:self._emit(n,epoch,session,'error',error)
        self._thread(read,'v2-reader-'+n.id)

    def send(self,n,kind,body,*,priority=False):
        owner=self.owners[(n.id,n.epoch)]
        if priority:
            while True:
                try:owner['queue'].get_nowait()
                except queue.Empty:break
        try:owner['queue'].put_nowait((kind,body))
        except queue.Full:raise codec.LocalProtocolError('Bounded Node control queue exhausted.')

    def close_node(self,n):
        owner=self.owners.pop((n.id,n.epoch),None)
        if owner:owner['stop'].set();owner['channel'].close()
        elif n.channel:n.channel.close()

    def close(self):
        self.stopping.set()
        for owner in list(self.owners.values()):owner['stop'].set();owner['channel'].close()
        with self.thread_gate:
            for channel in self.channels:channel.close()
        deadline=monotonic()+2
        for t in self.threads:t.join(timeout=max(0,deadline-monotonic()))
        living=[t.name for t in self.threads if t.is_alive()]
        if hasattr(self.profile,'close_local_sessions'):living.extend(self.profile.close_local_sessions())
        return living
