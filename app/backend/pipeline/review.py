"""Review tiers, the auto_checked gate, the review queue and the audit sample.

Tiers (``reviewed.REVIEW_STATUSES``): ``unreviewed`` < ``auto_checked`` < ``llm_adjudicated`` <
``human_reviewed``. A row reaches ``auto_checked`` only when every deterministic check in ``gate`` passes
*and* every input the checks need has been evaluated; that tier is evidence grade **G0** (the advisory,
the fix commit and the diff agree, label basis ``advisory_fix_diff``). It is not a claim that a person or
a verifier confirmed anything: grade G1 needs a review decision, G2 and G3 need verification receipts.
Unreviewed rows get supervision weight 0.0.

Review decisions are appended to ``derived.review_decisions`` (never updated). A stratified audit sample
of auto-checked candidates is drawn once and kept (``derived.audit_samples``); once people have reviewed
it, ``error_rates`` estimates how often the gate is wrong, with Wilson intervals, and names the strata that
fail so the rule behind them can be corrected.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from app.backend.pipeline import derived, ecosystems
from app.backend.pipeline.ingest import attribution
from app.backend.pipeline.schemas import reviewed
from app.backend.pipeline.db import pipeline_mongo as _pipeline_mongo

RUBRIC_VERSION = "rubric_v1"
RUBRIC_PATH = pathlib.Path(__file__).with_name("schemas") / "rubric_v1.md"

LINK_EVIDENCE_OK = ("strong", "moderate")
PARSE_OK = ("ok", "no_grammar")                      # no_grammar: the language has no parser, a known fallback
COMMIT_KIND_REASONS = ("revert_commit", "release_commit", "ambiguous_merge_parent")
ATTRIBUTED = tuple(attribution.TIERS["decision"])             # the gate takes the decision tier and nothing else (ingest/attribution.py)
EXPLANATION_OK = ("advisory", "llm_fallback")
CONTEXT_OK = ("complete", "partial")                 # "changed_files_only" is below partial
STRATA = ("ecosystem", "source", "mechanism", "synthetic", "extraction")
ERROR_STATES = ("no_supported_finding_in_scope", "insufficient_context", "unreviewed")


# -- mechanism families (a coarse stratification aid, not a claim about any vulnerability) -----------------------------

_FAMILIES = {
    "injection": (74, 77, 78, 79, 80, 88, 89, 90, 91, 94, 95, 96, 98, 113, 116, 117, 138, 917, 1236, 1336),
    "path_traversal": (22, 23, 24, 25, 26, 27, 28, 29, 36, 37, 38, 39, 40, 59, 73, 434),
    "deserialization": (502, 915),
    "access_control": (264, 269, 284, 285, 287, 288, 290, 294, 295, 306, 345, 346, 639, 732, 862, 863, 1220),
    "crypto": (295, 310, 311, 312, 319, 320, 321, 322, 323, 324, 325, 326, 327, 328, 329, 330, 331, 335, 338, 347, 916),
    "memory_safety": (119, 120, 121, 122, 123, 124, 125, 129, 131, 190, 191, 415, 416, 476, 787, 788, 824, 843),
    "resource_exhaustion": (400, 405, 407, 674, 770, 772, 834, 835, 1333),
    "information_exposure": (200, 201, 203, 209, 212, 215, 497, 532, 538, 552),
    "prototype_pollution": (1321, 915),
    "redirect_ssrf": (601, 918),
    "input_validation": (20, 74, 129, 1284),
    "race_condition": (362, 364, 367, 662),
    "csrf": (352,),
}
_CWE_FAMILY: dict[int, str] = {}
for _name, _ids in _FAMILIES.items():
    for _id in _ids:
        _CWE_FAMILY.setdefault(_id, _name)


def mechanism(cwe) -> str:
    """The coarse family of the first recognised CWE in ``cwe`` (text, JSON list or None)."""
    ids = [int(x) for x in re.findall(r"(?:CWE-)?\b(\d{1,5})\b", str(cwe or ""))]
    if not ids:
        return "unclassified"
    for i in ids:
        if i in _CWE_FAMILY:
            return _CWE_FAMILY[i]
    return "other"


# -- the gate ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    failed: tuple[str, ...]
    not_evaluated: tuple[str, ...]

    @property
    def passed(self) -> bool:                    # nothing that was evaluated failed
        return not self.failed

    @property
    def candidate(self) -> bool:                 # eligible for the review queue and the audit sample
        return self.passed

    @property
    def tier(self) -> str:                       # auto_checked needs every check evaluated and passed
        return "auto_checked" if self.passed and not self.not_evaluated else "unreviewed"

    def as_dict(self) -> dict:
        return {"passed": self.passed, "tier": self.tier, "failed": list(self.failed),
                "not_evaluated": list(self.not_evaluated)}


def _quarantine_failures(q: str | None) -> list[str]:
    if not q:
        return []
    if q in COMMIT_KIND_REASONS:
        return ["commit_kind"]
    if q == "no_op_change":
        return ["formatting_only"]
    if q == "parse_unresolved":
        return ["parse"]
    return ["quarantined"]


def gate(row: dict, *, split: str | None = None, context_completeness: str | None = None,
         hidden_fact_risk: bool | None = None) -> GateResult:
    """The deterministic checks behind ``auto_checked``. ``row`` is a code pair joined with its fix commit
    (``load_rows``). ``split`` (the split manifest), ``context_completeness`` (the context builder) and
    ``hidden_fact_risk`` (the view inferability check) come from later stages; when absent they are
    reported as not evaluated and the row stays ``unreviewed``."""
    failed: list[str] = []
    if row.get("fix_check") != "verified":
        failed.append("fix_link_verified")
    if row.get("evidence_level") not in LINK_EVIDENCE_OK:
        failed.append("link_evidence")
    if row.get("parent_count") is None or not row.get("parent_selection_reason"):
        failed.append("parent_recorded")
    failed += _quarantine_failures(row.get("quarantine_reason"))
    if row.get("parse_status") not in PARSE_OK and "parse" not in failed:
        failed.append("parse")
    if row.get("is_test") or row.get("tangled") or not row.get("code_available"):
        failed.append("production_code")
    if row.get("attribution_basis") not in ATTRIBUTED:
        failed.append("attributed")
    if row.get("explanation_source") not in EXPLANATION_OK or row.get("needs_explanation_enrichment"):
        failed.append("explanation_resolved")
    missing: list[str] = []
    for name, value, bad in (("graph_clean", split, lambda v: v not in ("train", "val")),
                             ("context_sufficient", context_completeness, lambda v: v not in CONTEXT_OK),
                             ("hidden_fact_risk", hidden_fact_risk, lambda v: bool(v))):
        if value is None:
            missing.append(name)
        elif bad(value):
            failed.append(name)
    order = {n: i for i, n in enumerate((
        "fix_link_verified", "link_evidence", "parent_recorded", "commit_kind", "formatting_only", "quarantined", "parse",
        "production_code", "attributed", "explanation_resolved", "graph_clean", "context_sufficient", "hidden_fact_risk"))}
    return GateResult(tuple(sorted(dict.fromkeys(failed), key=order.get)), tuple(sorted(missing, key=order.get)))


def auto_label(row: dict, **inputs) -> dict:
    """The label a row carries from the gate alone: G0 with basis ``advisory_fix_diff``, or unreviewed."""
    if gate(row, **inputs).tier == "auto_checked":
        return {"review_status": "auto_checked", "label_state": "vulnerability_supported",
                "label_basis": "advisory_fix_diff", "evidence_grade": "G0"}
    return {"review_status": "unreviewed", "label_state": "unreviewed", "label_basis": None, "evidence_grade": None}


MAX_SPANS, MAX_FACTS, MAX_FACT_CHARS = 5, 20, 300


def validate_fields(fields: dict, label_state: str, code: str | None, fixed_code: str | None = None) -> list[str]:
    """What is wrong with the structured fields of a decision (empty when they are usable).

    ``reviewed_security_spans`` (lines of the vulnerable code, 1-based, inclusive) feed the localization task and belong to a supported
    vulnerability; ``missing_facts`` (what the code shown does not tell) feed the insufficient-context task and belong to an
    insufficient_context decision. Both are optional, but when given they must be well formed: a malformed span would otherwise be a quote
    the student is trained to find in code that does not contain it."""
    problems: list[str] = []
    evidence = fields.get('evidence_spans')
    if evidence is not None and (not isinstance(evidence, list) or not evidence or any(
        not isinstance(e, dict) or not isinstance(e.get('quote'), str) or not e['quote'].strip() or e['quote'] not in (code or '') for e in evidence)):
        problems.append('evidence_spans must quote the supplied vulnerable code')
    fixed = fields.get('fixed_review')
    if fixed is not None:
        if not isinstance(fixed, dict) or set(fixed) - {'scope', 'safeguard', 'evidence'}:
            problems.append('fixed_review allows only scope, safeguard and evidence')
        elif not (isinstance(fixed.get('scope'), str) and fixed['scope'].strip() and isinstance(fixed.get('safeguard'), str) and fixed['safeguard'].strip()):
            problems.append('fixed_review requires a scope and an effective safeguard')
        elif not isinstance(fixed.get('evidence'), list) or not fixed['evidence'] or any(
            not isinstance(e, dict) or not isinstance(e.get('quote'), str) or not e['quote'].strip() or e['quote'] not in (fixed_code or '') for e in fixed['evidence']):
            problems.append('fixed_review evidence must quote the supplied fixed code')
    spans = fields.get("reviewed_security_spans")
    if spans is not None:
        lines = len((code or "").splitlines())
        if label_state != "vulnerability_supported":
            problems.append("reviewed_security_spans belong to a supported vulnerability")
        elif not isinstance(spans, list):
            problems.append("reviewed_security_spans must be a list of {start_line, end_line} objects")
        elif len(spans) > MAX_SPANS:
            problems.append(f"at most {MAX_SPANS} spans")
        else:
            for n, s in enumerate(spans, 1):
                if not isinstance(s, dict):
                    problems.append(f"span {n} must be an object with start_line and end_line")
                elif "start_line" not in s or "end_line" not in s:
                    problems.append(f"span {n} needs start_line and end_line")
                elif not all(isinstance(s[k], int) and not isinstance(s[k], bool) for k in ("start_line", "end_line")):
                    problems.append(f"span {n}: start_line and end_line must be integers")
                elif not (1 <= s["start_line"] <= s["end_line"] <= lines):
                    problems.append(f"span {n}: lines {s['start_line']}-{s['end_line']} are reversed or outside the vulnerable code (lines 1-{lines})")
    facts = fields.get("missing_facts")
    if facts is not None:
        if label_state != "insufficient_context":
            problems.append("missing_facts belong to an insufficient_context decision")
        elif (not isinstance(facts, list) or not facts or len(facts) > MAX_FACTS
              or not all(isinstance(f, str) and f.strip() and len(f) <= MAX_FACT_CHARS for f in facts)):
            problems.append(f"missing_facts must be a non-empty list (at most {MAX_FACTS}) of non-empty texts of at most {MAX_FACT_CHARS} characters")
    return problems


def effective_status(result: GateResult, decision: dict | None) -> tuple[str, str]:
    """(review_status, label_state) after applying the latest review decision. A decision can raise the
    tier; it never lowers one the gate granted."""
    status, label = ("auto_checked", "vulnerability_supported") if result.tier == "auto_checked" \
        else ("unreviewed", "unreviewed")
    if decision and reviewed.tier_at_least(decision["review_status"], status):
        if decision["review_status"] != "unreviewed":
            return decision["review_status"], decision["label_state"]
    return status, label


def supervision_weight(review_status: str, min_tier: str = "auto_checked") -> float:
    """1.0 for a row at or above the minimum tier, else 0.0; an unreviewed row is always 0.0."""
    if review_status == "unreviewed":
        return 0.0
    return 1.0 if reviewed.tier_at_least(review_status, min_tier) else 0.0


def tier_mix(statuses) -> dict:
    """How many rows sit in each tier (every manifest states this)."""
    c = Counter(statuses)
    return {t: c.get(t, 0) for t in reviewed.REVIEW_STATUSES}


# -- rows from the store ---------------------------------------------------------------------------------------------------



# -- the queue --------------------------------------------------------------------------------------------------------------

def _project(row: dict) -> str:
    return ecosystems.repository_identity(row.get("repo_url")) or "unknown"


def stratum(row: dict) -> dict:
    src = ",".join(sorted(s for s in re.split(r"[,\s]+", row.get("source") or "") if s)) or "unknown"
    return {"ecosystem": row.get("ecosystem") or "unknown", "source": src, "mechanism": mechanism(row.get("cwe")),
            "project": _project(row),
            "synthetic": "synthetic" if row.get("explanation_source") == "llm_fallback" else "real",
            "extraction": row.get("granularity") or "unknown"}


def _hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _item(row: dict, result: GateResult, decision: dict | None) -> dict:
    status, label = effective_status(result, decision)
    return {"record_id": reviewed.record_id_of(row["pair_key"]), "pair_key": row["pair_key"],
            "ecosystem": row.get("ecosystem"), "package": row.get("package"), "repository": _project(row),
            "granularity": row.get("granularity"), "function_name": row.get("function_name"),
            "file_path": row.get("file_path"), "language": row.get("language"),
            "evidence_level": row.get("evidence_level"), "attribution_basis": row.get("attribution_basis"),
            "mechanism": mechanism(row.get("cwe")), "gate": result.as_dict(),
            "effective": {"review_status": status, "label_state": label}}


def queue(rows: list[dict], decisions: dict, *, limit: int = 50, offset: int = 0, ecosystem: str | None = None,
          include_failed: bool = False, review_state: str = 'waiting', **gate_inputs) -> dict:
    """Rows waiting for a person: gate candidates without a human decision, strongest evidence first
    (a stable order: link evidence, then attribution, then a hash of the pair key)."""
    rank = {"strong": 0, "moderate": 1}
    waiting = []
    for r in rows:
        if ecosystem and r.get("ecosystem") != ecosystem:
            continue
        dec = decisions.get(reviewed.record_id_of(r["pair_key"]))
        if review_state == 'ai_adjudicated' and (not dec or dec['review_status'] != 'llm_adjudicated'):
            continue
        if review_state == 'waiting' and dec and (dec['review_status'] == 'human_reviewed' or
                (dec['review_status'] == 'llm_adjudicated' and
                 ((dec.get('fields') or {}).get('_review_exchange') or {}).get('complete') is True)):
            continue
        result = gate(r, **gate_inputs)
        if not (result.candidate or include_failed):
            continue
        waiting.append((rank.get(r.get("evidence_level"), 2), -(r.get("attribution_score") or 0.0), _hash(r["pair_key"]),
                        r, result, dec))
    waiting.sort(key=lambda t: t[:3])
    page = waiting[offset:offset + limit]
    return {"total": len(waiting), "limit": limit, "offset": offset,
            "items": [_item(r, res, dec) for _, _, _, r, res, dec in page]}


# -- the audit sample ------------------------------------------------------------------------------------------------------------

def audit_sample(rows: list[dict], n: int, *, seed: str = "audit", max_per_project: int = 5, **gate_inputs) -> list[dict]:
    """A deterministic stratified sample of gate candidates for people to review. Strata are the cells of
    ``STRATA``; each gets at least one pick when ``n`` allows (else the largest cells are taken), the rest is
    shared in proportion to size, and no project supplies more than ``max_per_project`` rows."""
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if gate(r, **gate_inputs).candidate:
            s = stratum(r)
            cells[tuple(s[k] for k in STRATA)].append({"row": r, "stratum": s})
    keys = sorted(cells, key=lambda k: (-len(cells[k]), k))
    if not keys or n <= 0:
        return []
    alloc = {k: 0 for k in keys}
    if n < len(keys):
        for k in keys[:n]:
            alloc[k] = 1
    else:
        for k in keys:
            alloc[k] = 1
        total = sum(len(cells[k]) for k in keys)
        spare = n - len(keys)
        quota = {k: spare * len(cells[k]) / total for k in keys}
        for k in keys:
            alloc[k] += int(quota[k])
        left = n - sum(alloc.values())
        for k in sorted(keys, key=lambda k: (-(quota[k] - int(quota[k])), k))[:max(left, 0)]:
            alloc[k] += 1
    per_project: Counter = Counter()
    out = []
    for k in keys:
        ordered = sorted(cells[k], key=lambda c: _hash(seed, c["row"]["pair_key"]))
        taken = 0
        for c in ordered:
            if taken >= min(alloc[k], len(ordered)):
                break
            proj = c["stratum"]["project"]
            if per_project[proj] >= max_per_project:
                continue
            per_project[proj] += 1
            taken += 1
            out.append({"record_id": reviewed.record_id_of(c["row"]["pair_key"]), "pair_key": c["row"]["pair_key"],
                        "stratum": c["stratum"]})
    return sorted(out, key=lambda i: _hash(seed, i["pair_key"]))[:n]


def draw_audit(conn, audit_id: str, rows: list[dict], n: int, *, seed: str | None = None, **kw) -> list[dict]:
    """Draw the sample once and keep it: asking again for the same audit returns the stored sample."""
    stored = load_audit(conn, audit_id)
    if stored:
        return stored
    sample = audit_sample(rows, n, seed=seed or audit_id, **kw)
    mg = derived.mongo_of(conn)
    mg.audit_keep(conn, audit_id, sample)
    return load_audit(conn, audit_id)


def load_audit(conn, audit_id: str) -> list[dict]:
    mg = derived.mongo_of(conn)
    return mg.audit_items(conn, audit_id)


def latest_audit_id(conn) -> str | None:
    mg = derived.mongo_of(conn)
    return mg.latest_audit_id(conn)


# -- error rates ----------------------------------------------------------------------------------------------------------------

def wilson(errors: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval of a proportion (all of [0, 1] when nothing was observed)."""
    if n <= 0:
        return 0.0, 1.0
    p = errors / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, round(centre - half, 6)), min(1.0, round(centre + half, 6))


def error_rates(sample: list[dict], decisions: dict, *, tolerance: float = 0.10, min_stratum_n: int = 5,
                statuses: tuple[str, ...] = ("human_reviewed",)) -> dict:
    """How often a reviewer disagrees with the gate on the audit sample. Only the listed decision
    statuses count (an LLM adjudication is not an independent check); an auto-checked row is an error when the
    reviewed label is not ``vulnerability_supported``. Strata whose Wilson lower bound exceeds ``tolerance``
    are returned as failed (correct the rule behind them), those that may exceed it as uncertain."""
    done = []
    for s in sample:
        d = decisions.get(s["record_id"])
        if d and d["review_status"] in statuses:
            done.append((s, d["label_state"] != "vulnerability_supported"))
    out = {"n_sampled": len(sample), "n_reviewed": len(done), "tolerance": tolerance, "statuses": list(statuses)}
    if not done:
        return {**out, "status": "unestimated", "errors": 0, "rate": None, "wilson_low": None, "wilson_high": None,
                "failed_strata": [], "uncertain_strata": [], "by_stratum": {}}
    errors = sum(e for _, e in done)
    lo, hi = wilson(errors, len(done))
    by: dict[str, dict] = {}
    failed, uncertain = [], []
    for dim in (*STRATA, "project"):
        groups: dict[str, list[bool]] = defaultdict(list)
        for s, e in done:
            groups[str(s["stratum"].get(dim, "unknown"))].append(e)
        for value, errs in sorted(groups.items()):
            k, m = sum(errs), len(errs)
            low, high = wilson(k, m)
            entry = {"dimension": dim, "value": value, "n": m, "errors": k, "rate": round(k / m, 4),
                     "wilson_low": low, "wilson_high": high}
            by[f"{dim}={value}"] = entry
            if m >= min_stratum_n and low > tolerance:
                failed.append(entry)
            elif m >= min_stratum_n and high > tolerance:
                uncertain.append(entry)
    return {**out, "status": "estimated", "errors": errors, "rate": round(errors / len(done), 4), "wilson_low": lo,
            "wilson_high": hi, "failed_strata": failed, "uncertain_strata": uncertain, "by_stratum": by}


def rubric_text() -> str:
    return RUBRIC_PATH.read_text(encoding="utf-8")


# -- embedded data layer (compatibility modules pipeline/db/*_mongo.py), bound under the established public names ---------------------
load_rows = _pipeline_mongo.review_rows
load_pair = _pipeline_mongo.review_pair
