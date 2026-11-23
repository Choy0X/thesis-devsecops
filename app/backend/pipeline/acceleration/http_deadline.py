"""Expose request-owned sockets to cooperative mining cancellation."""
from contextlib import contextmanager
from contextvars import ContextVar
import socket

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from . import operations

_request = ContextVar('mining_http_request', default=None)


class ConnectionCheck:
    def connect(self):
        operations.check()
        super().connect()
        operations.check()


class HTTP(ConnectionCheck, HTTPConnection): pass
class HTTPS(ConnectionCheck, HTTPSConnection): pass


class PoolCheck:
    def _get_conn(self, timeout=None):
        operations.check()
        connection = super()._get_conn(timeout=timeout)
        if request := _request.get(): request.connections.append(connection)
        operations.check()
        return connection


class HTTPPool(PoolCheck, HTTPConnectionPool): ConnectionCls = HTTP
class HTTPSPool(PoolCheck, HTTPSConnectionPool): ConnectionCls = HTTPS


class Adapter(HTTPAdapter):
    def init_poolmanager(self,*args,**kwargs):
        super().init_poolmanager(*args,**kwargs)
        self.poolmanager.pool_classes_by_scheme = {'http':HTTPPool,'https':HTTPSPool}

    def proxy_manager_for(self,*args,**kwargs):
        manager = super().proxy_manager_for(*args,**kwargs)
        manager.pool_classes_by_scheme = {'http':HTTPPool,'https':HTTPSPool}
        return manager


def install(session):
    if not hasattr(session,'adapters'): return
    for prefix, previous in list(session.adapters.items()):
        if isinstance(previous,HTTPAdapter) and not isinstance(previous,Adapter):
            session.mount(prefix,Adapter(pool_connections=previous._pool_connections,
                pool_maxsize=previous._pool_maxsize,max_retries=previous.max_retries,pool_block=previous._pool_block))
            previous.close()


class Request:
    def __init__(self):
        self.connections, self.responses = [], []

    def close(self):
        sockets = [getattr(c,'sock',None) for c in list(self.connections)]
        for response in list(self.responses):
            # HTTP/1 Connection: close responses detach the socket from the pool
            # connection while the body is still being consumed.
            raw = getattr(getattr(getattr(response.raw,'_fp',None),'fp',None),'raw',None)
            sockets.append(getattr(raw,'_sock',None))
        for sock in sockets:
            if sock is not None:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass  # Already closed sockets need no further shutdown.
        for connection in list(self.connections): connection.close()


@contextmanager
def capture():
    request = Request()
    token = _request.set(request)
    try:
        with operations.resource(request.close): yield request
    except BaseException:
        request.close()
        operations.check()
        raise
    finally:
        _request.reset(token)
