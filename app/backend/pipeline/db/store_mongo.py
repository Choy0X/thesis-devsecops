"""Embedded SQLite implementation of ``store.py`` (advisory records), under the retained compatibility module name.

``store.py`` binds these functions under its public names. The answers are the legacy flat dicts (``mappers.advisory_row``),
so routes and pipeline modules do not change. Mutations bump the change counter once per call.
"""
from __future__ import annotations

import re
import uuid

import pandas as pd

from app.backend.pipeline import util
from app.backend.pipeline.db import legacy, mappers as m, mongo

KEEP_SOURCES = ("manual", "import")


def _fields():
    from app.backend.pipeline import store
    return store.FIELDS


def _heavy():
    from app.backend.pipeline import store
    return store.HEAVY_COLUMNS


def change_token() -> str:
    return legacy.token(legacy.handle())


def _row_from_clean(clean: dict, key: str, rid: int, created: str, updated: str) -> dict:
    return {**{c: clean.get(c) for c in _fields()}, "id": rid, "key": key, "created": created, "updated": updated}


def add(rec: dict) -> dict:
    from app.backend.pipeline import store
    db = legacy.handle()
    data = store._clean(rec)
    data["source"] = data.get("source") or "manual"
    key = store._key_for(data)
    now = util.now_z()
    existing = db["advisories"].find_one({"key": key})
    rid = existing["_id"] if existing else legacy.ids(db, "advisories")
    created = m.fmt_dt(existing.get("created_at")) if existing else now
    row = _row_from_clean(data, key, rid, created, now)
    legacy.save_advisory(db, row)
    legacy.touch(db)
    return get(rid)


def get(rec_id: int) -> dict | None:
    db = legacy.handle()
    doc = db["advisories"].find_one({"_id": rec_id})
    return legacy.advisory_rows(db, [doc])[0] if doc else None


def update(rec_id: int, patch: dict) -> dict | None:
    from app.backend.pipeline import codestore, store
    db = legacy.handle()
    allowed = {c: patch[c] for c in _fields() if c in patch}
    if not allowed:
        return get(rec_id)
    before = get(rec_id)
    if before is None:
        return None
    lang_changed = False
    if "language" in allowed:
        lang = ",".join(l.strip().lower() for l in str(allowed["language"] or "").split(",") if l.strip())
        allowed["language"] = lang or None
        lang_changed = (allowed["language"] or None) != (before.get("language") or None)
        if lang_changed:
            allowed["language_source"] = "manual" if lang else None
    cleaned = store._clean({**{c: None for c in _fields()}, **allowed})
    row = {**before, **{c: cleaned[c] for c in allowed}, "updated": util.now_z()}
    legacy.save_advisory(db, row)
    legacy.touch(db)
    if lang_changed:
        codestore.requeue_record_commits(row)
        codestore.refresh_records([rec_id])
    return get(rec_id)


def delete(rec_id: int) -> bool:
    db = legacy.handle()
    n = db["advisories"].delete_one({"_id": rec_id}).deleted_count
    db["fix_links"].delete_many({"advisory_id": rec_id})
    if n:
        legacy.touch(db)
    return n > 0


def prune_before(since: str) -> int:
    db = legacy.handle()
    cutoff = m.parse_dt(str(since)[:10])
    q = {"published_at": {"$lt": cutoff}, "origin": {"$nin": list(KEEP_SOURCES)},
         "$nor": [{"origin": {"$exists": False}, "osv_id": {"$exists": False}}]}
    gone = [d["_id"] for d in db["advisories"].find(q, {"_id": 1})]
    if gone:
        db["advisories"].delete_many({"_id": {"$in": gone}})
        db["fix_links"].delete_many({"advisory_id": {"$in": gone}})
        legacy.touch(db)
    return len(gone)


def clear() -> int:
    db = legacy.handle()
    n = db["advisories"].delete_many({}).deleted_count
    db["fix_links"].delete_many({})
    legacy.touch(db)
    return n


def upsert_many(df: pd.DataFrame, source: str = "osv") -> int:
    """Upsert a DataFrame of records per ecosystem key; an incoming NULL never wipes an existing value (the SQLite ``COALESCE`` rule)."""
    from app.backend.pipeline import store
    if df is None or len(df) == 0:
        return 0
    db = legacy.handle()
    df = df.copy()
    fields = _fields()
    for c in fields:
        if c not in df.columns:
            df[c] = None
    base = [store._key_for(r) for r in df.to_dict("records")]
    ecos = [store._eco(e) for e in df["ecosystem"]]
    claimed: dict[str, str | None] = {}
    uniq = sorted(set(base))
    for i in range(0, len(uniq), 500):
        for d in db["advisories"].find({"key": {"$in": uniq[i:i + 500]}}, {"key": 1, "ecosystem": 1}):
            claimed[d["key"]] = store._eco(d.get("ecosystem"))
    keys = []
    for b, e in zip(base, ecos):
        if b not in claimed:
            claimed[b] = e
            keys.append(b)
        elif e is None or claimed[b] is None or claimed[b] == e:
            if claimed[b] is None:
                claimed[b] = e
            keys.append(b)
        else:
            keys.append(f"{b}@{e}")
    df["__key"] = keys
    pkg_sets: dict[str, set] = {}
    for key, p in zip(df["__key"], df["package"]):
        if isinstance(p, str):
            pkg_sets.setdefault(key, set()).update(x.strip() for x in p.split(",") if x.strip())
    pkgs = {k: ", ".join(sorted(v)) for k, v in pkg_sets.items()}
    df = df.drop_duplicates("__key", keep="first").set_index("__key")
    existing = {d["key"]: d for d in db["advisories"].find({"key": {"$in": list(df.index)}})}
    before_rows = {r["key"]: r for r in legacy.advisory_rows(db, list(existing.values()))}
    new_ids = legacy.ids(db, "advisories", sum(1 for k in df.index if k not in existing)) if len(existing) < len(df) else 0
    n = 0
    now = util.now_z()
    items = []
    for key, r in df.iterrows():
        rec = store._clean(r.to_dict())
        rec["source"] = rec.get("source") or source
        if key in pkgs:
            rec["package"] = pkgs[key]
        old = existing.get(key)
        if old is not None:
            before = before_rows[key]
            for c in fields:
                if rec.get(c) is None:
                    rec[c] = before.get(c)
            rid, created = old["_id"], m.fmt_dt(old.get("created_at"))
        else:
            rid, created = new_ids, now
            new_ids += 1
        row = _row_from_clean(rec, key, rid, created, now)
        pk = [(store._eco(rec.get("ecosystem")) or "", p) for p in sorted(pkg_sets.get(key, ()))]
        doc = m.advisory_doc(row, [(e, p) for e, p in pk])
        items.append((row, doc))
        n += 1
    legacy.save_advisories_bulk(db, items)
    legacy.touch(db)
    return n


def import_rows(rows: list[dict], source: str = "import") -> int:
    if not rows:
        return 0
    return upsert_many(pd.DataFrame(rows), source=source)


def get_meta(key: str, default: str | None = None) -> str | None:
    doc = legacy.handle()["system"].find_one({"_id": f"meta:{key}"})
    return doc["value"] if doc else default


def set_meta(key: str, value: str) -> None:
    db = legacy.handle()
    db["system"].replace_one({"_id": f"meta:{key}"}, {"_id": f"meta:{key}", "value": value}, upsert=True)
    legacy.touch(db)


def meta_keys(prefix: str) -> list[str]:
    docs = legacy.handle()["system"].find({"_id": {"$regex": "^meta:" + re.escape(prefix)}}, {"_id": 1})
    return sorted(d["_id"][len("meta:"):] for d in docs)


def count() -> int:
    return legacy.handle()["advisories"].count_documents({})


def known_keys(keys) -> dict[str, str | None]:
    wanted = [k for k in dict.fromkeys(keys) if isinstance(k, str) and k]
    if not wanted:
        return {}
    out: dict[str, str | None] = {}
    for doc in legacy.handle()["advisories"].find({"key": {"$in": wanted}}, {"key": 1, "ecosystem": 1}):
        eco = doc.get("ecosystem")
        out[doc["key"]] = eco.strip() if isinstance(eco, str) and eco.strip() else None
    return out


def _search_filter(search: str) -> dict:
    rx = {"$regex": re.escape(search), "$options": "i"}
    return {"$or": [{"cve_id": rx}, {"osv_id": rx}, {"packages.name": rx}, {"summary": rx}]}


def _severity_filter(severity: str) -> dict:
    from app.backend.pipeline import store
    if severity == "unrated":
        return {"$or": [{"osv_severity": {"$exists": False}}, {"osv_severity": None}, {"osv_severity": ""}]}
    return {"osv_severity": {"$in": [v for w in store.SEVERITY_WORDS[severity] for v in (w, w.lower(), w.title())]}}


def _filter(search: str, language: str, code_status: str, dependency_status: str, severity: str, ecosystem: str, skip: tuple[str, ...] = ()) -> dict:
    """The document filter of a records list; ``skip`` leaves one dimension out (a facet counts its own dimension under all the other filters)."""
    conds: list[dict] = []
    if search:
        conds.append(_search_filter(search))
    if language and "language" not in skip:
        conds.append({"mining.languages": language.lower()})
    if "code_status" not in skip:
        if code_status == "none":
            conds.append({"mining.code_status": {"$exists": False}})
        elif code_status:
            conds.append({"mining.code_status": code_status})
    if "dependency_status" not in skip:
        if dependency_status == "none":
            conds.append({"deps.status": {"$exists": False}})
        elif dependency_status:
            conds.append({"deps.status": dependency_status})
    if severity and "severity" not in skip:
        conds.append(_severity_filter(severity))
    if ecosystem and "ecosystem" not in skip:
        conds.append({"ecosystem": ecosystem})
    return {"$and": conds} if conds else {}


_SORT_FIELDS = {"id": "_id", "published": "published_at", "cve_id": "cve_id", "ecosystem": "ecosystem"}
# the fields of an advisory document a list row is built from (the long texts, the references and the embedded ranges stay in the database)
_LIST_PROJECTION = {"key": 1, "osv_id": 1, "cve_id": 1, "ecosystem": 1, "summary": 1, "published_at": 1, "osv_severity": 1, "cvss.manual_score": 1, "packages.name": 1,
                    "mining.languages": 1, "mining.code_status": 1, "mining.fix_commit_count": 1, "mining.pair_count": 1, "deps.status": 1}


def query(search: str = "", limit: int = 50, offset: int = 0, language: str = "", code_status: str = "", dependency_status: str = "", slim: bool = False,
          severity: str = "", ecosystem: str = "", sort: str = "id", direction: str = "desc", view: str = "") -> dict:
    from app.backend.pipeline import store
    store.check_choices(sort, direction, severity)
    db = legacy.handle()
    flt = _filter(search, language, code_status, dependency_status, severity, ecosystem)
    total, exact = db['advisories'].count_documents(flt), True
    field = _SORT_FIELDS[sort]
    order = [(field, 1 if direction == "asc" else -1)] + ([("_id", -1)] if field != "_id" else [])
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    if view == "list":                               # only the fields the table shows, read straight from the documents
        docs = list(db["advisories"].find(flt, _LIST_PROJECTION).sort(order).skip(offset).limit(limit))
        rows = []
        for d in docs:
            row = {k: v for k, v in m.advisory_row(d, None).items() if k in store.LIST_COLUMNS}
            row["summary"] = (row.get("summary") or "")[:store.LIST_SUMMARY] if row.get("summary") is not None else None
            rows.append(row)
        return {"total": total, "total_exact": exact, "limit": limit, "offset": offset, "records": rows}
    if slim:                                         # a list row without the long texts, and without the link lookups they need
        docs = list(db["advisories"].find(flt, {"details": 0, "refs": 0, "aliases": 0}).sort(order).skip(offset).limit(limit))
        heavy = _heavy()
        rows = [{k: v for k, v in m.advisory_row(d, None).items() if k not in heavy} for d in docs]
        return {"total": total, "total_exact": exact, "limit": limit, "offset": offset, "records": rows}
    docs = list(db["advisories"].find(flt).sort(order).skip(offset).limit(limit))
    return {"total": total, "total_exact": exact, "limit": limit, "offset": offset, "records": legacy.advisory_rows(db, docs)}


def facets(search: str = "", language: str = "", code_status: str = "", dependency_status: str = "", severity: str = "", ecosystem: str = "") -> dict:
    from app.backend.pipeline import store
    store.check_choices(severity=severity)
    db = legacy.handle()
    every = (search, language, code_status, dependency_status, severity, ecosystem)

    def groups(skip: str, key, before: list | None = None) -> list[dict]:
        return list(db["advisories"].aggregate([{"$match": _filter(*every, skip=(skip,))}, *(before or []), {"$group": {"_id": key, "n": {"$sum": 1}}}]))
    bands = dict.fromkeys(store.BANDS, 0)
    for g in groups("severity", {"$toUpper": {"$ifNull": ["$osv_severity", ""]}}):
        bands[store.BAND_OF_WORD.get(g["_id"], "unrated")] += g["n"]
    langs = {g["_id"]: g["n"] for g in groups("language", "$mining.languages", [{"$unwind": "$mining.languages"}]) if g["_id"]}
    return {"total": db["advisories"].count_documents(_filter(*every)), "severity": bands,
            "ecosystem": {(g["_id"] or "unknown"): g["n"] for g in groups("ecosystem", "$ecosystem")},
            "language": dict(sorted(langs.items(), key=lambda kv: -kv[1])),
            "code_status": {g["_id"]: g["n"] for g in groups("code_status", {"$ifNull": ["$mining.code_status", "none"]})},
            "dependency_status": {g["_id"]: g["n"] for g in groups("dependency_status", {"$ifNull": ["$deps.status", "none"]})}}


def packages_of(key: str) -> list[tuple[str, str]]:
    doc = legacy.handle()["advisories"].find_one({"key": key}, {"packages": 1})
    pairs = {(p.get("ecosystem") or "", p["name"]) for p in (doc or {}).get("packages", []) if p.get("name")}
    return sorted(pairs)


def ecosystem_funnel() -> dict:
    db = legacy.handle()
    out: dict[str, dict] = {}

    def row(eco):
        return out.setdefault(eco, {"advisories": 0, "malware": 0, "withdrawn": 0, "informational": 0,
                                    "with_fix_pointer": 0, "mined": 0, "pairs": 0, "usable_pairs": 0})
    for g in db["advisories"].aggregate([{"$group": {
            "_id": {"eco": {"$ifNull": ["$ecosystem", "unknown"]}, "kind": "$kind"}, "n": {"$sum": 1},
            "ptr": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$mining.fix_commit_count", 0]}, 0]}, 1, 0]}},
            "mined": {"$sum": {"$cond": [{"$eq": ["$mining.code_status", "mined"]}, 1, 0]}}}}]):
        r = row(g["_id"]["eco"])
        kind = g["_id"]["kind"]
        r["advisories" if kind == "vulnerability" else kind] += g["n"]
        if kind == "vulnerability":
            r["with_fix_pointer"] += g["ptr"]
            r["mined"] += g["mined"]
    for g in db["code_pairs"].aggregate([{"$group": {
            "_id": {"$ifNull": ["$subject.ecosystem", "unknown"]}, "n": {"$sum": 1},
            "usable": {"$sum": {"$cond": [{"$and": [{"$eq": ["$flags.available", True]}, {"$ne": ["$flags.is_test", True]},
                                                    {"$ne": ["$flags.tangled", True]}]}, 1, 0]}}}}]):
        r = row(g["_id"])
        r["pairs"], r["usable_pairs"] = g["n"], g["usable"]
    return dict(sorted(out.items()))


def summary(funnel_max_age: float = 0.0) -> dict:
    from app.backend.pipeline import store
    db = legacy.handle()
    coll = db["advisories"]
    total = coll.count_documents({})

    def counts(path: str, missing: str) -> dict:
        return {(g["_id"] if g["_id"] is not None else missing): g["n"]
                for g in coll.aggregate([{"$group": {"_id": f"${path}", "n": {"$sum": 1}}}])}
    langs: dict[str, int] = {}
    for g in coll.aggregate([{"$unwind": "$mining.languages"}, {"$group": {"_id": "$mining.languages", "n": {"$sum": 1}}}]):
        langs[g["_id"]] = g["n"]
    return {"total": total, "by_source": counts("origin", "?"),
            "with_cve": coll.count_documents({"cve_id": {"$exists": True}}),
            "with_code": coll.count_documents({"mining.pair_count": {"$gt": 0}}),
            "with_vector": coll.count_documents({"$or": [{"cvss.v3": {"$exists": True}}, {"cvss.v4": {"$exists": True}}]}),
            "by_code_status": counts("mining.code_status", "none"), "by_dependency_status": counts("deps.status", "none"),
            "by_language": dict(sorted(langs.items(), key=lambda kv: -kv[1])), "by_ecosystem": store.funnel_cached(funnel_max_age)}


def to_dataframe(ecosystems: list[str] | None = None) -> pd.DataFrame:
    db = legacy.handle()
    docs = list(db["advisories"].find({} if ecosystems is None else {"ecosystem": {"$in": list(ecosystems)}}).sort("_id", 1))
    return pd.DataFrame(legacy.advisory_rows(db, docs))


def metadata_batches(ecosystems=None, *, batch_size=1000):
    """Bounded legacy projections, in the same source order as to_dataframe."""
    db = legacy.handle()
    cursor = db['advisories'].find({} if ecosystems is None else {'ecosystem': {'$in': list(ecosystems)}}).sort('_id', 1).batch_size(batch_size)
    rows = []
    try:
        for document in cursor:
            rows.append(document)
            if len(rows) >= batch_size:
                yield legacy.advisory_rows(db, rows)
                rows = []
        if rows:
            yield legacy.advisory_rows(db, rows)
    finally:
        cursor.close()


# ── pipeline selections ─────────────────────────────────────────────────────────────────────────────────────────────

def _mineable_filter(*, only_mined: bool = False, deps_stale_before: str | None = None) -> dict:
    conds: list[dict] = [{"kind": "vulnerability"}]
    if only_mined:
        conds.append({"mining.code_status": "mined"})
    if deps_stale_before:
        conds.append({"$or": [{"deps.checked_at": {"$exists": False}}, {"deps.checked_at": {"$lt": m.parse_dt(deps_stale_before)}}]})
    return {"$and": conds}


def select_mineable(columns, *, no_fix: bool = False, ecosystems=None, only_mined: bool = False, deps_stale_before: str | None = None,
                    order: str = "id", limit: int | None = None) -> list[dict]:
    if ecosystems is not None and not ecosystems:
        return []
    db = legacy.handle()
    flt = _mineable_filter(only_mined=only_mined, deps_stale_before=deps_stale_before)
    if ecosystems:
        flt["$and"].append({"ecosystem": {"$in": list(ecosystems)}})
    if no_fix:
        linked = db["fix_links"].distinct("advisory_id")
        flt["$and"].append({"_id": {"$nin": linked}})
    cur = db["advisories"].find(flt).sort([("ecosystem", 1), ("_id", 1)] if order == "ecosystem" else [("_id", 1)])
    if limit:
        cur = cur.limit(int(limit))
    return [{c: r.get(c) for c in columns} for r in legacy.advisory_rows(db, list(cur))]


def count_mineable_records(*, only_mined: bool = False, deps_stale_before: str | None = None, ecosystems: list[str] | None = None) -> int:
    if ecosystems is not None and not ecosystems:
        return 0
    flt = _mineable_filter(only_mined=only_mined, deps_stale_before=deps_stale_before)
    if ecosystems:
        flt["$and"].append({"ecosystem": {"$in": list(ecosystems)}})
    return legacy.handle()["advisories"].count_documents(flt)


def records_by_ids(ids, columns) -> list[dict]:
    if not ids:
        return []
    db = legacy.handle()
    docs = list(db["advisories"].find({"_id": {"$in": list(ids)}}).sort("_id", 1))
    return [{c: r.get(c) for c in columns} for r in legacy.advisory_rows(db, docs)]


def set_fix_commits(updates) -> None:
    db = legacy.handle()
    for rid, text in updates:
        doc = db["advisories"].find_one({"_id": rid})
        if doc is None:
            continue
        row = legacy.advisory_rows(db, [doc])[0]
        row["fix_commits"] = text
        legacy.save_advisory(db, row, doc=m.advisory_doc(row))
    if updates:
        legacy.touch(db)


def update_records(rows) -> None:
    db = legacy.handle()
    for patch in rows:
        doc = db["advisories"].find_one({"_id": patch["id"]})
        if doc is None:
            continue
        row = {**legacy.advisory_rows(db, [doc])[0], **{k: v for k, v in patch.items() if k != "id"}}
        legacy.save_advisory(db, row, doc=m.advisory_doc(row))
    if rows:
        legacy.touch(db)


# ── registry cache ──────────────────────────────────────────────────────────────────────────────────────────────────

def _reg_key(ecosystem: str, package: str) -> dict:
    base, _, variant = ecosystem.partition(":")
    return {"ecosystem": base, "name": package}


def registry_rows() -> list[dict]:
    return [m.registry_row(d) for d in legacy.handle()["registry_packages"].find()]


def registry_row(ecosystem: str, package: str) -> dict | None:
    doc = legacy.handle()["registry_packages"].find_one(_reg_key(ecosystem, package))
    if doc is None:
        return None
    row = m.registry_row(doc)
    return {k: row[k] for k in ("versions", "dates", "ok", "fetched_at", "negative_status") if k in row}


def registry_save(ecosystem: str, package: str, versions: str, ok: int, fetched_at: str, dates: str | None, *, negative_status: int | None = None) -> None:
    if negative_status is not None and (ok or type(negative_status) is not int or negative_status not in (404, 410)):
        raise ValueError('Negative registry proof requires HTTP 404 or 410 and ok=false')
    db = legacy.handle()
    doc = m.registry_doc({"ecosystem": ecosystem, "package": package, "versions": versions, "ok": ok, "fetched_at": fetched_at, "dates": dates,
                         **({"negative_status": negative_status} if negative_status is not None else {})})
    db["registry_packages"].replace_one(_reg_key(ecosystem, package), doc, upsert=True)


def registry_set_dates(ecosystem: str, package: str, dates: str) -> None:
    row = legacy.handle()["registry_packages"].find_one(_reg_key(ecosystem, package))
    if row is None:
        return
    full = m.registry_row(row)
    full["dates"] = dates
    registry_save(ecosystem, package, full["versions"], full["ok"], full["fetched_at"], dates)


# ── record reads of the dataset builders, the leakage graph and the reviewed build ─────────────────────────────────────

_HAS_AFFECTED = {"$or": [{"packages.ranges": {"$exists": True}}, {"packages.versions": {"$exists": True}}, {"packages.versions_total": {"$exists": True}},
                         {"deps.packages.0": {"$exists": True}}]}


def _rows(flt: dict, columns, *, sort: str = "_id") -> list[dict]:
    db = legacy.handle()
    docs = list(db["advisories"].find(flt).sort(sort, 1))
    return [{c: r.get(c) for c in columns} for r in legacy.advisory_rows(db, docs)]


def severity_counts() -> dict:
    out: dict[str, int] = {}
    for g in legacy.handle()["advisories"].aggregate([{"$match": {"kind": "vulnerability"}}, {"$group": {"_id": "$osv_severity", "n": {"$sum": 1}}}]):
        key = (g["_id"] or "").strip().lower() or "none"
        out[key] = out.get(key, 0) + g["n"]
    return out


def count_by_code_status(status: str) -> int:
    return legacy.handle()["advisories"].count_documents({"mining.code_status": status})


def mined_records(statuses, columns) -> list[dict]:
    return _rows({"kind": "vulnerability", "mining.code_status": {"$in": list(statuses)}, "withdrawn_at": {"$exists": False}}, columns)


def dep_checked_records(columns) -> list[dict]:
    return _rows({"$and": [{"kind": "vulnerability", "withdrawn_at": {"$exists": False}, "deps.checked_at": {"$exists": True}}, _HAS_AFFECTED]}, columns)


def count_dep_unchecked() -> int:
    return legacy.handle()["advisories"].count_documents(
        {"$and": [{"kind": "vulnerability", "withdrawn_at": {"$exists": False}, "deps.checked_at": {"$exists": False}}, _HAS_AFFECTED]})


def vulnerability_records(columns) -> list[dict]:
    return _rows({"kind": "vulnerability"}, columns, sort="key")


def records_with_affected(columns) -> list[dict]:
    return _rows(_HAS_AFFECTED, columns)


def reset_deps_checked(only_mined: bool = False) -> None:
    db = legacy.handle()
    db["advisories"].update_many({"mining.code_status": "mined"} if only_mined else {}, {"$unset": {"deps.checked_at": ""}})
    legacy.touch(db)
