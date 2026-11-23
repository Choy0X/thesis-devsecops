"""The vulnerable-code detection set (``code_pairs_<tag>_detection.jsonl``).

A binary detector (vulnerable = 1, safe = 0) that only ever sees a vulnerable
function and its fixed twin learns "code that looks like a fix site is
vulnerable" and flags safe code. This module writes a set built to avoid that:

* **vulnerable** - the code before the fix (label 1);
* **fixed** - the same function after the fix (label 0): the hard negative, it
  differs from the vulnerable sample only by the fix. It is a candidate patch,
  not a function proven secure;
* **untouched** - functions in the same fixed file that the fix did not change
  (label 0), only with ``untouched_policy = "weak_negative"``. Not changing in a
  fix is no evidence of being secure (the file may hold a vulnerability the fix
  did not reach), so by default (``"exclude"``) these functions are not in the set
  at all. When they are asked for they carry ``label_verified = false`` and
  ``label_basis = "not_changed_by_this_fix"``, and a consumer can weight or drop
  them. Taken from the stored whole-file pair, so nothing is downloaded.

Every row says what its label rests on (``label_basis``) and none claims to be
verified. ``safe_ratio`` sets the class balance only when untouched functions are
asked for: safe samples per vulnerable sample (2 = 1 vulnerable : 2 safe). The fixed
twin counts as one, so each file adds about ``safe_ratio - 1`` untouched functions
per vulnerable sample in it, chosen deterministically.

Test-file pairs and tangled commits never enter the set, whatever the build
settings: their "vulnerable" side is test code or unrelated refactoring.

Inputs are cleaned the same way for every row: trailing spaces, common
indentation and runs of blank lines are removed, and a leading licence header
is dropped. A pair whose vulnerable or fixed code is longer than ``max_chars``
is skipped (counted, never truncated mid-code). Rows are then de-duplicated on
whitespace-insensitive code:

* a pair whose two sides differ only in whitespace is dropped (nothing to learn);
* code that is vulnerable somewhere is never also labelled safe (the safe row
  is dropped);
* identical code in train and val keeps only the train row (leak guard).
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import textwrap

from app.backend.pipeline import codestore, funcsplit

DEFAULTS = {"safe_ratio": 2.0, "max_chars": 8000, "untouched_min_lines": 3, "untouched_policy": "exclude"}
UNTOUCHED_POLICIES = ("exclude", "weak_negative")
LABEL_BASIS = {"vulnerable": "pre_fix_version_of_a_changed_function", "fixed": "post_fix_version_of_a_changed_function", "untouched": "not_changed_by_this_fix"}
BASE = ("cve", "cwe", "severity", "language", "file", "function", "granularity",
        "fix_check", "context", "split", "date")
_LICENSE = re.compile(r"licen[cs]e|copyright|spdx-license-identifier|all rights reserved", re.I)
_HEADER = re.compile(r"\A\s*(?:/\*.*?\*/|(?:(?:#|//)[^\n]*\n)+|\"\"\".*?\"\"\"|'''.*?''')\s*", re.S)


def clean_code(code: str | None) -> str:
    if not code:
        return ""
    m = _HEADER.match(code)
    if m and _LICENSE.search(m.group(0)):
        code = code[m.end():]
    lines = [l.rstrip() for l in code.expandtabs(4).splitlines()]
    out, blank = [], 0
    for l in lines:
        blank = blank + 1 if not l else 0
        if blank <= 1:
            out.append(l)
    return textwrap.dedent("\n".join(out)).strip("\n")


def code_key(code: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", "", code).encode("utf-8")).hexdigest()


def _pick(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _file_code(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """The stored whole-file pair of each (commit_key, file_path)."""
    out: dict[tuple[str, str], dict] = {}
    wanted = set(keys)
    for r in codestore.file_pairs_of_commits(sorted({c for c, _ in keys})):
        k = (r["commit_key"], r["file_path"])
        if k in wanted and r["code_after"] and k not in out:
            out[k] = r
    return out


def _untouched(samples: list[dict], where: dict[str, tuple[str, str]], opts: dict,
               counts: dict) -> list[dict]:
    """Untouched functions per file, up to the file's share of the ratio."""
    extra = max(0.0, float(opts["safe_ratio"]) - 1.0)
    if not extra or opts.get("untouched_policy") != "weak_negative":
        return []
    by_file: dict[tuple[str, str], list[dict]] = {}
    for s in samples:
        k = where.get(s["id"])
        if k and s.get("granularity") != "file":
            by_file.setdefault(k, []).append(s)
    files = _file_code(list(by_file))
    rows = []
    for k, group in by_file.items():
        f = files.get(k)
        if not f:
            counts["files_without_stored_file"] += 1
            continue
        funcs = funcsplit.unchanged_functions(f["language"] or group[0]["language"] or "", k[1],
                                              f["code_before"] or "", f["code_after"],
                                              int(opts["untouched_min_lines"]))
        if not funcs:
            continue
        quota = math.ceil(extra * len(group))
        first = sorted(group, key=lambda s: s["id"])[0]
        split = "train" if any(s["split"] == "train" for s in group) else first["split"]
        funcs.sort(key=lambda qc: _pick(f"{k[0]}:{k[1]}:{qc[0]}"))
        for q, code in funcs[:quota]:
            rows.append({"id": f"{k[0]}:{k[1]}:{q}#untouched", "pair_id": first["id"],
                         "code": code, "label": 0, "kind": "untouched", "label_basis": LABEL_BASIS["untouched"], "label_verified": False,
                         **{b: first.get(b) for b in BASE},
                         "function": q, "granularity": "function", "split": split})
    return rows


def build(samples: list[dict], where: dict[str, tuple[str, str]], opts: dict | None = None,
          exclude: set[str] | None = None) -> tuple[list[dict], dict]:
    """Detection rows for the built code-pair samples.

    ``where`` maps a sample id to its (commit_key, file_path); ``exclude`` holds
    sample ids that never enter the set (test-file and tangled pairs, whose
    "vulnerable" side is not vulnerable code)."""
    o = {**DEFAULTS, **{k: v for k, v in (opts or {}).items() if k in DEFAULTS and v is not None}}
    if o["untouched_policy"] not in UNTOUCHED_POLICIES:
        raise ValueError(f"untouched_policy must be one of {UNTOUCHED_POLICIES}, not {o['untouched_policy']!r}")
    excluded = [s for s in samples if s["id"] in (exclude or ())]
    samples = [s for s in samples if s["id"] not in (exclude or ())]
    counts = {"pairs": len(samples), "test_or_tangled_pairs": len(excluded), "over_budget_pairs": 0, "whitespace_only_pairs": 0,
              "files_without_stored_file": 0, "untouched_candidates": 0,
              "untouched_over_budget": 0, "safe_dropped_vulnerable_elsewhere": 0,
              "duplicates_dropped": 0, "val_dropped_in_train": 0}
    max_chars = int(o["max_chars"]) or 10 ** 9
    rows = []
    for s in samples:
        v, fx = clean_code(s["vulnerable_code"]), clean_code(s["fixed_code"])
        if len(v) > max_chars or len(fx) > max_chars:
            counts["over_budget_pairs"] += 1
            continue
        if code_key(v) == code_key(fx):
            counts["whitespace_only_pairs"] += 1
            continue
        base = {b: s.get(b) for b in BASE}
        rows.append({"id": f"{s['id']}#vuln", "pair_id": s["id"], "code": v, "label": 1,
                     "kind": "vulnerable", "label_basis": LABEL_BASIS["vulnerable"], "label_verified": False, **base})
        rows.append({"id": f"{s['id']}#fixed", "pair_id": s["id"], "code": fx, "label": 0,
                     "kind": "fixed", "label_basis": LABEL_BASIS["fixed"], "label_verified": False, **base})
    kept_ids = {r["pair_id"] for r in rows}
    unt = (_untouched([s for s in samples if s["id"] in kept_ids], where, o, counts) if o["untouched_policy"] == "weak_negative" else [])
    counts["untouched_candidates"] = len(unt)
    for r in unt:
        r["code"] = clean_code(r["code"])
        if len(r["code"]) > max_chars:
            counts["untouched_over_budget"] += 1
            continue
        rows.append(r)

    # de-duplicate: vulnerable wins over safe; train wins over val
    vuln_keys = {code_key(r["code"]) for r in rows if r["label"] == 1}
    seen: dict[tuple[str, int], str] = {}
    out = []
    for r in sorted(rows, key=lambda r: (r["split"] != "train", -r["label"])):
        key = code_key(r["code"])
        if r["label"] == 0 and key in vuln_keys:
            counts["safe_dropped_vulnerable_elsewhere"] += 1
            continue
        prev = seen.get((key, r["label"]))
        if prev is not None:
            counts["val_dropped_in_train" if prev == "train" and r["split"] != "train"
                   else "duplicates_dropped"] += 1
            continue
        seen[(key, r["label"])] = r["split"]
        out.append(r)
    out.sort(key=lambda r: r["id"])
    n_pos = sum(r["label"] == 1 for r in out)
    kinds: dict[str, int] = {}
    for r in out:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    stats = {"rows": len(out), "vulnerable": n_pos, "safe": len(out) - n_pos,
             "safe_per_vulnerable": round((len(out) - n_pos) / n_pos, 2) if n_pos else None,
             "by_kind": kinds,
             "by_split": {sp: sum(r["split"] == sp for r in out) for sp in sorted({r["split"] for r in out})},
             "settings": o, "funnel": counts}
    return out, stats


def write(rows: list[dict], path) -> None:
    from app.backend.pipeline import fsio

    def lines(part):
        with open(part, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    fsio.write_with(path, lines)


def describe(stats: dict) -> str:
    f = stats["funnel"]
    policy = stats["settings"].get("untouched_policy", "exclude")
    return (f"detection set: {stats['rows']:,} rows = {stats['vulnerable']:,} vulnerable + "
            f"{stats['safe']:,} safe ({stats['by_kind']}); safe per vulnerable "
            f"{stats['safe_per_vulnerable']}; untouched functions: "
            f"{'weak, unverified negatives' if policy == 'weak_negative' else 'left out (no evidence they are secure)'}; skipped: {f['test_or_tangled_pairs']} test-file / "
            f"tangled pairs (never in this set), {f['over_budget_pairs']} pairs over "
            f"{stats['settings']['max_chars']} chars, {f['whitespace_only_pairs']} whitespace-only "
            f"pairs, {f['safe_dropped_vulnerable_elsewhere']} safe rows whose code is vulnerable "
            f"elsewhere, {f['duplicates_dropped']} duplicates, {f['val_dropped_in_train']} val rows "
            f"already in train")
