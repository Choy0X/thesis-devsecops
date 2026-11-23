"""Resolve and load the exact pinned base checkpoint's tokenizer.

The phase trains Qwen/Qwen2.5-Coder-7B (the base checkpoint). Every token count,
mask and serialization uses the tokenizer of one pinned commit, never a moving
branch. Resolving a commit needs the network; loading needs only the tokenizer
files (a few MB), not the 15 GB of weights.
"""
from __future__ import annotations

import re
import pathlib

_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _api():
    from huggingface_hub import HfApi
    return HfApi()


def resolve_revision(model_id: str, *, api=None) -> str:
    """The commit id of the model repository's current head."""
    if api is None:
        from huggingface_hub import try_to_load_from_cache
        cached = try_to_load_from_cache(model_id, 'config.json')
        if isinstance(cached, str):
            commit = pathlib.Path(cached).parent.name
            if _COMMIT.fullmatch(commit):
                return commit
    sha = (api or _api()).model_info(model_id).sha
    if not isinstance(sha, str) or not _COMMIT.match(sha):
        raise ValueError(f"the hub did not return a commit id for {model_id}: {sha!r}")
    return sha


def load_tokenizer(model_id: str, revision: str):
    """The fast tokenizer of ``model_id`` at a pinned commit (no remote code)."""
    if not isinstance(revision, str) or not _COMMIT.match(revision):
        raise ValueError(f"a pinned 40-character commit revision is required, got {revision!r}")
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(model_id, revision=revision, use_fast=True,
                                         trust_remote_code=False)
