"""Failure disposition shared by adaptive item and stage retry paths."""


class LocalGitUnavailable(RuntimeError):
    """Git rejected chdir to a revalidated app-owned local bare repository."""


class LocalGitMemoryError(MemoryError):
    """Confirmed local Git allocator failure after owned process cleanup."""


def resource_pressure(error):
    return isinstance(error,MemoryError) or (
        type(error) is RuntimeError and str(error)=="can't start new thread")


def retryable(error):
    if isinstance(error,LocalGitUnavailable):return True
    if isinstance(error,PermissionError) and getattr(error,'winerror',None) in (5,32,33):
        return True
    import requests
    import sqlite3
    from app.backend.pipeline.ingest.depversions import RegistryUnavailable
    if isinstance(error,RegistryUnavailable):return True
    if isinstance(error,sqlite3.Error):
        code=getattr(error,'sqlite_errorcode',0)
        return (code & 255) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)
    from app.backend.pipeline.db.operations import AutoReconnect, ConnectionFailure, NetworkTimeout
    if isinstance(error, (requests.Timeout, requests.ConnectionError, TimeoutError, ConnectionError,
                          AutoReconnect, ConnectionFailure, NetworkTimeout)):
        return True
    if isinstance(error, requests.HTTPError):
        response = getattr(error, 'response', None)
        return response is not None and response.status_code in (408, 429, 500, 502, 503, 504)
    text = str(error).lower()
    permanent = ('not our ref', 'repository not found', 'could not read username',
                 'no commit found for sha', 'invalid snapshot path', 'unresolvable',
                 'authentication failed', '404 client error', '422 client error')
    if any(value in text for value in permanent):
        return False
    return any(value in text for value in ('timed out', 'timeout', 'connection reset',
        'remote end closed', 'temporary', 'connection aborted', 'could not resolve host',
        '502 bad gateway', '503 service', '504 gateway', 'failed to connect',
        'rpc failed', 'early eof', 'unexpected disconnect'))
