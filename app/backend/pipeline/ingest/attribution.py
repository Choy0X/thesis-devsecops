"""Which changed unit of a fix commit is the security-relevant one?

A fix commit often changes several functions; labelling every one of them vulnerable gives a precision ceiling of about one in three (a study of Go found 22%). This attributes each pair
with deterministic signals, no model involved. The rules follow PrimeVul: a unit is the vulnerable one when it is the only changed unit (ONEFUNC) or when the advisory text names it
(NVDCHECK); the commit message is a weaker witness.

* ``sole_changed_unit``    the only changed production unit of the commit (1.0);
* ``advisory_names_unit``  the advisory names the function, or names the file of a unit that is the only one changed in that file (0.8);
* ``message_names_unit``   the commit message does the same (0.6);
* ``advisory_names_file``  the advisory names only the file, and several units changed in it, so which one is not known (0.5, supporting tier);
* ``message_names_file``   the same for the commit message (0.4, supporting tier);
* ``none``                 no signal (0.0): the unit is a change in a fix commit, not known to be the vulnerable one. It stays usable as context and as a repair target, but is not
  a decision-positive label unless a review says so;
* ``not_applicable_test``  test pairs are never attributed (0.0).

Beside the basis each pair gets ``attribution_signals``, the evidence that was found (``advisory_names_function``, ``advisory_names_path``, ``message_names_function``,
``message_names_path``, ``test_references_unit``). A test of the same commit that names the unit is recorded, but lifts nothing: on the real store such a test names the unit for
15% of the units no other signal reaches and 29% of the advisory-named ones, which is corroboration, not proof.

Short and generic names (``get``, ``init``, ``run``, ``main``) never match: they appear in any text.
"""
from __future__ import annotations

import collections
import pathlib
import re

from app.backend.pipeline.ingest import pairquality

GENERIC = {"get", "set", "init", "__init__", "run", "main", "test", "tests", "index", "util", "utils", "handle",
           "handler", "call", "apply", "create", "update", "delete", "list", "load", "read", "write", "open",
           "close", "start", "stop", "process", "execute", "check", "build", "setup", "name", "data", "value",
           "config", "base", "common", "types", "core", "app", "lib", "package", "readme"}

# Which basis counts for what. ``decision``: strong enough that the review gate treats the unit as the vulnerable one. ``supporting``: the advisory or the message points at the file, not at
# the unit; usable as context and as a repair target, never auto-labelled. ``none``: no tie to the advisory, or a test. The scores are an ordinal ranking, not probabilities. The scores
# the embedded document mapper (``db/mappers.py``) and the gate (``review.ATTRIBUTED``, the decision tier) all derive from this table; a basis is in exactly one tier.
TIERS = {
    "decision": {"sole_changed_unit": 1.0, "advisory_names_unit": 0.8, "message_names_unit": 0.6},
    "supporting": {"advisory_names_file": 0.5, "message_names_file": 0.4},
    "none": {"none": 0.0, "not_applicable_test": 0.0},
}
SCORES = {basis: score for tier in TIERS.values() for basis, score in tier.items()}
SIGNALS = ("advisory_names_function", "advisory_names_path", "message_names_function", "message_names_path", "test_references_unit")


def tier_of(basis: str | None) -> str:
    """The tier a basis belongs to; an unknown or missing basis counts as no attribution."""
    return next((name for name, bases in TIERS.items() if basis in bases), "none")


def _mentions(text: str, token: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text, re.I))


def _function_names(pair: dict) -> list[str]:
    name = pair.get("function_name")
    if not name:
        return []
    return [part for part in {name.split("#")[0], *re.split(r"[.:#]+", name)} if len(part) >= 4 and part.lower() not in GENERIC and not part.startswith("__")]


def _path_names(pair: dict) -> list[str]:
    path = pair.get("file_path") or ""
    if not path:
        return []
    out = [path]
    stem = pathlib.PurePosixPath(path).stem
    if not pair.get("function_name") and len(stem) >= 4 and stem.lower() not in GENERIC:
        out.append(stem)
    return out


def _added_lines(diff: str | None) -> str:
    return "\n".join(l[1:] for l in (diff or "").split("\n") if l.startswith("+") and not l.startswith("+++"))


def assign(pairs: list[dict], advisory: str | None, message: str | None) -> None:
    """Set ``attribution_basis``, ``attribution_score`` and ``attribution_signals`` on every pair of one commit. Test pairs may carry their ``diff``: the added lines are searched
    for the names of the units."""
    advisory, message = advisory or "", message or ""
    has_sub = {p.get("file_path") for p in pairs
               if p.get("granularity") in ("function", "hunk") and p.get("code_available", 1)}
    units = [p for p in pairs if not p.get("is_test") and p.get("code_available", 1) and p.get("match_kind") != "residual"       # a change outside every function is not a rival of the function
             and (p.get("granularity") in ("function", "hunk")
                  or (p.get("granularity") == "file" and p.get("file_path") not in has_sub))]
    sole = units[0] if len(units) == 1 else None
    per_file = collections.Counter(p.get("file_path") for p in units)
    in_tests = "\n".join(_added_lines(p.get("diff")) for p in pairs if p.get("is_test"))
    for p in pairs:
        if not p.get("code_available", 1):
            continue                                  # a skipped file has no code to attribute
        found: set[str] = set()
        if p.get("is_test"):
            basis = "not_applicable_test"
        else:
            functions, paths = _function_names(p), _path_names(p)
            if any(_mentions(advisory, n) for n in functions):
                found.add("advisory_names_function")
            if any(_mentions(advisory, n) for n in paths):
                found.add("advisory_names_path")
            if any(_mentions(message, n) for n in functions):
                found.add("message_names_function")
            if any(_mentions(message, n) for n in paths):
                found.add("message_names_path")
            if in_tests and any(_mentions(in_tests, n) for n in functions):
                found.add("test_references_unit")
            alone = p in units and per_file[p.get("file_path")] == 1     # the only unit changed in its file: naming the file names the unit
            if p is sole:
                basis = "sole_changed_unit"
            elif "advisory_names_function" in found or ("advisory_names_path" in found and alone):
                basis = "advisory_names_unit"
            elif "message_names_function" in found or ("message_names_path" in found and alone):
                basis = "message_names_unit"
            elif "advisory_names_path" in found:
                basis = "advisory_names_file"
            elif "message_names_path" in found:
                basis = "message_names_file"
            else:
                basis = "none"
        p["attribution_basis"], p["attribution_score"] = basis, SCORES[basis]
        p["attribution_signals"] = [s for s in SIGNALS if s in found]


def refresh(commit_keys: list[str] | None = None, only_missing: bool = True) -> dict:
    """Attribute the stored pairs of the given commits (all commits by default), and store what was found beside each pair (``pair_quality``). A commit is done when every usable pair has a
    label and a quality row of the current version, so a pass that stops can be started again and goes on where it was, and a change of the rules (``QUALITY_VERSION``) covers every commit again."""
    from app.backend.pipeline import codestore
    counts: dict[str, int] = {}
    keys = codestore.attribution_commit_keys(only_missing, version=pairquality.QUALITY_VERSION)
    if commit_keys is not None:
        wanted = set(commit_keys)
        keys = [k for k in keys if k in wanted]
    for k in keys:
        rows = codestore.pairs_for_attribution(k, with_diff=True)
        advisory = next((r["explanation"] for r in rows if r["explanation"]), "")
        message = next((r["commit_message"] for r in rows if r["commit_message"]), "")
        assign(rows, advisory, message)
        codestore.set_attribution([(r["attribution_basis"], r["attribution_score"], r["pair_key"]) for r in rows])
        codestore.set_pair_quality(pairquality.rows(rows))
        for r in rows:
            counts[r["attribution_basis"]] = counts.get(r["attribution_basis"], 0) + 1
    return counts
