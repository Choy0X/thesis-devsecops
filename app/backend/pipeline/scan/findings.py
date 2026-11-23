"""The shape of what Scan Codebase reports: a finding, a check, and how findings from several tools are put together.

A **check** is one tool (or one rule set) that was asked to look: it says whether it ran, how far, and what it found, so that "no finding" can always be read with the list of checks that
produced it. A check's state is ``ran``, ``partial`` (it ran but could not read some files), ``env_error`` (it did not run to its end: missing, timed out, killed, or its output could not
be understood) or ``skipped`` (it was not attempted, with the reason: nothing to run it on, or a setting the project does not have). An ``env_error`` or a ``skipped`` check is never counted as a
clean result.

A **finding** is one thing a check reports about one place. Its ``state`` is one of:

* ``vulnerable``: the weakness is present as a fact, with evidence (a secret that is written in a file; later, a probe or a proof-of-concept that reproduced the behaviour);
* ``inconclusive``: a static tool flagged the place, nothing has confirmed it yet (a pattern or a data-flow rule is not proof that the program can be attacked this way);
* ``no_finding`` and ``env_error`` are states of a *target* in the report (``target_states``), not of a finding: a target with no finding by the checks that ran says "no finding by <those
  checks>", and a target some check could not look at says ``env_error``.

Severity always travels with its source: ``tool`` (the tool's own rating), ``rule`` (this app's policy for the kind of finding) or ``unknown``. A severity a tool does not give is
``unknown``, never a default that looks measured.
"""
from __future__ import annotations

import hashlib
import json

from app.backend.pipeline.scan import code_rules

STATES = ("vulnerable", "inconclusive")
TARGET_STATES = ("vulnerable", "inconclusive", "no_finding", "env_error")
CHECK_STATES = ("ran", "partial", "env_error", "skipped")
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
SEVERITY_SOURCES = ("tool", "rule", "unknown")
_RANK = {s: i for i, s in enumerate(("unknown", "low", "medium", "high", "critical"))}
# The kinds of weakness a person can read at a glance, grouped from CWE numbers by this app (not by the tools): the radar of a scan and its table count findings by these. A finding is
# put under the first of its CWE numbers that is listed here; one with no CWE, or none of these, is "Other".
TYPES = [
    ("Injection", {77, 78, 88, 89, 90, 91, 94, 95, 96, 98, 917}),
    ("Cross-site scripting", {79, 80, 83, 87}),
    ("Access and paths", {22, 23, 36, 73, 284, 285, 287, 306, 352, 425, 434, 639, 862, 863, 918}),
    ("Cryptography", {256, 257, 261, 296, 310, 311, 312, 319, 322, 323, 324, 325, 326, 327, 328, 329, 330, 331, 335, 338, 347, 759, 760, 916}),
    ("Secrets", {259, 321, 522, 523, 532, 798}),
    ("Unsafe data handling", {20, 502, 611, 776, 915, 1321}),
    ("Exposure and configuration", {16, 200, 201, 209, 215, 497, 538, 548, 693, 942, 1004, 1021, 1275}),
    ("Resource exhaustion", {400, 409, 674, 770, 834, 1333}),
]
OTHER_TYPE = "Other"
TYPE_NAMES = [name for name, _ in TYPES] + [OTHER_TYPE]
SNIPPET_CHARS = 240
MESSAGE_CHARS = 400


def finding_type(cwe: list[str] | tuple) -> str:
    """The kind of weakness for a finding's CWE numbers (``["CWE-95"]`` is Injection), or ``Other``."""
    for c in cwe:
        try:
            number = int(str(c).upper().replace("CWE-", "").strip())
        except ValueError:
            continue
        for name, numbers in TYPES:
            if number in numbers:
                return name
    return OTHER_TYPE


def finding(*, tool: str, rule: str, file: str, line: int | None, message: str, state: str, severity: str = "unknown", severity_source: str = "unknown", basis: str = "static",
            cwe: list[str] | tuple = (), snippet: str = "", confidence: str | None = None, evidence: dict | None = None, extra: dict | None = None) -> dict:
    """One finding in the report's shape. The id is stable for the same tool, rule and place, so a second scan can say what is new and what is gone."""
    if state not in STATES:
        raise ValueError(f"a finding is {STATES}, not {state!r}")
    if severity not in SEVERITIES or severity_source not in SEVERITY_SOURCES:
        raise ValueError("unknown severity or severity source")
    if severity == "unknown":
        severity_source = "unknown"
    fid = hashlib.sha256(f"{tool}|{rule}|{file}|{line}".encode("utf-8")).hexdigest()[:12]
    out = {"id": fid, "tool": tool, "sources": [tool], "rule": rule, "state": state, "basis": basis, "severity": severity, "severity_source": severity_source, "file": file, "line": line,
           "cwe": sorted({c for c in cwe if c}), "type": finding_type(sorted({c for c in cwe if c})), "message": code_rules.masked(message or "", MESSAGE_CHARS), "snippet": code_rules.masked(snippet or "", SNIPPET_CHARS), "confidence": confidence,
           "evidence": evidence}
    if extra:
        out["extra"] = extra
    return out


def check(*, tool: str, state: str, rule_set: str = "", version: str | None = None, files: int | None = None, findings: int = 0, note: str = "", evidence: str | None = None,
          sha256: str | None = None, problems: list[str] | None = None, extra: dict | None = None) -> dict:
    """One check: what looked, how far it got, and where the raw output is."""
    if state not in CHECK_STATES:
        raise ValueError(f"a check is {CHECK_STATES}, not {state!r}")
    out = {"tool": tool, "state": state, "rule_set": rule_set, "version": version, "files": files, "findings": findings, "note": note, "evidence": evidence, "sha256": sha256,
           "problems": [code_rules.masked(p, 300) for p in (problems or [])[:20]]}
    if extra:
        out.update(extra)
    return out


def merge(findings: list[dict]) -> list[dict]:
    """One finding per place and weakness: findings of different tools on the same file and line that share a CWE (or that both name none) are one finding that lists every source. The
    merged finding keeps the strongest state and the highest severity that has a known source; where the sources disagree on severity, the higher stands and ``severity_by`` says whose."""
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for f in findings:
        key = (f["file"], f["line"], tuple(f["cwe"][:1]) if f["cwe"] else ())
        if key not in merged:
            merged[key] = {**f, "sources": list(f["sources"]), "rules": [f["rule"]]}
            order.append(key)
            continue
        m = merged[key]
        m["sources"] = sorted(set(m["sources"]) | set(f["sources"]))
        m["rules"] = sorted(set(m["rules"]) | {f["rule"]})
        m["cwe"] = sorted(set(m["cwe"]) | set(f["cwe"]))
        if m["type"] == OTHER_TYPE:
            m["type"] = f["type"]
        if f["state"] == "vulnerable":
            m["state"] = "vulnerable"
        if _RANK[f["severity"]] > _RANK[m["severity"]]:
            m["severity"], m["severity_source"], m["severity_by"] = f["severity"], f["severity_source"], f["tool"]
        m["evidence"] = m["evidence"] or f["evidence"]
        m["snippet"] = m["snippet"] or f["snippet"]
    return [merged[k] for k in order]


def summarize(findings: list[dict], checks: list[dict]) -> dict:
    """The counts a report opens with, and the plain words about what the checks cover."""
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    states = {s: sum(1 for f in findings if f["state"] == s) for s in STATES}
    broken = [c["tool"] for c in checks if c["state"] == "env_error"]
    skipped = [c["tool"] for c in checks if c["state"] == "skipped"]
    partial = [c["tool"] for c in checks if c["state"] == "partial"]
    ran = [c["tool"] for c in checks if c["state"] in ("ran", "partial")]
    if findings:
        words = f"{len(findings)} findings from {', '.join(ran) or 'no tool'}."
        if broken:
            words += f" {', '.join(broken)} did not run to its end, so the code was not fully checked."
    elif ran and not broken:
        words = f"No finding by {', '.join(ran)}. That is what these checks found, not a statement that the code is safe."
    elif ran:
        words = f"No finding by {', '.join(ran)}, but {', '.join(broken)} did not run to its end, so the code was not fully checked."
    else:
        words = "No check ran to its end, so nothing can be said about the code."
    if partial:
        words += f" {', '.join(partial)} could not read every file."
    if skipped:
        words += f" Not attempted: {', '.join(skipped)}."
    by_type = {name: sum(1 for f in findings if f.get("type", OTHER_TYPE) == name) for name in TYPE_NAMES}
    return {"findings": len(findings), "by_severity": counts, "by_state": states, "by_type": by_type, "checks_ran": ran, "checks_partial": partial, "checks_failed": broken, "checks_skipped": skipped, "words": words}


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
