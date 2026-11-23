"""Separate automatic detection from review referral and dataset eligibility.

Intervals combine a deterministic group bootstrap with a Wilson boundary guard
using the number of independent groups. This guard avoids a zero-width interval
when every sampled result is identical. They are descriptive uncertainty bounds,
not a guarantee about unseen company repositories.
"""
from __future__ import annotations
import math
import random
from collections import Counter, defaultdict
from training.finetune import evaluation_data

DEPENDENCE_KEYS = tuple(dict.fromkeys(evaluation_data.IDENTITIES + ('security_component', 'pair_id', 'evaluation_component')))


def dependence_anchors(row):
    """Normalize identities while retaining unions from record/view joins."""
    anchors = {('repository' if key in ('repository_identity', 'repository_family') else evaluation_data._identity_space(key),
                evaluation_data._identity_value(key, value))
               for key in DEPENDENCE_KEYS for value in evaluation_data._strings(row.get(key))}
    for anchor in row.get('dependence_anchors') or []:
        if isinstance(anchor, (list, tuple)) and len(anchor) == 2 and all(isinstance(item, str) for item in anchor):
            anchors.add(tuple(anchor))
    return sorted(anchors)

POS = 'vulnerability_supported'
NEG = 'no_supported_finding_in_scope'
INSUFFICIENT = 'insufficient_context'


def dependence_groups(rows):
    """Connected repository, issue, pair and security-family units, never individual views."""
    rows = list(rows)
    parents = list(range(len(rows)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    anchors = {}
    for i, row in enumerate(rows):
        keys = dependence_anchors(row)
        # Missing all dependence metadata cannot manufacture independent trials.
        for key in keys or [('unknown', 'unknown')]:
            if key in anchors:
                a, b = root(i), root(anchors[key])
                parents[max(a, b)] = min(a, b)
            else:
                anchors[key] = i
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        groups[str(root(i))].append(row)
    return dict(groups)


def _decisive(row):
    return row.get('status') == 'answered' and row.get('prediction') in (POS, NEG)


def _point_metrics(rows):
    positives = [r for r in rows if r.get('truth') == POS]
    negatives = [r for r in rows if r.get('truth') == NEG]
    tp = sum(_decisive(r) and r['prediction'] == POS for r in positives)
    fp = sum(_decisive(r) and r['prediction'] == POS for r in negatives)
    tn = sum(_decisive(r) and r['prediction'] == NEG for r in negatives)
    answered_fn = sum(_decisive(r) and r['prediction'] == NEG for r in positives)
    fn = len(positives) - tp  # Positive referrals are automatic-detection misses.
    denominator = math.sqrt((tp + fp) * (tp + answered_fn) * (tn + fp) * (tn + answered_fn))
    precision = tp / (tp + fp) if tp + fp else None
    return {'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn, 'answered_fn': answered_fn,
            'precision': precision, 'recall': tp / len(positives) if positives else None,
            'false_positive_rate': fp / len(negatives) if negatives else None,
            'f1': 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            'mcc': (tp * tn - fp * answered_fn) / denominator if denominator else None,
            'coverage': sum(_decisive(r) for r in rows) / len(rows) if rows else 0.}


def _wilson(rate, n):
    if not n: return [0., 1.]
    z = 1.959963984540054; denominator = 1 + z * z / n
    centre = (rate + z * z / (2 * n)) / denominator
    half = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denominator
    return [max(0., centre - half), min(1., centre + half)]


def measure(attempts, *, bootstrap_samples=1000, seed=0):
    rows = list(attempts)
    # Alias and repository overlap form one dependence component. Distinct CVEs
    # from the same repository are not independent uncertainty samples.
    groups = dependence_groups(rows)
    def detected(r): return r.get('status') == 'answered' and r.get('prediction') == POS
    positives = [r for r in rows if r.get('truth') == POS]
    negatives = [r for r in rows if r.get('truth') == NEG]
    referred = [r for r in rows if r.get('status') != 'answered' or r.get('prediction') not in (POS, NEG)]
    tp = sum(detected(r) for r in positives); fp = sum(detected(r) for r in negatives)
    tn = sum(r.get('status') == 'answered' and r.get('prediction') == NEG for r in negatives)
    point = _point_metrics(rows)
    recall = tp / len(positives) if positives else None
    fpr = fp / len(negatives) if negatives else None
    rng = random.Random(seed); keys = sorted(groups); samples = {'recall': [], 'false_positive_rate': []}
    for _ in range(bootstrap_samples if keys else 0):
        chosen = [r for _ in keys for r in groups[rng.choice(keys)]]
        for truth, metric in ((POS, 'recall'), (NEG, 'false_positive_rate')):
            subset = [r for r in chosen if r.get('truth') == truth]
            if subset: samples[metric].append(sum(detected(r) for r in subset) / len(subset))
    uncertainty = {}
    for truth, name, value in ((POS, 'recall', recall), (NEG, 'false_positive_rate', fpr)):
        effective_n = sum(any(r.get('truth') == truth for r in group) for group in groups.values())
        bounds = _wilson(value or 0., effective_n)
        values = sorted(samples[name])
        if values:
            bounds = [min(bounds[0], values[int(.025 * (len(values) - 1))]), max(bounds[1], values[int(.975 * (len(values) - 1))])]
        uncertainty[name] = bounds
    pairs = defaultdict(list)
    for r in rows:
        if r.get('pair_id'): pairs[r['pair_id']].append(r)
    complete = [rs for rs in pairs.values() if {r.get('truth') for r in rs} >= {POS, NEG}]
    paired = sum(all(r.get('status') == 'answered' and r.get('prediction') == r.get('truth') for r in rs) for rs in complete)
    positive_referrals = sum(not _decisive(r) for r in positives)
    negative_referrals = sum(not _decisive(r) for r in negatives)
    return {'schema': 'security-validation/1', 'counts': {'tp': tp, 'fn': len(positives) - tp, 'fp': fp, 'tn': tn},
            'positive_examples': len(positives), 'negative_examples': len(negatives), 'recall': recall, 'false_positive_rate': fpr,
            'automatic_detection_recall': recall, 'precision': point['precision'], 'f1': point['f1'], 'mcc': point['mcc'],
            'metric_definitions': {
                'precision': 'TP / (TP + FP) on supported positive and scoped-negative ground truth; incomplete-context truth stays separate.',
                'f1': '2 TP / (2 TP + FP + FN), including every positive referral in FN.',
                'false_positive_rate': 'FP / all scoped-negative examples. Negative referrals are unresolved, never TN.',
                'coverage': 'Decisive answered predictions / all supplied examples, including incomplete ground truth and failures.',
                'uncertainty': 'Connected repository/issue/pair groups resampled together; Wilson guard uses groups containing the relevant truth. Descriptive, not certification.'},
            'mcc_scope': {'basis': 'answered_binary_labeled_examples',
                          'examples': len(positives) + len(negatives) - positive_referrals - negative_referrals,
                          'positive_referrals': positive_referrals, 'negative_referrals': negative_referrals,
                          'note': 'MCC uses answered binary outcomes only; assess together with overall detection recall and coverage.'},
            'positive_referrals': positive_referrals, 'negative_referrals': negative_referrals,
            'insufficient_context_examples': sum(r.get('truth') == INSUFFICIENT for r in rows),
            'unknown_truth_examples': sum(r.get('truth') not in (POS, NEG, INSUFFICIENT) for r in rows),
            'decisive_answers_on_insufficient_context': sum(r.get('truth') == INSUFFICIENT and _decisive(r) for r in rows),
            'total_examples': len(rows), 'status_counts': dict(Counter(r.get('status') or 'missing' for r in rows)),
            'timeouts': sum(r.get('status') == 'timeout' for r in rows),
            'excluded': sum(r.get('status') == 'excluded' for r in rows),
            'coverage': (len(rows) - len(referred)) / len(rows) if rows else 0., 'review_referrals': len(referred),
            'review_fraction': len(referred) / len(rows) if rows else None,
            'malformed': sum(r.get('status') == 'malformed' for r in rows),
            'abstentions': sum(r.get('status') == 'abstained' or r.get('prediction') == 'insufficient_context' for r in rows),
            'paired': {'correct': paired, 'total': len(complete), 'rate': paired / len(complete) if complete else None},
            'independent_groups': len(groups), 'uncertainty': uncertainty,
            'effective_groups_by_truth': {
                'positive': sum(any(r.get('truth') == POS for r in group) for group in groups.values()),
                'negative': sum(any(r.get('truth') == NEG for r in group) for group in groups.values())},
            'calibration': {'state': 'unavailable', 'reason': 'No validated continuous score and independent calibration protocol supplied.'},
            'risk_coverage': {'state': 'unavailable', 'reason': 'Binary verdicts do not define a confidence ranking.'},
            'repair_execution': {'state': 'not_run', 'reason': 'Detection outcomes contain no trusted patch-execution receipt.'},
            'target_met': bool(positives and negatives and uncertainty['recall'][0] >= .9 and uncertainty['false_positive_rate'][1] <= .01),
            'deployment_ready': False, 'deployment_blockers': ['requires_reviewed_untouched_scope_and_review_capacity_assessment'],
            'policy': 'Uncertain positive cases are automatic-detection misses. No-finding is scoped, never a clean-code certification.'}


def paired_differences(base, tuned, *, bootstrap_samples=1000, seed=0):
    """Bootstrap matched model differences by connected repository/issue units.

    Inputs must already be aligned by view identity with matching ground truth.
    These are descriptive intervals conditional on this cohort, not deployment bounds.
    """
    base, tuned = list(base), list(tuned)
    if len(base) != len(tuned) or not base:
        raise ValueError('Paired differences require a nonempty matched cohort')
    if any((b.get('view_id'), b.get('truth')) != (t.get('view_id'), t.get('truth')) for b, t in zip(base, tuned)):
        raise ValueError('Paired differences require aligned identities and labels')
    indexed = [{**r, '_paired_index': i} for i, r in enumerate(base)]
    groups = list(dependence_groups(indexed).values())
    names = ('recall', 'false_positive_rate', 'precision', 'f1', 'mcc', 'coverage')
    bpoint, tpoint = _point_metrics(base), _point_metrics(tuned)
    samples = {name: [] for name in names}
    rng = random.Random(seed)
    if len(groups) >= 2:
        for _ in range(bootstrap_samples):
            chosen = [r['_paired_index'] for _ in groups for r in rng.choice(groups)]
            bm = _point_metrics([base[i] for i in chosen])
            tm = _point_metrics([tuned[i] for i in chosen])
            for name in names:
                if bm[name] is not None and tm[name] is not None:
                    samples[name].append(tm[name] - bm[name])
    result = {'independent_groups': len(groups), 'views': len(base), 'seed': seed,
              'bootstrap_samples': bootstrap_samples, 'method': 'paired_repository_issue_component_bootstrap',
              'direction': 'tuned_minus_base',
              'scope': 'Conditional on matched cohort; correlated examples sampled together. No deployment guarantee.'}
    for name in names:
        values = sorted(samples[name])
        estimate = tpoint[name] - bpoint[name] if bpoint[name] is not None and tpoint[name] is not None else None
        result[name] = {'estimate': estimate,
                        'interval_95': [values[int(.025 * (len(values) - 1))], values[int(.975 * (len(values) - 1))]] if values else None,
                        'valid_bootstrap_samples': len(values),
                        'state': 'insufficient_independent_groups' if len(groups) < 2 else 'measured' if values else 'insufficient_data'}
    return result


def compare(channels):
    if set(channels) != {'model', 'tools', 'combined'}:
        raise ValueError('Supply all three independent channels: model, tools, combined')
    return {name: measure(rows) for name, rows in channels.items()}
