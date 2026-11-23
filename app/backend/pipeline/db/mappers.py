"""Legacy rows <-> embedded documents (``schema.py``).

The application keeps seeing today's flat dicts (``records``, ``fix_commits``, ``code_pairs``, ... with TEXT dates, 0/1 booleans, delimited
strings and JSON text); the repository stores the redesigned documents. These functions are the boundary:

* ``*_doc`` functions turn a legacy dict into documents (what a writer hands over);
* ``*_row`` functions rebuild the legacy dict from documents (what readers get).

Copies of other entities' data on a pair (explanation, commit message, dates, parent info, CVE lists) are not stored: ``pair_row`` resolves them
from the advisory and the commit. When a writer hands over a value that differs from what the owners give, the difference is kept in
``overrides`` so nothing is lost; the counts are what the migration report and the tests look at.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from app.backend.pipeline.ingest import attribution

# ---------------------------------------------------------------------------------------------------------------------------------
# primitives

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_dt(value) -> dt.datetime | None:
    """A date in any of the formats the old store used (RFC 3339 with or without fraction and zone, a bare day, a git offset) -> aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    m = _DATE_RE.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, zone = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    out = dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), micro, tzinfo=dt.timezone.utc)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        out -= sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return out


def fmt_dt(value) -> str | None:
    """A stored date as the ISO text the API has always returned (``2026-10-02T22:16:53.824484Z``; the fraction only when there is one)."""
    d = parse_dt(value)
    if d is None:
        return None
    base = d.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{d.microsecond:06d}Z" if d.microsecond else f"{base}Z"


def loads(text, default=None):
    """JSON text -> value; NULL, empty and unparsable text give ``default``."""
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dumps(value) -> str | None:
    return None if value is None else json.dumps(value)


def split_list(text, sep: str) -> list[str]:
    return [p.strip() for p in str(text).split(sep) if p.strip()] if text else []


def clean_nan(row: dict) -> dict:
    """Copy ``row`` with pandas' missing-value NaN normalised to None before document encoding; downstream text readers expect a missing value."""
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in row.items()}


def put(doc: dict, key: str, value) -> None:
    """Set ``key`` unless the value is absent (None or an empty string/list): absent means null in the model."""
    if value is None or value == "" or value == [] or value == {}:
        return
    doc[key] = value


def record_id_of(pair_key: str) -> str:
    return "rec_" + hashlib.sha1(pair_key.encode("utf-8")).hexdigest()[:16]


def advisory_text(summary, details) -> str:
    """The same normaliser as ``context.advisory_text`` (kept local so this module imports nothing from the pipeline)."""
    s, d = (summary or "").strip(), (details or "").strip()
    if s and d and d.startswith(s):
        s = ""
    return "\n\n".join(x for x in (s, d) if x)


def _b(value) -> bool:
    return bool(value) if value is not None else False


# ---------------------------------------------------------------------------------------------------------------------------------
# advisories (was records + advisory_packages)

CHECK_KEYS = ("first_fixed", "latest_fixed", "highest_affected", "open_range", "registry", "latest_version", "safe_version",
              "safe_is_prerelease", "status")

#: legacy ``records`` columns -> where they live now (or why they are gone)
RECORDS_MAPPED = {
    "id": "_id/seq", "key": "key", "osv_id": "osv_id", "cve_id": "cve_id", "package": "packages[].name", "summary": "summary", "details": "details",
    "published": "published_at", "modified": "modified_at", "withdrawn": "withdrawn_at", "cvss_vector": "cvss.v3", "cvss_v4_vector": "cvss.v4",
    "cvss_score": "cvss.manual_score", "osv_cwe": "cwe[]", "cwe": "cwe_manual[]", "source": "origin", "ecosystem": "ecosystem",
    "language": "mining.languages[]", "language_source": "mining.language_source", "fix_commits": "fix_links", "fix_commit_count": "mining.fix_commit_count",
    "code_status": "mining.code_status", "pair_count": "mining.pair_count", "affected": "packages[] + deps.packages[]",
    "affected_versions": "deps.headline.affected_versions", "fixed_version": "deps.headline.fixed_version",
    "safe_version": "deps.headline.safe_version", "dependency_status": "deps.status", "deps_checked_at": "deps.checked_at",
    "osv_severity": "osv_severity", "created": "created_at", "updated": "updated_at", "advisory_kind": "kind", "aliases": "aliases[]",
    "source_urls": "refs[]", "source_record_hash": "raw_sha256",
}
RECORDS_DROPPED = {
    "code_before": "a copy of the best pair's code (23,053 of 272,232 records); mining.best_pair_id points at the pair",
    "code_after": "a copy of the best pair's code", "filename": "a copy of the best pair's path and function",
}


def advisory_doc(row: dict, packages: list[tuple[str, str]] | None = None) -> dict:
    """One ``records`` row (and its ``advisory_packages`` pairs, if any) -> an ``advisories`` document."""
    doc: dict = {"_id": row["id"], "seq": row["id"], "key": row["key"]}
    for k in ("osv_id", "cve_id", "ecosystem", "summary", "details", "osv_severity"):
        put(doc, k, row.get(k))
    doc["kind"] = row.get("advisory_kind") or ("withdrawn" if row.get("withdrawn") else "malware" if str(row.get("osv_id") or "").startswith("MAL-") else "vulnerability")
    put(doc, "aliases", loads(row.get("aliases"), []))
    for src, dst in (("published", "published_at"), ("modified", "modified_at"), ("withdrawn", "withdrawn_at"),
                     ("created", "created_at"), ("updated", "updated_at")):
        put(doc, dst, parse_dt(row.get(src)))
    cvss = {}
    put(cvss, "v3", row.get("cvss_vector"))
    put(cvss, "v4", row.get("cvss_v4_vector"))
    put(cvss, "manual_score", row.get("cvss_score"))
    put(doc, "cvss", cvss)
    put(doc, "cwe", split_list(row.get("osv_cwe"), ";"))
    put(doc, "cwe_manual", split_list(row.get("cwe"), ";"))
    put(doc, "refs", loads(row.get("source_urls"), []))
    put(doc, "raw_sha256", row.get("source_record_hash"))
    put(doc, "origin", row.get("source"))
    affected = loads(row.get("affected"), [])
    adv_pkgs, check_pkgs = [], []
    for e in affected if isinstance(affected, list) else []:
        base = {"ecosystem": e.get("ecosystem"), "name": e.get("package")}
        adv = {**base, **{k: v for k, v in e.items() if k not in CHECK_KEYS and k not in ("ecosystem", "package")}}
        adv_pkgs.append({k: v for k, v in adv.items() if v is not None})
        chk = {k: e[k] for k in CHECK_KEYS if k in e}
        if chk:
            check_pkgs.append({**{k: v for k, v in base.items() if v is not None}, **chk})
    if not adv_pkgs:
        adv_pkgs = [{"ecosystem": e, "name": n} for e, n in (packages or [])] or \
                   [{"ecosystem": row.get("ecosystem"), "name": n} for n in split_list(row.get("package"), ",")]
    put(doc, "packages", adv_pkgs)
    mining = {}
    put(mining, "code_status", row.get("code_status"))
    put(mining, "languages", split_list(row.get("language"), ","))
    put(mining, "language_source", row.get("language_source"))
    for k in ("fix_commit_count", "pair_count"):
        if row.get(k) is not None:
            mining[k] = row[k]
    put(doc, "mining", mining)
    deps = {}
    put(deps, "status", row.get("dependency_status"))
    put(deps, "checked_at", parse_dt(row.get("deps_checked_at")))
    head = {}
    for k in ("affected_versions", "fixed_version", "safe_version"):
        put(head, k, row.get(k))
    put(deps, "headline", head)
    put(deps, "packages", check_pkgs)
    put(doc, "deps", deps)
    put(doc, "link_recovery", row["link_recovery"] if isinstance(row.get("link_recovery"), dict) else loads(row.get("link_recovery"), {}))
    return doc


def link_docs(row: dict) -> list[dict]:
    """The ``fix_links`` of a ``records`` row (from its ``fix_commits`` JSON pointer list)."""
    out = {}
    for p in loads(row.get("fix_commits"), []) or []:
        if not p.get("commit_key"):
            continue
        link = {"advisory_id": row["id"], "advisory_key": row["key"], "commit_key": p["commit_key"]}
        existing = out.setdefault(p["commit_key"], link)
        for field, plural, single in (("via", "vias", "via"), ("sources", "sources", "source")):
            values = p.get(plural) or ([p[single]] if p.get(single) else [])
            existing[field] = sorted(set(existing.get(field, [])) | set(values))
    return list(out.values())


def _pointer(link: dict) -> dict:
    host, _, rest = link["commit_key"].partition("/")
    owner, _, rest = rest.partition("/")
    repo, _, sha = rest.partition("@")
    out = {"host": host, "owner": owner, "repo": repo, "sha": sha, "repo_url": f"https://{host}/{owner}/{repo}", "commit_key": link["commit_key"]}
    if link.get("via"):
        out["via"] = min(link["via"])                    # one of possibly several; sorted so it does not depend on the order they were added in
    if len(link.get("via", [])) > 1:
        out["vias"] = sorted(link["via"])
    if link.get("sources"):
        out["source"] = min(link["sources"])
    if len(link.get("sources", [])) > 1:
        out["sources"] = sorted(link["sources"])
    return out


def advisory_row(doc: dict, links: list[dict] | None = None) -> dict:
    """An ``advisories`` document -> the legacy ``records`` dict (``fix_commits`` is rebuilt from ``links`` when given)."""
    cv, mining, deps = doc.get("cvss", {}), doc.get("mining", {}), doc.get("deps", {})
    pkgs, checks = doc.get("packages", []), {(c.get("ecosystem"), c.get("name")): c for c in deps.get("packages", [])}
    affected = []
    for p in pkgs:
        if not any(k in p for k in ("ranges", "versions", "versions_total")) and (p.get("ecosystem"), p.get("name")) not in checks:
            continue
        entry = {"ecosystem": p.get("ecosystem"), "package": p.get("name"), **{k: v for k, v in p.items() if k not in ("ecosystem", "name")}}
        entry.update({k: v for k, v in checks.get((p.get("ecosystem"), p.get("name")), {}).items() if k not in ("ecosystem", "name")})
        affected.append(entry)
    names = []
    for p in pkgs:
        if p.get("name") and p["name"] not in names:
            names.append(p["name"])
    head = deps.get("headline", {})
    row = {
        "id": doc["_id"], "key": doc["key"], "osv_id": doc.get("osv_id"), "cve_id": doc.get("cve_id"), "ecosystem": doc.get("ecosystem"),
        "package": ", ".join(names) or None, "summary": doc.get("summary"), "details": doc.get("details"),
        "published": fmt_dt(doc.get("published_at")), "modified": fmt_dt(doc.get("modified_at")), "withdrawn": fmt_dt(doc.get("withdrawn_at")),
        "cvss_vector": cv.get("v3"), "cvss_v4_vector": cv.get("v4"), "cvss_score": cv.get("manual_score"),
        "osv_cwe": ";".join(doc.get("cwe", [])) or None, "cwe": ";".join(doc.get("cwe_manual", [])) or None,
        "code_before": None, "code_after": None, "filename": None, "source": doc.get("origin"),
        "language": ",".join(mining.get("languages", [])) or None, "language_source": mining.get("language_source"),
        "fix_commits": dumps([_pointer(link) for link in links]) if links else None,
        "fix_commit_count": mining.get("fix_commit_count"), "code_status": mining.get("code_status"), "pair_count": mining.get("pair_count"),
        "affected": dumps(affected) if affected else None, "affected_versions": head.get("affected_versions"),
        "fixed_version": head.get("fixed_version"), "safe_version": head.get("safe_version"), "dependency_status": deps.get("status"),
        "deps_checked_at": fmt_dt(deps.get("checked_at")), "osv_severity": doc.get("osv_severity"),
        "created": fmt_dt(doc.get("created_at")), "updated": fmt_dt(doc.get("updated_at")), "advisory_kind": doc.get("kind"),
        "aliases": dumps(doc.get("aliases")) if doc.get("aliases") else None,
        "source_urls": dumps(doc.get("refs")) if doc.get("refs") else None, "source_record_hash": doc.get("raw_sha256"),
        "link_recovery": doc.get("link_recovery"),
    }
    return row


# ---------------------------------------------------------------------------------------------------------------------------------
# fix commits

COMMITS_MAPPED = {
    "id": "_id", "commit_key": "commit_key", "host": "repo.host", "owner": "repo.owner", "repo": "repo.name", "sha": "sha", "repo_url": "repo.url",
    "advisory_ids": "fix_links", "cve_ids": "fix_links", "sources": "fix_links.sources", "via": "fix_links.via", "status": "queue.status",
    "error": "queue.reason", "attempts": "queue.attempts", "parent_sha": "git.parent_sha", "commit_message": "git.message",
    "commit_date": "git.committed_at", "files_changed": "git.files_changed", "pairs": "git.pairs", "mined_at": "mined_at", "created": "created_at",
    "updated": "updated_at", "langs": "git.langs", "detected_langs": "git.detected_langs", "expected_langs": "queue.expected_langs",
    "fix_check": "verification.fix_check", "fix_check_reason": "verification.reason", "link": "verification.link",
    "fix_check_package": "verification.package", "link_evidence": "evidence.signals", "evidence_level": "evidence.level",
    "commit_kind": "git.kind", "parent_selection_reason": "git.parents.selection", "parent_count": "git.parents.count",
    "ecosystems": "queue.ecosystems", "campaign_id": "queue.campaign_id", "acquisition": "git.acquisition",
}
COMMITS_DROPPED: dict[str, str] = {}


def commit_doc(row: dict, links: list[dict] | None = None) -> dict:
    doc: dict = {"_id": row["id"], "commit_key": row["commit_key"]}
    repo = {}
    for src, dst in (("host", "host"), ("owner", "owner"), ("repo", "name"), ("repo_url", "url")):
        put(repo, dst, row.get(src))
    put(repo, "identity", f"{row.get('host')}/{row.get('owner')}/{row.get('repo')}".lower() if row.get("host") else None)
    put(doc, "repo", repo)
    put(doc, "sha", row.get("sha"))
    queue = {}
    put(queue, "status", row.get("status"))
    put(queue, "reason", row.get("error"))
    if row.get("attempts") is not None:
        queue["attempts"] = row["attempts"]
    put(queue, "expected_langs", loads(row.get("expected_langs"), []))
    put(queue, "ecosystems", loads(row.get("ecosystems"), []))
    put(queue, "campaign_id", row.get("campaign_id"))
    put(doc, "queue", queue)
    git = {}
    put(git, "message", row.get("commit_message"))
    put(git, "acquisition", row.get("acquisition"))
    put(git, "committed_at", parse_dt(row.get("commit_date")))
    put(git, "parent_sha", row.get("parent_sha"))
    parents = {}
    if row.get("parent_count") is not None:
        parents["count"] = row["parent_count"]
    put(parents, "selection", row.get("parent_selection_reason"))
    put(git, "parents", parents)
    put(git, "kind", row.get("commit_kind"))
    put(git, "langs", loads(row.get("langs"), {}))
    put(git, "detected_langs", loads(row.get("detected_langs"), []))
    for k in ("files_changed", "pairs"):
        if row.get(k) is not None:
            git[k] = row[k]
    put(doc, "git", git)
    ver = {}
    for src, dst in (("fix_check", "fix_check"), ("fix_check_reason", "reason"), ("fix_check_package", "package"), ("link", "link")):
        put(ver, dst, row.get(src))
    put(doc, "verification", ver)
    ev = {}
    put(ev, "level", row.get("evidence_level"))
    put(ev, "signals", loads(row.get("link_evidence"), []))
    put(doc, "evidence", ev)
    put(doc, "mined_at", parse_dt(row.get("mined_at")))
    put(doc, "created_at", parse_dt(row.get("created")))
    put(doc, "updated_at", parse_dt(row.get("updated")))
    return doc


def commit_row(doc: dict, links: list[dict] | None = None, advisories: dict[int, dict] | None = None) -> dict:
    """A ``fix_commits`` document -> the legacy dict; the four JSON link lists are rebuilt from the commit's ``fix_links``."""
    repo, queue, git, ver, ev = (doc.get(k, {}) for k in ("repo", "queue", "git", "verification", "evidence"))
    parents = git.get("parents", {})
    adv = advisories or {}
    linked = [adv.get(link["advisory_id"]) for link in links or []]
    row = {
        "id": doc["_id"], "commit_key": doc["commit_key"], "host": repo.get("host"), "owner": repo.get("owner"), "repo": repo.get("name"),
        "sha": doc.get("sha"), "repo_url": repo.get("url"),
        "advisory_ids": dumps([a.get("osv_id") or a.get("cve_id") for a in linked if a]) if links else None,
        "cve_ids": dumps([a["cve_id"] for a in linked if a and a.get("cve_id")]) if links else None,
        "sources": dumps(sorted({s for link in links or [] for s in link.get("sources", [])})) if links else None,
        "via": dumps(sorted({v for link in links or [] for v in link.get("via", [])})) if links else None,
        "status": queue.get("status"), "error": queue.get("reason"), "attempts": queue.get("attempts"),
        "parent_sha": git.get("parent_sha"), "commit_message": git.get("message"), "commit_date": fmt_dt(git.get("committed_at")),
        "files_changed": git.get("files_changed"), "pairs": git.get("pairs"), "mined_at": fmt_dt(doc.get("mined_at")),
        "created": fmt_dt(doc.get("created_at")), "updated": fmt_dt(doc.get("updated_at")),
        "langs": dumps(git.get("langs")) if git.get("langs") else None,
        "detected_langs": dumps(git.get("detected_langs")) if git.get("detected_langs") else None,
        "expected_langs": dumps(queue.get("expected_langs")) if queue.get("expected_langs") else None,
        "ecosystems": dumps(queue.get("ecosystems")) if queue.get("ecosystems") else None, "campaign_id": queue.get("campaign_id"),
        "fix_check": ver.get("fix_check"), "fix_check_reason": ver.get("reason"),
        "link": ver.get("link"),
        "fix_check_package": ver.get("package"), "link_evidence": dumps(ev.get("signals")) if ev.get("signals") is not None else None,
        "evidence_level": ev.get("level"), "commit_kind": git.get("kind"),
        "parent_selection_reason": parents.get("selection"), "parent_count": parents.get("count"), "acquisition": git.get("acquisition"),
    }
    return row


# ---------------------------------------------------------------------------------------------------------------------------------
# small entities

REPOSITORIES_MAPPED = {"repo_identity": "_id", "host": "host", "owner": "owner", "repo": "name", "license_spdx": "license.spdx",
                       "data_usage_status": "license.usage", "checked_at": "license.checked_at"}
COMMIT_FILES_MAPPED = {"commit_key": "commit_key", "path": "path", "old_path": "old_path (only when renamed)", "status": "status",
                       "language": "language", "category": "category", "additions": "additions", "deletions": "deletions",
                       "blob_sha": "blob_sha", "patch": "patch"}
PACKAGE_VERSIONS_MAPPED = {"ecosystem": "ecosystem (canonical case) + registry_variant", "package": "name", "versions": "versions[].v",
                           "ok": "ok", "fetched_at": "fetched_at", "dates": "versions[].released_at"}


def repository_doc(row: dict) -> dict:
    doc = {"_id": row["repo_identity"]}
    for k in ("host", "owner"):
        put(doc, k, row.get(k))
    put(doc, "name", row.get("repo"))
    lic = {}
    put(lic, "spdx", row.get("license_spdx"))
    put(lic, "usage", row.get("data_usage_status"))
    put(lic, "checked_at", parse_dt(row.get("checked_at")))
    put(doc, "license", lic)
    return doc


def repository_row(doc: dict) -> dict:
    lic = doc.get("license", {})
    return {"repo_identity": doc["_id"], "host": doc.get("host"), "owner": doc.get("owner"), "repo": doc.get("name"),
            "license_spdx": lic.get("spdx"), "data_usage_status": lic.get("usage"), "checked_at": fmt_dt(lic.get("checked_at"))}


def file_doc(row: dict) -> dict:
    doc = {"commit_key": row["commit_key"], "path": row["path"]}
    if row.get("old_path") and row["old_path"] != row["path"]:
        doc["old_path"] = row["old_path"]
    for k in ("status", "language", "category", "additions", "deletions", "blob_sha", "patch"):
        if row.get(k) is not None:
            doc[k] = row[k]
    return doc


def file_row(doc: dict) -> dict:
    return {"commit_key": doc["commit_key"], "path": doc["path"], "old_path": doc.get("old_path", doc["path"]), "status": doc.get("status"),
            "language": doc.get("language"), "category": doc.get("category"), "additions": doc.get("additions"),
            "deletions": doc.get("deletions"), "blob_sha": doc.get("blob_sha"), "patch": doc.get("patch")}


def registry_doc(row: dict) -> dict:
    eco = row["ecosystem"]
    base, _, variant = eco.partition(":")
    doc: dict = {"ecosystem": base, "name": row["package"]}
    if variant:
        doc["registry_variant"] = variant
    versions = loads(row.get("versions"), []) or []
    dates = loads(row.get("dates"), None)
    doc["versions"] = [({"v": v, "released_at": parse_dt(dates[v])} if dates and dates.get(v) else {"v": v}) for v in versions]
    doc["dates_fetched"] = dates is not None
    doc["ok"] = bool(row.get("ok"))
    if not doc["ok"] and type(row.get("negative_status")) is int and row["negative_status"] in (404, 410):
        doc["negative_status"] = row["negative_status"]
    put(doc, "fetched_at", parse_dt(row.get("fetched_at")))
    return doc


def registry_row(doc: dict) -> dict:
    versions = doc.get("versions", [])
    dates = {v["v"]: fmt_dt(v["released_at"])[:19] for v in versions if v.get("released_at")} if doc.get("dates_fetched") else None
    eco = doc["ecosystem"] + (":" + doc["registry_variant"] if doc.get("registry_variant") else "")
    return {"ecosystem": eco, "package": doc["name"], "versions": dumps([v["v"] for v in versions]), "ok": 1 if doc.get("ok") else 0,
            "fetched_at": (fmt_dt(doc.get("fetched_at")) or "")[:26].rstrip("Z") or None, "dates": dumps(dates) if dates is not None else None,
            **({"negative_status": doc["negative_status"]} if not doc.get("ok") and type(doc.get("negative_status")) is int and doc["negative_status"] in (404, 410) else {})}


# ---------------------------------------------------------------------------------------------------------------------------------
# code pairs (+ pair_code + pair_enrichments)

PAIRS_MAPPED = {
    "id": "_id/seq", "pair_key": "pair_key (+ record_id)", "commit_key": "commit_key", "cve_id": "subject.cve_id", "cve_ids": "resolved from fix_links",
    "advisory_ids": "resolved from fix_links", "ghsa_id": "resolved from fix_links", "ecosystem": "subject.ecosystem", "package": "subject.package",
    "cwe": "subject.cwe[]", "cvss_vector": "resolved from the advisory", "published": "resolved from the advisory (overrides when different)",
    "commit_date": "resolved from the commit", "repo_url": "repo_url", "fix_commit": "resolved from the commit (overrides when different)",
    "parent_commit": "resolved from the commit", "file_path": "unit.file", "language": "unit.language", "function_name": "unit.function",
    "granularity": "unit.granularity", "is_test": "flags.is_test", "tangled": "flags.tangled", "code_before": "pair_code.before",
    "code_after": "pair_code.after", "diff": "pair_code.diff", "commit_message": "resolved from the commit (overrides when different)",
    "code_available": "flags.available", "skip_reason": "flags.skip_reason", "content_hash": "extraction.content_hash", "source": "origin.sources[]",
    "explanation": "resolved from the advisory (overrides when different)", "root_cause_llm": "pair_enrichments.root_cause",
    "secure_pattern_llm": "pair_enrichments.secure_pattern", "explanation_llm": "pair_enrichments.explanation", "llm_model": "pair_enrichments.model",
    "llm_prompt_version": "pair_enrichments.prompt_version", "created": "created_at", "updated": "updated_at", "edited_at": "edited_at",
    "changed_lines": "extraction.changed_lines", "span": "unit.span", "old_path": "unit.renamed_from", "parent_count": "resolved from the commit",
    "parent_selection_reason": "resolved from the commit", "quarantine_reason": "flags.quarantine[]", "before_blob_sha": "extraction.blobs.before",
    "after_blob_sha": "extraction.blobs.after", "extraction_version": "extraction.version", "parse_status": "extraction.parse.status",
    "parse_failure_reason": "extraction.parse.reason", "function_name_before": "unit.function_before", "attribution_basis": "attribution.basis",
    "attribution_score": "computed from attribution.basis", "explanation_source": "explanation.source",
    "needs_explanation_enrichment": "explanation.needs_enrichment", "original_advisory_reference": "pair_enrichments.original_advisory_reference",
    "match_kind": "extraction.match.kind", "match_confidence": "extraction.match.confidence", "extraction_meta": "extraction.meta",
}
PAIRS_DROPPED: dict[str, str] = {}
ATTRIBUTION_SCORES = attribution.SCORES                       # one table for both stores (ingest/attribution.TIERS)


def _lines(code) -> int | None:
    return None if code is None else code.count("\n") + 1


def _has_code(before, after) -> bool:
    return bool((before or "").strip(" \t\n\r")) and bool((after or "").strip(" \t\n\r"))


def resolved_copies(commit: dict | None, advisories: list[dict]) -> dict:
    """The values a pair row used to repeat, as the owners give them now (the advisories are those linked to the pair's commit)."""
    git = (commit or {}).get("git", {})
    parents = git.get("parents", {})
    first = next((a for a in advisories if advisory_text(a.get("summary"), a.get("details"))), advisories[0] if advisories else None)
    ghsa = next((a.get("osv_id") for a in advisories if str(a.get("osv_id") or "").startswith("GHSA-")), None)
    cves = [a["cve_id"] for a in advisories if a.get("cve_id")]
    return {
        "explanation": advisory_text(first.get("summary"), first.get("details"))[:4000] if first else None,
        "commit_message": git.get("message"), "commit_date": fmt_dt(git.get("committed_at")),
        "repo_url": (commit or {}).get("repo", {}).get("url"), "fix_commit": (commit or {}).get("full_sha") or (commit or {}).get("sha"),
        "parent_commit": git.get("parent_sha"), "parent_count": parents.get("count"), "parent_selection_reason": parents.get("selection"),
        "cve_ids": dumps(cves) if cves else None, "advisory_ids": dumps([a.get("osv_id") or a.get("cve_id") for a in advisories]) if advisories else None,
        "ghsa_id": ghsa, "cvss_vector": (first or {}).get("cvss", {}).get("v3") or (first or {}).get("cvss", {}).get("v4"),
        "published": fmt_dt((first or {}).get("published_at")),
    }


OVERRIDABLE = ("explanation", "commit_message", "commit_date", "repo_url", "fix_commit", "parent_commit", "parent_count",
               "parent_selection_reason", "cve_ids", "advisory_ids", "ghsa_id", "cvss_vector", "published")


def pair_docs(row: dict, commit: dict | None, advisories: list[dict]) -> tuple[dict, dict, dict | None]:
    """One ``code_pairs`` row -> (pair, pair_code, pair_enrichment or None). ``commit`` and ``advisories`` are the owners' documents; a copy
    on the row that differs from what they give is kept in ``overrides``."""
    pid = row["id"]
    kind = "manual" if row.get("source") == "manual" or str(row.get("pair_key", "")).startswith("manual|") else "mined"
    pair: dict = {"_id": pid, "seq": pid, "pair_key": row["pair_key"], "record_id": record_id_of(row["pair_key"])}
    put(pair, "commit_key", row.get("commit_key"))
    put(pair, "repo_url", row.get("repo_url"))
    pair["origin"] = {"kind": kind, "sources": split_list(row.get("source"), ",")}
    subject = {}
    put(subject, "cve_id", row.get("cve_id"))
    put(subject, "ecosystem", row.get("ecosystem"))
    put(subject, "package", row.get("package"))
    put(subject, "cwe", split_list(row.get("cwe"), ";"))
    if advisories:
        subject["advisory_id"] = advisories[0]["_id"]
    put(pair, "subject", subject)
    unit = {"granularity": row.get("granularity"), "file": row.get("file_path")}
    put(unit, "renamed_from", row["old_path"] if row.get("old_path") and row["old_path"] != row.get("file_path") else None)
    put(unit, "function", row.get("function_name"))
    put(unit, "function_before", row.get("function_name_before"))
    put(unit, "span", row.get("span"))
    put(unit, "language", row.get("language"))
    pair["unit"] = {k: v for k, v in unit.items() if v is not None}
    flags = {"available": _b(row.get("code_available")), "is_test": _b(row.get("is_test")), "tangled": _b(row.get("tangled")),
             "quarantine": [row["quarantine_reason"]] if row.get("quarantine_reason") else []}
    put(flags, "skip_reason", row.get("skip_reason"))
    pair["flags"] = flags
    ext: dict = {}
    put(ext, "version", row.get("extraction_version"))
    parse = {}
    put(parse, "status", row.get("parse_status"))
    put(parse, "reason", row.get("parse_failure_reason"))
    put(ext, "parse", parse)
    put(ext, "content_hash", row.get("content_hash"))
    match = {}
    put(match, "kind", row.get("match_kind"))
    put(match, "confidence", row.get("match_confidence"))
    put(ext, "match", match)
    if row.get("extraction_meta"):
        ext["meta"] = json.loads(row["extraction_meta"])
    if row.get("changed_lines") is not None:
        ext["changed_lines"] = row["changed_lines"]
    ext["lines"] = _lines(row.get("code_before")) if row.get("code_before") is not None else None
    ext["has_code"] = _has_code(row.get("code_before"), row.get("code_after"))
    blobs = {}
    put(blobs, "before", row.get("before_blob_sha"))
    put(blobs, "after", row.get("after_blob_sha"))
    put(ext, "blobs", blobs)
    pair["extraction"] = {k: v for k, v in ext.items() if v is not None}
    if row.get("attribution_basis"):
        pair["attribution"] = {"basis": row["attribution_basis"]}
    expl = {}
    put(expl, "source", row.get("explanation_source"))
    if row.get("needs_explanation_enrichment") is not None:
        expl["needs_enrichment"] = _b(row["needs_explanation_enrichment"])
    put(pair, "explanation", expl)
    link = {}
    if commit:
        put(link, "fix_check", commit.get("verification", {}).get("fix_check"))
        put(link, "evidence_level", commit.get("evidence", {}).get("level"))
        put(link, "commit_kind", commit.get("git", {}).get("kind"))
    put(pair, "link", link)
    pair["enriched"] = row.get("root_cause_llm") is not None
    put(pair, "edited_at", parse_dt(row.get("edited_at")))
    put(pair, "created_at", parse_dt(row.get("created")))
    put(pair, "updated_at", parse_dt(row.get("updated")))
    owned = resolved_copies(commit, advisories)
    overrides = {k: row[k] for k in OVERRIDABLE if row.get(k) is not None and row[k] != owned.get(k)}
    put(pair, "overrides", overrides)
    code = {"_id": pid}
    for src, dst in (("code_before", "before"), ("code_after", "after"), ("diff", "diff")):
        if row.get(src) is not None:
            code[dst] = row[src]
    enrichment = None
    if any(row.get(k) is not None for k in ("root_cause_llm", "secure_pattern_llm", "explanation_llm", "llm_model", "llm_prompt_version",
                                            "original_advisory_reference")):
        enrichment = {"_id": pid}
        for src, dst in (("root_cause_llm", "root_cause"), ("secure_pattern_llm", "secure_pattern"), ("explanation_llm", "explanation"),
                         ("llm_model", "model"), ("llm_prompt_version", "prompt_version"),
                         ("original_advisory_reference", "original_advisory_reference")):
            put(enrichment, dst, row.get(src))
    return pair, code, enrichment


def pair_row(pair: dict, code: dict | None, enrichment: dict | None, commit: dict | None, advisories: list[dict]) -> dict:
    """The legacy ``code_pairs`` dict from its documents and the owners' documents (the resolved copies come from them)."""
    subj, unit, flags, ext = pair.get("subject", {}), pair.get("unit", {}), pair.get("flags", {}), pair.get("extraction", {})
    code, enr = code or {}, enrichment or {}
    own = {**resolved_copies(commit, advisories), **pair.get("overrides", {})}
    basis = pair.get("attribution", {}).get("basis")
    row = {
        "id": pair["_id"], "pair_key": pair["pair_key"], "commit_key": pair.get("commit_key"), "cve_id": subj.get("cve_id"),
        "cve_ids": own.get("cve_ids"), "advisory_ids": own.get("advisory_ids"), "ghsa_id": own.get("ghsa_id"),
        "ecosystem": subj.get("ecosystem"), "package": subj.get("package"), "cwe": ";".join(subj.get("cwe", [])) or None,
        "cvss_vector": own.get("cvss_vector"), "published": own.get("published"), "commit_date": own.get("commit_date"),
        "repo_url": pair.get("repo_url") or own.get("repo_url"), "fix_commit": own.get("fix_commit"), "parent_commit": own.get("parent_commit"),
        "file_path": unit.get("file"), "language": unit.get("language"), "function_name": unit.get("function"),
        "granularity": unit.get("granularity"), "is_test": int(bool(flags.get("is_test"))), "tangled": int(bool(flags.get("tangled"))),
        "code_before": code.get("before"), "code_after": code.get("after"), "diff": code.get("diff"),
        "commit_message": own.get("commit_message"), "code_available": int(bool(flags.get("available"))), "skip_reason": flags.get("skip_reason"),
        "content_hash": ext.get("content_hash"), "source": ",".join(pair.get("origin", {}).get("sources", [])) or None,
        "explanation": own.get("explanation"), "root_cause_llm": enr.get("root_cause"), "secure_pattern_llm": enr.get("secure_pattern"),
        "explanation_llm": enr.get("explanation"), "llm_model": enr.get("model"), "llm_prompt_version": enr.get("prompt_version"),
        "created": fmt_dt(pair.get("created_at")), "updated": fmt_dt(pair.get("updated_at")), "edited_at": fmt_dt(pair.get("edited_at")),
        "changed_lines": ext.get("changed_lines"), "span": unit.get("span"), "old_path": unit.get("renamed_from") or unit.get("file"),
        "parent_count": own.get("parent_count"), "parent_selection_reason": own.get("parent_selection_reason"),
        "quarantine_reason": (flags.get("quarantine") or [None])[0], "before_blob_sha": ext.get("blobs", {}).get("before"),
        "after_blob_sha": ext.get("blobs", {}).get("after"), "extraction_version": ext.get("version"),
        "parse_status": ext.get("parse", {}).get("status"), "parse_failure_reason": ext.get("parse", {}).get("reason"),
        "function_name_before": unit.get("function_before"), "attribution_basis": basis,
        "match_kind": ext.get("match", {}).get("kind"), "match_confidence": ext.get("match", {}).get("confidence"),
        "extraction_meta": json.dumps(ext["meta"], sort_keys=True, separators=(",", ":"), ensure_ascii=False) if ext.get("meta") else None,
        "attribution_score": ATTRIBUTION_SCORES.get(basis) if basis else None,
        "explanation_source": pair.get("explanation", {}).get("source"),
        "needs_explanation_enrichment": (int(bool(pair["explanation"]["needs_enrichment"])) if "needs_enrichment" in pair.get("explanation", {}) else None),
        "original_advisory_reference": enr.get("original_advisory_reference"),
    }
    return row
