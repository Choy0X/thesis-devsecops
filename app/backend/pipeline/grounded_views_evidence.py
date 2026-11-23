"""Grounded views for units without a flow proof (the evidence tiers of the v37 recovery build).

Two kinds of evidence give a unit a class, a quote and a reason:

* tier D, a fix that adds a guard (grounded_guard): the quote is the statement the guard now protects and the reason is a family-specific sentence that names only the line
  number. It must not mention the fixed code: at inference the model does not see it, and a reason that says "the fix changes this" is a boilerplate the model learned
  and hallucinated in an earlier run;
* an answer of the LLM attribution pass (tools/grounded_attribution_pass.py), already validated against the shown code: its quote, line, class and reason are used as written.

The views are the same kinds, forms and response schema as the flow tier's, so assembly, balancing, review and the evaluation harness do not change. Repairs are made only
when the edit list applies exactly, as in the flow tier. Clean neighbours are the matched real clean functions of grounded_clean.
"""
from __future__ import annotations

import re

from app.backend.pipeline import grounded_clean, grounded_diff, grounded_policy
from app.backend.pipeline import grounded_flow
from app.backend.pipeline import grounded_views as g
from app.backend.pipeline import grounded_views_v3 as v3

CALLER = 'the caller of this function'
# family -> (summary, reason); {n} is the line number of the quote. Only the line number and generic words: nothing from the fixed code or the advisory.
TEMPLATES = {
    'authorization': ('The statement at line {n} runs without an authorization check on the caller',
                      'Line {n} runs for any caller: nothing before it in this function checks that the caller is allowed to perform the action.'),
    'resource': ('The statement at line {n} runs with no bound on the amount of input it handles',
                 'Line {n} processes input whose size or count is not limited anywhere before it in this function.'),
    'csrf': ('The statement at line {n} acts on a request without verifying where it came from',
             "Line {n} acts on the request without verifying that it comes from the application's own page, for example with a token or an origin check."),
    'validation': ('The statement at line {n} uses its input without validating it',
                   "Line {n} uses the caller's input without any validation of its type, range or format earlier in this function."),
    'path_symlink': ('The statement at line {n} follows a path without resolving it or checking links',
                     'Line {n} uses a path without resolving it or checking for links before the access.'),
    'authentication': ('The statement at line {n} runs without verifying who the caller is',
                       'Line {n} runs without any check in this function that the caller has proven their identity.'),
}
_FIRST_SENTENCE = re.compile(r'^(.+?[.!?])(?:\s|$)')


def evidence_for(unit):
    """{'tier', 'class', 'quote', 'quote_line_no', 'reason', 'summary', 'authored_by', 'origin'} from the unit's raw evidence, or None."""
    llm = unit.get('llm_attribution')
    if llm:
        reason = llm['reason'].strip()
        first = _FIRST_SENTENCE.match(reason)
        return {'tier': 'L', 'class': llm['class'], 'quote': llm['quote'], 'quote_line_no': llm['quote_line_no'], 'reason': reason,
                'summary': (first.group(1) if first else reason)[:240], 'authored_by': 'llm', 'origin': 'recovered_L'}
    grounded = unit.get('diff_grounded')
    if grounded:
        summary, reason = grounded_diff.template(grounded)
        n = grounded['anchor_line_no']
        return {'tier': 'D', 'class': grounded['class'], 'quote': grounded['anchor'], 'quote_line_no': n, 'reason': reason.format(n=n), 'summary': summary.format(n=n),
                'authored_by': 'template', 'origin': 'recovered_D'}
    return None


def _response(unit, evidence, name, *, offset=0):
    where = unit['file'] + (f':{name}' if name else '')
    finding = {'summary': evidence['summary'], 'location': f"{where}, line {evidence['quote_line_no'] + offset}",
               'violated_invariant': v3.proto.TITLE_OF[evidence['class']], 'attacker_control': CALLER}
    return {'status': 'finding', 'findings': [finding], 'evidence': [{'quote': evidence['quote'], 'reason': evidence['reason'] if offset == 0 else _shift(evidence, offset)}],
            'assumptions': [], 'missing_context': [], 'validation_state': 'not_validated'}


def _shift(evidence, offset):
    """The reason cites line numbers; when a header is shown above the code (repair views) they move by its length."""
    return re.sub(r'(?i)\bline (\d+)\b', lambda m: f'line {int(m.group(1)) + offset}', evidence['reason'])


def build_unit_views(unit, *, eligible_scopes=(), neighbours=2, name_class=True):
    """Every grounded view of one evidenced unit (a fixed order); [] when the unit has no usable evidence."""
    evidence = evidence_for(unit)
    before, after, language = unit['before'], unit['after'], unit['language']
    if evidence is None or not before.strip() or not after.strip() or before == after or len(before) > g.MAX_CODE_CHARS or len(after) > g.MAX_CODE_CHARS:
        return []
    label = evidence['class']
    if label not in v3.proto.TITLE_OF or not g._numberable(before) or not g._numberable(after):
        return []
    quote = evidence['quote']
    if not (quote in before and g._usable_quote(quote) and g.substantial(before) and not g.has_hint_comment(before) and not g._trivial_change(before, after)
            and g._meaningful_change(before, after) and not g._ADVISORY_ID.search(after)):
        return []
    pool = [c for c in eligible_scopes if c not in v3.HELD_OUT]
    name = v3.symbol(before)
    response = _response(unit, evidence, name)
    if not g._valid('discovery', response):
        return []
    right_set = v3._right_set(unit, label, pool)
    out = [v3._discovery(unit, 'discovery_open', 'supported', before, 'open', [], response, label)]
    if name_class:
        out.append(v3._discovery(unit, 'discovery_scoped', 'supported', before, 'single', [label], response, label))
    if name_class and len(right_set) > 1:
        out.append(v3._discovery(unit, 'discovery_set', 'supported', before, 'set', right_set, response, label))
    repairs = v3._repair_views(unit, label, None, quote, name, v3.single_scope(label), response_fn=lambda offset: _response(unit, evidence, name, offset=offset))
    out += [v for v in repairs if name_class or v['kind'] == 'repair_open']
    negative = v3._negative_response()
    fixed_ok = g.substantial(after) and not g.has_hint_comment(after)
    if name_class and fixed_ok and not v3.has_flow_of(after, language, label):
        out.append(v3._discovery(unit, 'negative_fixed', 'scoped_negative', after, 'single', [label], negative, None))
        if len(right_set) > 1 and not any(v3.has_flow_of(after, language, c) for c in right_set):
            out.append(v3._discovery(unit, 'negative_fixed_set', 'scoped_negative', after, 'set', right_set, negative, None))
    if fixed_ok and not v3._flows_of(after, language, grounded_flow.SUPPORTED):
        out.append(v3._discovery(unit, 'negative_fixed_open', 'scoped_negative', after, 'open', [], negative, None))
    for index, source in enumerate(grounded_clean.matched_clean(unit, grounded_clean.untouched_functions(unit), language, count=neighbours)):
        out.append(g._view(unit, 'negative_neighbour_open', 'discovery', 'scoped_negative', g._context(unit['file'], language, source, v3.OPEN_SCOPE, numbered=True), negative,
                           neighbour=index, form='open', scope_classes=[], expected_class=None, clean_basis=grounded_clean.CLEAN_BASIS))
    diff_only = grounded_policy.is_diff_only(language)
    wrong = v3._wrong_choices(unit, before, label, pool) if name_class and not diff_only else []         # the confusion negatives need an analyser that rules the other classes out
    if wrong:
        out.append(v3._discovery(unit, 'negative_other_class', 'scoped_negative', before, 'single', [wrong[0]], negative, None))
    if len(wrong) >= 2:
        out.append(v3._discovery(unit, 'negative_other_set', 'scoped_negative', before, 'set', wrong[:2], negative, None))
    if diff_only:
        for view in out:
            view['proof'] = unit.get('proof')
            if view['kind'].startswith('negative'):
                view['clean_basis'] = 'diff_only'               # no analyser rules other weaknesses out: the answer says only that no issue is shown in this code
    return [v for v in out if g._valid(v['task'], v['response'])]
