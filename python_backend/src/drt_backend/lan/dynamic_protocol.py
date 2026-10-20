"""Shared session binding, authenticated failure classification and TLS stream owner."""
import socket
import ssl
import re
import errno
from uuid import UUID,uuid4
from .protocol import Channel as StaticChannel, KINDS,exact,require,integer,identifier,fingerprint,tls_context,verify_peer

class AuthenticationFailure(RuntimeError):
    pass

def transient(error,*,authenticated=False):
    if isinstance(error,ssl.SSLError):
        return authenticated and isinstance(error,(ssl.SSLEOFError,ssl.SSLZeroReturnError))
    if isinstance(error,(EOFError,ConnectionError,TimeoutError)):return True
    return isinstance(error,OSError) and (error.errno in {errno.ECONNREFUSED,errno.ECONNRESET,errno.ECONNABORTED,
        errno.ETIMEDOUT,errno.ENETUNREACH,errno.EHOSTUNREACH} or getattr(error,'winerror',None) in {10051,10065,10060,10061,10054,10053})


class Channel(StaticChannel):
    version=None
    kinds=KINDS|{'recover','recovered','drain'}
    def __init__(self,stream,session=None):
        if isinstance(stream,ssl.SSLSocket):
            from .tls_stream import TlsStream
            stream=TlsStream(stream)
        super().__init__(stream,session)


def uuid(value):
    require(type(value) is str and str(UUID(value))==value and UUID(value).int!=0,'Invalid dynamic UUID.')
    return value


def binding(value):
    exact(value,'run_id job_identity configuration_identity resource_identity task_id ordinal attempt_id generation node worker_id session epoch')
    uuid(value['run_id']);uuid(value['attempt_id']);uuid(value['session'])
    for field in ('job_identity','configuration_identity','task_id'):
        fingerprint(value[field])
    require(type(value['resource_identity']) is str and 0<len(value['resource_identity'])<=256,'Invalid resource identity.')
    identifier(value['node']);identifier(value['worker_id'])
    require(re.fullmatch(r'worker-([0-9]|[1-5][0-9]|6[0-3])',value['worker_id']) is not None,'Invalid Worker slot.')
    integer(value['ordinal'],0,16383);integer(value['generation'],1,5);integer(value['epoch'],1,8)
    return value


