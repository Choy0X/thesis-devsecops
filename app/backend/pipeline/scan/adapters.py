"""Adapters: the raw output of each tool of Scan Codebase turned into checks and findings in the shape of ``findings.py``.

Every adapter obeys the same rules:

* a tool that did not run to its end (``ToolRun.ran`` false), whose exit status means a failure of the tool itself, or whose output cannot be understood is an ``env_error`` check with no
  findings, never a clean result;
* a tool that ran but could not read some files is a ``partial`` check that says how many and why, so a clean result over a partly read project is never worded as complete;
* paths are made relative to the project root inside the container, so a finding points at ``src/app.py`` and not at ``/work/proj/src/app.py``;
* a finding keeps a pointer to the evidence file that holds the tool's raw output, and its message and snippet are masked for credentials;
* a static tool's finding is ``inconclusive`` until something confirms it; a secret that is written in a file is ``vulnerable`` (it is a fact, not a guess), and its value is never kept.

The commands (``*_argv``) are here too, next to the adapters that read their output, so the flags that make the output readable and keep the tool offline are not separated from the code
that depends on them. The shapes of the outputs are those the tools document; they have been tested against fixtures written from those documents, not against recorded runs, until the
tool image exists and the first real run replaces the fixtures.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from app.backend.pipeline.scan import code_rules
from app.backend.pipeline.scan.findings import check, finding

TOOLKIT = "/opt/toolkit"
MAX_XML_BYTES = 5 * 1024 * 1024


@dataclass
class Adapted:
    check: dict
    findings: list


# -- the commands ------------------------------------------------------------------------------------------------------------------------------------------------

def semgrep_argv(root: str) -> list:
    return ["semgrep", "scan", "--json", "--quiet", "--metrics=off", "--disable-version-check", "--config", f"{TOOLKIT}/rules", "--timeout", "30", "--max-target-bytes", "1000000", root]


def bandit_argv(root: str) -> list:
    return ["bandit", "-r", root, "-f", "json"]


def eslint_argv(root: str) -> list:
    return ["node", f"{TOOLKIT}/js/node_modules/eslint/bin/eslint.js", "-c", f"{TOOLKIT}/js/eslint.config.mjs", "-f", "json", "--no-error-on-unmatched-pattern", root]


def gitleaks_argv(root: str) -> list:
    return ["gitleaks", "detect", "--no-git", "--source", root, "--redact", "--no-banner", "--log-level", "error", "--report-format", "json", "--report-path", "/dev/stdout"]


def pytest_argv(junit: str = "/tmp/junit.xml") -> list:
    return ["python", "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={junit}"]


# -- helpers -----------------------------------------------------------------------------------------------------------------------------------------------------

def relative(path: str, root: str) -> str:
    path = (path or "").replace("\\", "/")
    root = root.rstrip("/")
    if path.startswith(root + "/"):
        return path[len(root) + 1:]
    return path


def find_json(text: str, want: type):
    """The report: all of standard output, which must be exactly one JSON value of the type ``want``. Standard error is not looked at, and nothing is searched for in the middle of the text, so
    a line that a project makes a tool or a test print (``{"results": []}``) cannot stand in for the report. None when standard output is anything else."""
    try:
        value = json.loads((text or "").strip())
    except ValueError:
        return None
    return value if isinstance(value, want) else None


def _evidence(run) -> dict | None:
    return {"file": run.evidence, "sha256": run.sha256} if run.evidence else None


def _not_run(run, tool: str, rule_set: str = "") -> Adapted | None:
    """The check of a tool that did not run to its end, or None when it did."""
    if not run.ran:
        return Adapted(check(tool=tool, state="env_error", rule_set=rule_set, note=run.note or f"the tool ended as {run.state}", evidence=run.evidence, sha256=run.sha256), [])
    if run.cut:
        return Adapted(check(tool=tool, state="env_error", rule_set=rule_set, note="the output was larger than what is kept, so it cannot be read whole", evidence=run.evidence, sha256=run.sha256), [])
    return None


def _unreadable(run, tool: str, rule_set: str, why: str) -> Adapted:
    return Adapted(check(tool=tool, state="env_error", rule_set=rule_set, note=why, evidence=run.evidence, sha256=run.sha256), [])


def _cwes(value) -> list[str]:
    out = []
    for v in value if isinstance(value, list) else [value]:
        m = re.search(r"CWE-(\d+)", str(v or ""), re.I)
        if m:
            out.append(f"CWE-{m.group(1)}")
    return out


def _done(run, tool: str, rule_set: str, findings: list, *, version=None, files=None, problems=None, note="") -> Adapted:
    state = "partial" if problems else "ran"
    if files == 0 and not findings and not problems:                       # a tool that looked at nothing has not checked anything: it is not a clean result
        return Adapted(check(tool=tool, state="skipped", rule_set=rule_set, version=version, files=0, note="the tool found no files of the kinds it reads, so it checked nothing", evidence=run.evidence,
                             sha256=run.sha256), [])
    return Adapted(check(tool=tool, state=state, rule_set=rule_set, version=version, files=files, findings=len(findings), note=note or (f"{len(problems)} problems while reading files" if problems else ""),
                         evidence=run.evidence, sha256=run.sha256, problems=problems), findings)


# -- the adapters ------------------------------------------------------------------------------------------------------------------------------------------------

RULES_PREFIX = "opt.toolkit.rules."                       # semgrep names a rule after the folder it was loaded from, which is where the image keeps them
SEMGREP_SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


def adapt_semgrep(run, root: str, rule_set: str = "vendored rules") -> Adapted:
    bad = _not_run(run, "semgrep", rule_set)
    if bad:
        return bad
    if run.exit_code is not None and run.exit_code >= 2:
        return _unreadable(run, "semgrep", rule_set, f"semgrep stopped with status {run.exit_code}")
    doc = find_json(run.raw, dict)
    if doc is None or "results" not in doc:
        return _unreadable(run, "semgrep", rule_set, "the output is not the JSON report semgrep writes")
    found = []
    for r in doc.get("results", []):
        extra = r.get("extra", {}) or {}
        meta = extra.get("metadata", {}) or {}
        sev = SEMGREP_SEVERITY.get(str(extra.get("severity", "")).upper())
        found.append(finding(tool="semgrep", rule=str(r.get("check_id", "")).removeprefix(RULES_PREFIX), file=relative(r.get("path", ""), root), line=(r.get("start") or {}).get("line"), message=extra.get("message", ""),
                             state="inconclusive", severity=sev or "unknown", severity_source="tool" if sev else "unknown", cwe=_cwes(meta.get("cwe")),
                             snippet=str(extra.get("lines", "")).strip(), confidence=str(meta.get("confidence", "")).lower() or None, evidence=_evidence(run)))
    problems = [f"{e.get('level', 'error')}: {e.get('message') or e.get('long_msg') or e.get('type')}" for e in doc.get("errors", []) if str(e.get("level", "error")).lower() == "error"]
    scanned = (doc.get("paths") or {}).get("scanned")
    return _done(run, "semgrep", rule_set, found, version=doc.get("version"), files=len(scanned) if isinstance(scanned, list) else None, problems=problems)


BANDIT_SEVERITY = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}


def adapt_bandit(run, root: str, rule_set: str = "bandit default tests") -> Adapted:
    bad = _not_run(run, "bandit", rule_set)
    if bad:
        return bad
    if run.exit_code not in (0, 1):
        return _unreadable(run, "bandit", rule_set, f"bandit stopped with status {run.exit_code}")
    doc = find_json(run.raw, dict)
    if doc is None or "results" not in doc:
        return _unreadable(run, "bandit", rule_set, "the output is not the JSON report bandit writes")
    found = []
    for r in doc.get("results", []):
        sev = BANDIT_SEVERITY.get(str(r.get("issue_severity", "")).upper())
        cwe = (r.get("issue_cwe") or {}).get("id")
        found.append(finding(tool="bandit", rule=f"{r.get('test_id', '')} {r.get('test_name', '')}".strip(), file=relative(r.get("filename", ""), root), line=r.get("line_number"),
                             message=r.get("issue_text", ""), state="inconclusive", severity=sev or "unknown", severity_source="tool" if sev else "unknown", cwe=[f"CWE-{cwe}"] if cwe else [],
                             snippet=str(r.get("code", "")).strip().splitlines()[-1] if str(r.get("code", "")).strip() else "", confidence=str(r.get("issue_confidence", "")).lower() or None,
                             evidence=_evidence(run)))
    problems = [f"{relative(e.get('filename', ''), root)}: {e.get('reason', 'could not be read')}" for e in doc.get("errors", [])]
    files = len([k for k in (doc.get("metrics") or {}) if k != "_totals"])
    return _done(run, "bandit", rule_set, found, files=files, problems=problems)


# this app's own mapping from the rules of eslint-plugin-security to a weakness class; the plugin gives no CWE and no severity of its own
ESLINT_CWE = {"security/detect-eval-with-expression": "CWE-95", "security/detect-child-process": "CWE-78", "security/detect-non-literal-fs-filename": "CWE-22",
              "security/detect-unsafe-regex": "CWE-1333", "security/detect-non-literal-require": "CWE-829", "security/detect-possible-timing-attacks": "CWE-208",
              "security/detect-pseudoRandomBytes": "CWE-338", "security/detect-disable-mustache-escape": "CWE-79"}


def adapt_eslint(run, root: str, rule_set: str = "eslint-plugin-security") -> Adapted:
    bad = _not_run(run, "eslint", rule_set)
    if bad:
        return bad
    if run.exit_code not in (0, 1):
        return _unreadable(run, "eslint", rule_set, f"eslint stopped with status {run.exit_code}")
    doc = find_json(run.raw, list)
    if doc is None:
        return _unreadable(run, "eslint", rule_set, "the output is not the JSON report eslint writes")
    found, problems = [], []
    for f in doc:
        rel = relative(f.get("filePath", ""), root)
        for m in f.get("messages", []):
            if m.get("fatal") or not m.get("ruleId"):
                problems.append(f"{rel}:{m.get('line', '?')}: {m.get('message', 'could not be read')}")
                continue
            rule = str(m["ruleId"])
            found.append(finding(tool="eslint", rule=rule, file=rel, line=m.get("line"), message=m.get("message", ""), state="inconclusive", severity="unknown",       # eslint severity is a setting, not a rating
                                 cwe=[ESLINT_CWE[rule]] if rule in ESLINT_CWE else [], evidence=_evidence(run)))
    return _done(run, "eslint", rule_set, found, files=len(doc), problems=problems)


def adapt_gitleaks(run, root: str, rule_set: str = "gitleaks default rules") -> Adapted:
    bad = _not_run(run, "gitleaks", rule_set)
    if bad:
        return bad
    if run.exit_code not in (0, 1):
        return _unreadable(run, "gitleaks", rule_set, f"gitleaks stopped with status {run.exit_code}")
    text = run.raw.strip()
    doc = [] if text in ("", "null") else find_json(text, list)
    if doc is None:
        return _unreadable(run, "gitleaks", rule_set, "the output is not the JSON report gitleaks writes")
    if run.exit_code == 1 and not doc:
        return _unreadable(run, "gitleaks", rule_set, "gitleaks reported leaks (status 1) but its report has none")
    # a rule for a known kind of credential (an AWS key, a GitHub token) is a fact about the file; a generic rule (a high-entropy value next to a word like "key") can be a placeholder or a
    # test value, so it is a place to look
    found = [finding(tool="gitleaks", rule=str(r.get("RuleID", "")), file=relative(r.get("File", ""), root), line=r.get("StartLine"), message=r.get("Description") or "A secret is written in this file.",
                     state="inconclusive" if str(r.get("RuleID", "")).startswith("generic") else "vulnerable", severity="high", severity_source="rule", basis="secret", cwe=["CWE-798"],
                     snippet="(the value is not kept)", evidence=_evidence(run)) for r in doc]
    return _done(run, "gitleaks", rule_set, found)


def adapt_pytest(run, report: str | None, root: str = "", rule_set: str = "the project's own tests") -> Adapted:
    """``run`` is the pytest run, ``report`` the text of the JUnit file it wrote (read back from the container). Failing tests are not security findings: they are listed on the check."""
    bad = _not_run(run, "pytest", rule_set)
    if bad:
        return bad
    if run.exit_code not in (0, 1, 5):
        why = {2: "the test run was interrupted", 3: "pytest had an internal error", 4: "pytest was started wrongly"}.get(run.exit_code, f"pytest stopped with status {run.exit_code}")
        missing = sorted(set(re.findall(r"No module named '([\w.]+)'", run.raw + "\n" + run.stderr)))
        if missing:
            why += f": the project's dependencies are not installed in the copy (missing: {', '.join(missing[:10])})"
        return _unreadable(run, "pytest", rule_set, why)
    if run.exit_code == 5:
        return Adapted(check(tool="pytest", state="ran", rule_set=rule_set, note="no tests were collected, so nothing about the code was tested", evidence=run.evidence, sha256=run.sha256, extra={"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}), [])
    # The report was written inside the container by a program the project controls, so it is untrusted. A JUnit file from pytest never has a document type declaration; one that does is refused
    # before it is parsed, which is what an external-entity or entity-expansion attack needs (defusedxml would do this too, and is not installed here).
    if not report or len(report.encode("utf-8")) > MAX_XML_BYTES or "<!DOCTYPE" in report.upper() or "<!ENTITY" in report.upper():
        return _unreadable(run, "pytest", rule_set, "the test report is missing, too large, or declares entities, so it is not read")
    try:
        tree = ET.fromstring(report)
    except ET.ParseError:
        return _unreadable(run, "pytest", rule_set, "the test report is not valid XML")
    suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
    total = {k: sum(int(s.get(k, 0) or 0) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
    failures = []
    for case in tree.iter("testcase"):
        bad_node = case.find("failure") if case.find("failure") is not None else case.find("error")
        if bad_node is not None and len(failures) < 20:
            failures.append(f"{case.get('classname', '')}.{case.get('name', '')}: {(bad_node.get('message') or '')[:200]}")
    passed = max(0, total["tests"] - total["failures"] - total["errors"] - total["skipped"])
    note = f"{total['tests']} tests: {passed} passed, {total['failures']} failed, {total['errors']} errors, {total['skipped']} skipped"
    return Adapted(check(tool="pytest", state="ran", rule_set=rule_set, note=note, evidence=run.evidence, sha256=run.sha256,
                         extra={"tests": total["tests"], "passed": passed, "failed": total["failures"], "errors": total["errors"], "skipped": total["skipped"], "failing": [code_rules.masked(f, 300) for f in failures]}), [])
