"""Pure parsing tasks. Spawn workers without importing pandas or database adapters."""
from __future__ import annotations
import hashlib
import time
from app.backend.pipeline import funcsplit, util, stopping
from app.backend.pipeline.ingest import splitquality

def initialize():
    # Parsing uses independent processes. BLAS threads add heaps without doing useful work here.
    import os
    for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','BLIS_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[name]='1'


def _hash(before: str | None, after: str | None) -> str:
    return hashlib.sha1(f"{util.norm_code(before)}\0{util.norm_code(after)}".encode()).hexdigest()

def _udiff(before: str, after: str, a: str, b: str) -> str:
    return util.udiff(before, after, a, b)

class AnalyzedFiles(list):
    pass

def analyze_files(items: list[tuple[dict, str | None]]) -> list[tuple[dict, list[dict], "funcsplit.Analysis | None"]]:
    """The CPU-bound half of mining a commit's changed files - tree-sitter parsing and AST-diffing (``sub_pairs_with_analysis``) - for every file whose
    ``code_before``/``code_after`` is already fetched (no I/O happens here). Picklable and top-level so a ``compute.Pool`` can run it in a worker process,
    off the thread that is fetching other commits, instead of sharing one core's GIL with all of their parsing too."""
    from app.backend.pipeline.acceleration.metrics import current
    before, cpu_start = current.snapshot(), time.process_time()
    with current.timer('parser_work'):
        result = AnalyzedFiles((rec, *sub_pairs_with_analysis(rec, old_path)) for rec, old_path in items
                               if not funcsplit.is_noop(rec.get("language"), rec["code_before"], rec["code_after"]))
    after = current.snapshot()
    result.performance = {'counts':{k:v-before['counts'].get(k,0) for k,v in after['counts'].items()},
        'timings':{k:{'calls':v['calls']-before['timings'].get(k,{}).get('calls',0),
                      'seconds':v['seconds']-before['timings'].get(k,{}).get('seconds',0)} for k,v in after['timings'].items()}}
    result.performance['counts']['parser_cpu_seconds'] = time.process_time()-cpu_start
    return result

def sub_pairs_with_analysis(file_pair: dict, old_path: str | None = None, policy: splitquality.Policy | None = None) -> tuple[list[dict], "funcsplit.Analysis | None"]:
    """Function / hunk pairs derived from a usable file-level pair (funcsplit), and the analysis they came from (None when the splitter failed).

    How the extraction went is recorded on ``file_pair`` itself (``parse_status``, ``parse_failure_reason``, and in ``extraction_meta`` the functions added or removed, the changes outside
    every function, the matches that are not one to one and the ledger of changed lines) and the status is inherited by every derived pair. Each derived pair records how it was matched
    (``match_kind``, ``match_confidence``) and where it came from (``extraction_meta``: lines and bytes of both versions, the signals, the parser version, the quality assessment).
    The quality ``policy`` (``ingest/splitquality.py``; by default the file ``split_policy.json`` of the interim folder if there is one) decides which pairs are quarantined for review: a weak
    or ambiguous match, a function over a parse error, a change outside every function carried as a hunk of its own. When a parser exists for the language but failed, the derived hunk pairs
    are quarantined as ``parse_unresolved`` (their boundaries are not trustworthy); a language without a grammar is a known fallback, not a failure."""
    before, after = file_pair["code_before"], file_pair["code_after"]
    path, old = file_pair["file_path"], old_path or file_pair["file_path"]
    base = file_pair["pair_key"][:-2] if file_pair["pair_key"].endswith(":*") else file_pair["pair_key"]
    policy = policy or splitquality.default_policy()
    out = []
    analysis = None
    try:
        analysis = funcsplit.analyze(file_pair.get("language") or "", path, before, after, diff=file_pair.get("diff"))
        parts, status, reason = analysis.pairs, analysis.status, analysis.reason
    except (stopping.Paused, MemoryError):
        raise
    except Exception:  # ignored: recorded as parse status 'fallback' with reason 'splitter_error'; the file-level pair is never lost (never lose the file-level pair)
        parts, status, reason = [], "fallback", "splitter_error"
    file_pair["parse_status"], file_pair["parse_failure_reason"] = status, reason
    if analysis is not None:
        file_pair["extraction_meta"] = splitquality.dumps({**splitquality.file_meta(analysis), "sig": splitquality.split_signature(file_pair, policy)})
    quarantine = file_pair.get("quarantine_reason") or ("parse_unresolved" if status == "fallback" else None)
    for u in parts:
        loc = u["function_name"] or f"@{u['span']}"
        b, a = u["code_before"], u["code_after"]
        assessment = splitquality.assess(u, analysis, policy, splitquality.check_provenance(u, before, after))
        match = u.get("match") or {}
        meta = splitquality.pair_meta(u, analysis, assessment)
        out.append({**file_pair, "granularity": u["granularity"], "function_name": u["function_name"],
                    "function_name_before": u.get("function_before"), "span": u["span"],
                    "code_before": b, "code_after": a, "pair_key": f"{base}:{loc}",
                    "diff": _udiff(b + "\n", a + "\n", f"a/{old}::{loc}", f"b/{path}::{loc}"),
                    "code_available": 1, "skip_reason": None, "content_hash": _hash(b, a),
                    "changed_lines": None, "quarantine_reason": quarantine or splitquality.quarantine_reason(assessment),
                    "attribution_basis": None, "attribution_score": None,
                    "match_kind": match.get("kind") or ("residual" if u.get("role") == "residual" else None), "match_confidence": match.get("confidence"),
                    "extraction_meta": meta})
    for key, first in splitquality.duplicates(out).items():           # a repeat within the file is pointed at its first, and kept
        row = next(r for r in out if r["pair_key"] == key)
        row["extraction_meta"] = {**row["extraction_meta"], "duplicate_of": first}
    for row in out:
        row["extraction_meta"] = splitquality.dumps(row["extraction_meta"])
    return out, analysis
