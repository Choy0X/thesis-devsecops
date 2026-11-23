"""Tier D of the grounded corpus: a real fix that ADDS a guard is grounded in its own diff, for classes that have no flow detector.

The flow tier proves a weakness by a source-to-sink flow the fix removes. Authorization, resource limits, CSRF, input validation, link following and authentication
are fixed by adding a check, and there is no flow to prove. For these a unit is admitted when the fix is small and focused, adds a guard that fits the class family,
and the statement the guard now protects exists in the vulnerable code (the anchor, which becomes the quote). The label is the advisory's class; the reason is built
from the diff alone. Weaker than a flow proof, so the tier trains at weight 0.4 until a review verdict binds.
"""
from __future__ import annotations

import collections
import difflib
import json
import re
from pathlib import Path

from app.backend.pipeline import grounded_views as g

LANGUAGES = ('python', 'javascript', 'typescript')
MAX_CHANGED_LINES = 30
MAX_HUNKS = 3
SCHEME = 'diff_grounded'

# family -> (v4 catalogue class, vocabulary of the guard lines that fit it)
FAMILIES = {
    'authorization': ('CWE-862', re.compile(r'auth|permission|perm|admin|owner|role|staff|csrf|login|user|forbidden|\b40[13]\b|access|superuser|can_', re.I)),
    'resource': ('CWE-400', re.compile(r'max|limit|size|length|\blen\b|timeout|depth|count|bytes|rate|quota|recursion|iteration|throttle', re.I)),
    'csrf': ('CWE-352', re.compile(r'csrf|xsrf|token|origin|referer|referrer|samesite', re.I)),
    'validation': ('CWE-20', re.compile(r'valid|isinstance|typeof|instanceof|isnan|isfinite|schema|allow|sanitiz|check|assert|ensure|must|expect', re.I)),
    'path_symlink': ('CWE-59', re.compile(r'symlink|islink|realpath|readlink|lstat|resolve|follow|link', re.I)),
    'authentication': ('CWE-306', re.compile(r'auth|login|token|session|credential|password|signed|jwt|bearer|verify', re.I)),
}
CLASS_FAMILY = {cls: name for name, (cls, _) in FAMILIES.items()}

_CATALOGUE = Path(__file__).resolve().parents[3] / 'training' / 'finetune' / 'catalogue_v4.json'
_ALIASES = json.loads(_CATALOGUE.read_text(encoding='utf-8'))['aliases'] if _CATALOGUE.is_file() else {}

_IF = ('if_statement', 'elif_clause')
_RAISE = ('raise_statement', 'throw_statement')
_CHECK_CALL = re.compile(r'\b\w*(?:check|verify|assert|validate|require|authori[sz]e|permission|ensure|guard|csrf|throttle|limit)\w*\s*\(', re.I)
_DEBUG_WORD = re.compile(r'debug|verbose|log|trace', re.I)
_KEYWORDS = {'if', 'elif', 'not', 'and', 'or', 'true', 'false', 'none', 'null', 'undefined', 'typeof', 'is', 'in'}
_TEST_PATH = re.compile(r'(^|/)(tests?|__tests__|spec|specs)(/|$)|\.(test|spec)\.[A-Za-z]+$|(^|/)test_[^/]*$')


def family_of(cwes):
    """(family, catalogue class) of the first CWE of the list that belongs to a guard family (children count for their parent), else None."""
    for cwe in cwes or []:
        parent = _ALIASES.get(cwe, cwe)
        if parent in CLASS_FAMILY:
            return CLASS_FAMILY[parent], parent
    return None


def _parse(code, language):
    if language == 'python':
        from app.backend.pipeline import grounded_flow_py
        parsed = grounded_flow_py.parse(code)
        return None if parsed is None else (parsed[0], parsed[1], 0)
    from app.backend.pipeline import grounded_flow
    parsed = grounded_flow.parse_variant(code, language)
    return None if parsed is None else (parsed[0], parsed[1], parsed[2][:parsed[3]].count('\n'))


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _added_indexes(old_lines, new_lines):
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    added = {j for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag in ('insert', 'replace') for j in range(j1, j2)}
    return matcher.get_opcodes(), added


def _debug_only(condition):
    names = [n for n in re.findall(r'[A-Za-z_]\w*', condition.lower()) if n not in _KEYWORDS]
    return bool(names) and all(_DEBUG_WORD.search(n) for n in names)


def added_guards(before, after, language):
    """The guard statements that `after` has and `before` lacks: [{'line': text, 'after_line_no': 1-based, 'kind': condition|raise|early_return|call}]. Empty when the
    code does not parse. A condition made only of debug, verbose, log or trace names is not a guard."""
    parsed = _parse(after, language) if language in LANGUAGES else None
    if parsed is None:
        return []
    root, _, prefix_lines = parsed
    new_lines = g.with_final_newline(after).splitlines()
    _, added = _added_indexes(g.with_final_newline(before).splitlines(), new_lines)
    found = {}
    for node in _walk(root):
        row = node.start_point[0] - prefix_lines
        if row not in added or not 0 <= row < len(new_lines) or row in found:
            continue
        text = new_lines[row].strip()
        if node.type in _IF:
            condition = node.child_by_field_name('condition')
            if condition is not None and _debug_only(condition.text.decode('utf-8', 'replace')):
                continue
            body = node.child_by_field_name('consequence')
            body_text = body.text.decode('utf-8', 'replace') if body is not None else ''
            kind = 'raise' if re.search(r'\b(raise|throw)\b', body_text) else 'early_return' if re.search(r'\breturn\b', body_text) else 'condition'
        elif node.type in _RAISE:
            kind = 'raise'
        elif node.type == 'assert_statement':
            kind = 'condition'
        elif node.type == 'expression_statement' and _CHECK_CALL.search(node.text.decode('utf-8', 'replace')):
            kind = 'call'
        else:
            continue
        found[row] = {'line': text, 'after_line_no': row + 1, 'kind': kind}
    return [found[row] for row in sorted(found)]


def diff_grounding(unit):
    """The diff-grounded evidence of a unit, or None. All of: Python, JavaScript or TypeScript; no flow detector for the unit's class (the flow tier owns those);
    the advisory belongs to a guard family; the code is substantial, numberable and within the context budget; the fix is at most 30 changed lines in at most 3
    hunks, mostly additive; an added guard fits the family vocabulary; and the statement after the guard exists in the vulnerable code and is quotable."""
    language, before, after = unit.get('language'), unit.get('before') or '', unit.get('after') or ''
    if language not in LANGUAGES or g.known_cwe(unit) is not None:
        return None
    family = family_of(unit.get('cwe'))
    if family is None or not before.strip() or not after.strip() or before == after:
        return None
    if len(before) > g.MAX_CODE_CHARS or len(after) > g.MAX_CODE_CHARS or not g._numberable(before) or not g._numberable(after):
        return None
    if not g.substantial(before) or not g.substantial(after):
        return None
    old_lines, new_lines = g.with_final_newline(before).splitlines(), g.with_final_newline(after).splitlines()
    opcodes, added = _added_indexes(old_lines, new_lines)
    changes = [op for op in opcodes if op[0] != 'equal']
    removed = sum(i2 - i1 for tag, i1, i2, _, _ in changes if tag in ('delete', 'replace'))
    if len(changes) > MAX_HUNKS or len(added) + removed > MAX_CHANGED_LINES or removed > max(1, len(added) // 3):
        return None
    name, (cls, vocabulary) = family[0], FAMILIES[family[0]]
    guards = [x for x in added_guards(before, after, language) if vocabulary.search(x['line'])]
    if not guards:
        return None
    index = guards[0]['after_line_no'] - 1
    start = next((i1 for tag, i1, i2, j1, j2 in changes if j1 <= index < j2), None)
    if start is None:
        return None
    anchor_index = next((i for i in range(start, len(old_lines)) if old_lines[i].strip()), None)
    if anchor_index is None or not g._usable_quote(old_lines[anchor_index]):
        return None
    return {'family': name, 'class': cls, 'guards': guards, 'anchor': old_lines[anchor_index].strip(), 'anchor_line_no': anchor_index + 1, 'scheme': SCHEME}


def limit_commit_breadth(units, limit=3):
    """Drop the units of every commit that has more than `limit` units outside test paths: a commit that rewrites many functions is not evidence for any one of them."""
    counts = collections.Counter(u.get('commit_key') for u in units if not _TEST_PATH.search((u.get('file') or '').lower()))
    return [u for u in units if counts[u.get('commit_key')] <= limit]
