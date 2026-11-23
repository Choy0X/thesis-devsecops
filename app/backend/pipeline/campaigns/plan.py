"""What a campaign was asked to do: the ecosystems, the stages that are on, and the knobs of those stages.

A plan is plain data (it is stored with the campaign and shown on its Config tab). ``normalize`` fills in what was left out and drops what is not a plan; whether the plan can run on this
computer now is for ``preflight`` and ``admission`` to say.
"""
from __future__ import annotations

# The order the stages of one ecosystem run in; ``build`` is the one stage of the whole campaign and runs last.
STAGE_ORDER = ("ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions", "enrich", "build")
TARGET_STAGES = STAGE_ORDER[:-1]
GLOBAL_STAGES = ("build",)
TARGET_ALL = "*"                       # the "target" of a step of the whole campaign

STAGE_LABELS = {
    "ingest": "Read the advisories", "queue": "Queue the fix commits", "recover": "Recover missing fix commits", "mine": "Mine the code pairs", "verify": "Check the fixes",
    "resplit": "Split pairs into functions", "dep_versions": "Check dependency versions", "enrich": "Write the context with the model", "build": "Build a draft dataset",
}

# Campaign presets: which stages are on. They are a starting point; every stage can be switched afterwards.
PRESETS = {
    "ingest_only": ("ingest",),
    "collect": ("ingest", "queue", "recover", "mine"),
    "collect_verify": ("ingest", "queue", "recover", "mine", "verify", "resplit"),
    "full": ("ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions"),
    "full_context": ("ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions", "enrich"),
}

DEFAULT_KNOBS = {
    "mine_workers": 4,                 # commits mined at once
    "pause_seconds": 10.0,             # between batches of mined commits
    "deps_workers": 24,                # registries asked at once
    "recover_workers": 8,              # GitHub requests for link recovery at once
    "recover_limit": None,             # most records to try link recovery on (None: all)
    "quota": None,                     # most commits to mine per ecosystem (None: until none are left)
    "refresh": False,                  # ask OSV for a newer dump before reading
}


# How deep the model-written context goes (the Enrich stage): which pairs are offered to the model, by how much real text they carry (weak, partial, strong) and whether an earlier draft
# exists. They map onto the filters of the candidate list in Code pairs, so what a campaign does is what the owner could do there by hand.
_ALL_LEVELS = ["weak", "partial", "strong"]
CONTEXT_PROFILES = (
    {"id": "weak", "name": "Only weak context", "description": "Pairs whose real text is thin or missing, so the model is spent where it helps most.",
     "filters": {"levels": ["weak"], "status": "missing"}},
    {"id": "weak_partial", "name": "Weak and partial", "description": "Pairs with little real text, and the ones with only some of it.",
     "filters": {"levels": ["weak", "partial"], "status": "missing"}},
    {"id": "all", "name": "All pairs", "description": "Every pair that has no model-written context yet.",
     "filters": {"levels": _ALL_LEVELS, "status": "missing"}},
    {"id": "redo", "name": "Redo older drafts", "description": "Pairs drafted with an earlier version of the prompt.",
     "filters": {"levels": _ALL_LEVELS, "status": "older"}},
)
CONTEXT_DEFAULTS = {"profile": "weak", "workers": 2, "cap": None}          # cap: most pairs to write per ecosystem (None: all that match)
CONTEXT_WORKERS = (1, 4)


def enabled_stages(plan: dict) -> list[str]:
    """The stages that are on, in the order they run."""
    on = plan.get("stages") or {}
    return [s for s in STAGE_ORDER if on.get(s) and not (s == 'enrich' and plan.get('workflow_version', 1) >= 2)]


def target_stages(plan: dict) -> list[str]:
    """The stages that run once for each ecosystem."""
    return [s for s in enabled_stages(plan) if s in TARGET_STAGES]


def global_stages(plan: dict) -> list[str]:
    return [s for s in enabled_stages(plan) if s in GLOBAL_STAGES]


def step_count(plan: dict) -> int:
    """How many steps a full run has: each target stage for each ecosystem, plus the stages of the whole campaign."""
    return len(plan.get("ecosystems") or []) * len(target_stages(plan)) + len(global_stages(plan))


def stages_of_preset(preset: str) -> dict[str, bool]:
    """``{stage: on}`` for a preset."""
    if preset not in PRESETS:
        raise ValueError(f"not a preset: {preset}")
    return {s: s in PRESETS[preset] for s in STAGE_ORDER}


def normalize(raw: dict) -> dict:
    """A plan from what the owner chose. The ecosystems keep the order given (without repeats); ``stages`` has every stage, on or off (a preset fills it when ``stages`` is not given);
    ``knobs`` has every knob; ``build_profile`` and ``context`` (the depth of the model-written context) pass through for the stages that read them."""
    from app.backend.pipeline.acceleration import mining_policy
    ecosystems = list(dict.fromkeys(str(e) for e in raw.get("ecosystems") or [] if str(e).strip()))
    preset = raw.get("preset")
    if raw.get("stages") is not None:
        stages = {s: bool((raw["stages"] or {}).get(s)) for s in STAGE_ORDER}
    else:
        stages = stages_of_preset(preset or "collect")
    knobs = {**DEFAULT_KNOBS, **{k: v for k, v in (raw.get("knobs") or {}).items() if k in DEFAULT_KNOBS}}
    mode = (raw.get('compute') or {}).get('mode', 'cpu')
    if mode not in ('cpu', 'cuda'):
        raise ValueError('Choose CPU or CUDA for the compute mode')
    return {"name": (raw.get("name") or None), "ecosystems": ecosystems, "preset": preset if preset in PRESETS else None, "stages": stages, "knobs": knobs,
            'workflow_version': 2, 'compute': {'mode': mode, 'version': 2},
            'mining': mining_policy.normalize(raw.get('mining')),
            "build_profile": raw.get("build_profile") or None, "context": {**CONTEXT_DEFAULTS, **{k: v for k, v in (raw.get("context") or {}).items() if k in CONTEXT_DEFAULTS}, 'manual': True}}
