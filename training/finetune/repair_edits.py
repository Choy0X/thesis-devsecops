"""The edit format of security_chat_v3 repairs, and its deterministic conversion to the unified diff the rest of the pipeline applies.

A unified diff asks a language model for three things at once: copy the context lines exactly, copy the changed lines exactly, and count them into the
hunk headers. The first and the last are bookkeeping that a program can do, and the evaluation showed they are where repairs fail (context copied
inexactly, wrong counts). An edit names only what changes:

    @@ 27
    -    const target = path.join(base, name);
    +    const target = path.join(base, path.basename(name));

``@@ N`` is the number of the first line the edit removes, as shown in the numbered code (for a pure insertion, the line it is inserted before).
Removed lines (``-`` followed by the exact line) come first, then added lines (``+`` followed by the line). There are no file headers, no context and no
counts. Several edits are written one after the other in ascending order and must not overlap.

No network, no torch: this module is part of the Colab bundle.
"""
from __future__ import annotations

import difflib
import re

HEADER = re.compile(r'^@@ (\d+)$')
EDIT_START = re.compile(r'\A@@ \d+\n')
MAX_EDITS = 4
MAX_CHANGED_LINES = 24


def looks_like_edits(text):
    """True for the edit format; a unified diff starts with file headers and a hunk header with commas, so the two cannot be confused."""
    return isinstance(text, str) and EDIT_START.match(text) is not None


def _lines(text):
    text = text if text.endswith('\n') or not text else text + '\n'
    return text.split('\n')[:-1] if text else []


def parse_edits(text):
    """([{'start', 'old', 'new'}], None) or (None, reason). Reasons: empty, not_edit_format, stray_line, edit_order, too_many_edits, too_many_lines."""
    if not isinstance(text, str) or not text.strip():
        return None, 'empty'
    if not looks_like_edits(text):
        return None, 'not_edit_format'
    edits, current, phase = [], None, None
    for line in _lines(text):
        header = HEADER.match(line)
        if header:
            current = {'start': int(header.group(1)), 'old': [], 'new': []}
            edits.append(current)
            phase = 'old'
        elif line.startswith('-') and current is not None:
            if phase != 'old':
                return None, 'edit_order'
            current['old'].append(line[1:])
        elif line.startswith('+') and current is not None:
            phase = 'new'
            current['new'].append(line[1:])
        else:
            return None, 'stray_line'
    if not edits:
        return None, 'not_edit_format'
    if len(edits) > MAX_EDITS:
        return None, 'too_many_edits'
    if sum(len(e['old']) + len(e['new']) for e in edits) > MAX_CHANGED_LINES:
        return None, 'too_many_lines'
    if any(not e['old'] and not e['new'] for e in edits):
        return None, 'empty_edit'
    return edits, None


def render_edits(edits):
    out = []
    for edit in edits:
        out.append(f"@@ {edit['start']}")
        out.extend('-' + line for line in edit['old'])
        out.extend('+' + line for line in edit['new'])
    return '\n'.join(out) + '\n'


def edits_from_versions(before, after):
    """The edits that turn `before` into `after` (both complete texts, line by line); [] when they are equal."""
    old, new = _lines(before), _lines(after)
    edits = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        edits.append({'start': i1 + 1, 'old': old[i1:i2], 'new': new[j1:j2]})
    return edits


def _locate(source_lines, edit):
    """Where the edit's old block really is: its own line when exact, else another line where the block occurs exactly, else None."""
    block = edit['old']
    if not block:
        return edit['start']
    at = edit['start'] - 1
    if 0 <= at and source_lines[at:at + len(block)] == block:
        return edit['start']
    hits = [i + 1 for i in range(len(source_lines) - len(block) + 1) if source_lines[i:i + len(block)] == block]
    return hits[0] if len(hits) == 1 else None


def check_edits(edits, source):
    """Do the edits apply to `source` exactly as written? {'state': 'applies_exactly'} or {'state': 'rejected', 'code', 'detail', 'relocated_exactly'}.

    Codes: out_of_range, overlap, wrong_line (the old lines are exact but at another line), old_lines_mismatch, whitespace_mismatch, no_change.
    `relocated_exactly` is True for wrong_line: the content is right, only the number is wrong, which is informative and never counted as applying."""
    lines = _lines(source)
    previous_end = 0
    for index, edit in enumerate(edits):
        start, old = edit['start'], edit['old']
        if start < 1 or start - 1 + len(old) > len(lines) or (not old and start > len(lines) + 1):
            return {'state': 'rejected', 'code': 'out_of_range', 'detail': f'edit {index} starts at line {start}, the code has {len(lines)} lines', 'relocated_exactly': False}
        if start - 1 < previous_end:
            return {'state': 'rejected', 'code': 'overlap', 'detail': f'edit {index} overlaps the previous edit', 'relocated_exactly': False}
        actual = lines[start - 1:start - 1 + len(old)]
        if actual != old:
            where = _locate(lines, edit)
            if where is not None:
                return {'state': 'rejected', 'code': 'wrong_line', 'detail': f'edit {index}: the removed lines are at line {where}, not {start}', 'relocated_exactly': True}
            if [l.strip() for l in actual] == [l.strip() for l in old]:
                return {'state': 'rejected', 'code': 'whitespace_mismatch', 'detail': f'edit {index}: indentation differs', 'relocated_exactly': False}
            return {'state': 'rejected', 'code': 'old_lines_mismatch', 'detail': f'edit {index}: the removed lines are not the code at line {start}', 'relocated_exactly': False}
        previous_end = start - 1 + len(old)
    if all(edit['old'] == edit['new'] for edit in edits):
        return {'state': 'rejected', 'code': 'no_change', 'detail': 'the edits change nothing', 'relocated_exactly': False}
    return {'state': 'applies_exactly'}


def apply_edits(edits, source):
    """The edited text (check_edits must have passed)."""
    lines = _lines(source)
    out, cursor = [], 0
    for edit in edits:
        at = edit['start'] - 1
        out.extend(lines[cursor:at])
        out.extend(edit['new'])
        cursor = at + len(edit['old'])
    out.extend(lines[cursor:])
    return '\n'.join(out) + '\n'


def to_unified_diff(edits, source, path, *, context=2):
    """(patch, None) in the form `repair_validation.assess_patch` applies, or (None, check result) when the edits do not apply."""
    result = check_edits(edits, source)
    if result['state'] != 'applies_exactly':
        return None, result
    before, after = _lines(source), _lines(apply_edits(edits, source))
    diff = difflib.unified_diff([l + '\n' for l in before], [l + '\n' for l in after], fromfile=f'a/{path}', tofile=f'b/{path}', n=context)
    return ''.join(diff), None


def assess(text, source, path):
    """One verdict for a repair answer in the edit format: {'state', 'code', 'detail', 'relocated_exactly', 'patch', 'edits'}. Never raises."""
    edits, reason = parse_edits(text)
    if edits is None:
        return {'state': 'rejected', 'code': reason, 'detail': 'the patch is not a valid edit list', 'relocated_exactly': False, 'patch': None, 'edits': None}
    patch, failure = to_unified_diff(edits, source, path)
    if patch is None:
        return {**failure, 'patch': None, 'edits': edits}
    return {'state': 'applies_exactly', 'code': None, 'detail': '', 'relocated_exactly': False, 'patch': patch, 'edits': edits}
