"""Typed project configuration for fine-tuning and data preparation.

``training/configs/dataset_finetuning.yaml`` is validated here (unknown keys rejected; a field that only has
one implemented value is pinned to it; a flag for a method without code is refused). The rest of the system
reads the config only through this module. Two load modes:

* ``contract``: placeholders (``RESOLVE_AND_PIN_COMMIT``, ``NEXT_UNUSED_VERSION``,
  ``RESOLVE_AFTER_BUILD``) are allowed; this is what the backend and the UI read.
* ``resolved``: every model revision, tokenizer revision, dataset version and
  manifest hash must be pinned; a completed run uses this mode.

This module imports no torch/transformers so the backend can read the phase cheaply.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Annotated, Literal

import yaml
from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_validator)

DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[1] / "configs" / "dataset_finetuning.yaml"
BASE_MODEL_ID = "Qwen/Qwen2.5-Coder-7B"
REVISION_PLACEHOLDER = "RESOLVE_AND_PIN_COMMIT"
VERSION_PLACEHOLDER = "NEXT_UNUSED_VERSION"
MANIFEST_PLACEHOLDER = "RESOLVE_AFTER_BUILD"
TASKS = ("discovery", "condition_assessment", "localization", "repair",
         "insufficient_context", "general_code_replay", "instruction_integrity")
POOLS = ("supported", "reviewed_scoped_negative", "insufficient_context")
PROJECTIONS = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")

# Methods a flag may enable only once their code exists and is tested. A flag for an
# unimplemented method is rejected: a YAML key is not an implementation.
IMPLEMENTED = {"preference": True, "invariance": True}

_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")      # org/name, as the Hub spells it
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^(v\d+|[a-z][a-z0-9_]{0,30})$")


class ConfigError(ValueError):
    """The configuration is invalid; the message lists every offending key."""


def _equal(expected):
    def check(value):
        if value != expected:
            raise ValueError(f"must be {expected!r} in this phase (got {value!r})")
        return value
    return check


def _only(expected):
    return AfterValidator(_equal(expected))


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- sections ---------------------------------------------------------------------

class Rag(_M):
    enabled: Annotated[bool, _only(False)]


class Model(_M):
    id: str
    revision: str
    tokenizer_revision: str
    initialization: str
    trust_remote_code: Annotated[bool, _only(False)]
    serialization: Literal['plain_prompt_completion_v1', 'native_chat_v1', 'sft_native_chat_v1', 'security_chat_v2', 'security_chat_v3', 'security_chat_v4', 'security_chat_v5']
    add_special_tokens: Annotated[bool, _only(False)]
    train_base_weights: Annotated[bool, _only(False)]

    @field_validator("id")
    @classmethod
    def _repo_id(cls, v):
        """A run may name any base model, but by its Hub repository id (org/name): its commit is pinned from the Hub, and a path or a URL would pin nothing."""
        if not _REPO_ID.match(v) or ".." in v:
            raise ValueError(f"must be a Hugging Face repository id such as org/name (got {v!r})")
        return v

    @field_validator("revision", "tokenizer_revision")
    @classmethod
    def _commit(cls, v):
        if v != REVISION_PLACEHOLDER and not _COMMIT.match(v):
            raise ValueError("must be a 40-character lowercase commit id or "
                             f"{REVISION_PLACEHOLDER}")
        return v


class ValidationRoles(_M):
    selection_fraction: float = Field(gt=0, lt=1)
    calibration_fraction: float = Field(gt=0, lt=1)
    fallback: Literal["grouped_crossfit_or_report_insufficient_data"]

    @model_validator(mode="after")
    def _sum(self):
        if abs(self.selection_fraction + self.calibration_fraction - 1.0) > 1e-9:
            raise ValueError("data.validation_roles fractions must sum to 1")
        return self


class Data(_M):
    ecosystems: list[str] = Field(min_length=1)
    languages: Literal["all_supported"] | list[str]
    pooling: Literal["multi_ecosystem", "single_ecosystem"]
    purpose: Literal["production", "software_smoke_test"]
    min_review_tier: Literal["auto_checked", "llm_adjudicated", "human_reviewed"]
    preserve_frozen_versions: bool
    dataset_version: str
    manifest_sha256: str
    canonical_format: Annotated[str, _only("parquet")]
    min_advisory_characters: int
    explanation_field: Annotated[str, _only("explanation")]
    synthetic_explanation_marker: Annotated[str, _only("explanation")]
    unreviewed_supervision_weight: float
    split_before_augmentation: Annotated[bool, _only(True)]
    split_policy: str
    validation_fraction_target: float = Field(gt=0, lt=0.5)
    internal_test_split: bool
    validation_roles: ValidationRoles
    train_on_validation: bool
    final_refit_default: bool
    max_accepted_augmented_views_per_parent: int = Field(ge=0, le=16)
    allow_target_advisory_in_discovery_input: bool
    allow_target_patch_in_discovery_input: bool
    allow_oracle_context_in_main_evaluation: bool
    train_slice_start: float = Field(default=0.0, ge=0, lt=1)       # train on the families from this share of them ...
    train_slice_end: float = Field(default=1.0, gt=0, le=1)         # ... up to this share, in the fixed order of finetune/subset.py (0 to 1: all)

    @model_validator(mode="after")
    def _slice(self):
        if self.train_slice_start >= self.train_slice_end:
            raise ValueError("data.train_slice_start must be below data.train_slice_end")
        return self

    @field_validator("ecosystems")
    @classmethod
    def _unique(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("data.ecosystems must not repeat an ecosystem")
        return v

    @field_validator("dataset_version")
    @classmethod
    def _version(cls, v):
        if v != VERSION_PLACEHOLDER and not _VERSION.match(v):
            raise ValueError(f"must be 'v<N>', a mutable build tag or {VERSION_PLACEHOLDER}")
        return v

    @field_validator("manifest_sha256")
    @classmethod
    def _manifest(cls, v):
        if v != MANIFEST_PLACEHOLDER and not _SHA256.match(v):
            raise ValueError(f"must be a 64-character sha256 or {MANIFEST_PLACEHOLDER}")
        return v


class Leakage(_M):
    near_clone_jaccard: float = Field(gt=0, le=1)
    same_repository_edges: bool
    small_component_max_groups: int = Field(ge=1)
    disagreement_routing: Literal["val_if_small_else_quarantine"]
    min_groups_per_role: int = Field(ge=2)


class Campaign(_M):
    exclude_advisory_kinds: list[str]
    priority: list[str]
    max_requests_per_hour: int = Field(gt=0, le=5000)


class Mining(_M):
    incremental: bool
    resumable: bool
    max_parallel_repository_jobs: int = Field(ge=1)
    max_parallel_api_requests: int = Field(ge=1)
    request_timeout_seconds: int = Field(gt=0)
    retry_attempts: int = Field(ge=0)
    rate_limit_headers_take_precedence: bool
    teacher_enabled: bool
    teacher_model: str | None
    teacher_budget: float | None
    teacher_candidates_initial: int = Field(ge=1)
    teacher_candidates_maximum: int = Field(ge=1)
    unresolved_examples: Annotated[str, _only("quarantine")]
    campaign: Campaign

    @model_validator(mode="after")
    def _teacher(self):
        if self.teacher_enabled and (not self.teacher_model or self.teacher_budget is None):
            raise ValueError("mining: teacher_enabled needs an explicit teacher_model and "
                             "teacher_budget (no silent external calls)")
        if self.teacher_candidates_maximum < self.teacher_candidates_initial:
            raise ValueError("mining: teacher_candidates_maximum < teacher_candidates_initial")
        return self


class Context(_M):
    policy: str
    include_callers_callees_and_configuration: bool
    consult_target_advisory_for_context_selection: bool
    max_total_tokens: int = Field(ge=1024)
    ordinary_completion_reserve: int = Field(ge=64)
    repair_completion_reserve: int = Field(ge=64)
    long_context_ablation_tokens: int = Field(ge=1024)
    silent_truncation: Annotated[bool, _only(False)]
    overlength_policy: Annotated[str, _only("rebuild_valid_view_or_quarantine")]

    @model_validator(mode="after")
    def _reserves(self):
        if max(self.ordinary_completion_reserve, self.repair_completion_reserve) >= self.max_total_tokens:
            raise ValueError("context: a completion reserve must be smaller than max_total_tokens")
        return self


class Sampling(_M):
    task_then_family_then_view: Annotated[bool, _only(True)]
    task_shares: dict[str, float]
    decision_pool_targets: dict[str, float]
    unavailable_pool_behavior: Annotated[str, _only("report_and_explicitly_renormalize")]
    empty_core_pool_behavior: Annotated[str, _only("fail")]
    log_family_exposure: bool

    @model_validator(mode="after")
    def _shares(self):
        corpus = {'upstream_patch', 'code_completion', 'change_analysis'}
        if set(self.task_shares) not in (set(TASKS), set(TASKS) | {'regression_test_design'},
                                       set(TASKS) | {'regression_test_design'} | corpus):
            raise ValueError(f"sampling.task_shares must name exactly {list(TASKS)}")
        if abs(sum(self.task_shares.values()) - 1.0) > 1e-9:
            raise ValueError("sampling.task_shares must sum to 1")
        if set(self.decision_pool_targets) != set(POOLS):
            raise ValueError(f"sampling.decision_pool_targets must name exactly {list(POOLS)}")
        if abs(sum(self.decision_pool_targets.values()) - 1.0) > 1e-9:
            raise ValueError("sampling.decision_pool_targets must sum to 1")
        return self


class Precision(_M):
    mode: Literal["qlora_nf4", "tpu_int4_bf16", "cpu_weight_offload"]
    load_in_4bit: bool
    bnb_4bit_quant_type: Literal["nf4"]
    bnb_4bit_use_double_quant: bool
    compute_dtype: Literal["auto_bf16_if_supported_else_fp16", "bf16"]
    fp16_loss_scaling: bool
    prepare_model_for_kbit_training: bool
    control_if_memory_allows: str


class Lora(_M):
    r: int = Field(ge=1)
    lora_alpha: int = Field(ge=1)
    lora_dropout: float = Field(ge=0, lt=1)
    bias: Literal["none"]
    task_type: Literal["CAUSAL_LM"]
    use_rslora: bool
    use_dora: bool = False             # weight-decomposed low-rank adaptation: a trained magnitude vector per wrapped projection next to the A and B matrices
    init_loftq: bool = False           # start the adapter from the 4-bit quantisation error (LoftQ) instead of from zero; needs the 4-bit load and the model's files on this computer; not with DoRA
    target_modules: list[str] = Field(min_length=1)
    train_and_save_decision_head: bool

    @field_validator("target_modules")
    @classmethod
    def _known(cls, v):
        bad = sorted(set(v) - set(PROJECTIONS))
        if bad:
            raise ValueError(f"lora.target_modules has unknown modules {bad}")
        return v

    @model_validator(mode="after")
    def _initialisation(self):
        if self.init_loftq and self.use_dora:
            raise ValueError("lora: init_loftq and use_dora cannot be on together (DoRA's magnitude vector is set from the weights before LoftQ changes what the adapter adds)")
        return self


class Training(_M):
    stage: Literal["sft_control", "field_balanced", "decision_pair"]
    seeds: list[int] = Field(min_length=1)
    pilot_seed: int
    per_device_train_batch_size: int = Field(ge=1)
    effective_sequence_batch_size: int = Field(ge=1)
    world_size: int = Field(ge=1)
    gradient_accumulation_steps: int = Field(ge=1)
    micro_batch_tuning: Literal['off', 'a100', 'auto'] = 'off'
    length_grouping: bool = False
    lm_loss_chunk_tokens: int = Field(default=64, ge=1, le=4096)
    optimizer: Literal["adamw_torch", "adafactor", "adamw_8bit", "adamw_cpu"] = "adamw_torch"         # adamw_8bit needs bitsandbytes on the machine that trains
    learning_rate: float = Field(gt=0)
    learning_rate_pilot_values: list[float]
    adam_beta1: float
    adam_beta2: float
    adam_epsilon: float
    weight_decay: float = Field(ge=0)
    scheduler: Literal["cosine", "linear", "constant", "constant_with_warmup"] = "cosine"
    warmup_ratio: float = Field(ge=0, lt=1)
    warmup_steps: int | None = Field(default=None, ge=0)         # warm up for this many steps instead of a share of the run; not together with a warmup_ratio above 0
    max_grad_norm: float = Field(gt=0)
    maximum_pilot_family_passes: float = Field(gt=0)
    evaluate_at_family_passes: list[float] = Field(min_length=1)
    evaluate_every_steps: int | None = Field(default=None, ge=1)  # evaluate every this many optimizer steps instead of at the family passes above (which then stay unused)
    activation_offload: bool = False
    pin_frozen_weights: bool = False
    pack_sequences: bool = False       # put several short examples into one row (never attending across examples); for the language-model loss alone, and it saves nothing at a micro-batch of 1
    early_stopping_patience_evaluations: int = Field(ge=1)
    selection_policy: Literal["preregistered_validation_operating_point_with_utility_gates"]
    gradient_checkpointing: bool
    use_cache: Annotated[bool, _only(False)]
    packing: Annotated[bool, _only(False)]
    completion_only_loss: Annotated[bool, _only(True)]
    mask_prompt_and_padding: Annotated[bool, _only(True)]
    supervise_terminal_eos: Annotated[bool, _only(True)]
    save_optimizer_scheduler_rng_sampler_state: Annotated[bool, _only(True)]
    checkpoint_every_optimizer_steps: int = Field(ge=1)
    checkpoint_every_minutes: int | None = Field(default=None, ge=1)
    keep_recent_checkpoints: int = Field(ge=1)
    always_keep_selected_checkpoint: Annotated[bool, _only(True)]

    @model_validator(mode="after")
    def _batch(self):
        product = (self.per_device_train_batch_size * self.gradient_accumulation_steps
                   * self.world_size)
        if product != self.effective_sequence_batch_size:
            raise ValueError(
                "training: effective_sequence_batch_size "
                f"({self.effective_sequence_batch_size}) != per_device_train_batch_size x "
                f"gradient_accumulation_steps x world_size ({product})")
        if self.pilot_seed not in self.seeds:
            raise ValueError("training.pilot_seed must be one of training.seeds")
        if self.pack_sequences and self.stage == "decision_pair":
            raise ValueError("training.pack_sequences is for the language-model loss alone; the decision_pair stage reads one example per row (turn packing off)")
        if self.warmup_steps is not None and self.warmup_ratio > 0:
            raise ValueError("training: set warmup_steps or warmup_ratio, not both (set warmup_ratio to 0 to warm up for a number of steps)")
        if max(self.evaluate_at_family_passes) > self.maximum_pilot_family_passes:
            raise ValueError("training.evaluate_at_family_passes exceeds "
                             "maximum_pilot_family_passes")
        if sorted(self.evaluate_at_family_passes) != self.evaluate_at_family_passes:
            raise ValueError("training.evaluate_at_family_passes must be ascending")
        return self

    def accumulation_for(self, world_size: int) -> int:
        """Gradient accumulation that keeps the effective batch when the world size changes."""
        per_step = self.per_device_train_batch_size * world_size
        if world_size < 1 or self.effective_sequence_batch_size % per_step:
            raise ConfigError(
                f"effective batch {self.effective_sequence_batch_size} is not divisible by "
                f"micro-batch {self.per_device_train_batch_size} x world_size {world_size}")
        return self.effective_sequence_batch_size // per_step


class Runtime(_M):
    session_budget_minutes: int | None = Field(default=None, ge=1)


class FieldWeights(_M):
    decision: float = Field(gt=0)
    evidence: float = Field(gt=0)
    explanation: float = Field(gt=0)
    uncertainty: float = Field(gt=0)
    patch: float = Field(gt=0)
    structure_eos: float = Field(gt=0)


class DecisionHead(_M):
    classes: list[str]
    pooling: Annotated[str, _only("last_nonpadding_input_position")]
    weight_stage_1: float = Field(ge=0)
    weight_stage_2: float = Field(ge=0)

    @field_validator("classes")
    @classmethod
    def _labels(cls, v):
        want = ["vulnerability_supported", "no_supported_finding_in_scope", "insufficient_context"]
        if v != want:
            raise ValueError(f"loss.decision_head.classes must be exactly {want} in this order")
        return v


class Pair(_M):
    enabled_stage_1: bool
    enabled_stage_2: bool
    margin: float = Field(gt=0)
    rank_weight: float = Field(ge=0)
    independent_version_encoding: Annotated[bool, _only(True)]
    unknown_pair_loss: Annotated[bool, _only(False)]


class Invariance(_M):
    enabled: bool
    weight_if_enabled: float = Field(ge=0)

    @field_validator("enabled")
    @classmethod
    def _implemented(cls, v):
        if v and not IMPLEMENTED["invariance"]:
            raise ValueError("loss.invariance.enabled: not implemented yet (a flag is not an "
                             "implementation)")
        return v


class Loss(_M):
    initial: Literal["per_example_completion_ce"]
    next_ablation: Literal["present_field_normalized_ce"]
    field_weights: FieldWeights
    decision_head: DecisionHead
    pair: Pair
    invariance: Invariance


class Calibration(_M):
    min_negatives: int = Field(ge=1)


class StudentCorrection(_M):
    enabled_after_selected_sft: bool
    train_pool_only: bool
    maximum_rounds: int = Field(ge=0, le=5)
    maximum_relevant_task_share: float = Field(gt=0, le=1)
    independent_adjudication_required: bool


class Preference(_M):
    enabled: bool
    algorithm: Literal["dpo_with_explicit_sft_replay"]
    beta: float = Field(gt=0)
    learning_rate: float = Field(gt=0)
    max_passes: int = Field(ge=1)
    replay_weight: float = Field(ge=0)
    reference: str
    verified_ordering_required: bool

    @field_validator("enabled")
    @classmethod
    def _implemented(cls, v):
        if v and not IMPLEMENTED["preference"]:
            raise ValueError("preference.enabled: not implemented yet (a flag is not an "
                             "implementation)")
        return v


class Advanced(_M):
    online_rl: bool
    continued_pretraining: bool
    full_parameter_finetuning: bool
    pcgrad: bool
    group_dro: bool
    adaptive_rank: bool
    gradient_diagnostics: bool

    @model_validator(mode="after")
    def _unsupported(self):
        on = [k for k in ("online_rl", "continued_pretraining", "full_parameter_finetuning",
                          "pcgrad", "group_dro", "adaptive_rank") if getattr(self, k)]
        if on:
            raise ValueError(f"advanced: unsupported in this phase (no verified implementation "
                             f"or deliberately deferred): {', '.join(on)}")
        return self


class Verification(_M):
    execution_scope: Annotated[str, _only("local_isolated_defensive_fixtures")]
    network_during_tests: Annotated[bool, _only(False)]
    readonly_acceptance_tests: Annotated[bool, _only(True)]
    allow_model_to_modify_verifier: Annotated[bool, _only(False)]
    require_before_after_security_check_for_verified_repair: bool
    require_functionality_for_verified_repair: bool
    environment_failure_is_success: bool
    patch_candidates_cap: int = Field(ge=1)
    patch_revision_rounds_cap: int = Field(ge=0)
    default_case_wall_time_seconds: int = Field(gt=0)
    resource_limits_required: Annotated[bool, _only(True)]


class Evaluation(_M):
    retrieval: str
    same_context_and_budget_across_models: bool
    stronger_model_fallback: bool
    greedy_primary: bool
    greedy_max_new_tokens: int = Field(ge=1)
    repair_max_new_tokens: int = Field(ge=1)
    sampled_patch_candidates: int = Field(ge=1)
    sampled_patch_temperature: float = Field(ge=0)
    sampled_patch_top_p: float = Field(gt=0, le=1)
    threshold_selection: str
    illustrative_fpr_operating_points: list[float]
    insufficient_negative_sample_action: str
    report_critical_false_negative_rate: bool
    report_abstention_and_coverage: bool
    primary_results_use_actual_selected_output: bool
    preserve_all_failed_runs: bool


class ProjectConfig(_M):
    schema_version: Annotated[int, _only(1)]
    rag: Rag = Field(default_factory=lambda: Rag(enabled=False), exclude=True)
    model: Model
    data: Data
    leakage: Leakage
    mining: Mining
    context: Context
    sampling: Sampling
    precision: Precision
    lora: Lora
    training: Training
    runtime: Runtime
    loss: Loss
    calibration: Calibration
    student_correction: StudentCorrection
    preference: Preference
    advanced: Advanced
    verification: Verification
    evaluation: Evaluation

    @model_validator(mode="after")
    def _loftq_needs_four_bit(self):
        if ('regression_test_design' in self.sampling.task_shares
                and self.model.serialization not in ('sft_native_chat_v1', 'security_chat_v2', 'security_chat_v3', 'security_chat_v4', 'security_chat_v5')):
            raise ValueError('regression_test_design requires sft_native_chat_v1 or security_chat_v2 serialization')
        if (set(self.sampling.task_shares) & {'upstream_patch', 'code_completion', 'change_analysis'}
                and self.model.serialization != 'sft_native_chat_v1'):
            raise ValueError('source corpus tasks require sft_native_chat_v1 serialization')
        if self.precision.mode == 'cpu_weight_offload' and (self.precision.load_in_4bit or self.precision.prepare_model_for_kbit_training):
            raise ValueError('CPU weight offload is a distinct full-precision path; disable 4-bit loading and kbit preparation.')
        if self.lora.init_loftq and not self.precision.load_in_4bit:
            raise ValueError("lora.init_loftq needs precision.load_in_4bit: LoftQ compensates the error of the 4-bit weights, so there is nothing to compensate without them")
        return self

    # -- hashes ---------------------------------------------------------------
    def _digest(self, payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def input_policy_hash(self) -> str:
        """Identity of what the model is shown (context policy, serialization and the input flags); two runs are comparable
        only when it matches."""
        d = self.data
        return self._digest({"context": self.context.model_dump(),
                             "serialization": self.model.serialization,
                             "allow": [d.allow_target_advisory_in_discovery_input,
                                       d.allow_target_patch_in_discovery_input,
                                       d.allow_oracle_context_in_main_evaluation]})


# -- loading ----------------------------------------------------------------------

_PLACEHOLDERS = {
    "model.revision": REVISION_PLACEHOLDER,
    "model.tokenizer_revision": REVISION_PLACEHOLDER,
    "data.dataset_version": VERSION_PLACEHOLDER,
    "data.manifest_sha256": MANIFEST_PLACEHOLDER,
}


def _format_errors(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"])
        lines.append(f"{loc}: {e['msg']}" if loc else e["msg"])
    return "; ".join(lines)


def unresolved(cfg: ProjectConfig) -> list[str]:
    """Keys that still hold a placeholder."""
    out = []
    for dotted, token in _PLACEHOLDERS.items():
        section, key = dotted.split(".")
        if getattr(getattr(cfg, section), key) == token:
            out.append(dotted)
    return out


def parse(data: dict, mode: Literal["contract", "resolved"] = "contract") -> ProjectConfig:
    if mode not in ("contract", "resolved"):
        raise ValueError("mode must be 'contract' or 'resolved'")
    try:
        cfg = ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_errors(e)) from e
    if mode == "resolved":
        open_keys = unresolved(cfg)
        if open_keys:
            raise ConfigError("unresolved placeholders in resolved mode: " + ", ".join(open_keys))
    return cfg


def load(path: str | pathlib.Path = DEFAULT_PATH,
         mode: Literal["contract", "resolved"] = "contract") -> ProjectConfig:
    text = pathlib.Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} is not a mapping")
    return parse(data, mode)


def file_sha256(path: str | pathlib.Path = DEFAULT_PATH) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
