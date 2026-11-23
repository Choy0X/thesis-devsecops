"""Fix commits and vulnerable/fixed code pairs, in the local SQLite database.

* ``fix_commits`` is the mining queue: one commit per security fix, linked to every advisory/CVE that references it, with a status the miner advances (pending -> mined | no_code | skipped | failed)
  so mining is resumable.
* ``code_pairs`` holds the mined vulnerable/fixed code: one pair per changed file (granularity ``file``) and, per changed function (``function``) or hunk, with the diff, the fix and parent commits
  and the advisory metadata. A pair whose before or after side is missing keeps ``code_available = 0`` and a ``skip_reason`` instead of being silently dropped.

The embedded document functions retain the compatibility module name ``db/codestore_mongo.py`` and are bound at the end of this module under the names the rest of the application uses; what stays here is the
shape of a pair (``PAIR_FIELDS``) and the rules that do not touch the database.
"""
from __future__ import annotations
import json

from app.backend.pipeline.ingest import context as _ctx
from app.backend.pipeline.db import codestore_mongo as _codestore_mongo

PAIR_FIELDS = [
    "commit_key", "cve_id", "cve_ids", "advisory_ids", "ghsa_id", "ecosystem",
    "package", "cwe", "cvss_vector", "published", "commit_date", "repo_url",
    "fix_commit", "parent_commit", "file_path", "language", "function_name",
    "granularity", "is_test", "tangled", "code_before", "code_after", "diff",
    "commit_message", "code_available", "skip_reason", "content_hash", "source",
    "explanation", "root_cause_llm", "secure_pattern_llm", "explanation_llm",
    "llm_model", "llm_prompt_version", "edited_at", "changed_lines",
    "span",                    # hunk pairs: lines "start-end" of the vulnerable file
    # how the pair was extracted and which parent it was taken against (ingest/codemine.py)
    "old_path", "parent_count", "parent_selection_reason", "quarantine_reason",
    "before_blob_sha", "after_blob_sha", "extraction_version",
    # parse outcome and unit attribution (funcsplit.split_with_status, ingest/attribution.py)
    "parse_status", "parse_failure_reason", "function_name_before",
    "attribution_basis", "attribution_score",
    # how the unit was matched and where it came from (funcsplit.analyze, ingest/splitquality.py): the kind and confidence of the match, and the evidence as canonical JSON
    "match_kind", "match_confidence", "extraction_meta",
]
# ── fix commits ─────────────────────────────────────────────────────────────

def _record_commit_keys(rec: dict) -> list[str]:
    try:
        return [p["commit_key"] for p in json.loads(rec.get("fix_commits") or "[]")
                if isinstance(p, dict) and p.get("commit_key")]
    except ValueError:
        return []


# ── code pairs ──────────────────────────────────────────────────────────────

def changed_lines(diff: str | None) -> int:
    """Added + removed lines of a unified diff (ranks a record's pairs)."""
    return sum(1 for l in (diff or "").splitlines()
               if l[:1] in ("+", "-") and not l.startswith(("+++", "---")))


def explanation_fields(explanation, explanation_llm, llm_model) -> dict:
    """The derived explanation columns of one pair. An LLM draft counts as an accepted
    fallback only when the pipeline recorded the model that wrote it."""
    draft = explanation_llm if (llm_model or "").strip() else None
    e = _ctx.resolve_explanation(None, None, explanation, draft)
    return {"explanation_source": e.source, "needs_explanation_enrichment": int(e.needs_enrichment),
            "original_advisory_reference": e.original_advisory_reference}


# ── manual curation (Dataset records -> code pairs) ─────────────────────────

# Fields a user may set on a pair; diff, hash and availability are derived.
EDITABLE = ["cve_id", "ghsa_id", "ecosystem", "package", "cwe", "cvss_vector", "published",
            "repo_url", "fix_commit", "parent_commit", "file_path", "language",
            "function_name", "granularity", "is_test", "code_before", "code_after",
            "explanation", "commit_message"]


def _blank(v):
    return None if v is None or (isinstance(v, str) and not v.strip()) else v


def _derive(rec: dict) -> dict:
    """Recompute what follows from the code: diff, content hash, availability."""
    from app.backend.pipeline.ingest.codemine import _hash, _udiff, language_of
    before, after = rec.get("code_before") or "", rec.get("code_after") or ""
    path = rec.get("file_path") or "snippet"
    rec["language"] = (rec.get("language") or language_of(path) or None)
    rec["language"] = rec["language"].lower() if rec["language"] else None
    rec["granularity"] = rec.get("granularity") or ("function" if rec.get("function_name") else "file")
    rec["is_test"] = int(bool(rec.get("is_test")))
    ok = bool(before.strip() and after.strip())
    rec["code_available"] = int(ok)
    rec["skip_reason"] = None if ok else "incomplete_manual_pair"
    loc = path + (f"::{rec['function_name']}" if rec.get("function_name") else "")
    rec["diff"] = _udiff(before, after, f"a/{loc}", f"b/{loc}") if ok else None
    rec["content_hash"] = _hash(before, after) if ok else None
    rec["changed_lines"] = changed_lines(rec["diff"])
    if rec.get("cve_id"):
        ids = json.loads(rec.get("cve_ids") or "[]")
        rec["cve_ids"] = json.dumps([rec["cve_id"], *[c for c in ids if c != rec["cve_id"]]])
    return rec


# The same facts counted by the database: one row per distinct combination instead of one per pair. Selected in a MATERIALIZED subquery and
# grouped outside it, because a grouped query with an indexed expression in its select list is not answered from the covering index.
THIN_CONTEXT = _ctx.MIN_ADVISORY   # advisory characters below which (and no commit message) context is thin


# ── write-back: mining results onto the Dataset records ─────────────────────

def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


GRANULARITY_RANK = {"function": 2, "hunk": 1, "file": 0}     # most focused first


def rank_pair(p: dict, langs: list[str]) -> tuple:
    """Best pair first: in the CVE's (dominant) language, function level, most
    changed lines. Shared by the record write-back and build_dataset."""
    lang = p.get("language")
    lang_rank = langs.index(lang) if lang in langs else len(langs)
    return (-lang_rank if langs else 0, GRANULARITY_RANK.get(p.get("granularity"), 0),
            int(p.get("changed_lines") or 0))


# ── what a campaign produced (the commits it queued or mined carry its id) ─────────────────────────────────────────

# ── pipeline selections (reads and writes the mining stages used to do with their own SQL) ───────────────────────

# ── pair and commit reads of the dataset builders, the leakage graph and the reviewed build ───────────────────────────────

# ── reset ───────────────────────────────────────────────────────────────────


# -- embedded data layer (compatibility modules pipeline/db/*_mongo.py), bound under the established public names ---------------------
queue_fix_commits = _codestore_mongo.queue_fix_commits
count_fix_commits = _codestore_mongo.count_fix_commits
pending_fix_commits = _codestore_mongo.pending_fix_commits
pending_fix_commit_identities = _codestore_mongo.pending_fix_commit_identities
revalidate_pending_fix_commit_identities = _codestore_mongo.revalidate_pending_fix_commit_identities
count_mineable = _codestore_mongo.count_mineable
sync_links = _codestore_mongo.sync_links
fix_commits_for_record = _codestore_mongo.fix_commits_for_record
requeue_record_commits = _codestore_mongo.requeue_record_commits
mark_fix_commit = _codestore_mongo.mark_fix_commit
replace_commit_files = _codestore_mongo.replace_commit_files
commit_files = _codestore_mongo.commit_files
repository = _codestore_mongo.repository
save_repository = _codestore_mongo.save_repository
set_fix_check = _codestore_mongo.set_fix_check
fix_status_counts = _codestore_mongo.fix_status_counts
fix_commit_summary = _codestore_mongo.fix_commit_summary
list_fix_commits = _codestore_mongo.list_fix_commits
reset_failed_fix_commits = _codestore_mongo.reset_failed_fix_commits
upsert_pairs = _codestore_mongo.upsert_pairs
list_pairs = _codestore_mongo.list_pairs
get_pair = _codestore_mongo.get_pair
pairs_dataframe = _codestore_mongo.pairs_dataframe
delete_pairs_for_commit = _codestore_mongo.delete_pairs_for_commit
create_pair = _codestore_mongo.create_pair
update_pair = _codestore_mongo.update_pair
delete_pair = _codestore_mongo.delete_pair
_refresh_for_ids = _codestore_mongo._refresh_for_ids
pairs_for_record = _codestore_mongo.pairs_for_record
delete_manual_pairs_for_record = _codestore_mongo.delete_manual_pairs_for_record
pair_summary = _codestore_mongo.pair_summary
pairs_for_enrichment = _codestore_mongo.pairs_for_enrichment
set_pair_llm = _codestore_mongo.set_pair_llm
records_for_commit = _codestore_mongo.records_for_commit
refresh_records = _codestore_mongo.refresh_records
campaign_outcomes = _codestore_mongo.campaign_outcomes
campaign_mined = _codestore_mongo.campaign_mined
campaign_mix = _codestore_mongo.campaign_mix
commits_by_status = _codestore_mongo.commits_by_status
count_commits_by_status = _codestore_mongo.count_commits_by_status
pair_files_of = _codestore_mongo.pair_files_of
set_link_evidence = _codestore_mongo.set_link_evidence
attribution_commit_keys = _codestore_mongo.attribution_commit_keys
pairs_for_attribution = _codestore_mongo.pairs_for_attribution
pair_attribution_for = _codestore_mongo.pair_attribution_for
set_pair_quality = _codestore_mongo.set_pair_quality
pair_quality_for = _codestore_mongo.pair_quality_for
set_attribution = _codestore_mongo.set_attribution
file_pair_ids_to_split = _codestore_mongo.file_pair_ids_to_split
count_file_pairs_to_split = _codestore_mongo.count_file_pairs_to_split
pairs_by_ids = _codestore_mongo.pairs_by_ids
derived_pair_keys = _codestore_mongo.derived_pair_keys
delete_pairs_by_keys = _codestore_mongo.delete_pairs_by_keys
file_pair_rows = _codestore_mongo.file_pair_rows
set_file_meta = _codestore_mongo.set_file_meta
set_parse_status = _codestore_mongo.set_parse_status
function_pair_counts = _codestore_mongo.function_pair_counts
set_tangled = _codestore_mongo.set_tangled
commits_basic = _codestore_mongo.commits_basic
usable_pairs_of_commits = _codestore_mongo.usable_pairs_of_commits
pair_code_of = _codestore_mongo.pair_code_of
file_pairs_of_commits = _codestore_mongo.file_pairs_of_commits
best_pair_candidates = _codestore_mongo.best_pair_candidates
light_pair_inputs = _codestore_mongo.light_pair_inputs
reset_mining = _codestore_mongo.reset_mining

reconcile_missing_fix_commits = _codestore_mongo.reconcile_missing_fix_commits
