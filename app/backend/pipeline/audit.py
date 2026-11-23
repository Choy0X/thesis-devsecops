"""Phase audit: aggregate facts about the repository and the mined data.

Everything here is computed by code from the database (read, never written) and
the source tree; no case is printed and no data is pasted into a prompt. The audit
describes what is there, not whether it is right: a row that parses has not been
semantically verified. It produces ``notes/finetuning_phase_audit.md`` (aggregates
only, committed) and a JSON copy under ``data/processed/audit/`` (gitignored).

Run it from the UI (job stage ``phase_audit``) or ``python -m app.backend.pipeline.audit``.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import importlib.metadata
import json
import os
import pathlib
import platform
import re
import shutil
import statistics
import subprocess
import sys

from app.backend.pipeline import fsio, llm_client
from app.backend.pipeline import split as splitter
from app.backend.pipeline.ingest import context as ctx

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
MODEL_PATTERNS = [
    r"Qwen/Qwen2\.5-Coder-[A-Za-z0-9.\-]+", r"microsoft/unixcoder-base", r"all-MiniLM-L6-v2",
    r"gpt-4o-mini", r"text-embedding-3-small", r"qwen2\.5-coder:[0-9a-z.\-]+",
    r"qwen3-embedding:[0-9a-z.\-]+", r"nomic-embed-text", r"qwen/qwen-2\.5-coder-32b-instruct"]
_MODEL_RE = re.compile("|".join(f"(?:{p})" for p in MODEL_PATTERNS))
SCAN_ROOTS = ("app", "training", "notes", "README.md", "CLAUDE.md",
              ".env.example", "Makefile", "data/README.md")
SCAN_EXTS = {".py", ".ts", ".tsx", ".ipynb", ".md", ".yaml", ".yml", ".txt", ".example", ""}
SKIP_PARTS = {"node_modules", "dist", "__pycache__", ".git", ".venv", "tests", "graphify-out"}
_VENDOR = re.compile(r"(^|/)(node_modules|vendor|third_party|dist|build|site-packages)(/|$)")
_GENERATED = re.compile(r"\.min\.(js|css)$|\.d\.ts$|\.pb\.go$|_pb2\.py$|\.generated\.|"
                        r"(^|/)(generated|__generated__)(/|$)|\.snap$")
_DOCS = re.compile(r"(^|/)(docs?|documentation)(/|$)|\.(md|rst|txt)$|(^|/)(README|CHANGELOG)", re.I)
_CONFIG = re.compile(r"\.(json|ya?ml|toml|ini|cfg|lock)$|(^|/)(Dockerfile|Makefile)$|(^|/)\.github/", re.I)
PROMPT_OVERHEAD_TOKENS = 256     # task/context headers of the plain serializer (an estimate)


# -- helpers ---------------------------------------------------------------------------

class Tables:
    """Stream the store's three flat row shapes through the embedded data layer's readers (nothing is written)."""
    BATCH = 1000

    def __init__(self, db=None):
        from app.backend.pipeline.db import legacy
        self.db = db if db is not None else legacy.handle()

    def _stream(self, collection, build):
        batch = []
        for doc in self.db[collection].find().sort("_id", 1):
            batch.append(doc)
            if len(batch) >= self.BATCH:
                yield from build(batch)
                batch = []
        if batch:
            yield from build(batch)

    def records(self):
        from app.backend.pipeline.db import legacy
        return self._stream("advisories", lambda docs: legacy.advisory_rows(self.db, docs))

    def commits(self):
        from app.backend.pipeline.db import legacy
        return self._stream("fix_commits", lambda docs: legacy.commit_rows(self.db, docs))

    def pairs(self, with_code: bool = False):
        from app.backend.pipeline.db import legacy
        return self._stream("code_pairs", lambda docs: legacy.pair_rows(self.db, docs, with_code=with_code))


def _nn(value, default):
    """SQL COALESCE: only a missing value (None) is replaced by the default; an empty text stays as it is."""
    return default if value is None else value


def _usable(p: dict) -> bool:
    """A pair the dataset can use: its code is available and it is neither a test nor tangled."""
    return p.get("code_available") == 1 and not (p.get("is_test") or 0) and not (p.get("tangled") or 0)


def _blank(text) -> bool:
    """SQL ``trim(x) = ''``: SQLite's trim removes spaces only, not newlines or tabs."""
    return text.strip(" ") == ""


def _quantiles(values) -> dict:
    v = sorted(values)
    if not v:
        return {"n": 0}
    pick = lambda q: v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]
    return {"n": len(v), "min": v[0], "p50": pick(0.5), "p90": pick(0.9), "p99": pick(0.99),
            "max": v[-1], "mean": round(statistics.fmean(v), 1)}


def _git_commit(root=PROJECT_ROOT) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # ignored: git may be missing; the commit is recorded as unknown
        return None


# -- repository scan ---------------------------------------------------------------------

def _source_files(repo_root: pathlib.Path):
    for entry in SCAN_ROOTS:
        base = repo_root / entry
        paths = [base] if base.is_file() else base.rglob("*") if base.is_dir() else []
        for p in paths:
            if p.is_file() and p.suffix.lower() in SCAN_EXTS and not (SKIP_PARTS & set(p.parts)):
                yield p


def repository(repo_root: pathlib.Path) -> dict:
    ids: dict[str, set] = collections.defaultdict(set)
    routes, rag_routes = 0, []
    for p in _source_files(repo_root):
        rel = p.relative_to(repo_root).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _MODEL_RE.finditer(text):
            ids[m.group(0).rstrip(".-")].add(rel)
        if re.fullmatch(r"app/backend/(\w+_)?api\.py", rel):                 # api.py and the domain routers
            for m in re.finditer(r'@(?:app|router)\.(get|post|put|delete|websocket)\("([^"]+)"', text):
                routes += 1
                if re.search(r"rag|embed", m.group(2)):
                    rag_routes.append(f"{m.group(1).upper()} {m.group(2)}")
    try:
        from app.backend import jobs
        stages = sorted(jobs.STAGES)
    except Exception as e:                               # pragma: no cover - import problems
        stages = [f"unavailable: {e}"]
    return {
        "model_ids": {k: {"files": sorted(v)[:12], "file_count": len(v)} for k, v in sorted(ids.items())},
        "job_stages": stages,
        "api_routes": routes,
        "rag_related_routes": rag_routes,
        "migration_targets": [
            "training notebooks arm_b/arm_c/arm_detect/arm_patch hard-code Qwen2.5-Coder-1.5B-Instruct "
            "(or UniXcoder) and duplicate trainer code: replaced by training/finetune and a thin notebook",
            "Engine, Settings and Overview RAG panels: marked deferred (done), code kept dormant",
            "five explanation normalizers and a duplicate 200-character constant: one resolver",
            "split.py recomputes the split on every build: persisted manifest with a leakage graph",
        ],
    }


# -- store -------------------------------------------------------------------------------

class _UnionFind:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


class _Families:
    """Independent issue families: code_pairs rows linked by CVE, advisory id, fix commit or identical code. A family is one issue however many rows or advisories it has."""

    def __init__(self):
        self.uf = _UnionFind()
        self.rows = 0
        self.row_roots: list = []
        self.hashes = collections.Counter()

    def add(self, r: dict) -> None:
        self.rows += 1
        nodes = [f"row:{r['pair_key']}"]
        if r.get("cve_id"):
            nodes.append(f"cve:{r['cve_id']}")
        if r.get("commit_key"):
            nodes.append(f"commit:{r['commit_key']}")
        if r.get("content_hash"):
            nodes.append(f"hash:{r['content_hash']}")
            self.hashes[r["content_hash"]] += 1
        try:
            for a in json.loads(r.get("advisory_ids") or "[]"):
                nodes.append(f"adv:{a}")
        except ValueError:
            pass
        for n in nodes[1:]:
            self.uf.union(nodes[0], n)
        self.row_roots.append(nodes[0])

    def result(self) -> dict:
        sizes = collections.Counter(self.uf.find(n) for n in self.row_roots)
        return {"pair_rows": self.rows, "families": len(sizes),
                "largest_family_rows": max(sizes.values(), default=0),
                "clone_groups": sum(1 for c in self.hashes.values() if c > 1),
                "rows_in_clone_groups": sum(c for c in self.hashes.values() if c > 1)}


def store_section(t: Tables) -> dict:
    kinds, ecosystems, code_status, cve_counts = (collections.Counter() for _ in range(4))
    packages: list[str] = []
    n_records = withdrawn = 0
    for r in t.records():
        n_records += 1
        osv = r.get("osv_id")
        upper = osv.upper() if isinstance(osv, str) else ""                      # SQLite's LIKE ignores the case of ASCII letters
        kinds["MAL" if upper.startswith("MAL-") else "GHSA" if upper.startswith("GHSA-") else "other"] += 1
        ecosystems[_nn(r.get("ecosystem"), "unknown")] += 1
        code_status[_nn(r.get("code_status"), "none")] += 1
        if r.get("withdrawn"):
            withdrawn += 1
        if r.get("cve_id") is not None:
            cve_counts[r["cve_id"]] += 1
        if r.get("package") is not None:
            packages.append(r["package"])
    out = {
        "status": "ok",
        "advisory_records": n_records,
        "by_advisory_kind": dict(kinds),
        "by_ecosystem": dict(ecosystems),
        "withdrawn_records": withdrawn,
        "distinct_cve_ids": len(cve_counts),
        "alias_groups_with_multiple_advisories": sum(1 for c in cve_counts.values() if c > 1),
        "records_by_code_status": dict(code_status),
        "distinct_package_fields": len(set(packages)),
        "multi_package_records": sum(1 for p in packages if "," in p),
    }
    n_commits, commit_status, shas, repos = 0, collections.Counter(), set(), set()
    for c in t.commits():
        n_commits += 1
        commit_status[_nn(c.get("status"), "none")] += 1
        if c.get("sha") is not None:
            shas.add(c["sha"])
        if c.get("repo_url") is not None:
            repos.add(c["repo_url"])
    out["fix_commits"] = {"total": n_commits, "by_status": dict(commit_status), "distinct_shas": len(shas)}
    out["distinct_repositories"] = len(repos)
    n_pairs = available = 0
    granularity, language, hashes = collections.Counter(), collections.Counter(), set()
    every, usable = _Families(), _Families()
    for p in t.pairs():
        n_pairs += 1
        granularity[_nn(p.get("granularity"), "none")] += 1
        if p.get("code_available") == 1:
            available += 1
            language[_nn(p.get("language"), "unknown")] += 1
        if p.get("content_hash") is not None:
            hashes.add(p["content_hash"])
        every.add(p)
        if _usable(p):
            usable.add(p)
    out["code_pairs"] = {"rows": n_pairs, "by_granularity": dict(granularity), "available": available, "by_language_available": dict(language),
                         "distinct_content_hashes": len(hashes)}
    out["issue_families"] = every.result()
    out["usable_issue_families"] = usable.result()
    out["levels_note"] = ("advisory records, fix commits, changed files (granularity file), function "
                          "or hunk pairs and independent issue families are different quantities: "
                          "one issue family spans many advisories, commits and rows")
    return out


# -- quality -----------------------------------------------------------------------------

def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def quality_section(t: Tables) -> dict:
    no_op = usable = empty_code = missing_parent = no_repo = no_fix = 0
    usable_by_gran, unavailable, incomplete = collections.Counter(), collections.Counter(), set()
    for p in t.pairs(with_code=True):
        before, after = p.get("code_before"), p.get("code_after")
        if p.get("code_available") == 1:
            if before is not None and after is not None and _squash(before) == _squash(after):
                no_op += 1
            if before is None or _blank(before) or after is None or _blank(after):
                empty_code += 1
            if not p.get("parent_commit"):
                missing_parent += 1
            if not p.get("repo_url"):
                no_repo += 1
            if not p.get("fix_commit"):
                no_fix += 1
        if _usable(p):
            usable += 1
            usable_by_gran[_nn(p.get("granularity"), "none")] += 1
        if p.get("code_available") == 0:
            unavailable[_nn(p.get("skip_reason"), "unknown")] += 1
            if p.get("skip_reason") in ("too_large", "vendored_or_generated", "binary") and p.get("commit_key") is not None:
                incomplete.add(p["commit_key"])
    sub = usable_by_gran.get("function", 0) + usable_by_gran.get("hunk", 0)
    unresolved, truncated, merge_like = collections.Counter(), 0, 0
    for c in t.commits():
        if c.get("status") is not None and c["status"] != "mined":
            unresolved[c["status"]] += 1
        if c.get("files_changed") is not None and c["files_changed"] >= 300:
            truncated += 1
        if isinstance(c.get("commit_message"), str) and c["commit_message"].lower().startswith("merge "):      # LIKE 'Merge %'
            merge_like += 1
    return {
        "status": "ok",
        "usable_pairs": usable,
        "usable_by_granularity": dict(usable_by_gran),
        "hunk_fallback_share_of_subfile_pairs": round(usable_by_gran.get("hunk", 0) / sub, 4) if sub else None,
        "unavailable_by_reason": dict(unavailable),
        "empty_code_on_available_rows": empty_code,
        "no_op_diffs": no_op,
        "missing_parent_commit": missing_parent,
        "missing_provenance": {"repo_url": no_repo, "fix_commit": no_fix},
        "unresolved_commits": dict(unresolved),
        "truncated_file_lists_suspected": truncated,
        "incomplete_multi_file_commits": len(incomplete),
        "merge_like_commit_messages": merge_like,
        "not_recorded": {
            "parser_failures": "not recorded: a parse failure silently falls back to a hunk pair, no reason is stored",
            "renamed_or_moved_functions": "not recorded: functions are matched by qualified name, renames are dropped, no old path is stored",
            "deleted_functions": "not recorded: only file-level removal appears (skip_reason file_removed)",
            "parent_selection": "not recorded: the first parent is always used and the parent count is not stored",
        },
    }


# -- categories --------------------------------------------------------------------------

def categories_section(t: Tables) -> dict:
    counts = collections.Counter()
    for r in t.pairs():
        path = r.get("file_path") or ""
        if r.get("is_test"):
            counts["tests"] += 1
        if r.get("tangled"):
            counts["tangled"] += 1
        if _VENDOR.search(path):
            counts["vendor"] += 1
        elif _GENERATED.search(path):
            counts["generated"] += 1
        elif _DOCS.search(path):
            counts["docs"] += 1
        elif _CONFIG.search(path):
            counts["config"] += 1
        elif r.get("code_available") and not r.get("is_test") and not r.get("tangled"):
            counts["production_candidates"] += 1
    keys = ("tests", "tangled", "vendor", "generated", "docs", "config", "production_candidates")
    out = {k: counts.get(k, 0) for k in keys}
    out["how_used"] = {
        "tests": "flagged is_test, kept in the store, excluded from the dataset build and the detection set by default",
        "tangled": "flagged when a commit changes more than 20 functions (test functions are wrongly counted); excluded by default",
        "vendor": "vendored or generated paths are skipped at mining (skip_reason); docs directories are treated the same way",
        "generated": "minified and declaration files are only partly recognised by name",
        "docs": "documentation files are not mined as pairs (source extensions only)",
        "config": "configuration files are never mined (extension filter): security-relevant config changes are lost",
        "production_candidates": "available, not test, not tangled, not docs, config, vendor or generated by path",
    }
    out["note"] = "path categories are heuristics on file paths; the 'how used' lines describe the pipeline before the planned fixes"
    return out


# -- distributions -----------------------------------------------------------------------

def _mined(t: Tables) -> list[dict]:
    """The mined, non-withdrawn advisory records (the basis of the advisory-level distributions)."""
    return [r for r in t.records() if r.get("code_status") == "mined" and not r.get("withdrawn")]


def distributions_section(t: Tables) -> dict:
    recs = _mined(t)
    # explanation source by the 200-character rule (context.MIN_ADVISORY)
    src = collections.Counter()
    for r in recs:
        n = len(ctx.advisory_text(r.get("summary"), r.get("details")))
        src["advisory" if n >= ctx.MIN_ADVISORY else "short_advisory" if n else "missing"] += 1
    cwe_counts, missing = collections.Counter(), 0
    for r in recs:
        toks = [x.upper() for x in re.split(r"[;,\s]+", r.get("osv_cwe") or "") if x.upper().startswith("CWE-")]
        if not toks:
            missing += 1
        cwe_counts.update(set(toks))
    years = collections.Counter((r.get("published") or "")[:4] or "unknown" for r in recs)
    sev = collections.Counter((r.get("osv_severity") or "missing") for r in recs)
    cvss = sum(1 for r in recs if r.get("cvss_vector") or r.get("cvss_v4_vector"))
    fix_check, commit_years = {}, collections.Counter()
    for c in t.commits():
        fix_check[c["commit_key"]] = c.get("fix_check")
        if c.get("commit_date") is not None:
            commit_years[c["commit_date"][:4]] += 1
    legacy_llm, lens, line_counts = 0, [], []
    languages, repos, grade = collections.Counter(), collections.Counter(), collections.Counter()
    for p in t.pairs(with_code=True):
        draft = p.get("explanation_llm")
        if draft is not None and not _blank(draft):
            legacy_llm += 1
        if _usable(p):
            languages[_nn(p.get("language"), "unknown")] += 1
            repos[p.get("repo_url")] += 1
            grade[_nn(fix_check.get(p.get("commit_key")), "unchecked")] += 1
            before = p.get("code_before")
            if before is not None:
                lens.append(len(before))
                line_counts.append(before.count("\n") + 1)
    explanation = {"boundary": ctx.MIN_ADVISORY, "advisory": src["advisory"],
                   "short_advisory": src["short_advisory"], "missing": src["missing"],
                   "legacy_pair_rows_with_explanation_llm": legacy_llm,
                   "note": "measured on mined, non-withdrawn records; the single canonical field and its source "
                           "column come with the schema migration"}
    ranked = sorted(repos.items(), key=lambda kv: (-kv[1], "" if kv[0] is None else str(kv[0])))      # most pairs first; equal counts by name, so the order is the same on every run
    total = sum(c for _, c in ranked) or 1
    return {
        "status": "ok",
        "basis": "mined, non-withdrawn records (advisory-level) and usable pairs (code-level)",
        "explanation_source": explanation,
        "cwe": {"top": dict(cwe_counts.most_common(15)), "distinct": len(cwe_counts), "missing": missing,
                "note": "missing taxonomy is kept as missing, not mapped to a class"},
        "language": dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))),
        "ecosystem": dict(collections.Counter(_nn(r.get("ecosystem"), "unknown") for r in recs)),
        "project_concentration": {
            "repositories": len(ranked),
            "top1_share": round(ranked[0][1] / total, 4) if ranked else None,
            "top10_share": round(sum(c for _, c in ranked[:10]) / total, 4),
            "top10": [{"repo": repo, "pairs": c} for repo, c in ranked[:10]],
        },
        "published_year": dict(sorted(years.items())),
        "commit_year": dict(sorted(commit_years.items())),
        "context_length_chars": _quantiles(lens),
        "context_length_lines": _quantiles(line_counts),
        "severity_osv": dict(sev),
        "cvss_vector_present": cvss,
        "mined_records": len(recs),
        "evidence_grade": {
            "G0_verified_link": grade.get("verified", 0), "G0_suspect_link": grade.get("suspect", 0),
            "G0_unknown_link": grade.get("unknown", 0) + grade.get("unchecked", 0),
            "G1_reviewed": 0, "G2_reproduced": 0, "G3_behaviour_preserved": 0,
            "note": "G0 is the release-date plausibility check of the fix link only; no review decision or "
                    "execution receipt exists yet, so G1 to G3 are zero"},
    }


# -- split and contamination -------------------------------------------------------------

def split_section(t: Tables, root: pathlib.Path) -> dict:
    import pandas as pd
    recs = [(r.get("cve_id"), r.get("osv_id"), r.get("ecosystem"), r.get("package")) for r in _mined(t)]
    df = pd.DataFrame(recs, columns=["cve_id", "osv_id", "ecosystem", "package"])
    out = {"status": "ok", "rule": splitter.RULE, "val_share_target": splitter.VAL_SHARE}
    if len(df):
        df["split"] = splitter.assign(df)
        group = df["cve_id"].where(df["cve_id"].notna(), df["osv_id"])
        per_group = df.groupby(group)["split"].first()
        out.update(groups=int(len(per_group)), val_groups=int((per_group == "val").sum()),
                   val_share_of_groups=round(float((per_group == "val").mean()), 4),
                   val_share_of_records=round(float((df["split"] == "val").mean()), 4))
    else:
        out.update(groups=0, val_groups=0, val_share_of_groups=0.0, val_share_of_records=0.0)
    pairs = [(p.get("cve_id"), p.get("ghsa_id"), p.get("ecosystem"), p.get("package"), p.get("repo_url")) for p in t.pairs() if _usable(p)]
    pdf = pd.DataFrame(pairs, columns=["cve_id", "osv_id", "ecosystem", "package", "repo_url"])
    if len(pdf):
        pdf["split"] = splitter.assign(pdf)
        both = pdf.groupby("repo_url")["split"].nunique()
        out["repositories_spanning_train_and_val"] = int((both > 1).sum())
        out["usable_pairs_val_share"] = round(float((pdf["split"] == "val").mean()), 4)
        val_pkgs = pdf[pdf["split"] == "val"].groupby("package").size().sort_values(ascending=False)
        out["largest_val_package_share"] = round(float(val_pkgs.iloc[0] / val_pkgs.sum()), 4) if len(val_pkgs) else None
    else:
        out.update(repositories_spanning_train_and_val=0, usable_pairs_val_share=0.0, largest_val_package_share=None)
    out["unmodelled"] = ("the split is recomputed on every build and no manifest is persisted; near-clones, "
                         "forks and fix families across packages are not checked")
    runs = [p for p in (root / "experiments").glob("*/run_config.json")] if (root / "experiments").exists() else []
    trained = 0
    for p in runs:
        try:
            cfg = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if any(k in cfg for k in ("best_step", "adapter", "final_steps")):
            trained += 1
    out["model_exposure"] = {
        "run_records": len(runs), "trained_checkpoints_recorded": trained,
        "statement": ("No model has been trained on this store (no trained checkpoint is recorded), so no "
                      "validation observation is contaminated by model exposure." if not trained else
                      f"{trained} trained run(s) are recorded; whether they saw candidate validation families "
                      "cannot be told from the store and must be checked against their run_config.")}
    return out


# -- tokens ------------------------------------------------------------------------------

def tokens_section(t: Tables, tokenizer=None, info=None, network=True, cfg=None) -> dict:
    if tokenizer is None and network:
        try:
            from training.finetune import hub
            model_id = (cfg.model.id if cfg else "Qwen/Qwen2.5-Coder-7B")
            rev = hub.resolve_revision(model_id)
            tokenizer, info = hub.load_tokenizer(model_id, rev), {"model": model_id, "revision": rev}
        except Exception as e:
            return {"status": "unavailable", "reason": f"could not load the pinned tokenizer: {type(e).__name__}: {e}"}
    if tokenizer is None:
        return {"status": "unavailable", "reason": "no tokenizer supplied and network access is disabled"}
    total = cfg.context.max_total_tokens if cfg else 4096
    ordinary = cfg.context.ordinary_completion_reserve if cfg else 1024
    repair = cfg.context.repair_completion_reserve if cfg else 1536
    chosen = sorted(((p["pair_key"], p["code_before"], p["code_after"], _nn(p.get("language"), "unknown")) for p in t.pairs(with_code=True)
                     if _usable(p) and p.get("code_before") is not None and p.get("code_after") is not None), key=lambda row: row[0])
    keys, before, after, langs = ([row[i] for row in chosen] for i in range(4))

    def count(texts):
        out = []
        for i in range(0, len(texts), 256):
            enc = tokenizer(texts[i:i + 256], add_special_tokens=False, return_attention_mask=False)["input_ids"]
            out += [len(x) for x in enc]
        return out

    nb, na = count(before), count(after)
    n = len(nb) or 1
    over_disc = [b + PROMPT_OVERHEAD_TOKENS > total - ordinary for b in nb]
    over_rep_in = [b + PROMPT_OVERHEAD_TOKENS > total - repair for b in nb]
    over_rep_out = [a > repair for a in na]
    by_lang = {}
    for lang in sorted(set(langs)):
        idx = [i for i, l in enumerate(langs) if l == lang]
        by_lang[lang] = {"pairs": len(idx), "over_discovery_budget": round(sum(over_disc[i] for i in idx) / len(idx), 4)}
    return {
        "status": "ok", "tokenizer": info or {}, "pairs_measured": len(nb),
        "budget": {"max_total_tokens": total, "ordinary_completion_reserve": ordinary,
                   "repair_completion_reserve": repair, "prompt_overhead_estimate": PROMPT_OVERHEAD_TOKENS},
        "vulnerable_code_tokens": _quantiles(nb), "fixed_code_tokens": _quantiles(na),
        "over_prompt_budget": {"discovery": round(sum(over_disc) / n, 4),
                               "repair_input": round(sum(over_rep_in) / n, 4),
                               "repair_output_code": round(sum(over_rep_out) / n, 4)},
        "over_prompt_budget_by_language": by_lang,
        "supervised_tokens_by_task": "not available before the first reviewed dataset build: views do not exist yet",
        "note": "estimates from the code alone; the real budget also holds context, instruction and the response schema",
    }


# -- environment -------------------------------------------------------------------------

PACKAGES = ("torch", "transformers", "peft", "accelerate", "datasets", "trl", "bitsandbytes", "safetensors",
            "tokenizers", "pyarrow", "pandas", "numpy", "tree-sitter", "tree-sitter-language-pack",
            "fastapi", "pydantic", "pytest", "huggingface-hub", "packaging")


def _version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _gpu() -> dict:
    """The machine's GPU. CUDA is asked of the interpreter that runs the GPU jobs (``compute.gpu_info``), not of the app's own
    interpreter, which usually has no CUDA torch; the report names the interpreter it asked."""
    from app.backend import compute
    out = {"nvidia_smi": None, "cuda_available": False, "torch_cuda_build": None, "interpreter": None, "reason": None}
    try:
        r = compute.run_text(["nvidia-smi", "--query-gpu=name,memory.total,compute_cap,driver_version", "--format=csv,noheader"], timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            out["nvidia_smi"] = [x.strip() for x in r.stdout.strip().splitlines()]
    except Exception:  # ignored: nvidia-smi is optional
        pass
    try:
        info = compute.gpu_info()
        out["interpreter"] = info.get("python")
        out["cuda_available"] = bool(info.get("available"))
        out["torch_cuda_build"] = info.get("cuda_build")
        if not info.get("available"):
            out["reason"] = info.get("reason")
    except Exception as e:  # ignored: the probe is optional; the report says why it has no answer
        out["reason"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def _github_rate(network: bool):
    token = os.getenv("GITHUB_TOKEN")
    if not (network and token):
        return None
    try:
        import requests
        r = requests.get("https://api.github.com/rate_limit", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        core = r.json().get("resources", {}).get("core", {})
        return {k: core.get(k) for k in ("limit", "remaining", "reset")}
    except Exception:  # ignored: the GitHub rate limit is informational
        return None


def _database_mb():
    """The size of the application database (the data of its collections), or None when the server cannot say."""
    try:
        from app.backend.pipeline.db import legacy
        return round(legacy.handle().command("dbStats")["dataSize"] / 1e6, 1)
    except Exception:  # ignored: the size is informational, the audit does not depend on it
        return None


def environment_section(root: pathlib.Path, network: bool, cfg=None) -> dict:
    from app.backend.pipeline import models_local
    hf = models_local.hub_cache_dir()
    cached = sorted(p.name for p in hf.glob("models--Qwen*")) if hf.exists() else []
    usage = shutil.disk_usage(root if root.exists() else pathlib.Path("."))
    return {
        "python": platform.python_version(), "platform": platform.platform(),
        "packages": {p: _version(p) for p in PACKAGES},
        "gpu": _gpu(),
        "disk": {"free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1),
                 "database_mb": _database_mb()},
        "huggingface_cache_qwen_models": cached,
        "external_calls": {
            "teacher_enabled": bool(cfg.mining.teacher_enabled) if cfg else False,
            "teacher_budget": cfg.mining.teacher_budget if cfg else None,
            "llm_endpoint_configured": llm_client.resolved().configured,
            "github_token_configured": bool(os.getenv("GITHUB_TOKEN")),
            "nvd_key_configured": bool(os.getenv("NVD_API_KEY")),
            "github_rate_limit": _github_rate(network),
        },
    }


# -- assemble, render, run ---------------------------------------------------------------

def collect(root: pathlib.Path = pathlib.Path("."), network: bool = True,
            tokenizer=None, tokenizer_info=None, repo_root: pathlib.Path | None = None) -> dict:
    from app.backend.pipeline.db import database
    root, repo_root = pathlib.Path(root), pathlib.Path(repo_root or PROJECT_ROOT)
    try:
        from app.backend import project_config
        cfg = project_config.config()
    except Exception:  # ignored: an unreadable config is reported as missing in the audit
        cfg = None
    out = {"meta": {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    "git_commit": _git_commit(repo_root),
                    "store": f"SQLite database {database.db_name()} ({database.file_path()})"},
           "repository": repository(repo_root)}
    try:
        tables = Tables()
    except database.DatabaseUnavailable as e:
        missing = {"status": "missing", "reason": str(e)}
        out.update(store=missing, quality=missing, categories=missing, distributions=missing, split=missing,
                   tokens={"status": "unavailable", "reason": "no database"})
    else:
        out["store"] = store_section(tables)
        out["quality"] = quality_section(tables)
        out["categories"] = categories_section(tables)
        out["distributions"] = distributions_section(tables)
        out["split"] = split_section(tables, root)
        out["tokens"] = tokens_section(tables, tokenizer, tokenizer_info, network, cfg)
    out["environment"] = environment_section(root, network, cfg)
    return out


def _kv(d: dict, keys=None) -> str:
    keys = keys or list(d)
    return "\n".join(f"| {k} | {d[k]} |" for k in keys if not isinstance(d.get(k), (dict, list)))


def _table(title: str, mapping: dict, head=("value", "count")) -> str:
    if not mapping:
        return f"\n_{title}: none_\n"
    rows = "\n".join(f"| {k} | {v} |" for k, v in mapping.items())
    return f"\n**{title}**\n\n| {head[0]} | {head[1]} |\n|---|---|\n{rows}\n"


def render(a: dict) -> str:
    m, repo, st, q, c, d, sp, tk, env = (a[k] for k in (
        "meta", "repository", "store", "quality", "categories", "distributions", "split", "tokens", "environment"))
    L = [f"# Fine-tuning phase audit\n",
         f"Generated {m['generated_at']} at commit `{m['git_commit']}` from `{m['store']}`. "
         "Every figure is an aggregate computed by code from the database, which the audit only reads. "
         "None of it was semantically verified: a row that parses, links to a commit and has code is "
         "not thereby a correct vulnerability example.\n"]

    L.append("## 1. Repository and entry points\n")
    L.append(f"Job stages ({len(repo['job_stages'])}): {', '.join(repo['job_stages'])}.\n")
    L.append(f"API routes (api.py and the routers): {repo['api_routes']}; RAG-related: {', '.join(repo['rag_related_routes']) or 'none'}.\n")
    L.append("| model or embedding id referenced | files |\n|---|---|")
    for k, v in repo["model_ids"].items():
        L.append(f"| `{k}` | {v['file_count']} |")
    L.append("")
    L.append("Migration targets:\n" + "\n".join(f"- {t}" for t in repo["migration_targets"]) + "\n")

    L.append("## 2. Dataset levels\n")
    if st.get("status") != "ok":
        L.append(f"Store {st.get('status')}: {st.get('reason', '')}\n")
    else:
        L.append(f"{st['levels_note']}.\n")
        L.append("| quantity | count |\n|---|---|")
        for k in ("advisory_records", "withdrawn_records", "distinct_cve_ids",
                  "alias_groups_with_multiple_advisories", "distinct_package_fields", "multi_package_records",
                  "distinct_repositories"):
            L.append(f"| {k.replace('_', ' ')} | {st[k]} |")
        L.append(f"| fix commits queued | {st['fix_commits']['total']} |")
        L.append(f"| code pair rows | {st['code_pairs']['rows']} |")
        L.append(f"| code pairs with code available | {st['code_pairs']['available']} |")
        L.append(f"| independent issue families (all rows) | {st['issue_families']['families']} |")
        L.append(f"| independent issue families (usable rows) | {st['usable_issue_families']['families']} |")
        L.append(f"| clone groups (identical code) | {st['issue_families']['clone_groups']} |")
        L.append(_table("advisory kinds", st["by_advisory_kind"]))
        L.append(_table("ecosystems", st["by_ecosystem"]))
        L.append(_table("records by code status", st["records_by_code_status"]))
        L.append(_table("fix commits by status", st["fix_commits"]["by_status"]))
        L.append(_table("pairs by granularity", st["code_pairs"]["by_granularity"]))
        L.append(_table("pairs with code by language", st["code_pairs"]["by_language_available"]))

    L.append("\n## 3. Quality findings\n")
    if q.get("status") == "ok":
        L.append("| finding | count |\n|---|---|")
        for k in ("usable_pairs", "empty_code_on_available_rows", "no_op_diffs", "missing_parent_commit",
                  "truncated_file_lists_suspected", "incomplete_multi_file_commits", "merge_like_commit_messages",
                  "hunk_fallback_share_of_subfile_pairs"):
            L.append(f"| {k.replace('_', ' ')} | {q[k]} |")
        L.append(f"| missing provenance | {q['missing_provenance']} |")
        L.append(_table("unavailable pairs by reason", q["unavailable_by_reason"]))
        L.append(_table("commits not mined, by status", q["unresolved_commits"]))
        L.append("\nNot recorded by the current pipeline (so not measurable):\n" +
                 "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in q["not_recorded"].items()) + "\n")
    else:
        L.append("No store.\n")

    L.append("## 4. Categories\n")
    if c.get("status") != "missing":
        L.append("| category | pair rows |\n|---|---|")
        for k in ("tests", "tangled", "vendor", "generated", "docs", "config", "production_candidates"):
            L.append(f"| {k.replace('_', ' ')} | {c[k]} |")
        L.append("\n" + "\n".join(f"- **{k}**: {v}" for k, v in c["how_used"].items()) + f"\n\n{c['note']}.\n")

    L.append("## 5. Distributions\n")
    if d.get("status") == "ok":
        e = d["explanation_source"]
        L.append(f"Basis: {d['basis']}. Mined records: {d['mined_records']}; CVSS vector present on {d['cvss_vector_present']}.\n")
        L.append(f"**Explanation source** (boundary {e['boundary']} characters): advisory {e['advisory']}, "
                 f"short advisory {e['short_advisory']}, missing {e['missing']}; legacy pair rows holding "
                 f"`explanation_llm`: {e['legacy_pair_rows_with_explanation_llm']}.\n")
        L.append(_table("CWE (top 15; " + d["cwe"]["note"] + f"; missing {d['cwe']['missing']}, distinct {d['cwe']['distinct']})", d["cwe"]["top"]))
        L.append(_table("language (usable pairs)", d["language"]))
        L.append(_table("ecosystem (mined records)", d["ecosystem"]))
        pc = d["project_concentration"]
        L.append(f"\n**Project concentration**: {pc['repositories']} repositories; top-1 share {pc['top1_share']}, "
                 f"top-10 share {pc['top10_share']}.\n")
        L.append(_table("top repositories (usable pairs)", {r["repo"]: r["pairs"] for r in pc["top10"]}, ("repository", "pairs")))
        L.append(_table("advisory publication year", d["published_year"]))
        L.append(_table("OSV severity (qualitative; missing kept as missing)", d["severity_osv"]))
        L.append(f"\n**Context length** (usable pairs, vulnerable side): chars {d['context_length_chars']}; lines {d['context_length_lines']}.\n")
        g = d["evidence_grade"]
        L.append("**Evidence grade**: " + ", ".join(f"{k} {v}" for k, v in g.items() if k != "note") + f". {g['note']}.\n")

    L.append("## 6. Splits and contamination\n")
    if sp.get("status") == "ok":
        L.append(f"Rule: `{sp['rule']}`.\n")
        L.append("| measure | value |\n|---|---|")
        for k in ("groups", "val_groups", "val_share_of_groups", "val_share_of_records",
                  "repositories_spanning_train_and_val", "usable_pairs_val_share", "largest_val_package_share"):
            L.append(f"| {k.replace('_', ' ')} | {sp.get(k)} |")
        L.append(f"\n{sp['unmodelled']}.\n")
        L.append(f"**Model exposure**: {sp['model_exposure']['statement']}\n")

    L.append("## 7. Token statistics (exact pinned tokenizer)\n")
    if tk.get("status") == "ok":
        L.append(f"Tokenizer: {tk['tokenizer']}. Pairs measured: {tk['pairs_measured']}. Budget: {tk['budget']}.\n")
        L.append(f"- vulnerable code tokens: {tk['vulnerable_code_tokens']}\n- fixed code tokens: {tk['fixed_code_tokens']}\n"
                 f"- share over the prompt budget: {tk['over_prompt_budget']}\n- {tk['supervised_tokens_by_task']}\n- {tk['note']}\n")
        L.append(_table("share over the discovery budget by language", {k: v["over_discovery_budget"] for k, v in tk["over_prompt_budget_by_language"].items()}, ("language", "share")))
    else:
        L.append(f"Unavailable: {tk.get('reason')}.\n")

    L.append("## 8. Environment and budgets\n")
    L.append(f"Python {env['python']} on {env['platform']}. Disk free {env['disk']['free_gb']} of {env['disk']['total_gb']} GB; "
             f"database {env['disk']['database_mb']} MB.\n")
    L.append(f"GPU: {env['gpu']}. Hugging Face cache (Qwen models): {env['huggingface_cache_qwen_models'] or 'none'}.\n")
    L.append("| package | version |\n|---|---|")
    for k, v in env["packages"].items():
        L.append(f"| {k} | {v or 'not installed'} |")
    L.append(f"\nExternal-call budgets: {env['external_calls']}.\n")

    L.append("## 9. What this audit does not establish\n")
    L.append("- Whether any label is correct: no row has been reviewed or executed. Parsing, a verified fix link "
             "and a present advisory only show that the record is complete.\n"
             "- Whether a changed function is the vulnerable one: the pipeline labels every changed function of a "
             "fix commit as vulnerable.\n"
             "- Contamination by pretraining: the base model's training data is not known.\n"
             "- Supervised token counts by task and family: they need the view builder.\n")
    return "\n".join(L)


def run(out_md="notes/finetuning_phase_audit.md", out_json=None, root=pathlib.Path("."), network=True) -> dict:
    a = collect(root, network)
    out_md = fsio.write_text(out_md, render(a))
    stamp = a["meta"]["generated_at"].replace(":", "").replace("-", "")[:15]
    out_json = pathlib.Path(out_json or pathlib.Path(root) / "data" / "processed" / "audit" / f"audit_{stamp}.json")
    fsio.write_json(out_json, a, indent=2, default=str)
    print(f"audit written: {out_md} and {out_json}")
    return a


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-network", action="store_true", help="skip the tokenizer download and the GitHub quota call")
    ap.add_argument("--out", default="notes/finetuning_phase_audit.md")
    a = ap.parse_args()
    from app.backend import config  # noqa: F401  (overlays the saved settings onto the environment)
    run(out_md=a.out, network=not a.no_network)
