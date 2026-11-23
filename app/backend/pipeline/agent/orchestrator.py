"""The response orchestrator (Chapter 4.9): scan scope x response mode, on a project of the Projects page (P3f.7).

The steps, each a real code path with its own tests:

1. **Collect** the findings of the scope. *Dependencies*: the dependency scan (advisory matching against the advisories on this computer) gives one finding per affected advisory of each
   vulnerable package. *Codebase*: the quick scan (pattern rules, no Docker) or the full scan (``deep_scan``, the tools in the closed container).
2. **Classify** each finding into Recommendation, Warning or Critical with the source of the class (``classification``); a finding with no score is Unknown, and a model is asked for an
   estimate only when the person turned that on.
3. **Respond** by mode: ``log`` records the finding; ``email`` composes one summary of the findings (and sends it only when mail is set up in Settings); ``auto-patch`` makes a patch
   *candidate* for a finding in a file of the project (``candidates``: proposed by a model, tested before and after in the closed container, stored, never applied) and recommends the upgrade
   for a vulnerable package that has a fixed release. "Autonomous" is used strictly in the sense of section 1.2: the response runs without per-finding input, inside the local sandbox, and a
   patch is staged for review and is never applied to the project or pushed anywhere.
4. **Report**: ``data/projects/<id>/scans/<scan id>/report.json`` (kind ``response``), whose findings have the shape of the other reports plus ``severity_class``, ``class_source``,
   ``class_note`` and ``action``.

Parts that cannot be done say so in the report (``notes`` and the ``action`` of a finding): a finding without a file gets no candidate, a package with no fixed release gets no upgrade, and so on.
"""
from __future__ import annotations

from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.agent import candidates as cand
from app.backend.pipeline.agent import classification as cls
from app.backend.pipeline.scan import deep_scan, project_code, project_deps, sandbox_run

SCHEMA = "project-scan/3"
SCOPES = ("dependencies", "codebase", "both")
MODES = ("log", "email", "auto-patch")
DEPTHS = ("quick", "full")
MAX_CANDIDATES = 5
ORDER = {"Critical": 0, "Warning": 1, "Recommendation": 2, "Unknown": 3}


# -- 1. collect --------------------------------------------------------------------------------------------------------------------------------------------

def dependency_findings(report: dict) -> list[dict]:
    """One finding per affected advisory of each vulnerable package of a dependency scan."""
    out = []
    for row in report.get("dependencies", []):
        if row.get("state") != "vulnerable":
            continue
        for a in row.get("advisories", []):
            out.append({"id": f"{row['id']}:{a['id']}", "kind": "dependency", "tool": "advisory match", "basis": "advisory", "state": "vulnerable", "ecosystem": row["ecosystem"], "package": row["name"],
                        "version": row["version"], "advisory": a["id"], "cve": a.get("cve"), "message": a.get("summary") or f"{row['name']} {row['version']} is affected by {a['id']}",
                        "severity": a.get("severity") or "unknown", "cwe": [c for c in str(a.get("cwe") or "").replace(";", ",").split(",") if c.strip()],
                        "fixed_in": a.get("safe_version") or a.get("first_fixed"), "file": (row.get("manifests") or [None])[0], "line": None, "affected_versions": a.get("affected_versions")})
    return out


def code_findings(project: dict, depth: str, options: dict, progress) -> tuple[list[dict], list[str]]:
    """``(findings, notes)`` of the codebase: the quick scan, or the full scan in the closed container."""
    if depth == "full":
        report = deep_scan.run(project, options=options, progress=progress)
        notes = [f"{c['tool']} did not run to its end: {c['note']}" for c in report["checks"] if c["state"] == "env_error"]
        return [{**f, "kind": "codebase"} for f in report["findings"]], notes
    report = project_code.run(project, progress=progress, write=False)
    findings = [{**deep_scan.from_rule(f), "kind": "codebase"} for f in report["findings"]]
    notes = [f"{lang}: no vulnerability rules yet, checked for hard-coded secrets only" for lang in report["summary"]["secrets_only_languages"]]
    if report["summary"]["cut_short"]:
        notes.append("The scan stopped at its limits, so more findings may exist.")
    return findings, notes


# -- 3. respond --------------------------------------------------------------------------------------------------------------------------------------------

def summary_email(project: dict, findings: list[dict]) -> tuple[str, str]:
    """One message for the whole run: counts and a line per finding, with no code in it."""
    counts = cls.counts(findings)
    subject = f"[{project['name']}] {len(findings)} findings: " + ", ".join(f"{n} {c}" for c, n in counts.items() if n)
    lines = [f"{f['severity_class']}: {f.get('package') or f.get('file') or f.get('rule')} - {f.get('message') or ''}"[:200] for f in sorted(findings, key=lambda f: ORDER[f["severity_class"]])[:100]]
    return subject, "\n".join(lines) + ("\n\nThe list is cut at 100 findings." if len(findings) > 100 else "")


def respond(project: dict, findings: list[dict], mode: str, *, allow_remote: bool = False, test: bool = True, make=cand.make, notifier=None) -> tuple[list[dict], dict | None]:
    """The findings with an ``action`` each, and the email of the run (None unless the mode is ``email``)."""
    out, mail, made = [], None, 0
    if mode == "email":
        from app.backend.pipeline import notify
        notifier = notifier or notify
        subject, body = summary_email(project, findings)
        if notifier.smtp_configured():
            res = notifier.send_email(subject, body)
            mail = {"subject": subject, "body": body, "sent": bool(res.get("sent")), **({} if res.get("sent") else {"reason": res.get("error") or res.get("reason") or "the message was not sent"})}
        else:
            mail = {"subject": subject, "body": body, "sent": False, "reason": "mail is not set up in Settings, so the message was composed and not sent"}
    for f in sorted(findings, key=lambda f: (ORDER[f["severity_class"]], str(f.get("file")), f.get("line") or 0)):
        action = {"action": "logged"}
        if mode == "email":
            action = {"action": "email_sent" if mail and mail["sent"] else "email_composed"}
        elif mode == "auto-patch":
            if f.get("kind") == "dependency":
                action = ({"action": "upgrade_recommended", "upgrade_to": f["fixed_in"], "how": "Use Update dependencies on the project page: it makes the change on a copy and shows the files before anything is written."}
                          if f.get("fixed_in") else {"action": "no_candidate", "why": "no fixed release of this package is known: replace it or mitigate"})
            elif not f.get("file") or str(f["file"]).startswith("("):
                action = {"action": "no_candidate", "why": "this finding is not about a file of the project"}
            elif made >= MAX_CANDIDATES:
                action = {"action": "candidate_skipped", "why": f"only the first {MAX_CANDIDATES} findings (the most serious first) get a candidate in one run"}
            else:
                made += 1
                rec = make(project, f, allow_remote=allow_remote, test=test)
                action = {"action": "candidate_made" if not rec.get("stopped") else "candidate_stopped", "candidate_id": rec["candidate_id"], "outcome": rec["tests"]["outcome"],
                          "receipt_state": rec["receipt_state"], **({"why": rec["stopped"]} if rec.get("stopped") else {})}
        out.append({**f, "action": action})
    return out, mail


# -- the run -----------------------------------------------------------------------------------------------------------------------------------------------

def run(project: dict, *, scope: str = "both", mode: str = "log", depth: str = "quick", assist: bool = False, allow_remote: bool = False, test: bool = True,
        options: dict | None = None, progress=lambda *a, **k: None) -> dict:
    """Run the orchestrator on ``project`` and write its report; returns it. Refused for a project without the confirmation that it may be tested."""
    projects.require_authorized(project)
    if scope not in SCOPES or mode not in MODES or depth not in DEPTHS:
        raise ValueError(f"scope is one of {SCOPES}, mode one of {MODES}, depth one of {DEPTHS}")
    scan_id = sandbox_run.new_scan_id()
    started = util.now_zulu()
    findings, notes = [], []
    if scope in ("dependencies", "both"):
        progress(state="dependencies", label="Matching the dependencies against the advisories")
        deps = project_deps.run(project, check_outdated=False, progress=progress)
        findings += dependency_findings(deps)
        s = deps["summary"]
        if s.get("inconclusive") or s.get("env_error"):
            notes.append(f"{s.get('inconclusive', 0)} dependencies could not be matched exactly and {s.get('env_error', 0)} could not be checked; they are not findings and not clean (see the dependency scan)")
    if scope in ("codebase", "both"):
        progress(state="codebase", label="Scanning the code" + (" in the sandbox" if depth == "full" else ""))
        code, more = code_findings(project, depth, options or {}, progress)
        findings += code
        notes += more
    progress(state="classify", label="Classifying the findings")
    findings = cls.classify_findings(findings, assist=assist, allow_remote=allow_remote)
    progress(state="respond", label=f"Responding ({mode})")
    findings, mail = respond(project, findings, mode, allow_remote=allow_remote, test=test)
    by_class = cls.counts(findings)
    by_action: dict[str, int] = {}
    for f in findings:
        by_action[f["action"]["action"]] = by_action.get(f["action"]["action"], 0) + 1
    finished = util.now_zulu()
    report = {"schema": SCHEMA, "kind": "response", "scan_id": scan_id, "project_id": project["id"], "started": started, "finished": finished, "scope": scope, "mode": mode, "depth": depth,
              "options": {"assist": assist, "allow_remote": allow_remote, "test": test}, "findings": findings, "email": mail, "notes": notes,
              "summary": {"findings": len(findings), "by_class": by_class, "by_action": by_action},
              "words": "Every response here is staged for a person: a candidate patch is not applied, not verified and not called correct, and an upgrade is a recommendation. "
                       "A finding with no score is Unknown; a class marked as a model's estimate is an estimate, not a measured score."}
    fsio.write_json(projects.ROOT / project["id"] / "scans" / scan_id / "report.json", report, indent=2)
    projects.audit(project["id"], tool="response", summary=f"{scope} / {mode}: {len(findings)} findings, " + ", ".join(f"{n} {c}" for c, n in by_class.items() if n), target=project["name"],
                   started=started, ended=finished, exit_status=0, evidence=f"scans/{scan_id}/report.json")
    projects.record_scan(project["id"], {"at": finished, "scan_id": scan_id, "kind": "response", "summary": f"{len(findings)} findings classified ({by_class['Critical']} Critical, {by_class['Warning']} Warning, "
                                                                                                             f"{by_class['Recommendation']} Recommendation, {by_class['Unknown']} Unknown); mode {mode}"})
    return report
