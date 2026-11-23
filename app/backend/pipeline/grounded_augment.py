"""Label-preserving variants of whole units for the training set (recipe 11): one local name is renamed consistently in the code, in both versions and in every answer.

A fine-tuned model that has seen each function a handful of times can learn the spelling instead of the check (identifier perturbation costs plain SFT 3.9 to 7.2 F1 in the
Semantic Trap study, arXiv 2601.22655). A rename changes the spelling and nothing else: line numbers do not move, so the answers stay valid with the same substitution applied to
their strings. A variant is accepted only if (1) every numbered line of every view and every answer string is rewritten with one rule, (2) the project analyser still finds the same
flow in the vulnerable code and none in the fixed code (units whose flow it proved), and (3) every rewritten answer still passes the strict claim gate against the rewritten code.
Measured on the v38 training pairs by the research agent: 136 of 140 renames kept the label (97.1 percent). No model, no LLM.

A variant is a new unit: record id ``<record>#aug1``, new view ids and keys, the same package and family, so a partition stays disjoint.
"""
from __future__ import annotations

import hashlib
import json
import re

from app.backend.pipeline import grounded_flow
from app.backend.pipeline.grounded_guard_answer import NUMBERED, shown_lines
from training.finetune import grounded_claim_gate as claim_gate, repair_edits
from training.finetune.grounded_trial import shown_source

KEYWORDS = set('const let var function async await return if else for while new this true false null undefined def self cls None True False in not and or is class import from try '
               'except raise throw catch finally typeof lambda yield with as pass del global nonlocal assert break continue switch case default do void delete instanceof'.split())
SUFFIX = '#aug1'
PREFIX = 'v_'
FREE_TEXT_FIELDS = ('location',)                       # file path and function name: never rewritten
KINDS = ('discovery', 'negative_fixed', 'negative_other', 'repair')
PROVED = ('upstream_fix', 'sanitiser_removal', 'unsafe_substitution')


def local_names(lines, language):
    text = '\n'.join(lines)
    names = []
    if language == 'python':
        names += re.findall(r'^\s*([A-Za-z_]\w*)\s*=(?!=)', text, re.M)
        m = re.search(r'def\s+\w+\s*\(([^)]*)\)', text)
        if m:
            names += [p.split(':')[0].split('=')[0].strip() for p in m.group(1).split(',')]
    else:
        names += re.findall(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', text)
    return [n for n in dict.fromkeys(names) if n and re.fullmatch(r'[A-Za-z_$][\w$]*', n) and n not in KEYWORDS and len(n) >= 3]


def pattern_for(name):
    return re.compile(r'(?<![\w.$])' + re.escape(name) + r'(?![\w$])')


def renamable(name, before, after):
    """True when one substitution renames every occurrence of the name correctly: it is used in both versions, never inside a string, never as a key or annotation."""
    both = '\n'.join(before) + '\n' + '\n'.join(after)
    pat = pattern_for(name)
    if not pat.search('\n'.join(before)) or not pat.search('\n'.join(after)):
        return False
    if re.search(r'[\'"`][^\'"`\n]*(?<![\w.$])' + re.escape(name) + r'(?![\w$])[^\'"`\n]*[\'"`]', both):
        return False
    if re.search(r'(?<![\w.$])' + re.escape(name) + r'(?![\w$])\s*:(?!:)', both):
        return False
    return (PREFIX + name) not in both


def rewrite_context(context_text, pattern, new):
    out = []
    for line in context_text.split('\n'):
        m = NUMBERED.match(line)
        if m:
            head = line[:len(line) - len(m.group(2))]
            out.append(head + pattern.sub(new, m.group(2)))
        else:
            out.append(line)
    return '\n'.join(out)


def rewrite_value(value, pattern, new):
    if isinstance(value, str):
        return pattern.sub(new, value)
    if isinstance(value, list):
        return [rewrite_value(v, pattern, new) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in FREE_TEXT_FIELDS else rewrite_value(v, pattern, new)) for k, v in value.items()}
    return value


def rewrite_view(view, pattern, new):
    out = dict(view)
    out['context_text'] = rewrite_context(view['context_text'], pattern, new)
    out['response_json'] = json.dumps(rewrite_value(json.loads(view['response_json']), pattern, new), sort_keys=True)
    return out


def with_identity(view):
    """The rewritten view with the fields that name a case made new: record, subject, view id, key and content hash."""
    out = dict(view)
    tag = hashlib.sha256((view['view_id'] + SUFFIX).encode()).hexdigest()[:24]
    out['view_id'] = 'gv_aug_' + tag
    out['record_id'] = view['record_id'] + SUFFIX
    out['subject_id'] = view['subject_id'] + SUFFIX
    out['view_key'] = view['view_key'].replace(view['record_id'], out['record_id'], 1) if view.get('view_key') else out['view_id']
    out['content_sha'] = hashlib.sha256((out['context_text'] + '\n' + out['response_json']).encode('utf-8')).hexdigest()
    return out


def code_of(view):
    return [t for _, t in shown_lines(view['context_text'])]


def patch_applies(view):
    """True when the repair answer's edit list applies exactly to the code its prompt shows (the same check the trial runs on generated patches)."""
    try:
        answer = json.loads(view['response_json'])
        path = list(view['allowed_patch_paths'])[0]
        return repair_edits.assess(answer['patch'], shown_source(view['context_text']), path)['state'] == 'applies_exactly'
    except (ValueError, KeyError, IndexError, TypeError):
        return False


def augment_unit(views, *, origin):
    """(new views, why) for the discovery views of one unit (all of its forms and kinds), or ([], reason). `views`: dicts of one record. Only units of an analysed origin are
    augmented, because only for those can the analyser confirm that the label survived."""
    if origin not in PROVED:
        return [], 'origin not analysed'
    finding = next((v for v in views if v['kind'].startswith('discovery')), None)
    fixed = next((v for v in views if v['kind'].startswith('negative_fixed')), None)
    if finding is None or fixed is None:
        return [], 'no pair'
    if finding.get('language') not in ('python', 'javascript', 'typescript'):
        return [], 'language not covered'
    before, after = code_of(finding), code_of(fixed)
    cwe, language = finding.get('expected_class'), finding['language']
    if cwe not in grounded_flow.SUPPORTED:
        return [], 'class not supported by the analyser'
    flows = grounded_flow.find_flows('\n'.join(before), language, cwe)
    if not flows or grounded_flow.find_flows('\n'.join(after), language, cwe):
        return [], 'unit is not analyser-proved'
    sink = flows[0]['sink']
    name = next((n for n in local_names(before, language) if renamable(n, before, after)), None)
    if name is None:
        return [], 'no renamable local'
    pattern, new = pattern_for(name), PREFIX + name
    by_id = {v['view_id']: rewrite_view(v, pattern, new) for v in views if v['kind'].startswith(KINDS)}
    for view_id in [i for i, v in by_id.items() if v['kind'].startswith('repair') and not patch_applies(v)]:
        del by_id[view_id]                                       # a repair view whose renamed patch no longer applies exactly is left out, not repaired by hand
    rewritten = list(by_id.values())
    before_new, after_new = code_of(by_id[finding['view_id']]), code_of(by_id[fixed['view_id']])
    if '\n'.join(before_new) == '\n'.join(before):
        return [], 'rename changed nothing'
    flows_new = grounded_flow.find_flows('\n'.join(before_new), language, cwe)
    if not any(f['sink'] == sink for f in flows_new) or grounded_flow.find_flows('\n'.join(after_new), language, cwe):
        return [], 'analyser disagrees after the rename'
    for view in rewritten:
        ok, reasons = claim_gate.gate(json.loads(view['response_json']), view['context_text'], strict=True)
        if not ok:
            return [], 'claim gate: ' + reasons[0]
    return [with_identity(v) for v in rewritten], 'ok'
