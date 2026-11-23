"""Transactional optimizer batches for the online CUDA execution controller."""
from __future__ import annotations

import copy
import gc
import json
import math
import time
from dataclasses import asdict

import torch

from training.finetune import checkpoint as ck
from training.finetune.resource_controller import Controller
from training.finetune.throughput import _checkpointing


def configure(trainer, *, mode='auto', log=lambda message:None):
    if mode not in ('auto','observe','off'):
        raise ValueError('resource-controller must be auto, observe, or off')
    loss=trainer.loss_cfg
    eligible=(trainer.backend.name=='cuda' and not trainer.pack and loss.decision_weight==0
              and not loss.pair_enabled and not loss.invariance_enabled and loss.lm_pair_weight==0
              and len({e.weight for e in trainer.dataset._ex.values()})<=1)
    if mode=='off' or not eligible:
        return None
    from training.finetune.resource_metrics import ResourceMonitor
    from training.finetune.staging import BatchStager
    from training.finetune.runtime_resources import resource_paths
    paths=resource_paths(trainer.run_dir.parent,trainer.run_dir)
    monitor=ResourceMonitor(trainer.backend,storage_paths=paths)
    cfg=trainer.model.config
    reserve=sum(p.numel()*p.element_size()*2 for p in trainer.params)
    automatic=getattr(trainer.training,'micro_batch_tuning','off')=='auto'
    c=Controller(monitor,safe_batch=1 if automatic else trainer.micro_batch_size,
                 chunk=min(64,trainer.chunk) if automatic else trainer.chunk,
                 optimizer_reserve=reserve,vocab_size=cfg.vocab_size,hidden_size=cfg.hidden_size,
                 layers=cfg.num_hidden_layers,mode=mode)
    c.load_state_dict(getattr(trainer,'_controller_checkpoint',None))
    old=getattr(trainer,'_throughput_checkpoint',None)
    old_path=trainer.run_dir/'throughput_profile.json'
    if not old and old_path.exists():
        old=json.loads(old_path.read_text(encoding='utf-8'))
    if not c.observations and old:
        identity=old.get('identity',{})
        # Old evidence is useful only on matching hardware; model continuation
        # remains valid independently of the performance profile.
        old_name=identity.get('gpu',identity.get('gpu_name',identity.get('name')))
        name=c.monitor.hardware_signature.get('device_name')
        matching=(identity.get('hardware')==c.monitor.hardware_signature if 'hardware' in identity else
                  old_name==name and identity.get('capacity_bytes')==c.monitor.hardware_signature.get('total_bytes'))
        if matching:
            c.import_legacy(old)
            if old.get('batch_schedule'):
                c.safe_batch=min(int(x['micro_batch_size']) for x in old['batch_schedule'])
    trainer.controller=c
    trainer.resource_monitor=monitor
    trainer.stager=BatchStager(trainer.backend,trainer.pad_id,max_host_bytes=64*1024**2,enabled=False)
    if trainer.analytics:
        trainer.analytics.enable_async()
    log(f'Online resource controller {mode}: same effective batch {trainer.training.effective_sequence_batch_size}; no startup allocation probes')
    return c.state_dict()


def establish_optimizer(trainer):
    """Allocate the exact native AdamW state without taking a dummy update."""
    c=trainer.controller
    if type(trainer.optimizer) is torch.optim.AdamW:
        for group in trainer.optimizer.param_groups:
            for p in group['params']:
                state=trainer.optimizer.state[p]
                if state:
                    continue
                device=p.device if group.get('capturable') or group.get('fused') else torch.device('cpu')
                state['step']=torch.zeros((),dtype=torch.float32,device=device)
                state['exp_avg']=torch.zeros_like(p,memory_format=torch.preserve_format)
                state['exp_avg_sq']=torch.zeros_like(p,memory_format=torch.preserve_format)
                if group.get('amsgrad'):
                    state['max_exp_avg_sq']=torch.zeros_like(p,memory_format=torch.preserve_format)
        c.optimizer_ready=True
        c.optimizer_reserve=0
    elif trainer.optimizer.state:
        c.optimizer_ready=True
        c.optimizer_reserve=0


def transfer_diagnostics(tree):
    """Copy all detached diagnostic values with one device-to-host transfer."""
    tensors=[]
    def encode(value):
        if torch.is_tensor(value):
            index=len(tensors); tensors.append(value.detach().float().reshape(-1))
            return ('tensor',index,tuple(value.shape))
        if isinstance(value,dict):
            return ('dict',[(k,encode(v)) for k,v in value.items()])
        if isinstance(value,list):
            return ('list',[encode(v) for v in value])
        return ('value',value)
    structure=encode(tree)
    flat=torch.cat(tensors).cpu().tolist() if tensors else []
    slices=[]; pos=0
    for tensor in tensors:
        slices.append(flat[pos:pos+tensor.numel()]); pos+=tensor.numel()
    def decode(node):
        tag=node[0]
        if tag=='tensor':
            return slices[node[1]][0] if not node[2] else slices[node[1]]
        if tag=='dict':
            return {k:decode(v) for k,v in node[1]}
        if tag=='list':
            return [decode(v) for v in node[1]]
        return node[1]
    return decode(structure)


class PressureChanged(RuntimeError):
    pass


class ResourceLimit(RuntimeError):
    """A pre-update resource stop; the current optimizer boundary is consistent."""
    def __init__(self, report):
        self.report = report
        super().__init__('Cannot fit the same examples safely; last consistent checkpoint retained: '
                         + str(report.get('failure_detail', report.get('reason', 'resource limit'))))


def _persist(t, name, report):
    path = t.run_dir / name
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(report, indent=2, default=str) + '\n', encoding='utf-8')
        temporary.replace(path)
    except OSError as error:
        t.meta['resource_diagnostic_write_error'] = str(error)


def _resource_stop(t, failure_type, detail, retries, groups):
    choice = getattr(t, '_active_choice', None)
    report = {'schema': 'training-resource-stop/1', 'reason': 'resource_limit',
              'failure_type': failure_type, 'failure_detail': detail, 'retry_count': retries,
              'consistent_step': t.step, 'optimizer_updated_in_failed_attempt': False,
              'choice': asdict(choice) if choice is not None else None,
              'sequence_tokens': getattr(t, '_active_length', None),
              'optimizer_batch_view_ids': [e.view_id for group in groups for e in group],
              'resources': t.controller.monitor.snapshot(force=True)}
    _persist(t, 'resource_failure.json', report)
    _persist(t, 'resource_controller.json', t.controller.state_dict())
    return ResourceLimit(report)


def _attempt(t, groups, n):
    c=t.controller
    examples=[e for g in groups for e in g]
    began=time.perf_counter()
    plan=c.plan(examples,length=lambda e:len(e.encoded.input_ids),baseline=getattr(t,'_controller_baseline',False))
    planning=time.perf_counter()-began
    records=[]
    control_started=time.perf_counter()
    baseline_allocated=int(c.monitor.snapshot(force=True).get('allocated_bytes') or 0)
    c.control_seconds+=time.perf_counter()-control_started
    cuda=t.backend.name=='cuda'
    stager=getattr(t,'stager',None)
    if stager:
        host=c.monitor.snapshot(force=True)
        if 'host_available_bytes' in host:
            # Optional lookahead must give way to host pressure. A missing
            # counter grants no pinned-memory budget. Direct collation remains
            # available and the same optimizer batch is still executed.
            available=host.get('host_available_bytes')
            headroom=max(0,int(available or 0)-512*1024**2)
            stager.max_host_bytes=min(stager.host_budget_cap,headroom//16)
            if stager.max_host_bytes<1024**2:
                stager.max_host_bytes=0
                stager.enabled=False
        if stager.max_host_bytes<=0:
            stager=None
    tickets=[]
    ticket=None
    if stager:
        ticket=stager.submit(plan[0].examples); tickets.append(ticket)
    for index,p in enumerate(plan):
        t._active_choice=p.choice
        t._active_length=max(len(e.encoded.input_ids) for e in p.examples)
        control_started=time.perf_counter()
        snapshot=c.monitor.snapshot(force=True)
        if not c.admit(p.choice,t._active_length,snapshot):
            raise PressureChanged('live GPU budget fell before micro-batch')
        if bool(getattr(t.model,'is_gradient_checkpointing',False))!=p.choice.checkpointing:
            _checkpointing(t.model,p.choice.checkpointing)
        t.chunk=p.choice.chunk
        if hasattr(c.monitor,'reset_peak'):
            c.monitor.reset_peak()
        c.control_seconds+=time.perf_counter()-control_started
        started=time.perf_counter()
        if t.analytics:
            t.analytics.emit('micro_batch_started',step=t.step+1,micro_batch=index,micro_batches=len(plan),
                             sequences=len(p.examples),controller_mode=c.mode,policy_epoch=c.policy_epoch)
        timing={}
        if stager:
            t._prepared_batch,timing=stager.acquire(ticket)
            t._loss_metadata=timing.pop('loss_metadata',None)
            if index+1<len(plan) and stager.enabled:
                next_ticket=stager.submit(plan[index+1].examples); tickets.append(next_ticket)
            else:
                next_ticket=None
        events=None
        if cuda:
            events=(torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True))
            events[0].record(torch.cuda.current_stream(t.backend.device))
        loss,parts=t.compute_loss(p.examples)
        t._prepared_batch=None
        t._loss_metadata=None
        scale=len(p.examples)/n
        (t.scaler.scale(loss*scale) if t.scaler else loss*scale).backward()
        if events:
            events[1].record(torch.cuda.current_stream(t.backend.device))
        control_started=time.perf_counter()
        after=c.monitor.snapshot(force=True)
        c.control_seconds+=time.perf_counter()-control_started
        records.append(dict(group=p.examples,choice=p.choice,length=t._active_length,loss=loss.detach(),parts=parts,
                            scale=scale,events=events,seconds=time.perf_counter()-started,timing=timing,ticket=ticket,
                            peak=int(after.get('peak_allocated_bytes') or after.get('allocated_bytes') or 0),
                            baseline=baseline_allocated,prediction=p.seconds))
        if stager:
            # With prefetch disabled, release a consumed ticket before the next
            # allocation. CUDA records keep no pinned tensors themselves.
            if not stager.enabled:
                stager.resolve(ticket)
            ticket=next_ticket
            if index+1<len(plan) and ticket is None:
                ticket=stager.submit(plan[index+1].examples); tickets.append(ticket)
    return records,planning,tickets


def optimizer_step(t):
    c=t.controller
    step_started=time.perf_counter()
    control_before=c.control_seconds
    # Recovery point precedes draws and state initialization. Optimizer failure
    # may partially mutate weights; never save those weights or blindly retry.
    if ck.latest_checkpoint(t.checkpoint_dir) is None and not getattr(t,'_benchmark_active',False):
        t.save_checkpoint()
    try:
        establish_optimizer(t)
    except torch.cuda.OutOfMemoryError:
        t.optimizer.state.clear()
        gc.collect()
        if t.backend.name=='cuda':
            torch.cuda.empty_cache()
        path=ck.latest_checkpoint(t.checkpoint_dir)
        if path and not getattr(t,'_benchmark_active',False):
            t.resume(path)
        raise RuntimeError('Optimizer state allocation failed; last consistent checkpoint retained') from None
    sampler_before=copy.deepcopy(t.sampler.state_dict())
    groups,n=t._draw_groups()
    rng=t.backend.rng_state()
    initial_step=t.step
    force_baseline_before=c.force_baseline
    t.model.train(); t.head.train()
    t._defer_metrics=True
    retries=0
    records=None
    try:
        while True:
            t.optimizer.zero_grad(set_to_none=True)
            failure=None
            failure_detail=None
            t._active_choice=None
            t._active_length=None
            try:
                records,planning,tickets=_attempt(t,groups,n)
            except (torch.cuda.OutOfMemoryError,PressureChanged,MemoryError) as error:
                failure=type(error).__name__
                failure_detail=str(error)
            if failure is None:
                break
            # Outside except: traceback no longer retains failed graphs.
            t._prepared_batch=None
            t.optimizer.zero_grad(set_to_none=True)
            if getattr(t,'stager',None):
                t.stager.cancel_all()
            records=None
            gc.collect()
            if t.backend.name=='cuda':
                torch.cuda.empty_cache()
            t.backend.restore_rng(rng)
            choice=getattr(t,'_active_choice',None)
            if choice is None or (choice.size==1 and choice.chunk<=1) or retries>=8:
                t.sampler.load_state_dict(sampler_before)
                raise _resource_stop(t, failure, failure_detail, retries, groups)
            if failure == 'OutOfMemoryError':
                c.failed(choice,t._active_length)
            else:
                # Admission refusals are not observed allocation failures.
                c.chunk=max(1,min(c.chunk,choice.chunk//2))
                c.force_baseline=True
            retries+=1
            if t.analytics:
                t.analytics.emit('controller_retry',step=t.step+1,retry_count=retries,reason=failure)
        if t.scaler:
            t.scaler.unscale_(t.optimizer)
        norm=torch.nn.utils.clip_grad_norm_(t.params,t.training.max_grad_norm)
        t.backend.synchronize()
        prepared=[]
        for rec in records:
            events=rec.pop('events')
            if events:
                rec['seconds']=events[0].elapsed_time(events[1])/1000
            if getattr(t,'stager',None) and t.stager.enabled:
                rec['timing'].update(t.stager.resolve(rec['ticket'],wait=False))
            prepared.append({'loss':rec['loss'],'parts':rec['parts']})
        diagnostics=transfer_diagnostics({'micro':prepared,'grad_norm':norm})
        if not math.isfinite(diagnostics['grad_norm']) or any(not math.isfinite(r['loss']) for r in diagnostics['micro']):
            raise FloatingPointError(f'Non-finite training value at optimizer step {t.step+1}')
        lr=t.optimizer.param_groups[0]['lr']
        optimizer_started=time.perf_counter()
        optimizer_failure=None
        try:
            if t.scaler:
                t.scaler.step(t.optimizer); t.scaler.update()
            else:
                t.backend.optimizer_step(t.optimizer)
            t.backend.synchronize()
        except BaseException as error:
            optimizer_failure=f'{type(error).__name__}: {error}'
        if optimizer_failure:
            if not getattr(t,'_benchmark_active',False):
                path=ck.latest_checkpoint(t.checkpoint_dir)
                t.optimizer.zero_grad(set_to_none=True)
                t.optimizer.state.clear()
                gc.collect()
                if t.backend.name=='cuda':
                    torch.cuda.empty_cache()
                if path:
                    t.resume(path)
                t.stopped='optimizer_failure'
            raise RuntimeError('Optimizer failed; restored last consistent checkpoint and stopped without retry: '+optimizer_failure)
        optimizer_seconds=time.perf_counter()-optimizer_started
        t.scheduler.step()
        c.optimizer_ready=True; c.optimizer_reserve=0
        tokens=sum(r['parts']['tokens'] for r in diagnostics['micro'])
        t.counters['step']+=1; t.counters['sequences']+=n; t.counters['tokens']+=tokens
        aggregate={'loss':0.,'lm_loss':0.}; fields={}
        preparation=transfers=compute=0.
        logging_started=time.perf_counter()
        for i,(r,d) in enumerate(zip(records,diagnostics['micro'])):
            parts=d['parts']; group=r['group']; scale=r['scale']
            aggregate['loss']+=d['loss']*scale; aggregate['lm_loss']+=parts['lm']*scale
            for k,v in parts['fields'].items():
                fields[k]=fields.get(k,0.)+v*scale
            control_started=time.perf_counter()
            c.observe(r['choice'],r['length'],r['seconds'],r['peak'],baseline=r['baseline'])
            if not any(o['checkpointing']==r['choice'].checkpointing and o['chunk']==r['choice'].chunk
                       and o['size']==r['choice'].size and abs(o['length']/max(1,r['length'])-1)<.15
                       for o in c.observations[:-1]):
                c.exploration_seconds+=max(0,r['seconds']-r['prediction'])
            c.control_seconds+=time.perf_counter()-control_started
            timing=r['timing']
            preparation+=max(timing.get('collation_seconds',0),timing.get('input_wait_seconds',0))
            transfers+=timing.get('h2d_seconds',0); compute+=r['seconds']
            if t.analytics:
                t.analytics.emit('micro_batch',step=t.step,micro_batch=i,micro_batches=len(records),
                                 device_batch_size=len(group),examples=[e.view_id for e in group],
                                 loss=d['loss'],lm_loss=parts['lm'],fields=parts['fields'],
                                 per_example_completion_loss=parts['per_example_completion_loss'],
                                 components={k:parts[k] for k in ('decision','rank','invariance')},
                                 weights=[e.weight for e in group],accumulation_scale=scale,sequences=len(group),
                                 tokens=parts['tokens'],sequence_lengths=[len(e.encoded.input_ids) for e in group],
                                 prompt_tokens=sum(e.encoded.prompt_tokens for e in group),
                                 padding_tokens=parts.get('padding_tokens',0),forward_backward_seconds=r['seconds'])
        logging=time.perf_counter()-logging_started
        c.committed(records)
        seconds=time.perf_counter()-step_started
        input_tokens=sum(len(e.encoded.input_ids) for g in groups for e in g)
        rec=dict(step=t.step,**aggregate,grad_norm=diagnostics['grad_norm'],lr=lr,tokens=tokens,sequences=n,
                 family_pass=t.sampler.family_passes,fields=fields,step_seconds=seconds,
                 supervised_tokens_per_second=tokens/max(seconds,1e-9),input_tokens_per_second=input_tokens/max(seconds,1e-9),
                 optimizer_seconds=optimizer_seconds,preparation_seconds=preparation,transfer_seconds=transfers,
                 compute_seconds=compute,logging_seconds=logging,controller_seconds=c.control_seconds-control_before,
                 controller_mode=c.mode,policy_epoch=c.policy_epoch,retry_count=retries)
        if getattr(t,'stager',None) and c.mode=='auto' and t.step>=2:
            t.stager.enabled=(preparation+transfers)>seconds*.05
        if t.analytics:
            t.analytics.emit('optimizer_step',**rec,gradient_clip_threshold=t.training.max_grad_norm,counters=dict(t.counters))
            t.analytics.emit('resource',step=t.step,**c.monitor.snapshot(force=True))
            t.analytics.emit('controller',step=t.step,policy_epoch=c.policy_epoch,controller_mode=c.mode,
                             decision=c.last_decision,control_seconds=c.control_seconds,
                             exploration_seconds=c.exploration_seconds,training_seconds=c.training_seconds)
        t.history.append(rec)
        with (t.run_dir/'metrics.jsonl').open('a',encoding='utf-8') as fh:
            fh.write(json.dumps(rec)+'\n')
        return rec
    except BaseException:
        if t.step==initial_step and t.stopped!='optimizer_failure':
            t.sampler.load_state_dict(sampler_before)
        t.optimizer.zero_grad(set_to_none=True)
        raise
    finally:
        c.force_baseline=force_baseline_before
        _persist(t, 'resource_controller.json', c.state_dict())
        t._defer_metrics=False; t._prepared_batch=None; t._loss_metadata=None
        if getattr(t,'stager',None):
            t.stager.cancel_all()
