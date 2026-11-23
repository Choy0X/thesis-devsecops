"""A review queue for an independent real-code evaluation set. It never makes anything eligible.

Real collected records can only become evaluation views after source-bound permission, authored labels and independent security
adjudication. This module selects reviewable candidates (one per repository and issue group), plans the views a reviewer would author
(discovery before and after, a repair, an insufficient-context case), attaches the existing admission decision and lists every blocker
with its remediation. ``independent_evaluation_eligible`` stays false and ``admitted_views`` stays zero here by construction.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

SCHEMA = 'security-real-code-candidate-queue/1'
BENCHMARK_SCHEMA = 'security-benchmark/1'
VIEW_PLAN = (('discovery', 'before', 'supported'), ('discovery', 'after', 'scoped_negative'), ('repair', 'before', 'supported'),
             ('insufficient_context', 'withheld', 'insufficient_context'))
REMEDIATION = {
    'independent_adjudication_pending': 'An independent reviewer must adjudicate each scoped label, trace and withheld decisive fact.',
    'authored_response_required': 'A reviewer authors the response for each view; no label or response is inferred from the record.',
    'authored_withheld_fact_required': 'A reviewer names the decisive fact to withhold and authenticates its source.',
    'no_admitted_task': 'The existing admission policy admits no task for this record.'}


def _sha(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def _reviewable(r):
    return (r.get('data_quality_status') == 'ok' and r.get('provenance_status') == 'verified' and r.get('granularity') == 'function'
            and r.get('data_usage_status') in ('permissive', 'usable') and r.get('repository_license') not in (None, 'NOASSERTION')
            and r.get('vulnerable_code') and r.get('fixed_code') and r['vulnerable_code'] != r['fixed_code']
            and 200 < len(r['vulnerable_code']) < 2500 and len(r['fixed_code']) < 2500 and r.get('cwe_ids') and r.get('file_path_before'))


def select_candidate_records(rows, *, count):
    """Deterministic and order independent: one reviewable record per repository and issue group, at most two per weakness class."""
    ordered = sorted((r for r in rows if _reviewable(r)), key=lambda r: (_sha(r['record_id']), r['record_id']))
    chosen, repositories, groups, classes = [], set(), set(), Counter()
    for record in ordered:
        cwe = str(record['cwe_ids'][0])
        if (record['repository_identity'] in repositories or record['issue_group_id'] in groups or classes[cwe] >= 2):
            continue
        chosen.append(record)
        repositories.add(record['repository_identity'])
        groups.add(record['issue_group_id'])
        classes[cwe] += 1
        if len(chosen) == count:
            break
    return chosen


def benchmark_readiness(queue_state):
    """What ``evaluation_data.preflight`` needs from an independent benchmark, and what this queue still lacks."""
    def requirement(name, met, detail):
        return {'requirement': name, 'met': met, 'detail': detail}
    return {'schema_required': BENCHMARK_SCHEMA, 'state': 'blocked', 'requirements': [
        requirement('frozen_manifest', False, 'No frozen security-benchmark/1 manifest with a pinned SHA256 exists; this queue is unreviewed.'),
        requirement('independent_adjudication', False, 'adjudication must be independent_review; no reviewer identity is recorded.'),
        requirement('deployment_context_acquisition', False, 'context must be acquired by deployment retrieval; the queue only holds record snapshots.'),
        requirement('frozen_before_tuning', False, 'The benchmark must be frozen before any tuning; nothing is frozen.'),
        requirement('overlap_review_fork_families', False, 'A reviewer must review fork families against training data.'),
        requirement('overlap_review_generated_derivatives', False, 'A reviewer must review generated derivatives against training data.'),
        requirement('no_overlap_with_training', False, 'The overlap audit cannot run before a frozen benchmark exists.')]}


PERMISSION_OR_SOURCE = ('permission_not_usable', 'pinned_source_permission_not_established', 'permission_source_blob_unavailable',
                         'after_source_unavailable', 'backend_source_unavailable')


def permission_free_records(records, admissions):
    """Records whose admission decision carries no permission or source blocker (still not eligible without independent review)."""
    return sum(not {r['code'] for r in admissions[rec['record_id']].get('reasons', [])} & set(PERMISSION_OR_SOURCE) for rec in records)


def build_queue(records, admissions, *, groups):
    candidates, reasons_by_record = [], {}
    for record in records:
        admitted = admissions[record['record_id']]
        reasons_by_record[record['record_id']] = {r['code']: r for r in admitted.get('reasons', [])}
        for task, side, pool in VIEW_PLAN:
            code = record['vulnerable_code'] if side in ('before', 'withheld') else record['fixed_code']
            candidates.append({
                'view_id': f'real:{record["record_id"]}:{task}:{side}', 'record_id': record['record_id'], 'issue_group_id': record['issue_group_id'],
                'repository_identity': record['repository_identity'], 'task': task, 'side': side, 'pool': pool,
                'eligible': False, 'review_status': 'independent_review_required', 'label': 'unreviewed', 'proposed_response': None,
                'source': {'file_path_before': record.get('file_path_before'), 'file_path_after': record.get('file_path_after'),
                           'symbol': record.get('symbol'), 'language': record.get('language'), 'cwe_ids': list(record.get('cwe_ids') or []),
                           'parent_commit': record.get('relevant_parent_commit'), 'fix_commit': record.get('fix_commit'),
                           'shown_sha256': _sha(code), 'vulnerable_sha256': _sha(record['vulnerable_code']), 'fixed_sha256': _sha(record['fixed_code']),
                           'license': record.get('repository_license'), 'data_usage_status': record.get('data_usage_status')},
                'admission': {'policy': 'sft-admission/1', 'source_integrity': admissions[record['record_id']].get('source_integrity'),
                              'training_eligibility': admissions[record['record_id']].get('training_eligibility'),
                              'eligible_tasks': list(admissions[record['record_id']].get('eligible_tasks') or []),
                              'reason_codes': sorted(reasons_by_record[record['record_id']])}})
    blockers = defaultdict(lambda: {'records': 0, 'views': 0})
    detail = {}
    for record in records:
        codes = set(reasons_by_record[record['record_id']])
        if not record.get('reviewer_or_verifier_identity'):
            codes.add('independent_adjudication_pending')
        if not admissions[record['record_id']].get('eligible_tasks'):
            codes.add('no_admitted_task')
        for code in codes:
            blockers[code]['records'] += 1
            detail.setdefault(code, reasons_by_record[record['record_id']].get(code, {}).get('remediation') or REMEDIATION.get(code, ''))
    views = len(candidates)
    blockers['authored_response_required'] = {'records': len(records), 'views': views}
    blockers['authored_withheld_fact_required'] = {'records': len(records), 'views': sum(c['task'] == 'insufficient_context' for c in candidates)}
    detail['authored_response_required'] = REMEDIATION['authored_response_required']
    detail['authored_withheld_fact_required'] = REMEDIATION['authored_withheld_fact_required']
    shortfall = max(0, groups - len(records))
    state = 'insufficient_candidates' if shortfall else 'review_required'
    result = {'schema': SCHEMA, 'state': state, 'target_views': groups * len(VIEW_PLAN), 'target_groups': groups, 'shortfall_groups': shortfall,
              'admission_policy': 'sft-admission/1', 'independent_evaluation_eligible': False, 'admitted_views': 0,
              'candidates': candidates,
              'blockers': [{'code': code, **counts, 'remediation': detail.get(code, '')} for code, counts in sorted(blockers.items())],
              'summary': {'groups': len(records), 'repositories': len({r['repository_identity'] for r in records}),
                          'records_free_of_permission_and_source_blockers': permission_free_records(records, admissions),
                          'cwe_classes': dict(Counter(str(r['cwe_ids'][0]) for r in records)),
                          'languages': dict(Counter(r.get('language') for r in records))},
              'interpretation': 'Review candidates only. No label, response or permission is established; independent security accuracy remains unmeasured.'}
    result['benchmark_readiness'] = benchmark_readiness(result)
    validate_queue(result)
    return result


def validate_queue(queue):
    """A queue can never claim eligibility, admitted views or a passed benchmark gate."""
    if queue.get('schema') != SCHEMA:
        raise ValueError('Not a real-code candidate queue')
    if queue.get('independent_evaluation_eligible') is not False or queue.get('admitted_views') != 0:
        raise ValueError('A candidate queue cannot be eligible for independent evaluation')
    for candidate in queue['candidates']:
        if candidate['eligible'] is not False or candidate['review_status'] != 'independent_review_required' or candidate['proposed_response'] is not None:
            raise ValueError('A collected candidate must stay not eligible, unreviewed and without an inferred response (eligible=false required)')
    if any(r['met'] for r in queue['benchmark_readiness']['requirements']) or queue['benchmark_readiness']['state'] != 'blocked':
        raise ValueError('Benchmark requirements cannot be met by an unreviewed queue')


def publish(queue, out_dir):
    """Write the immutable queue publication; refuses to reuse a folder."""
    root = Path(out_dir)
    if root.exists():
        raise FileExistsError('Refusing to reuse a queue publication: ' + str(root))
    validate_queue(queue)
    root.mkdir(parents=True)
    pieces = {'candidate_queue.json': {k: v for k, v in queue.items() if k not in ('benchmark_readiness',)},
              'blockers.json': {'schema': SCHEMA + '#blockers', 'state': queue['state'], 'blockers': queue['blockers']},
              'benchmark_readiness.json': queue['benchmark_readiness']}
    for name, body in pieces.items():
        (root / name).write_bytes((json.dumps(body, indent=1, sort_keys=True) + '\n').encode())
    readme = ('Real-code evaluation candidate queue (plain text).\n\n'
              f'State: {queue["state"]}. Candidate views: {len(queue["candidates"])} across {queue["summary"]["groups"]} repository and issue groups.\n'
              'Every candidate is not eligible. No permission, label or response has been established and no independent review has happened.\n'
              'blockers.json lists what must be resolved; benchmark_readiness.json lists the security-benchmark/1 preflight requirements still unmet.\n'
              'Independent security accuracy remains unmeasured until a frozen, independently adjudicated, permission-cleared benchmark exists.\n')
    (root / 'README.txt').write_bytes(readme.encode())
    files = {p.name: {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in sorted(root.iterdir())}
    manifest = {'schema': SCHEMA + '#publication', 'state': queue['state'], 'files': files}
    (root / 'manifest.json').write_bytes((json.dumps(manifest, indent=1, sort_keys=True) + '\n').encode())
    return manifest
