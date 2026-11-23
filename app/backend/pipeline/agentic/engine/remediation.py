"""Protect regression tests recorded in existing project histories."""
from __future__ import annotations

import posixpath


def mark_stale(task_doc: dict) -> None:
    """Files changed after a verification: what it verified is no longer what is there. The record stays (it happened) and says so."""
    for r in task_doc.get("remediations") or []:
        if r.get("status") == "verified":
            r["stale"] = True


def _rel_path(path: str) -> str:
    p = posixpath.normpath(str(path or "").replace("\\", "/").strip())
    return "" if p == "." else p.lstrip("/")


def frozen_refusal(task_doc: dict, path: str) -> str | None:
    """The sentence that refuses a write to a test that a verified remediation froze, or None."""
    norm = _rel_path(path)
    for f in task_doc.get("frozen") or []:
        if _rel_path(f["path"]) == norm:
            return (f"Not allowed: {norm} is the regression test that verify_fix verified, and changing it would change what was checked. Write a new test file instead "
                    "if the test has to change.")
    return None
