"""Deterministic priority of the weakly attributed units that the LLM attribution pass looks at first.

The pass is capped (500, 1,500, 3,000 units), so the units with the best expected yield go first. The score uses only evidence that is already in the unit: how few
functions the commit changes, how many words of the advisory text reappear in the function and its fix lines, whether the fix lines sit at a sink or a guard word, how
small the change is and whether the advisory class is a catalogue class. No network and no model.
"""
from __future__ import annotations

import collections
import difflib
import json
import re
from pathlib import Path

from app.backend.pipeline import grounded_policy

HELD_OUT = grounded_policy.HELD_OUT
WEIGHTS = {'breadth': 0.25, 'words': 0.25, 'sink': 0.15, 'small': 0.1, 'catalogue': 0.1, 'attribution': 0.15}
STRONG = ('advisory_names_unit', 'advisory_names_file', 'sole_changed_unit')
MESSAGE = ('message_names_unit', 'message_names_file')
_STOP = frozenset('this that with from have which when where there their about into over under after before would could should these those other than then they them also only some such because while being been does done used uses using more most very each both without within'.split())
_SINKISH = re.compile(r'exec|eval|query|open|read|write|request|fetch|redirect|render|innerhtml|subprocess|pickle|yaml|md5|sha1|check|verify|valid|auth|perm|limit|max|size|csrf|escape|sanitiz|quote|path|join|resolve|token|session', re.I)
_CATALOGUE = Path(__file__).resolve().parents[3] / 'training' / 'finetune' / 'catalogue_v4.json'
_FROZEN = json.loads(_CATALOGUE.read_text(encoding='utf-8')) if _CATALOGUE.is_file() else {'catalogue': [], 'aliases': {}}
_IDS = {cwe for cwe, _ in _FROZEN['catalogue']}
_ALIASES = _FROZEN['aliases']
_TEST_PATH = re.compile(r'(^|/)(tests?|__tests__|spec|specs)(/|$)|\.(test|spec)\.[A-Za-z]+$|(^|/)test_[^/]*$')


def _words(text):
    return {w for w in re.findall(r'[a-z]{5,}', (text or '').lower()) if w not in _STOP}


def _identifier_words(code):
    parts = re.findall(r'[A-Za-z][A-Za-z0-9]*', code or '')
    out = set()
    for part in parts:
        for piece in re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', part).lower().split():
            if len(piece) >= 5:
                out.add(piece)
    return out


def _fix_lines(before, after):
    old, new = (before or '').splitlines(), (after or '').splitlines()
    lines = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != 'equal':
            lines += old[i1:i2] + new[j1:j2]
    return lines


def held_out(unit):
    """True when any CWE of the advisory is a held-out class: such a unit is never used, whatever else it carries."""
    return any(_ALIASES.get(c, c) in HELD_OUT for c in unit.get('cwe') or [])


def priority(unit, *, commit_units=1):
    """A score in [0, 1], or None for a unit of a held-out class. `commit_units` is the number of units of the same commit."""
    if held_out(unit):
        return None
    breadth = 1.0 if commit_units <= 1 else 0.6 if commit_units <= 3 else 0.2 if commit_units <= 6 else 0.0
    advisory = unit.get('advisory') or {}
    wanted = _words(advisory.get('summary')) | _words(advisory.get('details'))
    fix = _fix_lines(unit.get('before'), unit.get('after'))
    have = _identifier_words(unit.get('before')) | _identifier_words('\n'.join(fix))
    words = min(1.0, len(wanted & have) / 3) if wanted else 0.0
    sink = 1.0 if any(_SINKISH.search(line) for line in fix) else 0.0
    small = 1.0 if len(fix) <= 10 else 0.5 if len(fix) <= 30 else 0.0
    catalogue = 1.0 if any(_ALIASES.get(c, c) in _IDS for c in unit.get('cwe') or []) else 0.0
    basis = unit.get('attribution')
    link = 1.0 if basis in STRONG else 0.6 if basis in MESSAGE else 0.0
    parts = {'breadth': breadth, 'words': words, 'sink': sink, 'small': small, 'catalogue': catalogue, 'attribution': link}
    return round(sum(WEIGHTS[k] * v for k, v in parts.items()), 6)


def rank(units):
    """The units by descending priority (ties by unit id), without the held-out ones. The functions of each commit are counted over the list that is passed in."""
    counts = collections.Counter(u.get('commit_key') for u in units if not _TEST_PATH.search((u.get('file') or '').lower()))
    scored = []
    for unit in units:
        score = priority(unit, commit_units=max(1, counts[unit.get('commit_key')]))
        if score is not None:
            scored.append((-score, unit.get('unit_id') or '', unit))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [unit for _, _, unit in scored]
