"""Optional LLM enrichment of code pairs: root cause, secure pattern, explanation.

The *real* explanation of every pair comes from the advisory text and the commit
message. This job adds a model-drafted, structured description in **separate**
``*_llm`` columns, recorded with the model name and prompt version, and exported
in ``synthetic_fields`` so it is always distinguishable from source data. It is
usable as training text but is never used as evaluation ground truth.

Targeting: the candidates are the rows a dataset build would contain
(``build_code_dataset.select`` + ``split``: the record's language, no suspect fix
commits, function / hunk level preferred, de-duplicated), rated by how much real
context they carry (``context.py``: weak / partial / strong and the missing
signals), so the LLM is spent where the real text is thin.

Prompt ``enrich-v3`` gives the model the record's advisory (summary + details),
the commit message, CWE, severity and the location, the diff and - when the
advisory is not full - the vulnerable code itself. A draft that says nothing (an empty field, a secure pattern that is only
a function name, a root cause copied from the advisory title, one sentence in every field) is asked
for once more with the reason in the request, and refused if it is still empty (check_draft).
"""
from __future__ import annotations
import json
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

import pandas as pd

from app.backend.pipeline import codestore, llm_client, store
from app.backend.pipeline.ingest import build_code_dataset as bcd
from app.backend.pipeline.ingest import context as ctx

PROMPT_VERSION = "enrich-v3"
MAX_TOKENS = 600             # the first request
LONG_MAX_TOKENS = 1500       # when the answer was cut off at MAX_TOKENS
from app.backend.pipeline.ingest.draft_quality import FIELDS, MIN_WORDS, SAME_TEXT, check_draft
SYSTEM = (
    "You are a secure-code reviewer. You are given a vulnerability advisory and the "
    "code change (unified diff) that fixed it. Using only this material, explain the "
    "weakness. Respond with JSON only: {\"root_cause\": \"...\", \"secure_pattern\": "
    "\"...\", \"explanation\": \"...\"}. root_cause: the programming mistake in the "
    "vulnerable code (one or two sentences). secure_pattern: how the fixed code avoids "
    "it, stated as a reusable rule. explanation: why the original code was exploitable. "
    "Write each field as a full sentence of its own: never repeat the advisory title, never answer "
    "with only a function name, and do not say the same thing in two fields. "
    "If the diff does not show the cause, say so instead of guessing.")
STRICTER = ("\n\nYour previous answer was rejected: {reason}. Answer again with JSON only. Each of the three fields must be a different full sentence "
            "(root_cause: what the code did wrong; secure_pattern: the rule the fix follows, in words and not a function name; explanation: how an attacker could use the mistake).")

# Build filters the candidate list shares with the dataset build (passed to select()).
SHARED = ("date_from", "date_to", "languages", "severities", "cwes", "ecosystems",
          "granularity", "prefer_function", "max_lines", "include_tests", "include_tangled",
          "exclude_suspect", "per_cve_cap", "collapse_backports")
DEFAULTS = {
    **{k: bcd.DEFAULTS[k] for k in SHARED},
    "levels": ["weak"],            # context levels to offer
    "missing": [],                 # a pair must lack every signal listed (context.MISSING keys)
    "min_advisory": ctx.MIN_ADVISORY,
    "splits": ["train", "val"],                  # (there is no test split)
    "status": "missing",           # missing | older (earlier prompt version) | any
    "order": "weakest",            # weakest | newest
}
_ORDER = {"weak": 0, "partial": 1, "strong": 2}


def _v(x):
    """pandas NaN / NA -> None (rows go out as JSON)."""
    if isinstance(x, (list, dict, str)) or x is None:
        return x
    try:
        return None if pd.isna(x) else x
    except (TypeError, ValueError):
        return x


def _client():
    return llm_client.configured(timeout=120)


# ── candidates ───────────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def _stamp() -> tuple:
    """Changes whenever pairs or records change (LLM drafts, mining, edits)."""
    return (store.change_token(),)


def _dataset_rows(build_f: dict, min_advisory: int = 0) -> pd.DataFrame:
    """The rows a build with these filters contains, already rated and without the advisory texts, cached until the store changes.

    The cache used to hold the whole selection (about 60 columns, with every advisory text) and rated it again on each call; that was most of
    the API process's resident memory after a session. It now keeps one slim, rated frame: the texts are reduced to what the list shows (the
    first 160 characters and the length), and ``_with_text`` fetches the full text of the few rows that are enriched."""
    key = (json.dumps(build_f, sort_keys=True, default=str), _stamp(), int(min_advisory))
    with _cache_lock:
        if _cache.get("key") == key:
            return _cache["df"]
    df, _, _ = bcd.select(build_f)
    if len(df):
        df, _ = bcd.split(df, build_f.get("val_share", 0.05), bool(build_f.get("train_all")))
        df = _slim(_rate(df, min_advisory))
    with _cache_lock:
        _cache.clear()                                   # one selection at a time
        _cache.update(key=key, df=df)
    return df


def _rate(df: pd.DataFrame, min_advisory: int) -> pd.DataFrame:
    rated = [ctx.assess_row(r, min_advisory) for r in bcd._records(df, bcd.ASSESS_COLUMNS)]
    return df.assign(level=[x["level"] for x in rated], missing=[x["missing"] for x in rated],
                     advisory_state=[x["advisory"] for x in rated],
                     commit_state=[x["commit_msg"] for x in rated])


# Heavy columns of a selection that the candidate list does not show (the code and its derivatives stay in the store).
_DROP_AFTER_RATING = ("summary", "details", "commit_message", "explanation", "affected_versions", "fixed_version", "safe_version",
                      "before_hash", "content_hash", "quarantine_reason", "parent_selection_reason")


def _slim(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the first 160 characters of the advisory and its length, then drop the texts."""
    adv = [ctx.advisory_text(s, d) for s, d in zip(df.get("summary", [None] * len(df)), df.get("details", [None] * len(df)))]
    df = df.assign(advisory_head=[a[:160] for a in adv], advisory_chars=[len(a) for a in adv])
    return df.drop(columns=[c for c in _DROP_AFTER_RATING if c in df.columns])


def _status_mask(df: pd.DataFrame, status: str) -> pd.Series:
    enriched = df.enriched.fillna(0).astype(int) == 1
    if status == "missing":
        return ~enriched
    if status == "older":
        from app.backend.pipeline.context_exchange import PROMPT_VERSION as expert_version
        return enriched & ~df.llm_prompt_version.fillna('').isin([PROMPT_VERSION, expert_version])
    return pd.Series(True, index=df.index)


def select_candidates(filters: dict | None) -> tuple[pd.DataFrame, dict]:
    """(matching rows in enrichment order, counts for the filter chips)."""
    f = {**DEFAULTS, **{k: v for k, v in (filters or {}).items() if v is not None}}
    build_f = {**bcd.DEFAULTS, **{k: f[k] for k in SHARED}}
    df = _dataset_rows(build_f, int(f["min_advisory"] or 0))
    counts = {"dataset_rows": int(len(df)), "levels": {l: 0 for l in ctx.LEVELS},
              "missing": {k: 0 for k in ctx.MISSING}, "enriched": 0, "older_prompt": 0}
    if not len(df):
        return df, counts
    counts["enriched"] = int((df.enriched.fillna(0) == 1).sum())
    counts["older_prompt"] = int(_status_mask(df, "older").sum())
    counts["all_levels"] = {l: int(n) for l, n in df.level.value_counts().items()}
    status_ok = _status_mask(df, f["status"])
    counts["splits"] = {s: int(n) for s, n in df[status_ok].split.value_counts().items()}
    base = df[df.split.isin(f["splits"] or ["train", "val"]) & status_ok]
    counts["levels"].update({l: int(n) for l, n in base.level.value_counts().items()})
    if f["levels"]:
        base = base[base.level.isin(f["levels"])]
    for k in ctx.MISSING:
        counts["missing"][k] = int(base.missing.map(lambda m, k=k: k in m).sum())
    if f["missing"]:
        want = set(f["missing"])
        base = base[base.missing.map(lambda m: want <= set(m))]
    if f["order"] == "newest":
        base = base.sort_values("date", ascending=False)
    else:
        gran = base.granularity.map(codestore.GRANULARITY_RANK).fillna(0)
        base = base.assign(_l=base.level.map(_ORDER), _g=-gran, _c=-base.changed_lines.fillna(0)) \
            .sort_values(["_l", "_g", "_c"]).drop(columns=["_l", "_g", "_c"])
    base = base.drop_duplicates("pair_id")
    counts["matching"] = int(len(base))
    return base, counts


def candidates(filters: dict | None = None, limit: int = 50, offset: int = 0) -> dict:
    """A page of candidates for the UI, with the chip counts."""
    df, counts = select_candidates(filters)
    page = df.iloc[int(offset):int(offset) + int(limit)] if len(df) else df
    rows = []
    for r in page.to_dict("records"):
        r = {k: _v(v) for k, v in r.items()}
        msg = next((l.strip() for l in str(r.get("msg_head") or "").splitlines() if l.strip()), "")
        rows.append({
            "pair_id": int(r["pair_id"]), "record_id": int(r["record_id"]),
            "cve": r.get("cve_id") or r.get("osv_id"), "osv_id": r.get("osv_id"),
            "file": r.get("file_path"), "function": r.get("function_name"), "span": r.get("span"),
            "granularity": r.get("granularity"), "language": r.get("language"),
            "severity": r.get("severity"), "cwe": r.get("cwe"), "split": r.get("split"),
            "changed_lines": int(r.get("changed_lines") or 0), "level": r["level"],
            "missing": r["missing"], "advisory_state": r["advisory_state"],
            "commit_state": r["commit_state"], "advisory_len": int(r.get("advisory_chars") or 0),
            "advisory": r.get("advisory_head") or "",
            "commit_message": msg[:160],
            "enriched": bool(r.get("enriched")), "llm_prompt_version": r.get("llm_prompt_version"),
            "llm_model": r.get("llm_model")})
    return {**counts, "total": counts.get("matching", 0), "rows": rows,
            "prompt_version": PROMPT_VERSION, "missing_labels": ctx.MISSING}


# ── prompting ────────────────────────────────────────────────────────────────

def _prompt(p: dict) -> str:
    where = p.get("file_path") or "?"
    if p.get("function_name"):
        where += f" :: {p['function_name']}"
    elif p.get("span"):
        where += f" (lines {p['span']})"
    advisory = p.get("advisory") or ""
    parts = [f"Advisory id: {p.get('cve_id') or p.get('osv_id') or 'unknown'}", f"CWE: {p.get('cwe') or 'unknown'}",
             f"Severity: {p.get('severity') or 'unknown'}", f"Language: {p.get('language')}",
             f"Location: {where} ({p.get('granularity') or 'file'} level)", "",
             f"Advisory:\n{advisory[:2000] or '(none)'}", "",
             f"Fix commit message:\n{(p.get('commit_message') or '').strip()[:600] or '(none)'}"]
    if p.get("advisory_state") != "full" and p.get("code_before"):
        parts += ["", f"Vulnerable code:\n{p['code_before'][:4000]}"]
    parts += ["", f"Diff:\n{(p.get('diff') or '')[:6000]}"]
    return "\n".join(parts)


def _parse(text: str) -> dict:
    """The JSON object in a model's answer, or {} when there is none."""
    try:
        out = json.loads(text)
    except Exception:  # ignored: not strict JSON; the next lines extract the first object
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            out = json.loads(m.group(0)) if m else {}
        except Exception:  # ignored: no usable object in the answer
            out = {}
    return out if isinstance(out, dict) else {}


class Rejected(ValueError):
    """The model answered twice and neither draft says anything; the message is why, and how it stopped."""



def _ask(client, model: str, p: dict, tokens: int, extra: str = "") -> tuple[str, str | None]:
    kwargs = {}
    if os.getenv("LLM_JSON_MODE", "0") == "1":
        kwargs["response_format"] = {"type": "json_object"}
    r = client.chat.completions.create(
        model=model, temperature=0, max_tokens=tokens,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": _prompt(p) + extra}], **kwargs)
    choice = r.choices[0]
    return choice.message.content or "", getattr(choice, "finish_reason", None)


def _one(client, model: str, p: dict) -> dict:
    """The model's draft for one pair. A draft that says nothing (``check_draft``) or that is not JSON is asked for once more, with the reason in the request; an answer cut off by the token limit gets
    more room. Two bad drafts raise ``Rejected`` with the reason and how the model stopped."""
    tokens, extra, reason, finish = MAX_TOKENS, "", None, None
    for _ in range(2):
        text, finish = _ask(client, model, p, tokens, extra)
        out = _parse(text)
        reason = check_draft(out, p) if out else "the model returned no usable JSON"
        if reason is None:
            return out
        if finish == "length":
            tokens = LONG_MAX_TOKENS
        extra = STRICTER.format(reason=reason)
    raise Rejected(f"{reason}" + (f" (finish_reason {finish})" if finish else ""))


def _with_text(df: pd.DataFrame) -> list[dict]:
    """Candidate rows + the text the prompt needs (diff, code, full commit message)."""
    if not len(df):
        return []
    code = bcd._code(df.pair_id.astype(int).tolist(), ["diff", "code_before", 'code_after', "commit_message"])
    texts = _record_texts(df) if "details" not in df.columns else {}
    out = []
    for r in df.to_dict("records"):
        r = {k: _v(v) for k, v in {**r, **texts.get(int(r["record_id"]), {}), **code.get(int(r["pair_id"]), {})}.items()}
        r["advisory"] = ctx.advisory_text(r.get("summary"), r.get("details"))
        out.append(r)
    return out


def _record_texts(df: pd.DataFrame) -> dict[int, dict]:
    """Summary and details of the records behind a few rows (the cached selection keeps no advisory text)."""
    out = {}
    for rid in {int(i) for i in df.record_id}:
        rec = store.get(rid) or {}
        out[rid] = {"summary": rec.get("summary"), "details": rec.get("details")}
    return out


def _run(todo: list[dict], workers: int, progress=None, verbose: bool = True, should_stop=None) -> dict:
    """Ask the model about each pair and store what it writes. With ``should_stop`` the requests not yet started are dropped when it says yes; the ones in flight finish and are stored."""
    client, model = _client()
    report = progress or (lambda **_: None)
    done, errors, by_level, stopped = 0, [], {}, {"yes": False}
    t0, last = time.time(), 0.0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = {ex.submit(_one, client, model, p): p for p in todo}

        def finished():
            """The requests as they complete. With ``should_stop`` it looks twice a second, and when it says yes the requests not yet started are dropped."""
            if should_stop is None:
                yield from as_completed(futs)
                return
            pending = set(futs)
            while pending:
                ready, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                if not stopped["yes"] and should_stop():
                    stopped["yes"] = True
                    for f in list(pending):
                        if f.cancel():
                            pending.discard(f)
                yield from ready
        for i, fut in enumerate(finished(), 1):
            p = futs[fut]
            loc = p.get("file_path", "") + (f"::{p['function_name']}" if p.get("function_name") else "")
            tag = f"[{i}/{len(todo)}] {p.get('cve_id') or p.get('osv_id') or '-'} {loc}"
            info = f" ({p['level']}: {', '.join(ctx.MISSING[m] for m in p['missing'])})" \
                if p.get("level") and p.get("missing") else ""
            try:
                out = fut.result()
                if not out.get("root_cause"):
                    raise ValueError("the model returned no usable JSON")
            except Exception as e:
                errors.append(f"{loc}: {str(e)[:200]}")
                if verbose:
                    print(f"{tag}: failed - {str(e)[:120]}")
            else:
                codestore.set_pair_llm(int(p["pair_id"]), str(out.get("root_cause"))[:2000],
                                       str(out.get("secure_pattern") or "")[:2000] or None,
                                       str(out.get("explanation") or "")[:3000] or None,
                                       model, PROMPT_VERSION)
                done += 1
                lvl = p.get("level") or "?"
                by_level[lvl] = by_level.get(lvl, 0) + 1
                if verbose:
                    print(f"{tag}: ok{info}")
            if time.time() - last >= 1 or i == len(todo) or stopped["yes"]:
                last = time.time()
                report(state="enrich", label="Enriching with the LLM", processed=i,
                       total=len(todo), counts={"enriched": done, "failed": len(errors)})
    if verbose:
        print(f"\nenriched {done}/{len(todo)} pairs in {time.time() - t0:.0f}s"
              + (f" ({', '.join(f'{k} {v}' for k, v in by_level.items())})" if by_level else "")
              + (f"; {len(errors)} failed" if errors else ""))
    return {"enriched": done, "failed": len(errors), "errors": errors, "model": model,
            "prompt_version": PROMPT_VERSION, "by_level": by_level, "paused": stopped["yes"]}


def _rows_for_ids(pair_ids: list[int]) -> list[dict]:
    """The given pairs with their record's text; pairs outside every mined record
    (or excluded by the build rules) fall back to the pair's own fields."""
    ids = {int(i) for i in pair_ids}
    lenient = {**bcd.DEFAULTS, "include_tests": True, "include_tangled": True,
               "exclude_suspect": False, "prefer_function": False, "max_lines": 0,
               "per_cve_cap": 0}
    df, _, _ = bcd.select(lenient)
    found = df[df.pair_id.isin(ids)].drop_duplicates("pair_id") if len(df) else df
    rows = _with_text(_rate(found, ctx.MIN_ADVISORY)) if len(found) else []
    rest = sorted(ids - {int(r["pair_id"]) for r in rows})
    for p in codestore.pairs_for_enrichment(ids=rest) if rest else []:
        p = {**p, "pair_id": p["id"], "advisory": p.get("explanation") or ""}
        rated = ctx.assess(p["advisory"], p.get("commit_message"), p.get("cwe"), None,
                           p.get("file_path"), p.get("function_name"))
        rows.append({**p, "level": rated["level"], "missing": rated["missing"],
                     "advisory_state": rated["advisory"]})
    return rows


def enrich_ids(pair_ids: list[int], advisory: str | None = None, workers: int = 2,
               progress=None, verbose: bool = False) -> dict:
    """Enrich specific pairs now (chosen in the candidate list, one pair, or all pairs
    of one record), replacing earlier LLM drafts. ``advisory`` (the record's text)
    is used when it is longer than what a pair carries. Raises RuntimeError when no
    LLM is configured."""
    _client()                                          # fail fast without an LLM
    todo = _rows_for_ids(pair_ids)
    for p in todo:
        if advisory and len((p.get("advisory") or "").strip()) < len(advisory.strip()):
            p["advisory"] = advisory
            p["advisory_state"] = "full" if len(advisory.strip()) >= ctx.MIN_ADVISORY else "short"
    if verbose:
        print(f"enriching {len(todo)} selected pairs with {llm_client.resolved().model_label} "
              f"(prompt {PROMPT_VERSION}); results are stored as synthetic *_llm fields")
    return _run(todo, workers, progress, verbose)


def coverage(df: pd.DataFrame) -> dict:
    """How many of the rows of a dataset selection have model-written context, by how much real context they carry: ``{level: {"total", "enriched"}}``."""
    out = {l: {"total": 0, "enriched": 0} for l in ctx.LEVELS}
    if len(df):
        marked = df.assign(_enriched=(df.enriched.fillna(0).astype(int) == 1))
        for level, grp in marked.groupby("level"):
            out[level] = {"total": int(len(grp)), "enriched": int(grp._enriched.sum())}
    return out


def run_selected(filters: dict | None, *, limit: int | None = None, workers: int = 2, chunk: int = 25, should_stop=None, progress=None, verbose: bool = False) -> dict:
    """Write model drafts for the pairs that match ``filters`` (weakest context first), ``chunk`` pairs at a time. The selection is made once; ``limit`` caps the pairs written.

    ``should_stop`` (see ``stopping``) is asked before each chunk and between requests: a pause ends the run, keeping every draft written so far, and the result carries
    ``"paused": True`` (running it again picks up the pairs still without a draft). The result names the model and the prompt version, how many pairs were offered, written and failed,
    and the coverage by level before and after, so a campaign can show what it changed. Raises RuntimeError when no model is configured."""
    from app.backend.pipeline import stopping
    _client()                                                         # fail fast without a model
    report = progress or (lambda **_: None)
    f = {**DEFAULTS, **{k: v for k, v in (filters or {}).items() if v is not None}}
    build_f = {**bcd.DEFAULTS, **{k: f[k] for k in SHARED}}
    before = coverage(_dataset_rows(build_f, int(f["min_advisory"] or 0)))
    df, _ = select_candidates(f)
    if limit and len(df):
        df = df.head(int(limit))
    total = len(df)
    done = failed = 0
    written: dict[str, int] = {}
    errors: list[str] = []
    paused = False
    model = llm_client.resolved().model
    for start in range(0, total, max(1, int(chunk))):
        if stopping.stop(should_stop):
            paused = True
            break
        base_done, base_failed = done, failed

        def sub(_done=base_done, _failed=base_failed, **state):
            counts = state.get("counts") or {}
            report(state="enrich", label="Writing context with the model", processed=_done + _failed + int(state.get("processed") or 0), total=total,
                   counts={"enriched": _done + int(counts.get("enriched", 0)), "failed": _failed + int(counts.get("failed", 0))})
        res = _run(_with_text(df.iloc[start:start + int(chunk)]), workers, progress=sub, verbose=verbose, should_stop=should_stop)
        done += res["enriched"]
        failed += res["failed"]
        errors += res["errors"]
        for level, n in res["by_level"].items():
            written[level] = written.get(level, 0) + n
        model = res["model"] or model
        if res["paused"]:
            paused = True
            break
    after = {level: {"total": c["total"], "enriched": c["enriched"] + written.get(level, 0)} for level, c in before.items()}
    report(state="paused" if paused else "done", label="Paused" if paused else "Context written", processed=done + failed, total=total, counts={"enriched": done, "failed": failed})
    out = {"candidates": total, "enriched": done, "failed": failed, "errors": errors[:20], "model": model, "prompt_version": PROMPT_VERSION, "by_level": written,
           "coverage_before": before, "coverage_after": after}
    if paused:
        out["paused"] = True
    return out


def enrich(limit: int = 50, workers: int = 2, filters: dict | None = None,
           languages: list[str] | None = None, overwrite: bool = False,
           thin_only: bool = False, progress=None) -> int:
    """Enrich up to ``limit`` candidates matching ``filters`` (weakest first). The
    older arguments map onto filters: thin_only -> weak only, overwrite -> any."""
    _client()
    f = dict(filters or {})
    if filters is None:
        f["levels"] = ["weak"] if thin_only else list(ctx.LEVELS)
        f["status"] = "any" if overwrite else "missing"
    if languages:
        f["languages"] = [l.lower() for l in languages]
    df, counts = select_candidates(f)
    todo = _with_text(df.head(int(limit))) if len(df) else []
    lv = ", ".join(f"{k} {v}" for k, v in counts.get("levels", {}).items())
    print(f"{counts.get('dataset_rows', 0):,} dataset rows; {counts.get('matching', 0):,} match the "
          f"enrichment filters (in the selected splits and status: {lv})")
    print(f"enriching {len(todo)} pairs, weakest context first, with {llm_client.resolved().model_label} "
          f"(prompt {PROMPT_VERSION}); results are stored as synthetic *_llm fields")
    if not todo:
        return 0
    return _run(todo, workers, progress)["enriched"]
