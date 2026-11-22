"""Training sampling with resumable source-record coverage. No torch.

Mined source objectives visit each training record once per shuffled pass, then
choose an available objective and source view. Coverage takes precedence over
soft exposure caps. Historical security tasks retain family-uniform sampling.

* **Tasks** are drawn by the contract's ``sampling.task_shares``, renormalized over the tasks that have examples (an
  unavailable share is reported, never hidden); a missing core task fails construction.
* **Decision pools** (supported, scoped negative, insufficient context) follow ``decision_pool_targets`` within a task,
  renormalized over the pools that exist.
* **Families** (issue groups) are the unit of exposure and are drawn uniformly, so a family with many views is not drawn
  more often than one with a single view; **views** are uniform inside the family. A *family pass* is the number of draws
  divided by the number of families.
* **Exposure caps**: after a warm-up, no repository (and no single family) may take more than its share of all draws, so a
  monorepo cannot dominate. A cap that cannot be met (everything left belongs to capped owners) is relaxed for that draw
  and counted, never silently ignored.
* Rows without weight (unreviewed) never enter.

The state (random generator and every counter) is plain JSON and is saved with a checkpoint: a resumed sampler continues with
exactly the draws an uninterrupted one would have made. It refuses a state saved over other data.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass

from training.finetune import planning


@dataclass(frozen=True)
class Item:
    view_id: str
    task: str
    pool: str
    family_id: str
    repo: str
    weight: float = 1.0
    record_id: str | None = None


class Sampler:
    def __init__(self, items, *, task_shares, pool_targets, seed: int = 17, family_cap: float = 0.05,
                 repo_cap: float = 0.10, cap_warmup: int = 200, on_draw=None, task_cycle=None, task_counts=None, pairs=None, epoch_plan=None):
        elig = sorted((i for i in items if i.weight > 0), key=lambda i: i.view_id)
        counts: dict[str, Counter] = {}
        self._index: dict[str, dict[str, tuple[list[str], dict[str, list[Item]]]]] = {}
        for it in elig:
            counts.setdefault(it.task, Counter())[it.pool] += 1
            fams = self._index.setdefault(it.task, {}).setdefault(it.pool, ([], {}))
            if it.family_id not in fams[1]:
                fams[0].append(it.family_id)
                fams[1][it.family_id] = []
            fams[1][it.family_id].append(it)
        counts = {t: dict(c) for t, c in counts.items()}
        planning.check_core(counts, core=tuple(task for task in planning.CORE_TASKS
                                              if task_shares.get(task, 0) > 0))
        task_plan = planning.plan_task_shares(counts, task_shares)
        self.plan = {"tasks": task_plan,
                     "pools": {t: planning.plan_decision_pools(counts[t], pool_targets) for t in task_plan["shares"]}}
        self._tasks = list(task_plan["shares"])
        self._task_weights = [task_plan["shares"][t] for t in self._tasks]
        self.task_cycle = tuple(task_cycle) if task_cycle is not None else None
        if self.task_cycle is not None:
            if (not self.task_cycle or len(set(self.task_cycle)) != len(self.task_cycle)
                    or set(self.task_cycle) != set(self._tasks)
                    or any(abs(weight - 1 / len(self.task_cycle)) > 1e-9 for weight in self._task_weights)):
                raise ValueError('Task cycle requires every eligible task exactly once and equal requested task shares')
        self._pool_names = {t: list(self.plan["pools"][t]["shares"]) for t in self._tasks}
        self._pool_weights = {t: [self.plan["pools"][t]["shares"][p] for p in self._pool_names[t]] for t in self._tasks}
        self.n_families = len({i.family_id for i in elig})
        from training.finetune.sft_contracts import CORPUS_TASKS
        self._record_views = {}
        if elig and all(item.task in CORPUS_TASKS for item in elig):
            for item in elig:
                self._record_views.setdefault(item.record_id or item.view_id, {}).setdefault(item.task, []).append(item)
        if self.task_cycle and self._record_views:
            raise ValueError('Task cycle cannot replace source-record coverage sampling')
        self._batch_slots, self._epoch_items, self._epoch_order, self._epoch_pos = None, {}, {}, {}
        self.task_counts = None
        if task_counts is not None:
            if (self.task_cycle is not None or self._record_views or not isinstance(task_counts, dict)
                    or set(task_counts) != set(self._tasks)
                    or any(type(n) is not int or n < 1 for n in task_counts.values())):
                raise ValueError('task_counts must name every eligible task once with a positive integer and cannot be combined '
                                 'with a task_cycle or source-record coverage sampling')
            self.task_counts = {task: task_counts[task] for task in sorted(task_counts)}
            self._batch_slots = [task for task, n in self.task_counts.items() for _ in range(n)]
            for task in self.task_counts:
                self._epoch_items[task] = {i.view_id: i for i in elig if i.task == task}
                self._epoch_order[task] = sorted(self._epoch_items[task])
                self._epoch_pos[task] = 0
        self.pairs, self._pair_units = None, {}
        if pairs is not None:
            self._init_pairs(pairs)
        self._epoch_plan = {}
        if epoch_plan is not None:
            self._init_epoch_plan(epoch_plan)
        self._record_order = sorted(self._record_views)
        self.progress_units = len(self._record_order) or self.n_families
        self._repo_of = {i.family_id: i.repo for i in elig}
        self.family_cap, self.repo_cap, self.cap_warmup, self.on_draw = family_cap, repo_cap, cap_warmup, on_draw
        self.seed = seed
        self._rng = random.Random(seed)
        self._fingerprint = hashlib.sha256(json.dumps(
            [[i.view_id, i.task, i.pool, i.family_id, i.repo] for i in elig]
            + [self._tasks, [round(w, 12) for w in self._task_weights]], sort_keys=True).encode("utf-8")).hexdigest()
        self.draws = 0
        if self._record_order:
            self._fingerprint = hashlib.sha256((self._fingerprint + json.dumps(
                [[item.view_id, item.record_id] for item in elig])).encode()).hexdigest()
        if self.task_cycle:
            self._fingerprint = hashlib.sha256((self._fingerprint + json.dumps(
                {'policy': 'equal_task_cycle_v1', 'cycle': self.task_cycle})).encode()).hexdigest()
        if self._batch_slots:
            self._fingerprint = hashlib.sha256((self._fingerprint + json.dumps(
                {'policy': 'weighted_task_epoch_v1', 'task_counts': self.task_counts}, sort_keys=True)).encode()).hexdigest()
            if self.pairs:
                self._fingerprint = hashlib.sha256((self._fingerprint + json.dumps(
                    {'policy': 'paired_epoch_v1', 'pairs': self.pairs}, sort_keys=True)).encode()).hexdigest()
            if self._epoch_plan:
                self._fingerprint = hashlib.sha256((self._fingerprint + json.dumps(
                    {'policy': 'planned_epoch_v1', 'plan': self._epoch_plan}, sort_keys=True)).encode()).hexdigest()
            for task in self.task_counts:                      # the first pass of every task is shuffled once, in a fixed task order
                self._shuffle_epoch(task)
        self._by_task: Counter = Counter()
        self._by_pool: Counter = Counter()
        self._by_family: Counter = Counter()
        self._by_repo: Counter = Counter()
        self._by_view: Counter = Counter()
        self.cap_relaxed = 0

    # -- pairing -----------------------------------------------------------------------------------------------------------
    def _init_pairs(self, pairs):
        """`pairs`: [(view_id, view_id)], two views of one task that contrast (a vulnerable function and its fixed twin). The epoch order of that task lays each pair out next to
        each other, and the batch slots of the task are an even number, so the two views of a pair are drawn in the same optimizer batch. Views without a partner are put
        together two by two. A view is in at most one pair."""
        if not self._batch_slots:
            raise ValueError('pairs need the weighted task epoch sampler (task_counts)')
        seen, by_task = set(), {}
        for a, b in pairs:
            tasks = {t for t in self.task_counts if a in self._epoch_items[t] and b in self._epoch_items[t]}
            if len(tasks) != 1 or a == b or a in seen or b in seen:
                raise ValueError(f'invalid training pair {a}, {b}: both views must be eligible views of one task and in one pair only')
            seen.update((a, b))
            by_task.setdefault(tasks.pop(), []).append(tuple(sorted((a, b))))
        for task, task_pairs in by_task.items():
            if self.task_counts[task] % 2:
                raise ValueError(f'task {task} is drawn {self.task_counts[task]} times per batch: pairs need an even number')
            singles = sorted(v for v in self._epoch_items[task] if v not in seen)
            units = sorted(task_pairs) + [tuple(singles[i:i + 2]) for i in range(0, len(singles), 2)]
            self._pair_units[task] = units
        self.pairs = sorted(pair for task_pairs in by_task.values() for pair in task_pairs)

    def _init_epoch_plan(self, epoch_plan):
        """`epoch_plan`: {task: [round, ...]}, a round being [(view_id, view_id), ...] (see training.finetune.allocation.plan_epoch). The epoch of that task is the rounds in order, each shuffled
        inside, every unit two consecutive draws: a pair, the two views of one function, lands in one optimizer batch when the task is drawn an even number of times per batch. A view the plan
        does not name gets no draw; a view named several times is drawn that many times in an epoch. Weights play no part: how often a view is drawn is the plan."""
        if not self._batch_slots:
            raise ValueError('an epoch plan needs the weighted task epoch sampler (task_counts)')
        for task, rounds in epoch_plan.items():
            if task not in self.task_counts:
                raise ValueError(f'epoch plan names the task {task}, which is not drawn')
            if self.task_counts[task] % 2:
                raise ValueError(f'task {task} is drawn {self.task_counts[task]} times per batch: a planned epoch of pairs needs an even number')
            clean = []
            for unit_list in rounds:
                for unit in unit_list:
                    if len(unit) != 2 or any(v not in self._epoch_items[task] for v in unit):
                        raise ValueError(f'epoch plan unit {unit} is not two eligible views of {task}')
                clean.append([list(unit) for unit in unit_list])
            if not any(clean):
                raise ValueError(f'epoch plan for {task} is empty')
            self._epoch_plan[task] = clean
            self._epoch_order[task] = [v for unit_list in clean for unit in unit_list for v in unit]

    def _shuffle_epoch(self, task):
        plan = self._epoch_plan.get(task)
        if plan is not None:
            flat = []
            for unit_list in plan:
                order = [list(unit) for unit in unit_list]
                self._rng.shuffle(order)
                for unit in order:
                    if self._rng.random() < 0.5:                  # which twin comes first is not a cue
                        unit.reverse()
                    flat.extend(unit)
            self._epoch_order[task][:] = flat
            return
        units = self._pair_units.get(task)
        if units is None:
            self._rng.shuffle(self._epoch_order[task])
            return
        order = list(units)
        self._rng.shuffle(order)
        flat = []
        for unit in order:
            unit = list(unit)
            self._rng.shuffle(unit)
            flat.extend(unit)
        self._epoch_order[task][:] = flat

    def pair_report(self):
        if not self.pairs:
            return None
        drawn = sum(1 for a, b in self.pairs if self._by_view[a] and self._by_view[b])
        paired = {v for pair in self.pairs for v in pair}
        total = sum(len(self._epoch_items[task]) for task in self._pair_units)
        return {'pairs': len(self.pairs), 'pairs_with_both_views_drawn': drawn, 'views_in_pairs': len(paired), 'views_without_partner': total - len(paired)}

    # -- drawing -----------------------------------------------------------------------------------------------------------
    def _allowed(self, family: str) -> bool:
        if self.draws < self.cap_warmup:
            return True
        n = self.draws + 1
        return ((self._by_repo[self._repo_of[family]] + 1) / n <= self.repo_cap
                and (self._by_family[family] + 1) / n <= self.family_cap)

    def _pick_family(self, families: list[str]) -> str:
        for _ in range(30):
            f = families[self._rng.randrange(len(families))]
            if self._allowed(f):
                return f
        allowed = [f for f in families if self._allowed(f)]
        if allowed:
            return allowed[self._rng.randrange(len(allowed))]
        self.cap_relaxed += 1                                  # every family belongs to a capped owner: take the least exposed
        return min(families, key=lambda f: (self._by_repo[self._repo_of[f]], self._by_family[f], f))

    def draw(self) -> Item:
        if self._record_order:
            position = self.draws % len(self._record_order)
            if position == 0:
                self._rng.shuffle(self._record_order)
            record = self._record_views[self._record_order[position]]
            tasks = [task for task in self._tasks if task in record and self.plan['tasks']['shares'][task] > 0]
            if not tasks:
                raise ValueError('A corpus record has no requested source objective')
            task = self._rng.choices(tasks, [self.plan['tasks']['shares'][task] for task in tasks])[0]
            views = record[task]
            item = views[self._rng.randrange(len(views))]
            family, pool = item.family_id, item.pool
            if not self._allowed(family):
                self.cap_relaxed += 1
        elif self._batch_slots:
            task = self._batch_slots[self.draws % len(self._batch_slots)]
            order = self._epoch_order[task]
            if self._epoch_pos[task] >= len(order):            # a full pass is done: reshuffle, so no view repeats inside a pass
                self._shuffle_epoch(task)
                order = self._epoch_order[task]
                self._epoch_pos[task] = 0
            item = self._epoch_items[task][order[self._epoch_pos[task]]]
            self._epoch_pos[task] += 1
            family, pool = item.family_id, item.pool
        else:
            task = (self.task_cycle[self.draws % len(self.task_cycle)] if self.task_cycle
                    else self._rng.choices(self._tasks, self._task_weights)[0])
            pool = self._rng.choices(self._pool_names[task], self._pool_weights[task])[0]
            families, by_family = self._index[task][pool]
            family = self._pick_family(families)
            views = by_family[family]
            item = views[self._rng.randrange(len(views))]
        self.draws += 1
        self._by_task[task] += 1
        self._by_pool[f"{task}|{pool}"] += 1
        self._by_family[family] += 1
        self._by_repo[item.repo] += 1
        self._by_view[item.view_id] += 1
        if self.on_draw:
            self.on_draw(self.draws, item)
        return item

    def draw_many(self, n: int) -> list[Item]:
        return [self.draw() for _ in range(n)]

    @property
    def family_passes(self) -> float:
        # Compatibility name consumed by the trainer. Source datasets declare
        # progress_unit=record_pass in their run and exposure reports.
        return self.draws / self.progress_units if self.progress_units else 0.0

    # -- reporting ----------------------------------------------------------------------------------------------------------
    def exposure_report(self) -> dict:
        n = max(self.draws, 1)

        def top(c: Counter, k=5):
            return [{"key": key, "draws": v, "share": round(v / n, 6)} for key, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
        tasks = {t: {"planned": self.plan["tasks"]["shares"][t], "actual": self._by_task[t] / n, "draws": self._by_task[t]}
                 for t in self._tasks}
        pools = {k: {"draws": v} for k, v in sorted(self._by_pool.items())}
        schedule = {'policy': 'equal_task_cycle_v1' if self.task_cycle else 'weighted_task_draw',
                    'cycle': list(self.task_cycle) if self.task_cycle else None}
        extra = {}
        if self._batch_slots:
            schedule = {'policy': 'weighted_task_epoch_v1', 'cycle': None,
                        'task_counts_per_batch': dict(self.task_counts), 'batch_size': len(self._batch_slots)}
            drawn = {task: sum(1 for view_id in self._epoch_items[task] if self._by_view[view_id]) for task in self.task_counts}
            size = {task: len(self._epoch_items[task]) for task in self.task_counts}
            extra['view_coverage'] = {
                'total_views': sum(size.values()), 'views_drawn': sum(drawn.values()), 'views_drawn_by_task': drawn,
                'views_by_task': size, 'updates_for_full_coverage': max(-(-size[t] // self.task_counts[t]) for t in size),
                'draws_by_task': {task: self._by_task[task] for task in self.task_counts},
                'draws_per_view_by_task': {task: self._by_task[task] / size[task] for task in self.task_counts},
                'full_passes_by_task': {task: self._by_task[task] // size[task] for task in self.task_counts}}
        if self.pairs:
            extra['pairing'] = self.pair_report()
            schedule['policy'] = 'paired_epoch_v1'
        if self._epoch_plan:
            schedule['policy'] = 'planned_epoch_v1'
            extra['planned_epoch'] = {task: {'rounds': len(rounds), 'draws_per_epoch': sum(2 * len(r) for r in rounds)} for task, rounds in self._epoch_plan.items()}
        return {"draws": self.draws, "family_passes": self.family_passes, "tasks": tasks, "pools": pools,
                'task_schedule': schedule, **extra,
                'progress_unit': 'record_pass' if self._record_order else 'family_pass',
                'record_coverage': {'records_per_pass': len(self._record_order),
                    'completed_passes': self.draws // len(self._record_order) if self._record_order else None,
                    'policy': 'every_training_record_once_per_pass' if self._record_order else 'family_sampling'},
                "family": {"largest_share": max(self._by_family.values(), default=0) / n, "distinct_drawn": len(self._by_family),
                           "n_families": self.n_families, "top": top(self._by_family)},
                "repository": {"largest_share": max(self._by_repo.values(), default=0) / n, "distinct_drawn": len(self._by_repo),
                               "top": top(self._by_repo)},
                "cap_relaxed": self.cap_relaxed,
                "plan_report": "\n".join([self.plan["tasks"]["report"], *(p["report"] for p in self.plan["pools"].values())])}

    # -- checkpoint state -------------------------------------------------------------------------------------------------
    def state_dict(self) -> dict:
        v, internal, gauss = self._rng.getstate()
        state = {"fingerprint": self._fingerprint, "rng": [v, list(internal), gauss], "draws": self.draws,
                 "by_task": dict(self._by_task), "by_pool": dict(self._by_pool), "by_family": dict(self._by_family),
                 "by_repo": dict(self._by_repo), "by_view": dict(self._by_view), "cap_relaxed": self.cap_relaxed,
                 'record_order': list(self._record_order)}
        if self._batch_slots:
            state['epoch'] = {'order': {task: list(order) for task, order in self._epoch_order.items()}, 'pos': dict(self._epoch_pos)}
        return state

    def load_state_dict(self, state: dict) -> None:
        if state.get("fingerprint") != self._fingerprint:
            raise ValueError("this sampler state was saved over different data (the views, tasks or families differ)")
        v, internal, gauss = state["rng"]
        self._rng.setstate((v, tuple(internal), gauss))
        self.draws, self.cap_relaxed = int(state["draws"]), int(state["cap_relaxed"])
        self._by_task, self._by_pool = Counter(state["by_task"]), Counter(state["by_pool"])
        self._by_family, self._by_repo, self._by_view = Counter(state["by_family"]), Counter(state["by_repo"]), Counter(state["by_view"])
        if self._batch_slots:
            epoch = state.get('epoch') or {}
            for task, order in self._epoch_order.items():
                saved, pos = (epoch.get('order') or {}).get(task), (epoch.get('pos') or {}).get(task)
                if saved is None or sorted(saved) != sorted(order) or type(pos) is not int or not 0 <= pos <= len(order):
                    raise ValueError('Epoch sampler checkpoint view coverage differs')
            self._epoch_order = {task: list(epoch['order'][task]) for task in self._epoch_order}
            self._epoch_pos = {task: int(epoch['pos'][task]) for task in self._epoch_pos}
        if self._record_order:
            order = state.get('record_order', [])
            if len(order) != len(self._record_order) or set(order) != set(self._record_order):
                raise ValueError('Corpus sampler checkpoint record coverage differs')
            self._record_order = list(order)
