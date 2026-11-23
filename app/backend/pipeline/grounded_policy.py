"""The two policies every grounded builder shares, in one place.

HELD_OUT: the weakness classes that are never a training positive and never a scope (SSRF and code injection); the partition code, the view builders, the
attribution pass and the catalogue all read this tuple, and tests/test_grounded_policy.py checks that the frozen catalogue and the serialisation agree.

LANGUAGE_TIER: how a language's units can be proven. `flow_analysed` languages have a flow analyser (grounded_flow / grounded_flow_py), so a unit can be
proved by a flow the fix removes. Every other language is `diff_only`: the unit is proven by its diff (an added guard, a message that names the unit, a strong
attribution), which is weaker evidence and is tagged, capped and reported apart (plan v6).
"""
from __future__ import annotations

HELD_OUT = ('CWE-918', 'CWE-94')

FLOW_ANALYSED = ('python', 'javascript', 'typescript')


def language_tier(language):
    return 'flow_analysed' if language in FLOW_ANALYSED else 'diff_only'


def is_diff_only(language):
    return language_tier(language) == 'diff_only'
