"""Task views T1 to T7: what a student is shown and what it is asked to answer.

``build_input`` makes the student-visible text from a subject, context items and (for two tasks) an explicit
catalog condition or report, and is never given the reviewed record; ``build_view`` adds the target answer, which
is the only place the reviewed record is read. See ``core`` for the rules and ``pools`` for the sampling pools.
"""
from app.backend.pipeline.views.core import (EXTRA_KEYS, STUDENT_KINDS, Exclusion, StudentInput, View, build_injection,
                                             build_input, build_view, evidence_lines, first_sentences, hidden_facts,
                                             name_tokens, target_summary)

__all__ = ["EXTRA_KEYS", "STUDENT_KINDS", "Exclusion", "StudentInput", "View", "build_injection", "build_input",
           "build_view", "evidence_lines", "first_sentences", "hidden_facts", "name_tokens",
           "target_summary"]
