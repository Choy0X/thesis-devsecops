"""Exact in-memory unified-diff applicability. Never execute source or tests."""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import PurePosixPath
import re

from training.finetune import serialization

HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\r?\n?$')


def _path(header, prefix):
    value = header[len(prefix):].rstrip('\r\n').split('\t')[0]
    if value.startswith(('a/', 'b/')):
        value = value[2:]
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or '\\' in value or ':' in value or
            any(part in ('.', '..', '.git') for part in path.parts) or path.as_posix() != value):
        raise ValueError('Unsafe or unsupported patch path')
    return value


def assess_patch(patch, sources, *, allowed_paths, keep_text=False):
    result = {'schema': 'repair-applicability/1', 'state': 'rejected', 'execution_state': 'not_run',
              'security_correctness': 'not_adjudicated', 'functional_correctness': 'not_run'}
    try:
        if not isinstance(patch, str) or not patch or len(patch.encode()) > 2 << 20:
            raise ValueError('Missing or oversized unified diff')
        lines, i, changed, hashes, texts = patch.splitlines(True), 0, [], {}, {}
        while i < len(lines):
            if not lines[i].startswith('--- '):
                if changed and lines[i][:1] in (' ', '-', '+'):
                    # Hunk lines remain after a hunk whose header declared fewer: a wrong line count, not missing file headers.
                    raise ValueError('Hunk body exceeds declared line counts')
                raise ValueError('Expected unified diff file headers; binary, rename and mode changes are unsupported')
            old = _path(lines[i], '--- ')
            i += 1
            if i >= len(lines) or not lines[i].startswith('+++ '):
                raise ValueError('Missing new-file header')
            new = _path(lines[i], '+++ ')
            i += 1
            if old != new or old not in allowed_paths or old not in sources or old in changed:
                raise ValueError('Patch must modify an authenticated allowed existing source file once')
            original, cursor, out, hunks = sources[old].splitlines(True), 0, [], 0
            while i < len(lines) and lines[i].startswith('@@ '):
                match = HUNK.fullmatch(lines[i])
                if not match:
                    raise ValueError('Malformed hunk header')
                start, count, new_start, new_count = [int(value) if value is not None else 1 for value in match.groups()]
                position = start if count == 0 else start - 1
                new_position = new_start if new_count == 0 else new_start - 1
                if position < cursor or position > len(original):
                    raise ValueError('Overlapping or out-of-range source hunk')
                out.extend(original[cursor:position])
                cursor = position
                if len(out) != new_position:
                    raise ValueError('New-file hunk offset disagrees with previous edits')
                i += 1
                consumed = produced = 0
                while consumed < count or produced < new_count:
                    if i >= len(lines) or lines[i][:1] not in (' ', '-', '+'):
                        raise ValueError('Incomplete or unsupported hunk body')
                    sign, body = lines[i][0], lines[i][1:]
                    i += 1
                    if i < len(lines) and lines[i].startswith('\\ No newline at end of file'):
                        body = body[:-1] if body.endswith('\n') else body
                        i += 1
                    if sign in (' ', '-'):
                        if cursor >= len(original) or original[cursor] != body:
                            raise ValueError('Patch context does not exactly match authenticated source')
                        cursor += 1
                        consumed += 1
                    if sign in (' ', '+'):
                        out.append(body)
                        produced += 1
                    if consumed > count or produced > new_count:
                        raise ValueError('Hunk body exceeds declared line counts')
                hunks += 1
            if not hunks:
                raise ValueError('File patch has no hunks')
            out.extend(original[cursor:])
            modified = ''.join(out)
            if modified == sources[old]:
                raise ValueError('Patch makes no source change')
            changed.append(old)
            hashes[old] = hashlib.sha256(modified.encode()).hexdigest()
            texts[old] = modified
        if not changed:
            raise ValueError('No source change')
        result.update(state='applies_exactly', changed_paths=changed, modified_sha256=hashes)
        if keep_text:
            result['modified_sources'] = texts
    except (ValueError, TypeError, KeyError) as error:
        result['detail'] = str(error)
    return result


def measure(raw_generations, views, records, data_dir, *, protocol=serialization.SERIALIZATION_ID):
    from training.finetune import generation_validation
    indexed = {}
    for raw in raw_generations:
        if isinstance(raw, dict):
            indexed.setdefault(raw.get('view_id'), []).append(raw)
    rows = []
    for view in views:
        if view['task'] != 'repair':
            continue
        result = {'view_id': view['view_id'], 'state': 'not_attempted', 'execution_state': 'not_run',
                  'security_correctness': 'not_adjudicated', 'functional_correctness': 'not_run'}
        supplied = indexed.get(view['view_id'], [])
        if len(supplied) != 1:
            if supplied: result['state'] = 'duplicate_output'
            rows.append(result)
            continue
        text = supplied[0].get('text')
        answer, problems = ((serialization.parse_response('repair', text, protocol=protocol) if protocol in serialization.SECURITY_PROTOCOLS
                             else serialization.parse_response('repair', text)) if isinstance(text, str)
                            else (None, ['Missing or nontext generation']))
        if answer is None:
            result.update(state='malformed', detail=problems)
        elif answer['status'] != 'finding' or not answer.get('patch'):
            result['state'] = 'no_patch'
        else:
            record = records.get(view['record_id'], {})
            state, captured, revision = generation_validation._sources(view, record, data_dir)
            if state != 'verified_snapshot_bytes':
                result.update(state='source_unavailable', detail=state)
            else:
                sources = dict(captured)
                if any(sources[path] != body for path, body in captured):
                    result.update(state='source_unavailable', detail='conflicting_logical_source_snapshots')
                    rows.append(result)
                    continue
                target = view.get('allowed_patch_paths') or [record.get('file_path_before') or record.get('file_path_after')]
                if target == [None] and len(sources) == 1:
                    target = list(sources)
                result.update(assess_patch(answer['patch'], sources, allowed_paths=set(target) - {None}))
        rows.append(result)
    return {'schema': 'repair-assessment/1', 'selected_views': len(rows),
            'status_counts': dict(Counter(row['state'] for row in rows)), 'per_view': rows,
            'execution_state': 'not_run', 'security_correctness': 'not_adjudicated',
            'interpretation': 'Exact patch application is mechanical evidence, not a validated security or functional repair.'}
