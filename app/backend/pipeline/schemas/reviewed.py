"""The reviewed-record schema (``reviewed-record/1``).

One row per example, holding everything known about it: provenance, code and
context, the security supervision, explanation provenance, verification evidence and
grouping. It is the *reviewed* layer between the immutable raw store
(the ``advisories`` / ``fix_commits`` / ``code_pairs`` collections) and the training views.
Metadata here is for review, grouping and traceability: it is NOT allowed into a
student input; views are built through per-task allowlists (``pipeline/views``).

``FIELDS`` is the single documented mapping: every field names the existing column
it is taken from, or ``new:`` with where it will come from. Nested objects are JSON
text columns; lists are Parquet ``list<string>``.

Schema validity and semantic validity are different checks: ``validate`` checks types,
vocabularies and the explanation-provenance invariants of the agreed policy; it does
not say the label is right.
"""
from __future__ import annotations

import json
import pathlib
from typing import NamedTuple

SCHEMA_VERSION = "reviewed-record/1"

LABEL_STATES = ("vulnerability_supported", "no_supported_finding_in_scope",
                "insufficient_context", "unreviewed")
REVIEW_STATUSES = ("unreviewed", "auto_checked", "llm_adjudicated", "human_reviewed")
EXPLANATION_SOURCES = ("advisory", "llm_fallback", "short_advisory", "missing")
GRANULARITIES = ("function", "hunk", "file")
SPLITS = ("train", "val", "test", "quarantine")
DATA_QUALITY = ("ok", "quarantined", "excluded")
GROUPS = ("trace", "identity", "code", "security", "annotation", "verification", "grouping")


class Field(NamedTuple):
    name: str
    type: str          # str | int | float | bool | list | json
    group: str
    source: str        # existing column, or "new: ..."


def _f(group, rows):
    return [Field(n, t, group, s) for n, t, s in rows]


FIELDS: list[Field] = (
    _f("trace", [
        ("schema_version", "str", "new: reviewed-record schema constant"),
        ("raw_pair_key", "str", "code_pairs.pair_key (the raw row this record was built from)"),
        ("raw_record_keys", "list", "records.key of the advisories linked to the pair"),
    ]) + _f("identity", [
        ("record_id", "str", "new: stable id derived from code_pairs.pair_key"),
        ("issue_group_id", "str", "new: leakage-graph component (derived.leakage_edges)"),
        ("advisory_aliases", "list", "records.cve_id + code_pairs.advisory_ids + records.aliases (new column)"),
        ("repository_identity", "str", "code_pairs.repo_url, canonicalized"),
        ("package_identity", "str", "code_pairs.ecosystem + code_pairs.package, canonicalized by the ecosystem adapter"),
        ("ecosystem", "str", "code_pairs.ecosystem"),
        ("language", "str", "code_pairs.language"),
        ("source_urls", "list", "new: records.source_urls (OSV references) + fix_commits.repo_url"),
        ("source_record_hashes", "list", "derived.raw_record_hashes"),
        ("repository_license", "str", "new: repository license captured by the collector"),
        ("data_usage_status", "str", "new: derived from repository_license (usable | restricted | unknown)"),
        ("source_snapshot_time", "str", "meta.collection_at / derived.raw_snapshots.collected_at"),
        ("advisory_first_published_at", "str", "code_pairs.published (earliest of OSV and NVD)"),
        ("advisory_modified_at", "str", "records.modified"),
        ("fix_commit", "str", "code_pairs.fix_commit"),
        ("relevant_parent_commit", "str", "code_pairs.parent_commit"),
        ("parent_selection_reason", "str", "new: code_pairs.parent_selection_reason (parent audit)"),
        ("before_tree_hash", "str", "new: git tree id of the parent (parent audit)"),
        ("after_tree_hash", "str", "new: git tree id of the fix commit (parent audit)"),
        ("file_blob_hashes", "json", "new: code_pairs.before_blob_sha / after_blob_sha"),
        ("collector_version", "str", "new: collector version constant"),
        ("extraction_version", "str", "funcsplit.VERSION / new: code_pairs.extraction_version"),
        ("review_version", "str", "new: reviewer rubric version"),
    ]) + _f("code", [
        ("vulnerable_code", "str", "code_pairs.code_before"),
        ("fixed_code", "str", "code_pairs.code_after"),
        ("diff", "str", "code_pairs.diff"),
        ("file_path_before", "str", "new: code_pairs.old_path (rename mapping)"),
        ("file_path_after", "str", "code_pairs.file_path"),
        ("symbol", "str", "code_pairs.function_name"),
        ("byte_span_before", "str", "new: computed from the stored blob"),
        ("line_span_before", "str", "code_pairs.span (hunks) / computed for functions"),
        ("byte_span_after", "str", "new: computed from the stored blob"),
        ("line_span_after", "str", "new: computed from the stored blob"),
        ("changed_lines", "int", "code_pairs.changed_lines"),
        ("reviewed_security_spans", "json", "new: derived.review_decisions (human or adjudicated spans)"),
        ("granularity", "str", "code_pairs.granularity"),
        ("context_before", "str", "new: context builder (supplied local context before the unit)"),
        ("context_after", "str", "new: context builder"),
        ("context_manifest", "json", "new: context builder (files, spans, token counts, omissions)"),
        ("entrypoints", "list", "new: context builder"),
        ("callers", "list", "new: context builder"),
        ("callees", "list", "new: context builder"),
        ("imports", "list", "new: context builder"),
        ("relevant_configuration", "list", "new: context builder (config and manifest files)"),
        ("dependency_versions", "json", "records.affected_versions / records.fixed_version / records.safe_version"),
        ("evidence_completeness", "str", "new: context builder (complete | partial | changed_files_only)"),
        ("omitted_context_reasons", "list", "new: context builder"),
    ]) + _f("security", [
        ("condition_scope", "str", "new: review (what condition was checked)"),
        ("trust_boundary", "str", "new: review"),
        ("attacker_control", "str", "new: review"),
        ("preconditions", "list", "new: review"),
        ("violated_invariant", "str", "new: review"),
        ("evidence_spans", "json", "new: review; auto_checked rows hold diff-line spans marked unreviewed"),
        ("decisive_safeguard", "str", "new: review"),
        ("fixed_review", "json", "new: independent scoped fixed-side review with source quotes"),
        ("behavioral_intent", "str", "new: reviewed intended behavior preserved by the repair"),
        ("supported_impact", "str", "new: review"),
        ("missing_facts", "list", "new: review"),
        ("label_state", "str", "new: review decision (unreviewed until a decision or an auto-check basis exists)"),
        ("label_basis", "str", "new: advisory_fix_diff | human_review | llm_adjudication | verified_execution"),
        ("production_relevance", "str", "new: derived from path category (production | test | docs | config | vendor | generated)"),
        ("cwe_ids", "list", "records.osv_cwe (fallback: nvd.cwe)"),
        ("cwe_source", "str", "new: osv | nvd | teacher_suggestion | missing"),
        ("advisory_cvss", "float", "records.cvss_score / score_from_vector(records.cvss_vector)"),
        ("cvss_version", "str", "records.cvss_vector prefix / nvd.cvss_version"),
        ("severity_label", "str", "build_common.label(advisory_cvss), kept apart from discovery"),
        ("severity_source", "str", "build_code_dataset._severity (nvd | cvss_v3 | cvss_v4 | record)"),
        ("severity_applicability", "str", "new: known_advisory_only (never discovery ground truth)"),
    ]) + _f("annotation", [
        ("explanation", "str", "code_pairs.explanation, resolved by context.resolve_explanation"),
        ("synthetic_fields", "list", "new: derived from the explanation source and llm fields"),
        ("explanation_source", "str", "new: advisory | llm_fallback | short_advisory | missing"),
        ("original_advisory_reference", "str", "new: the advisory text kept when a fallback replaced a short one"),
        ("teacher_model_id", "str", "code_pairs.llm_model"),
        ("teacher_revision_if_available", "str", "new: teacher provider revision"),
        ("teacher_prompt_hash", "str", "new: hash of the teacher prompt (code_pairs.llm_prompt_version)"),
        ("generation_time", "str", "new: derived.explanation_receipts.created_at"),
        ("review_status", "str", "new: unreviewed | auto_checked | llm_adjudicated | human_reviewed"),
        ("reviewer_or_verifier_identity", "str", "new: derived.review_decisions.reviewer"),
        ("evidence_summary", "str", "new: extractive summary of the advisory (extractive_first_sentences_v1)"),
        ("evidence_summary_provenance", "str", "new: extractive | teacher | human"),
        ("admission_policy", "str", "new: versioned automatic admission policy; absent on legacy records"),
        ("admission_outcome", "str", "new: admitted | insufficient_context | excluded"),
        ("admission_reasons", "list", "new: automatic evidence and source checks"),
        ("admission_provenance", "json", "new: source-bound automatic decision receipt, never human approval"),
        ("source_revalidation", "json", "new: backend-issued source supersession; no security verdict"),
        ("context_suggestions", "json", "new: optional context quality and missing-fact suggestions"),
    ]) + _f("verification", [
        ("provenance_status", "str", "fix_commits.fix_check (G0: verified | suspect | unknown)"),
        ("semantic_status", "str", "new: G1 status derived from review_decisions"),
        ("before_security_result", "str", "new: verification receipt (G2)"),
        ("after_security_result", "str", "new: verification receipt (G2)"),
        ("functionality_result", "str", "new: verification receipt (G3)"),
        ("verifier_integrity_result", "str", "new: verification receipt"),
        ("environment_digest", "str", "new: verification receipt"),
        ("dependency_lock_hash", "str", "new: verification receipt"),
        ("verification_receipt_ids", "list", "new: verification receipts"),
        ("test_artifact_hashes", "list", "new: verification receipts"),
        ("environment_failure_reason", "str", "new: verification receipt"),
        ("functional_contract", "str", "new: declared legitimate behaviour for repair"),
    ]) + _f("grouping", [
        ("lineage_parent_ids", "list", "new: parent record ids of a derived (augmented) record"),
        ("fix_family_id", "str", "new: leakage graph (fix commit, backport series)"),
        ("clone_group_ids", "list", "new: leakage graph (identical and near-identical code)"),
        ("split", "str", "split.assign, persisted in the split manifest"),
        ("split_manifest_version", "str", "new: derived.split_manifests.version"),
        ("eligible_tasks", "list", "new: derived from review status, label state and context completeness"),
        ("quarantine_reasons", "list", "new: unresolved extraction, ambiguous parent, graph conflicts"),
        ("exclusion_reasons", "list", "new: overlength, test, tangled, restricted license"),
        ("data_quality_status", "str", "new: ok | quarantined | excluded"),
        ("augmentation_policy_version", "str", "new: augmentation policy constant"),
    ])
)
NAMES = [f.name for f in FIELDS]
_BY_NAME = {f.name: f for f in FIELDS}


def record_id_of(pair_key: str) -> str:
    """The stable id of a reviewed record: derived from the raw pair key, so a decision follows its pair."""
    import hashlib
    return "rec_" + hashlib.sha1(pair_key.encode("utf-8")).hexdigest()[:16]


def tier_at_least(status: str, minimum: str) -> bool:
    """True when review status ``status`` is at or above ``minimum`` in REVIEW_STATUSES order."""
    return REVIEW_STATUSES.index(status) >= REVIEW_STATUSES.index(minimum)


def _default(f: Field):
    return [] if f.type == "list" else None


def new_record(**kw) -> dict:
    """A complete record with defaults: unreviewed and untrusted until evidence exists."""
    unknown = sorted(set(kw) - set(NAMES))
    if unknown:
        raise ValueError(f"unknown reviewed-record field(s): {unknown}")
    rec = {f.name: _default(f) for f in FIELDS}
    rec.update(schema_version=SCHEMA_VERSION, label_state="unreviewed", review_status="unreviewed",
               synthetic_fields=[], data_quality_status="ok")
    rec.update(kw)
    return rec


def validate(rec: dict) -> list[str]:
    """Problems with a record (empty when valid): types, vocabularies, the explanation
    provenance invariants, and 'an unreviewed row carries no trusted label'."""
    out: list[str] = []
    for k in sorted(set(rec) - set(NAMES)):
        out.append(f"unknown field {k}")
    for f in FIELDS:
        if f.name not in rec:
            if f.name == 'source_revalidation':
                continue
            if f.name in {"admission_policy", "admission_outcome", "admission_reasons", "admission_provenance", "context_suggestions"} and not rec.get('admission_policy'):
                continue  # Historical records predate these additive metadata fields.
            out.append(f"missing field {f.name}")
            continue
        v = rec[f.name]
        if v is None:
            continue
        ok = {"str": isinstance(v, str),
              "int": isinstance(v, int) and not isinstance(v, bool),
              "float": isinstance(v, (int, float)) and not isinstance(v, bool),
              "bool": isinstance(v, bool),
              "list": isinstance(v, list) and all(isinstance(x, str) for x in v),
              "json": isinstance(v, str)}[f.type]
        if not ok:
            out.append(f"{f.name} must be {f.type if f.type != 'json' else 'JSON text'}, got {type(v).__name__}")
        elif f.type == "json":
            try:
                json.loads(v)
            except ValueError:
                out.append(f"{f.name} is not valid JSON text")
    if out:
        return out
    if not rec["record_id"]:
        out.append("record_id is required")
    if not rec["ecosystem"]:
        out.append("ecosystem is required")
    for name, vocab, nullable in (("label_state", LABEL_STATES, False), ("review_status", REVIEW_STATUSES, False),
                                  ("explanation_source", EXPLANATION_SOURCES, True),
                                  ("granularity", GRANULARITIES, True), ("split", SPLITS, True),
                                  ("data_quality_status", DATA_QUALITY, True)):
        v = rec[name]
        if v is None and nullable:
            continue
        if v not in vocab:
            out.append(f"{name} {v!r} is not one of {list(vocab)}")
    if rec["schema_version"] != SCHEMA_VERSION:
        out.append(f"schema_version {rec['schema_version']!r} != {SCHEMA_VERSION}")
    if rec["review_status"] == "unreviewed" and rec["label_state"] != "unreviewed":
        out.append("an unreviewed record must have label_state 'unreviewed' (no trusted label without a review status)")
    src, synth = rec["explanation_source"], rec["synthetic_fields"]
    if src == "llm_fallback" and "explanation" not in synth:
        out.append("explanation_source llm_fallback requires 'explanation' in synthetic_fields")
    if src in ("advisory", "short_advisory", "missing") and "explanation" in synth:
        out.append(f"explanation_source {src} is real or absent text: 'explanation' must not be in synthetic_fields")
    return out


def _arrow_type(t: str):
    import pyarrow as pa
    return {"str": pa.string(), "int": pa.int64(), "float": pa.float64(), "bool": pa.bool_(),
            "list": pa.list_(pa.string()), "json": pa.string()}[t]


def arrow_schema():
    import pyarrow as pa
    return pa.schema([(f.name, _arrow_type(f.type)) for f in FIELDS],
                     metadata={"schema_version": SCHEMA_VERSION})


def write_parquet(rows: list[dict], path) -> pathlib.Path:
    """Validate every row, then write Parquet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.backend.pipeline import fsio

    problems = []
    for i, r in enumerate(rows):
        for p in validate(r):
            problems.append(f"row {i} ({r.get('record_id')}): {p}")
    if problems:
        raise ValueError("invalid reviewed record(s): " + "; ".join(problems[:10]) +
                         (f" ... and {len(problems) - 10} more" if len(problems) > 10 else ""))
    path = pathlib.Path(path)
    table = pa.Table.from_pylist(rows, schema=arrow_schema())
    fsio.write_with(path, lambda part: pq.write_table(table, part))
    return path


def read_parquet(path) -> list[dict]:
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


def mapping_markdown() -> str:
    """The one mapping from the spec's field groups onto existing columns, for data/README.md."""
    lines = ["| field | group | type | source |", "|---|---|---|---|"]
    lines += [f"| {f.name} | {f.group} | {f.type} | {f.source} |" for f in FIELDS]
    return "\n".join(lines)
