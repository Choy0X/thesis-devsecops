"""Evidence-grounded SFT eligibility, independent of automatic proof coverage."""
from __future__ import annotations

import difflib
import hashlib
import re

from . import sft_evidence as source

POLICY = 'sft-admission/1'
TASKS = ('discovery', 'condition_assessment', 'localization', 'repair', 'regression_test_design')
_MATHJS_BEFORE = 'df36ad309aa5d3ff0485910fc4ab95d7f77716eb9b765ae99a243cee25fe605c'
_MATHJS_AFTER = 'f66b5bf7f2791e46d13d83d2392fd504e013931a75b8407ae776e6030ceb2548'
_MATHJS_CONTEXT = {
    'before': ('a2858e266ad2baf7a71730629b76e10881a15330', '86cddf8f1de36d7d77e539d7fc0faf4b86e7ea7d080f3ab33c88ff4e5d59c036', _MATHJS_BEFORE),
    'after': ('ecb80514e80bce4e6ec7e71db8ff79954f07c57e', 'c872303574db0ee951035d569d12000e4767f54e1fb030f42a2037bcfc47c580', _MATHJS_AFTER)}
_MATHJS_HELPER = 'function hasOwnProperty (object, property) {\n  return object && Object.hasOwnProperty.call(object, property)\n}'


def _sha(text):
    return hashlib.sha256((text or '').encode()).hexdigest() if text is None or isinstance(text, str) else None


def _quotes(record, side):
    """Extract exact target-owned source windows, without assigning labels."""
    before, after = record.get('vulnerable_code') or '', record.get('fixed_code') or ''
    selected = before if side == 'before' else after
    other = after if side == 'before' else before
    lines = selected.splitlines(keepends=True)
    other_lines = set(other.splitlines())
    candidates = [index for index, line in enumerate(lines)
                  if re.search(r'[A-Za-z0-9_$]', line)
                  and not line.lstrip().startswith(('//', '/*', '*'))]
    changed = [index for index in candidates if lines[index].rstrip('\r\n') not in other_lines]
    if not changed:
        changed = candidates[:1]

    def unique(quote):
        start = selected.find(quote)
        return bool(quote) and start >= 0 and selected.find(quote, start + 1) < 0

    def window(index):
        quote = lines[index].strip()
        if unique(quote):
            return quote
        # Preserve exact line terminators when a short statement repeats. The
        # final quote mapper still checks executable ownership and uniqueness.
        for width in range(2, min(8, len(lines)) + 1):
            for start in range(max(0, index - width + 1), min(index, len(lines) - width) + 1):
                quote = ''.join(lines[start:start + width]).strip()
                if unique(quote):
                    return quote
        return selected.strip()

    quotes = []
    for index in changed[:4]:
        quote = window(index)
        if not any(entry['quote'] == quote for entry in quotes):
            quotes.append({'quote': quote, 'reason': 'Quoted executable source within the reviewed unit.',
                           'path': record.get('file_path_' + side), 'side': side,
                           'owner': 'target', 'source_sha256': _sha(selected)})
        if len(quotes) == 4:
            break
    return quotes


def _mathjs_context_support(record, bound, attributed):
    """Vetted teaching facts require their known binding and displayed helper.

    This limits only this authored exemplar, never generic source/label support.
    Authenticated full-module bytes alone do not make omitted context visible.
    """
    if not (_sha(record.get('vulnerable_code')) == _MATHJS_BEFORE
            and record.get('package_identity') == 'npm:mathjs'
            and any('CVE-2020-7743' in item['advisory'].get('aliases', []) for item in attributed)):
        return {}, []
    supported, missing = {}, []
    path = 'src/utils/object.js'
    for side, (revision, module_sha, unit_sha) in _MATHJS_CONTEXT.items():
        snapshot = bound['snapshots'].get(side, {})
        context = source.unpack(record.get('context_' + side), {})
        items = context.get('items', []) if isinstance(context, dict) else []
        items = items if isinstance(items, list) else []
        shown = any(isinstance(item, dict) and item.get('kind') == 'callee'
                    and item.get('name') == 'hasOwnProperty' and item.get('path') == path
                    and item.get('text') == _MATHJS_HELPER for item in items)
        module = snapshot.get('files', {}).get(path)
        supported[side] = bool(shown and record.get('repository_identity') == 'github.com/josdejong/mathjs'
            and record.get('symbol') == 'deepExtend' and record.get('file_path_' + side) == path
            and record.get('relevant_parent_commit' if side == 'before' else 'fix_commit') == revision
            and snapshot.get('revision') == revision and _sha(module) == module_sha
            and module.count(_MATHJS_HELPER) == 1
            and _sha(record.get('vulnerable_code' if side == 'before' else 'fixed_code')) == unit_sha)
        if not supported[side]:
            missing.append(source.reason('vetted_context_missing', 'context_' + side,
                'The vetted helper definition and exact module binding are not both established in the displayed ' + side + ' context.',
                'Supply the authenticated hasOwnProperty callee definition from the same exact src/utils/object.js revision, or retain scoped generic supervision.'))
    return supported, missing


def _supervision(record, attributed, rich_sides):
    scope = 'The behavior of ' + str(record.get('symbol') or record.get('file_path_before')) + ' in the supplied source and context.'
    before, after = record.get('vulnerable_code') or '', record.get('fixed_code') or ''
    evidence, fixed_evidence = _quotes(record, 'before'), _quotes(record, 'after') if after else []
    result = {'review_scope': scope,
              'assumptions': ['Review is limited to the shown unit and context. Application reachability and target-specific triggering inputs are not established.'],
              'finding': {'summary': 'The quoted lines localize the affected code within the reviewed unit; target-specific triggers and impact are not established here.',
                          'location': str(record.get('file_path_before')) + ':' + str(record.get('symbol') or '')},
              'evidence': evidence, 'fixed_evidence': fixed_evidence,
              'fixed': {'eligible': bool(after and fixed_evidence), 'scope': scope},
              'patch': ''.join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                          fromfile='a/' + str(record.get('file_path_before')), tofile='b/' + str(record.get('file_path_after')))) if after else '',
              'behavioral_intent': 'Apply the exact upstream change within the advisory-attributed unit. Target-specific preservation details are not independently documented.',
              'tests': []}
    # This is a vetted training exemplar, NOT a general mechanism validator.
    # Exact source identities prevent these authored facts spreading to other units.
    if rich_sides.get('before'):
        result['assumptions'] = [
            'An application passes attacker-controlled configuration data to deepExtend.',
            'The source object is produced by JSON.parse, so a __proto__ property is an own property.',
            'The destination starts as an ordinary empty object and built-in prototypes have not been modified.']
        result['finding'].update(
            summary='deepExtend recursively copies attacker-controlled own property names into an ordinary destination object.',
            violated_invariant='Configuration updates must not write through inherited prototype objects.',
            attacker_control='Own property names and nested values in the source configuration object.',
            impact='An own __proto__ key can select the inherited Object.prototype destination and copy a nested property into it under the stated assumptions.')
        result['evidence'] = [
            {'quote': quote, 'reason': explanation, 'path': record['file_path_before'], 'side': 'before'}
            for quote, explanation in (
                ('if (hasOwnProperty(b, prop)) {', 'The source-side own-property check admits an own __proto__ key.'),
                ('if (a[prop] && a[prop].constructor === Object) {', 'The destination lookup can resolve the inherited Object.prototype object.'),
                ('deepExtend(a[prop], b[prop])', 'Recursive copying then writes into the selected inherited object.'),
                ('      } else {\n        a[prop] = b[prop]\n      }', 'The recursive scalar assignment writes the attacker-supplied nested value.'))]
        if rich_sides.get('after'):
            result['fixed_evidence'] = [{'quote': 'if (hasOwnProperty(b, prop) && !(prop in Object.prototype) && !(prop in Function.prototype)) {',
                'reason': 'Before copying or recursing, the added checks exclude property names present on these built-in prototypes.',
                'path': record['file_path_after'], 'side': 'after'}]
            result['behavioral_intent'] = ('Preserve copying and recursive merging of ordinary own configuration properties, while skipping names present on Object.prototype or Function.prototype. Array rejection remains unchanged.')
            result['tests'] = [{
                'purpose': 'Proposed isolated regression for the own __proto__ input, ordinary nested merging, and existing array rejection.',
                'test_code': "// Proposed only. Run separately against each exact revision in an isolated disposable runtime.\nconst assert = require('node:assert/strict');\nconst marker = '__sft_mathjs_probe__';\nassert.equal(Object.hasOwnProperty.call(Object.prototype, marker), false);\ntry {\n  deepExtend({}, JSON.parse('{\"__proto__\":{\"__sft_mathjs_probe__\":true}}'));\n  assert.equal(Object.hasOwnProperty.call(Object.prototype, marker), false);\n  assert.deepEqual(deepExtend({nested:{keep:1}}, {nested:{add:2}}), {nested:{keep:1,add:2}});\n  assert.throws(() => deepExtend({}, []), TypeError);\n} finally {\n  delete Object.prototype[marker];\n}\n",
                'expected_observation': 'The prototype-property assertion is expected to fail before the fix and pass after it under the stated assumptions. The ordinary merge and array-rejection assertions are expected to pass on both revisions. These are proposed expectations, not observations.',
                'execution_state': 'not_run'}]
    if rich_sides and not rich_sides.get('after'):
        result['fixed']['eligible'] = False
        result['fixed_evidence'] = []
    return result


def admit_record(record, *, root=None, evidence=None, provider=None):
    """Compute a new disposition. Never mutate records, issue receipts or run proof.

    ``evidence`` is reserved for test instrumentation; dictionary claims are ignored.
    Production source authentication always runs, even with a supplied provider.
    """
    result = {'policy': POLICY, 'record_id': record.get('record_id'), 'source_integrity': 'unknown',
              'label_support': 'unknown', 'training_eligibility': 'ineligible',
              'execution_verification': 'not_run', 'proof_engine_status': 'not_evaluated',
              'eligible_tasks': [], 'task_decisions': {}, 'reasons': [],
              'provenance': {'side_integrity': {'before': 'unknown', 'after': 'unknown'}}, 'supervision': {}}
    side_problems = {'before': [], 'after': []}
    rich_context_reasons = []
    # Validate record-controlled container shapes before traversing them. Context
    # validation stays per-side so a bad fixed context cannot erase good detection.
    for field in ('vulnerable_code', 'file_path_before', 'symbol', 'repository_identity', 'package_identity',
                  'relevant_parent_commit', 'fix_commit', 'language', 'ecosystem'):
        if record.get(field) is not None and not isinstance(record[field], str):
            result['reasons'].append(source.reason('malformed_evidence_field', field, 'This source identity field must be a string.', 'Restore the captured source identity field.'))
    for field in ('advisory_aliases', 'source_record_hashes', 'raw_record_keys'):
        value = source.unpack(record.get(field), [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            result['reasons'].append(source.reason('malformed_evidence_field', field, 'This evidence field must be an array of strings.', 'Restore the captured evidence identifiers.'))
    blobs = source.unpack(record.get('file_blob_hashes'), {})
    if not isinstance(blobs, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in blobs.items()):
        result['reasons'].append(source.reason('malformed_evidence_field', 'file_blob_hashes', 'Blob identities must be a mapping of strings.', 'Restore the captured source blob identities.'))
    if result['reasons']:
        result['task_decisions'] = {task: {'eligible': False, 'reasons': list(result['reasons'])} for task in TASKS}
        return result
    historical = source.unpack(record.get('admission_reasons'), [])
    if any('unsupported' in str(reason) and ('semantic' in str(reason) or 'proof' in str(reason)) for reason in historical):
        result['proof_engine_status'] = 'unsupported'
    reasons = result['reasons']
    bound = source.authenticate(record, root=root, provider=provider)
    if bound and bound.get('failure'):
        reasons.extend(bound['failure'])
        result['source_integrity'] = bound['source_integrity']
    elif not bound:
        reasons.append(source.reason('backend_source_unavailable', 'source', 'No backend-bound exact source evidence validates this record.', 'Recover or correct the exact source, parent, spans and permission evidence.'))
    else:
        side_problems = {side: source.source_problems(record, bound, side_only=side) for side in ('before', 'after')}
        problems = side_problems['before'] + side_problems['after']
        reasons.extend(problems)
        side_integrity = {side: 'invalid' if side_problems[side] else 'verified' if record.get(key) else 'unknown'
                          for side, key in (('before', 'vulnerable_code'), ('after', 'fixed_code'))}
        result['source_integrity'] = ('verified' if all(state == 'verified' for state in side_integrity.values())
                                      else 'partial' if side_integrity['before'] == 'verified' else side_integrity['before'])
        foundation = bound['foundation']
        unit_attribution = bound.get('unit_attribution', {'before': 'verified', 'after': 'verified'})
        result['provenance'] = {**foundation, 'side_integrity': side_integrity, 'unit_attribution': unit_attribution,
            'context_discovery_index': source.context_discovery_index(record), 'context_fingerprints': {
            side: source.digest(source.unpack(record.get('context_' + side), {})) for side in ('before', 'after')},
            'unit_fingerprints': {'before': _sha(record.get('vulnerable_code')), 'after': _sha(record.get('fixed_code'))}}
        execution = source.authenticated_execution(record) if not problems else None
        status, attributed, failures = source.attribute(record, bound, unit_evidence=execution is not None)
        if execution is not None:
            result['execution_verification'] = 'verified'
            result['proof_engine_status'] = 'supported'
            result['provenance']['trusted_execution_id'] = execution['receipt_id']
        result['label_support'] = status
        reasons.extend(failures)
        if attributed:
            result['provenance']['attribution'] = [{'advisory_sha256': item['sha256'], 'references': item['references'], 'basis': item['basis']} for item in attributed]
            exclusive = next((item['exclusive_unit_change'] for item in attributed if item.get('exclusive_unit_change')), None)
            if exclusive:
                result['provenance']['exclusive_unit_change'] = exclusive
        if side_integrity['before'] == 'verified' and status == 'attributed':
            usable_after = side_integrity['after'] == 'verified' and unit_attribution.get('after') == 'verified'
            usable_record = record if usable_after else {**record, 'fixed_code': None}
            rich_sides, rich_context_reasons = _mathjs_context_support(usable_record, bound, attributed)
            result['supervision'] = _supervision(usable_record, attributed, rich_sides)
            if rich_sides:
                result['provenance']['vetted_context_support'] = rich_sides
            if not usable_after:
                result['supervision']['fixed']['eligible'] = False
                result['supervision'].update(fixed_evidence=[], patch='', tests=[])
    path = str(record.get('file_path_before') or '').replace('\\', '/').lower()
    if (record.get('production_relevance') not in (None, 'production') or record.get('is_test')
            or re.search(r'(^|/)(tests?|__tests__|fixtures?|examples?)/|[._](test|spec)\.[^/]+$', path)):
        reasons.append(source.reason('nonproduction_unit', 'production_relevance', 'This record is test, fixture, example, or context-only source.', 'Select the independently attributed production unit.'))
    permission = source.permission_resolution(record, result['provenance'])
    result['provenance']['permission_resolution'] = permission
    if permission['effective_status'] != 'permissive':
        reasons.append(source.reason('permission_not_usable', 'data_usage_status', 'The raw permission metadata is not resolved by the exact source evidence under this permission policy.', 'Recover compatible, source-bound permission evidence and resolve non-placeholder restrictions.'))
    # Historical mining flags do not override independently established unit evidence.
    base_reasons = [problem for problem in reasons if problem not in side_problems['after']]
    positive = result['provenance']['side_integrity']['before'] == 'verified' and result['label_support'] == 'attributed' and not base_reasons
    supervision = result['supervision']
    for task in TASKS:
        exclusions = list(base_reasons)
        if task == 'regression_test_design':
            exclusions.extend(rich_context_reasons)
        if task in ('repair', 'regression_test_design') and result['provenance']['side_integrity']['after'] != 'verified':
            exclusions.extend(side_problems['after'] or [source.reason('after_source_unavailable', 'fixed_code', 'The fixed side has not been authenticated for this task.', 'Recover the exact fixed unit and its same-revision context.')])
        if task in ('repair', 'regression_test_design') and result['provenance'].get('unit_attribution', {}).get('after', 'verified') != 'verified':
            exclusions.append(source.reason('after_unit_attribution_pending', 'fixed_code', 'The fixed source bytes do not have an established exact target-unit boundary.', 'Recover the exact fixed miner unit and its bounds.'))
        if positive and task == 'repair' and not supervision.get('patch'):
            exclusions.append(source.reason('repair_target_missing', 'fixed_code', 'An exact repair target is unavailable.', 'Recover the corresponding fixed unit.'))
        if positive and task == 'regression_test_design' and not supervision.get('tests'):
            exclusions.append(source.reason('regression_design_unavailable', 'tests', 'No target-specific proposed test is grounded in the available evidence.', 'Provide source-grounded input, expected behavior and a controlled test design.'))
        result['task_decisions'][task] = {'eligible': positive and not exclusions, 'reasons': exclusions}
        if positive and not exclusions:
            result['eligible_tasks'].append(task)
        for problem in exclusions:
            if problem not in result['reasons']:
                result['reasons'].append(problem)
    # Task-specific failures do not revoke the supported tasks already assessed.
    count = len(result['eligible_tasks'])
    result['training_eligibility'] = 'eligible' if count == len(TASKS) else 'partial' if count else 'ineligible'
    return result
