"""Online execution planning. This module never draws examples or changes the recipe.

Memory predictions are upper envelopes, latency predictions are conservative local
interpolations. Unknown choices must fit an analytical bound before productive
exploration. No capacity probing by intentional allocation failure is performed.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Choice:
    size: int
    checkpointing: bool
    chunk: int


@dataclass
class Partition:
    examples: list
    choice: Choice
    seconds: float
    upper_bytes: float


class Controller:
    VERSION = 'resource-controller/1'

    def __init__(self, monitor, *, safe_batch=1, chunk=64, optimizer_reserve=0,
                 vocab_size=152064, hidden_size=3584, layers=28, mode='auto', clock=time.perf_counter):
        self.monitor, self.safe_batch, self.chunk = monitor, safe_batch, chunk
        self.optimizer_reserve = optimizer_reserve
        self.vocab, self.hidden, self.layers = vocab_size, hidden_size, layers
        self.mode, self.clock = mode, clock
        self.observations = []
        self.errors = []
        self.pressure = []
        self.max_size = 32
        self.policy_epoch = 0
        self.training_seconds = self.control_seconds = self.exploration_seconds = 0.0
        self.optimizer_ready = optimizer_reserve == 0
        self.last_decision = 'safe starting policy'
        self._last_policy = None
        self._last_external = None
        self._cached = None
        self.force_baseline = False
        self.baseline_schedule = []
        self.search_seconds = .002
        self.failed_shapes = []
        self.memory_admission = None

    @property
    def force_baseline(self):
        return self._force_baseline

    @force_baseline.setter
    def force_baseline(self,value):
        value=bool(value)
        if value!=getattr(self,'_force_baseline',False):
            self.policy_epoch+=1
            self._last_policy=None
        self._force_baseline=value

    def state_dict(self):
        return dict(schema=self.VERSION, hardware=self.monitor.hardware_signature,
                    observations=self.observations[-512:], errors=self.errors[-64:],
                    pressure=self.pressure[-64:], max_size=self.max_size,
                    policy_epoch=self.policy_epoch, training_seconds=self.training_seconds,
                    control_seconds=self.control_seconds, exploration_seconds=self.exploration_seconds,
                    baseline_schedule=self.baseline_schedule,force_baseline=self.force_baseline,
                    chunk=self.chunk,safe_batch=self.safe_batch,known_policy=self._cached,
                    failed_shapes=self.failed_shapes,search_seconds=self.search_seconds,
                    memory_admission=self.memory_admission)

    def load_state_dict(self, state):
        if not state or state.get('schema') != self.VERSION:
            return
        if state.get('hardware') != self.monitor.hardware_signature:
            self.observations, self.errors, self.pressure = [], [], []
            self.baseline_schedule, self.failed_shapes = [], []
            self._cached = self._last_policy = self._last_external = None
            self.training_seconds = self.control_seconds = self.exploration_seconds = 0.
            self.max_size, self.search_seconds = 32, .002
            self._force_baseline = False
            self.policy_epoch += 1
            self.last_decision = 'hardware changed; compatible checkpoint retained, measurements reset'
            return
        self.observations = list(state.get('observations', []))[-512:]
        self.errors = list(state.get('errors', []))[-64:]
        self.pressure = list(state.get('pressure', []))[-64:]
        self.max_size = int(state.get('max_size',32))
        self.policy_epoch = int(state.get('policy_epoch',0))
        self.baseline_schedule=list(state.get('baseline_schedule',[]))
        self._force_baseline=bool(state.get('force_baseline',False))
        self.chunk=int(state.get('chunk',self.chunk))
        self.safe_batch=int(state.get('safe_batch',self.safe_batch))
        self._cached=state.get('known_policy')
        self.failed_shapes=list(state.get('failed_shapes',[]))[-32:]
        self.search_seconds=float(state.get('search_seconds',.002))
        # Session budgets and ETA are deliberately not restored.

    def import_legacy(self, profile):
        if not profile:
            return
        self.baseline_schedule=list(profile.get('batch_schedule',[]))
        for row in profile.get('measurements', []):
            if row.get('state') != 'passed' or not row.get('seconds'):
                continue
            size = row.get('micro_batch_size',row.get('batch_size',1))
            length = row.get('max_sequence_tokens', row.get('sequence_tokens',4096))
            # A startup peak includes persistent model/optimizer memory. Keeping
            # the full peak as incremental is conservative until real work wins.
            self.observe(Choice(int(size),bool(row.get('gradient_checkpointing',True)),
                                int(profile.get('lm_loss_chunk_tokens',self.chunk))),
                         int(length),float(row['seconds']),int(row.get('peak_allocated_bytes',0)),
                         baseline=0, legacy=True)

    def budget(self, snapshot=None):
        s = snapshot or self.monitor.snapshot(force=True)
        free = max(0,int(s.get('free_bytes') or 0))
        allocated = int(s.get('allocated_bytes') or 0)
        reserved = int(s.get('reserved_bytes') or allocated)
        fragmented = int(s.get('inactive_split_bytes') or 0)
        cache_known = all(s.get(key) is not None for key in
                          ('allocated_bytes', 'reserved_bytes', 'inactive_split_bytes'))
        reusable = max(0,reserved-allocated-fragmented) if cache_known else 0
        external = max(0,int(s.get('total_bytes') or free)-free-reserved)
        if self._last_external is not None:
            self.pressure.append(max(0,external-self._last_external))
            self.pressure = self.pressure[-64:]
        self._last_external = external
        # Reserve allocator workspaces and observed upper-tail surprises. This
        # scales with measured errors/pressure rather than a fixed VRAM share.
        transient = max([64*1024**2, *self.errors[-64:]])
        transient += min(max(self.pressure[-64:] or [0]),free*.1)
        # cudaMemGetInfo free memory already excludes allocator reservations.
        # Inactive split blocks are excluded from reusable cache above. Charging
        # them again against driver-free memory double counts the same blocks.
        headroom = self.optimizer_reserve + transient
        budget = max(0,free+reusable-headroom) if s.get('free_bytes') is not None else 0
        self.memory_admission = dict(schema='resource-memory-admission/1',
            free_bytes=s.get('free_bytes'), allocated_bytes=s.get('allocated_bytes'),
            reserved_bytes=s.get('reserved_bytes'),
            non_reusable_cache_bytes=s.get('inactive_split_bytes'), reusable_cache_bytes=reusable,
            optimizer_reserve_bytes=self.optimizer_reserve,
            transient_reserve_bytes=transient, budget_bytes=budget,
            accounting='driver_free_plus_reusable_cache_minus_unallocated_reserves')
        return budget, headroom

    def _work(self, size, length, checkpointing):
        # Saved hidden states plus attention/workspace term (SDPA may use less).
        saved = size*length*self.hidden*2*self.layers*(3 if checkpointing else 12)
        attention = size*length*length*2*(1 if checkpointing else 4)
        return saved+attention

    def predict(self, choice, length):
        work = self._work(choice.size,length,choice.checkpointing)
        projection = min(choice.chunk,choice.size*length)*self.vocab*8
        compatible = [r for r in self.observations if r['checkpointing']==choice.checkpointing]
        upper = float(work+projection)
        seconds = max(.001,work/2e10+projection/5e9+.003)
        if not compatible and self.observations and not choice.checkpointing:
            seconds = self.predict(Choice(choice.size,True,choice.chunk),length)[0]*1.5
        known = False
        if compatible:
            nearest = min(compatible,key=lambda r:abs(math.log(max(1,r['length'])/max(1,length)))+
                          abs(math.log(r['size']/choice.size))+.2*abs(math.log(r['chunk']/choice.chunk)))
            ratio = work/max(1,self._work(nearest['size'],nearest['length'],nearest['checkpointing']))
            ratio_p = projection/max(1,min(nearest['chunk'],nearest['size']*nearest['length'])*self.vocab*8)
            exact = [r for r in compatible if r['size']==choice.size and r['chunk']==choice.chunk
                     and abs(r['length']/max(1,length)-1)<.15 and not r.get('legacy')]
            known = len(exact)>=3
            # A smaller exact vocabulary projection cannot increase memory.
            # Select the activation reference and its confidence independently
            # of chunk; otherwise shrinking chunk can switch to a shorter,
            # less familiar reference and paradoxically refuse a safe policy.
            memory_nearest = min(compatible, key=lambda r:
                abs(math.log(max(1,r['length'])/max(1,length)))+
                abs(math.log(r['size']/choice.size)))
            memory_exact = [r for r in compatible if r['size']==choice.size
                            and abs(r['length']/max(1,length)-1)<.15 and not r.get('legacy')]
            memory_known = len(memory_exact)>=3
            memory_ratio = work/max(1,self._work(memory_nearest['size'],memory_nearest['length'],
                                               memory_nearest['checkpointing']))
            def activation_bytes(row):
                return max(0,row['bytes']-min(row['chunk'],row['size']*row['length'])*self.vocab*8)
            # Retain linear activation extrapolation and all nearby measured
            # residual peaks, adding only the requested projection afterwards.
            upper = max(activation_bytes(memory_nearest)*memory_ratio+projection,
                        upper if not memory_known else 0)
            if memory_exact:
                upper = max(upper, max(activation_bytes(r)+projection for r in memory_exact[-8:]))
            if exact:
                seconds = sum(r['seconds'] for r in exact[-8:])/len(exact[-8:])
                if not known:
                    seconds *= 1.15
            else:
                # Keep measured policy until productive evidence establishes an
                # alternative. Account for fixed launch cost when changing size.
                seconds = nearest['seconds']*(.25+.75*max(ratio,ratio_p))
            upper *= 1.05 if memory_known else 1.3
        for failure in self.failed_shapes:
            if (length>=failure['length'] and choice.size>=failure['size']
                and choice.chunk>=failure['chunk'] and choice.checkpointing==failure['checkpointing']):
                upper=max(upper,failure['required_bytes'])
        return seconds, upper+max(self.errors[-32:] or [0]), known

    def admit(self, choice, length, snapshot=None):
        budget,_ = self.budget(snapshot)
        return choice.size <= self.max_size and self.predict(choice,length)[1] <= budget

    def observe(self, choice, length, seconds, peak_bytes, *, baseline=0, legacy=False):
        if seconds <= 0 or not math.isfinite(seconds):
            return
        used = max(0,peak_bytes-baseline)
        if self.observations:
            predicted = self.predict(choice,length)[1]
            self.errors.append(max(0,used-predicted))
            self.errors = self.errors[-64:]
        self.observations.append(dict(size=choice.size,checkpointing=choice.checkpointing,
                                      chunk=choice.chunk,length=length,seconds=seconds,bytes=used,legacy=legacy))
        self.observations = self.observations[-512:]
        if not legacy:
            self.training_seconds += seconds
            matches=[r for r in self.observations if not r.get('legacy') and r['size']==choice.size
                     and r['checkpointing']==choice.checkpointing and r['chunk']==choice.chunk
                     and abs(r['length']/max(1,length)-1)<.15]
            if len(matches)>=3:
                score=max(r['seconds'] for r in matches[-8:])/choice.size
                row=dict(length=max(r['length'] for r in matches[-8:]),size=choice.size,
                         checkpointing=choice.checkpointing,chunk=choice.chunk,seconds_per_example=score)
                policy=list(self._cached or [])
                near=[r for r in policy if abs(r['length']/max(1,length)-1)<.15]
                if not near or score<min(r.get('seconds_per_example',float('inf')) for r in near)*.97:
                    policy=[r for r in policy if r not in near]+[row]
                    self._cached=policy[-64:]

    def failed(self, choice, length):
        budget,_=self.budget()
        self.failed_shapes.append(dict(length=length,size=choice.size,chunk=choice.chunk,
                                       checkpointing=choice.checkpointing,required_bytes=budget*1.25))
        self.failed_shapes=self.failed_shapes[-32:]
        self.max_size = min(self.max_size,max(1,choice.size//2))
        self.chunk = max(1,min(self.chunk,choice.chunk//2))
        self.policy_epoch += 1
        self.last_decision = 'unexpected allocation failure; retry same optimizer batch with smaller partition'

    def committed(self, records):
        """Keep the last successful execution policy when search credit is low."""
        # The temporary retry ceiling belongs to the failed transaction, not to
        # every future length and future free-memory reading. Failed-shape
        # memory envelopes retain the evidence and permit resource recovery.
        self.max_size=32

    def _fallback(self, examples, length, budget, predict, baseline):
        out=[]; offset=0; n=len(examples)
        while offset<n:
            limit=self.safe_batch; cp=True; chunk=self.chunk
            if self.baseline_schedule:
                limit=next((r['micro_batch_size'] for r in self.baseline_schedule
                            if length(examples[offset])<=r['max_sequence_tokens']),self.safe_batch)
            if self._cached and not baseline and self.mode!='observe':
                rows=[r for r in self._cached if r['length']>=length(examples[offset])]
                if rows:
                    row=min(rows,key=lambda r:r['length'])
                    limit,cp,chunk=row['size'],row['checkpointing'],row['chunk']
            for size in range(min(limit,self.max_size,n-offset),0,-1):
                end_length=length(examples[offset+size-1])
                if self.baseline_schedule and (baseline or not self._cached):
                    cap=next((r['micro_batch_size'] for r in self.baseline_schedule
                              if end_length<=r['max_sequence_tokens']),self.safe_batch)
                    if size>cap:
                        continue
                choice=Choice(size,cp,chunk)
                seconds,upper,_=predict(choice,end_length)
                if upper>budget and (not cp or chunk!=self.chunk) and size<=self.safe_batch:
                    choice=Choice(size,True,self.chunk)
                    seconds,upper,_=predict(choice,end_length)
                if upper>budget and size==1:
                    # Reduce the exact vocabulary projection before refusing a
                    # single example. This preserves its tokens and objective.
                    reduced=min(choice.chunk,self.chunk)
                    while upper>budget and reduced>1:
                        reduced=max(1,reduced//2)
                        choice=Choice(1,True,reduced)
                        seconds,upper,_=predict(choice,end_length)
                    if upper<=budget and reduced<self.chunk:
                        self.chunk=reduced
                        self.policy_epoch+=1
                if upper<=budget:
                    out.append(Partition(examples[offset:offset+size],choice,seconds,upper))
                    offset+=size
                    break
            else:
                raise MemoryError('No predicted-safe single-example partition fits live GPU headroom')
        return out

    def plan(self, examples, *, length, baseline=False):
        began = self.clock()
        examples = sorted(examples,key=length)
        n = len(examples)
        snapshot = self.monitor.snapshot(force=True)
        budget,headroom = self.budget(snapshot)
        # DP is bounded to the optimizer batch (normally 32), all integer cuts.
        costs, plans = [float('inf')]*(n+1), [None]*(n+1)
        costs[0],plans[0] = 0,[]
        baseline = baseline or self.force_baseline
        credit=.02*self.training_seconds-self.control_seconds-self.exploration_seconds
        budget_available = bool(self.observations) and credit>=self.search_seconds*1.5
        explore = budget_available and self.optimizer_ready and not baseline and self.mode=='auto'
        cache={}
        def predict(choice, seq):
            key=(choice,seq)
            if key not in cache:
                cache[key]=self.predict(choice,seq)
            return cache[key]
        if not budget_available or baseline or self.mode=='observe':
            # Never spend repeatedly on search before enough productive work
            # has earned control credit. Memory admission still runs live.
            out=self._fallback(examples,length,budget,predict,baseline)
            self.control_seconds+=self.clock()-began
            self.last_decision=('comparison retained baseline' if baseline else 'control budget replenishing; safe admitted partition')
            return out
        for end in range(1,n+1):
            if self.clock()-began>credit*.8:
                out=self._fallback(examples,length,budget,predict,baseline)
                elapsed=self.clock()-began
                self.search_seconds=max(self.search_seconds,elapsed)
                self.control_seconds+=elapsed
                self.last_decision='search deadline reached; retained successful admitted policy'
                return out
            for size in range(1,min(end,self.max_size)+1):
                start=end-size
                if plans[start] is None:
                    continue
                seq=length(examples[end-1])
                for cp in ((True,False) if explore else (True,)):
                    for chunk in (sorted({self.chunk,max(16,self.chunk//2),self.chunk*2}) if explore else (self.chunk,)):
                        choice=Choice(size,cp,chunk)
                        seconds,upper,known=predict(choice,seq)
                        if upper > budget:
                            continue
                        if baseline or self.mode=='observe' or not budget_available or not self.optimizer_ready:
                            if size > self.safe_batch or not cp or chunk!=self.chunk:
                                continue
                        mode_known = any(r['checkpointing']==cp and r['chunk']==chunk for r in self.observations)
                        mode_samples=sum(r['checkpointing']==cp and r['chunk']==chunk and not r.get('legacy') for r in self.observations)
                        if (cp is not True or chunk!=self.chunk) and mode_samples<3:
                            continue
                        # The primary tuning dimension is also exploration.
                        # Unfamiliar integer sizes are considered for prediction,
                        # but only one budgeted productive trial may use them.
                        safe_start=(cp and chunk==self.chunk and size<=self.safe_batch)
                        legacy_safe=any(r.get('legacy') and r['size']>=size and r['length']>=seq
                                        and r['checkpointing']==cp and r['chunk']==chunk for r in self.observations)
                        if not known and not safe_start and not legacy_safe:
                            continue
                        if not mode_known and self.observations:
                            # Unknown modes get at most one small productive
                            # trial, charged by predicted excess over baseline.
                            reference = self.predict(Choice(size,True,self.chunk),seq)[0]
                            excess = max(.001, seconds-reference)
                            if (size>min(self.safe_batch,2) or
                                self.control_seconds+self.exploration_seconds+excess > .02*self.training_seconds):
                                continue
                            seconds *= 1.15
                        cost=costs[start]+seconds
                        if cost<costs[end]:
                            costs[end]=cost
                            plans[end]=plans[start]+[Partition(examples[start:end],choice,seconds,upper)]
        elapsed=self.clock()-began
        self.search_seconds=.8*self.search_seconds+.2*elapsed
        self.control_seconds += elapsed
        if plans[n] is None:
            # Search considers a bounded set of faster chunks. A refusal there
            # is not proof that the existing exact, smaller-chunk path fails.
            plan = self._fallback(examples, length, budget, predict, baseline=True)
            self.last_decision = 'dynamic search exhausted headroom; admitted exact minimum-chunk fallback'
            return plan
        plan=plans[n]
        exploration_started=self.clock()
        if explore and plan:
            candidates=[]
            for size in range(1,min(n,self.max_size)+1):
                if self.clock()-began>credit*.9:
                    break
                for cp in (True,False):
                    for chunk in sorted({self.chunk,max(16,self.chunk//2),self.chunk*2}):
                        candidate=Choice(size,cp,chunk)
                        seconds,upper,known=predict(candidate,length(examples[size-1]))
                        if not known and upper<=budget:
                            candidates.append((seconds/size,candidate,seconds,upper))
            # Optimistic improvement is only a reason to collect evidence, never
            # a reason to replace the whole update with an unmeasured policy.
            for _,candidate,seconds,upper in sorted(candidates,key=lambda x:x[0]):
                size=candidate.size
                if size==n:
                    continue
                tail=[]; consumed=0; split_cost=0.
                for p in plan:
                    end=consumed+len(p.examples)
                    if end>size:
                        remaining=p.examples[max(0,size-consumed):]
                        choice=Choice(len(remaining),p.choice.checkpointing,p.choice.chunk)
                        ts,tu,_=predict(choice,length(remaining[-1]))
                        if tu>budget:
                            tail=[]; break
                        tail.append(Partition(remaining,choice,ts,tu))
                        if consumed<size:
                            split_cost=ts
                    consumed=end
                debit=seconds+split_cost
                if tail and self.control_seconds+self.exploration_seconds+debit<=.02*self.training_seconds:
                    plan=[Partition(examples[:size],candidate,seconds,upper)]+tail
                    self.exploration_seconds+=debit
                    break
        self.control_seconds+=self.clock()-exploration_started
        policy=tuple(sorted({(p.choice.checkpointing,p.choice.chunk) for p in plan}))
        if self._last_policy is not None and policy!=self._last_policy:
            self.policy_epoch+=1
        self._last_policy=policy
        self.last_decision = ('observe safe profile' if self.mode=='observe' else 'minimum predicted latency')+f'; partitions={[p.choice.size for p in plan]}; headroom={int(headroom)}'
        return plan
