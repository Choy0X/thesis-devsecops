"""Portable stage artifacts. No sessions, loaders or native ASTs are pickled."""
import base64
from dataclasses import asdict,fields,dataclass
import hashlib
import json
from pathlib import Path
from . import operations


@dataclass(frozen=True)
class ExcludedSource:
    reason: str


class FrozenProfile:
    def __init__(self,value):self.value=value
    def repo_profile(self,revision):return self.value


class Sources:
    def __init__(self,base,document,profile):self.base,self.document,self.profile=base,document,profile
    def repo_profile(self,revision):return self.profile
    def prefetch(self,refs):return None
    def content(self,revision,path):
        key=revision+":"+path
        if key not in self.document['refs']:raise ValueError("Parser requested source absent from acquisition checkpoint: "+key)
        value=self.document['refs'][key]
        if value is None:return None
        if isinstance(value,dict):return ExcludedSource(value['excluded'])
        data=base64.b64decode(self.document['blobs'][value],validate=True)
        if hashlib.sha256(data).hexdigest()!=value:raise ValueError("Source blob checksum mismatch")
        return data


def encode_commit(commit,row,gh,*,profile=None):
    from app.backend.pipeline.ingest import codemine as cm
    loader=commit.loader.base if isinstance(commit.loader,Sources) else commit.loader
    needs_profile=bool(commit.parent and not any(json.loads(row.get("expected_langs") or "[]")) and any(cm.language_of(f.path) for f in commit.files))
    profile=(loader.repo_profile(commit.sha) if needs_profile else {}) if profile is None else profile
    if isinstance(loader,cm._BatchedGitLoader):spec={'kind':'batched','path':loader.d,'fallback':loader.fallback}
    elif isinstance(loader,cm._GitLoader):spec={'kind':'git','path':loader.d}
    elif isinstance(loader,cm._RawLoader):spec={'kind':'raw','owner':loader.owner,'repo':loader.repo}
    else:raise ValueError("Unsupported stage source loader")
    acquisition = commit.acquisition
    facts = getattr(gh, 'repository_facts', None) if gh is not None else None
    if facts is not None and 'repository_facts' not in (acquisition or {}):
        observation = facts.observations(row.get('owner'), row.get('repo'))
        if observation:
            acquisition = {**(acquisition or {}), 'repository_facts': observation}
    return {'schema':'mining-commit/3','sha':commit.sha,'parent':commit.parent,'message':commit.message,
        'date':commit.date,'parents':commit.parents,'tree_equal':commit.tree_equal,'files':[asdict(f) for f in commit.files],
        'loader':spec,'profile':profile,'profile_order':list(profile),'acquisition':acquisition}


def decode_commit(document,gh):
    from app.backend.pipeline.ingest import codemine as cm
    if document.get('schema')!='mining-commit/3':raise ValueError("Unknown staged commit schema")
    profile={key:document['profile'][key] for key in document.get('profile_order',document['profile'])}
    spec=document['loader'];kind=spec['kind']
    if kind=='batched':
        loader=cm._BatchedGitLoader(Path(spec['path']),FrozenProfile(profile))
        loader.fallback=spec.get('fallback',False)
    elif kind=='git':
        loader=cm._GitLoader(Path(spec['path']))
        loader.repo_profile=FrozenProfile(profile).repo_profile
    elif kind=='raw' and gh is not None:
        loader=cm._RawLoader(gh.s,spec['owner'],spec['repo'])
        loader.repo_profile=FrozenProfile(profile).repo_profile
    else:raise ValueError("Staged source loader unavailable")
    if 'sources' in document:loader=Sources(loader,document['sources'],profile)
    return cm.Commit(document['sha'],document['parent'],document['message'],document['date'],
        [cm.FileChange(**f) for f in document['files']],loader,document['parents'],document['tree_equal'],document.get('acquisition'))


def prepare_sources(commit,row,meta,max_files,*,reader_scope=None):
    from app.backend.pipeline.ingest import codemine as cm
    from app.backend.pipeline import ecosystems
    src=[f for f in commit.files if cm.language_of(f.path)]
    expected=[lang for lang in json.loads(row.get('expected_langs') or '[]') if lang]
    if expected:src=[f for f in src if cm.language_of(f.path) in expected]
    refs,blobs={},{}
    if not commit.parent or len(src)>max_files:return {'refs':refs,'blobs':blobs}
    plan=[]
    for f in src:
        if ecosystems.is_vendored_path(f.path,meta.get('ecosystem')) or ecosystems.is_generated_path(f.path,meta.get('ecosystem')):continue
        if f.status!='added':plan.append((commit.parent,f.old_path))
        if f.status!='removed':plan.append((commit.sha,f.path))
    # Empty plans must not reserve a pipe slot and evict another useful reader.
    # Keep the ordinary prefetch/empty-output behavior outside any lease.
    if not plan or reader_scope is None:return _read_source_plan(commit,plan)
    with reader_scope(commit.loader):
        return _read_source_plan(commit,plan)


def _read_source_plan(commit,plan):
    from app.backend.pipeline.ingest import codemine as cm
    refs,blobs={},{}
    if prefetch:=getattr(commit.loader,'prefetch',None):prefetch(plan)
    sources=dict.fromkeys(plan)
    source_oids,inventory={},{}
    if isinstance(commit.loader,cm._BatchedGitLoader) and not commit.loader.fallback:
        repository=commit.loader.repository
        for revision,path in sources:
            operations.check()
            source_oids[revision,path]=repository.tree(revision).get(path)
        # Inventory only this admitted plan. Missing metadata never replaces an
        # actual read; positive sizes belong to immutable exact blob IDs.
        oids=list(dict.fromkeys(oid for oid in source_oids.values() if oid))
        if oids:
            operations.check()
            from .repository_service import service
            rows=service(repository.path).info_many(oids)
            inventory={oid:rows[index] for index,oid in enumerate(oids)}
    for revision,path in sources:
        operations.check()
        if isinstance(commit.loader,cm._BatchedGitLoader) and not commit.loader.fallback:
            info=inventory.get(source_oids.get((revision,path)))
            if info is not None and info[2]>cm.MAX_FILE_BYTES:
                refs[revision+":"+path]={'excluded':'too_large'};continue
        data=commit.loader.content(revision,path)
        if data is None:refs[revision+":"+path]=None;continue
        if len(data)>cm.MAX_FILE_BYTES:
            refs[revision+":"+path]={'excluded':'too_large'};continue
        digest=hashlib.sha256(data).hexdigest()
        refs[revision+":"+path]=digest
        blobs.setdefault(digest,base64.b64encode(data).decode('ascii'))
    return {'refs':refs,'blobs':blobs}


def encode_result(result,row,gh):
    return {'schema':'mining-result/3','result':{f.name:getattr(result,f.name) for f in fields(result) if f.name!='commit'},
        'commit':encode_commit(result.commit,row,gh,profile=result.commit.loader.profile if isinstance(result.commit.loader,Sources) else None) if result.commit else None}


def decode_result(document,gh):
    from app.backend.pipeline.ingest import codemine as cm
    if document.get('schema')!='mining-result/3':raise ValueError("Unknown staged result schema")
    return cm.MineResult(commit=decode_commit(document['commit'],gh) if document['commit'] else None,**document['result'])


def source_fingerprint(root=None):
    # Context, attribution and fix-check changes invalidate earlier checkpoints too.
    root=Path(root) if root else Path(__file__).resolve().parents[1]
    digest=hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        digest.update(str(path.relative_to(root)).replace('\\','/').encode())
        digest.update(b'\0');digest.update(path.read_bytes());digest.update(b'\0')
    return digest.hexdigest()


def implementation_fingerprint(scope,max_files,max_functions):
    from app.backend.pipeline import funcsplit
    from app.backend.pipeline.ingest import codemine as cm,splitquality
    from .pipeline_spool import encoded
    return hashlib.sha256(encoded([3,scope,cm.EXTRACTION_VERSION,funcsplit.VERSION,
        funcsplit.parser_fingerprint(),max_files,max_functions,source_fingerprint(),asdict(splitquality.default_policy())])).hexdigest()


class StagedMiner:
    def __init__(self,gh,git,meta,max_files,max_functions,cpu_pool,counts,tick,*,root=None,
                 admit=None,progress=lambda state:None,should_stop=lambda:False,service_seconds=600):
        import uuid
        from .publication_process import PublicationProcess, enabled as publication_process_enabled, batch_size
        use_publication_process = publication_process_enabled()
        publication_batch_size = batch_size()
        from .disk_scanner import ScanProcess, enabled as disk_scanner_enabled
        use_disk_scanner = disk_scanner_enabled()
        from .acquisition_hints import enabled as selection_hints_enabled
        self.use_selection_hints = selection_hints_enabled()
        from app.backend import paths
        from app.backend.pipeline import campaign_ctx
        from .publication import Publisher
        from .pipeline_scheduler import Engine
        from .pipeline_spool import Spool
        self.gh,self.git,self.meta=gh,git,meta
        self.max_files,self.max_functions,self.cpu_pool=max_files,max_functions,cpu_pool
        self.should_stop=should_stop
        self.selection_hints=None
        self._selection_validate=None
        self._selection_window=()
        self.counts,self.tick,self.user_progress=counts,tick,progress
        from .acquisition_groups import Groups,git_group
        from .resource_control import active
        self.resources=active()
        from .mining_policy import acquisition_config
        self.acquisition_config=acquisition_config()
        from .acquisition_groups import _group_filter
        self.acquisition_config['git_filter']=_group_filter()
        group_acquire=(lambda rows,stop:git_group(self.git,rows,stop,hints=self._selection_lookup)) if self.use_selection_hints else (
            lambda rows,stop:git_group(self.git,rows,stop))
        self.groups=Groups(group_acquire,should_stop=should_stop,workers=4 if self.resources else 2)
        self.scope=campaign_ctx.current() or 'manual' 
        self.publisher=Publisher(self.scope,uuid.uuid4().hex)
        root=Path(root) if root else paths.interim_dir()/'acceleration'/'pipelines-v3'
        from .disk_budget import Budget
        from .cache import default
        from app.backend.pipeline.ingest import codemine as cm
        scanner_factory=(lambda root, baselines: ScanProcess(root, baselines,
            resources=self.resources, should_stop=should_stop)) if use_disk_scanner else None
        self.disk_budget=Budget(root,additional=(cm.REPOS,default().root),scanner_factory=scanner_factory,auto_limit=True)
        self.engine=Engine(Spool(root,scope=self.scope),{name:getattr(self,name) for name in ('acquire','parse','context','publish')},
            admit=admit,progress=self.report,should_stop=should_stop,failure=self.failed,
            service_seconds=service_seconds,recover=lambda task:self.publisher.receipt(task.publication_id),
            resources=self.resources, load_previous={'publish': not use_publication_process},
            limits=({**(self.resources.stage_limits() if self.resources else
                {'acquire':2,'parse':1,'context':2,'publish':1}), 'publish':publication_batch_size}
                if publication_batch_size > 1 else None))
        self.disk_budget.reclaim=self.engine.spool.reclaim_disposable
        self.publication_process = self.repository_facts = None
        if self.resources:self.resources.trimmers.append(self.trim_caches)
        try:
            from .repository_service import _trees
            self._tree_cache_before=_trees.limit
            _trees.resize(self.acquisition_config['tree_cache_bytes'])
            if use_publication_process:
                self.publication_process=PublicationProcess(self.publisher,self.engine.spool,
                    resources=self.resources,should_stop=should_stop)
                self.engine.before_dispatch=self.publication_process.register
                self.engine.after_dispatch=self.publication_process.skip
            from .repository_facts import RepositoryFacts, enabled as repository_facts_enabled
            self.repository_facts = RepositoryFacts(gh.s, should_stop=should_stop) if gh is not None and repository_facts_enabled() else None
            if self.repository_facts is not None:
                gh.repository_facts = gh.s.repository_facts = self.repository_facts
        except BaseException:
            self.close()
            raise

    def trim_caches(self,budget):
        from .repository_service import _blobs,_trees
        from .pipeline_spool import _parsed_cache
        from .resource_control import HEADROOM_BYTES,GROWTH_BYTES
        resources=getattr(self,'resources',None)
        if resources is not None:
            state=resources.refresh()
            if resources.reclaim_needed(budget):
                self._cache_pressure=True
                self.trim_recovery_available_bytes=HEADROOM_BYTES+GROWTH_BYTES
            elif state.get('available_bytes',0)>=HEADROOM_BYTES+GROWTH_BYTES:
                self._cache_pressure=False
                self.trim_recovery_available_bytes=0
        pressured=getattr(self,'_cache_pressure',False)
        ceiling=getattr(self,'acquisition_config',{}).get('tree_cache_bytes',16<<20)
        changed=False
        for cache,ceiling in ((_blobs,64<<20),(_trees,ceiling),(_parsed_cache,64<<20)):
            limit=1<<20 if pressured else min(ceiling,max(1<<20,budget//32))
            changed|=cache.limit!=limit
            cache.resize(limit)
        # Force the governor to resample released cache memory before it decides
        # whether a more expensive reader or worker retirement is still needed.
        return changed

    def report(self,state):
        from .metrics import current
        from .repository_service import _blobs,_trees
        from .pipeline_spool import _parsed_cache
        from .pipeline_scheduler import memory_snapshot
        disk_state=self.disk_budget.snapshot()
        scanner=self.disk_budget.scanner
        state={**state,'acquisition_config':getattr(self,'acquisition_config',{}),
               'source_cache_bytes':_blobs.size,'source_cache_limit_bytes':_blobs.limit,
               'tree_cache_bytes':_trees.size,'parsed_cache_bytes':_parsed_cache.size,'parsed_cache_limit_bytes':_parsed_cache.limit,
               'publication_writer':self.publication_process.snapshot() if self.publication_process else None,
               'numeric_request_limit_bytes':32<<20,'memory':self.resources.refresh() if self.resources else memory_snapshot(),'disk_budget':disk_state, 'disk_scanner':scanner.snapshot() if scanner is not None else None,
               **({'selection_hints':self.selection_hints.snapshot()} if self.selection_hints is not None else {})}
        current.pipeline(state)
        self.user_progress(state)
        self.tick()

    def _reader_scope(self,loader):
        from contextlib import nullcontext
        from .mining_policy import current as current_mining
        from .repository_service import service
        from app.backend.pipeline.ingest import codemine as cm
        loader=loader.base if isinstance(loader,Sources) else loader
        if current_mining().get('version')==4 and isinstance(loader,cm._GitLoader):
            return service(loader.d).reader_lease(resources=self.resources)
        return nullcontext()

    def acquire(self,row,previous,task):
        from app.backend.pipeline.ingest import codemine as cm
        from .mining_policy import current as current_mining
        if current_mining().get('version') == 4:
            self.groups.start(row)
            from .repository_service import service
            commit=cm.acquire_commit(row,self.gh,self.git,before_git=lambda:self.groups.wait(row),
                git_scope=lambda:service(self.git._repo(row)).reader_lease(resources=self.resources))
        else:
            self.groups.wait(row)
            commit=cm.acquire_commit(row,self.gh,self.git)
        document=encode_commit(commit,row,self.gh)
        document['sources']=prepare_sources(commit,row,row['_mining_meta'],self.max_files,
            reader_scope=self._reader_scope)
        return document

    def parse(self,row,document,task):
        from app.backend.pipeline.ingest import codemine as cm
        commit=decode_commit(document,self.gh)
        result=cm.build_pairs(row,commit,row['_mining_meta'],self.max_files,self.max_functions,cpu_pool=self.cpu_pool)
        return encode_result(result,row,self.gh)

    def context(self,row,document,task):
        from app.backend.pipeline.ingest import codemine as cm
        from app.backend.pipeline import security_snapshots,fsio
        from app.backend import paths
        from app.backend.pipeline import stopping
        result=decode_result(document,self.gh)
        if result.status=='mined' and result.commit:
            from .mining_policy import hydrate_context
            if hydrate_context() and any(p.get('language') in ('javascript','typescript','tsx','jsx') for p in result.pairs):
                operations.phase('hydrating_context')
                from dataclasses import replace
                loader=result.commit.loader.base if isinstance(result.commit.loader,Sources) else result.commit.loader
                if isinstance(loader,cm._GitLoader):
                    if not isinstance(loader,cm._BatchedGitLoader):
                        loader=cm._BatchedGitLoader(loader.d,loader)
                    cached=replace(result.commit,loader=loader)
                else:
                    cached=self.git.commit({**row,"sha":result.commit.sha})
                    if isinstance(cached.loader,cm._GitLoader) and not isinstance(cached.loader,cm._BatchedGitLoader):
                        cached=replace(cached,loader=cm._BatchedGitLoader(cached.loader.d,cached.loader))
                from .mining_policy import current as current_mining
                with self._reader_scope(cached.loader):
                    provider = None
                    if current_mining().get('version') == 4:
                        from .context_prefetch import prepare
                        provider = security_snapshots.HydrationSnapshots()
                        prepare(result.pairs, cached, provider=provider)
                    try:
                        hydrated=security_snapshots.hydrate_pairs(result.pairs,cached,**({'provider':provider} if provider is not None else {}))
                    except (stopping.Paused,operations.DeadlineExpired):raise
                    except Exception as error:
                        from .mining_policy import current as current_mining
                        if current_mining().get('version') == 4:raise
                        hydrated={'schema':'snapshot-hydration/1','fetched':0,'omissions':[{'reason':str(error)[:500]}]}
                receipt=paths.interim_dir()/'snapshot_hydration'/'v3'/(task.publication_id+'.json')
                fsio.write_json(receipt,hydrated)
                document['hydration_receipt']=str(receipt)
            try:
                operations.phase('checking_fix')
                from app.backend.pipeline.ingest import fixcheck
                result.fix_check=fixcheck.check_commit(row,result.commit.date)
            except (MemoryError,stopping.Paused,operations.DeadlineExpired):raise
            except Exception as error:
                from .failures import retryable,resource_pressure
                if resource_pressure(error) or retryable(error):raise
                result.fix_check=('unknown','check failed: '+str(error)[:80],None,None)
        output=encode_result(result,row,self.gh)
        if 'hydration_receipt' in document:output['hydration_receipt']=document['hydration_receipt']
        return output

    def publish(self,row,document,task):
        from collections import Counter
        from app.backend.pipeline.ingest import codemine as cm
        if self.publication_process is not None:
            receipt,delta=self.publication_process.publish(row,task)
            with self.engine.stats_lock:
                self.counts.update(delta)
            print(row['commit_key']+': published - '+str(delta.get('pairs',0))+' pairs (publication process)')
            return receipt
        result=decode_result(document,self.gh)
        delta=Counter()
        def check():
            task.operation.check()
            self.engine.spool.check(task.epoch)
        def write():
            delta.clear()
            cm._store_result(row,result,delta,row['commit_key'],log=lambda *a:None)
        receipt=self.publisher.publish(row['commit_key'],task.publication_id,write,check=check)
        with self.engine.stats_lock:
            self.counts.update(delta)
        print(row['commit_key']+': '+result.status+' - '+str(delta.get('pairs',0))+' pairs (published)')
        return receipt

    def failed(self,task,error):
        from app.backend.pipeline import codestore
        row=self.engine.spool.input(task.key)
        def write():
            from .failures import retryable
            from app.backend.pipeline import redact
            codestore.mark_fix_commit(row['commit_key'],'failed',error=redact.text(error)[:300],retryable=retryable(error))
            codestore.refresh_records(codestore.records_for_commit(row))
        receipt=self.publisher.failure(row['commit_key'],task.publication_id,write,check=lambda:self.engine.spool.check(task.epoch))
        if receipt is None:
            self.counts['failed']+=1
            from app.backend.pipeline import redact
            print(row['commit_key']+': failed - '+redact.text(error)[:180])
        return receipt

    def _selection_fence(self):
        from app.backend.pipeline import stopping
        stopping.check(self.should_stop)
        from .pipeline_spool import StaleAttempt
        owner=self.publisher.db['mining_attempts'].find_one({'_id':self.publisher.scope,
            'attempt':self.publisher.attempt,'generation':self.publisher.generation},{'_id':1})
        if owner is None:raise StaleAttempt('Selected acquisition attempt was superseded')

    def configure_selection(self,identities,*,selected_count,selector_fingerprint,validate):
        """Bind one selected-attempt manifest; it never replaces task selection."""
        if not self.use_selection_hints:return None
        if self.selection_hints is not None:raise ValueError('Selection hints are already configured')
        from .acquisition_hints import SelectionHints
        from .pipeline_spool import DiskPressure
        from app.backend.pipeline import stopping
        source=iter(identities)
        try:
            self._selection_fence()
            helper=SelectionHints(self.engine.spool,attempt=self.publisher.attempt,generation=self.publisher.generation,
                implementation=implementation_fingerprint(self.scope,self.max_files,self.max_functions),
                selector_fingerprint=selector_fingerprint,selected_count=selected_count)
            helper.populate(source,fence=self._selection_fence)
        except DiskPressure as error:
            paused=stopping.Paused(str(error))
            paused.reason={'code':'pipeline_disk_pressure','detail':str(error),'retryable':True}
            raise paused from error
        finally:
            if close:=getattr(source,'close',None):close()
        self._selection_validate=validate
        self.selection_hints=helper
        return helper.snapshot()

    def _selection_lookup(self,row,capacity):
        helper=self.selection_hints
        epoch=self.engine.epoch
        if helper is None or epoch is None:return []
        return helper.take((row['host'],row['owner'],row['repo']),self._selection_window,capacity,
            epoch=epoch,validate=self._selection_validate,fence=self._selection_fence)
    def run(self,rows,*,refill=None):
        fingerprint=implementation_fingerprint(self.scope,self.max_files,self.max_functions)
        if self.use_selection_hints:
            self._selection_window=tuple(row['commit_key'] for row in rows)
        self.groups.prepare(rows)
        if self.repository_facts is not None:
            self.repository_facts.prepare(rows)
        from .pipeline_spool import DiskPressure
        from app.backend.pipeline import stopping
        pending_raw=None
        def prepared_refill(capacity,active):
            nonlocal pending_raw
            if pending_raw is None:pending_raw=refill(min(64,capacity),active)
            if not isinstance(pending_raw,(list,tuple)) or len(pending_raw)>min(64,capacity):
                raise ValueError('Refill source exceeded its bounded descriptor capacity')
            return [{**row,'_mining_meta':self.meta.for_commit(row)} for row in pending_raw]
        def prepare_refill(prepared,active):
            live=[*active,*prepared]
            self.groups.update(live)
            if self.repository_facts is not None:self.repository_facts.update(live)
            if self.use_selection_hints:self._selection_window=tuple(row['commit_key'] for row in live)
        def refill_committed():
            nonlocal pending_raw
            pending_raw=None
        try:
            return self.engine.run([{**row,'_mining_meta':self.meta.for_commit(row)} for row in rows],
                implementation=fingerprint,**({'refill':prepared_refill,'prepare_refill':prepare_refill,
                    'refill_committed':refill_committed} if refill is not None else {}))
        except DiskPressure as error:
            paused=stopping.Paused(str(error))
            paused.reason={'code':'pipeline_disk_pressure','detail':str(error),'retryable':True}
            raise paused from error

    def close(self):
        from .repository_service import close_all
        if self.resources:self.resources.trimmers.remove(self.trim_caches)
        error=None
        cleanups = [self.groups.close, self.engine.close, close_all, self.disk_budget.close]
        if self.publication_process is not None:
            cleanups.insert(0,self.publication_process.close)
        if self.repository_facts is not None:
            cleanups.insert(0, self.repository_facts.close)
        for cleanup in cleanups:
            try:cleanup()
            except Exception as failure:
                if error is None:error=failure
        if self.repository_facts is not None:
            if getattr(self.gh, 'repository_facts', None) is self.repository_facts:
                del self.gh.repository_facts
            if getattr(self.gh.s, 'repository_facts', None) is self.repository_facts:
                del self.gh.s.repository_facts
        if getattr(self,'_tree_cache_before',None) is not None:
            from .repository_service import _trees
            _trees.resize(self._tree_cache_before)
            self._tree_cache_before=None
        if error:raise error
    def __enter__(self):
        from .disk_budget import use
        self._budget_context=use(self.disk_budget)
        self._budget_context.__enter__()
        self._token=_active.set(self)
        return self
    def __exit__(self,*args):
        try:self.close()
        finally:
            _active.reset(self._token)
            self._budget_context.__exit__(*args)


from contextvars import ContextVar
_active=ContextVar('staged_miner',default=None)


def active():return _active.get()
