"""Are two runs comparable? Reads their recorded settings and names every difference that would make a comparison misleading.

A comparison is allowed when the things that decide what a score means are the same in both runs: what the model was shown, whether it could look things up,
which items it was scored on, and how. A difference is a **blocker**. A setting that neither run recorded is **unconfirmed**: it is not a difference, but it
is said, so a reader knows the match is not established. Nothing is guessed from a run's name.
"""
from __future__ import annotations

# (the dotted key in run_config.json, what a difference means). Order is the order they are reported in.
CHECKS: list[tuple[str, str]] = [
    ("input_policy_hash", "The runs were shown different inputs (the input policy differs)."),
    ("context_policy", "The runs used different context policies."),
    ("rag_enabled", "One run could look things up (retrieval) and the other could not."),
    ("retrieval.embedder", "The runs retrieved with different embedding models."),
    ("retrieval.kb_version", "The runs retrieved from different knowledge bases."),
    ("split", "The runs were scored on different splits."),
    ("dataset_version", "The runs used different dataset versions."),
    ("external_data", "One run used imported data that was not built and reviewed here, or the two used different imports: they are not comparable as if nothing differed."),
    ("eval_view", "The runs were scored on different evaluation views."),
    ("prompt_version", "The runs used different prompts."),
    ("loss_mode", "The runs were trained with different loss modes."),
    ("engine.model", "The runs were answered by different models."),
    ('manifest_sha256', 'The evaluation dataset manifests differ.'),
    ('evaluation_dataset', 'The independently authenticated evaluation dataset identities differ.'),
    ('training_dataset', 'The recorded training dataset identities differ; treat this as an explicitly designed training-data ablation.'),
    ('model_revision', 'The pinned base model revisions differ.'),
    ('tokenizer_revision', 'The tokenizer revisions differ.'),
    ('serialization', 'The serialization protocols or prompt templates differ.'),
    ('context_policy_hash', 'The context acquisition policies differ.'),
    ('evaluation_cohort_sha256', 'The selected evaluation view cohorts differ.'),
    ('generation.do_sample', 'The decoding sampling policies differ.'),
    ('generation.max_new_tokens', 'The generation length budgets differ.'),
    ('generation.temperature', 'The decoding temperatures differ.'),
    ('generation.top_p', 'The decoding nucleus policies differ.'),
    ('generation.num_beams', 'The decoding beam budgets differ.'),
    ('generation.candidate_budget', 'The patch candidate budgets differ.'),
]


OPTIONAL = {'external_data', 'manifest_sha256', 'evaluation_dataset', 'training_dataset', 'model_revision',
            'tokenizer_revision', 'serialization', 'context_policy_hash', 'evaluation_cohort_sha256',
            'generation.do_sample', 'generation.max_new_tokens', 'generation.temperature', 'generation.top_p',
            'generation.num_beams', 'generation.candidate_budget'}


def _get(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def check(a: dict, b: dict) -> dict:
    """``{"comparable": bool, "blockers": [...], "unconfirmed": [...]}`` for two run configurations."""
    blockers, unconfirmed = [], []
    for key, meaning in CHECKS:
        va, ha = _get(a, key)
        vb, hb = _get(b, key)
        if not ha and not hb:
            if key in OPTIONAL:
                continue
            unconfirmed.append({"field": key, "reason": "Neither run recorded it, so it cannot be confirmed to match."})
        elif ha != hb or va != vb:
            blockers.append({"field": key, "reason": meaning, "a": va if ha else "not recorded", "b": vb if hb else "not recorded"})
    return {"comparable": not blockers, "blockers": blockers, "unconfirmed": unconfirmed}
