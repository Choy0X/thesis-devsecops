"""Scan the dependencies of a project: read its manifests, match every exact version against the advisories on this computer, and optionally ask the registries whether newer
releases exist. Nothing is installed and nothing of the project is executed; its files are only read.

Every dependency ends in one of four states and the page never turns the last three into "fine":

* ``vulnerable``   an advisory affects the exact version in use (with the advisory, its range and the rule that matched as evidence);
* ``no_finding``   the version is known and none of the advisories of this computer affects it: "no finding by advisory matching", not a proof of safety;
* ``inconclusive`` only a range is declared, so the version in use is not known (a lock file would say);
* ``env_error``    the advisories of the ecosystem are not on this computer, or the manifest could not be read: nothing was checked.

The report is written under ``data/projects/<id>/scans/<scan>/report.json`` and every tool run is appended to the project's audit log.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import secrets

from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.scan import advisories as adv, capabilities, manifests as mf

SCHEMA = "project-scan/1"
SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
ORDER = ["critical", "high", "medium", "low", "unknown"]
MAX_LOOKUPS = 300                                  # registry lookups per scan; a project with more is checked for the first ones and says so


def inventory(project: dict) -> tuple[list[dict], list[dict]]:
    """``(dependencies, problems)``: every dependency of the project's manifests, merged to one row per package and version in use, and the manifests that could not be read or are of a
    kind that is not read yet."""
    deps, problems = [], []
    for folder in project["folders"]:
        root = pathlib.Path(folder["path"])
        for eco, rels in (folder.get("manifests") or {}).items():
            for rel in rels:
                if mf.reader_for(pathlib.PurePosixPath(rel).name) is None:
                    problems.append({"ecosystem": eco, "manifest": rel, "folder": folder["path"], "reason": f"{pathlib.PurePosixPath(rel).name} is a kind of file this app does not read yet."})
                    continue
                try:
                    for d in mf.read(root, rel):
                        deps.append({**d, "folder": folder["path"]})
                except mf.ManifestError as e:
                    problems.append({"ecosystem": eco, "manifest": rel, "folder": folder["path"], "reason": str(e)})
    return mf.merge(deps), problems


def _entry_row(a: dict, version: str, depversions) -> dict:
    e = a["entry"]
    ev = e if "status" in e else {**e, **depversions.evaluate(e, None)}
    rule = "explicit version" if version in set(e.get("versions") or []) else "range " + depversions.describe_ranges(e)
    sev = SEVERITY.get(str(a.get("osv_severity") or "").upper(), "unknown")
    return {"id": a["osv_id"], "cve": a.get("cve_id"), "summary": a.get("summary"), "severity": sev, "cwe": a.get("osv_cwe"), "affected_versions": depversions.describe_ranges(e),
            "first_fixed": ev.get("first_fixed"), "safe_version": ev.get("safe_version"), "rule": rule}


def classify(dep: dict, found: dict[str, list[dict]], source: dict, depversions) -> dict:
    """The state of one dependency and the evidence for it."""
    eco, name = dep["ecosystem"], dep["name"]
    base = {k: dep[k] for k in ("ecosystem", "name", "version", "declared", "source", "manifests", "folder") if k in dep}
    if not source["dump_present"] and not source["store"]:
        return {**base, "state": "env_error", "checks": [], "reason": f"The advisories of {eco} are not on this computer, so nothing was checked. Download and ingest them under CVE Collections."}
    if not dep["version"]:
        return {**base, "state": "inconclusive", "checks": [], "reason": f"Only a range is declared ({dep['declared'] or 'no version'}), so the version in use is not known. A lock file would say which version is installed."}
    candidates = found.get(adv.norm(eco, name), [])
    hits, seen = [], set()
    for a in candidates:
        if a["osv_id"] in seen or not depversions.is_affected(eco, dep["version"], a["entry"]):
            continue
        seen.add(a["osv_id"])
        hits.append(_entry_row(a, dep["version"], depversions))
    if not hits:
        return {**base, "state": "no_finding", "checks": ["advisory match"], "advisories_for_package": len({a["osv_id"] for a in candidates}),
                "reason": f"No finding by advisory matching: {len({a['osv_id'] for a in candidates})} advisories on this computer name this package and none affects {dep['version']}."}
    worst = min(hits, key=lambda h: ORDER.index(h["severity"]))["severity"]
    safe = next((h["safe_version"] for h in hits if h["safe_version"]), None)
    return {**base, "state": "vulnerable", "checks": ["advisory match"], "severity": worst, "advisories": hits,
            "recommendation": f"Upgrade to {safe}." if safe else "No fixed release is known: replace the package or mitigate.",
            "reason": f"{dep['version']} is affected by {len(hits)} advisor{'y' if len(hits) == 1 else 'ies'}."}


def _bump(current: str, latest: str, ecosystem: str, depversions) -> str | None:
    try:
        a, b = depversions.VKey(ecosystem, current), depversions.VKey(ecosystem, latest)
        if not a < b:
            return None
        pa, pb = [int(x) for x in str(current).split("-")[0].split(".")[:3] if x.isdigit()], [int(x) for x in str(latest).split("-")[0].split(".")[:3] if x.isdigit()]
        for i, word in enumerate(("major", "minor", "patch")):
            if (pa[i] if i < len(pa) else 0) != (pb[i] if i < len(pb) else 0):
                return word
        return "patch"
    except Exception:  # ignored: versions that cannot be compared are reported as newer without a size
        return "newer"


def outdated(rows: list[dict], releases, depversions, progress=lambda *a, **k: None, limit: int = MAX_LOOKUPS) -> dict:
    """Ask the registry for the latest release of each package with an exact version (a package once); the answer goes onto its rows as ``outdated``. Returns the counts."""
    asked: dict[tuple[str, str], dict] = {}
    counts = {"looked_up": 0, "outdated": 0, "up_to_date": 0, "unavailable": 0, "not_looked_up": 0}
    todo = [r for r in rows if r.get("version")]
    for i, r in enumerate(todo):
        key = (r["ecosystem"], r["name"])
        if key not in asked:
            if len(asked) >= limit:
                r["outdated"] = {"state": "not_looked_up", "reason": f"Only the first {limit} packages were looked up."}
                counts["not_looked_up"] += 1
                continue
            progress(state="registry", label=f"Looking up {r['name']}", processed=i, total=len(todo))
            rel = releases(r["ecosystem"], r["name"])
            counts["looked_up"] += 1
            stable = [v for v in (rel or []) if not depversions.is_prerelease(r["ecosystem"], v)]
            asked[key] = {"latest": max(stable, key=lambda v: depversions.VKey(r["ecosystem"], v))} if stable else {"unavailable": True} if rel is None else {"latest": None}
        got = asked[key]
        if got.get("unavailable"):
            r["outdated"] = {"state": "unavailable", "reason": "The registry did not answer."}
            counts["unavailable"] += 1
        elif not got.get("latest"):
            r["outdated"] = {"state": "unavailable", "reason": "The registry lists no stable release."}
            counts["unavailable"] += 1
        else:
            bump = _bump(r["version"], got["latest"], r["ecosystem"], depversions)
            r["outdated"] = {"state": "outdated" if bump else "up_to_date", "latest": got["latest"], "behind": bump}
            counts["outdated" if bump else "up_to_date"] += 1
    return counts


def summarise(rows: list[dict], problems: list[dict]) -> dict:
    states = {s: sum(1 for r in rows if r["state"] == s) for s in ("vulnerable", "no_finding", "inconclusive", "env_error")}
    sev = {s: sum(1 for r in rows if r["state"] == "vulnerable" and r["severity"] == s) for s in ORDER}
    return {"dependencies": len(rows), **states, "severity": sev, "manifests_not_read": len(problems)}


def run(project: dict, *, check_outdated: bool = False, advisories=adv.by_package, releases=None, progress=lambda *a, **k: None) -> dict:
    """Scan the dependencies of ``project`` and write the report; returns it. Refused when the project has no confirmation that it may be tested."""
    from app.backend.pipeline.ingest import depversions
    projects.require_authorized(project)
    releases = releases or depversions.releases
    scan_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    started = util.now_zulu()
    folder = projects.ROOT / project["id"] / "scans" / scan_id
    progress(state="manifests", label="Reading the manifests")
    rows_in, problems = inventory(project)
    projects.audit(project["id"], tool="manifest reader", summary=f"{len(rows_in)} dependencies from {sum(len(v) for f in project['folders'] for v in (f.get('manifests') or {}).values())} manifests; "
                   f"{len(problems)} not read", target=", ".join(f["path"] for f in project["folders"]), started=started, ended=util.now_zulu(), exit_status=0 if not problems else 1)
    by_eco: dict[str, list[dict]] = {}
    for d in rows_in:
        by_eco.setdefault(d["ecosystem"], []).append(d)
    rows, ecosystems = [], {}
    table = capabilities.matrix(sorted(by_eco) or capabilities.ECOSYSTEMS)
    for eco, deps in sorted(by_eco.items()):
        cells = table["ecosystems"][eco]
        t0 = util.now_zulu()
        if cells["manifest_parsing"]["status"] == capabilities.NOT:
            ecosystems[eco] = {"checked": False, "reason": f"Not supported yet for {eco}: its manifests cannot be read."}
            continue
        progress(state="advisories", label=f"Matching {eco} against its advisories")
        found, source = advisories(eco, [d["name"] for d in deps])                    # the packages of this ecosystem only
        classified = [classify(d, found, source, depversions) for d in deps]
        rows.extend(classified)
        ecosystems[eco] = {"checked": True, "dependencies": len(deps), "advisory_source": source, "capabilities": {k: v["status"] for k, v in cells.items()}}
        projects.audit(project["id"], tool="advisory match", summary=f"{eco}: {len(deps)} dependencies against {source['store']} stored and {source['dump']} dumped advisory entries",
                       target=eco, started=t0, ended=util.now_zulu(), exit_status=0)
    for eco in project.get("ecosystems") or []:                                     # an ecosystem whose manifests cannot be read yet is said, never left out as if it were clean
        if eco not in ecosystems:
            cells = capabilities.matrix([eco])["ecosystems"][eco]
            if cells["manifest_parsing"]["status"] == capabilities.NOT:
                ecosystems[eco] = {"checked": False, "reason": f"Not supported yet for {eco}: its manifests cannot be read, so nothing was checked."}
            else:                                                                   # its manifests can be read and they list no dependency that was understood
                ecosystems[eco] = {"checked": True, "dependencies": 0, "advisory_source": None, "capabilities": {k: v["status"] for k, v in cells.items()},
                                   "reason": f"The {eco} manifests that can be read list no dependencies; manifests of a kind not read yet are named under the problems."}
    outdated_counts = None
    if check_outdated:
        t0 = util.now_zulu()
        outdated_counts = outdated(rows, releases, depversions, progress)
        projects.audit(project["id"], tool="registry lookup", summary=f"{outdated_counts['looked_up']} packages looked up, {outdated_counts['outdated']} outdated, {outdated_counts['unavailable']} without an answer",
                       target="package registries", started=t0, ended=util.now_zulu(), exit_status=0)
    rows.sort(key=lambda r: (r["state"] != "vulnerable", ORDER.index(r.get("severity", "unknown")) if r["state"] == "vulnerable" else 9, r["ecosystem"], r["name"]))      # findings first
    for i, r in enumerate(rows):
        r["id"] = f"dep-{i + 1}"
    report = {"schema": SCHEMA, "kind": "dependencies", "scan_id": scan_id, "project_id": project["id"], "started": started, "finished": util.now_zulu(), "outdated_checked": check_outdated,
              "ecosystems": ecosystems, "problems": problems, "dependencies": rows, "outdated": outdated_counts, "summary": summarise(rows, problems),
              "words": "A dependency without a finding means no finding by advisory matching, not that it is safe. A range without a lock file is inconclusive."}
    folder.mkdir(parents=True, exist_ok=True)
    fsio.write_json(folder / "report.json", report, indent=2)
    projects.record_scan(project["id"], {"at": report["finished"], "scan_id": scan_id, "kind": "dependencies", "summary": _line(report["summary"])})
    return report


def _line(s: dict) -> str:
    return f"{s['dependencies']} dependencies: {s['vulnerable']} vulnerable, {s['inconclusive']} inconclusive, {s['env_error']} not checked"
