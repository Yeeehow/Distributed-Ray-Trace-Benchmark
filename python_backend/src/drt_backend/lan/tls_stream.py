"""Single-owner nonblocking TLS I/O with bounded byte handoff.

Only this actor touches the SSLSocket after authentication. Logical protocol
readers/writers never race SSL operations or modify the socket timeout. The v1
transport is retained separately. This removes a concurrency risk, without
claiming a proven explanation for historical intermittent authenticated EOF.
"""
import queue
import select
import socket
import ssl
from threading import Thread,Event,Condition,current_thread
from time import monotonic

class TlsStream:
    MAX_PENDING_BYTES=131072
    def __init__(self,stream):
        self.raw=stream
        self.timeout=3
        self.incoming=bytearray()
        self.condition=Condition()
        self.outgoing=queue.Queue(maxsize=1)
        self.stopping=Event()
        self.error=None
        self.thread=Thread(target=self._io,name='single-owner-tls',daemon=True)
        self.thread.start()

    def settimeout(self,seconds):self.timeout=seconds # Logical read timeout, not an SSL mutation.
    def fileno(self):return self.raw.fileno()

    def recv(self,count):
        deadline=monotonic()+self.timeout
        with self.condition:
            while not self.incoming:
                if self.error is not None:raise self.error
                if self.stopping.is_set():return b''
                left=deadline-monotonic()
                if left<=0:raise socket.timeout('Logical TLS read wait expired.')
                self.condition.wait(left)
            value=bytes(self.incoming[:count]);del self.incoming[:count]
            self.condition.notify_all()
            return value

    def sendall(self,data):
        if len(data)>65540:raise ValueError('TLS output frame exceeds bound.')
        deadline=monotonic()+3
        done=Event();item=dict(data=data,done=done,error=None)
        if self.stopping.is_set():raise self.error or EOFError('TLS owner stopped.')
        try:self.outgoing.put(item,timeout=max(0,deadline-monotonic()))
        except queue.Full:raise TimeoutError('TLS pending write bound exceeded.')
        if not done.wait(max(0,deadline-monotonic())):
            self.shutdown(socket.SHUT_RDWR);raise TimeoutError('TLS write deadline expired.')
        if item['error'] is not None:raise item['error']

    def _io(self):
        pending=None;offset=0
        try:
            self.raw.setblocking(False)
            self.raw.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            while not self.stopping.is_set():
                want_write=False
                if pending is None:
                    try:pending=self.outgoing.get_nowait();offset=0
                    except queue.Empty:pass
                if pending is not None:
                    try:
                        sent=self.raw.send(memoryview(pending['data'])[offset:])
                        if sent<=0:raise EOFError('Authenticated TLS write closed.')
                        offset+=sent
                        if offset==len(pending['data']):pending['done'].set();pending=None
                        else:want_write=True
                    except ssl.SSLWantWriteError:want_write=True
                    except ssl.SSLWantReadError:pass
                with self.condition:free=self.MAX_PENDING_BYTES-len(self.incoming)
                if free:
                    try:
                        raw=self.raw.recv(min(8192,free))
                        if not raw:raise EOFError('Authenticated peer disconnected.')
                        with self.condition:self.incoming.extend(raw);self.condition.notify_all()
                        # Drain already decrypted records before waiting for more
                        # encrypted bytes. The byte budget remains authoritative.
                        if self.raw.pending():continue
                    except ssl.SSLWantReadError:pass
                    except ssl.SSLWantWriteError:want_write=True
                if free or want_write:select.select([self.raw] if free else [],[self.raw] if want_write else [],[],.005)
                else:self.stopping.wait(.005)
        except BaseException as error:
            self.error=error
        finally:
            self.stopping.set()
            self.raw.close() # Socket shutdown/close also belongs to this single owner.
            if pending is not None:
                pending['error']=self.error or EOFError('TLS owner closed.');pending['done'].set()
            while True:
                try:item=self.outgoing.get_nowait()
                except queue.Empty:break
                item['error']=self.error or EOFError('TLS owner closed.');item['done'].set()
            with self.condition:self.condition.notify_all()

    def shutdown(self,_):
        self.stopping.set()
        with self.condition:self.condition.notify_all()

    def close(self):
        self.shutdown(socket.SHUT_RDWR)
        if current_thread() is not self.thread:
            self.thread.join(timeout=.5)
            if self.thread.is_alive():raise RuntimeError('Single TLS owner did not stop within its close deadline.')
