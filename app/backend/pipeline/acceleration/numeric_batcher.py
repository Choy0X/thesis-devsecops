"""Bounded 20 ms coalescing of compatible numeric requests; no device fallback."""
from collections import deque
from concurrent.futures import Future,TimeoutError
import json
import threading
import time
from . import operations
from .metrics import current


class Batcher:
    def __init__(self,execute,*,wait_seconds=.02,max_bytes=32<<20):
        self.execute=execute
        self.wait_seconds=min(.02,max(0,float(wait_seconds)))
        self.max_bytes=int(max_bytes)
        self.condition=threading.Condition()
        self.queue=deque()
        self.bytes=0
        self.closed=False
        self.thread=threading.Thread(target=self._run,name='campaign-numeric-batches',daemon=True)
        self.thread.start()

    def submit(self,operation,data):
        field={'overlap':'pairs','minhash':'rows','compare':'left'}.get(operation)
        if field is None:raise ValueError('Unsupported numeric operation')
        wire=json.dumps(data,separators=(',',':'),default=lambda value:value.tolist())
        size=len(wire.encode())+len(operation)+64
        if size>self.max_bytes:raise ValueError('Numeric request exceeds payload limit')
        data=json.loads(wire)
        params={key:value for key,value in data.items() if key not in (field,'right')}
        if operation=='compare':params['width']=len(data['left'][0]) if data['left'] else 0
        identity=(operation,json.dumps(params,sort_keys=True,separators=(',',':')))
        future=Future()
        with self.condition:
            while not self.closed and self.bytes+size>self.max_bytes:
                operations.check();self.condition.wait(.05)
            if self.closed:raise RuntimeError('Numeric broker is closed')
            self.queue.append((identity,data,future,size,time.monotonic(),field))
            self.bytes+=size
            self.condition.notify_all()
        try:
            while True:
                operations.check()
                try:return future.result(timeout=.1)
                except TimeoutError:continue
        except BaseException:
            future.cancel();raise

    def _run(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:self.condition.wait()
                if self.closed and not self.queue:return
                first=self.queue.popleft()
                deadline=first[4]+self.wait_seconds
                while not self.closed and time.monotonic()<deadline:
                    self.condition.wait(deadline-time.monotonic())
                batch=[first]
                rest=deque()
                size=first[3]
                while self.queue:
                    item=self.queue.popleft()
                    if item[0]==first[0] and size+item[3]<=self.max_bytes:
                        batch.append(item);size+=item[3]
                    else:rest.append(item)
                self.queue=rest
            live=[item for item in batch if item[2].set_running_or_notify_cancel()]
            if live:
                identity,data,_,_,_,field=live[0]
                combined={key:value for key,value in data.items() if key not in (field,'right')}
                combined[field]=[row for item in live for row in item[1][field]]
                if identity[0]=='compare':combined['right']=[row for item in live for row in item[1]['right']]
                try:
                    result=self.execute(identity[0],combined)
                    if result is None or len(result)!=sum(len(item[1][field]) for item in live):
                        raise RuntimeError('Numeric batch returned an invalid result count')
                    offset=0
                    for item in live:
                        length=len(item[1][field]);value=result[offset:offset+length];offset+=length
                        if not item[2].cancelled():item[2].set_result(value)
                    current.count('numeric_batches')
                    current.count('numeric_subrequests',len(live))
                except BaseException as error:
                    for item in live:
                        if not item[2].done():item[2].set_exception(error)
            with self.condition:
                self.bytes-=sum(item[3] for item in batch)
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed=True
            while self.queue:
                item=self.queue.popleft();self.bytes-=item[3]
                if not item[2].done():item[2].set_exception(RuntimeError('Numeric broker closed'))
            self.condition.notify_all()
        self.thread.join(timeout=5)
        if self.thread.is_alive():raise RuntimeError('Numeric broker termination unconfirmed')
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
