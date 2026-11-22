"""Paired epochs: a vulnerable function and its fixed twin are drawn in the same optimizer batch, and everything else about the epoch sampler is unchanged."""
import json
from collections import Counter

import pytest

from training.finetune.sampler import Item, Sampler

COUNTS = {'discovery': 6, 'repair': 2}
PAIRS = [(f'discovery:v{n}', f'discovery:f{n}') for n in range(9)]               # 9 pairs = 18 views
SINGLES = [f'discovery:s{n}' for n in range(4)]                                   # 4 views without a partner


def make(seed=17, pairs=PAIRS, counts=COUNTS, singles=SINGLES):
    ids = [v for pair in PAIRS for v in pair] + list(singles)
    items = [Item(view_id=v, task='discovery', pool='supported', family_id=v, repo=v) for v in ids]
    items += [Item(view_id=f'repair:{n}', task='repair', pool='supported', family_id=f'repair:{n}', repo=f'repo{n}') for n in range(6)]
    return Sampler(items, task_shares={'discovery': .75, 'repair': .25}, pool_targets={'supported': 1.0}, seed=seed, task_counts=counts, pairs=pairs)


def batches(sampler, n):
    return [[item.view_id for item in sampler.draw_many(8)] for _ in range(n)]


def test_partners_are_drawn_in_the_same_optimizer_batch():
    sampler = make()
    for batch in batches(sampler, 3):                                            # the first epoch has 11 units = 22 views... three batches draw 18 discovery views
        discovery = [v for v in batch if v.startswith('discovery')]
        assert len(discovery) == 6 and sum(v.startswith('repair') for v in batch) == 2
        for v in discovery:
            if v.startswith(('discovery:v', 'discovery:f')):
                partner = v.replace(':v', ':f') if ':v' in v else v.replace(':f', ':v')
                assert partner in discovery, (v, batch)


def test_every_view_is_drawn_once_per_pass_and_the_batch_counts_hold():
    sampler = make()
    draws = sampler.draw_many(8 * 11)
    discovery = [item.view_id for item in draws if item.task == 'discovery']
    size = len(PAIRS) * 2 + len(SINGLES)
    assert Counter(discovery[:size]) == Counter({v: 1 for pair in PAIRS for v in pair} | {s: 1 for s in SINGLES})
    assert Counter(item.task for item in draws) == Counter({'discovery': 66, 'repair': 22})


def test_order_is_deterministic_by_seed_and_the_pairing_changes_the_order():
    assert batches(make(17), 4) == batches(make(17), 4)
    assert batches(make(17), 4) != batches(make(18), 4)


def test_resume_is_exact_and_a_state_over_other_pairs_is_refused():
    sampler = make()
    sampler.draw_many(11)
    state = json.loads(json.dumps(sampler.state_dict()))
    expected = sampler.draw_many(60)
    resumed = make()
    resumed.load_state_dict(state)
    assert resumed.draw_many(60) == expected
    with pytest.raises(ValueError, match='different data'):
        make(pairs=PAIRS[:-1]).load_state_dict(state)
    with pytest.raises(ValueError, match='different data'):
        Sampler([Item(view_id=v, task='discovery', pool='supported', family_id=v, repo=v) for v in [x for p in PAIRS for x in p] + SINGLES]
                + [Item(view_id=f'repair:{n}', task='repair', pool='supported', family_id=f'repair:{n}', repo=f'repo{n}') for n in range(6)],
                task_shares={'discovery': .75, 'repair': .25}, pool_targets={'supported': 1.0}, seed=17, task_counts=COUNTS).load_state_dict(state)


def test_the_report_counts_pairs_and_unpaired_views():
    sampler = make()
    sampler.draw_many(8 * 12)
    report = sampler.exposure_report()
    assert report['pairing'] == {'pairs': 9, 'pairs_with_both_views_drawn': 9, 'views_in_pairs': 18, 'views_without_partner': 4}
    assert report['task_schedule']['policy'] == 'paired_epoch_v1'


def test_invalid_pairs_are_refused():
    with pytest.raises(ValueError, match='even number'):
        make(counts={'discovery': 5, 'repair': 3})
    with pytest.raises(ValueError, match='invalid training pair'):
        make(pairs=PAIRS + [('discovery:v0', 'discovery:s0')])                     # a view in two pairs
    with pytest.raises(ValueError, match='invalid training pair'):
        make(pairs=[('discovery:v0', 'repair:0')])                                  # two tasks
    with pytest.raises(ValueError, match='invalid training pair'):
        make(pairs=[('discovery:v0', 'discovery:nope')])


def test_without_pairs_the_draws_are_exactly_the_old_ones():
    ids = [v for pair in PAIRS for v in pair] + SINGLES
    items = [Item(view_id=v, task='discovery', pool='supported', family_id=v, repo=v) for v in ids] + [
        Item(view_id=f'repair:{n}', task='repair', pool='supported', family_id=f'repair:{n}', repo=f'repo{n}') for n in range(6)]
    old = Sampler(items, task_shares={'discovery': .75, 'repair': .25}, pool_targets={'supported': 1.0}, seed=17, task_counts=COUNTS)
    assert old.pairs is None and old.exposure_report()['task_schedule']['policy'] == 'weighted_task_epoch_v1' and 'pairing' not in old.exposure_report()
