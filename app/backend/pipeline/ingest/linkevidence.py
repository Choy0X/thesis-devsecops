"""How well does the evidence support that a commit is *the* fix of an advisory?

The release-date check (``fixcheck``) says a commit is plausible in time. This adds what
the commit itself and the advisory say, as named signals, and a level:

* ``strong``   the advisory declares the link (an OSV ``FIX`` reference or ``GIT`` range, an NVD
  ``Patch`` tag) or the commit message names the advisory (a CVE or GHSA id);
* ``moderate`` at least two of: the message uses security words, the advisory names a changed
  file, the link was recovered (pull request, closing commit, fixed-version range) rather than
  listed;
* ``weak``     only a mention.

The level is evidence, not truth: a ``strong`` link can still point at the wrong commit, and a
``weak`` one at the right commit. The tier gate for training rows uses it next to the
release-date verdict.
"""
from __future__ import annotations

import json
import pathlib
import re

DECLARED = {"osv_git_range", "osv_ref_fix", "nvd_ref_patch"}
_RECOVERED = ("pr_", "issue_", "version_range")
_SECURITY = re.compile(
    r"\b(security|vulnerab\w*|cve-\d{4}-\d+|ghsa-[0-9a-z\-]+|xss|csrf|ssrf|rce|injection|traversal|"
    r"sanitiz\w*|escap(?:e|ing)|redos|denial[ -]of[ -]service|overflow|prototype pollution|"
    r"authenticat\w*|authoriz\w*|bypass|exploit\w*|untrusted|malicious|disclos\w*)\b", re.I)
_GENERIC_STEMS = {"index", "main", "utils", "util", "init", "__init__", "app", "lib", "test", "tests",
                  "readme", "package", "config", "setup", "common", "base", "types", "core"}


def _named_files(advisory: str, files: list[str]) -> bool:
    text = advisory.lower()
    for path in files:
        p = path.lower()
        stem = pathlib.PurePosixPath(p).stem
        if p in text or (len(stem) >= 4 and stem not in _GENERIC_STEMS
                         and re.search(rf"(?<![a-z0-9]){re.escape(stem)}(?![a-z0-9])", text)):
            return True
    return False


def evidence(*, message: str | None, aliases: list[str], advisory: str | None, files: list[str],
             vias: list[str], link: str | None) -> dict:
    """{'level', 'signals'} for one commit."""
    msg = (message or "").lower()
    signals = []
    if link == "declared" or any(v in DECLARED for v in vias):
        signals.append("declared_link")
    if any(a and a.lower() in msg for a in aliases):
        signals.append("message_names_advisory")
    if _SECURITY.search(message or ""):
        signals.append("message_security_words")
    if _named_files(advisory or "", files):
        signals.append("files_named_in_advisory")
    if any(str(v).startswith(_RECOVERED) for v in vias):
        signals.append("recovered_link")
    if "declared_link" in signals or "message_names_advisory" in signals:
        level = "strong"
    elif sum(s in signals for s in ("message_security_words", "files_named_in_advisory", "recovered_link")) >= 2:
        level = "moderate"
    else:
        level = "weak"
    return {"level": level, "signals": signals}


def refresh(commit_keys: list[str] | None = None) -> dict:
    """Compute and store the evidence of mined fix commits (all, or the given keys)."""
    from app.backend.pipeline import codestore, store
    from app.backend.pipeline.ingest import context as ctx
    counts: dict[str, int] = {}
    rows = codestore.commits_by_status("mined")
    if commit_keys is not None:
        want = set(commit_keys)
        rows = [r for r in rows if r["commit_key"] in want]
    for r in rows:
        ids = codestore.records_for_commit(r)
        aliases, texts = set(), []
        for rec in store.records_by_ids(ids, ("osv_id", "cve_id", "aliases", "summary", "details")):
            aliases |= {rec["osv_id"], rec["cve_id"]}
            try:
                aliases |= set(json.loads(rec["aliases"] or "[]"))
            except ValueError:
                pass
            texts.append(ctx.advisory_text(rec["summary"], rec["details"]))
        files = codestore.pair_files_of(r["commit_key"])
        try:
            vias = json.loads(r["via"] or "[]")
        except ValueError:
            vias = []
        out = evidence(message=r.get("commit_message"), aliases=sorted(a for a in aliases if a),
                       advisory="\n".join(texts), files=files, vias=vias, link=r.get("link"))
        codestore.set_link_evidence(r["commit_key"], json.dumps(out["signals"]), out["level"])
        counts[out["level"]] = counts.get(out["level"], 0) + 1
    return counts
