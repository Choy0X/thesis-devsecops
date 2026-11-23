"""Bounded independent stages. Queues carry fingerprints, not source bodies."""
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from contextvars import copy_context
import threading
import time

from app.backend.pipeline import stopping
from . import operations
from .pipeline_spool import STAGES, DiskPressure, StaleAttempt


@dataclass(frozen=True)
class Task:
    key: str
    stage: str
    publication_id: str
    epoch: int
    operation: object


class Engine:
    def __init__(self, spool, callbacks, *, admit=None, should_stop=lambda:False,
                 progress=lambda state:None, failed=lambda row,error:None, service_seconds=600,
                 limits=None, recover=lambda task:None, failure=None, resources=None, load_previous=None,
                 before_dispatch=lambda task:None, after_dispatch=lambda task:None):
        if load_previous is None:
            load_previous = {}
        if not isinstance(load_previous, Mapping):
            raise TypeError('Previous-stage input policy must be a mapping')
        self.load_previous = dict(load_previous)
        for stage, enabled in self.load_previous.items():
            if stage not in STAGES:
                raise ValueError('Unknown previous-input stage: ' + str(stage))
            if type(enabled) is not bool:
                raise ValueError('Previous-stage input policy values must be bool')
        self.spool,self.callbacks=spool,callbacks
        self.before_dispatch,self.after_dispatch=before_dispatch,after_dispatch
        self.resources=resources
        self.resource_state="processing"
        self.admit=admit or memory_admission
        self.should_stop,self.progress,self.on_failed=should_stop,progress,failed
        self.service_seconds,self.recover,self.failure=service_seconds,recover,failure
        self.limits=limits or (resources.stage_limits() if resources else {"acquire":2,"parse":1,"context":2,"publish":1})
        self.pools={name:ThreadPoolExecutor(self.limits[name],thread_name_prefix="mining-"+name) for name in STAGES}
        self.epoch=None
        self.started=time.perf_counter()
        self.first_result_seconds=None
        self.quota_until=0.0
        self.stats_lock=threading.RLock()
        self.stats={name:{'calls':0,'service_seconds':0.0,'queue_seconds':0.0,'cpu_seconds':0.0} for name in STAGES}

    def close(self):
        for pool in self.pools.values():pool.shutdown(wait=True,cancel_futures=True)
        self.spool.close()

    def _register(self,pending,future,task):
        pending[future]=task

    def _execute(self, task):
        started,measured=time.monotonic(),time.perf_counter()
        cpu_started=time.thread_time()
        op=task.operation
        op.resource_ticket=(task.key,task.stage)
        waited=getattr(op,"resource_wait_seconds",0.)
        try:
            with operations.use(op):
                op.deadline=started+max(.001,self.service_seconds-self.spool.spent(task.key))
                op.phase(task.stage)
                index=STAGES.index(task.stage)
                row=self.spool.input(task.key)
                value=self.recover(task) if task.stage=="publish" else None
                if task.stage=="publish" and value is not None:
                    op._reused_publication_receipt=True
                if value is None:
                    previous=(self.spool.output(task.key,STAGES[index-1])
                              if index and self.load_previous.get(task.stage,True) else None)
                    value=self.callbacks[task.stage](row,previous,task)
                op.check()
                seconds=max(0,time.perf_counter()-measured-getattr(op,"resource_wait_seconds",0.)+waited)
                self.spool.checkpoint(task.key,task.stage,value,task.epoch,seconds=seconds)
                op.deadline=float("inf")
                op.phase("queued_"+(self.spool.stage(task.key) or "completed"))
            return seconds
        except BaseException as error:
            error._pipeline_seconds=time.perf_counter()-measured
            raise
        finally:
            # Stage-thread CPU includes decoding/checkpoint work, excludes other
            # threads, child processes, GPU work and time waiting for admission.
            self.after_dispatch(task)
            cpu_seconds=max(0.0,time.thread_time()-cpu_started)
            with self.stats_lock:
                self.stats[task.stage]['calls']+=1
                self.stats[task.stage]['cpu_seconds']+=cpu_seconds
                self.stats[task.stage]['service_seconds']+=time.perf_counter()-measured

    def state(self):
        with self.stats_lock:
            stats={name:dict(value) for name,value in self.stats.items()}
        capacity={name:value['service_seconds']/max(1,value['calls'])/self.limits[name] for name,value in stats.items()}
        from .bulk_git import _network
        return {**self.spool.describe(),'stage_metrics':stats,'stage_limits':dict(self.limits),
            'descriptor_limit':256,'git_network_limit':_network.limit(),'github_api_limit':1,
            'bottleneck':'quota_wait' if self.quota_until>time.time() else (max(capacity,key=capacity.get) if any(capacity.values()) else None),
            'quota_until':self.quota_until,
            'first_result_seconds':self.first_result_seconds,'private_memory_target_bytes':3_000_000_000 if self.resources else 2<<30,
            'engine_version':4 if self.resources else 3,'resource_state':self.resource_state,
            **({'resources':self.resources.snapshot()} if self.resources else {})}

    def _failure(self,task,error):
        receipt=self.recover(task)
        if receipt is None and self.failure is not None:receipt=self.failure(task,error)
        if receipt is not None:
            try:self.spool.acknowledge(task.key,receipt,task.epoch)
            except Exception as error:
                raise stopping.Paused('Database publication is complete; local acknowledgement needs retry') from error
            return False
        self.spool.fail(task.key,error,task.epoch,seconds=getattr(error,'_pipeline_seconds',0))
        self.on_failed(self.spool.input(task.key),error)
        return True

    def run(self, rows, *, implementation, refill=None, prepare_refill=lambda added,active:None,
            refill_committed=lambda:None):
        if len(rows)>256:raise ValueError("Pipeline descriptor lookahead is limited to 256")
        self.epoch=self.spool.begin()
        self.spool.reconcile_receipts(lambda key,pub:self.recover(Task(key,'publish',pub,self.epoch,None)),epoch=self.epoch)
        keys=(self.spool.enqueue_descriptors(rows,implementation,self.epoch) if refill is not None
              else [self.spool.enqueue(row,implementation,epoch=self.epoch) for row in rows])
        inputs=dict(zip(keys,rows))
        exhausted=refill is None
        def repository(key):
            row=inputs.get(key,{})
            return (row['host'],row['owner'],row['repo']) if all(k in row for k in ('host','owner','repo')) else key
        for key in keys:self.spool.reconcile(key,epoch=self.epoch)
        pending,ops,terminal,failed={}, {},set(),0
        peak,last_progress=0,0.0
        stopped=False
        deferred={}
        ready={}
        retry_at, retries, admission_at = {}, {}, {}
        refill_rows=refill_keys=None
        try:
            while len(terminal)<len(keys) or not exhausted:
                if self.resources:self.resources.trim()
                if self.should_stop():
                    stopped=True
                    for op in ops.values():op.cancel()
                    break
                if refill is not None:
                    # Retire metadata only after authoritative completion. Keep
                    # historical tasks/receipts on SSD, not in the active window.
                    if terminal:
                        for key in terminal:
                            inputs.pop(key,None);ops.pop(key,None)
                            retry_at.pop(key,None);admission_at.pop(key,None)
                            for stage in STAGES:
                                ready.pop((key,stage),None);retries.pop((key,stage),None)
                        keys=[key for key in keys if key not in terminal]
                        terminal.clear()
                    if not exhausted and len(keys)<=192:
                        capacity=256-len(keys)
                        ticket=('descriptor-refill',self.epoch)
                        can_refill=True
                        if self.resources:
                            from .resource_control import STAGE_BYTES
                            can_refill=self.resources.reserve(ticket,STAGE_BYTES['acquire'])
                        blocked=not can_refill
                        if can_refill:
                            try:
                                active_rows=tuple(inputs[key] for key in keys)
                                if refill_rows is None:refill_rows=refill(capacity,active_rows)
                                if refill_keys is None:
                                    if (not isinstance(refill_rows,(list,tuple)) or len(refill_rows)>capacity
                                        or any(not isinstance(row,dict) or not isinstance(row.get('commit_key'),str) for row in refill_rows)
                                        or len({row['commit_key'] for row in refill_rows})!=len(refill_rows)
                                        or self.spool.seen_descriptors([row['commit_key'] for row in refill_rows],self.epoch)):
                                        raise ValueError('Refill must supply distinct new descriptors within available capacity')
                                self.spool.check(self.epoch)
                                prepare_refill(refill_rows,active_rows)
                                if refill_keys is None:
                                    refill_keys=self.spool.enqueue_descriptors(refill_rows,implementation,self.epoch)
                                for key in refill_keys:self.spool.reconcile(key,epoch=self.epoch)
                                # Build the replacement frontier before publishing
                                # it. A retry retains its already queued batch and
                                # cannot consume another source batch or reclaim it.
                                new_keys=keys+refill_keys
                                new_inputs={**inputs,**dict(zip(refill_keys,refill_rows))}
                                refill_committed()
                                keys,inputs=new_keys,new_inputs
                                if not refill_rows:exhausted=True
                                refill_rows=refill_keys=None
                            except MemoryError:
                                if not self.resources:raise
                                self.resources.pressure();self.resource_state='waiting_memory';blocked=True
                            finally:
                                if self.resources:self.resources.release(ticket)
                    if not keys and exhausted:break
                width={name:sum(task.stage==name for task in pending.values()) for name in STAGES}
                active={task.key for task in pending.values()}
                next_stages=self.spool.stages(keys)
                admitted=False
                # Downstream first so completed source is drained before fetching more.
                for stage in reversed(STAGES):
                    for key in keys:
                        if (key in terminal or key in active or (key in retry_at and retry_at[key]>time.time())
                                or admission_at.get(key,0)>time.monotonic()):continue
                        next_stage=next_stages[key]
                        if next_stage is None:
                            terminal.add(key);continue
                        if next_stage!=stage:continue
                        if self.resources and stage=='acquire' and any(
                                task.stage=='acquire' and repository(task.key)==repository(key) for task in pending.values()):
                            continue
                        ready.setdefault((key,stage),time.perf_counter())
                        if width[stage]>=self.limits[stage] or (stage in deferred and deferred[stage]>time.time()):continue
                        if self.resources:
                            from .resource_control import STAGE_BYTES
                            if not self.resources.reserve((key,stage),self.resources.stage_cost(stage)):continue
                        elif not self.admit(stage):continue
                        if self.spool.spent(key)>=self.service_seconds:
                            if self.resources:self.resources.release((key,stage))
                            error=operations.DeadlineExpired("Commit active-service budget exhausted")
                            if key not in ops:ops[key]=operations.Operation(self.spool.input(key)['commit_key'],self.service_seconds,self.should_stop,kind='commit')
                            task=Task(key,stage,self.spool.publication_id(key),self.epoch,ops[key])
                            is_failed=self._failure(task,error)
                            ops[key].finish('failed' if is_failed else 'done',str(error) if is_failed else None,
                                completion=None if is_failed else 'reused')
                            terminal.add(key);failed+=int(is_failed);continue
                        if key not in ops:
                            ops[key]=operations.Operation(self.spool.input(key)["commit_key"],self.service_seconds,self.should_stop,kind="commit")
                        with self.stats_lock:
                            self.stats[stage]['queue_seconds']+=time.perf_counter()-ready.pop((key,stage))
                        task=Task(key,stage,self.spool.publication_id(key),self.epoch,ops[key])
                        self.spool.running(key,self.epoch)
                        try:
                            self.before_dispatch(task)
                            from .submission import submit_owned
                            submit_owned(self.pools[stage],copy_context().run,self._execute,task,
                                register=lambda future:self._register(pending,future,task))
                        except BaseException as error:
                            self.after_dispatch(task)
                            if self.resources:self.resources.release((key,stage))
                            from .failures import resource_pressure
                            if self.resources and resource_pressure(error):
                                task.operation.cancel()
                                if task.operation.failed_resources:
                                    raise stopping.Paused('Pipeline resource cleanup could not be confirmed') from error
                                self.spool.defer(key,self.epoch)
                                task.operation.finish('resource_retry',str(error))
                                ops.pop(key,None)
                                self.resources.pressure()
                                admission_at[key]=time.monotonic()+max(.05,self.resources.poll_seconds)
                                self.resource_state='waiting_memory'
                                from .metrics import current
                                current.count('memory_pressure_retries')
                                continue
                            raise
                        active.add(key);width[stage]+=1;admitted=True
                peak=max(peak,len(pending))
                now=time.monotonic()
                if now-last_progress>=1:
                    last_progress=now
                    self.progress({**self.state(),"inflight":width,"peak_inflight":peak})
                if not pending:
                    if len(terminal)==len(keys):
                        if exhausted:break
                        if keys:continue
                    if not admitted and self.resources:
                        self.resource_state=("waiting_quota" if any(until>time.time() for until in deferred.values()) else
                                             "waiting_retry" if (any(until>time.time() for until in retry_at.values())
                                                 or any(until>time.monotonic() for until in admission_at.values())) else "waiting_memory")
                        self.progress(self.state())
                        self.resources.idle(should_stop=self.should_stop,kind=self.resource_state)
                        continue
                    if not admitted and any(until>time.time() for until in deferred.values()):
                        if self.admit is memory_admission and not self.admit('acquire'):
                            raise memory_pause()
                        from .metrics import current
                        waiting=time.perf_counter()
                        time.sleep(.1)
                        current.count('quota_wait_seconds',time.perf_counter()-waiting)
                        continue
                    if not admitted:
                        raise memory_pause()
                done,_=wait(list(pending),timeout=.05,return_when=FIRST_COMPLETED)
                for future in done:
                    task=pending.pop(future)
                    if self.resources:self.resources.release((task.key,task.stage))
                    self.resource_state="processing"
                    try:
                        future.result()
                    except (DiskPressure,StaleAttempt):
                        raise
                    except stopping.Paused:
                        if not self.should_stop():raise
                        stopped=True
                    except Exception as error:
                        if task.operation.failed_resources:
                            raise stopping.Paused("Pipeline resource cleanup could not be confirmed") from error
                        if hasattr(error,'reset'):
                            deferred['acquire']=deferred['context']=float(error.reset)+5
                            self.quota_until=max(self.quota_until,float(error.reset)+5)
                            self.spool.defer(task.key,self.epoch,seconds=getattr(error,"_pipeline_seconds",0))
                            task.operation.finish('rate_limited',str(error))
                            ops.pop(task.key,None)
                            continue
                        from .resource_control import AdmissionDeferred
                        if self.resources and isinstance(error,AdmissionDeferred):
                            error.__traceback__=None
                            self.spool.defer(task.key,self.epoch,seconds=getattr(error,"_pipeline_seconds",0))
                            task.operation.finish('admission_wait',str(error))
                            ops.pop(task.key,None)
                            admission_at[task.key]=time.monotonic()+max(.05,self.resources.poll_seconds)
                            self.resource_state='waiting_memory'
                            from .metrics import current
                            current.count('resource_admission_deferrals')
                            continue
                        from .failures import resource_pressure
                        if self.resources and resource_pressure(error):
                            task.operation.cancel()
                            if task.operation.failed_resources:
                                raise stopping.Paused('Pipeline resource cleanup could not be confirmed') from error
                            error.__traceback__=None
                            self.spool.defer(task.key,self.epoch,seconds=getattr(error,"_pipeline_seconds",0))
                            task.operation.finish("resource_retry",str(error))
                            ops.pop(task.key,None)
                            self.resources.pressure()
                            self.resource_state="waiting_memory"
                            self.resources.idle(should_stop=self.should_stop)
                            from .metrics import current
                            current.count("memory_pressure_retries")
                            continue
                        from .failures import retryable
                        identity=(task.key,task.stage)
                        if self.resources and retryable(error) and retries.get(identity,0)<2:
                            retries[identity]=retries.get(identity,0)+1
                            retry_at[task.key]=time.time()+2**(retries[identity]-1)
                            from app.backend.pipeline.ingest.depversions import RegistryUnavailable
                            if isinstance(error,RegistryUnavailable) and error.retry_at is not None:
                                retry_at[task.key]=max(retry_at[task.key],error.retry_at)
                            task.operation.finish("retrying",str(error))
                            ops.pop(task.key,None)
                            self.spool.defer(task.key,self.epoch,seconds=getattr(error,"_pipeline_seconds",0))
                            from .metrics import current
                            current.count("adaptive_stage_retries")
                            continue
                        is_failed=self._failure(task,error)
                        task.operation.finish('failed' if is_failed else 'done',str(error) if is_failed else None,
                            completion=None if is_failed else 'reused')
                        terminal.add(task.key);failed+=int(is_failed)
                    else:
                        if self.spool.stage(task.key) is None:
                            task.operation.finish(completion='reused' if getattr(task.operation,'_reused_publication_receipt',False) else 'acknowledged')
                            if self.first_result_seconds is None:self.first_result_seconds=time.perf_counter()-self.started
                            terminal.add(task.key)
        finally:
            if pending:
                for op in ops.values():op.cancel()
                cleanup_error=None
                for future,task in pending.items():
                    try:
                        if future.cancel():self.after_dispatch(task)
                    except BaseException as error:
                        if cleanup_error is None:cleanup_error=error
                wait(list(pending))
                if self.resources:
                    for task in pending.values():self.resources.release((task.key,task.stage))
                if cleanup_error is not None:raise cleanup_error
            for op in ops.values():
                if not op.finished:op.finish("cancelled")
        result={**self.state(),"failed":failed,"cancelled":stopped,"peak_inflight":peak,"first_result_seconds":self.first_result_seconds}
        self.progress(result)
        return result


_private_lock=threading.RLock()
_private_sample={}


def memory_snapshot():
    import os
    import psutil
    from app.backend.pipeline.campaigns.admission import _reserve_of
    vm=psutil.virtual_memory()
    identity=(os.getpid(),id(psutil.Process))
    with _private_lock:
        if _private_sample.get('identity')!=identity or time.monotonic()-_private_sample.get('at',0)>.5:
            private=rss=0
            try:
                root=psutil.Process()
                for process in [root,*root.children(recursive=True)]:
                    try:
                        info=process.memory_full_info()
                        private+=getattr(info,'uss',getattr(info,'rss',0))
                        rss+=getattr(info,'rss',0)
                    except psutil.NoSuchProcess:continue
            except (psutil.AccessDenied,OSError):private=rss=None
            _private_sample.update(identity=identity,at=time.monotonic(),private_bytes=private,rss_bytes=rss)
        return {'private_bytes':_private_sample['private_bytes'],'rss_bytes':_private_sample['rss_bytes'],
                'available_bytes':vm.available,'reserve_bytes':int(_reserve_of(vm.total/2**30)*2**30)}


def memory_admission(stage):
    from .memory_profile import current as memory_profile
    profile=memory_profile()
    state=memory_snapshot()
    reserve=state['reserve_bytes'] if profile.reserve_bytes is None else profile.reserve_bytes
    return state['private_bytes'] is not None and state['private_bytes'] < profile.ceiling_bytes and state['available_bytes']>=reserve+(8<<20)


def memory_pause():
    paused=stopping.Paused('Mining pipeline is waiting for the system RAM reserve')
    from .memory_profile import current as memory_profile
    paused.reason={'code':'pipeline_memory_pressure','detail':str(paused),'retryable':True,'resume_needs_gb':memory_profile().resume_needs_gb}
    return paused



