"""Draw allocation for the paired recipe (recipe 11). No torch.

Why this exists. The v38 run drew a vulnerable function and its fixed twin together on only about half of its discovery draws, gave the units whose label no analyser can prove
(LLM-attributed, ``recovered_L``) about 54 percent of all draws, and its review weights (1.0 / 0.4) changed nothing: the sampler only tests ``weight > 0`` and the trainer's
one-example micro-batches cancel a per-example weight in the loss. So the weights are replaced here by what a weight was meant to do, a number of draws.

Model. The chance that a function is learned after ``d`` draws is taken as ``1 - exp(-d / tau)`` (tau = 4: it reproduces the measured fixed-code clear rate of v38 at d = 4 and of
v36 at d = 10). A unit ``i`` is worth ``q_i`` (``eta`` by origin times ``T``, 1 when the unit has a pair). Maximising ``sum q_i (1 - exp(-d_i / tau))`` with ``sum d_i = N`` and
``0 <= d_i <= d_max`` has the closed form ``d_i = clip(tau * ln(q_i / nu), 0, d_max)`` (the log water-filling rule); ``nu`` is found by bisection.

The epoch plan lays the draws out in rounds. Each round holds, for every unit that still has exposures, one pair (a finding view and its fixed twin in the same prompt form);
a pair is two consecutive draws, so with an even number of discovery draws per batch both views are in one optimizer update. Pairs are cycled over the unit's forms and
over its label-preserving variants (record ids ``<record>#aug<n>``), so a unit is not repeated literally.
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict

# Worth of a draw by unit origin. The proved flows (a real fix whose flow the analyser confirms, or a synthetic twin made by that analyser) are worth the most; an LLM-attributed
# unit has no analyser proof and a recall of 13/24 against 31/37 on proved ones in the v38 evaluation (p = 0.019).
ETA = {'upstream_fix': 1.0, 'sanitiser_removal': 1.0, 'unsafe_substitution': 1.0, 'recovered_D': 0.7, 'recovered_L': 0.4,
       'diff_guard': 0.7, 'diff_sink': 0.55, 'strict_attribution': 0.4}      # plan v6: a unit proved by its diff only is worth by the strength of that proof (lineage `proof`)
DEFAULT_ETA = 0.4
TAU = 4.0
D_MAX = 12.0
LONE_SHARE = 0.2                                  # share of the discovery draws for views that have no twin (class-confusion negatives, neighbours, unpaired positives)
FORM_COST = {'scoped': 0.0, 'open': 0.6, 'set': 0.8}
KIND_FORM = {'discovery_scoped': 'scoped', 'discovery_open': 'open', 'discovery_set': 'set'}
AUG = '#aug'


def base_record(record_id):
    return record_id.split(AUG, 1)[0]


def variant_index(record_id):
    return int(record_id.split(AUG, 1)[1]) if AUG in record_id else 0


def water_fill(q, total, tau=TAU, d_max=D_MAX):
    """d_i = clip(tau * ln(q_i / nu), 0, d_max) with sum d_i = total (or d_max for all, when the budget exceeds what the units can take). q: list of positive worths."""
    q = [float(x) for x in q]
    if not q:
        return []
    if total >= d_max * sum(1 for x in q if x > 0):
        return [d_max if x > 0 else 0.0 for x in q]

    def spent(nu):
        return sum(min(d_max, max(0.0, tau * math.log(x / nu))) for x in q if x > 0)
    lo, hi = min(x for x in q if x > 0) * math.exp(-d_max / tau) * 0.5, max(q)
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if spent(mid) > total else (lo, mid)
    nu = math.sqrt(lo * hi)
    return [min(d_max, max(0.0, tau * math.log(x / nu))) if x > 0 else 0.0 for x in q]


def largest_remainder(values, total):
    """Integers close to `values` that sum to `total` exactly."""
    floors = [int(math.floor(v)) for v in values]
    left = total - sum(floors)
    order = sorted(range(len(values)), key=lambda i: (-(values[i] - floors[i]), i))
    for i in order[:max(0, left)]:
        floors[i] += 1
    return floors


def unit_origin(view):
    lineage = view.get('lineage_json')
    if isinstance(lineage, str):
        try:
            lineage = json.loads(lineage)
        except ValueError:
            lineage = {}
    lineage = lineage or {}
    return lineage.get('proof') or lineage.get('unit_origin')          # `proof` exists only on the diff-proved units of v40; every earlier row keeps its origin


def plan_epoch(views, pairs, *, total_draws, seed=0, tau=TAU, d_max=D_MAX, lone_share=LONE_SHARE, eta=None):
    """The discovery epoch of recipe 11.

    views: training views (dicts with view_id, task, kind, record_id, lineage_json); pairs: [(finding view id, fixed twin view id)].
    Returns {'rounds': [[(view id, view id), ...], ...], 'report': {...}}. Every unit of a round is two views; the epoch is the rounds in order, each shuffled inside."""
    eta = dict(ETA if eta is None else eta)
    rng = random.Random(seed)
    disc = {v['view_id']: v for v in views if v['task'] == 'discovery'}
    in_pair = {x for pair in pairs for x in pair}
    by_unit = defaultdict(list)                                    # base record -> [(cost, pair)]
    for a, b in sorted(pairs):
        if a not in disc or b not in disc:
            continue
        va = disc[a]
        form = KIND_FORM.get(va['kind'])
        if form is None:
            raise ValueError(f'pair {a}, {b}: the first view is not a finding view ({va["kind"]})')
        by_unit[base_record(va['record_id'])].append((FORM_COST[form] + 0.9 * variant_index(va['record_id']), (a, b)))
    units = sorted(by_unit)
    origin = {u: unit_origin(disc[by_unit[u][0][1][0]]) for u in units}
    worth = [eta.get(origin[u], DEFAULT_ETA) for u in units]
    n_pair_draws = int(round(total_draws * (1 - lone_share)))
    n_pair_draws -= n_pair_draws % 2
    d = water_fill(worth, n_pair_draws, tau=tau, d_max=d_max)
    exposures = largest_remainder([x / 2 for x in d], n_pair_draws // 2)
    rounds_needed = max(max(exposures, default=0), 4)
    rounds = [[] for _ in range(rounds_needed)]
    drawn_pairs = defaultdict(int)
    for u, k, w in zip(units, exposures, worth):
        plist = [p for _, p in sorted(by_unit[u])]
        for e in range(k):
            pair = plist[e % len(plist)]
            rounds[min(rounds_needed - 1, e * rounds_needed // k)].append(pair)
            drawn_pairs[u] += 1
    # The lane of views that have no twin: weighted by origin, at most three times each, two views to a unit.
    lone = sorted(v for v in disc if v not in in_pair)
    lone_worth = [eta.get(unit_origin(disc[v]), DEFAULT_ETA) for v in lone]
    n_lone = total_draws - n_pair_draws
    lone_counts = [0] * len(lone)
    if lone:
        raw = [n_lone * w / sum(lone_worth) for w in lone_worth]
        lone_counts = largest_remainder(raw, n_lone)
        for cap in (3, 4, 5, 6, 8, 12):                            # no lone view more than `cap` times; the cap rises only if the lane cannot otherwise be filled
            capped = [min(cap, c) for c in lone_counts]
            if sum(capped) >= n_lone - 1 or cap == 12:
                lone_counts = capped
                break
            room = [cap - c for c in capped]
            extra, i = n_lone - sum(capped), 0
            while extra > 0 and any(r > 0 for r in room):
                j = i % len(capped)
                if room[j] > 0:
                    capped[j] += 1
                    room[j] -= 1
                    extra -= 1
                i += 1
            if sum(capped) >= n_lone - 1:
                lone_counts = capped
                break
    pool = []
    for view_id, k in zip(lone, lone_counts):
        for e in range(k):
            pool.append((e * rounds_needed // max(k, 1), rng.random(), view_id))
    pool.sort()
    by_round = defaultdict(list)
    for r, _, view_id in pool:
        by_round[r].append(view_id)
    carry = []
    for r in range(rounds_needed):
        ids = carry + by_round.get(r, [])
        carry = ids[-1:] if len(ids) % 2 else []
        for i in range(0, len(ids) - len(carry), 2):
            rounds[r].append((ids[i], ids[i + 1]))
    report = plan_report(rounds, disc, pairs, units, origin, by_unit, tau, d_max)
    return {'rounds': rounds, 'report': report}


def plan_report(rounds, disc, pairs, units, origin, by_unit, tau, d_max):
    pair_set = {tuple(sorted(p)) for p in pairs}
    draws = defaultdict(int)
    in_pair_draws = total = no_finding = 0
    by_origin = defaultdict(lambda: [0, 0])
    for unit_list in rounds:
        for a, b in unit_list:
            total += 2
            co = tuple(sorted((a, b))) in pair_set
            in_pair_draws += 2 if co else 0
            for v in (a, b):
                draws[v] += 1
                no_finding += disc[v]['kind'] not in KIND_FORM
    record_draws = defaultdict(int)
    for v, n in draws.items():
        record_draws[base_record(disc[v]['record_id'])] += n
    for u in units:
        by_origin[origin[u]][0] += 1
        by_origin[origin[u]][1] += record_draws.get(u, 0)
    paired_units = [record_draws.get(u, 0) for u in units]
    return {'policy': 'water_fill_pairs_v1', 'tau': tau, 'd_max': d_max, 'rounds': len(rounds), 'draws': total, 'draws_in_co_drawn_pairs': in_pair_draws,
            'share_in_co_drawn_pairs': in_pair_draws / total if total else 0.0, 'share_no_finding': no_finding / total if total else 0.0,
            'paired_units': len(units), 'paired_units_mean_draws': sum(paired_units) / len(units) if units else 0.0,
            'paired_units_min_draws': min(paired_units, default=0),
            'by_origin': {str(o): {'units': n, 'draws': d, 'draws_per_unit': d / n} for o, (n, d) in sorted(by_origin.items(), key=lambda kv: str(kv[0]))}}
