"""Guard-citing no-finding answers for the fixed twins of vulnerable functions (recipe 11). No model, no LLM.

Until v38 every no-finding answer in the training data was one identical sentence with an empty evidence list, so the model learned a rich template for "flag" and one token for
"do not flag", and never saw what a safe version of a function looks like. For a fixed twin whose vulnerable twin is known, the safe version differs by a check that runs before the
call the vulnerable twin was flagged for. When that is visible, the answer names it: the evidence quotes the guard line (a verbatim line of the shown code), and the reason gives the
two line numbers and the call. It says nothing about a fix (the model is shown one version only) and names only code that is shown; every answer is run through the strict claim gate
(training/finetune/grounded_claim_gate.py) before it is used.

The guard is found from the difference between the two versions (a line the fixed version has and the vulnerable one does not), before the flagged line, that looks like a check. The
claim that no unchecked path is visible is made only for units whose flow the analyser proved in the vulnerable code and no longer finds in the fixed code (``analysed``); for the
others the answer states only the order of the lines. Answers are analyser-derived and unreviewed by a person.
"""
from __future__ import annotations

import difflib
import json
import re

from app.backend.pipeline import grounded_flow
from training.finetune import grounded_claim_gate as claim_gate

NUMBERED = re.compile(r'^\s*(\d+)\|\s?(.*)$')
GUARD = re.compile(r'\b(?:if|elif|unless|throw|raise|assert|abort|guard)\b|valid|sanit|escap|quot|check|safe|allow|resolve|normali[sz]e|startsWith|startswith|endsWith|includes|'
                   r'isabs|isAbsolute|basename|realpath|abspath|shlex|encodeURI|hasOwn|whitelist|allowlist|permit|verify|ensure', re.I)
CONDITION = re.compile(r'^(?:if|elif|else if|assert|unless|guard)\b|^\}?\s*else if\b')
EXIT = re.compile(r'^(?:throw|raise|return|continue|break|abort)\b')
VALIDATOR_CALL = re.compile(r'\w*(?:valid|sanit|escap|check|verify|ensure|assert|allow|safe|normali[sz]e|resolve|isabs|basename|realpath|abspath|shlex|quote)\w*\s*\(', re.I)
LANGUAGES = ('python', 'javascript', 'typescript')
QUOTE_CHARS = ('"', "'", '`')
COMMENT = re.compile(r'^\s*(?://|#|\*|/\*)')
BASE_ASSUMPTION = 'No finding is limited to the shown code and the review scope; other vulnerabilities have not been ruled out.'


def shown_lines(context_text):
    """[(number, text)] for the numbered code lines of a prompt's context (the margin and the [target] and [scope] blocks are not code)."""
    out = []
    for line in (context_text or '').splitlines():
        m = NUMBERED.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


def balanced(text):
    pairs = {')': '(', ']': '[', '}': '{'}
    stack = []
    for ch in text:
        if ch in '([{':
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def looks_like_check(text):
    """A complete line that is a condition or a call to a validator-named function: not a string continuation, a half statement or an argument list."""
    if not text or text[0] in QUOTE_CHARS or text.endswith(',') or COMMENT.match(text) or not balanced(text):
        return False
    return bool(CONDITION.match(text) or EXIT.match(text) or VALIDATOR_CALL.search(text))


def find_guard(vulnerable_lines, fixed_lines, sink_quote):
    """(guard_index, sink_index) into fixed_lines, or None. The sink line must be in both versions verbatim; the guard is the nearest added line before it that looks like a check,
    and a line that only exits (throw, return) is replaced by the condition just above it."""
    wanted = (sink_quote or '').strip()
    if not wanted:
        return None
    vtexts, ftexts = [t.strip() for _, t in vulnerable_lines], [t.strip() for _, t in fixed_lines]
    if wanted not in vtexts:
        return None
    sinks = [i for i, t in enumerate(ftexts) if t == wanted]
    if not sinks:
        return None
    sink = sinks[0]
    added = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, vtexts, ftexts, autojunk=False).get_opcodes():
        if tag in ('insert', 'replace'):
            added.update(range(j1, j2))
    candidates = [j for j in sorted(added) if j < sink and 4 <= len(ftexts[j]) <= 160 and (looks_like_check(ftexts[j]) or (EXIT.match(ftexts[j]) and balanced(ftexts[j])))]
    if not candidates:
        return None
    guard = candidates[-1]
    if EXIT.match(ftexts[guard]):
        guard = next((k for k in range(guard - 1, max(guard - 4, -1), -1) if CONDITION.match(ftexts[k]) and balanced(ftexts[k]) and len(ftexts[k]) <= 160), None)
        if guard is None:
            return None
    return guard, sink


def build_answer(guard_line, guard_number, sink_number, sink_text, *, analysed):
    sink = sink_text.strip()
    callee = f' `{sink}`' if sink and '`' not in sink and len(sink) <= 100 else ''
    reason = f'Line {guard_number} is evaluated before the call on line {sink_number}{callee}.'
    if analysed:
        reason += ' No unchecked path from an input to that call is visible in the shown code.'
    return {'assumptions': [BASE_ASSUMPTION], 'evidence': [{'quote': guard_line.strip(), 'reason': reason}], 'findings': [], 'missing_context': [],
            'status': 'no_finding', 'validation_state': 'not_validated'}


def derive(vulnerable, fixed, *, require_analyser):
    """A guard-citing no_finding answer for the fixed view, or (None, why). `vulnerable`/`fixed`: view dicts with context_text, response_json (vulnerable), language, expected_class.
    require_analyser: the unit's flow was proved by the analyser, so the answer may say no unchecked path is visible, and the analyser must agree (a flow before, none after)."""
    try:
        answer_v = json.loads(vulnerable['response_json'])
        quote = answer_v['evidence'][0]['quote']
    except (ValueError, KeyError, IndexError, TypeError):
        return None, 'no sink quote'
    if vulnerable.get('language') not in LANGUAGES:
        return None, 'language not covered'
    vlines, flines = shown_lines(vulnerable['context_text']), shown_lines(fixed['context_text'])
    if not vlines or not flines:
        return None, 'no code'
    hit = find_guard(vlines, flines, quote)
    if hit is None:
        return None, 'no guard before a kept sink'
    guard, sink = hit
    analysed = False
    if require_analyser:
        cwe, language = vulnerable.get('expected_class'), vulnerable.get('language')
        if cwe not in grounded_flow.SUPPORTED:
            return None, 'class not supported by the analyser'
        before = '\n'.join(t for _, t in vlines)
        after = '\n'.join(t for _, t in flines)
        if not grounded_flow.find_flows(before, language, cwe):
            return None, 'analyser finds no flow before'
        if grounded_flow.find_flows(after, language, cwe):
            return None, 'analyser still finds a flow after'
        analysed = True
    answer = build_answer(flines[guard][1], flines[guard][0], flines[sink][0], flines[sink][1], analysed=analysed)
    ok, reasons = claim_gate.gate(answer, fixed['context_text'], strict=True)
    if not ok:
        return None, 'claim gate: ' + reasons[0]
    return answer, 'ok'


def kept_sink_with_added_check(vulnerable_lines, fixed_lines, sink_quote):
    """True when the flagged line is in both versions verbatim and the fixed version has, before it, a line the vulnerable one lacks that looks like a check (loose pattern).
    The admission test for an LLM-attributed unit: its fixed twin then differs from the vulnerable one by something that can be read as a guard before the same call."""
    wanted = (sink_quote or '').strip()
    vtexts, ftexts = [t.strip() for _, t in vulnerable_lines], [t.strip() for _, t in fixed_lines]
    if not wanted or wanted not in vtexts or wanted not in ftexts:
        return False
    sink = ftexts.index(wanted)
    added = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, vtexts, ftexts, autojunk=False).get_opcodes():
        if tag in ('insert', 'replace'):
            added.update(range(j1, j2))
    return any(j < sink and ftexts[j] and not COMMENT.match(ftexts[j]) and GUARD.search(ftexts[j]) for j in added)
