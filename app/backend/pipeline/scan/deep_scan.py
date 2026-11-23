"""Scan Codebase in full: the static rules that run in this process, then the approved toolkit inside a closed container on a copy of the project, and one report with every finding, every
check, and the evidence of each (P3f.4).

The order, and what each part is for:

1. The in-process rules (``project_code``) read the files on the host and need no container. Their candidates are the first findings.
2. A closed container is made from the tool image and the project is copied into it (``sandbox_run``). Nothing of the host is mounted and the container has no network.
3. In the copy: semgrep and gitleaks always; bandit for Python; eslint for JavaScript and TypeScript; the project's own tests; and, when the project says how it starts, the loopback probes.
4. Each tool's output is read by its adapter into checks and findings (``adapters``, ``dynamic``). A tool that did not run to its end is a failed check and the report says so; it never
   makes the result cleaner.
5. Findings of different tools on the same place and weakness are merged; the summary says what ran and what did not, and never calls the code safe.

The report is ``data/projects/<id>/scans/<scan_id>/report.json`` with the evidence files beside it; the run is also in the project's audit log, line by line.
"""
from __future__ import annotations
import pathlib

from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.scan import adapters as ad
from app.backend.pipeline.scan import dynamic, project_code, sandbox_run
from app.backend.pipeline.scan.findings import check, finding, merge, summarize

SCHEMA = "project-scan/2"
TIMEOUTS = {"semgrep": 300, "bandit": 120, "eslint": 180, "gitleaks": 120}
PYTHON, JS = {"Python"}, {"JavaScript", "TypeScript"}
CAVEAT = ("A finding marked inconclusive is a place a tool flagged, not a confirmed weakness. A finding marked vulnerable has evidence beside it. 'No finding' means only that the checks listed "
          "found nothing; it is not a statement that the code is safe.")


def from_rule(f: dict) -> dict:
    """A candidate of the in-process rules in the shape of the report."""
    return finding(tool="rules", rule=f["rule"], file=f["file"], line=f["line"], message=f["summary"], state="inconclusive", severity=f["severity"], severity_source="rule", basis="static",
                   cwe=[f["cwe"]] if f.get("cwe") else [], snippet=f.get("snippet", ""), extra={"why": f.get("why"), "advice": f.get("advice"), "in_tests": f.get("in_tests", False), "context": f.get("context", [])})


def languages_of(project: dict) -> set:
    return set(project.get("languages") or {})


def run_tools(box, project: dict, options: dict, progress, env: dict | None = None) -> tuple[list[dict], list[dict]]:
    """``(checks, findings)`` of the toolkit on every folder of the project, inside ``box`` (an open ``ScanSandbox``): semgrep and gitleaks always, bandit for Python, eslint for JavaScript and
    TypeScript, the project's own tests (with ``env`` in their environment, for the packages an update trial installed) and, with a start command and a port, the loopback probes. The findings are
    not merged yet; with several folders each check names its folder and each finding's file is prefixed with it."""
    langs = languages_of(project)
    checks, findings = [], []
    many = len(box.roots) > 1
    for folder, root in zip(project["folders"], box.roots):
        label = root.rsplit("/", 1)[-1]
        plan = [("semgrep", lambda root=root: ad.adapt_semgrep(box.run("semgrep", ad.semgrep_argv(root), timeout=TIMEOUTS["semgrep"], workdir=root), root))]
        if langs & PYTHON:
            plan.append(("bandit", lambda root=root: ad.adapt_bandit(box.run("bandit", ad.bandit_argv(root), timeout=TIMEOUTS["bandit"], workdir=root), root)))
        if langs & JS:
            plan.append(("eslint", lambda root=root: ad.adapt_eslint(box.run("eslint", ad.eslint_argv(root), timeout=TIMEOUTS["eslint"], workdir=root), root)))
        plan.append(("gitleaks", lambda root=root: ad.adapt_gitleaks(box.run("gitleaks", ad.gitleaks_argv(root), timeout=TIMEOUTS["gitleaks"], workdir=root), root)))
        results: list[ad.Adapted] = []
        for name, step in plan:
            progress(state=name, label=f"Running {name} on {label}")
            results.append(step())
        progress(state="tests", label=f"Running the project's own tests in {label}")
        results.extend(dynamic.run_tests(box, root, folder["path"], env=env))
        progress(state="probe", label=f"Starting {label} and asking it questions on its own loopback")
        results.append(dynamic.run_probes(box, root, command=str(options.get("start_command") or ""), port=options.get("port") or 0))
        for r in results:
            if many:
                r.check["folder"] = label
                for f in r.findings:
                    if not f["file"].startswith("("):
                        f["file"] = f"{label}/{f['file']}"
            checks.append(r.check)
            findings.extend(r.findings)
    return checks, findings


def run(project: dict, *, options: dict | None = None, progress=lambda *a, **k: None) -> dict:
    """Run the full scan of ``project`` and write its report. Raises ``ToolkitMissing`` before anything starts when the tool image has not been built, and ``ProjectError`` for a project
    without the confirmation that it may be tested. ``options`` may carry ``start_command`` and ``port`` for the loopback probes."""
    projects.require_authorized(project)
    opts = options or {}
    image = sandbox_run.toolkit_image()
    scan_id = sandbox_run.new_scan_id()
    started = util.now_zulu()

    progress(state="rules", label="Reading the source files with the static rules")
    static = project_code.run(project, progress=progress, write=False)
    findings = []
    folder_prefixes = {folder['path']: f"{i + 1}-{sandbox_run._slug(pathlib.Path(folder['path']).name)}/"
                       for i, folder in enumerate(project['folders'])} if len(project['folders']) > 1 else {}
    for item in static['findings']:
        findings.append(from_rule({**item, 'file': folder_prefixes.get(item.get('folder'), '') + item['file']}))
    s = static["summary"]
    checks = [check(tool="rules", state="partial" if s["cut_short"] else "ran", rule_set=f"{static['rules']} pattern rules", files=s["files"], findings=len(findings),
                    note=("The scan stopped at its limits." if s["cut_short"] else "") or ("; ".join(f"{k}: secrets only" for k in s["secrets_only_languages"])))]

    copy: list[dict] = []
    with sandbox_run.ScanSandbox(project, scan_id, image=image["tag"]) as box:
        copy = box.copy
        more_checks, more_findings = run_tools(box, project, opts, progress)
        checks += more_checks
        findings += more_findings

    merged = merge(findings)
    merged.sort(key=lambda f: (["critical", "high", "medium", "low", "unknown"].index(f["severity"]), f["state"] != "vulnerable", f["file"], f["line"] or 0))
    model_review = None
    if opts.get('model_review'):
        from app.backend.pipeline.scan import model_review as local_review
        try:
            model_review = local_review.run(project, merged, max_files=int(opts.get('model_max_files', 32)), progress=progress)
        except Exception as error:
            model_review = {'schema': 'local-hybrid-scan/1', 'state': 'failed', 'error': str(error),
                'decisions': [], 'raw_generations': [], 'omissions': [{'reason': str(error)}], 'files_total': None,
                'review_referrals': None, 'repository_safe': False, 'deployment_ready': False}
    summary = summarize(merged, checks)
    finished = util.now_zulu()
    report = {"schema": SCHEMA, "kind": "codebase", "scan_id": scan_id, "project_id": project["id"], "started": started, "finished": finished, "image": image, "container": sandbox_run.limits_record(),
              "copy": copy, "checks": checks, "findings": merged, "summary": summary, "words": summary["words"] + " " + CAVEAT,
              'security_review': {'system_mode': 'tools_only', 'model_validated': False, 'repository_safe': False,
                                  'review_required': bool(merged), 'unresolved_checks': len(summary['checks_failed']) + len(summary['checks_skipped'])}}
    folder = projects.ROOT / project["id"] / "scans" / scan_id
    if model_review is not None:
        fsio.write_json(folder / 'local_model_review.json', model_review, indent=2)
        report['model_review'] = model_review
        report['security_review'].update(system_mode='model_tools_and_sandbox', review_required=True,
                                         model_file_coverage={'total': model_review['files_total'], 'attempted': len(model_review['decisions'])})
    fsio.write_json(folder / "report.json", report, indent=2)
    by = summary["by_state"]
    projects.record_scan(project["id"], {"at": finished, "scan_id": scan_id, "kind": "codebase",
                                         "summary": f"{summary['findings']} findings ({by['vulnerable']} vulnerable, {by['inconclusive']} inconclusive); {len(summary['checks_ran'])} checks ran, "
                                                    f"{len(summary['checks_failed'])} failed, {len(summary['checks_skipped'])} not attempted"})
    return report
