"""Recovery of mined pairs that the admission gates drop: which failing gates can be repaired (soft) and which cannot (hard), and the evidence tiers that admit a pair
which fails only soft gates.

A HARD gate means the pair is not a usable function-level fix (test file, revert, parse errors, module-level region, unverified fix or provenance, ambiguous match):
any label would be wrong. A SOFT gate means the evidence is missing and the pair may be fine (weak attribution, unknown licence, noise path, tangled commit).
The licence policy of the owner (2026-11-06) is that an unknown licence is admitted at full weight and recorded; it is waived here, not repaired.
"""
from __future__ import annotations

from app.backend.pipeline import grounded_admission as admission

LICENCE_POLICY = 'unknown_admitted_full_weight'
MESSAGE_BASES = ('message_names_unit', 'message_names_file')
# Quarantine reasons the store produces; all of them mean the function pairing or the commit kind is unreliable.
HARD_QUARANTINE = ('residual_region', 'revert_commit', 'parse_errors_in_unit', 'parse_unresolved', 'ambiguous_merge_parent', 'ambiguous_match',
                   'cross_file_match', 'low_match_confidence')
HARD_GATES = ('test_file', 'quality_not_ok', 'quarantined_hard', 'quarantined_other', 'provenance_not_verified', 'fix_not_verified', 'no_change')
SOFT_GATES = ('weak_attribution', 'licence_unknown', 'noise_path', 'tangled')
ORDER = ('test_file', 'noise_path', 'tangled', 'quarantined_hard', 'quarantined_other', 'quality_not_ok', 'provenance_not_verified', 'licence_unknown',
         'weak_attribution', 'no_change', 'fix_not_verified')


def _passes(code, pair, record):
    for _, gate_code, keep in admission.GATES:
        if gate_code == code:
            return keep(pair, record)
    raise KeyError(code)


def failing(pair, record):
    """Every failing gate of a pair, in a fixed order. The quarantine gate is split into hard reasons and unknown ones, and the derived quality status is reported
    only when no flag (test, tangled, quarantine) explains it, because it is the same fact seen twice."""
    flags = pair.get('flags') or {}
    failed = set()
    for code in ('test_file', 'noise_path', 'tangled', 'provenance_not_verified', 'licence_unknown', 'weak_attribution', 'no_change', 'fix_not_verified'):
        if not _passes(code, pair, record):
            failed.add(code)
    reasons = flags.get('quarantine') or []
    if reasons:
        failed.add('quarantined_hard' if any(r in HARD_QUARANTINE for r in reasons) else 'quarantined_other')
    explained = bool(flags.get('is_test') or flags.get('tangled') or reasons)
    if record.get('data_quality_status') != 'ok' and not explained:
        failed.add('quality_not_ok')
    return [code for code in ORDER if code in failed]


def soft_only(pair, record):
    """True for a pair that fails at least one gate and only soft ones."""
    gates = failing(pair, record)
    return bool(gates) and all(g in SOFT_GATES for g in gates)




def message_tier(pair):
    """True when the commit message names the unit or its file (attribution basis message_names_unit or message_names_file): evidence for tier M."""
    return (pair.get('attribution') or {}).get('basis') in MESSAGE_BASES


def classify_recovery(pair, record):
    """(tier, basis) for a pair that the strict gates refuse: ('A' or 'B', 'licence_policy') when only the licence gate fails (the link is strong, the licence is
    unknown and admitted by policy); ('R?', None) when only soft gates fail and an evidence tier must decide; (None, reason) when a hard gate fails."""
    failed = failing(pair, record)
    if not failed:
        return None, 'admitted'
    hard = [g for g in failed if g in HARD_GATES]
    if hard:
        return None, hard[0]
    if failed == ['licence_unknown']:
        level = (pair.get('link') or {}).get('evidence_level')
        return ('A' if level in ('moderate', 'strong') else 'B'), 'licence_policy'
    return 'R?', None


def diff_tier(pair, code):
    """The diff-grounded evidence of the unit (Task 4: grounded_guard.diff_grounding), or None. Returns None while that module is absent."""
    try:
        from app.backend.pipeline import grounded_guard
    except ImportError:
        return None
    unit = dict(pair.get('unit') or {})
    unit.update(before=code.get('before') or '', after=code.get('after') or '', cwe=list((pair.get('subject') or {}).get('cwe') or []))
    return grounded_guard.diff_grounding(unit)


def clean_attribution(unit, record):
    """The accepted LLM attribution `record` made fit to train on, or None. A reason is a statement about the code the model is shown, which is `before` only: sentences that
    describe the fix are cut (the model never sees the fix) and every code name left must be in `before`. The v37 audit found 1,857 of 1,872 reasons narrating the fix and
    1,540 naming code that only the fixed version has (the validator then looked at before + after)."""
    from training.finetune import grounded_claim_gate as gate
    reason = gate.strip_fix_sentences(record.get('reason') or '')
    if not reason or gate.unshown_names(reason, unit.get('before') or ''):
        return None
    if len(gate.sentences(reason)) > 3:
        return None
    return dict(record, reason=reason)


def attach_attributions(units, records, report=None):
    """Copies of `units` in which every unit with an accepted LLM attribution (records of attributed.jsonl) carries it as `llm_attribution` and the origin
    `recovered_L`. The input is not changed; records of unknown units are ignored. The reason of each attribution is cleaned first (see clean_attribution); a record whose
    reason cannot be made to describe the shown code is not attached, so the unit stays what it was. `report` (a dict) receives the counts."""
    by_id = {r['unit_id']: r for r in records}
    stats = {'records': len(records), 'attached': 0, 'cut_fix_sentences': 0, 'dropped': 0}
    out = []
    for unit in units:
        record = by_id.get(unit['unit_id'])
        if record is None:
            out.append(unit)
            continue
        cleaned = clean_attribution(unit, record)
        if cleaned is None:
            stats['dropped'] += 1
            out.append(unit)
            continue
        stats['attached'] += 1
        stats['cut_fix_sentences'] += cleaned['reason'] != (record.get('reason') or '').strip()
        keep = {k: cleaned[k] for k in ('class', 'quote', 'quote_line_no', 'reason', 'answer_sha256', 'batch') if k in cleaned}
        out.append(dict(unit, llm_attribution=keep, origin='recovered_L'))
    if report is not None:
        report.update(stats)
    return out
