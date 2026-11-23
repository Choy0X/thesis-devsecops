"""Matched real clean functions for the evidence tiers (M, D, L).

A clean negative taught from unmodified real code must not be an easy slice: if every clean example were short and plain, "short and plain" would stand for "clean". So a
clean function is drawn from the untouched functions of the unit's own file, within a factor of 1.5 of the unit's size, and when the unit makes calls that look like sinks
(files, processes, requests, queries, rendering) so must the clean function. It must also show no flow of any analysed class. This proves the absence of what the tools
can see, not of every bug; the view's lineage says so (`clean_basis`).
"""
from __future__ import annotations

import hashlib
import re

from app.backend.pipeline import grounded_flow
from app.backend.pipeline import grounded_views as g

SIZE_FACTOR = 1.5
MAX_CHARS = 2500
CLEAN_BASIS = 'no_flow_matched'
_SINKISH = re.compile(r'exec|spawn|query|open|read|write|request|fetch|send|redirect|eval|render|subprocess|os\.|fs\.|http', re.I)


def untouched_functions(unit):
    """The functions of the unit's file at the vulnerable revision that the fix did not touch and that are not the unit itself, as source texts."""
    text = unit.get('file_before') or ''
    if not text.strip():
        return []
    try:
        from app.backend.pipeline import funcsplit
        parsed = funcsplit.parse_units(unit['language'], unit['file'], text)
    except Exception:                   # ignored: a file the splitter cannot parse yields no untouched functions
        return []
    fixed = {line.strip() for line in g._fix_lines(unit['before'], unit['after']) if line.strip()}
    out = []
    for source, _nested in parsed.values():
        source = source if isinstance(source, str) else str(source)
        if source.strip() == unit['before'].strip() or g.has_hint_comment(source) or any(line in source for line in fixed):
            continue
        out.append(source)
    return out


def _has_flow(source, language):
    return any(grounded_flow.find_flows(source, language, cwe) for cwe in grounded_flow.SUPPORTED)


def _order(source, unit_id):
    return hashlib.sha256(f'{unit_id}|{source}'.encode('utf-8')).hexdigest()


def matched_clean(unit, candidates, language, *, count=2):
    """Up to `count` clean functions among `candidates`, in a stable hash order: no flow of an analysed class, within a factor of 1.5 of the unit's size, substantial and
    numberable, and with a sink-like token whenever the unit's own code has one."""
    before = unit.get('before') or ''
    wants_sink = _SINKISH.search(before) is not None
    low, high = len(before) / SIZE_FACTOR, len(before) * SIZE_FACTOR
    chosen = []
    for source in candidates:
        if not (low <= len(source) <= high) or len(source) > MAX_CHARS or not g.substantial(source) or not g._numberable(source):
            continue
        if wants_sink and _SINKISH.search(source) is None:
            continue
        if _has_flow(source, language):
            continue
        chosen.append(source)
    chosen.sort(key=lambda s: _order(s, unit.get('unit_id', '')))
    return chosen[:count]
