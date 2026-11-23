"""Pure SFT rendering downstream of authenticated admission, never label authority.

No source recovery, proof execution, code execution, model or tokenizer is used.
Each task can fail independently. Original source ownership is checked before
evidence is mapped through the versioned comment renderer into visible quotes.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath

from app.backend.pipeline.views import rendering
from training.finetune import serialization as historical_serialization
from training.finetune import sft_contracts as ct

POLICY = 'sft-views/1'
TASKS = ('discovery', 'condition_assessment', 'localization', 'repair', 'regression_test_design')
DETECTION_TASKS = rendering.DETECTION_TASKS
SCOPED_NEGATIVE = ('No finding is limited to the addressed issue within the supplied review scope; '
                   'other vulnerabilities have not been ruled out.')


def rendering_identity():
    """Explicit SFT display variant; shared/historical callers retain defaults."""
    return rendering.identity(preserve_istanbul_comments=True)


def _digest(value):
    # Admission's sft_evidence.digest uses this exact canonical JSON convention.
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _source_digest(source):
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


class _ViewError(ValueError):
    def __init__(self, code, field='supervision', detail=None):
        super().__init__(code)
        self.code, self.field, self.detail = code, field, detail or code


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _exclusion(task, side, code, field, detail=None):
    return {'task': task, 'side': side, 'code': code, 'field': field,
            'detail': detail or code,
            'remediation': 'Supply authenticated task-specific facts and exact evidence visible in this source snapshot.'}


def _expected_fingerprint(admission, kind, side):
    provenance = admission.get('provenance')
    fingerprints = provenance.get(kind) if isinstance(provenance, dict) else None
    value = fingerprints.get(side) if isinstance(fingerprints, dict) else None
    prefix = 'source' if kind == 'unit_fingerprints' else 'context'
    if not isinstance(value, str) or len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
        raise _ViewError(prefix + '_fingerprint_missing', 'admission.provenance.' + kind + '.' + side)
    return value


def _require_verified_side(admission, side):
    provenance = admission.get('provenance')
    side_integrity = provenance.get('side_integrity') if isinstance(provenance, dict) else None
    # Older globally verified results predate the per-side field. Partial
    # results always require an explicit authenticated selected-side verdict.
    if (admission.get('source_integrity') == 'verified' and isinstance(provenance, dict)
            and 'side_integrity' not in provenance):
        return
    if not isinstance(side_integrity, dict) or side_integrity.get(side) != 'verified':
        raise _ViewError('source_side_not_verified', 'admission.provenance.side_integrity.' + side)


def _json_state(text):
    """Recognize complete JSON or a bounded grammar-valid prefix, without edits.

    Source selection can stop inside a manifest string at its immutable budget.
    EOF is allowed only where additional JSON characters could complete the
    grammar. Comments, invalid escapes, control characters and trailing tokens
    remain errors. This is context display validation, never execution evidence.
    """
    class PrefixEnd(Exception):
        pass

    if not text.strip() or len(text) > 100000:
        raise ValueError('JSON display budget or empty input')
    index, size = 0, len(text)

    def char():
        if index == size:
            raise PrefixEnd
        return text[index]

    def whitespace():
        nonlocal index
        while index < size and text[index] in ' \t\r\n':
            index += 1

    def consume(expected):
        nonlocal index
        if char() != expected:
            raise ValueError('unexpected JSON token')
        index += 1

    def string():
        nonlocal index
        consume('"')
        while True:
            current = char()
            index += 1
            if current == '"':
                return
            if ord(current) < 32:
                raise ValueError('JSON string control character')
            if current == '\\':
                escape = char()
                index += 1
                if escape == 'u':
                    for _ in range(4):
                        if char() not in '0123456789abcdefABCDEF':
                            raise ValueError('invalid JSON Unicode escape')
                        index += 1
                elif escape not in '"\\/bfnrt':
                    raise ValueError('invalid JSON escape')

    def digits():
        nonlocal index
        if char() not in '0123456789':
            raise ValueError('JSON number requires a digit')
        while index < size and text[index] in '0123456789':
            index += 1

    def number():
        nonlocal index
        if char() == '-':
            index += 1
        if char() == '0':
            index += 1
        else:
            digits()
        if index < size and text[index] == '.':
            index += 1
            digits()
        if index < size and text[index] in 'eE':
            index += 1
            if char() in '+-':
                index += 1
            digits()

    def value(depth=0):
        nonlocal index
        if depth > 64:
            raise ValueError('JSON display depth exceeded')
        whitespace()
        current = char()
        if current == '"':
            string()
        elif current in '-0123456789':
            number()
        elif current in 'tfn':
            for expected in {'t': 'true', 'f': 'false', 'n': 'null'}[current]:
                consume(expected)
        elif current in '[{':
            is_object = current == '{'
            closing = '}' if is_object else ']'
            index += 1
            whitespace()
            if char() == closing:
                index += 1
                return
            while True:
                if is_object:
                    string()
                    whitespace()
                    consume(':')
                value(depth + 1)
                whitespace()
                if char() == closing:
                    index += 1
                    return
                consume(',')
                whitespace()
        else:
            raise ValueError('invalid JSON value')

    try:
        value()
        whitespace()
        if index != size:
            raise ValueError('trailing JSON data')
        return 'complete'
    except PrefixEnd:
        return 'valid_prefix'


def _render_configuration(item):
    """Render code configurations by their format; strictly parse plain JSON.

    Unsupported formats cannot be silently dropped: a label may depend on that
    context. JSON literals stay byte-exact, including comment-like text. Budget
    prefixes are named as prefixes and never claimed to be complete JSON.
    """
    text = item.get('text')
    if not isinstance(text, str):
        raise _ViewError('configuration_format_unsupported', 'context.configuration')
    suffix = PurePosixPath(str(item.get('path') or '').replace('\\', '/')).suffix.lower()
    language = {'.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript', '.jsx': 'javascript',
                '.ts': 'typescript', '.mts': 'typescript', '.cts': 'typescript', '.tsx': 'tsx'}.get(suffix)
    if language:
        return rendering.render_code(text, language, preserve_istanbul_comments=True), {
            'format': language, 'state': 'complete', 'policy': rendering_identity()['policy'],
            'execution_evidence': False}
    if suffix == '.json':
        try:
            state = _json_state(text)
        except (ValueError, TypeError):
            raise _ViewError('configuration_format_unsupported', 'context.configuration') from None
        return None, {'format': 'json', 'state': state, 'policy': 'json-prefix-display/1',
                      'execution_evidence': False}
    raise _ViewError('configuration_format_unsupported', 'context.configuration')


def _selected_input(record, admission, side):
    _require_verified_side(admission, side)
    code_key, path_key = ('vulnerable_code', 'file_path_before') if side == 'before' else ('fixed_code', 'file_path_after')
    source = record.get(code_key)
    if not _nonempty(source):
        raise _ViewError('target_source_missing', code_key)
    expected = _expected_fingerprint(admission, 'unit_fingerprints', side)
    if expected != _source_digest(source):
        raise _ViewError('source_fingerprint_mismatch', code_key)
    context_key = 'context_' + side
    bundle = record.get(context_key)
    if isinstance(bundle, str):
        try:
            bundle = json.loads(bundle)
        except (ValueError, TypeError):
            raise _ViewError('context_shape_invalid', context_key) from None
    if bundle is None:
        bundle = {}
    if not isinstance(bundle, dict) or not isinstance(bundle.get('items', []), list):
        raise _ViewError('context_shape_invalid', context_key)
    expected_context = _expected_fingerprint(admission, 'context_fingerprints', side)
    if expected_context != _digest(bundle):
        raise _ViewError('context_fingerprint_mismatch', context_key)
    items = []
    for item in bundle.get('items', []):
        if not isinstance(item, dict):
            raise _ViewError('context_shape_invalid', context_key)
        if item.get('kind') in rendering.CODE_KINDS | rendering.UNCHANGED_KINDS:
            items.append({key: item.get(key) for key in ('kind', 'path', 'name', 'text')})
    target = {'code': source, 'path': record.get(path_key) or record.get('file_path') or '',
              'language': record.get('language'), 'symbol': record.get('symbol')}
    rendered = rendering.render_input(target, items, preserve_istanbul_comments=True)
    rendered['configuration'] = []
    for index, item in enumerate(items):
        if item.get('kind') in rendering.UNCHANGED_KINDS:
            source_rendering, metadata = _render_configuration(item)
            rendered['configuration'].append({'item': index, 'path': item.get('path'),
                                               'source_sha256': _source_digest(item['text']), **metadata})
            if source_rendering is not None:
                rendered['items'][index]['text'] = source_rendering['rendered']
                rendered['sources']['items'][index] = source_rendering
    return target, items, bundle, rendered


def _mapped_evidence(entries, side, target, items, rendered, *, admitted_unit_sha256):
    if not isinstance(entries, list) or not entries:
        raise _ViewError('evidence_missing', 'supervision.evidence')
    out, mapping = [], []
    target_owner = (target.get('path'), target['code'], rendered['sources']['target'], 'target')
    candidates = [target_owner]
    candidates += [(item.get('path'), item.get('text'), source, f'item:{index}')
                   for index, (item, source) in enumerate(zip(items, rendered['sources']['items'])) if source]
    for entry in entries:
        if not isinstance(entry, dict) or not _nonempty(entry.get('reason')):
            raise _ViewError('evidence_shape_invalid')
        evidence_side = entry.get('side')
        if evidence_side is not None and evidence_side != side:
            raise _ViewError('evidence_side_mismatch')
        quote, path = entry.get('quote'), entry.get('path')
        if 'owner' in entry or 'source_sha256' in entry:
            if entry.get('owner') != 'target':
                raise _ViewError('evidence_owner_invalid')
            if evidence_side is None:
                raise _ViewError('evidence_owner_side_missing')
            if not path or path != target.get('path'):
                raise _ViewError('evidence_owner_path_mismatch')
            actual_sha256 = rendered['sources']['target']['provenance']['source_sha256']
            if (entry.get('source_sha256') != admitted_unit_sha256
                    or entry.get('source_sha256') != actual_sha256
                    or actual_sha256 != _source_digest(target['code'])):
                raise _ViewError('evidence_owner_source_mismatch')
            # An authenticated descriptor selects a source, never an occurrence
            # within it. map_quote below retains its exact uniqueness/token gate.
            owners = [target_owner]
        else:
            owners = [candidate for candidate in candidates
                      if (not path or path == candidate[0]) and isinstance(quote, str) and quote in candidate[1]]
        if not owners:
            raise _ViewError('quote_not_owned')
        if len(owners) != 1:
            raise _ViewError('quote_ambiguous')
        owner_path, source, source_rendering, owner = owners[0]
        mapped = rendering.map_quote(source, source_rendering, quote, preserve_istanbul_comments=True)
        out.append({'quote': mapped['quote'], 'reason': entry['reason']})
        mapping.append({'owner': owner, 'path': owner_path, 'side': side,
                        'source_sha256': source_rendering['provenance']['source_sha256'],
                        'source_span': mapped['source_span'], 'rendered_span': mapped['rendered_span']})
    return out, mapping


def _make_view(record, admission, task, side, role, selected):
    supervision = admission.get('supervision', {})
    if not isinstance(supervision, dict):
        raise _ViewError('supervision_missing')
    target, items, bundle, rendered = selected
    fixed = side == 'after'
    scope = supervision.get('review_scope')
    if not _nonempty(scope):
        raise _ViewError('review_scope_missing', 'supervision.review_scope')
    assumptions = supervision.get('assumptions', [])
    if not isinstance(assumptions, list) or not all(isinstance(item, str) for item in assumptions):
        raise _ViewError('assumptions_invalid', 'supervision.assumptions')
    extra = {'review_scope': scope}
    if assumptions:
        extra['scenario'] = '\n'.join(assumptions)
    if task == 'condition_assessment':
        extra['condition'] = scope
    if task in ('repair', 'regression_test_design'):
        finding = supervision.get('finding')
        if not isinstance(finding, dict) or not _nonempty(finding.get('summary')):
            raise _ViewError('finding_missing', 'supervision.finding')
        extra['report'] = finding['summary']
    rendering.assert_identifier_free(rendered, extra)
    sections = ['[review scope]\n' + scope]
    if 'scenario' in extra:
        sections.append('[scenario assumptions]\n' + extra['scenario'])
    if 'condition' in extra:
        sections.append('[condition]\n' + extra['condition'])
    if 'report' in extra:
        sections.append('[report]\n' + extra['report'])
    visible_source = historical_serialization.render_context(rendered['target'], rendered['items'])
    sections.append(visible_source)
    context_text = '\n\n'.join(sections)
    evidence, mapped = _mapped_evidence(supervision.get('fixed_evidence' if fixed else 'evidence'),
                                       side, target, items, rendered,
                                       admitted_unit_sha256=_expected_fingerprint(admission, 'unit_fingerprints', side))
    findings = []
    if not fixed:
        finding = supervision.get('finding')
        if not isinstance(finding, dict):
            raise _ViewError('finding_missing', 'supervision.finding')
        findings = [copy.deepcopy(finding)]
    response = {'status': 'no_finding' if fixed else 'finding', 'findings': findings, 'evidence': evidence,
                'assumptions': list(assumptions) + ([SCOPED_NEGATIVE] if fixed else []),
                'missing_context': [], 'validation_state': 'not_validated'}
    if fixed:
        fixed_scope = supervision.get('fixed', {}).get('scope')
        if not _nonempty(fixed_scope) or fixed_scope != scope:
            raise _ViewError('fixed_scope_mismatch', 'supervision.fixed.scope')
    if task == 'repair':
        if not _nonempty(supervision.get('patch')) or not _nonempty(supervision.get('behavioral_intent')):
            raise _ViewError('repair_target_missing', 'supervision.patch')
        response.update(patch=supervision['patch'], behavioral_intent=supervision['behavioral_intent'])
    if task == 'regression_test_design':
        if not isinstance(supervision.get('tests'), list) or not supervision['tests']:
            raise _ViewError('proposed_tests_missing', 'supervision.tests')
        response['tests'] = copy.deepcopy(supervision['tests'])
    problems = ct.validate_response(task, response)
    if problems:
        raise _ViewError('response_contract_invalid', 'supervision', '; '.join(problems))
    response = ct.normalize(task, response)
    # The old hidden-fact heuristic adds no labels. It only rejects a target
    # that names source identifiers absent from this student's own snapshot.
    from app.backend.pipeline.views.core import hidden_facts
    explanation = '\n'.join([str(value) for finding in response['findings'] for value in finding.values()
                             if value is not None] + [entry['reason'] for entry in response['evidence']]
                            + response['assumptions'])
    if hidden_facts(explanation, visible_source):
        raise _ViewError('hidden_fact_risk', 'supervision')
    source_sha256, context_sha256 = _source_digest(target['code']), _digest(bundle)
    messages = [{'role': 'system', 'content': ct.CHAT_SYSTEM},
                {'role': 'user', 'content': ct.TASK_TEXT[task] + '\n\nSupplied source context:\n' + context_text}]
    pool = 'scoped_negative' if fixed else 'supported'
    family_id = record.get('family_id') or record.get('issue_group_id') or record.get('record_id')
    identity = {'policy': POLICY, 'record_id': record.get('record_id'), 'family_id': family_id,
                'task': task, 'side': side, 'role': role, 'messages': messages, 'response': response,
                'source_sha256': source_sha256, 'context_sha256': context_sha256,
                'admission_sha256': _digest(admission), 'rendering': rendering_identity(),
                'configuration': rendered['configuration']}
    subject_id = str(record.get('record_id')) + (':fixed' if fixed else ':original')
    return {'view_id': 'sft_' + _digest(identity)[:24], 'record_id': record.get('record_id'),
            'family_id': family_id, 'issue_group_id': record.get('issue_group_id'),
            'subject_id': subject_id, 'task': task, 'side': side, 'role': role, 'split': record.get('split'),
            'val_role': record.get('val_role'), 'pool': pool, 'eligible': True, 'weight': 1.0,
            'context_text': context_text, 'messages': messages, 'response': response,
            'response_json': json.dumps(response, ensure_ascii=False, separators=(', ', ': ')),
            'source_sha256': source_sha256, 'context_sha256': context_sha256,
            'provenance': {'policy': POLICY, 'admission_policy': admission['policy'],
                'admission_sha256': identity['admission_sha256'], 'source_sha256': source_sha256,
                'context_sha256': context_sha256, 'rendering': identity['rendering'], 'evidence': mapped,
                'configuration': rendered['configuration']},
            'lineage_json': json.dumps({'policy': POLICY, 'parent_record_id': record.get('record_id'),
                                       'side': side, 'source_sha256': source_sha256, 'context_sha256': context_sha256},
                                      sort_keys=True)}


def build_views(record, admission, *, role=None):
    """Render only admitted tasks; return structured, inspectable exclusions.

    ``admission`` is the trusted backend result of ``sft_admission.admit_record``.
    Imported record labels are never read. Required producer unit/context hashes
    bind each selected original snapshot before any rendering. This function
    does not authenticate a caller-supplied dict in place of the backend provider.
    """
    views, exclusions = [], []
    role = role if role is not None else record.get('role') or record.get('split') or 'unassigned'
    invalid = None
    if not isinstance(admission, dict) or admission.get('policy') != 'sft-admission/1':
        invalid = ('admission_policy_invalid', 'admission.policy')
    elif admission.get('record_id') != record.get('record_id'):
        invalid = ('admission_record_mismatch', 'admission.record_id')
    elif admission.get('source_integrity') not in ('verified', 'partial'):
        invalid = ('source_not_verified', 'admission.source_integrity')
    elif admission.get('label_support') != 'attributed':
        invalid = ('label_not_attributed', 'admission.label_support')
    if invalid:
        return {'views': [], 'exclusions': [_exclusion(task, 'before', *invalid) for task in TASKS]}
    supervision = admission.get('supervision')
    supervision = supervision if isinstance(supervision, dict) else {}
    fixed = supervision.get('fixed')
    fixed_eligible = isinstance(fixed, dict) and fixed.get('eligible') is True
    selected_cache = {}
    for task in TASKS:
        decision = admission.get('task_decisions', {}).get(task, {})
        if decision.get('eligible') is not True or task not in admission.get('eligible_tasks', []):
            exclusions.append(_exclusion(task, 'before', 'task_not_admitted', 'admission.task_decisions.' + task))
            continue
        sides = ['before'] + (['after'] if task in DETECTION_TASKS and fixed_eligible else [])
        if task in DETECTION_TASKS and not fixed_eligible:
            exclusions.append(_exclusion(task, 'after', 'fixed_not_admitted', 'supervision.fixed'))
        for side in sides:
            try:
                if task in ('repair', 'regression_test_design'):
                    _require_verified_side(admission, 'after')
                if side not in selected_cache:
                    try:
                        selected_cache[side] = _selected_input(record, admission, side)
                    except (_ViewError, rendering.RenderingError) as error:
                        selected_cache[side] = error
                if isinstance(selected_cache[side], Exception):
                    raise selected_cache[side]
                views.append(_make_view(record, admission, task, side, role, selected_cache[side]))
            except _ViewError as error:
                exclusions.append(_exclusion(task, side, error.code, error.field, error.detail))
            except rendering.RenderingError as error:
                exclusions.append(_exclusion(task, side, str(error), 'source.' + side))
    return {'views': views, 'exclusions': exclusions}
