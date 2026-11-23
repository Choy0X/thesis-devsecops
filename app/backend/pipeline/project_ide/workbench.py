"""Read-only provenance projection for existing chat scan records."""
from __future__ import annotations

from pathlib import Path
import re

from . import workspace
from .analysis import _regular
from .scans import _match_entry


def _recorded(row, index, origin, scan_id, *, package=None, static=False):
    source = row.get("evidence") if static else row
    source = source if isinstance(source, dict) else {}
    path = source.get("path")
    line = source.get("line")
    quote = source.get("quote")
    result = {"id": f"{scan_id}:{origin}:{index}", "origin": origin, "scanId": scan_id,
              "status": "lead" if static else "grounded",
              "title": row.get("class") or row.get("rule") or "Source finding",
              "summary": row.get("explanation") or row.get("message") or row.get("note") or "",
              "severity": row.get("severity"), "function": row.get("function")}
    if isinstance(path, str) and type(line) is int and line > 0 and isinstance(quote, str) and quote:
        result["source"] = {"path": path, "line": line, "quote": quote, "sha256": source.get("sha256"),
                            "freshness": "unknown"}
    if package:
        result["package"] = package
    return result


def _advisory_rows(scan_id, package, version, check, recommendation=None):
    if check.get("status") != "affected":
        return
    package = _package_identity(package, version)
    for advisory in check.get("advisories", []):
        cve = next((name for name in advisory.get("ids", []) if re.fullmatch(r"CVE-\d{4}-\d{4,}", name)), None)
        affected = []
        for entry in advisory.get("affected", []):
            if not _match_entry(package["ecosystem"], package["name"], version, entry):
                continue
            if version in entry.get("versions", []):
                affected.append("Includes " + version)
            for group in entry.get("ranges", []):
                if group.get("type") == "GIT":
                    continue
                labels = {"introduced": "From ", "fixed": "before ", "limit": "before ", "last_affected": "through "}
                words = [labels[key] + value for event in group.get("events", []) for key, value in event.items()
                         if key in labels and isinstance(value, str)]
                if words:
                    affected.append(", ".join(words).replace("From 0,", "From first release,"))
        yield {"id": f"{scan_id}:advisory:{package['id']}:{version}:{advisory['id']}", "origin": "advisory",
               "scanId": scan_id, "status": "advisory_match", "title": advisory["id"],
               "summary": advisory.get("summary") or advisory.get("details") or "Affected version matched.",
               "package": package, "advisory": {**advisory, "cve": cve,
                                                  "affected_versions": " / ".join(dict.fromkeys(affected))},
               "recommendation": recommendation,
               "lookup": check.get("lookup")}


def _package_identity(package, version):
    return {"id": package.get("package_id") or package.get("id"), "name": package.get("name"),
            "ecosystem": package.get("ecosystem"), "version": version}


def _coverage(check):
    if check.get("status") == "affected":
        return "match"
    if check.get("status") == "no_known_vulnerabilities" and (check.get("lookup") or {}).get("status") == "fresh":
        return "no_match"
    return "unavailable"


def _exact_checks(package):
    version = package.get("version")
    if version:
        return [(version, package.get("advisory_context") or {})]
    checks = package.get("checks") or {}
    return [(version, checks.get(version, {})) for version in package.get("resolved") or []]


def _fresh_coverage(check):
    return (check.get("lookup") or {}).get("status") == "fresh" and _coverage(check) != "unavailable"


def _package_key(package):
    return package.get("id"), package.get("version")


def _dependency_partial(result, mode):
    packages = result.get("packages") or []
    if not packages or result.get("problems"):
        return True
    for package in packages:
        checks = _exact_checks(package)
        if not checks or not all(_fresh_coverage(check) for _, check in checks):
            return True
        if mode == "source" and (package.get("status") != "ready_for_review"
                                 or (package.get("coverage") or {}).get("skipped")
                                 or ((package.get("analysis") or {}).get("coverage") or {}).get("partial")):
            return True
    return False


def _current_file(item, relative):
    try:
        path = workspace.bound(item["root"], relative)
        if _regular(path).st_size <= 4 * 1024 * 1024:
            return workspace.read(item, relative)["version"]
    except (OSError, ValueError):
        pass
    return None


def _assessment_rows(item, receipts):
    files = {}
    for receipt in receipts:
        assessment = receipt.get("assessment") or {}
        if assessment.get("status") != "finding" or not receipt.get("id"):
            continue
        if Path(receipt.get("workspace") or ".").resolve() != Path(item["root"]).resolve():
            continue
        claims = assessment.get("findings") or []
        quotes = {str(row.get("quote") or "").strip() for row in assessment.get("evidence") or []}
        quotes.discard("")
        matches = []
        if len(claims) == 1 and quotes:
            for relative, expected in (receipt.get("files") or {}).items():
                if relative.startswith("dependency:"):
                    continue
                if relative not in files:
                    try:
                        path = workspace.bound(item["root"], relative)
                        files[relative] = workspace.read(item, relative) if _regular(path).st_size <= 4 * 1024 * 1024 else None
                    except (OSError, ValueError):
                        files[relative] = None
                saved = files[relative]
                if saved and saved["version"] == expected:
                    matches.extend({"path": relative, "line": number, "quote": line.strip(), "sha256": expected, "freshness": "current"}
                                   for number, line in enumerate(saved["text"].splitlines(), 1) if line.strip() in quotes)
        for index, claim in enumerate(claims):
            row = {"id": f"assessment:{receipt['id']}:{index}", "origin": "assessment", "status": "recorded",
                   "title": claim.get("violated_invariant") or "Recorded assessment",
                   "summary": claim.get("summary") or ""}
            if len(matches) == 1:
                row["source"] = matches[0]
            yield row


def _dependency_source(package, source, scan_vault, owned_vault, files):
    if not source or not source["sha256"] or not scan_vault or not owned_vault or not package.get("source_path"):
        return
    try:
        root = Path(owned_vault).resolve(strict=True)
        run = Path(scan_vault).resolve(strict=True)
        snapshot = Path(package["source_path"]).resolve(strict=True)
        if run.parent != root or snapshot.parent.parent != run or snapshot.name != "review":
            return
        if not any(row.get("path") == source["path"] and row.get("sha256") == source["sha256"]
                   and row.get("status") == "text" for row in package.get("files", [])):
            return
        path = workspace.bound(snapshot, source["path"])
        identity = (path, source["line"], source["sha256"], source["quote"])
        if identity not in files:
            if _regular(path).st_size > 4 * 1024 * 1024:
                return
            saved = workspace.read({"root": snapshot}, source["path"])
            lines = saved["text"].splitlines()
            files[identity] = (saved["version"] == source["sha256"] and source["line"] <= len(lines)
                               and lines[source["line"] - 1].strip() == source["quote"].strip())
        source["freshness"] = "current" if files[identity] else "stale"
    except (OSError, ValueError):
        source["freshness"] = "stale"


def findings(item, runs, recorded, *, source_vault=None):
    raw, dependency, scans, coverage, evidence, advisory_coverage = list(recorded), [], [], [], [], {}
    current_files = {}
    snapshot_files = {}
    latest_coverage = {}
    for run in runs:
        if run["kind"] != "scan":
            continue
        scan_id, state = run["id"], run["state"]
        result, payload = run["result"] or {}, run["payload"]
        scope = payload.get("scope")
        top = result.get("findings") or []
        omitted = result.get("skipped") or []
        gated = sum(row.get("gate_ok") is not True for row in top) if scope == "model_review" else 0
        summaries = result.get("coverage") or {}
        status = state
        if state == "completed":
            status = result.get("status") or ("partial" if summaries.get("partial") else "complete")
            if scope == "model_review" and (gated or not result.get("reviewed")):
                status = "partial"
            if scope == "dependencies" and _dependency_partial(result, payload.get("dependency_mode")):
                status = "partial"
            if (result.get("agent") or {}).get("status") == "incomplete":
                status = "partial"
        scans.append({"id": scan_id, "scope": scope, "state": state, "status": status,
                      "mode": payload.get("dependency_mode"),
                      "reviewed": result.get("reviewed"), "skipped": len(omitted), "withheld": gated,
                      "error": result.get("error") or (result.get("reason") if state != "completed" else None)})
        if run["result"]:
            coverage.append(summaries)
        raw.extend(top)
        for index, row in enumerate(top):
            if scope == "model_review" and row.get("gate_ok") is not True:
                continue
            origin = "model_review" if scope == "model_review" else "project_code"
            entry = _recorded(row, index, origin, scan_id, static=origin != "model_review")
            source = entry.get("source")
            if scope == "model_review" and not source:
                continue
            if source and source["sha256"]:
                path = source["path"]
                if path not in current_files:
                    current_files[path] = _current_file(item, path)
                source["freshness"] = "current" if current_files[path] == source["sha256"] else "stale"
            evidence.append(entry)
            advisory_coverage[entry["id"]] = "unavailable"
        for index, package in enumerate(result.get("packages", [])):
            for version, check in _exact_checks(package):
                for row in _advisory_rows(scan_id, package, version, check, package.get("recommendation")):
                    evidence.append(row)
                    advisory_coverage[row["id"]] = "match"
                if state == "completed" and _fresh_coverage(check):
                    latest_coverage[_package_key(_package_identity(package, version))] = _coverage(check)
            version = package.get("version")
            check = package.get("advisory_context") or {}
            for position, row in enumerate((package.get("analysis") or {}).get("findings", [])):
                dependency.append({**row, "package_id": package.get("package_id"), "version": version,
                                   "source_origin": package.get("origin")})
                identity = _package_identity(package, version)
                entry = _recorded(row, f"{index}:{position}", "dependency_source", scan_id, package=identity, static=True)
                source = entry.get("source")
                lookup = _coverage(check) if version and identity["id"] else "unavailable"
                _dependency_source(package, source, result.get("vault"), source_vault, snapshot_files)
                if (state == "completed" and source and source["freshness"] == "current" and lookup != "unavailable"
                        and (check.get("lookup") or {}).get("status") == "fresh"):
                    entry["status"] = "grounded"
                evidence.append(entry)
                advisory_coverage[entry["id"]] = lookup

    for row in evidence:
        if row["origin"] == "dependency_source" and row["status"] == "grounded":
            latest = latest_coverage.get(_package_key(row["package"]))
            if latest:
                advisory_coverage[row["id"]] = latest
    evidence.extend(_assessment_rows(item, recorded))

    unique = {entry["id"]: entry for entry in evidence}
    return {"findings": raw + dependency, "coverage": coverage, "scans": scans, "evidence": list(unique.values()),
            "revision": item.get("revision"),
            "advisoryCoverageAvailable": bool(latest_coverage),
            "advisoryCoverage": advisory_coverage}
