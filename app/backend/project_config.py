"""The project configuration, read from the one config file.

``training/configs/dataset_finetuning.yaml`` (typed in ``training/finetune/config.py``) holds the model, the data
scope, the training settings and the evaluation settings. Everything that needs them (API, workers, exporters,
evaluator, UI) asks this module. It imports no torch, chromadb or sentence-transformers.

The config is cached by file identity and re-read when the file changes. A config that fails validation raises:
the system never falls back to a default. ``ZTSA_CONFIG`` points at an alternative file (tests).
"""
from __future__ import annotations

import os
import pathlib
import re
import threading

from training.finetune import config as cfgmod

_LOCK = threading.Lock()
_CACHE: dict = {}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def config_path() -> pathlib.Path:
    override = os.environ.get("ZTSA_CONFIG")
    return pathlib.Path(override) if override else cfgmod.DEFAULT_PATH


def reload() -> None:
    """Forget the cached config (tests, after the file changed)."""
    with _LOCK:
        _CACHE.clear()


def config() -> cfgmod.ProjectConfig:
    path = config_path()
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _LOCK:
        if _CACHE.get("key") != key:
            _CACHE["cfg"] = cfgmod.load(path)       # raises ConfigError when invalid
            _CACHE["key"] = key
            _CACHE["sha"] = cfgmod.file_sha256(path)
        return _CACHE["cfg"]


def run_fields() -> dict:
    """Fields every run record carries; a stage cannot override them."""
    cfg = config()
    d = cfg.data
    return {"rag_enabled": cfg.rag.enabled,
            "input_policy_hash": cfg.input_policy_hash(),
            "context_policy": cfg.context.policy,
            # what the model is allowed to be shown
            "supplied_context": {
                "policy": cfg.context.policy,
                "advisory_in_discovery_input": d.allow_target_advisory_in_discovery_input,
                "patch_in_discovery_input": d.allow_target_patch_in_discovery_input,
                "oracle_context_in_main_evaluation": d.allow_oracle_context_in_main_evaluation,
                "retrieval": cfg.evaluation.retrieval}}


def summary() -> dict:
    """What the UI shows about the configuration (GET /api/config/summary)."""
    cfg = config()
    return {
        "config_path": str(config_path()),
        "config_sha256": _CACHE["sha"],
        "rag": {"enabled": cfg.rag.enabled},
        "model": {"id": cfg.model.id,
                  "initialization": cfg.model.initialization,
                  "serialization": cfg.model.serialization,
                  "revision_resolved": bool(_COMMIT.match(cfg.model.revision)),
                  "tokenizer_revision_resolved": bool(_COMMIT.match(cfg.model.tokenizer_revision))},
        "data": {"pooling": cfg.data.pooling,
                 "ecosystems": list(cfg.data.ecosystems),
                 "languages": cfg.data.languages,
                 "min_review_tier": cfg.data.min_review_tier,
                 "purpose": cfg.data.purpose,
                 "dataset_version": cfg.data.dataset_version,
                 "validation_fraction_target": cfg.data.validation_fraction_target},
        "training": {"stage": cfg.training.stage,
                     "precision": cfg.precision.mode,
                     "max_total_tokens": cfg.context.max_total_tokens},
    }
