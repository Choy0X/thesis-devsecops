"""The "light" fields of a code pair: what the dataset preview and builder read about each pair without its code (``codestore.light_pair_inputs`` returns a
dict with exactly these keys for each pair). They are listed once so the producer and its consumers can be checked against the same names."""
from __future__ import annotations

FIELDS: tuple[str, ...] = (
    "id", "pair_key", "commit_key", "cve_id", "language", "granularity", "is_test", "tangled", "file_path", "function_name", "changed_lines", "content_hash", "commit_date",
    "repo_url", "fix_commit", "parent_commit", "source", "quarantine_reason", "lines", "has_msg", "enriched", "span", "msg_head", "llm_prompt_version", "llm_model", "has_code",
)
