"""Read results from earlier patch-verification runs."""
import json
import pathlib
from app.backend.pipeline import runs

CASE_KEYS = ("case_id", "library", "repo_url", "fix_sha", "parent_sha", "tests", "prod", "evidence_level", "cve_ids")

def known_cases(source=None) -> list[dict]:
    """Read case identifiers and outcomes from a saved pilot report."""
    f = pathlib.Path(source or runs.EXP / "verify_pilot") / "results.json"
    try:
        rows = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{**{k: r[k] for k in CASE_KEYS}, "outcome": r.get("outcome")} for r in rows if isinstance(r, dict) and all(k in r for k in CASE_KEYS)]

def rerun_case(case_id, **kwargs):
    raise RuntimeError("Vulnerability reproduction is unavailable in this distribution.")
