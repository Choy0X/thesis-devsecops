"""Source learning from every mined record, without proof or review admission.

Targets are captured code and mechanical edit descriptions. An upstream change
is supervision for that change, not a claim that every changed unit is vulnerable
or that its fixed twin has been executed or independently verified.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from itertools import zip_longest
from collections import Counter

from training.finetune import sft_serialization

POLICY = 'mined-corpus/1'
TASKS = ('upstream_patch', 'code_completion', 'change_analysis')
CHUNK_BYTES = 768


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def source_chunks(text, limit=CHUNK_BYTES):
    """Losslessly split on UTF-8 character boundaries, preferring line ends."""
    if limit < 4:
        raise ValueError('source chunk budget must hold a UTF-8 character')
    chunks, start = [], 0
    while start < len(text):
        end, size = start, 0
        while end < len(text):
            width = len(text[end].encode('utf-8'))
            if size + width > limit:
                break
            size += width
            end += 1
        if end < len(text):
            newline = text.rfind('\n', start, end)
            if newline >= start + (end - start) // 2:
                end = newline + 1
        chunks.append((start, text[start:end]))
        start = end
    return chunks


def admit_record(record, **kwargs):
    return {'policy': 'sft-admission/1', 'record_id': record.get('record_id'),
        'source_integrity': 'mined_snapshot', 'label_support': 'not_required_for_source_learning',
        'training_eligibility': 'eligible', 'execution_verification': 'not_run',
        'proof_engine_status': 'not_required', 'eligible_tasks': list(TASKS),
        'task_decisions': {task: {'eligible': True, 'reasons': []} for task in TASKS},
        'reasons': [], 'provenance': {'schema': POLICY, 'basis': 'immutable_mined_record',
            'record_sha256': digest(record),
            'unit_fingerprints': {side: hashlib.sha256((record.get(field) or '').encode()).hexdigest()
                for side, field in (('before', 'vulnerable_code'), ('after', 'fixed_code'))}}}


def _patch(before, after):
    """A unified diff for the displayed fragments, including absent final LF."""
    lines = list(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                    fromfile='before', tofile='after'))
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)


def build_views(record, admission, *, role=None):
    views = []
    rid = record['record_id']
    family = record.get('issue_group_id') or rid
    language = str(record.get('language') or 'code')[:64]
    advisory = (record.get('explanation') or '')
    advisory = source_chunks(advisory, 512)[0][1] if advisory else ''
    before, after = record.get('vulnerable_code') or '', record.get('fixed_code') or ''
    source_sha256 = digest([before, after])

    def add(task, context, answer, origin, *, side='before', objective=None):
        if not isinstance(answer, str) or not answer.strip():
            return
        # Whitespace remains intact in source-backed responses. Leading source
        # whitespace is allowed for these text tasks by the SFT serializer.
        identity = {'policy': POLICY, 'record_id': rid, 'task': task,
                    'side': side, 'origin': origin, 'context': context, 'answer': answer}
        views.append({'view_id': 'mined_' + digest(identity)[:24], 'record_id': rid,
            'family_id': family, 'task': task, 'side': side, 'role': role,
            'pool': 'supported', 'eligible': True, 'weight': 1.0,
            'context_text': context, 'messages': sft_serialization.messages(task, context),
            'response': answer, 'source_sha256': source_sha256,
            'provenance': {'policy': POLICY, 'objective': objective or task,
                           'source_slice': origin, 'execution_state': 'not_run'},
            'lineage_json': json.dumps({'policy': POLICY, 'parent_record_id': rid,
                'side': side, 'objective': objective or task, 'source_slice': origin}, sort_keys=True)})

    # Both revisions remain useful even if the edit has no security attribution.
    # Identical revisions need only one set of completion targets per record.
    for side, code in [('before', before)] + ([('after', after)] if after != before else []):
        for offset, chunk in source_chunks(code):
            cut = min(len(chunk) // 4, 128)
            while cut < len(chunk) and not chunk[cut:].strip():
                cut -= 1
                if cut < 0:
                    break
            if cut < 0:
                continue
            prefix = code[max(0, offset - 128):offset] + chunk[:cut]
            context = (f'Language: {language}\nRevision: {side}\n'
                f'Continue the captured source at character offset {offset + cut}. '
                f'Return the next {len(chunk) - cut} characters only.\nSource prefix:\n' + prefix)
            add('code_completion', context, chunk[cut:],
                {'side': side, 'start': offset + cut, 'end': offset + len(chunk)}, side=side)

    # Line-level matching avoids quadratic character matching on long minified
    # source. Each edit is divided into explicit fragment pairs for long changes.
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    old_offsets, new_offsets = [0], [0]
    for line in old_lines:
        old_offsets.append(old_offsets[-1] + len(line))
    for line in new_lines:
        new_offsets.append(new_offsets[-1] + len(line))
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for operation, i, j, a, b in matcher.get_opcodes():
        if operation == 'equal':
            continue
        old, new = ''.join(old_lines[i:j]), ''.join(new_lines[a:b])
        for index, (left, right) in enumerate(zip_longest(source_chunks(old), source_chunks(new), fillvalue=None)):
            left_offset, left_text = left if left else (len(old), '')
            right_offset, right_text = right if right else (len(new), '')
            origin = {'before_start': old_offsets[i] + left_offset,
                'before_end': old_offsets[i] + left_offset + len(left_text),
                'after_start': new_offsets[a] + right_offset,
                'after_end': new_offsets[a] + right_offset + len(right_text), 'fragment': index}
            patch = _patch(left_text, right_text)
            if not patch:
                continue
            common = (f'Language: {language}\nEdit coordinates: ' + json.dumps(origin, sort_keys=True)
                + '\nAdvisory context (package-level, not proof for this unit):\n' + advisory)
            add('upstream_patch', common + '\nOriginal fragment:\n' + left_text,
                patch, origin)
            analysis = json.dumps({'operation': operation, 'removed': left_text, 'added': right_text,
                'execution_state': 'not_run'}, ensure_ascii=False)
            add('change_analysis', common + '\nObserved upstream fragment patch:\n' + patch,
                analysis, origin)

    if not views:
        # A future metadata-only record remains represented without fabricating code.
        summary = json.dumps({key: record.get(key) for key in ('language', 'symbol', 'file_path_before')})
        add('change_analysis', 'Summarize these captured record fields as JSON:\n' + summary,
            summary, {}, objective='metadata_summary')
    return {'views': views, 'exclusions': []}


def coverage_policy():
    return {'schema': POLICY, 'scope': 'all_mined_records', 'tasks': list(TASKS),
        'exclusion_filters': [], 'security_labels': 'not_inferred_from_mining',
        'evaluation': 'none_all_records_training', 'partition_policy': 'all_records_train/1',
        'max_source_chunk_bytes': CHUNK_BYTES}


def recover_partitions(plan, historical=None):
    """Assign every mined record to training, retaining previous roles as history."""
    groups = {}
    for rid, assignment in plan['records'].items():
        assignment['_source_split'] = (historical or {}).get(rid, {}).get('split')
        groups.setdefault(assignment['component'], []).append(assignment)
    for members in groups.values():
        split = role = 'train'
        for member in members:
            member['previous_assignment'] = {key: member.get(key) for key in ('split', 'role', 'quarantined')}
            member.pop('_source_split', None)
            member.update(split=split, role=role, quarantined=False)
        component = members[0]['component']
        if component in plan.get('groups', {}):
            plan['groups'][component].update(split=split, role=members[0]['role'], overrides={},
                reason='mined_corpus_group_partition')
    plan['counts'] = dict(Counter(member['split'] for member in plan['records'].values()))
    plan['historical_conflicted_components'] = list(plan.get('conflicted_components', []))
    plan['conflicted_components'] = []
