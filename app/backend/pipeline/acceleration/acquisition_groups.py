"""Shared bounded acquisition. A dependent can detach without cancelling siblings."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from contextvars import copy_context
import threading
import hashlib
import os
from . import operations
from .metrics import current


class Groups:
    def __init__(self,acquire,*,should_stop=lambda:False,workers=2):
        self.acquire,self.should_stop=acquire,should_stop
        self.lock=threading.RLock()
        self.pool=ThreadPoolExecutor(workers,thread_name_prefix='repository-acquisition')
        self.groups,self.lookup={},{}
        self.closed=False

    def prepare(self,rows):
        with self.lock:
            self.groups,self.lookup={},{}
            self._append(rows)

    def _append(self,rows):
        by_repo=defaultdict(list)
        for row in rows:by_repo[(row['host'],row['owner'],row['repo'])].append(row)
        for members in by_repo.values():
            for start in range(0,len(members),8):
                group_rows=members[start:start+8]
                key=hashlib.sha256('|'.join(row['commit_key'] for row in group_rows).encode()).hexdigest()
                self.groups[key]={'rows':group_rows,'live':{row['commit_key'] for row in group_rows},'future':None,'operation':None}
                for row in group_rows:self.lookup[row['commit_key']]=key

    def update(self,rows):
        """Refill without draining or discarding another item's shared request."""
        if len(rows)>256:raise ValueError('Acquisition lookahead is limited to 256 descriptors')
        with self.lock:
            active={row['commit_key'] for row in rows}
            for key in set(self.lookup)-active:
                group=self.groups[self.lookup.pop(key)]
                group['live'].discard(key)
            retained=set(self.lookup.values())
            for key,group in list(self.groups.items()):
                op=group['operation'];future=group['future']
                if op is not None and op.failed_resources:raise OSError('Shared acquisition cleanup unconfirmed')
                if key not in retained and (future is None or future.done()):self.groups.pop(key)
            self._append([row for row in rows if row['commit_key'] not in self.lookup])

    def _work(self,key,group,startup_credit=None):
        def stop():
            with self.lock:return self.closed or self.should_stop() or not group['live']
        op=operations.Operation('repository-group:'+key,240,stop)
        if startup_credit is not None:
            op._reader_startup_credit=startup_credit
            op.resource_ticket=startup_credit[1]
        with self.lock:group['operation']=op
        try:
            with operations.use(op):
                op.phase('fetching_snapshot_group',total=len(group['rows']),unit='commit snapshots')
                self.acquire(group['rows'],stop)
        except BaseException as error:
            op.cancel();op.finish('group_failed',str(error));raise
        else:op.finish('group_acquired')

    def start(self,row):
        """Start shared Git work without occupying the metadata stage during its wait."""
        with self.lock:
            key=self.lookup[row['commit_key']];group=self.groups[key]
            future=group['future']
            if future is not None and future.done() and not future.cancelled() and future.exception() is not None:
                from . import mining_policy
                if mining_policy.current().get('version') == 4:
                    from app.backend.pipeline import stopping
                    previous=group['operation']
                    if previous is not None and previous.failed_resources:
                        paused=stopping.Paused('Shared acquisition cleanup could not be confirmed')
                        paused.reason={'code':'mining_acquisition_cleanup','detail':str(paused),'retryable':True}
                        raise paused
                    if self.closed or self.should_stop():raise stopping.Paused()
                    from .failures import retryable,resource_pressure
                    from .resource_control import AdmissionDeferred
                    error=future.exception()
                    if resource_pressure(error) or isinstance(error,AdmissionDeferred) or retryable(error):
                        group['future']=group['operation']=None
                        group['live'].add(row['commit_key'])
                        current.count('git_acquisition_group_retries')
            if group['future'] is None:
                # copy_context retains the same mutable requester Operation.
                # Bind its live admission before queueing, including its owner
                # and generation so a later stage/retry cannot lend its credit.
                from .resource_control import active
                owner=active()
                ticket=getattr(operations.current(),'resource_ticket',None)
                startup_credit=None
                if owner is not None and ticket is not None:
                    with owner.lock:
                        generation=owner._ticket_generations.get(ticket)
                        if generation is not None and ticket in owner.tickets:
                            startup_credit=(owner,ticket,generation)
                from .submission import submit_owned
                submit_owned(self.pool,copy_context().run,self._work,key,group,startup_credit,
                    register=lambda future:group.__setitem__('future',future))
            else:current.count('git_acquisition_group_reuse')
            return group['future']

    def wait(self,row):
        future = self.start(row)
        with self.lock:
            group = self.groups[self.lookup[row['commit_key']]]
        try:
            while True:
                operations.check()
                try:return future.result(timeout=.1)
                except TimeoutError:
                    if future.done():return future.result()
                    continue
        finally:
            with self.lock:group['live'].discard(row['commit_key'])

    def close(self):
        with self.lock:
            self.closed=True
            active=[group['operation'] for group in self.groups.values() if group['operation']]
        for op in active:
            if not op.finished or op.failed_resources:op.cancel()
        self.pool.shutdown(wait=True,cancel_futures=True)
        if any(op.failed_resources for op in active):raise OSError('Shared acquisition cleanup unconfirmed')
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def _group_filter(policy=None):
    """Recorded transport profile; source admission remains independently bounded."""
    from . import mining_policy
    policy=mining_policy.current() if policy is None else policy
    if policy.get('version')!=4:return 'blob:none'
    option='ZTSA_EXPERIMENTAL_GIT_GROUP_BLOB_LIMIT'
    default=mining_policy.acquisition_config(policy)['initial_blob_bytes']
    value=os.environ.get(option,str(default) if default else '')
    if value not in ('','32768','131072','262144'):
        raise ValueError(option+' must be empty, 32768, 131072, or 262144')
    return 'blob:limit='+value if value else 'blob:none'


def _hint_inventory(shared,shas,should_stop):
    """Optional inventory owns cleanup without poisoning the required operation."""
    from app.backend.pipeline import stopping
    from .repository_service import CompatibilityFallback
    parent=operations.current()
    operations.check()
    child=operations.Operation('selected-hint-inventory',operations.timeout(30),should_stop)
    result=None
    failure=None
    def close_child():
        child.cancel()
        if child.failed_resources:raise OSError('Selected hint inventory cleanup unconfirmed')
    try:
        # Catch expected transport failures inside the parent's resource scope.
        # A failed child close still escapes and retains parent ownership.
        with operations.resource(close_child):
            try:
                with operations.use(child):result=shared.info_many(shas)
            except (MemoryError,operations.DeadlineExpired,stopping.Paused):raise
            except (OSError,CompatibilityFallback) as error:
                close_child()
                failure=error
            if parent:parent.check()
    finally:
        child.finish('hint_inventory_fallback' if failure else 'hint_inventory_finished',str(failure) if failure else None)
    if failure:current.count('git_selected_hint_inventory_fallbacks')
    return result


def git_group(fetcher,rows,should_stop,*,hints=None):
    filter_spec=_group_filter()
    from app.backend.pipeline.ingest import codemine as cm
    from .repository_service import service,CompatibilityFallback
    first=rows[0]
    path=fetcher._repo(first)
    with cm._repo_lock(path):
        shared=service(path)
        wanted=[]
        from .resource_control import active
        # Only the bounded local inventory burst is pinned. Release before any
        # optional hints or remote fetch; the existing repository lock remains.
        with shared.reader_lease(resources=active()):
            for row in rows:
                sha=row['sha']
                if len(sha)!=40:continue
                try:
                    shared._exact(sha)
                    info=shared.info_many([sha])[0]
                    parent=shared.metadata(sha)['parent'] if info else None
                    if info is None or (parent and shared.info_many([parent])[0] is None):wanted.append(sha)
                except CompatibilityFallback:continue
        if not wanted:return
        required=list(dict.fromkeys(wanted))
        future=[]
        if hints is not None and len(required)<8:
            from itertools import islice
            repo=(first['host'],first['owner'],first['repo'])
            capacity=8-len(required)
            for row in islice(hints(first,capacity),capacity):
                if not isinstance(row,dict) or tuple(row.get(key) for key in ('host','owner','repo'))!=repo:
                    continue
                sha=row.get('sha')
                if not isinstance(sha,str) or len(sha)!=40 or sha in required or sha in future:
                    continue
                try:
                    shared._exact(sha)
                    inventory=_hint_inventory(shared,[sha],should_stop)
                    if inventory is None:continue
                    if inventory[0] is not None:
                        current.count('git_selected_hint_local_skips')
                        continue
                except CompatibilityFallback:continue
                future.append(sha)
        request=required+future
        def fetch(shas):
            return cm._git(['-C',str(path),'-c','protocol.version=2','fetch','-q','--depth=2',
                '--filter='+filter_spec,'--no-tags','--no-auto-maintenance','--no-write-fetch-head','origin','--stdin'],
                input_data=('\n'.join(shas)+'\n').encode(),timeout=240)
        try:
            try:
                fetch(request)
            except cm.FetchError:
                if not future:raise
                operations.check()
                current.count('git_hint_current_only_retries')
                fetch(required)
                future=[]
            shared.invalidate_availability()
            current.count('git_commit_fetch_groups')
            current.count('git_group_filter_'+filter_spec.replace(':','_').replace('=','_'))
            current.count('git_grouped_commit_items',len(wanted))
            if future:
                inventory=_hint_inventory(shared,future,should_stop)
                verified=sum(info is not None and info[1]=='commit' for info in inventory) if inventory is not None else 0
                current.count('git_selected_hint_items',verified)
                if verified<len(future):current.count('git_selected_hint_unverified',len(future)-verified)
        except cm.FetchError as error:
            operations.check()
            current.count('git_group_reference_fallbacks')
            print('Grouped snapshot acquisition requires per-item recovery: '+str(error)[:180])
