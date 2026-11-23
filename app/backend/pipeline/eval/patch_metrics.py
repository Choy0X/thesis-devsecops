"""Shared evaluation for patch generation (vulnerable code -> fixed code).

Every patch-generation run - the API model baseline, the base open model
zero-shot and the LoRA fine-tuned model (Colab) - is scored here with the same
prompt and the same metrics, so the comparison is valid (all arms report through the
shared evaluation path).

Per sample, against the reference fix mined from the real fix commit:

* ``exact_match``       normalised prediction == reference fixed code;
* ``edit_similarity``   difflib ratio between prediction and reference (0..1);
* ``fix_line_recall``   share of the lines the reference fix *added* that appear in
                        the prediction;
* ``vuln_line_removal`` share of the lines the reference fix *removed* (the
                        vulnerable lines) that no longer appear in the prediction;
* ``unchanged``         the prediction returns the vulnerable code unchanged;
* ``syntax_valid``      Python only: the prediction parses (``ast``).

``plausible_fix`` (all vulnerable lines removed, at least half of the fix lines
present, and syntactically valid where checkable) is a static proxy, NOT a verified
fix; behavioural verification (build, tests, exploit) is the sandbox harness of RQ5.
"""
from __future__ import annotations
import datetime
import difflib
import json
import pathlib
import textwrap

import pandas as pd

from app.backend.pipeline.pyparse import parse_python
from app.backend.pipeline import util

PROC = pathlib.Path("data/processed")
PROMPT_VERSION = "patch-v1"
PATCH_SYSTEM = (
    "You are a security engineer. The code below contains a security vulnerability. "
    "Rewrite it so that the vulnerability is fixed while keeping its behaviour and "
    "interface. Return only the complete fixed code, with no explanation and no "
    "markdown fences.")


def build_prompt(s: dict) -> str:
    """User prompt for one sample (identical for every arm; the Colab notebook embeds
    the same text, keep them in sync with PROMPT_VERSION)."""
    return (f"Language: {s.get('language') or 'unknown'}\n"
            f"CWE: {s.get('cwe') or 'unknown'}\n"
            f"Advisory: {(s.get('explanation') or '').strip()[:800]}\n\n"
            f"Vulnerable code:\n{s.get('vulnerable_code') or ''}")


def extract_code(text: str) -> str:
    """Strip a markdown fence if the model added one anyway."""
    t = (text or "").strip()
    if "```" in t:
        parts = t.split("```")
        if len(parts) >= 3:
            block = parts[1]
            first, _, rest = block.partition("\n")
            t = rest if first.strip() and " " not in first.strip() else block
    return t.strip("\n")


def default_dataset() -> pathlib.Path:
    from app.backend.pipeline import versions
    return versions.resolve("code_pairs", ".jsonl", "code_pairs_preview.jsonl", PROC)


def load_samples(path: str | pathlib.Path | None = None, split: str | None = "val",
                 languages: list[str] | None = None, granularity: str | None = None,
                 n: int | None = None) -> list[dict]:
    path = pathlib.Path(path) if path else default_dataset()
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; build the code-pair dataset first")
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            s = json.loads(line)
            if split and s.get("split") != split:
                continue
            if languages and s.get("language") not in {l.lower() for l in languages}:
                continue
            if granularity and s.get("granularity") != granularity:
                continue
            out.append(s)
    return out[:n] if n else out


# ── metrics ─────────────────────────────────────────────────────────────────


def _lines(code: str | None) -> list[str]:
    return [l.strip() for l in (code or "").splitlines() if l.strip()]


def _changes(before: str, after: str) -> tuple[list[str], list[str]]:
    """(removed lines, added lines) of the reference fix, whitespace-insensitive."""
    b, a = _lines(before), _lines(after)
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=b, b=a, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed += b[i1:i2]
        if op in ("replace", "insert"):
            added += a[j1:j2]
    return removed, added


def _similarity(a: str, b: str) -> float:
    """Character-level difflib ratio; for very long code (quadratic cost) a line-level
    ratio is used instead so a single huge function cannot stall the evaluation."""
    if len(a) + len(b) <= 20000:
        return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()
    return difflib.SequenceMatcher(a=a.splitlines(), b=b.splitlines(), autojunk=False).ratio()


def score(vulnerable: str, reference: str, prediction: str, language: str | None) -> dict:
    pred = util.norm_code(prediction)
    ref, vul = util.norm_code(reference), util.norm_code(vulnerable)
    removed, added = _changes(vulnerable, reference)
    pred_set = set(_lines(prediction))
    recall = (sum(l in pred_set for l in added) / len(added)) if added else None
    removal = (sum(l not in pred_set for l in removed) / len(removed)) if removed else None
    syntax = None
    if (language or "").lower() == "python":
        try:
            parse_python(textwrap.dedent(prediction or ""))
            syntax = True
        except (SyntaxError, ValueError):
            syntax = False
    plausible = (pred != vul and (removal is None or removal == 1.0)
                 and (recall is None or recall >= 0.5) and syntax is not False)
    return {
        "exact_match": float(pred == ref),
        "edit_similarity": _similarity(pred, ref),
        "fix_line_recall": recall, "vuln_line_removal": removal,
        "unchanged": float(pred == vul), "syntax_valid": syntax,
        "plausible_fix": float(plausible),
    }


def _aggregate(df: pd.DataFrame) -> dict:
    def mean(col):
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        return float(s.mean()) if len(s) else None
    syn = df.syntax_valid.dropna()
    return {
        "n": int(len(df)), "exact_match": mean("exact_match"),
        "edit_similarity": mean("edit_similarity"), "fix_line_recall": mean("fix_line_recall"),
        "vuln_line_removal": mean("vuln_line_removal"), "unchanged_rate": mean("unchanged"),
        "syntax_valid_rate": float(syn.astype(float).mean()) if len(syn) else None,
        "plausible_fix_rate": mean("plausible_fix"),
    }


def evaluate_patches(preds: pd.DataFrame, tag: str, dataset: str | pathlib.Path | None = None,
                     outdir: str = "experiments", extra: dict | None = None) -> dict:
    """preds: columns ``id`` and ``prediction``. References come from the code-pair
    JSONL (default: frozen v1, else the preview)."""
    path = pathlib.Path(dataset) if dataset else default_dataset()
    refs = {s["id"]: s for s in load_samples(path, split=None)}
    rows = []
    for r in preds.itertuples(index=False):
        s = refs.get(r.id)
        if s is None:
            continue
        pred = extract_code("" if pd.isna(r.prediction) else str(r.prediction))
        rows.append({"id": r.id, "language": s.get("language"), "cwe": s.get("cwe"),
                     "split": s.get("split"),
                     **score(s.get("vulnerable_code"), s.get("fixed_code"), pred,
                             s.get("language"))})
    if not rows:
        raise ValueError("no prediction matched a pair id in the dataset")
    df = pd.DataFrame(rows)
    metrics = {"task": "patch", "tag": tag, **_aggregate(df),
               "by_language": {l: _aggregate(g) for l, g in df.groupby("language")},
               "dataset": str(path), "prompt_version": PROMPT_VERSION,
               "unmatched_predictions": int(len(preds) - len(df)),
               "note": "plausible_fix is a static proxy; behavioural verification is the "
                       "sandbox harness (RQ5).",
               **(extra or {})}
    d = pathlib.Path(outdir) / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(json.dumps(metrics, indent=2))
    df.to_csv(d / "patch_scores.csv", index=False)
    print(f"{tag}: n={metrics['n']}  exact={_f(metrics['exact_match'])}  "
          f"edit-sim={_f(metrics['edit_similarity'])}  fix-recall={_f(metrics['fix_line_recall'])}  "
          f"vuln-removal={_f(metrics['vuln_line_removal'])}  unchanged={_f(metrics['unchanged_rate'])}  "
          f"plausible={_f(metrics['plausible_fix_rate'])}")
    print("written to", d / "metrics.json")
    return metrics


def _f(x):
    return "n/a" if x is None else f"{x:.3f}"


def write_run_config(tag: str, cfg: dict, outdir: str = "experiments") -> None:
    """Every run records what produced it."""
    d = pathlib.Path(outdir) / tag
    d.mkdir(parents=True, exist_ok=True)
    cfg = {"timestamp": datetime.datetime.utcnow().isoformat() + "Z", "task": "patch",
           "prompt_version": PROMPT_VERSION, "system_prompt": PATCH_SYSTEM,
           "user_prompt_template": build_prompt({"language": "{language}", "cwe": "{cwe}",
                                                 "explanation": "{advisory}",
                                                 "vulnerable_code": "{vulnerable_code}"}),
           **cfg}
    (d / "run_config.json").write_text(json.dumps(cfg, indent=2))
