"""Patch candidates for the findings of a project (P3f.7.2): a model proposes a minimal diff for the file a finding is about, the diff is checked and applied to a temporary copy of that one
file, the project's own tests are run before and after in the closed container of ``sandbox_run``, and everything is stored under the project as a *candidate* for a person to review.

What a candidate is, and what it is not:

* it is never applied to the project's folders here, never called correct, safe or ready to merge, and its receipt state is ``not_verified``: the verification of ``pipeline/verify`` needs a
  test that reproduces the weakness before the patch and passes after it, and a finding found by a scanner has none. The record says that in words;
* the model sees only the lines around the finding (and the finding's words), and only when it is on this computer or the person allowed sending to it; the diff may name only the finding's own
  file, must not create or change a link, and is limited in size;
* the diff is applied by ``git apply`` to a temporary folder that holds only that file, so nothing else of the host is reachable, and the patched text is what is put into the container;
* the tests run in the container with no network and no mount of the host: the baseline first, then after the file was replaced. If the tool image or Docker is missing, or the project has
  no Python tests, the steps say ``not_run`` with the reason, and the candidate is still stored.

Stored as ``data/projects/<id>/candidates/<candidate id>/candidate.json`` and ``patch.diff``, with every step in the project's audit log.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

from app.backend import sandbox, sandbox_files
from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.scan import code_rules, dynamic, sandbox_run

SCHEMA = "patch-candidate/1"
STAGING_SCHEMA = "patch-staging-checks/1"
STEPS = ("target", "model", "diff", "apply", "tests_before", "tests_after")
MAX_FILE_BYTES = 200_000
CONTEXT_LINES = 60
MAX_DIFF_LINES = 200
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
_FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
_DIFF_HEADER = re.compile(r"^--- .+\n\+\+\+ .+$", re.M)


class CandidateError(ValueError):
    """Why a candidate cannot be made, in a sentence for the person."""


def _step(status: str, detail: str | None = None) -> dict:
    return {"status": status, "detail": detail}


def _unsafe(path: str) -> bool:
    parts = pathlib.PurePosixPath(path.replace("\\", "/")).parts
    return path.startswith(("/", "\\")) or ".." in parts or (len(path) > 1 and path[1] == ":")


# -- the file ----------------------------------------------------------------------------------------------------------------------------------------------------

def locate(project: dict, finding: dict) -> tuple[int, str, pathlib.Path]:
    """``(index of the folder, path inside it, full path)`` of the file a finding is about. A finding of a project with several folders is named ``<n>-<folder>/path`` by the scan."""
    name = str(finding.get("file") or "")
    if not name or name.startswith("(") or _unsafe(name):
        raise CandidateError("This finding is not about a file of the project, so there is nothing to patch.")
    folders = project.get("folders") or []
    index, rel = 0, name
    if len(folders) > 1:
        m = re.match(r"^(\d+)-[^/]+/(.+)$", name)
        if not m or not 1 <= int(m.group(1)) <= len(folders):
            raise CandidateError("The finding does not say which of the project's folders its file is in.")
        index, rel = int(m.group(1)) - 1, m.group(2)
    if not folders:
        raise CandidateError("The project has no source folder.")
    base = pathlib.Path(folders[index]["path"])
    full = base / pathlib.PurePosixPath(rel)
    if sandbox_run.is_link(full) or not full.is_file():
        raise CandidateError(f"{rel} is not a plain file of the project.")
    if os.path.commonpath([os.path.realpath(base), os.path.realpath(full)]) != os.path.realpath(base):
        raise CandidateError(f"{rel} leads out of the project's folder.")
    if full.stat().st_size > MAX_FILE_BYTES:
        raise CandidateError(f"{rel} is larger than {MAX_FILE_BYTES // 1000} KB, which is more than a candidate is made for.")
    return index, rel, full


def read_text(full: pathlib.Path, rel: str) -> str:
    try:
        return full.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise CandidateError(f"{rel} is not UTF-8 text.") from None


def excerpt(text: str, line: int | None) -> tuple[str, int]:
    """``(the lines around the finding, the number of the first one)``."""
    lines = text.splitlines()
    center = max(1, min(int(line or 1), len(lines) or 1))
    lo, hi = max(1, center - CONTEXT_LINES), min(len(lines), center + CONTEXT_LINES)
    return "\n".join(lines[lo - 1:hi]), lo


# -- the model ---------------------------------------------------------------------------------------------------------------------------------------------------

def extract_diff(reply: str) -> str:
    """The unified diff in a model's reply. A small model may put an empty or labelled fence in front of the diff (seen for real with a 0.5B model, which answered with a fence that held
    the words "diff code block" and then the diff), so the first fenced block that looks like a diff is taken; failing that, the text from the first ``---`` / ``+++`` header on; failing
    that, what the research baseline's extractor takes (the first fence, or the whole reply), which the diff check then refuses if it is not a diff."""
    from app.backend.pipeline.patch import generate
    text = reply or ""
    for block in _FENCE.findall(text):
        if _DIFF_HEADER.search(block) and re.search(r"^@@ ", block, re.M):
            return block.strip()
    found = _DIFF_HEADER.search(text)
    if found:
        return text[found.start():].split("```", 1)[0].strip()
    return generate.extract_diff(text)


def ask_for_diff(finding: dict, rel: str, text: str, *, allow_remote: bool = False) -> tuple[str, dict]:
    """``(the diff the model proposed, who proposed it)``. Raises ``CandidateError`` when no model is configured, when it is not on this computer and sending was not allowed, or when it
    fails or returns nothing."""
    from app.backend.pipeline import llm_client, project_chat
    from app.backend.pipeline.patch import generate
    where = project_chat.endpoint()
    if not where["configured"]:
        raise CandidateError("No language model is configured, so no patch can be proposed.")
    if not where["local"] and not allow_remote:
        raise CandidateError("The language model is not on this computer, and sending part of the file to it was not allowed.")
    code, first = excerpt(text, finding.get("line"))
    advisory = " ".join(str(x) for x in (finding.get("message"), (finding.get("extra") or {}).get("why") or finding.get("why"), (finding.get("extra") or {}).get("advice") or finding.get("advice"),
                                          ", ".join(finding.get("cwe") or []) if isinstance(finding.get("cwe"), list) else finding.get("cwe")) if x)
    prompt = generate.PROMPT.format(advisory=advisory[:3000], filename=rel, code=f"(the excerpt starts at line {first} of the file)\n{code}"[:6000])
    try:
        client, model = llm_client.configured(timeout=llm_client.DEFAULT_TIMEOUT)
        reply = client.chat.completions.create(model=model, temperature=0, messages=[{"role": "user", "content": prompt}])
        diff = extract_diff(reply.choices[0].message.content)
    except Exception as e:
        raise CandidateError(f"The language model could not be asked ({type(e).__name__}).") from None
    if not diff.strip():
        raise CandidateError("The language model did not return a diff.")
    return diff, {"model": where["model"], "host": where["host"], "local": where["local"]}


def check_diff(diff: str, rel: str) -> None:
    """Refuse a diff that names another file, a path outside the project, a link, or is longer than a candidate should be."""
    from app.backend.pipeline.verify import runner
    paths = runner.patch_paths(diff)
    if not paths:
        raise CandidateError("The proposed text is not a unified diff.")
    if any(_unsafe(p) for p in paths):
        raise CandidateError("The diff names a path outside the project.")
    if set(paths) != {rel}:
        raise CandidateError(f"The diff touches {', '.join(sorted(set(paths) - {rel})) or 'another file'}; a candidate may change only {rel}.")
    if runner.makes_links(diff):
        raise CandidateError("The diff creates or changes a symbolic link.")
    if len(diff.splitlines()) > MAX_DIFF_LINES:
        raise CandidateError(f"The diff is longer than {MAX_DIFF_LINES} lines, which is more than a minimal fix.")


def apply_to_copy(rel: str, original: str, diff: str) -> str:
    """The text of the file after the diff, made by ``git apply`` in a temporary folder that holds nothing but that file. A model writes line feeds, and a file on Windows often has carriage
    return and line feed at the end of every line (found by the first real run): such a file is applied to as line feeds and the patched text gets its line endings back, so a diff does
    not fail on the line endings alone. A file with mixed endings is left as it is."""
    crlf = "\r\n" in original and "\n" not in original.replace("\r\n", "")
    source = original.replace("\r\n", "\n") if crlf else original
    with tempfile.TemporaryDirectory(prefix="candidate-") as tmp:
        target = pathlib.Path(tmp) / pathlib.PurePosixPath(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.encode("utf-8"))
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env["GIT_CEILING_DIRECTORIES"] = str(pathlib.Path(tmp).resolve().parent)
        try:
            done = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", "--whitespace=nowarn", "-"], input=(diff.rstrip("\n") + "\n").encode("utf-8"),
                                  cwd=tmp, env=env, capture_output=True, timeout=60)
        except FileNotFoundError:
            raise CandidateError("git is not available on this computer to apply the diff.") from None
        except subprocess.TimeoutExpired:
            raise CandidateError("Applying the diff did not finish within 60 seconds.") from None
        if done.returncode != 0:
            why = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise CandidateError("The diff does not apply to the file" + (f": {why[-1][:200]}" if why else "."))
        if not target.is_file() or target.is_symlink():
            raise CandidateError("The proposed diff must update an existing regular file.")
        patched = target.read_bytes().decode("utf-8", "replace")
    if crlf:
        patched = patched.replace("\n", "\r\n")
    if patched == original:
        raise CandidateError("The diff changes nothing.")
    return patched


# -- the tests ---------------------------------------------------------------------------------------------------------------------------------------------------

def compare(before: dict | None, after: dict | None, what: str = "the patch") -> tuple[str, str]:
    """``(outcome, sentence)`` from the two test checks: same, improved, regression, or why they cannot be compared. ``what`` names the change in the sentence ("the patch", "the update")."""
    if not before or not after:
        return "not_run", "the tests were not run"
    for label, c in (("before", before), ("after", after)):
        if c.get("state") != "ran":
            return ("env_error" if c.get("state") == "env_error" else "not_run"), f"the tests {label} {what}: {c.get('note') or c.get('state')}"
    bad = lambda c: (c.get("failed") or 0) + (c.get("errors") or 0)       # noqa: E731
    if bad(after) > bad(before):
        return "regression", f"{bad(after)} tests fail after {what} against {bad(before)} before"
    if bad(after) < bad(before):
        return "improved", f"{bad(after)} tests fail after {what} against {bad(before)} before"
    return "same", f"{after.get('passed', 0)} tests pass after {what}, as many failures as before ({bad(before)})"


def _sandbox_has_file(box, target: str, expected: bytes) -> bool:
    try:
        _, actual = sandbox_files.download(box.ct.id, target)
    except Exception:                   # ignored: a file that cannot be downloaded from the sandbox counts as not present
        return False
    return actual == expected


def run_tests(project: dict, index: int, rel: str, original: str, patched: str, scan_id: str) -> tuple[dict, dict, bool]:
    """Return project checks and whether the sandbox held the reviewed bytes during their runs."""
    folder = project["folders"][index]["path"]
    with sandbox_run.ScanSandbox(project, scan_id) as box:
        root = box.roots[index]
        target = f"{root}/{rel}"
        original_bytes, patched_bytes = original.encode("utf-8"), patched.encode("utf-8")
        original_match = _sandbox_has_file(box, target, original_bytes)
        before = next(a.check for a in dynamic.run_tests(box, root, folder))
        bound_before = original_match and _sandbox_has_file(box, target, original_bytes)
        box.put_file(target, patched_bytes)
        patched_match = _sandbox_has_file(box, target, patched_bytes)
        after = next(a.check for a in dynamic.run_tests(box, root, folder))
        bound_after = patched_match and _sandbox_has_file(box, target, patched_bytes)
    return before, after, bound_before and bound_after


def _passed(check: dict) -> bool:
    count = check.get("tests")
    return (check.get("tool") == "pytest" and check.get("state") == "ran" and type(count) is int
            and count > 0 and check.get("passed") == count
            and all(check.get(name) == 0 for name in ("failed", "errors", "skipped")))


def _staging_target(relative: str) -> bool:
    from app.backend.pipeline.verify.runner import shadowed_paths

    path = pathlib.PurePosixPath(relative)
    cleaned = {part.lower() for part in path.parts}
    return (path.suffix.lower() in projects.LANGUAGES and not cleaned.intersection({"tests", "test", "spec"})
            and path.name.lower() not in {"conftest.py", "setup.py"} and not path.name.lower().startswith("test_")
            and not path.stem.lower().endswith(("_test", ".test", ".spec")) and not shadowed_paths([relative]))


def staging_receipt(record: dict, source_root: pathlib.Path) -> dict | None:
    """Bind passing project checks to the one reviewed file and diff for isolated staging."""
    tests = record["tests"]
    before, after = tests.get("before"), tests.get("after")
    if not before or not after or not _passed(before) or not _passed(after) or before["tests"] != after["tests"]:
        return None
    if not record.get("file") or not _staging_target(record["file"]):
        return None
    fields = ("project_id", "candidate_id", "file", "original_sha256", "diff_sha256", "patched_sha256")
    body = {"schema": STAGING_SCHEMA, "source_root": str(source_root.resolve()), "tests": tests,
            **{field: record[field] for field in fields}}
    receipt_id = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**body, "id": receipt_id}


# -- the candidate -----------------------------------------------------------------------------------------------------------------------------------------------

def make(project: dict, finding: dict, *, allow_remote: bool = False, test: bool = True, ask=ask_for_diff,
         expected_source_sha256: str | None = None) -> dict:
    """Make, check and store a candidate for ``finding``. Never raises for a candidate that fails a step: the record says which step and why."""
    projects.require_authorized(project)
    cid = sandbox_run.new_scan_id()
    steps = {n: _step("not_run") for n in STEPS}
    rec = {"schema": SCHEMA, "candidate_id": cid, "project_id": project["id"], "created": util.now_zulu(), "label": "candidate", "receipt_state": "not_verified", "steps": steps,
           "finding": {k: finding.get(k) for k in ("id", "file", "line", "rule", "message", "state", "cwe", "severity_class")}, "file": None, "model": None, "diff_sha256": None,
           "original_sha256": None, "patched_sha256": None, "tests": {"before": None, "after": None, "outcome": "not_run", "words": "the tests were not run"}}
    diff, patched = "", None
    try:
        try:
            index, rel, full = locate(project, finding)
            original = read_text(full, rel)
            if expected_source_sha256 and hashlib.sha256(original.encode("utf-8")).hexdigest() != expected_source_sha256:
                raise CandidateError("The project file changed after the finding was reviewed.")
        except CandidateError as e:
            steps["target"] = _step("failed", str(e))
            raise
        rec["file"], rec["original_sha256"] = rel, hashlib.sha256(original.encode("utf-8")).hexdigest()
        line = finding.get("line")
        lines = original.splitlines()
        if type(line) is int and 0 < line <= len(lines):
            rec["source_quote"] = code_rules.masked(lines[line - 1], 1000)
        steps["target"] = _step("passed", f"{rel}, {len(original.splitlines())} lines")
        try:
            diff, who = ask(finding, rel, original, allow_remote=allow_remote)
        except CandidateError as e:
            steps["model"] = _step("failed", str(e))
            raise
        rec["model"] = who
        steps["model"] = _step("passed", f"{who['model']} on {who['host']}")
        projects.audit(project["id"], tool=f"model: {who['model']}", summary=f"a diff for {rel} was proposed for finding {finding.get('id')}", target=rel, exit_status=0)
        try:
            diff = diff.replace("\r\n", "\n").rstrip("\n") + "\n"
            check_diff(diff, rel)
        except CandidateError as e:
            steps["diff"] = _step("failed", str(e))
            raise
        steps["diff"] = _step("passed", f"{len(diff.splitlines())} lines")
        rec["diff_sha256"] = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        try:
            patched = apply_to_copy(rel, original, diff)
        except CandidateError as e:
            steps["apply"] = _step("failed", str(e))
            raise
        steps["apply"] = _step("passed", "applied to a temporary copy of the file")
        rec["patched_sha256"] = hashlib.sha256(patched.encode("utf-8")).hexdigest()
        if test:
            try:
                before, after, bound = run_tests(project, index, rel, original, patched, cid)
            except (sandbox_run.ToolkitMissing, sandbox_run.CopyRefused, projects.ProjectError) as e:
                steps["tests_before"] = steps["tests_after"] = _step("not_run", str(e))
            except Exception as e:
                steps["tests_before"] = steps["tests_after"] = _step("env_error", f"{type(e).__name__}: {str(e)[:200]}")
            else:
                for name, c in (("tests_before", before), ("tests_after", after)):
                    steps[name] = _step({"ran": "passed", "partial": "passed"}.get(c["state"], c["state"] if c["state"] in ("env_error",) else "not_run"), c.get("note"))
                outcome, words = compare(before, after)
                rec["tests"] = {"before": before, "after": after, "outcome": outcome, "words": words}
                if bound:
                    receipt = staging_receipt(rec, pathlib.Path(project["folders"][index]["path"]))
                    if receipt:
                        rec["staging_receipt"] = receipt
        else:
            steps["tests_before"] = steps["tests_after"] = _step("not_run", "tests were not asked for")
    except CandidateError as e:
        rec["stopped"] = str(e)
    rec["reasons"] = _reasons(rec)
    rec["words"] = ("A candidate for a person to review. It has not been applied to the project, and it is not called correct, safe or ready to merge: "
                    + "; ".join(rec["reasons"]) + ".")
    folder = projects.ROOT / project["id"] / "candidates" / cid
    if diff:
        fsio.write_text(folder / "patch.diff", diff.rstrip("\n") + "\n")
    fsio.write_json(folder / "candidate.json", rec, indent=2)
    projects.audit(project["id"], tool="patch candidate", summary=f"{rec['candidate_id']} for {rec['file'] or finding.get('file')}: {rec['stopped'] if rec.get('stopped') else rec['tests']['words']}",
                   target=str(rec["file"] or ""), exit_status=0 if not rec.get("stopped") else 1, evidence=f"candidates/{cid}/candidate.json")
    return rec


def _reasons(rec: dict) -> list[str]:
    out = ["no test reproduces the weakness before the patch, so it cannot be verified"]
    if rec.get("stopped"):
        out.append(rec["stopped"].rstrip("."))
    outcome = rec["tests"]["outcome"]
    if outcome == "regression":
        out.append(rec["tests"]["words"])
    elif outcome in ("not_run", "env_error") and not rec.get("stopped"):
        out.append(rec["tests"]["words"])
    return out


def get(project_id: str, candidate_id: str) -> dict:
    if not _ID.match(candidate_id or ""):
        raise projects.NotFound("There is no such candidate.")
    projects.get(project_id)
    path = projects.ROOT / project_id / "candidates" / candidate_id / "candidate.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise projects.NotFound("There is no such candidate.") from None


def diff_of(project_id: str, candidate_id: str) -> str:
    get(project_id, candidate_id)
    path = projects.ROOT / project_id / "candidates" / candidate_id / "patch.diff"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def listing(project_id: str) -> list[dict]:
    """The project's candidates, newest first, as ``{candidate_id, file, rule, outcome, receipt_state, created}``."""
    projects.get(project_id)
    root = projects.ROOT / project_id / "candidates"
    out = []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            r = json.loads((d / "candidate.json").read_text(encoding="utf-8"))
            out.append({"candidate_id": r["candidate_id"], "file": r.get("file") or (r.get("finding") or {}).get("file"), "rule": (r.get("finding") or {}).get("rule"),
                        "outcome": r["tests"]["outcome"], "receipt_state": r["receipt_state"], "stopped": r.get("stopped"), "created": r["created"]})
        except (OSError, ValueError, KeyError):
            continue
    return out
