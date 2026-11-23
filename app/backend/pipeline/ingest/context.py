"""How much real context a code pair carries - which pairs the LLM should enrich.

A training sample is more useful when the text around the code says what is wrong.
That text is real when it comes from the advisory (the record's summary and
details) or the developer's fix message; the optional LLM enrichment drafts it
where it is missing. This module rates the **real** context of a pair (the
``*_llm`` fields are never looked at):

=============  ============================================================
signal         rule
=============  ============================================================
advisory       ``full``: summary + details >= ``min_advisory`` characters;
               ``short``: shorter; ``none``: empty
commit_msg     ``informative``; ``generic`` (merge lines skipped, the rest
               under 12 characters, or bump / release / version / typo / wip
               / a bare "fix #123"); ``none``
cwe            a CWE id is known (NVD-CWE-Other / NVD-CWE-noinfo do not count)
severity       a CVSS-derived severity is known
located        the advisory or the commit message names the function or file
=============  ============================================================

Level: **weak** - neither a full advisory nor an informative commit message;
**strong** - a full advisory, an informative message or a named location, and a
CWE; **partial** - everything else.
"""
from __future__ import annotations
import pathlib
import re
from dataclasses import dataclass

MIN_ADVISORY = 200
LEVELS = ("weak", "partial", "strong")
MISSING = {                 # key -> label (the UI and the logs use the labels)
    "advisory": "no or short advisory",
    "commit_msg": "no informative commit message",
    "cwe": "no CWE",
    "severity": "no severity",
    "located": "location not named",
}
# a merge line says nothing; what follows it may
_MERGE = re.compile(r"^\s*merge (pull request|branch|remote[- ]tracking branch|tag)\b", re.I)
_GENERIC = re.compile(
    r"^\s*((bump|release|prepare (for )?release|update (changelog|readme|version|deps|dependencies)"
    r"|typo|wip|minor|cleanup|clean up|lint|format|style)\b"
    r"|(v?\d+(\.\d+)+|(fix(es|ed)?|closes?|resolves?)\s*(#?\d+)?)\s*\.?\s*$)", re.I)


def _s(v) -> str:
    """Text or "" (None and pandas NaN are missing)."""
    return v.strip() if isinstance(v, str) else ""


def advisory_text(summary, details, fallback=None) -> str:
    """The one normaliser of real advisory text (every other place calls this): each part
    is stripped at its edges, a summary that the details repeat is dropped, the parts are
    joined with a blank line, and ``fallback`` (a stored copy) is used only when the
    record has no text of its own. Inner whitespace is kept and counts as characters."""
    summary, details = _s(summary), _s(details)
    if summary and details and details.startswith(summary):
        summary = ""
    return "\n\n".join(x for x in (summary, details) if x) or _s(fallback)


EXPLANATION_SOURCES = ("advisory", "llm_fallback", "short_advisory", "missing")


@dataclass(frozen=True)
class Explanation:
    """The one canonical explanation of an example and where it came from."""
    text: str | None
    source: str                      # advisory | llm_fallback | short_advisory | missing
    synthetic: bool                  # True only for llm_fallback (then 'explanation' is a synthetic field)
    needs_enrichment: bool           # no adequate explanation exists yet
    advisory: str                    # the normalised real advisory text (may be empty)
    original_advisory_reference: str | None   # real short text kept when a fallback replaced it


def resolve_explanation(summary, details, stored=None, fallback=None,
                        min_advisory: int = MIN_ADVISORY) -> Explanation:
    """The agreed explanation policy, a provenance rule and not a quality score.

    ``advisory = advisory_text(...)``. At ``min_advisory`` characters (200) or more the
    real advisory is the explanation, unchanged, and never marked synthetic. Below that
    an accepted LLM fallback (``fallback``, passed only when one exists) takes its place
    and is marked synthetic, with the short real text kept as a reference. With no
    fallback the available real short text (or nothing) is kept as is, never
    fabricated, and the row is marked as needing enrichment; that never removes its
    other training uses."""
    advisory = advisory_text(summary, details, stored)
    if advisory and len(advisory) >= int(min_advisory):
        return Explanation(advisory, "advisory", False, False, advisory, None)
    draft = _s(fallback)
    if draft:
        return Explanation(draft, "llm_fallback", True, False, advisory, advisory or None)
    if advisory:
        return Explanation(advisory, "short_advisory", False, True, advisory, None)
    return Explanation(None, "missing", False, True, "", None)


def commit_quality(msg) -> str:
    lines = [l.strip() for l in _s(msg).splitlines() if l.strip()]
    while lines and _MERGE.match(lines[0]):
        lines = lines[1:]                     # "Merge pull request #12 from x/y"
    if not lines:
        return "generic" if _s(msg) else "none"
    first, body = lines[0], " ".join(lines)
    if len(body) < 12:
        return "generic"
    if _GENERIC.match(first) and len(body) < 60:
        return "generic"
    return "informative"


def _has_cwe(cwe) -> bool:
    return any(c.strip().startswith("CWE-") for c in _s(cwe).replace(",", ";").split(";"))


def _names(file_path, function) -> list[str]:
    out = []
    if _s(function):
        for part in re.split(r"[.:#]+", _s(function)):
            if len(part) >= 4 and part not in ("prototype", "self", "this"):
                out.append(part)
    if _s(file_path):
        stem = pathlib.PurePosixPath(_s(file_path)).stem
        if len(stem) >= 4 and stem.lower() not in ("index", "main", "utils", "util", "init", "__init__"):
            out.append(stem)
    return out


def assess(advisory: str | None, commit_message: str | None, cwe, severity,
           file_path=None, function=None, min_advisory: int = MIN_ADVISORY) -> dict:
    """{advisory, commit_msg, cwe, severity, located, level, missing[]}."""
    adv = _s(advisory)
    a = "full" if len(adv) >= int(min_advisory or 0) and adv else ("short" if adv else "none")
    m = commit_quality(commit_message)
    text = f"{adv}\n{_s(commit_message)}".lower()
    located = any(n.lower() in text for n in _names(file_path, function))
    has_cwe, has_sev = _has_cwe(cwe), bool(_s(severity))
    if a != "full" and m != "informative":
        level = "weak"
    elif a == "full" and (m == "informative" or located) and has_cwe:
        level = "strong"
    else:
        level = "partial"
    missing = [k for k, ok in (("advisory", a == "full"), ("commit_msg", m == "informative"),
                               ("cwe", has_cwe), ("severity", has_sev), ("located", located)) if not ok]
    return {"advisory": a, "commit_msg": m, "cwe": has_cwe, "severity": has_sev,
            "located": located, "level": level, "missing": missing}


def assess_row(r: dict, min_advisory: int = MIN_ADVISORY) -> dict:
    """``assess`` for a dataset-builder row (record text + pair location)."""
    return assess(advisory_text(r.get("summary"), r.get("details")),
                  r.get("msg_head", r.get("commit_message")), r.get("cwe"), r.get("severity"),
                  r.get("file_path"), r.get("function_name"), min_advisory)
