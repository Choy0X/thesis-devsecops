"""Deterministic explanation usability, independent of security admission.

An advisory need not be long or model-written. Repository omissions are tracked
separately and are never a request for a model to invent missing evidence.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

VERSION = 'explanation-usability/2'
_PLACEHOLDER = re.compile(r'^(?:todo|tbd|n/?a|none|null|unknown|placeholder|not available|no (?:explanation|description|advisory)(?: available| provided)?|(?:add|insert|write|generate) (?:an? |the )?(?:explanation|description)(?: here)?)[.!\s]*$', re.I)


def _mapping(value):
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def _explanation(record):
    """Prefer supplied source prose, then a retained generated explanation."""
    override = _mapping(record.get('overrides'))
    values = [override.get('explanation'), record.get('explanation'), record.get('advisory'),
              record.get('details'), record.get('summary'), record.get('explanation_llm'),
              _mapping(record.get('draft')).get('explanation')]
    malformed = False
    for value in values:
        if isinstance(value, Mapping):
            # Raw pair explanation metadata is not prose and is not malformed.
            text_keys = ('text', 'content', 'description')
            if any(key in value for key in text_keys):
                value = next((value[key] for key in text_keys if key in value), None)
            elif set(value) <= {'source', 'status', 'stale', 'unrelated', 'source_changed', 'version', 'needs_enrichment'}:
                continue
            else:
                malformed = True
                continue
        if value is None or value == '':
            continue
        if isinstance(value, str):
            if value.strip():
                return value.strip(), malformed
        else:
            malformed = True
    return None, malformed


def _evidence_problems(record):
    reasons = []
    for key in ('source_mismatch', 'source_changed', 'unrelated_source', 'evidence_unrelated'):
        if record.get(key) is True:
            reasons.append(key)
    quarantine = record.get('quarantine_reasons')
    if isinstance(quarantine, str):
        try:
            quarantine = json.loads(quarantine)
        except (ValueError, TypeError):
            pass
    if record.get('data_quality_status') in ('excluded', 'quarantined') or quarantine:
        reasons.append('source_excluded_or_quarantined')
    for side in ('before', 'after'):
        context = _mapping(record.get('context_' + side))
        for omission in context.get('omissions') or []:
            if isinstance(omission, str) and any(flag in omission.lower() for flag in (
                    'target_source_mismatch', 'mixed_snapshot', 'unrelated_source')):
                reasons.append(side + ': ' + omission)
    return list(dict.fromkeys(reasons))


def assess_explanation(record) -> dict:
    """Classify text using concrete faults only; never assign labels or proof."""
    text, malformed = _explanation(record)
    meta = _mapping(record.get('explanation'))
    reasons = []
    if record.get('explanation_stale') is True or meta.get('stale') is True or meta.get('source_changed') is True or meta.get('status') == 'stale':
        reasons.append('stale_explanation')
    if record.get('explanation_unrelated') is True or meta.get('unrelated') is True or meta.get('status') == 'unrelated':
        reasons.append('unrelated_explanation')
    if text is None:
        reasons.append('malformed_explanation' if malformed else 'missing_explanation')
    elif _PLACEHOLDER.fullmatch(text):
        reasons.append('placeholder_explanation')
    elif any(ord(char) < 32 and char not in '\n\r\t' for char in text):
        reasons.append('malformed_explanation')
    elif text.startswith(('{', '[')):
        # Only actual serialized objects/arrays are malformed prose. Markdown
        # citations and parameter notation are legitimate explanation text.
        try:
            if isinstance(json.loads(text), (dict, list)):
                reasons.append('malformed_explanation')
        except (ValueError, TypeError):
            pass
    state = 'missing' if reasons == ['missing_explanation'] else 'unusable' if reasons else 'present_usable'
    return {'state': state, 'reasons': reasons,
            'generation_eligible': state != 'present_usable' and not _evidence_problems(record)}


def context_suggestions(record, decisive_missing=()) -> dict:
    """Legacy quality plus explicit, independent explanation/evidence states.

    Ordinary caller/budget omissions do not establish decisive missing facts.
    Only the evidence evaluator may supply those through decisive_missing.
    """
    explanation = assess_explanation(record)
    evidence = list(dict.fromkeys([str(reason) for reason in decisive_missing if reason] + _evidence_problems(record)))
    quality = {'present_usable': 'strong', 'missing': 'missing', 'unusable': 'weak'}[explanation['state']]
    return {'quality': quality, 'reasons': explanation['reasons'], 'optional': True,
            'explanation_state': explanation['state'], 'explanation_reasons': explanation['reasons'],
            'generation_eligible': explanation['generation_eligible'] and not evidence,
            'evidence_state': 'incomplete' if evidence else 'not_evaluated', 'evidence_reasons': evidence}
