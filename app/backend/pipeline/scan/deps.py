"""Dependency scanner: match a project's installed packages against OSV advisories.

Implements Chapter 4.3. Parses a Python dependency manifest, resolves each pinned
version, and matches it against the affected version ranges harvested from OSV
(``data/interim/osv_pypi.parquet``), so the scan runs fully offline against the
frozen snapshot used for the rest of the thesis.

Version comparison uses ``packaging.version`` (via ``depversions``), never string
comparison (a project convention: "1.10" < "1.9" as strings but not as versions).
Every range of an advisory is checked (introduced..fixed, ..last_affected,
explicit versions), and each finding names the first fixed and the latest safe
release to upgrade to, or says that no fixed release exists.

Each match becomes a finding carrying the advisory's severity label, so the same
three-class scheme used by the classification arms applies to dependency
findings. The finding records which rule matched (an explicit affected version, or
an introduced..fixed range), supporting the Chapter 9 requirement that every
finding be explainable.
"""
import re
import json
import pathlib
import argparse

import pandas as pd
from packaging.requirements import Requirement
from app.backend import paths

INTERIM = paths.INTERIM


def parse_manifest(path: pathlib.Path):
    """Return {package_lower: version_string_or_None} from a requirements file.

    Handles ``name==1.2.3`` pins, ``name>=x`` specifiers (version left None, so
    only explicit-version advisories match), and ``pip freeze`` output. For
    poetry.lock or Pipfile.lock, pass the lockfile and its ``name``/``version``
    pairs are read.
    """
    text = path.read_text(errors="ignore")
    deps = {}
    if path.name.endswith(".lock") or path.name == "poetry.lock":
        name = None
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r'name\s*=\s*"([^"]+)"', line)
            if m:
                name = m.group(1).lower()
            v = re.match(r'version\s*=\s*"([^"]+)"', line)
            if v and name:
                deps[name] = v.group(1)
                name = None
        return deps

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except Exception:  # ignored: not a valid requirement; the pinned-version pattern below is tried
            m = re.match(r"([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)", line)
            if m:
                deps[m.group(1).lower()] = m.group(2)
            continue
        pinned = None
        for spec in req.specifier:
            if spec.operator == "==":
                pinned = spec.version
        deps[req.name.lower()] = pinned
    return deps


def _legacy_entry(package, introduced, fixed, versions) -> dict:
    """Dumps parsed before the structured 'affected' column existed only keep
    comma-joined introduced / fixed lists, whose pairing is lost. Keep the
    original scanner's reading (first introduced .. first fixed) rather than guess
    pairs; re-running the ingest gives the full ranges."""
    def first(s):
        return next((x.strip() for x in (s or "").split(",") if x.strip()), None)
    r = {"introduced": first(introduced) or "0"}
    if first(fixed):
        r["fixed"] = first(fixed)
    return {"ecosystem": "PyPI", "package": package, "ranges": [r],
            "versions": [v.strip() for v in (versions or "").split(",") if v.strip()]}


def _advisories_by_package() -> dict:
    """{package_lower: [advisory]} with structured affected ranges: the working
    store's records first (they also carry the safe version), then the OSV dump."""
    from app.backend.pipeline import store
    from app.backend.pipeline.ingest import depversions
    out: dict[str, list] = {}
    seen: set = set()
    try:
        rows = store.records_with_affected(("osv_id", "cve_id", "summary", "cvss_vector", "osv_cwe", "affected"))
    except Exception:  # ignored: no advisory table means no local advisories
        rows = []
    for r in rows:
        for e in json.loads(r["affected"] or "[]"):
            if (e.get("ecosystem") or "").lower() == "pypi":
                out.setdefault(e["package"].lower(), []).append({**r, "entry": e})
                seen.add((r["osv_id"], e["package"].lower()))
    pq = INTERIM / "osv_pypi.parquet"
    if pq.exists():
        osv = pd.read_parquet(pq)
        osv = osv.astype(object).where(osv.notna(), None)          # NaN -> None
        if "affected" not in osv.columns:
            print("NOTE: osv_pypi.parquet predates the structured ranges; re-run the "
                  "ingest so every range of every advisory is checked")
        for r in osv.drop_duplicates(["osv_id", "package"]).to_dict("records"):
            pkg = str(r.get("package") or "").lower()
            if not pkg or (r["osv_id"], pkg) in seen:
                continue
            if r.get("affected"):
                entries = [e for e in json.loads(r["affected"]) if e["package"].lower() == pkg]
                entries = depversions.merge_packages(entries)
            else:
                entries = [_legacy_entry(r["package"], r.get("introduced"), r.get("fixed"), r.get("versions"))]
            for e in entries:
                out.setdefault(pkg, []).append({**r, "entry": e})
            seen.add((r["osv_id"], pkg))
    return out


def scan(manifest: str, output=None):
    from app.backend.pipeline.ingest import depversions
    adv_by_pkg = _advisories_by_package()

    deps = parse_manifest(pathlib.Path(manifest))
    findings = []
    for pkg, ver in deps.items():
        for a in adv_by_pkg.get(pkg, []):
            e = a["entry"]
            if ver is None or not depversions.is_affected("PyPI", ver, e):
                continue
            ev = e if "status" in e else {**e, **depversions.evaluate(e, None)}
            rule = ("explicit" if ver in set(e.get("versions") or [])
                    else "range:" + depversions.describe_ranges(e))
            safe = ev.get("safe_version")
            rec = (f"upgrade to {safe}" + (f" (first fixed: {ev['first_fixed']})"
                                           if ev.get("first_fixed") and ev["first_fixed"] != safe else "")
                   if safe else "no fixed release: replace the package or mitigate")
            findings.append({
                "package": pkg, "installed": ver,
                "advisory": a["osv_id"], "cve": a.get("cve_id"),
                "severity_vector": a.get("cvss_vector"),
                "osv_severity": a.get("osv_severity"),
                "cwe": a.get("osv_cwe"),
                "affected_versions": depversions.describe_ranges(e),
                "fixed_in": ev.get("first_fixed"), "safe_version": safe,
                "dependency_status": ev.get("status"), "recommendation": rec,
                "match_rule": rule, "summary": a.get("summary"),
            })

    # Deduplicate: OSV carries several affected-package / severity rows per
    # advisory, so the same (package, advisory) can match more than once.
    seen, deduped = set(), []
    for f in findings:
        key = (f["package"], f["advisory"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    findings = deduped

    result = {"manifest": manifest, "n_dependencies": len(deps),
              "n_findings": len(findings), "findings": findings}
    text = json.dumps(result, indent=2, default=str)
    if output:
        pathlib.Path(output).write_text(text)
    print(f"{len(deps)} dependencies scanned, {len(findings)} findings")
    for f in findings[:20]:
        print(f"  {f['package']}=={f['installed']}  {f['cve'] or f['advisory']}  "
              f"({f['match_rule']}) -> {f['recommendation']}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", help="requirements.txt / pip freeze / poetry.lock")
    ap.add_argument("--output", default=None)
    a = ap.parse_args()
    scan(a.manifest, a.output)
