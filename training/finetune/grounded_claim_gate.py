"""The claim gate: plain checks on the text of a grounded answer against the code the model is shown. No model is involved.

One rule set for two uses: (1) training answers (the attribution validator, the dataset verifier that ships in the bundle) and (2) the model's own answers
(evaluation scoring, the scan path). It lives in training/ because the bundle carries only that package.

The rules come from the v37 audit: 1,857 of 1,872 LLM-attributed training answers narrated the fix and 1,540 named identifiers that exist only in the fixed code, which
the model is never shown. A model trained on them wrote "The fix passes `newUrl` through `normalizeUrl`" about code that has neither name.

    gate(answer, shown_code) -> (ok, [reasons])

The answer is the JSON object of the security_chat protocols (status, findings, evidence, assumptions, missing_context). A no_finding answer passes the narration and name
rules too: it may not describe a fix or name code that is not shown.
"""
from __future__ import annotations

import re

# A sentence about the fix is information the model cannot have: it sees one version of the function.
_NARRATION = re.compile(r"\b(?:the\s+(?:upstream\s+)?(?:fix|patch|commit)|this\s+(?:fix|patch|commit)|upstream\s+fix|fix(?:es|ed|ing)?|patch(?:es|ed)?)\b", re.IGNORECASE)
_SENTENCE = re.compile(r'(?<=[.!?])\s+')
_SPAN = re.compile(r'(`+)(.+?)\1', re.DOTALL)
_SPACE = re.compile(r'\s+')
_CUT = re.compile(r'\.\.\.[\s.]*$')
_TEMPLATE_WORDS = re.compile(r'\b(?:derives from|receives|through|from parameter)\b')


def sentences(text):
    return [s for s in _SENTENCE.split((text or '').strip()) if s]


def narrates_fix(text):
    """True when a sentence of `text` talks about the fix, the patch or the commit."""
    return any(_NARRATION.search(s) for s in sentences(text))


def strip_fix_sentences(text):
    """`text` without the sentences that talk about the fix (empty when nothing else is left)."""
    return ' '.join(s for s in sentences(text) if not _NARRATION.search(s))


def _norm(text):
    return _SPACE.sub(' ', text or '').strip()


def unshown_names(text, shown_code):
    """The code spans (backticks) of `text` that are not in the shown code. A span cut with '...' is compared by its prefix; a call like name(...) by its name;
    whitespace differences are ignored."""
    haystack = _norm(shown_code)
    plain = haystack.replace('`', '')
    missing = []
    for _fence, span in _SPAN.findall(text or ''):
        body = _norm(span)
        if len(body) < 2 or _TEMPLATE_WORDS.search(body):          # a pair of fences that straddles the template's own words: the code span itself held a backtick
            continue
        flat = _norm(body.replace('`', ''))
        cut = _CUT.search(flat)                                   # a span cut with '...' is compared by what is left of it
        if cut:
            flat = flat[:cut.start()].rstrip()
        if len(flat) < 2 or flat in plain or flat.split('(')[0].strip() in plain:
            continue
        if len(flat) > 24 and flat[:16] in plain:                 # a long expression that the flow template rewrote (string joins, template literals): its start is the code's
            continue
        missing.append(body)
    return missing


def quote_is_a_line(quote, shown_code):
    wanted = (quote or '').strip()
    return bool(wanted) and any(line.strip() == wanted for line in (shown_code or '').splitlines())


MIN_QUOTE = 8                                                  # a shorter fragment is too common to name a line


def numbered_lines(shown_code):
    """{line number: text} of a numbered view ('  14| code'); empty when the code is shown without its margin."""
    found = {}
    for line in (shown_code or '').splitlines():
        match = re.match(r'\s*(\d+)\|\s?(.*)$', line)
        if match:
            found[int(match.group(1))] = match.group(2)
    return found


def claimed_line(answer):
    """The line number named in the location of the first finding that names one ('file:function, line 14'), else None."""
    for finding in (answer or {}).get('findings') or []:
        match = re.search(r'\bline (\d+)\b', str((finding or {}).get('location') or ''))
        if match:
            return int(match.group(1))
    return None


def normalize_quotes(answer, shown_code):
    """(answer, repaired): the evidence quotes that are only the start of the claimed line, completed to that whole line. A quote is completed only when it is not already a line, has at least
    MIN_QUOTE characters, is an exact prefix of the text of the line the finding claims, and no other shown line starts with it. Anything else (an unrelated quote, a quote spanning lines, an ambiguous
    prefix, shown code without line numbers) is left as it is and still fails quote_is_a_line. The input is not changed; `repaired` lists the evidence indexes that were completed."""
    if not isinstance(answer, dict) or not isinstance(answer.get('evidence'), list):
        return answer, []
    lines, number = numbered_lines(shown_code), claimed_line(answer)
    if number not in lines:
        return answer, []
    plain = strip_line_numbers(shown_code)
    target, repaired, evidence = lines[number].strip(), [], []
    for index, item in enumerate(answer['evidence']):
        quote = (item or {}).get('quote') if isinstance(item, dict) else None
        wanted = quote.strip() if isinstance(quote, str) else ''
        if (len(wanted) >= MIN_QUOTE and not quote_is_a_line(wanted, plain) and target.startswith(wanted)
                and sum(1 for text in lines.values() if text.strip().startswith(wanted)) == 1):
            item = dict(item, quote=target)
            repaired.append(index)
        evidence.append(item)
    return (dict(answer, evidence=evidence) if repaired else answer), repaired


def _texts(answer):
    for finding in answer.get('findings') or []:
        yield 'finding summary', finding.get('summary')
    for evidence in answer.get('evidence') or []:
        yield 'evidence reason', evidence.get('reason')
    for key in ('assumptions', 'missing_context'):
        for item in answer.get(key) or []:
            yield key, item if isinstance(item, str) else (item or {}).get('detail')


def strip_line_numbers(shown_code):
    """The shown code of a numbered view ('   3| code') without the margin, so quotes and names compare with the code itself."""
    return '\n'.join(re.sub(r'^\s*\d+\|\s?', '', line) for line in (shown_code or '').splitlines())


HARD = ('talks about the fix', 'quote is not a line')           # information the model cannot have, or a quote that is not code: the finding cannot stand
# Soft: a name that is not a literal substring (a canonical sink label such as innerHTML, a rewritten expression). Strict mode (training data) rejects it too.


def check(answer, shown_code):
    """[(kind, why)] for one answer, kind 'hard' or 'soft'."""
    code = strip_line_numbers(shown_code)
    found = []
    if not isinstance(answer, dict):
        return [('hard', 'answer is not an object')]
    for where, text in _texts(answer):
        if not isinstance(text, str):
            continue
        if narrates_fix(text):
            found.append(('hard', f'{where} talks about the fix'))
        missing = unshown_names(text, code)
        if missing:
            found.append(('soft', f'{where} names code that is not shown: {missing[0][:60]}'))
    for evidence in answer.get('evidence') or []:
        if not quote_is_a_line(evidence.get('quote'), code):
            found.append(('hard', 'quote is not a line of the shown code'))
    return found


def gate(answer, shown_code, *, strict=True):
    """(True, []) or (False, [why, ...]). strict (training answers): any problem fails. Not strict (model answers): only the hard problems fail; an unshown name is
    reported by check() but does not suppress the finding. `shown_code` is the code section of the prompt, with or without the line-number margin."""
    reasons = [why for kind, why in check(answer, shown_code) if strict or kind == 'hard']
    return not reasons, reasons


WITHHELD = 'A possible finding was withheld: its explanation does not match the shown code (insufficient evidence).'


def enforce(answer, shown_code):
    """The model's answer as it may be shown: a finding that fails a hard rule (it talks about the fix, or its quote is not a line of the code) becomes a no_finding answer that says
    a finding was withheld. A quote that is only the start of the claimed line is first completed to that line (normalize_quotes; `quotes_completed` lists the evidence indexes). Returns {'answer': dict, 'withheld': bool, 'problems': [(kind, why)], 'quotes_completed': [int]}; `problems` also lists soft ones (names that are not shown), which do not
    withhold the finding. This is the one place the scan path should call; the evaluation reports the same rule as its gate-on columns."""
    answer, repaired = normalize_quotes(answer, shown_code)
    problems = check(answer, shown_code)
    if not isinstance(answer, dict):
        answer = {}
    hard = [why for kind, why in problems if kind == 'hard']
    if answer.get('status') == 'finding' and hard:
        withheld = {'status': 'no_finding', 'findings': [], 'evidence': [], 'missing_context': [], 'validation_state': 'not_validated',
                    'assumptions': list(answer.get('assumptions') or []) + [WITHHELD]}
        return {'answer': withheld, 'withheld': True, 'problems': problems, 'quotes_completed': repaired}
    return {'answer': answer, 'withheld': False, 'problems': problems, 'quotes_completed': repaired}
