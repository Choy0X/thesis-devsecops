"""Run the project's regression tests in the analysis container."""
from __future__ import annotations
import json
import os
import pathlib
import re
from app.backend.pipeline.scan import adapters as ad
from app.backend.pipeline.scan.findings import check

PLACEHOLDER_TEST = re.compile(r"no test specified", re.I)

def detect_tests(folder: str | os.PathLike) -> dict:
    """What kind of tests a project folder has (read-only): ``{"python": bool, "npm": bool}``."""
    root = pathlib.Path(folder)
    py = any((root / n).is_file() for n in ("pytest.ini", "tox.ini", "conftest.py"))
    if not py:
        for sub in ("tests", "test"):
            d = root / sub
            if d.is_dir() and (any(d.rglob("test_*.py")) or any(d.rglob("*_test.py"))):
                py = True
                break
    npm = False
    try:
        scripts = (json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {})
        cmd = str(scripts.get("test") or "")
        npm = bool(cmd) and not PLACEHOLDER_TEST.search(cmd)
    except (OSError, ValueError):
        pass
    return {"python": py, "npm": npm}


def run_tests(box, root: str, folder: str | os.PathLike, *, timeout: int = 600, env: dict | None = None) -> list[ad.Adapted]:
    """The project's own tests in the copy at ``root`` (the container path of ``folder`` on the host). One adapted check per kind of tests found; ``skipped`` ones say why."""
    found = detect_tests(folder)
    out = []
    if found["python"]:
        r = box.run("pytest", ad.pytest_argv(), timeout=timeout, workdir=root, env=env)
        report = box.run("pytest-report", ["cat", "/tmp/junit.xml"], timeout=30, workdir=root)
        out.append(ad.adapt_pytest(r, report.output if report.ran and report.exit_code == 0 else None, root))
    if found["npm"]:
        out.append(ad.Adapted(check(tool="npm test", state="skipped", rule_set="the project's own tests",
                                    note="npm tests need the project's packages, which are not in the copy (it has no network). They run once the packages are pre-fetched on this computer, which the update flow will do."), []))
    if not found["python"] and not found["npm"]:
        out.append(ad.Adapted(check(tool="tests", state="skipped", rule_set="the project's own tests", note="no tests were found (no pytest setup or test files, and no npm test script)"), []))
    return out

def run_probes(box, root, *, command, port, ready_timeout=30):
    return ad.Adapted(check(tool="probe", state="skipped", rule_set="runtime probes",
                            note="Active vulnerability probes are unavailable in this distribution."), [])
