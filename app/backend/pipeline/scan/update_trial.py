"""Trying an update in the sandbox (P3f.5.2, P3f.5.3): the packages of a candidate update are fetched on this computer, installed offline in a closed container next to a copy of the project, and
the project's tests and the toolkit run there, once with the packages as they are now and once with the updated ones. What changed between the two is stored with the update.

What it does, step by step, and what it is careful about:

1. **Pre-fetch, on this computer, with the network.** For the requirements files the update changes, ``pip download --only-binary=:all:`` fetches the wheels of the packages as they are now and
   of the updated set, one folder each, for Linux x86-64 and the Python of the tool image. Wheels only: a source package would run its ``setup.py`` here, and nothing from a package or from the
   project is run on this computer. A package with no wheel for that platform ends the trial with the reason. This is the only step that uses the network, and the page asks first.
2. **Two closed containers**, one per set (no network, no mount of the host, the same hardening as every scan). Into each go the copy of the project, the wheels of its set, and, for the second,
   the updated requirements files in place of the old. ``pip install --no-index`` puts the packages into a folder of the copy, and the project's tests and the tools run with that folder on
   ``PYTHONPATH``. If an install fails, the set's tests are not run and the reason is kept.
3. **The comparison.** Tests before and after (the same comparison as a patch candidate's), and the findings of the tools by id: new, gone, unchanged. The code of the project is the same in both
   copies, so static findings normally do not move; the tests are what show whether the new versions still work with the project.

The project's folders are only read. Everything is written under the update's own folder (``wheels/``, ``trial.json``), and the audit log has a line for each step. A trial says what the tests and
tools said in a closed container; it does not say that the update is safe, correct or ready to apply.

Only requirements files (``requirements*.txt``) are installed so far. A pyproject, a poetry lock or a package.json that the update changes is said so and is not installed.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

from app.backend import sandbox
from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.agent import candidates
from app.backend.pipeline.scan import deep_scan, sandbox_run, update_plan
from app.backend.pipeline.scan.findings import merge

SCHEMA = "update-trial/1"
IMAGE_PYTHON = "3.11"
PLATFORMS = ("manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux_2_28_x86_64", "manylinux1_x86_64")
MAX_WHEEL_BYTES = 300 * 1024 * 1024
SITE = "/work/.site"
WHEELS = "/work/.wheels"
REQUIREMENTS = re.compile(r"^requirements[-_.\w]*\.txt$")


class TrialRefused(projects.ProjectError):
    """Why a trial cannot start, in a sentence."""


def requirement_files(update: dict) -> list[dict]:
    """The files of the update that are requirements files, and those that are not (which a trial cannot install)."""
    return [f for f in update["files"] if REQUIREMENTS.match(pathlib.PurePosixPath(f["rel"]).name)]


def requirement_lines(text: str) -> tuple[list[str], list[str]]:
    """``(lines to fetch, lines refused)`` of a requirements file. A file written by the project is untrusted, and pip runs on this computer: only a plain requirement from the package index is fetched
    (a name, extras, a version specifier, a marker). Comments and blank lines are dropped; an option or an include (``-r``, ``--index-url``), a URL, a git or local path requirement and anything else
    that does not parse as a plain requirement is refused by name, because pip would otherwise reach whatever address or folder the line names."""
    from packaging.requirements import InvalidRequirement, Requirement
    keep, refused = [], []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            refused.append(line)
            continue
        if req.url is not None or "://" in line or line.startswith((".", "/", "\\")) or "@" in line.split(";")[0]:
            refused.append(line)
            continue
        keep.append(str(req))
    return keep, refused


def pip_python(runner=subprocess.run) -> str | None:
    """A Python on this computer that has pip: the one running the app, else the interpreter its virtual environment was made from (an environment made by uv has no pip of its own)."""
    seen: list[str] = []
    for candidate in (sys.executable, str(pathlib.Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3"))):
        if candidate in seen or not pathlib.Path(candidate).exists():
            continue
        seen.append(candidate)
        try:
            if runner([candidate, "-m", "pip", "--version"], capture_output=True, text=True, timeout=60).returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def prefetch(lines: list[str], dest: pathlib.Path, *, timeout: int = 900, runner=subprocess.run) -> dict:
    """Download the wheels for ``lines`` into ``dest``: ``{"ok", "files", "bytes", "note"}``. Wheels only (nothing is built or run here), for the platform and Python of the tool image."""
    python = pip_python(runner)
    if python is None:
        return {"ok": False, "files": [], "bytes": 0, "note": "pip is not available to the Python that runs the app, nor to the one its environment was made from"}
    dest = pathlib.Path(dest).resolve()                                          # pip runs in this folder, so every path it is given must not depend on where it is started
    dest.mkdir(parents=True, exist_ok=True)
    spec = dest.parent / f"{dest.name}.requirements.txt"
    fsio.write_text(spec, "\n".join(lines) + "\n")
    cmd = [python, "-m", "pip", "download", "--only-binary=:all:", "--isolated", "--index-url", "https://pypi.org/simple", "--disable-pip-version-check", "--no-input", "--progress-bar", "off", "--dest", str(dest),
           "--python-version", IMAGE_PYTHON, "--implementation", "cp", "--abi", f"cp{IMAGE_PYTHON.replace('.', '')}", *[x for p in PLATFORMS for x in ("--platform", p)], "-r", str(spec)]
    try:
        done = runner(cmd, capture_output=True, text=True, timeout=timeout, cwd=dest)       # --isolated: no pip.conf, no PIP_* variables; the index is PyPI; the folder is the download folder
    except subprocess.TimeoutExpired:
        return {"ok": False, "files": [], "bytes": 0, "note": f"the download did not finish within {timeout} seconds"}
    except OSError as e:
        return {"ok": False, "files": [], "bytes": 0, "note": f"pip could not be started ({type(e).__name__})"}
    files = sorted(p.name for p in dest.iterdir() if p.is_file())
    size = sum(p.stat().st_size for p in dest.iterdir() if p.is_file())
    if done.returncode != 0:
        tail = [ln for ln in (done.stderr or "").strip().splitlines() if ln.strip()][-1:] or ["pip failed"]
        return {"ok": False, "files": files, "bytes": size, "note": tail[0][:300]}
    if size > MAX_WHEEL_BYTES:
        return {"ok": False, "files": files, "bytes": size, "note": f"the packages are {size // 1048576} MB, more than the {MAX_WHEEL_BYTES // 1048576} MB a trial copies into a container"}
    return {"ok": True, "files": files, "bytes": size, "note": ""}


def _install_argv(files: list[str]) -> list:
    argv = ["pip", "install", "--no-index", "--find-links", WHEELS, "--target", SITE, "--disable-pip-version-check", "--no-input"]
    for rel in files:
        argv += ["-r", rel]
    return argv


def run_set(project: dict, scan_id: str, wheels: pathlib.Path, overlay: dict[str, str], files: list[str], index: int, options: dict, progress, label: str) -> dict:
    """One closed container: the project, the wheels of one set, the requirements files of that set, the install, then the toolkit with the installed folder on PYTHONPATH."""
    with sandbox_run.ScanSandbox(project, scan_id) as box:
        root = box.roots[index]
        for rel, text in overlay.items():
            box.put_file(f"{root}/{rel}", text.encode("utf-8"))
        box.put_tree(wheels, WHEELS)
        progress(state="install", label=f"Installing the {label} packages offline")
        install = box.run("pip install", _install_argv(files), timeout=900, workdir=root)
        out = {"label": label, "install": {"state": install.state, "exit_code": install.exit_code, "evidence": install.evidence, "note": install.note or (install.stderr or install.output)[-300:]},
               "checks": [], "findings": []}
        if not install.ran or install.exit_code != 0:
            out["install"]["state"] = "failed"
            out["skipped"] = "the packages could not be installed, so nothing was run"
            return out
        out["checks"], found = deep_scan.run_tools(box, project, options, progress, env={"PYTHONPATH": SITE})
        out["findings"] = merge(found)
    return out


def _tests(checks: list[dict]) -> dict | None:
    return next((c for c in checks if c["tool"] == "pytest"), None)


def compare(before: dict, after: dict) -> dict:
    """What changed between the two sets: tests, findings by id, and the checks that did not end the same way."""
    t_outcome, t_words = candidates.compare(_tests(before["checks"]) if before["checks"] else None, _tests(after["checks"]) if after["checks"] else None, "the update")
    ids_b, ids_a = {f["id"]: f for f in before["findings"]}, {f["id"]: f for f in after["findings"]}
    brief = lambda f: {k: f.get(k) for k in ("id", "file", "line", "rule", "message", "state", "severity")}       # noqa: E731
    states_b, states_a = {c["tool"]: c["state"] for c in before["checks"]}, {c["tool"]: c["state"] for c in after["checks"]}
    return {"tests": {"outcome": t_outcome, "words": t_words, "before": _tests(before["checks"]), "after": _tests(after["checks"])},
            "findings": {"new": [brief(ids_a[i]) for i in sorted(set(ids_a) - set(ids_b))], "gone": [brief(ids_b[i]) for i in sorted(set(ids_b) - set(ids_a))], "unchanged": len(set(ids_a) & set(ids_b))},
            "checks_changed": [{"tool": t, "before": states_b.get(t), "after": states_a.get(t)} for t in sorted(set(states_b) | set(states_a)) if states_b.get(t) != states_a.get(t)]}


def words_of(trial: dict) -> str:
    if trial["state"] != "done":
        return trial["note"]
    c = trial["comparison"]
    f = c["findings"]
    return (f"With the updated packages in a closed container: {c['tests']['words']}; {len(f['new'])} new findings, {len(f['gone'])} gone, {f['unchanged']} unchanged. This is what the tests and the tools said; "
            "it does not say that the update is safe or correct, and nothing in the project has changed.")


def run(project: dict, uid: str, *, options: dict | None = None, prefetch_fn=None, progress=lambda *_a, **_k: None) -> dict:
    """Try the update ``uid`` in the sandbox and store ``trial.json`` with it. Raises ``TrialRefused`` before anything is fetched or started when it cannot run at all; a trial that runs into a
    problem (a package with no wheel, an install that fails) is stored with the reason."""
    projects.require_authorized(project)
    fetch = prefetch_fn or prefetch                                           # looked up now, so a test can put its own in place of pip
    update = update_plan.get(project["id"], uid)
    update_plan._clean(update)
    if update["status"] != "candidate":
        raise TrialRefused("Only a candidate update that has not been applied can be tried.")
    reqs = requirement_files(update)
    if not reqs:
        raise TrialRefused("This update changes no requirements file, and only requirements files can be installed in the sandbox so far: " + ", ".join(f["file"] for f in update["files"]) + ".")
    sandbox_run.toolkit_image()                                              # raises ToolkitMissing, in words, before any download
    if not sandbox.available():
        raise TrialRefused("Docker is not running, so the sandbox cannot be started.")
    base = update_plan._dir(project["id"]) / uid
    refused = sorted({ln for text in (*[(base / "original" / pathlib.PurePosixPath(f["file"])).read_text(encoding="utf-8") for f in reqs], *[(base / "copy" / pathlib.PurePosixPath(f["file"])).read_text(encoding="utf-8") for f in reqs])
                      for ln in requirement_lines(text)[1]})
    if refused:
        raise TrialRefused("A trial fetches plain requirements from the package index and nothing else. These lines name an option, a URL, a repository or a path, which pip would follow on this computer: " + "; ".join(refused[:5]) + ".")
    folder_index = {f["path"]: i for i, f in enumerate(project["folders"])}
    started = util.now_zulu()
    trial_id = sandbox_run.new_scan_id()
    original = {f["rel"]: (base / "original" / pathlib.PurePosixPath(f["file"])).read_text(encoding="utf-8") for f in reqs}
    updated = {f["rel"]: (base / "copy" / pathlib.PurePosixPath(f["file"])).read_text(encoding="utf-8") for f in reqs}
    index = folder_index.get(reqs[0]["folder"], 0)
    if any(folder_index.get(f["folder"], 0) != index for f in reqs):
        raise TrialRefused("This update changes requirements files in more than one folder of the project; a trial handles one folder at a time.")
    trial = {"schema": SCHEMA, "id": trial_id, "update_id": uid, "project_id": project["id"], "started": started, "state": "running", "files": [f["file"] for f in reqs], "python": IMAGE_PYTHON,
             "platform": "Linux x86-64 (manylinux)", "prefetch": {}, "before": None, "after": None, "comparison": None,
             "not_installed": [f["file"] for f in update["files"] if f not in reqs], "note": ""}
    notes = ["Only requirements files are installed; " + ", ".join(trial["not_installed"]) + " not installed."] if trial["not_installed"] else []
    out_file = base / "trial.json"
    try:
        for label in ("before", "after"):
            progress(state="prefetch", label=f"Fetching the {label} packages (wheels only)")
            lines = [ln for rel in original for ln in requirement_lines((original if label == "before" else updated)[rel])[0]]
            got = fetch(lines, base / "wheels" / label)
            trial["prefetch"][label] = got
            projects.audit(project["id"], tool="pip download", summary=f"{label}: {len(got['files'])} wheels, {got['bytes'] // 1024:,} KB" + ("" if got["ok"] else f" - {got['note']}"), target=str(base / "wheels" / label),
                           exit_status=0 if got["ok"] else 1)
            if not got["ok"]:
                trial.update(state="prefetch_failed", note=f"The {label} packages could not be fetched as wheels: {got['note']}")
                return _finish(project, trial, out_file, started)
        sets = {}
        for label in ("before", "after"):
            overlay = {} if label == "before" else updated
            sets[label] = run_set(project, f"{trial_id}-{label[0]}", base / "wheels" / label, overlay, list(original), index, options or {}, progress, label)
        trial["before"], trial["after"] = sets["before"], sets["after"]
        failed = [s["label"] for s in sets.values() if s["install"]["state"] == "failed"]
        if failed:
            trial.update(state="install_failed", note="The " + " and ".join(failed) + " packages could not be installed offline: " + "; ".join(f"{s['label']}: {s['install']['note']}" for s in sets.values() if s["install"]["state"] == "failed"))
        else:
            trial["comparison"] = compare(sets["before"], sets["after"])
            trial["state"] = "done"
        trial["note"] = trial["note"] or " ".join(notes)
    except (sandbox_run.ToolkitMissing, sandbox_run.CopyRefused) as e:
        raise TrialRefused(str(e)) from None
    return _finish(project, trial, out_file, started)


def _finish(project: dict, trial: dict, path: pathlib.Path, started: str) -> dict:
    trial["finished"] = util.now_zulu()
    trial["words"] = words_of(trial)
    fsio.write_json(path, trial, indent=2)
    projects.audit(project["id"], tool="update trial", summary=f"{trial['state']}: {trial['words']}"[:300], target=trial["update_id"], started=started, ended=trial["finished"], exit_status=0 if trial["state"] == "done" else 1,
                   evidence=f"updates/{trial['update_id']}/trial.json")
    return trial


def get(project_id: str, uid: str) -> dict:
    update_plan.get(project_id, uid)
    try:
        return json.loads((update_plan._dir(project_id) / uid / "trial.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise projects.NotFound("This update has not been tried in the sandbox.") from None
