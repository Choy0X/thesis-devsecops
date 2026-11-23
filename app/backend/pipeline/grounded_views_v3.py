"""Grounded views for security_chat_v3 (policy security-grounded-views/3, tag grounded_v35).

What this builder fixes relative to grounded_views (v34), from the 60-case evaluation:

* The answer follows from the code and the scope, never from the form of the prompt. Every unit is shown in every scope form that is true of it: the
  vulnerable code under any class, under its class, and under a set holding its class (findings); the fixed code under any class, under its class and under
  a set holding its class, the vulnerable code under a wrong class and under a wrong set, and a clean neighbour (no finding). The dataset assembler then
  balances the finding share inside each form of the training partition (grounded_dataset.balance).
* The explanation is made of facts of the shown code (names, line numbers, the order of the data flow), not of a class sentence; the class is one title of
  the closed catalogue and appears only in violated_invariant. The code is numbered in every view.
* A repair is an edit list (training.finetune.repair_edits). Its names must be visible in the shown code or in the import lines shown above it, and the
  import lines are chosen for the names the fix needs, so a fix that uses an imported helper is teachable.
"""
from __future__ import annotations

import re

from app.backend.pipeline import grounded_flow, grounded_policy
from app.backend.pipeline import grounded_views as g
from training.finetune import repair_edits
from training.finetune import security_serialization_v3 as proto
from training.finetune.repair_validation import assess_patch

POLICY = 'security-grounded-views/3'
OPEN_SCOPE = 'Review the shown code for security vulnerabilities of any class in the schema list.'
SCOPED_NEGATIVE = ('No finding is limited to the shown code and the review scope; other vulnerabilities have not been ruled out.')
BEHAVIOURAL_INTENT = 'Preserve the unit\'s ordinary behaviour while removing the weakness named in the finding.'
import contextlib


@contextlib.contextmanager
def protocol_scope(name):
    """Build views for another prompt protocol (security_chat_v4 has a larger class catalogue) and restore the default afterwards."""
    global proto
    from training.finetune import serialization
    previous, proto = proto, serialization.security_module(name)
    try:
        yield proto
    finally:
        proto = previous


HELD_OUT = grounded_policy.HELD_OUT                 # never a scope, never a training positive; the grounded_dataset TRANSFER_CWES
MAX_SET = 3
MAX_HEADER_STATEMENTS = 6
MAX_HEADER_CHARS_FOR_NAMES = 600


def symbol(code):
    """The name of the function under review. Comments are removed first (a word after "function" in a doc comment is not a name), and the first lines are
    tried before the rest, because the declaration comes first."""
    python = re.search(r'(?m)^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(', code)
    if python and not re.search(r'\bfunction\b|=>', code.split('\n', 1)[0]):
        return python.group(1)
    bare = re.sub(r'/\*.*?\*/', '', code, flags=re.S)
    bare = re.sub(r'(?m)^\s*//.*$', '', bare)
    head = '\n'.join([line for line in bare.splitlines() if line.strip()][:3])
    return g.symbol(head) or g.symbol(bare)


def label_of(unit):
    """The catalogue class of the unit (CWE-77 is reported as CWE-78), else None."""
    evidence = unit.get('llm_attribution') or unit.get('diff_grounded')
    if evidence:                                                   # a unit without a flow proof carries its class (grounded_views_evidence)
        return evidence['class'] if evidence.get('class') in proto.TITLE_OF else None
    raw = g.known_cwe(unit)
    label = proto.CLASS_ALIASES.get(raw, raw)
    return label if label in proto.TITLE_OF else None


def single_scope(cwe):
    return f'Review only for: {proto.TITLE_OF[cwe]}.'


def set_scope(classes):
    ordered = sorted(classes, key=lambda c: int(c.split('-')[1]))
    return 'Review only for these classes: ' + '; '.join(proto.TITLE_OF[c] for c in ordered) + '.'


def scope_text(form, classes):
    return OPEN_SCOPE if form == 'open' else single_scope(classes[0]) if form == 'single' else set_scope(classes)


def _negative_response():
    return {'status': 'no_finding', 'findings': [], 'evidence': [], 'assumptions': [SCOPED_NEGATIVE], 'missing_context': [], 'validation_state': 'not_validated'}


def _finding_response(unit, label, flow, quote, name, *, offset=0):
    summary, control, reason = grounded_flow.describe(flow, offset=offset)
    where = unit['file'] + (f':{name}' if name else '')
    finding = {'summary': summary, 'location': f"{where}, line {flow['sink_line_no'] + offset}", 'violated_invariant': proto.TITLE_OF[label], 'attacker_control': control}
    return {'status': 'finding', 'findings': [finding], 'evidence': [{'quote': quote, 'reason': reason}], 'assumptions': [], 'missing_context': [],
            'validation_state': 'not_validated'}


def _flows_of(code, language, cwes):
    return any(grounded_flow.find_flows(code, language, c) for c in cwes)


def _analyser_classes(label):
    """The analyser classes behind a catalogue class (CWE-78 is analysed as CWE-78 and CWE-77)."""
    return tuple(c for c in grounded_flow.SUPPORTED if proto.CLASS_ALIASES.get(c, c) == label)


def has_flow_of(code, language, label):
    return _flows_of(code, language, _analyser_classes(label))


def _pick(items, unit_id, tag, count):
    ranked = sorted(items, key=lambda item: g._digest(unit_id, tag, item))
    return ranked[:count]


def _right_set(unit, label, pool):
    others = [c for c in pool if c != label and c not in HELD_OUT]
    size = 1 + (g._digest(unit['unit_id'], 'set-size')[0] in '01234567')          # two or three classes
    return [label, *_pick(others, unit['unit_id'], 'right-set', size)] if others else [label]


def _wrong_choices(unit, code, label, pool):
    """Classes of the pool, other than the unit's, for which the analyser finds no flow in `code`; look-alikes (their sink words occur) first."""
    lowered = code.lower()
    out = []
    for other in pool:
        if other == label or other in HELD_OUT or has_flow_of(code, unit['language'], other):
            continue
        lookalike = any(sink in lowered for sink in g.CWE_SINKS.get(other, ()))
        out.append((0 if lookalike else 1, g._digest(unit['unit_id'], 'wrong', other), other))
    return [c for _, _, c in sorted(out)]


# ---- repair ----------------------------------------------------------------------------------------------------------------------------------------

PY_KEYWORDS = {'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global',
               'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield', 'self', 'cls'}
PY_BUILTINS = {'str', 'int', 'float', 'bool', 'bytes', 'list', 'dict', 'set', 'tuple', 'len', 'range', 'enumerate', 'zip', 'map', 'filter', 'sorted', 'min', 'max', 'sum', 'abs', 'round',
               'any', 'all', 'isinstance', 'issubclass', 'type', 'open', 'print', 'repr', 'format', 'getattr', 'setattr', 'hasattr', 'ord', 'chr', 'iter', 'next', 'reversed', 'super',
               'Exception', 'ValueError', 'TypeError', 'KeyError', 'IndexError', 'OSError', 'PermissionError', 'FileNotFoundError', 'RuntimeError', 'NotImplementedError', 'AssertionError'}
PY_MEMBERS = {'join', 'split', 'strip', 'lstrip', 'rstrip', 'startswith', 'endswith', 'replace', 'lower', 'upper', 'format', 'encode', 'decode', 'get', 'items', 'keys', 'values', 'append',
              'extend', 'pop', 'update', 'add', 'remove', 'path', 'basename', 'dirname', 'abspath', 'realpath', 'normpath', 'exists', 'isfile', 'isdir', 'commonpath', 'commonprefix',
              'quote', 'escape', 'safe_load', 'loads', 'dumps', 'parse', 'urlparse', 'netloc', 'hostname', 'scheme', 'resolve', 'is_relative_to', 'name', 'parent', 'parents', 'match',
              'fullmatch', 'compile', 'search', 'isalnum', 'isdigit', 'isalpha', 'isnumeric', 'isidentifier', 'copy', 'sub', 'ValueError', 'PermissionError', 'abort'}
_PY_IDENT = re.compile(r'(?<![\w.])([A-Za-z_]\w*)')
_PY_MEMBER = re.compile(r'\.\s*([A-Za-z_]\w*)')
_PY_DECLARED = re.compile(r'(?:^|[\s(,])([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)|\bfor\s+([\w, ]+?)\s+in\b|\bas\s+(\w+)|\blambda\s+([\w, ]*):|\bdef\s+(\w+)\s*\(([^)]*)\)')
_PY_KWARG = re.compile(r'(?<![\w.])([A-Za-z_]\w*)\s*=(?!=)')


def _unseen_names_py(added_lines, shown):
    """The Python rule: an added line may use only names the model can see (in the shown code or the import lines above it), names it declares itself, Python keywords and
    builtins, keyword-argument names, and common standard members. Empty means grounded."""
    blanked = g._STRING.sub('""', shown)
    visible = set(re.findall(r'[A-Za-z_]\w*', blanked))
    declared = set()
    cleaned = [g._STRING.sub('""', line) for line in added_lines]
    for line in cleaned:
        for match in _PY_DECLARED.finditer(line):
            for group in match.groups():
                declared.update(re.findall(r'[A-Za-z_]\w*', group or ''))
    unseen = set()
    for line in cleaned:
        kwargs = set(_PY_KWARG.findall(line))
        for name in _PY_IDENT.findall(line):
            if name not in PY_KEYWORDS and name not in PY_BUILTINS and name not in declared and name not in visible and name not in kwargs:
                unseen.add(name)
        for member in _PY_MEMBER.findall(line):
            if member not in PY_MEMBERS and member not in visible and member not in declared:
                unseen.add(member)
    return unseen


# Language words that need no definition in the shown code, for the languages without an analyser (keywords and the few builtins every one of them has). A name
# outside this list and outside the shown code refuses the repair: conservative on purpose, since no list of every standard library can be kept per language.
NEUTRAL_NAMES = {'if', 'else', 'elif', 'elsif', 'unless', 'for', 'foreach', 'while', 'do', 'switch', 'case', 'default', 'return', 'break', 'continue', 'goto', 'try', 'catch',
                 'finally', 'throw', 'throws', 'raise', 'rescue', 'ensure', 'defer', 'go', 'panic', 'recover', 'sizeof', 'typeof', 'nameof', 'len', 'cap', 'make', 'new', 'delete',
                 'null', 'nil', 'NULL', 'None', 'true', 'false', 'void', 'int', 'long', 'char', 'bool', 'string', 'byte', 'size_t', 'uint8_t', 'uint32_t', 'u8', 'u32', 'usize',
                 'assert', 'static', 'const', 'unsigned', 'signed', 'struct', 'self', 'this', 'super', 'Some', 'Ok', 'Err', 'unwrap', 'expect', 'is_empty', 'is_none', 'isset',
                 'empty', 'count', 'strlen', 'intval', 'abs', 'min', 'max', 'Math', 'Objects', 'requireNonNull', 'Exception', 'IllegalArgumentException', 'ArgumentException',
                 'ArgumentNullException', 'ArgumentError', 'RuntimeError', 'errors', 'fmt', 'Errorf', 'New'}


def _unseen_names(added_lines, shown, language='javascript'):
    """Names a repair uses that the model cannot see: not in the shown code, not declared by the repair, not a standard name. Empty means grounded."""
    if language == 'python':
        return _unseen_names_py(added_lines, shown)
    diff_only = grounded_policy.is_diff_only(language)
    if not diff_only and any(g._REGEX_LITERAL.search(line) for line in added_lines):
        return {'<regex literal>'}
    blanked = g._STRING.sub('""', shown)
    visible, members = set(g._IDENT.findall(blanked)), set(g._ANY_NAME.findall(blanked))
    cleaned = [g._STRING.sub('""', line) for line in added_lines]
    declared = set()
    for line in cleaned:
        for match in g._DECLARED.finditer(line):
            declared.update(name.strip() for group in match.groups() if group for name in group.split(',') if name.strip())
    unseen = set()
    for line in cleaned:
        for member in g._MEMBER.findall(line):
            if member not in g.BUILTIN_CALLS and member not in g.STANDARD_MEMBERS and member not in declared and member not in members:
                unseen.add(member)
        for name in g._IDENT.findall(line):
            if name not in g.BUILTIN_CALLS and name not in g._KEYWORDS and name not in declared and name not in visible and not (diff_only and name in NEUTRAL_NAMES):
                unseen.add(name)
    return unseen


def _header_statements(text, language='javascript'):
    """The top-level import / require statements of an import header, each as written."""
    if language == 'python':
        out, current = [], []
        for line in text.split('\n'):
            current.append(line)
            if line.count('(') <= line.count(')') and ('(' not in ''.join(current) or ')' in ''.join(current)):
                out.append('\n'.join(current))
                current = []
        return [s for s in out if s.strip()]
    parser = grounded_flow._parser('js')
    if parser is None or not text.strip():
        return []
    tree = parser.parse(text.encode('utf-8'))
    return [grounded_flow._text(node) for node in tree.root_node.named_children]


def _relevant_header(unit, names, language='javascript'):
    """Only the import statements of the file that bind one of `names` (at most MAX_HEADER_STATEMENTS, MAX_HEADER_CHARS_FOR_NAMES characters), else ''."""
    chosen, size = [], 0
    for statement in _header_statements(unit.get('exec_context') or '', unit.get('language') or language):
        if names & set(g._ANY_NAME.findall(statement)):
            if size + len(statement) + 1 > MAX_HEADER_CHARS_FOR_NAMES or len(chosen) >= MAX_HEADER_STATEMENTS or g._ADVISORY_ID.search(statement):
                return ''
            chosen.append(statement)
            size += len(statement) + 1
    return '\n'.join(chosen)


def _repair_views(unit, label, flow, quote, name, scope_single, response_fn=None):
    """(repair, repair_open) views, or []. The edit list must apply exactly, stay small, and use only names the prompt shows."""
    before, after, language = unit['before'], unit['after'], unit['language']
    if not g._substantive_patch(before, after):
        return []
    bare = repair_edits.edits_from_versions(g.with_final_newline(before), g.with_final_newline(after))
    if not bare or len(bare) > repair_edits.MAX_EDITS or sum(len(e['old']) + len(e['new']) for e in bare) > repair_edits.MAX_CHANGED_LINES:
        return []
    added = [line for edit in bare for line in edit['new']]
    header = g._header(unit)
    unseen = _unseen_names(added, (header + '\n' + before) if header else before, language)
    if unseen and '<regex literal>' not in unseen:
        header = _relevant_header(unit, unseen)
        unseen = _unseen_names(added, (header + '\n' + before) if header else before, language)
    if unseen:
        return []
    shown_before, shown_after = ((header + '\n' + before), (header + '\n' + after)) if header else (before, after)
    if not g._numberable(shown_before):
        return []
    edits = repair_edits.edits_from_versions(g.with_final_newline(shown_before), g.with_final_newline(shown_after))
    path = unit['file']
    verdict = repair_edits.assess(repair_edits.render_edits(edits), g.with_final_newline(shown_before), path)
    if verdict['state'] != 'applies_exactly' or assess_patch(verdict['patch'], {path: g.with_final_newline(shown_before)}, allowed_paths={path})['state'] != 'applies_exactly':
        return []
    offset = len(header.splitlines()) if header else 0
    response = response_fn(offset) if response_fn else _finding_response(unit, label, flow, quote, name, offset=offset)
    repair = dict(response, patch=repair_edits.render_edits(edits), behavioral_intent=BEHAVIOURAL_INTENT)
    if not g._valid('repair', repair):
        return []
    extra = {'header_lines': offset, 'form': None, 'scope_classes': [], 'expected_class': label}
    return [g._view(unit, 'repair', 'repair', 'supported', g._context(path, language, shown_before, scope_single, numbered=True), repair, **{**extra, 'form': 'single', 'scope_classes': [label]}),
            g._view(unit, 'repair_open', 'repair', 'supported', g._context(path, language, shown_before, OPEN_SCOPE, numbered=True), repair, **{**extra, 'form': 'open'})]


# ---- views -----------------------------------------------------------------------------------------------------------------------------------------

def _discovery(unit, kind, pool_name, code, form, classes, response, label):
    context = g._context(unit['file'], unit['language'], code, scope_text(form, classes), numbered=True)
    return g._view(unit, kind, 'discovery', pool_name, context, response, form=form, scope_classes=list(classes), expected_class=label if response['status'] == 'finding' else None)


def build_unit_views(unit, *, eligible_scopes=(), neighbours=2, name_class=True):
    """Every grounded view of one unit (a fixed order). Nothing is produced for a unit without a confirmed, quotable flow in a catalogue class.

    `eligible_scopes`: catalogue classes that other scopes may name (they have training positives), the unit's own class and the held-out classes aside.
    `name_class=False` (a training unit of a class with too few training positives): only the open-scope views are made, so that a class which cannot be balanced
    is never named in a training scope."""
    if unit.get('llm_attribution') or unit.get('diff_grounded'):
        from app.backend.pipeline import grounded_views_evidence
        return grounded_views_evidence.build_unit_views(unit, eligible_scopes=eligible_scopes, neighbours=neighbours, name_class=name_class)
    before, after, language = unit['before'], unit['after'], unit['language']
    if not before.strip() or not after.strip() or before == after or len(before) > g.MAX_CODE_CHARS or len(after) > g.MAX_CODE_CHARS:
        return []
    label = label_of(unit)
    raw = g.known_cwe(unit)
    if not label or not g._numberable(before) or not g._numberable(after):
        return []
    pool = [c for c in eligible_scopes if c not in HELD_OUT]
    found = grounded_flow.confirmed_flow(before, after, language, raw)
    quote = found['line'] if found and g._usable_quote(found['line']) else None
    name = symbol(before)
    if not (quote and quote in before and g.substantial(before) and not g.has_hint_comment(before) and not g._trivial_change(before, after)
            and g._meaningful_change(before, after) and not g._ADVISORY_ID.search(after)):
        return []
    response = _finding_response(unit, label, found, quote, name)
    if not g._valid('discovery', response):
        return []
    right_set = _right_set(unit, label, pool)
    out = [_discovery(unit, 'discovery_open', 'supported', before, 'open', [], response, label)]
    if name_class:
        out.append(_discovery(unit, 'discovery_scoped', 'supported', before, 'single', [label], response, label))
    if name_class and len(right_set) > 1:
        out.append(_discovery(unit, 'discovery_set', 'supported', before, 'set', right_set, response, label))
    repairs = _repair_views(unit, label, found, quote, name, single_scope(label))
    out += [v for v in repairs if name_class or v['kind'] == 'repair_open']
    negative = _negative_response()
    fixed_ok = g.substantial(after) and not g.has_hint_comment(after)
    if name_class and fixed_ok and not has_flow_of(after, language, label):
        out.append(_discovery(unit, 'negative_fixed', 'scoped_negative', after, 'single', [label], negative, None))
        if len(right_set) > 1 and not any(has_flow_of(after, language, c) for c in right_set):
            out.append(_discovery(unit, 'negative_fixed_set', 'scoped_negative', after, 'set', right_set, negative, None))
    if fixed_ok and not _flows_of(after, language, grounded_flow.SUPPORTED):
        out.append(_discovery(unit, 'negative_fixed_open', 'scoped_negative', after, 'open', [], negative, None))
    for index, source in enumerate(g._neighbours(unit, neighbours)):
        if name_class:
            out.append(g._view(unit, 'negative_neighbour', 'discovery', 'scoped_negative',
                               g._context(unit['file'], language, source, single_scope(label), numbered=True), negative, neighbour=index, form='single',
                               scope_classes=[label], expected_class=None))
        if not _flows_of(source, language, grounded_flow.SUPPORTED) and g._numberable(source):
            out.append(g._view(unit, 'negative_neighbour_open', 'discovery', 'scoped_negative',
                               g._context(unit['file'], language, source, OPEN_SCOPE, numbered=True), negative, neighbour=index, form='open',
                               scope_classes=[], expected_class=None))
    wrong = _wrong_choices(unit, before, label, pool) if name_class else []
    if wrong:
        out.append(_discovery(unit, 'negative_other_class', 'scoped_negative', before, 'single', [wrong[0]], negative, None))
    if len(wrong) >= 2:
        out.append(_discovery(unit, 'negative_other_set', 'scoped_negative', before, 'set', wrong[:2 + (g._digest(unit['unit_id'], 'wrong-size')[0] in '01234567' and len(wrong) > 2)], negative, None))
    return [v for v in out if g._valid(v['task'], v['response'])]
