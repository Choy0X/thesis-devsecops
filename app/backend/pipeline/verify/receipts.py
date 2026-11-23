"""Legacy verification receipt format and self-consistency checks. No docker, no torch.

Version 1 is unauthenticated historical evidence. Recomputing its hashes can make
imported JSON self-consistent without running anything. New automatic admission
requires ``verify.automatic.trusted_receipt_for`` and its backend-owned completed
execution journal. Reading or attaching a v1 receipt never upgrades it to v2.

A receipt records one verification of one subject (a patch) against one case: the patch and the trees it was applied to (by hash), six
steps (the patch applied, the code built, the security check failed *before* and passed *after*, the functionality still passes, the
acceptance tests and the verifier were not touched), the commands (by hash), and the runner with its limits. The receipt id is the hash
of its content; ``validate`` recomputes the id, the verdict and the grade, so an edited receipt is detected, and a docker receipt must
carry the image digest and the hardening it ran under.

* ``verified``: every step as required, with the vulnerability reproduced before the patch. Anything else is ``not_verified``, and any
  environment failure (a timeout, no daemon, a refused fixture) is ``env_error``: never a success, never a plain failure.
* Grade G2: the vulnerability reproduced before and gone after, the patch applied, built and left the verifier alone. G3: and the
  functionality still passes. A feature-disabling patch is G2 at best and never ``verified``.
* A receipt of the in-process fixture runner is marked ``fixture_only`` and can never be attached to a real record.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import pathlib
import re

SCHEMA = "verify-receipt/1"
STEP_NAMES = ("apply", "build", "security_before", "security_after", "functionality", "integrity")
STEP_STATUSES = ("passed", "failed", "not_run", "env_error")
RUNNER_KINDS = ("docker", "fixture_inprocess")
_HEX = re.compile(r"^[0-9a-f]{64}$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _files(root: pathlib.Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and "__pycache__" not in rel.split("/") and not rel.endswith(".pyc"):
            yield rel, p


def tree_sha256(path, *, normalize_newlines: bool = False, ignore=()) -> str:
    """Hash of a folder: every file's relative path and content. With ``normalize_newlines`` CRLF and LF are the same. Files whose path matches a pattern of ``ignore`` are left out."""
    h = hashlib.sha256()
    for rel, p in _files(pathlib.Path(path)):
        if ignore and is_protected(rel, ignore):
            continue
        data = p.read_bytes()
        if normalize_newlines:
            data = data.replace(b"\r\n", b"\n")
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def commands_sha256(case) -> str:
    """Hash of the exact command templates of a case (build, security, functionality) and its image."""
    return sha256_text(json.dumps({"build": list(case.build), "security": list(case.security),
                                   "functionality": list(case.functionality), "image": case.image}, sort_keys=True))


def is_protected(path: str, patterns) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


# -- verdict and grade ---------------------------------------------------------------------------------------------------------------------------

def verdict_of(steps: dict) -> tuple[str, list[str]]:
    st = {n: steps[n]["status"] for n in STEP_NAMES}
    env = [n for n, s in st.items() if s == "env_error"]
    if env:
        return "env_error", [f"{n}: environment failure" for n in env]
    reasons = []
    if st["security_before"] == "passed":
        reasons.append("security_before: the vulnerability was not reproduced before the patch")
    elif st["security_before"] != "failed":
        reasons.append(f"security_before: {st['security_before']}")
    reasons += [f"{n}: {st[n]}" for n in ("apply", "build", "security_after", "functionality", "integrity") if st[n] != "passed"]
    return ("verified", []) if not reasons else ("not_verified", reasons)


def grade_of(steps: dict) -> str:
    st = {n: steps[n]["status"] for n in STEP_NAMES}
    fixed = (st["apply"] == "passed" and st["build"] == "passed" and st["integrity"] == "passed"
             and st["security_before"] == "failed" and st["security_after"] == "passed")
    if not fixed:
        return "none"
    return "G3" if st["functionality"] == "passed" else "G2"


# -- the receipt ---------------------------------------------------------------------------------------------------------------------------------

def receipt_id_of(receipt: dict) -> str:
    body = {k: v for k, v in receipt.items() if k != "receipt_id"}
    return sha256_text(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str))


def new_receipt(*, case_id: str, subject_sha256: str, tree_before: str, tree_after: str, acceptance_sha256: str, steps: dict,
                runner: dict, commands_sha256: str) -> dict:
    verdict, reasons = verdict_of(steps)
    body = {"schema": SCHEMA, "case_id": case_id, "subject_sha256": subject_sha256, "tree_before": tree_before, "tree_after": tree_after,
            "acceptance_sha256": acceptance_sha256, "commands_sha256": commands_sha256, "steps": steps, "runner": runner,
            "verdict": verdict, "reasons": reasons, "grade": grade_of(steps)}
    body["receipt_id"] = receipt_id_of(body)
    return body


def _docker_problems(runner: dict, *, digest_required: bool) -> list[str]:
    out = []
    if digest_required and not str(runner.get("image_digest") or "").startswith("sha256:"):
        out.append("runner.image_digest: a docker receipt needs the image digest")
    if runner.get("fixture_only") is not False:
        out.append("runner.fixture_only must be false for a docker receipt")
    lim = runner.get("limits") or {}
    want = {"network": "none", "non_root": True, "read_only_root": True, "cap_drop": ["ALL"], "no_new_privileges": True,
            "acceptance_read_only": True}
    out += [f"runner.limits.{k} must be {v!r}, got {lim.get(k)!r}" for k, v in want.items() if lim.get(k) != v]
    out += [f"runner.limits.{k} must be a positive number" for k in ("pids_limit", "memory_mb", "cpus", "timeout_seconds")
            if not isinstance(lim.get(k), (int, float)) or lim.get(k) <= 0]
    return out


def validate(receipt: dict) -> list[str]:
    """What is wrong with a receipt (empty when it is sound): the schema, the hashes, the steps, the recomputed verdict and grade and id."""
    if receipt.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"]
    out = []
    for key in ("subject_sha256", "tree_before", "tree_after", "acceptance_sha256", "commands_sha256"):
        if not _HEX.match(str(receipt.get(key) or "")):
            out.append(f"{key} must be a sha256 hex digest")
    steps = receipt.get("steps") or {}
    if set(steps) != set(STEP_NAMES):
        out.append(f"steps must be exactly {list(STEP_NAMES)}, got {sorted(steps)} (an unknown or missing step)")
    elif any((steps[n] or {}).get("status") not in STEP_STATUSES for n in STEP_NAMES):
        out.append(f"every step status must be one of {STEP_STATUSES}")
    else:
        verdict, reasons = verdict_of(steps)
        if receipt.get("verdict") != verdict or receipt.get("reasons") != reasons:
            out.append(f"verdict is {receipt.get('verdict')!r} but the steps give {verdict!r}")
        if receipt.get("grade") != grade_of(steps):
            out.append(f"grade is {receipt.get('grade')!r} but the steps give {grade_of(steps)!r}")
    runner = receipt.get("runner") or {}
    if runner.get("kind") not in RUNNER_KINDS:
        out.append(f"runner.kind must be one of {RUNNER_KINDS}")
    elif runner["kind"] == "docker":
        out += _docker_problems(runner, digest_required=receipt.get("verdict") != "env_error")      # no digest when docker never ran
    elif runner.get("fixture_only") is not True:
        out.append("runner.fixture_only must be true for a fixture receipt")
    if receipt.get("receipt_id") != receipt_id_of(receipt):
        out.append("receipt_id does not match the content (the receipt was edited)")
    return out


def attempt_fields(receipt: dict) -> dict:
    """The fields of a repair attempt in the shared evaluator: only a verified receipt is a success."""
    verification = {"verified": "passed", "not_verified": "failed", "env_error": "env_error"}[receipt["verdict"]]
    return {"verification": verification, "receipt_id": receipt["receipt_id"]}


def attach(record: dict, receipt: dict) -> dict:
    """A copy of a reviewed record with the verification fields of a receipt. A fixture receipt, a receipt of another record or an
    invalid receipt is refused."""
    problems = validate(receipt)
    if problems:
        raise ValueError("invalid receipt: " + "; ".join(problems))
    if receipt["runner"]["kind"] == "fixture_inprocess":
        raise ValueError("a fixture receipt cannot be attached to a real record")
    if receipt["case_id"] != record.get("record_id"):
        raise ValueError(f"the receipt is for record {receipt['case_id']!r}, not {record.get('record_id')!r}")
    s = receipt["steps"]
    out = copy.deepcopy(record)
    out.update(before_security_result=s["security_before"]["status"], after_security_result=s["security_after"]["status"],
               functionality_result=s["functionality"]["status"], verifier_integrity_result=s["integrity"]["status"],
               environment_digest=receipt["runner"]["image_digest"],
               verification_receipt_ids=sorted({*(record.get("verification_receipt_ids") or []), receipt["receipt_id"]}),
               test_artifact_hashes=sorted({*(record.get("test_artifact_hashes") or []), receipt["acceptance_sha256"]}))
    if receipt["verdict"] == "env_error":
        out["environment_failure_reason"] = "; ".join(receipt["reasons"])
    return out
