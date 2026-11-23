"""The attempt table: one model answer to one view. No torch, no network.

Both sides use this module: the GPU side (``infer.py``) writes the attempts of an evaluation, the app (``eval/shared.py``) reads them, checks
them and scores them. An attempt has a status (``answered``, ``malformed``, ``abstained``, ``timeout`` or ``env_error``) and nothing is
dropped; ``detail`` says why one was not answered (for instance a generation cut off at the token limit). The Parquet file has a fixed schema
and order (sorted by attempt id), so the same attempts give the same bytes.
"""
from __future__ import annotations

import json
import pathlib

STATUSES = ("answered", "malformed", "abstained", "timeout", "env_error")
TRACKS = ("generated_decision", "head_decision", "repair", "instruction_integrity", "general_utility", "severity_triage")
SUP, NOF, INS = "vulnerability_supported", "no_supported_finding_in_scope", "insufficient_context"
STATE_LABELS = (SUP, NOF, INS)
SEVERITIES = ("Recommendation", "Warning", "Critical")
VERIFICATION = ("passed", "failed", "env_error", "unavailable", "not_applicable")
DECISION_TRACKS = ("generated_decision", "head_decision")
STATUS_TO_LABEL = {"finding": SUP, "no_finding": NOF, "insufficient_context": INS}
PAIR_POOLS = {"supported": "vulnerable", "scoped_negative": "protected"}
COLUMNS = [("attempt_id", "string"), ("arm", "string"), ("track", "string"), ("view_id", "string"), ("status", "string"),
           ("truth", "string"), ("prediction", "string"), ("score", "float64"), ("severity", "string"),
           ("predicted_severity", "string"), ("issue_group_id", "string"), ("repository_identity", "string"),
           ("ecosystem", "string"), ("language", "string"), ("pair_id", "string"), ("role", "string"), ("path", "string"),
           ("repair_input", "string"), ("verification", "string"), ("receipt_id", "string"), ("followed_injection", "bool"),
           ("utility_pass", "bool"), ("split", "string"), ("detail", "string")]
FIELDS = tuple(name for name, _ in COLUMNS)
REQUIRED = ("attempt_id", "arm", "track", "view_id", "status")


# -- the attempt table ---------------------------------------------------------------------------------------------------------------------

def validate_attempt(row: dict) -> list[str]:
    """What is wrong with one attempt (empty when it is usable)."""
    problems = [f"unknown field {k}" for k in row if k not in FIELDS]
    problems += [f"{k} is required" for k in REQUIRED if row.get(k) in (None, "")]
    if row.get("status") not in STATUSES:
        problems.append(f"status must be one of {STATUSES}, got {row.get('status')!r}")
    if row.get("track") not in TRACKS:
        problems.append(f"track must be one of {TRACKS}, got {row.get('track')!r}")
    if row.get("truth") not in (None, *STATE_LABELS):
        problems.append(f"truth must be one of {STATE_LABELS}, got {row.get('truth')!r}")
    if row.get("track") in DECISION_TRACKS and row.get("status") == "answered":
        if row.get("prediction") not in STATE_LABELS:
            problems.append(f"an answered decision attempt needs a prediction in {STATE_LABELS}, got {row.get('prediction')!r}")
        if row.get("track") == "head_decision" and row.get("score") is None:
            problems.append("an answered head_decision attempt needs a score")
    if row.get("verification") not in (None, *VERIFICATION):
        problems.append(f"verification must be one of {VERIFICATION}, got {row.get('verification')!r}")
    return problems


def write_attempts(path, rows) -> pathlib.Path:
    """Parquet with a fixed schema and order (sorted by attempt id): the same attempts give the same bytes."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = list(rows)
    problems = [f"{r.get('attempt_id')}: {p}" for r in rows for p in validate_attempt(r)]
    if problems:
        raise ValueError("invalid attempts: " + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
    schema = pa.schema([(name, getattr(pa, "bool_" if kind == "bool" else kind)()) for name, kind in COLUMNS])
    ordered = sorted(rows, key=lambda r: r["attempt_id"])
    table = pa.Table.from_pylist([{k: r.get(k) for k in FIELDS} for r in ordered], schema=schema)
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return path


def read_attempts(path) -> list[dict]:
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


# -- what a view says about its attempt -----------------------------------------------------------------------------------------------------

def truth_of(view: dict) -> str | None:
    """The label state of a view: what its target answer says (``finding`` is a supported vulnerability, and so on)."""
    response = json.loads(view["response_json"])
    return STATUS_TO_LABEL.get(response.get("status")) if isinstance(response, dict) else None


FACT_KEYS = ("truth", "issue_group_id", "repository_identity", "ecosystem", "language", "pair_id", "role", "path", "severity", "split")


def facts_of(view: dict, record: dict) -> dict:
    """The facts of an attempt that come from the dataset and not from the model: its truth, its group, repository, ecosystem, language, pair,
    path and severity (only a vulnerable case has one). An importer recomputes these from the bundle's own dataset and refuses a mismatch."""
    task, truth = view["task"], truth_of(view)
    pair = task == "discovery" and view["pool"] in PAIR_POOLS
    return {"truth": truth, "issue_group_id": view.get("issue_group_id"), "repository_identity": record.get("repository_identity"),
            "ecosystem": record.get("ecosystem"), "language": record.get("language"),
            "pair_id": f"discovery:{view['record_id']}" if pair else None, "role": PAIR_POOLS.get(view["pool"]) if pair else None,
            "path": record.get("file_path_after"), "severity": record.get("severity_label") if truth == SUP else None,
            "split": view.get("split")}
