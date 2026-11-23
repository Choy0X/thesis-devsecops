"""Quality assessment of the pairs the splitter makes (``funcsplit.analyze``): deterministic, with reasons, and configurable.

The splitter reports evidence; this module turns it into a decision about what may enter a training-ready dataset. For every pair:

* **accept**  the extraction is consistent and the match is as sure as the evidence allows: no reason to hold the pair back;
* **review**  the pair is real and kept, but something makes it uncertain (a weaker or ambiguous match, a function overlapping a parse error, a change outside every function that the
  splitter carried as a hunk of its own); it is stored with a ``quarantine_reason`` so the default dataset build leaves it out, and a reviewer or ``include_quarantined`` can release it;
* **reject**  the extraction contradicts itself (the recorded span does not slice the source to the pair's code) or has no provenance; stored, quarantined, never released automatically.

Nothing is thrown away: a pair that is not accepted keeps its code, its diff and the evidence (``extraction_meta``), so it can be analysed or reviewed. The scores it rests on are
``match_confidence`` (the matcher's, see ``splitmatch``), ``structural`` (the recorded spans slice the source to the code), ``provenance`` (lines and bytes of both versions are recorded),
``coverage`` (the share of the file's changed lines the splitter accounted for), ``ambiguity`` (rivals within the matcher's margin) and ``parse`` (no parse error inside the unit).
The default thresholds come from ``tools/funcsplit_mutation_bench.py``: among the renames and moves it accepted with a confidence of
0.9 or more every one was the right partner; below 0.8 the benchmark has too few cases to say more than "uncertain", which is what review means.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

META_VERSION = 1
MAX_LISTED = 200                     # most units / regions listed in a file's meta; the totals are always kept
TIERS = ("accept", "review", "reject")
_ORDER = {t: i for i, t in enumerate(TIERS)}
REASONS = ("cross_file_match", "low_match_confidence", "ambiguous_match", "parse_errors_in_unit", "residual_region", "missing_provenance", "inconsistent_extraction")


@dataclasses.dataclass(frozen=True)
class Policy:
    accept_confidence: float = 0.90       # a match at least this sure needs no review
    review_confidence: float = 0.60       # below this a match is not credible as a pairing
    low_confidence: str = "review"        # the tier for a match below accept_confidence (reject below review_confidence is not applied: it stays this tier)
    residual: str = "review"              # a change outside every function carried as a hunk of its own
    cross_file: str = "review"            # a function paired with one that left another file of the same commit
    partial_parse: str = "review"         # a function that overlaps a parse error
    ambiguous: str = "review"             # a match with a rival within the matcher's margin
    missing_provenance: str = "reject"
    inconsistent: str = "reject"

    def __post_init__(self):
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            if f.type == "str" and v not in TIERS:
                raise ValueError(f"{f.name}: {v!r} is not one of {TIERS}")
            if f.type == "float" and not 0.0 <= float(v) <= 1.0:
                raise ValueError(f"{f.name}: {v!r} is not between 0 and 1")

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        """The default policy with the values of a JSON file laid over it; no file is the default policy, a name that is not a setting is an error."""
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - names)
        if unknown:
            raise ValueError(f"unknown split policy setting(s): {', '.join(unknown)}")
        return cls(**data)


_policy_cache: dict[tuple, Policy] = {}


def default_policy() -> Policy:
    """The policy in force: ``split_policy.json`` in the interim folder when there is one, else the defaults (read again when the file changes)."""
    from app.backend import paths
    f = Path(paths.INTERIM) / "split_policy.json"
    stamp = (str(f), f.stat().st_mtime_ns if f.exists() else None)
    if stamp not in _policy_cache:
        _policy_cache.clear()
        _policy_cache[stamp] = Policy.from_file(f)
    return _policy_cache[stamp]


def _worse(tier: str, other: str) -> str:
    return other if _ORDER[other] > _ORDER[tier] else tier


def check_provenance(pair: dict, before: str, after: str) -> list[str]:
    """Problems found when the recorded spans of a pair are laid over the source it came from (an empty list: they slice it to exactly the pair's code)."""
    prov = pair.get("prov")
    if not prov:
        return []
    problems = []
    for side, src, code in (("before", before, pair.get("code_before")), ("after", after, pair.get("code_after"))):
        entry = prov.get(side) or {}
        if "bytes" in entry:
            lo, hi = entry["bytes"]
            if src.encode("utf-8")[lo:hi].decode("utf-8", "replace") != code:
                problems.append(f"{side}_bytes_do_not_slice_the_code")
        elif "lines" in entry and pair.get("granularity") == "hunk":
            lo, hi = entry["lines"]
            lines = src.split("\n")
            if "\n".join(lines[lo - 1:hi]) != code:
                problems.append(f"{side}_lines_do_not_slice_the_code")
    return problems


def assess(pair: dict, analysis, policy: Policy, problems: list[str] | None = None) -> dict:
    """``{"tier", "reasons", "scores"}`` for one pair of an ``funcsplit.Analysis``. ``problems`` are what ``check_provenance`` found."""
    tier, reasons = "accept", []
    match = pair.get("match") or {}
    prov = pair.get("prov")
    role = pair.get("role")
    cov = analysis.coverage or {}
    changed = cov.get("changed_lines") or 0
    scores = {"match_confidence": match.get("confidence"), "structural": 0.0 if problems else 1.0, "provenance": 1.0 if prov else 0.0,
              "coverage": round(1.0 - (cov.get("unaccounted", 0) / changed), 4) if changed else 1.0, "ambiguity": float(bool(match.get("alternatives"))),
              "parse": 0.0 if (match.get("signals") or {}).get("parse_errors_in_unit") else 1.0}

    def flag(level: str, reason: str):
        nonlocal tier
        tier = _worse(tier, level)
        if reason not in reasons:
            reasons.append(reason)

    if problems:
        flag(policy.inconsistent, "inconsistent_extraction")
    if not prov:
        flag(policy.missing_provenance, "missing_provenance")
    if match.get("kind") == "cross_file_move":
        flag(policy.cross_file, "cross_file_match")
    if pair.get("granularity") == "function":
        conf = match.get("confidence")
        if conf is None or conf < policy.accept_confidence:
            flag(policy.low_confidence, "low_match_confidence")
        if match.get("alternatives") or match.get("kind") == "positional":
            flag(policy.ambiguous, "ambiguous_match")
        if (match.get("signals") or {}).get("parse_errors_in_unit"):
            flag(policy.partial_parse, "parse_errors_in_unit")
    if role == "residual":
        flag(policy.residual, "residual_region")
    return {"tier": tier, "reasons": reasons, "scores": scores}


def quarantine_reason(assessment: dict) -> str | None:
    """The ``quarantine_reason`` a pair is stored with: none for an accepted pair, else its first reason."""
    if assessment["tier"] == "accept" or not assessment["reasons"]:
        return None
    return assessment["reasons"][0]


def label_reliability(assessment: dict, attribution_basis: str | None) -> str:
    """How far the label "this pair is the vulnerable unit" can be trusted, from the match and the attribution (``ingest/attribution.py``): ``attributed`` (the advisory or the fix names the
    unit, or it is the only one changed, and the match is sure), ``supporting`` (only the file is named), ``uncertain_match`` (the unit may not be the one that changed), ``unattributed``."""
    from app.backend.pipeline.ingest import attribution
    if assessment["tier"] != "accept":
        return "uncertain_match"
    tier = attribution.tier_of(attribution_basis)
    return {"decision": "attributed", "supporting": "supporting"}.get(tier, "unattributed")


def duplicates(pairs: list[dict]) -> dict[str, str]:
    """``{pair_key: key of the first pair with the same content}`` for the repeats among the pairs of one file."""
    first: dict[str, str] = {}
    out: dict[str, str] = {}
    for p in pairs:
        h = p.get("content_hash")
        if not h:
            continue
        if h in first:
            out[p["pair_key"]] = first[h]
        else:
            first[h] = p["pair_key"]
    return out


def dumps(obj) -> str:
    """Canonical compact JSON (sorted keys, no spaces): the same data is the same text on both backends."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=list)


def pair_meta(pair: dict, analysis, assessment: dict) -> dict:
    """What is kept beside a pair: where it came from in both versions, how it was matched, how the parse went and what the policy decided."""
    from app.backend.pipeline import funcsplit
    match = pair.get("match") or {}
    meta = {"v": META_VERSION, "role": pair.get("role") or "primary", "parser": funcsplit.parser_fingerprint(), "prov": pair.get("prov"),
            "quality": {"tier": assessment["tier"], "reasons": assessment["reasons"], "scores": assessment["scores"]}}
    if match:
        meta["signals"] = match.get("signals")
        if match.get("alternatives"):
            meta["alternatives"] = match["alternatives"][:3]
    if pair.get("residual_kind"):
        meta["residual_kind"] = pair["residual_kind"]
    for key in ("ast", "deps"):
        if pair.get(key):
            meta[key] = pair[key]
    return {k: v for k, v in meta.items() if v is not None}


# what a derived pair copies from its file pair, besides the two versions of the code and the diff: when none of it and nothing about the splitter changed, splitting again gives the same pairs
SIG_FIELDS = ("commit_key", "cve_id", "cve_ids", "advisory_ids", "ghsa_id", "ecosystem", "package", "cwe", "cvss_vector", "published", "commit_date", "repo_url", "fix_commit", "parent_commit",
              "file_path", "old_path", "language", "is_test", "commit_message", "source", "parent_count", "parent_selection_reason", "quarantine_reason", "before_blob_sha", "after_blob_sha",
              "extraction_version")


def policy_digest(policy: Policy | None = None) -> str:
    return hashlib.blake2b(dumps(dataclasses.asdict(policy or default_policy())).encode("utf-8"), digest_size=8).hexdigest()


def split_signature(file_pair: dict, policy: Policy | None = None) -> str:
    """A digest of everything the result of splitting one stored file pair depends on: the splitter (its version, the parsers, the metadata and context formats, the quality policy) and
    the file pair (both versions of the code, the diff, and the fields the derived pairs inherit). Two splits with one signature give the same pairs, so the second can be skipped; a
    change to any part gives another signature. ``tangled`` is left out: the split itself computes it."""
    from app.backend.pipeline import funcsplit, splitdeps
    h = hashlib.blake2b(digest_size=16)
    head = [str(funcsplit.VERSION), funcsplit.parser_fingerprint(), str(META_VERSION), str(splitdeps.DEPS_VERSION), policy_digest(policy)]
    fields = dumps({k: file_pair.get(k) for k in SIG_FIELDS})
    for part in (*head, fields, file_pair.get("code_before") or "", file_pair.get("code_after") or "", file_pair.get("diff") or ""):
        h.update(part.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
    return h.hexdigest()


def stored_signature(meta) -> str | None:
    """The signature recorded in a file pair's ``extraction_meta`` (a JSON string or a dict), or None."""
    if not meta:
        return None
    try:
        data = json.loads(meta) if isinstance(meta, str) else meta
        sig = data.get("sig")
        return sig if isinstance(sig, str) else None
    except (ValueError, AttributeError, TypeError):
        return None


def file_meta(analysis) -> dict:
    """What is kept beside the file-level pair: the functions added or removed, the changes outside every function, the matches that are not one to one, and the ledger."""
    from app.backend.pipeline import funcsplit

    def short(u: dict) -> dict:
        return {k: u[k] for k in ("side", "name", "kind", "scope", "signature", "lines") if k in u}

    def region(r: dict) -> dict:
        out = {"kind": r["kind"], "before": r.get("before"), "after": r.get("after"), "emitted": r.get("emitted", False), "n_lines": r.get("n_lines")}
        for side in ("before", "after"):
            text = r.get(f"text_{side}") or ""
            if text:
                out[f"text_{side}"] = text[:600]
        return out
    meta = {"v": META_VERSION, "parser": funcsplit.parser_fingerprint(), "coverage": analysis.coverage,
            "unpaired": [short(u) for u in analysis.unpaired[:MAX_LISTED]], "unpaired_total": len(analysis.unpaired),
            "residual": [region(r) for r in analysis.residual[:MAX_LISTED]], "residual_total": len(analysis.residual),
            "links": [{k: l[k] for k in ("kind", "before", "after", "confidence")} for l in analysis.links[:MAX_LISTED]], "links_total": len(analysis.links),
            "noop_regions": len(analysis.noop_regions), "status": analysis.status, "reason": analysis.reason}
    for k in ("grammar_before", "grammar_after", "errors_before", "errors_after", "recovered_regions_before", "recovered_regions_after"):
        if k in analysis.parser:
            meta.setdefault("parse", {})[k] = analysis.parser[k]
    return meta
