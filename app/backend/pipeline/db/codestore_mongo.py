"""Embedded SQLite implementation of ``codestore.py`` (fix commits, files, repositories, code pairs, mining write-back), under the retained compatibility module name.

``codestore.py`` binds these functions under its public names. Arguments and answers are the legacy flat dicts, so callers do not change;
the documents are the redesigned ones (``schema.py``): commits with ``queue/git/verification/evidence`` sub-documents, links in ``fix_links``,
light pairs in ``code_pairs`` with the code in ``pair_code`` and LLM drafts in ``pair_enrichments``. Each function that writes bumps the
change counter once.
"""
from __future__ import annotations

import json
import re

from app.backend.pipeline.db.operations import BulkWriteError

from app.backend.pipeline import campaign_ctx, util
from app.backend.pipeline.db import legacy, mappers as m, mongo

HEAVY = {"code_before", "code_after", "diff", "explanation", "commit_message", "root_cause_llm", "secure_pattern_llm", "explanation_llm",
         "original_advisory_reference", "extraction_meta"}


def _cs():
    from app.backend.pipeline import codestore
    return codestore


def _utc():
    return m.parse_dt(util.now_z())


def _rx(text: str) -> dict:
    return {"$regex": re.escape(text), "$options": "i"}


# =================================================================================================================================
# fix commits

def queue_fix_commits(items: list[dict]) -> tuple[int, int]:
    db = legacy.handle()
    grouped: dict[str, dict] = {}
    for it in items:
        g = grouped.setdefault(it["commit_key"], {**it, "advisory_ids": [], "cve_ids": [], "sources": [], "vias": [], "ecosystems": []})
        g["advisory_ids"].append(it.get("advisory_id"))
        g["cve_ids"].append(it.get("cve_id"))
        g["sources"].append(it.get("source"))
        g["vias"].append(it.get("via"))
        g["ecosystems"].append(it.get("ecosystem"))
    keys = list(grouped)
    existing = {d["commit_key"]: d.get("queue", {}).get("ecosystems", []) for d in
                db["fix_commits"].find({"commit_key": {"$in": keys}}, {"commit_key": 1, "queue.ecosystems": 1})} if keys else {}
    new_keys = [k for k in keys if k not in existing]
    now = util.now_z()
    campaign = campaign_ctx.current()
    if new_keys:
        first = legacy.ids(db, "fix_commits", len(new_keys))
        docs = []
        for i, k in enumerate(new_keys):
            g = grouped[k]
            tags = sorted({e for e in g["ecosystems"] if e and isinstance(e, str)})
            row = {"id": first + i, "commit_key": k, "host": g["host"], "owner": g["owner"], "repo": g["repo"], "sha": g["sha"],
                   "repo_url": g["repo_url"], "status": "pending", "attempts": 0, "created": now, "updated": now,
                   "ecosystems": json.dumps(tags) if tags else None, "campaign_id": campaign}
            docs.append(m.commit_doc(row))
        try:
            db["fix_commits"].insert_many(docs, ordered=False)
        except BulkWriteError as e:
            # Two callers queueing the same new commit at once (several worker processes do, in a real campaign) can both see it as missing and
            # both try to insert it; the loser hits commit_key's own unique index. ordered=False already let every other document in the batch
            # through; a duplicate key here means someone else queued that one first, which is the outcome this call wanted anyway, not a failure.
            if any(err.get("code") != 11000 for err in e.details.get("writeErrors", [])):
                raise
    # a commit that was queued before (by hand, or before the tagging existed) gets the ecosystems of the records that point to it now
    from app.backend.pipeline.db.operations import UpdateOne
    tag_ops = []
    for k, have in existing.items():
        add = [e for e in dict.fromkeys(grouped[k]["ecosystems"]) if e and isinstance(e, str) and e not in have]
        if add:
            tag_ops.append(UpdateOne({"commit_key": k}, {"$addToSet": {"queue.ecosystems": {"$each": add}}}))
    if tag_ops:
        db["fix_commits"].bulk_write(tag_ops, ordered=False)
    # links: the advisory is found by its OSV id, else by its CVE id
    osv = {a for g in grouped.values() for a in g["advisory_ids"] if a}
    cve = {c for g in grouped.values() for c in g["cve_ids"] if c}
    by_osv, by_cve = {}, {}
    if osv or cve:
        for a in db["advisories"].find({"$or": [{"osv_id": {"$in": list(osv)}}, {"cve_id": {"$in": list(cve)}}, {"key": {"$in": list(osv)}}]},
                                       {"key": 1, "osv_id": 1, "cve_id": 1}).sort("_id", 1):
            by_osv.setdefault(a.get("osv_id") or a["key"], a)
            by_osv.setdefault(a["key"], a)
            if a.get("cve_id"):
                by_cve.setdefault(a["cve_id"], a)
    changed = 0
    for key, g in grouped.items():
        targets = {}
        for adv_id, cve_id in zip(g["advisory_ids"] or [None], g["cve_ids"] or [None]):
            a = by_osv.get(adv_id) or by_cve.get(cve_id)
            if a:
                targets[a["_id"]] = a
        for aid, a in targets.items():
            via = sorted({v for v in g["vias"] if v})
            src = sorted({s for s in g["sources"] if s})
            # One atomic upsert, not a find-then-write: two callers linking the same (advisory, commit) at once (recover_fixes and queue run several
            # worker processes) used to both see "not there yet" and both insert, and the second hit fix_links' own unique index as a DuplicateKeyError
            # that crashed the whole stage. $addToSet dedupes via/sources itself, so the merge this used to do by hand is no longer needed either.
            res = db["fix_links"].update_one({"advisory_id": aid, "commit_key": key}, {"$setOnInsert": {"advisory_key": a["key"]},
                                              "$addToSet": {"via": {"$each": via}, "sources": {"$each": src}}}, upsert=True)
            if res.upserted_id is not None or res.modified_count:
                changed += 1
    if new_keys or changed or tag_ops:
        legacy.touch(db)
    return len(new_keys), db["fix_commits"].count_documents({})


def count_fix_commits() -> int:
    return legacy.handle()["fix_commits"].count_documents({})


def _scoped(flt: dict, ecosystems) -> dict:
    """``flt`` limited to the commits tagged with one of ``ecosystems`` (None: no limit; an untagged commit is in no scope, an empty scope matches nothing)."""
    if ecosystems is not None:
        flt["queue.ecosystems"] = {"$in": list(ecosystems)}
    return flt


def pending_fix_commits(limit: int | None = None, ecosystems: list[str] | None = None, *, exclude=None) -> list[dict]:
    if exclude is not None and (not isinstance(exclude,(list,tuple)) or len(exclude)>256
                               or any(not isinstance(key,str) for key in exclude)):
        raise ValueError('Refill exclusion is limited to 256 active commit keys')
    db = legacy.handle()
    selector=_scoped({"queue.status":"pending"},ecosystems)
    if exclude:selector={'$and':[selector,{'commit_key':{'$nin':list(exclude)}}]}
    cur = db["fix_commits"].find(selector).sort("_id", 1)
    if limit:
        cur = cur.limit(int(limit))
    return legacy.commit_rows(db, list(cur))


_IDENTITY_PROJECTION = {'commit_key': 1, 'repo.host': 1, 'repo.owner': 1, 'repo.name': 1, 'sha': 1}


def _commit_identity(doc):
    # Copy source fields exactly. Hints must not reconstruct or casefold keys.
    repo = doc.get('repo')
    repo = repo if isinstance(repo, dict) else {}
    return {'commit_key': doc.get('commit_key'), 'host': repo.get('host'),
            'owner': repo.get('owner'), 'repo': repo.get('name'), 'sha': doc.get('sha')}


def pending_fix_commit_identities(limit=None, ecosystems=None):
    """Stream the same pending FIFO prefix without advisory or source bodies."""
    cur = legacy.handle()['fix_commits'].find(
        _scoped({'queue.status': 'pending'}, ecosystems), _IDENTITY_PROJECTION).sort('_id', 1).batch_size(128)
    if limit:
        cur = cur.limit(int(limit))
    source = iter(cur)
    try:
        for doc in source:
            yield _commit_identity(doc)
    finally:
        close = getattr(source, 'close', None)
        if close is not None:
            close()
        cur.close()


def revalidate_pending_fix_commit_identities(candidates, ecosystems=None):
    """Authority remains the original typed selector, narrowed to at most 8 keys."""
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 8:
        raise ValueError('Hint authority validation is limited to eight identities')
    if not candidates:
        return []
    selector = {'$and': [_scoped({'queue.status': 'pending'}, ecosystems),
                         {'commit_key': {'$in': [row['commit_key'] for row in candidates]}}]}
    cur = legacy.handle()['fix_commits'].find(selector, _IDENTITY_PROJECTION).sort('_id', 1)
    return [_commit_identity(doc) for doc in cur]

def count_mineable(ecosystems: list[str] | None = None) -> int:
    return legacy.handle()["fix_commits"].count_documents(_scoped({"queue.status": "pending"}, ecosystems))


def sync_links(linked: set[str], ecosystems: list[str] | None = None) -> tuple[int, int]:
    db = legacy.handle()
    keys = list(linked)
    if ecosystems is not None:      # a campaign's scope: its own commits only, and only those tagged with nothing else
        scope = list(ecosystems)
        inside = {"$and": [{"queue.ecosystems": {"$in": scope}}, {"queue.ecosystems": {"$not": {"$elemMatch": {"$nin": scope}}}}]}
        off = db["fix_commits"].update_many({"queue.status": "pending", "commit_key": {"$nin": keys}, **inside}, {"$set": {"queue.status": "unlinked"}}).modified_count
        on = db["fix_commits"].update_many({"queue.status": "unlinked", "commit_key": {"$in": keys}, "queue.ecosystems": {"$in": scope}},
                                           {"$set": {"queue.status": "pending"}}).modified_count
        if off or on:
            legacy.touch(db)
        return off, on
    off = db["fix_commits"].update_many({"queue.status": "pending", "commit_key": {"$nin": keys}}, {"$set": {"queue.status": "unlinked"}}).modified_count
    on = db["fix_commits"].update_many({"queue.status": "unlinked", "commit_key": {"$in": keys}}, {"$set": {"queue.status": "pending"}}).modified_count
    if off or on:
        legacy.touch(db)
    return off, on


def fix_commits_for_record(rec: dict) -> list[dict]:
    db = legacy.handle()
    ptrs = [p for p in json.loads(rec.get("fix_commits") or "[]") if isinstance(p, dict)]
    keys = [p["commit_key"] for p in ptrs if p.get("commit_key")]
    rows = {}
    for r in legacy.commit_rows(db, list(db["fix_commits"].find({"commit_key": {"$in": keys}}))):
        rows[r["commit_key"]] = {k: r.get(k) for k in ("commit_key", "status", "error", "detected_langs", "pairs", "commit_date", "fix_check",
                                                       "fix_check_reason", "link")}
    return [{**p, **rows.get(p.get("commit_key"), {"status": "not queued"})} for p in ptrs]


def requeue_record_commits(rec: dict) -> int:
    keys = _cs()._record_commit_keys(rec)
    if not keys:
        return 0
    db = legacy.handle()
    langs = [l for l in (rec.get("language") or "").split(",") if l] if rec.get("language_source") == "manual" else []
    op = {"$set": {"queue.status": "pending"}, "$unset": {"queue.reason": ""}}
    if langs:
        op["$set"]["queue.expected_langs"] = langs
    else:
        op["$unset"]["queue.expected_langs"] = ""
    n = db["fix_commits"].update_many({"commit_key": {"$in": keys}, "queue.status": {"$ne": "unlinked"}}, op).modified_count
    if n:
        legacy.touch(db)
    return n


_MARK_PATHS = {"retryable": "queue.retryable", "full_sha": "git.full_sha", "error": "queue.reason", "parent_sha": "git.parent_sha", "commit_message": "git.message", "commit_date": "git.committed_at",
               "files_changed": "git.files_changed", "pairs": "git.pairs", "mined_at": "mined_at", "langs": "git.langs",
               "detected_langs": "git.detected_langs", "fix_check": "verification.fix_check", "fix_check_reason": "verification.reason",
               "link": "verification.link", "fix_check_package": "verification.package", "parent_count": "git.parents.count",
               "commit_kind": "git.kind", "parent_selection_reason": "git.parents.selection", "acquisition": "git.acquisition"}
_DATE_FIELDS = {"commit_date", "mined_at"}
_JSON_FIELDS = {"langs", "detected_langs"}


def _sync_pair_snapshot(db, commit_key: str) -> None:
    """Copy the commit's verification, evidence level and kind onto its pairs (the review gate reads them there)."""
    c = db["fix_commits"].find_one({"commit_key": commit_key}, {"verification": 1, "evidence": 1, "git.kind": 1})
    if not c:
        return
    sets, unsets = {}, {}
    for path, value in (("link.fix_check", c.get("verification", {}).get("fix_check")), ("link.evidence_level", c.get("evidence", {}).get("level")),
                        ("link.commit_kind", c.get("git", {}).get("kind"))):
        (sets if value is not None else unsets)[path] = value if value is not None else ""
    op = {}
    if sets:
        op["$set"] = sets
    if unsets:
        op["$unset"] = unsets
    if op:
        db["code_pairs"].update_many({"commit_key": commit_key}, op)


def mark_fix_commit(commit_key: str, status: str, **fields) -> None:
    db = legacy.handle()
    sets: dict = {"queue.status": status, "updated_at": _utc()}
    if campaign_ctx.current():                      # the campaign that works on the commit is the one it is stamped with
        sets["queue.campaign_id"] = campaign_ctx.current()
    unsets: dict = {}
    for k, v in fields.items():
        path = _MARK_PATHS.get(k)
        if path is None:
            continue
        if k in _DATE_FIELDS:
            v = m.parse_dt(v)
        elif k in _JSON_FIELDS and isinstance(v, str):
            v = m.loads(v, None)
        if v is None or v == "" or v == [] or v == {}:
            unsets[path] = ""
        else:
            sets[path] = v
    op = {"$set": sets, "$inc": {"queue.attempts": 1}}
    if unsets:
        op["$unset"] = unsets
    db["fix_commits"].update_one({"commit_key": commit_key}, op)
    if {"fix_check", "commit_kind"} & set(fields):
        _sync_pair_snapshot(db, commit_key)
    legacy.touch(db)


def replace_commit_files(commit_key: str, files: list[dict]) -> None:
    db = legacy.handle()
    docs = [m.file_doc({"commit_key": commit_key, **f}) for f in files]
    seen, unique = set(), []
    for d in docs:                                         # INSERT OR REPLACE: the last row of a path wins
        seen.add(d["path"])
    last = {d["path"]: d for d in docs}
    unique = list(last.values())

    def body(session):
        db["commit_files"].delete_many({"commit_key": commit_key}, session=session)
        if unique:
            db["commit_files"].insert_many(unique, session=session)
    from app.backend.pipeline.acceleration.publication import current_session
    if inherited := current_session():
        body(inherited)
    else:
        with mongo.client().start_session() as s:
            s.with_transaction(body)
    legacy.touch(db)


def commit_files(commit_key: str) -> list[dict]:
    return [m.file_row(d) for d in legacy.handle()["commit_files"].find({"commit_key": commit_key}).sort("path", 1)]


def repository(identity: str) -> dict | None:
    d = legacy.handle()["repositories"].find_one({"_id": identity})
    return m.repository_row(d) if d else None


def save_repository(identity: str, host: str, owner: str, repo: str, spdx: str | None, status: str) -> None:
    db = legacy.handle()
    doc = m.repository_doc({"repo_identity": identity, "host": host, "owner": owner, "repo": repo, "license_spdx": spdx,
                            "data_usage_status": status, "checked_at": util.now_z()})
    db["repositories"].replace_one({"_id": identity}, doc, upsert=True)
    legacy.touch(db)


def set_fix_check(commit_key: str, verdict: str, reason: str | None, link: str | None, package: str | None = None) -> None:
    db = legacy.handle()
    ver = {k: v for k, v in (("fix_check", verdict), ("reason", reason), ("link", link), ("package", package)) if v not in (None, "")}
    db["fix_commits"].update_one({"commit_key": commit_key}, {"$set": {"verification": ver}})
    _sync_pair_snapshot(db, commit_key)
    legacy.touch(db)


def fix_status_counts(ecosystems: list[str] | None = None) -> dict:
    db = legacy.handle()
    return {g["_id"]: g["n"] for g in db["fix_commits"].aggregate([
        {"$match": _scoped({"queue.status": {"$in": ["pending", "failed"]}}, ecosystems)}, {"$group": {"_id": "$queue.status", "n": {"$sum": 1}}}])}


def fix_commit_summary() -> dict:
    db = legacy.handle()
    coll = db["fix_commits"]
    total = coll.count_documents({})
    by_status = {g["_id"]: g["n"] for g in coll.aggregate([{"$group": {"_id": "$queue.status", "n": {"$sum": 1}}}])}
    by_fix_check = {(g["_id"] or "unchecked"): g["n"] for g in coll.aggregate([
        {"$match": {"queue.status": "mined"}}, {"$group": {"_id": "$verification.fix_check", "n": {"$sum": 1}}}])}
    by_host = {g["_id"]: g["n"] for g in coll.aggregate([{"$group": {"_id": "$repo.host", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 8}])}
    live = {d["commit_key"] for d in coll.find({"queue.status": {"$ne": "unlinked"}}, {"commit_key": 1})}
    adv_ids, sources_of = set(), {}
    for link in db["fix_links"].find({"commit_key": {"$in": list(live)}}):
        adv_ids.add(link["advisory_id"])
        sources_of.setdefault(link["commit_key"], set()).update(link.get("sources", []))
    cves = {a["cve_id"] for a in db["advisories"].find({"_id": {"$in": list(adv_ids)}}, {"cve_id": 1}) if a.get("cve_id")}
    by_source: dict[str, int] = {}
    for srcs in sources_of.values():
        for s in srcs:
            by_source[s] = by_source.get(s, 0) + 1
    return {"total": total, "by_status": by_status, "by_fix_check": by_fix_check, "by_host": by_host, "by_source": by_source,
            "advisories_with_fix": len(adv_ids), "cves_with_fix": len(cves)}


def list_fix_commits(status: str = "", search: str = "", limit: int = 50, offset: int = 0) -> dict:
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    db = legacy.handle()
    conds: list[dict] = []
    if status:
        conds.append({"queue.status": status})
    if search:
        rx = _rx(search)
        adv = [a["_id"] for a in db["advisories"].find({"$or": [{"cve_id": rx}, {"osv_id": rx}]}, {"_id": 1})]
        linked = [link["commit_key"] for link in db["fix_links"].find({"advisory_id": {"$in": adv}}, {"commit_key": 1})] if adv else []
        conds.append({"$or": [{"repo.url": rx}, {"sha": rx}, {"commit_key": {"$in": linked}}]})
    flt = {"$and": conds} if conds else {}
    total = db["fix_commits"].count_documents(flt)
    docs = list(db["fix_commits"].find(flt).sort("_id", -1).skip(int(offset)).limit(int(limit)))
    return {"total": total, "records": legacy.commit_rows(db, docs), 'limit': limit, 'offset': offset}


def reset_failed_fix_commits(ecosystems: list[str] | None = None, *, retryable_only=False) -> int:
    db = legacy.handle()
    n = db["fix_commits"].update_many(_scoped({"queue.status": "failed", 'queue.resolution.action': {'$ne': 'skip'}, **({"queue.retryable":{"$in":[True,None]}} if retryable_only else {})}, ecosystems), {"$set": {"queue.status": "pending"}, "$unset": {"queue.reason": ""}}).modified_count
    if n:
        legacy.touch(db)
    return n


# =================================================================================================================================
# code pairs

def _pair_filter(language: str = "", granularity: str = "", search: str = "", available: str = "", source: str = "") -> dict:
    conds: list[dict] = []
    if language:
        conds.append({"unit.language": language})
    if granularity:
        conds.append({"unit.granularity": granularity})
    if source:
        conds.append({"origin.sources": source.split(",")})
    if available in ("0", "1"):
        conds.append({"flags.available": available == "1"})
    elif available == "usable":
        conds.append({"flags.available": True, "flags.is_test": {"$ne": True}, "flags.tangled": {"$ne": True}})
    if search:
        rx = _rx(search)
        conds.append({"$or": [{"subject.cve_id": rx}, {"unit.file": rx}, {"unit.function": rx}, {"repo_url": rx}, {"subject.cwe": rx},
                              {"subject.package": rx}]})
    return {"$and": conds} if conds else {}


def _owners_for_rows(db, rows: list[dict]):
    keys = sorted({r["commit_key"] for r in rows if r.get("commit_key")})
    commits, advisories = legacy.owners_of(db, keys)
    return commits, advisories


def _advisories_for_manual(db, row: dict) -> list[dict]:
    ids = [i for i in (row.get("ghsa_id"),) if i]
    ids += [a for a in m.loads(row.get("advisory_ids"), []) or [] if a]
    cves = [c for c in ([row.get("cve_id")] + (m.loads(row.get("cve_ids"), []) or [])) if c]
    if not ids and not cves:
        return []
    return list(db["advisories"].find({"$or": [{"osv_id": {"$in": ids}}, {"key": {"$in": ids}}, {"cve_id": {"$in": cves}}]}).sort("_id", 1))


def _save_pairs(db, rows: list[dict], *, respect_edited: bool) -> int:
    """Write legacy pair dicts as documents. New pairs get ids from the counter; existing ones keep theirs."""
    from app.backend.pipeline.db.operations import ReplaceOne
    cs = _cs()
    if not rows:
        return 0
    keys = [r["pair_key"] for r in rows]
    existing = {d["pair_key"]: d for d in db["code_pairs"].find({"pair_key": {"$in": keys}},
        {"pair_key": 1, "created_at": 1, "edited_at": 1})}
    old_enr = {e["_id"]: e for e in db["pair_enrichments"].find({"_id": {"$in": [d["_id"] for d in existing.values()]}},
        {"root_cause": 1, "secure_pattern": 1, "explanation": 1, "model": 1, "prompt_version": 1})}
    commits, advisories = _owners_for_rows(db, rows)
    n_new = sum(1 for k in keys if k not in existing)
    nid = legacy.ids(db, "code_pairs", n_new) if n_new else 0
    now = util.now_z()
    pairs, codes, enrs = [], [], []
    for r in rows:
        old = existing.get(r["pair_key"])
        if old is not None and respect_edited and old.get("edited_at") is not None:
            continue                                   # a pair corrected by hand is never overwritten by re-mining
        row = m.clean_nan(r)
        if row.get("changed_lines") is None:
            row["changed_lines"] = cs.changed_lines(row.get("diff"))
        row.update(cs.explanation_fields(row.get("explanation"), row.get("explanation_llm"), row.get("llm_model")))
        if old is not None:
            row["id"], row["created"] = old["_id"], m.fmt_dt(old.get("created_at"))
            prev = old_enr.get(old["_id"], {})
            for src, dst in (("root_cause_llm", "root_cause"), ("secure_pattern_llm", "secure_pattern"), ("explanation_llm", "explanation"),
                             ("llm_model", "model"), ("llm_prompt_version", "prompt_version")):
                if row.get(src) is None:               # re-mining keeps the earlier LLM draft (COALESCE)
                    row[src] = prev.get(dst)
        else:
            row["id"], row["created"] = nid, now
            nid += 1
        row["updated"] = now
        key = row.get("commit_key")
        advs = advisories.get(key, []) if key else _advisories_for_manual(db, row)
        pair, code, enr = m.pair_docs(row, commits.get(key) if key else None, advs)
        pairs.append(ReplaceOne({"_id": pair["_id"]}, pair, upsert=True))
        codes.append(ReplaceOne({"_id": pair["_id"]}, code, upsert=True))
        if enr:
            enrs.append(ReplaceOne({"_id": pair["_id"]}, enr, upsert=True))
    if pairs:
        db["code_pairs"].bulk_write(pairs, ordered=False)
        db["pair_code"].bulk_write(codes, ordered=False)
        if enrs:
            db["pair_enrichments"].bulk_write(enrs, ordered=False)
        legacy.touch(db)
    return len(rows)


def upsert_pairs(rows: list[dict]) -> int:
    return _save_pairs(legacy.handle(), rows, respect_edited=True)


def list_pairs(limit: int = 50, offset: int = 0, **filters) -> dict:
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    cs = _cs()
    db = legacy.handle()
    flt = _pair_filter(**filters)
    total = db["code_pairs"].count_documents(flt)
    docs = list(db["code_pairs"].find(flt).sort("_id", -1).skip(int(offset)).limit(int(limit)))
    light = ["id", "pair_key", *[c for c in cs.PAIR_FIELDS if c not in HEAVY]]
    return {"total": total, "records": [{k: r.get(k) for k in light} for r in legacy.pair_rows(db, docs, with_code=False)], 'limit': limit, 'offset': offset}


def get_pair(pair_id: int) -> dict | None:
    db = legacy.handle()
    d = db["code_pairs"].find_one({"_id": pair_id})
    return legacy.pair_rows(db, [d])[0] if d else None


def pairs_dataframe(**filters):
    import pandas as pd
    db = legacy.handle()
    docs = list(db["code_pairs"].find(_pair_filter(**filters)).sort("_id", 1))
    return pd.DataFrame(legacy.pair_rows(db, docs))


def _drop_pairs(db, flt: dict) -> int:
    ids = [d["_id"] for d in db["code_pairs"].find(flt, {"_id": 1})]
    if not ids:
        return 0
    db["code_pairs"].delete_many({"_id": {"$in": ids}})
    db["pair_code"].delete_many({"_id": {"$in": ids}})
    db["pair_enrichments"].delete_many({"_id": {"$in": ids}})
    db["pair_quality"].delete_many({"_id": {"$in": ids}})
    legacy.touch(db)
    return len(ids)


def delete_pairs_for_commit(commit_key: str) -> int:
    return _drop_pairs(legacy.handle(), {"commit_key": commit_key, "edited_at": {"$exists": False}})


def create_pair(data: dict) -> dict:
    import uuid
    cs = _cs()
    db = legacy.handle()
    rec = {c: cs._blank(data.get(c)) for c in cs.EDITABLE}
    osv_id = cs._blank(data.get("osv_id"))
    rec.update({"pair_key": f"manual|{uuid.uuid4().hex[:16]}", "commit_key": None, "advisory_ids": json.dumps([osv_id] if osv_id else []),
                "cve_ids": json.dumps([rec["cve_id"]] if rec.get("cve_id") else []),
                "ghsa_id": rec.get("ghsa_id") or (osv_id if osv_id and osv_id.startswith("GHSA-") else None),
                "source": "manual", "tangled": 0, "edited_at": util.now_z()})
    if not (rec.get("cve_id") or osv_id):
        raise ValueError("a code pair needs the record's CVE id or OSV id")
    upsert_pairs([cs._derive(rec)])
    _refresh_for_ids(rec.get("cve_id"), osv_id)
    d = db["code_pairs"].find_one({"pair_key": rec["pair_key"]})
    return legacy.pair_rows(db, [d])[0]


def update_pair(pair_id: int, data: dict) -> dict | None:
    cs = _cs()
    db = legacy.handle()
    cur = get_pair(pair_id)
    if cur is None:
        return None
    rec = {**cur, **{c: cs._blank(data[c]) for c in cs.EDITABLE if c in data}}
    rec = cs._derive(rec)
    rec["edited_at"] = util.now_z()
    _save_pairs(db, [rec], respect_edited=False)
    _refresh_for_ids(rec.get("cve_id"), (json.loads(rec.get("advisory_ids") or "[]") or [None])[0])
    return get_pair(pair_id)


def delete_pair(pair_id: int) -> bool:
    db = legacy.handle()
    cur = get_pair(pair_id)
    ok = _drop_pairs(db, {"_id": pair_id}) > 0
    if cur:
        _refresh_for_ids(cur.get("cve_id"), (json.loads(cur.get("advisory_ids") or "[]") or [None])[0])
    return ok


def _refresh_for_ids(cve_id: str | None, osv_id: str | None) -> None:
    db = legacy.handle()
    conds = ([{"cve_id": cve_id}] if cve_id else []) + ([{"osv_id": osv_id}] if osv_id else [])
    if not conds:
        return
    ids = [a["_id"] for a in db["advisories"].find({"$or": conds}, {"_id": 1})]
    if ids:
        refresh_records(ids)


def _record_pair_filter(db, cve_id: str | None, osv_id: str | None) -> dict:
    """Pairs of one advisory: by their subject CVE, by the advisories linked to their commit, or (hand-made) by the advisory itself."""
    conds: list[dict] = []
    adv_conds = ([{"cve_id": cve_id}] if cve_id else []) + ([{"osv_id": osv_id}, {"key": osv_id}] if osv_id else [])
    if cve_id:
        conds.append({"subject.cve_id": cve_id})
    if adv_conds:
        advs = list(db["advisories"].find({"$or": adv_conds}, {"_id": 1}))
        ids = [a["_id"] for a in advs]
        if ids:
            conds.append({"subject.advisory_id": {"$in": ids}})
            keys = [link["commit_key"] for link in db["fix_links"].find({"advisory_id": {"$in": ids}}, {"commit_key": 1})]
            if keys:
                conds.append({"commit_key": {"$in": keys}})
    return {"$or": conds} if conds else {"_id": -1}


def pairs_for_record(cve_id: str | None, osv_id: str | None) -> list[dict]:
    cs = _cs()
    db = legacy.handle()
    docs = list(db["code_pairs"].find(_record_pair_filter(db, cve_id, osv_id)))

    def rank(d):
        u, f = d.get("unit", {}), d.get("flags", {})
        return (not f.get("available"), u.get("granularity") != "function", u.get("granularity") != "hunk", bool(f.get("is_test")),
                bool(f.get("tangled")), d["_id"])
    docs.sort(key=rank)
    docs = docs[:200]
    light = ["id", "pair_key", *[c for c in cs.PAIR_FIELDS if c not in HEAVY]]
    out = []
    for d, r in zip(docs, legacy.pair_rows(db, docs, with_code=False)):
        row = {k: r.get(k) for k in light}
        row.update(has_advisory=int(bool(r.get("explanation"))), has_msg=int(bool((r.get("commit_message") or "").strip())),
                   has_llm=int(bool(d.get("enriched"))), fix_check=d.get("link", {}).get("fix_check"))
        out.append(row)
    return out


def delete_manual_pairs_for_record(cve_id: str | None, osv_id: str | None) -> int:
    db = legacy.handle()
    flt = {"$and": [{"origin.kind": "manual"}, _record_pair_filter(db, cve_id, osv_id)]}
    return _drop_pairs(db, flt)


def pair_summary() -> dict:
    from collections import Counter
    db = legacy.handle()
    groups = db["code_pairs"].aggregate([{"$group": {"_id": {
        "available": "$flags.available", "is_test": "$flags.is_test", "tangled": "$flags.tangled", "granularity": "$unit.granularity",
        "language": "$unit.language", "source": "$origin.sources", "skip": "$flags.skip_reason", "cwe": "$subject.cwe", "llm": "$enriched"},
        "n": {"$sum": 1}}}])
    total = usable = tangled = tests = functions = enriched = 0
    by_language, by_granularity, by_source, by_skip, cwe = Counter(), Counter(), Counter(), Counter(), Counter()
    for g in groups:
        k, n = g["_id"], g["n"]
        total += n
        by_source[",".join(k.get("source") or []) or "?"] += n
        enriched += n if k.get("llm") else 0
        if k.get("available"):
            if not k.get("is_test") and not k.get("tangled"):
                usable += n
            tangled += n if k.get("tangled") else 0
            tests += n if k.get("is_test") else 0
            functions += n if k.get("granularity") == "function" else 0
            by_language[k.get("language") if k.get("language") is not None else "?"] += n
            by_granularity[k.get("granularity")] += n
            if k.get("cwe"):
                cwe[";".join(k["cwe"])] += n
        elif k.get("available") is False:
            by_skip[k.get("skip")] += n
    cves = next(iter(db["code_pairs"].aggregate([{"$match": {"flags.available": True, "subject.cve_id": {"$exists": True}}},
                                                 {"$group": {"_id": "$subject.cve_id"}}, {"$count": "n"}])), {"n": 0})["n"]
    commits = next(iter(db["code_pairs"].aggregate([{"$match": {"flags.available": True, "commit_key": {"$exists": True}}},
                                                    {"$group": {"_id": "$commit_key"}}, {"$count": "n"}])), {"n": 0})["n"]
    return {"total": total, "usable": usable, "tangled": tangled, "tests": tests, "functions": functions, "cves": cves, "commits": commits,
            "enriched": enriched, "by_language": dict(by_language.most_common()), "by_granularity": dict(by_granularity),
            "by_source": dict(by_source), "by_skip_reason": dict(by_skip.most_common()), "top_cwe": [list(r) for r in cwe.most_common(10)]}


def pairs_for_enrichment(limit: int = 50, languages: list[str] | None = None, overwrite: bool = False, thin_only: bool = False,
                         ids: list[int] | None = None) -> list[dict]:
    cs = _cs()
    db = legacy.handle()
    cols = ["id", "cve_id", "cwe", "language", "file_path", "function_name", "granularity", "explanation", "commit_message", "diff"]
    if ids:
        docs = list(db["code_pairs"].find({"_id": {"$in": list(ids)}}))
        return [{k: r.get(k) for k in cols} for r in legacy.pair_rows(db, docs)]
    flt: dict = {"flags.available": True, "flags.is_test": {"$ne": True}, "flags.tangled": {"$ne": True}}
    blocked = [c["commit_key"] for c in db["fix_commits"].find({"$or": [{"queue.status": "unlinked"}, {"verification.fix_check": "suspect"}]}, {"commit_key": 1})]
    if blocked:
        flt["commit_key"] = {"$nin": blocked}
    if not overwrite:
        flt["enriched"] = {"$ne": True}
    if languages:
        flt["unit.language"] = {"$in": [l.lower() for l in languages]}
    docs = list(db["code_pairs"].find(flt).sort("_id", 1))

    def rank(d):
        return (bool(d.get("enriched")), d.get("unit", {}).get("granularity") != "function", d.get("unit", {}).get("granularity") != "hunk", d["_id"])
    docs.sort(key=rank)
    rows = legacy.pair_rows(db, docs[: int(limit) * 4 if thin_only else int(limit)])
    if thin_only:
        rows = [r for r in rows if len(r.get("explanation") or "") < cs.THIN_CONTEXT and not (r.get("commit_message") or "").strip()]
    return [{k: r.get(k) for k in cols} for r in rows[: int(limit)]]


def set_pair_llm(pair_id: int, root_cause: str | None, secure_pattern: str | None, explanation: str | None, model: str, prompt_version: str) -> None:
    cs = _cs()
    db = legacy.handle()
    pair = db["code_pairs"].find_one({"_id": pair_id})
    if pair is None:
        return
    enr = {"_id": pair_id, "root_cause": root_cause, "secure_pattern": secure_pattern, "explanation": explanation, "model": model,
           "prompt_version": prompt_version, "generated_at": _utc()}
    prev = db["pair_enrichments"].find_one({"_id": pair_id}) or {}
    if prev.get("original_advisory_reference"):
        enr["original_advisory_reference"] = prev["original_advisory_reference"]
    enr = {k: v for k, v in enr.items() if v is not None}
    row = get_pair(pair_id)
    f = cs.explanation_fields(row.get("explanation"), explanation, model)
    if f["original_advisory_reference"]:
        enr["original_advisory_reference"] = f["original_advisory_reference"]
    db["pair_enrichments"].replace_one({"_id": pair_id}, enr, upsert=True)
    db["code_pairs"].update_one({"_id": pair_id}, {"$set": {"enriched": root_cause is not None, "updated_at": _utc(),
                                                            "explanation.source": f["explanation_source"],
                                                            "explanation.needs_enrichment": bool(f["needs_explanation_enrichment"])}})
    legacy.touch(db)


# =================================================================================================================================
# write-back of the mining results onto the advisories

def records_for_commit(row: dict) -> list[int]:
    db = legacy.handle()
    return sorted(link["advisory_id"] for link in db["fix_links"].find({"commit_key": row["commit_key"]}, {"advisory_id": 1}))


def refresh_records(ids: list[int] | None = None, dry_run: bool = False) -> int:
    cs = _cs()
    db = legacy.handle()
    flt = {} if ids is None else {"_id": {"$in": list(ids)}}
    advs = list(db["advisories"].find(flt, {"cve_id": 1, "osv_id": 1, "mining": 1}))
    links: dict[int, list[str]] = {}
    for link in db["fix_links"].find({"advisory_id": {"$in": [a["_id"] for a in advs]}}).sort("_id", 1):
        links.setdefault(link["advisory_id"], []).append(link["commit_key"])
    keys = sorted({k for ks in links.values() for k in ks})
    commits = {c["commit_key"]: c for c in db["fix_commits"].find({"commit_key": {"$in": keys}}, {"commit_key": 1, "queue.status": 1, "git.detected_langs": 1,
                                                                                                  "verification.fix_check": 1})}
    pairs_by_commit: dict[str, list[dict]] = {}
    for p in db["code_pairs"].find({"commit_key": {"$in": keys}, "flags.available": True, "flags.is_test": {"$ne": True}, "flags.tangled": {"$ne": True}},
                                   {"commit_key": 1, "unit": 1, "extraction.changed_lines": 1}):
        pairs_by_commit.setdefault(p["commit_key"], []).append(p)
    cves = sorted({a["cve_id"] for a in advs if a.get("cve_id")})
    manual: dict[str, list[dict]] = {}
    for p in db["code_pairs"].find({"origin.kind": "manual", "flags.available": True, "flags.is_test": {"$ne": True}, "subject.cve_id": {"$in": cves}},
                                   {"subject": 1, "unit": 1, "extraction.changed_lines": 1}):
        manual.setdefault(p["subject"]["cve_id"], []).append(p)

    def view(p):                                                # the fields rank_pair reads
        return {"language": p.get("unit", {}).get("language"), "granularity": p.get("unit", {}).get("granularity"),
                "changed_lines": p.get("extraction", {}).get("changed_lines")}
    updates, changed = [], 0
    for a in advs:
        ks = links.get(a["_id"], [])
        sts = [commits.get(k, {}).get("queue", {}).get("status") for k in ks]
        ok_ks = [k for k in ks if commits.get(k, {}).get("verification", {}).get("fix_check") != "suspect"]
        n_suspect = sum(len(pairs_by_commit.get(k, [])) for k in ks if k not in ok_ks)
        pairs = [p for k in ok_ks for p in pairs_by_commit.get(k, [])] + manual.get(a.get("cve_id"), [])
        mining = a.get("mining", {})
        if mining.get("language_source") == "manual" and mining.get("languages"):
            langs, src = list(mining["languages"]), "manual"
            pairs = [p for p in pairs if p.get("unit", {}).get("language") in langs]
        else:
            langs = []
            for k in ks:
                for l in commits.get(k, {}).get("git", {}).get("detected_langs", []) or []:
                    if l not in langs:
                        langs.append(l)
            src = "git" if langs else None
        if pairs:
            status = "mined"
        elif n_suspect and not any(st in ("pending", None) for st in sts):
            status = "suspect"
        elif not ks:
            status = "no_fix"
        elif any(st in ("pending", None) for st in sts):
            status = "pending"
        elif any(st == "failed" for st in sts):
            status = "failed"
        else:
            status = "no_code"
        new = {"code_status": status, "fix_commit_count": len(ks), "pair_count": len(pairs)}
        if langs:
            new["languages"] = langs
        if src:
            new["language_source"] = src
        if pairs:
            new["best_pair_id"] = max(pairs, key=lambda p: cs.rank_pair(view(p), langs))["_id"]
        if dry_run:
            old = {k: mining.get(k) for k in ("code_status", "fix_commit_count", "pair_count", "languages", "language_source")}
            changed += int((old["code_status"], old["fix_commit_count"], old["pair_count"], old["languages"] or [], old["language_source"])
                           != (new["code_status"], new["fix_commit_count"], new["pair_count"], new.get("languages", []), new.get("language_source")))
        else:
            updates.append((a["_id"], new))
    if dry_run:
        return changed
    from app.backend.pipeline.db.operations import UpdateOne
    ops = [UpdateOne({"_id": aid}, {"$set": {"mining": new}}) for aid, new in updates]
    if ops:
        db["advisories"].bulk_write(ops, ordered=False)
        legacy.touch(db)
    return len(advs)


def reset_mining(scope: str = "mining", keep_manual: bool = True) -> dict:
    db = legacy.handle()
    keep = {"$nor": [{"origin.kind": "manual"}, {"edited_at": {"$exists": True}}]} if keep_manual else {}
    pairs = _drop_pairs(db, keep)
    if scope == "all":
        commits = db["fix_commits"].delete_many({}).deleted_count
        db["fix_links"].delete_many({})
        db["advisories"].update_many({}, {"$unset": {"mining.code_status": "", "mining.fix_commit_count": "", "mining.best_pair_id": ""},
                                          "$set": {"mining.pair_count": 0}})
        db["advisories"].update_many({"mining.language_source": {"$ne": "manual"}}, {"$unset": {"mining.languages": "", "mining.language_source": ""}})
    else:
        commits = db["fix_commits"].update_many(
            {"queue.status": {"$ne": "unlinked"}},
            {"$set": {"queue.status": "pending", "queue.attempts": 0},
             "$unset": {"queue.reason": "", "git.langs": "", "git.detected_langs": "", "git.pairs": "", "mined_at": "", "verification": ""}}).modified_count
        db["advisories"].update_many({"mining.language_source": {"$ne": "manual"}}, {"$unset": {"mining.languages": "", "mining.language_source": "",
                                                                                              "mining.best_pair_id": ""}, "$set": {"mining.pair_count": 0}})
        db["advisories"].update_many({"mining.language_source": "manual"}, {"$set": {"mining.pair_count": 0}, "$unset": {"mining.best_pair_id": ""}})
        db["advisories"].update_many({"mining.fix_commit_count": {"$gt": 0}}, {"$set": {"mining.code_status": "pending"}})
    db["system"].delete_one({"_id": "meta:mining_run"})
    cves = db["code_pairs"].distinct("subject.cve_id")
    ids = [a["_id"] for a in db["advisories"].find({"cve_id": {"$in": [c for c in cves if c]}}, {"_id": 1})] if cves else []
    legacy.touch(db)
    if ids:
        refresh_records(ids)
    return {"scope": scope, "pairs_deleted": pairs, ("commits_deleted" if scope == "all" else "commits_requeued"): commits}


# â”€â”€ pipeline selections â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _campaign_review_selection(campaign_id, filters=None):
    """Page scalar source identities using indexes, without joined source bodies.

    Each index path has one root ordinal per document. Requiring that ordinal
    and string kind excludes missing, null and array-valued source identities.
    Count and page share a read snapshot; source bodies are read only after the
    ordered page of pair IDs has been selected.
    """
    db = legacy.handle()
    commits, pairs = db['fix_commits'], db['code_pairs']
    for path in ('queue.campaign_id', 'queue.status', 'commit_key'):
        commits.ensure_path(path)
    pairs.ensure_path('commit_key')
    # Keep ownership first. With additional filter predicates SQLite otherwise
    # chose a pair-by-all-mined-commits product before joining the owner. CROSS
    # JOIN retains inner-join results while fixing this bounded lookup order.
    selected = f""" FROM {commits._values} owner
        CROSS JOIN {commits._values} state ON state.doc_key=owner.doc_key
          AND state.path='queue.status' AND state.kind=3 AND state.ordinal='/queue/status' AND state.value='mined'
        CROSS JOIN {commits._values} source ON source.doc_key=owner.doc_key
          AND source.path='commit_key' AND source.kind=3 AND source.ordinal='/commit_key'
        CROSS JOIN {commits._table} c ON c.id_key=owner.doc_key
        CROSS JOIN {pairs._values} pair ON pair.path='commit_key' AND pair.kind=3
          AND pair.ordinal='/commit_key' AND pair.value=source.value
        CROSS JOIN {pairs._table} p ON p.id_key=pair.doc_key
        WHERE owner.path='queue.campaign_id' AND owner.kind=3
          AND owner.ordinal='/queue/campaign_id' AND owner.value=:campaign"""
    params = {'campaign': campaign_id}
    if campaign_id is None:
        # Global collected-data context suggestions also include manual and
        # historical pairs. One scalar _id value per document supplies the same
        # stable indexed ID ordering without loading source bodies.
        pairs.ensure_path('_id')
        selected = f""" FROM {pairs._values} pair
            CROSS JOIN {pairs._table} p ON p.id_key=pair.doc_key
            WHERE pair.path='_id' AND pair.ordinal='/_id'"""
        params = {}
    q = filters or {}
    def indexed(path, expression, key, value, *, negate=False):
        nonlocal selected
        pairs.ensure_path(path)
        params[key] = value
        params[key + '_path'] = path
        selected += f" AND {'NOT ' if negate else ''}EXISTS (SELECT 1 FROM {pairs._values} f WHERE f.doc_key=pair.doc_key AND f.path=:{key}_path AND {expression})"
    for key, path in (('language', 'unit.language'), ('cwe', 'subject.cwe')):
        if q.get(key):
            indexed(path, f'f.kind=3 AND f.value=:{key}', key, q[key])
    if q.get('granularity'):
        kinds = ['function', 'hunk'] if q['granularity'] == 'units' else [q['granularity']]
        indexed('unit.granularity', 'f.kind=3 AND f.value IN (SELECT value FROM json_each(:granularity))',
                'granularity', json.dumps(kinds))
    if q.get('exclude_tests'):
        indexed('flags.is_test', 'f.kind=1 AND f.value=:is_test', 'is_test', 1, negate=True)
    if q.get('usable_only'):
        indexed('flags.available', 'f.kind=1 AND f.value=:available', 'available', 1)
        indexed('flags.tangled', 'f.kind=1 AND f.value=:tangled', 'tangled', 1, negate=True)
    if q.get('search'):
        for path in ('pair_key', 'unit.file', 'subject.package'):
            pairs.ensure_path(path)
        params['search'] = q['search']
        selected += f" AND EXISTS (SELECT 1 FROM {pairs._values} f WHERE f.doc_key=pair.doc_key AND f.path IN ('pair_key','unit.file','subject.package') AND f.kind=3 AND instr(lower(f.value),lower(:search))>0)"
    from .codec import id_key
    for key, operation in (('_include_ids', 'IN'), ('_exclude_ids', 'NOT IN')):
        if key in q:
            params[key] = json.dumps([id_key(value) for value in q[key]])
            selected += f' AND pair.doc_key {operation} (SELECT value FROM json_each(:{key}))'
    if q.get('context_quality') or q.get('draft_quality'):
        from app.backend.pipeline.acceleration.context_quality import prepare
        prepare(db)
    return db, pairs, selected, params


def campaign_review_ids(campaign_id, *, filters=None, selection_limit=None, with_total=False):
    """An ordered ID snapshot only; source bodies stay on disk."""
    db, pairs, selected, params = _campaign_review_selection(campaign_id, filters)
    from .codec import decode_document
    with db.transaction(write=False):
        conn = db._connection()
        from app.backend.pipeline.acceleration.context_quality import select
        selected, params, _ = select(db, pairs, selected, params, filters or {})
        total = conn.execute('SELECT count(*)' + selected, params).fetchone()[0] if with_total else None
        tail = ''
        if selection_limit is not None:
            params['selection_limit'] = int(selection_limit)
            tail = ' LIMIT :selection_limit'
        rows = db._connection().execute('SELECT pair.doc_key' + selected +
            ' ORDER BY pair.id_sort,pair.doc_key' + tail, params)
        ids = [decode_document(*json.loads(r[0])) for r in rows]
        return (ids, total) if with_total else ids


def campaign_review_page(campaign_id: str, *, offset=0, limit=100, filters=None, with_quality=False):
    from .query import field
    db, pairs, selected, params = _campaign_review_selection(campaign_id, filters)
    with db.transaction(write=False):
        conn = db._connection()
        from app.backend.pipeline.acceleration.context_quality import select, page_assessments
        selected, params, ratings = select(db, pairs, selected, params, filters or {})
        params.update(limit=min(500, max(1, int(limit))), offset=max(0, int(offset)))
        total = conn.execute('SELECT count(*)' + selected, params).fetchone()[0]
        columns = ','.join(field('d.payload', path) for path in ('_id', 'pair_key', 'unit.file', 'unit.language'))
        rows = list(conn.execute('WITH page AS MATERIALIZED (SELECT pair.doc_key,pair.id_sort' + selected +
            ' ORDER BY pair.id_sort,pair.doc_key LIMIT :limit OFFSET :offset) SELECT ' + columns +
            ' FROM page JOIN ' + pairs._table + ' d ON d.id_key=page.doc_key ORDER BY page.id_sort,page.doc_key', params))
        if with_quality:
            quality = ({row[0]: ratings[row[0]] for row in rows} if ratings else
                       page_assessments(db, [row[0] for row in rows]))
            return rows, total, quality
    return rows, total


def campaign_outcomes(campaign_id: str) -> dict:
    db = legacy.handle()
    flt = {"queue.campaign_id": campaign_id}
    by_status = {(g["_id"] or "unknown"): g["n"] for g in db["fix_commits"].aggregate([{"$match": flt}, {"$group": {"_id": "$queue.status", "n": {"$sum": 1}}}])}
    reasons = [{"status": g["_id"]["status"], "reason": g["_id"]["reason"], "n": g["n"]} for g in db["fix_commits"].aggregate([
        {"$match": {**flt, "queue.status": {"$in": ["failed", "no_code", "skipped"]}}},
        {"$group": {"_id": {"status": "$queue.status", "reason": {"$ifNull": ["$queue.reason", "(none given)"]}}, "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 25}])]
    return {"commits": sum(by_status.values()), "by_status": by_status, "reasons": reasons}


def campaign_mined(campaign_id: str) -> list[tuple[str, int]]:
    cur = legacy.handle()["fix_commits"].find({"queue.campaign_id": campaign_id, "mined_at": {"$exists": True}}, {"mined_at": 1, "git.pairs": 1}).sort("mined_at", 1)
    return [(m.fmt_dt(d["mined_at"]), (d.get("git") or {}).get("pairs") or 0) for d in cur]


def campaign_mix(campaign_id: str) -> dict:
    from app.backend.pipeline import store
    db = legacy.handle()
    keys = [d["commit_key"] for d in db["fix_commits"].find({"queue.campaign_id": campaign_id}, {"commit_key": 1})]
    pairs = commits = 0
    totals: dict[str, dict] = {"language": {}, "granularity": {}, "ecosystem": {}, "cwe": {}}
    per_cve: dict[str, int] = {}

    def add(into: dict, rows) -> None:
        for g in rows:
            k = g["_id"] if g["_id"] not in (None, "") else "unknown"
            into[k] = into.get(k, 0) + g["n"]
    for i in range(0, len(keys), 5000):
        chunk = keys[i:i + 5000]
        group = lambda field: [{"$group": {"_id": field, "n": {"$sum": 1}}}]
        out = next(iter(db["code_pairs"].aggregate([
            {"$match": {"commit_key": {"$in": chunk}, "flags.available": True, "flags.is_test": {"$ne": True}, "flags.tangled": {"$ne": True}}},
            {"$facet": {"n": [{"$count": "n"}], "commits": [{"$group": {"_id": "$commit_key"}}, {"$count": "n"}], "language": group("$unit.language"),
                        "granularity": group("$unit.granularity"), "ecosystem": group("$subject.ecosystem"),
                        "cwe": [{"$unwind": {"path": "$subject.cwe", "preserveNullAndEmptyArrays": True}}, *group("$subject.cwe")], "cve": group("$subject.cve_id")}}])), {})
        pairs += (out.get("n") or [{"n": 0}])[0]["n"]
        commits += (out.get("commits") or [{"n": 0}])[0]["n"]
        for name in totals:
            add(totals[name], out.get(name) or [])
        add(per_cve, out.get("cve") or [])
    words: dict[str, str] = {}
    cves = [c for c in per_cve if c != "unknown"]
    for i in range(0, len(cves), 2000):
        for a in db["advisories"].find({"cve_id": {"$in": cves[i:i + 2000]}}, {"cve_id": 1, "osv_severity": 1}):
            words.setdefault(a["cve_id"], (a.get("osv_severity") or "").upper())
    severity: dict[str, int] = {}
    for cve, n in per_cve.items():
        band = store.BAND_OF_WORD.get(words.get(cve, ""), "unrated")
        severity[band] = severity.get(band, 0) + n
    sort = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1]))
    cwe = sort({("none given" if k == "unknown" else k): n for k, n in totals["cwe"].items()})
    return {"pairs": pairs, "commits": commits, "language": sort(totals["language"]), "granularity": sort(totals["granularity"]), "ecosystem": sort(totals["ecosystem"]),
            "cwe": dict(list(cwe.items())[:15]), "severity": severity}


def commits_by_status(status: str, only_unchecked: bool = False, ecosystems: list[str] | None = None) -> list[dict]:
    db = legacy.handle()
    flt: dict = _scoped({"queue.status": status}, ecosystems)
    if only_unchecked:
        flt["verification.fix_check"] = {"$exists": False}
    return legacy.commit_rows(db, list(db["fix_commits"].find(flt).sort("_id", 1)))


def count_commits_by_status(status: str, only_unchecked: bool = False, ecosystems: list[str] | None = None) -> int:
    flt: dict = _scoped({"queue.status": status}, ecosystems)
    if only_unchecked:
        flt["verification.fix_check"] = {"$exists": False}
    return legacy.handle()["fix_commits"].count_documents(flt)


def pair_files_of(commit_key: str) -> list[str]:
    return sorted(legacy.handle()["code_pairs"].distinct("unit.file", {"commit_key": commit_key}))


def set_link_evidence(commit_key: str, signals_json: str, level: str) -> None:
    db = legacy.handle()
    db["fix_commits"].update_one({"commit_key": commit_key}, {"$set": {"evidence.signals": m.loads(signals_json, []), "evidence.level": level}})
    _sync_pair_snapshot(db, commit_key)
    legacy.touch(db)


def attribution_commit_keys(only_missing: bool = True, version: int | None = None) -> list[str]:
    db = legacy.handle()
    flt: dict = {"flags.available": True, "commit_key": {"$exists": True}}
    if not only_missing:
        return db["code_pairs"].distinct("commit_key", flt)
    if version is None:
        return db["code_pairs"].distinct("commit_key", {**flt, "attribution.basis": {"$exists": False}})
    current = {d["_id"] for d in db["pair_quality"].find({"version": int(version)}, {"_id": 1})}
    missing: set[str] = set()
    for d in db["code_pairs"].find(flt, {"_id": 1, "commit_key": 1, "attribution.basis": 1}):
        if d["_id"] not in current or not (d.get("attribution") or {}).get("basis"):
            missing.add(d["commit_key"])
    return sorted(missing)


def pairs_for_attribution(commit_key: str, with_diff: bool = False) -> list[dict]:
    db = legacy.handle()
    docs = list(db["code_pairs"].find({"commit_key": commit_key, "flags.available": True}).sort("_id", 1))
    keys = ("pair_key", "granularity", "function_name", "file_path", "is_test", "code_available", "explanation", "commit_message", "match_kind") + (("diff", "content_hash") if with_diff else ())
    return [{k: r.get(k) for k in keys} for r in legacy.pair_rows(db, docs, with_code=with_diff)]


def pair_attribution_for(pair_ids: list[int]) -> dict[int, dict]:
    if not pair_ids:
        return {}
    db = legacy.handle()
    ids = [int(i) for i in pair_ids]
    quality = {d["_id"]: d for d in db["pair_quality"].find({"_id": {"$in": ids}})}
    out = {}
    for d in db["code_pairs"].find({"_id": {"$in": ids}, "flags.available": True}, {"attribution.basis": 1}):
        basis = (d.get("attribution") or {}).get("basis")
        q = quality.get(d["_id"], {})
        out[d["_id"]] = {"attribution_basis": basis, "attribution_score": m.ATTRIBUTION_SCORES.get(basis) if basis else None, "change_key": q.get("change_key"), "quality_hash": q.get("content_hash")}
    return out


def set_pair_quality(rows: list[dict]) -> None:
    from app.backend.pipeline.db.operations import ReplaceOne
    if not rows:
        return
    db = legacy.handle()
    ids = {d["pair_key"]: d["_id"] for d in db["code_pairs"].find({"pair_key": {"$in": [r["pair_key"] for r in rows]}}, {"_id": 1, "pair_key": 1})}
    ops = [ReplaceOne({"_id": ids[r["pair_key"]]}, {"_id": ids[r["pair_key"]], "pair_key": r["pair_key"], "version": int(r["version"]), "content_hash": r.get("content_hash"),
                                                    "change_key": r.get("change_key"), "signals": list(r.get("signals") or [])}, upsert=True) for r in rows if r["pair_key"] in ids]
    if ops:
        db["pair_quality"].bulk_write(ops, ordered=False)
        legacy.touch(db)


def pair_quality_for(pair_keys: list[str]) -> dict[str, dict]:
    if not pair_keys:
        return {}
    return {d["pair_key"]: {"version": d.get("version"), "content_hash": d.get("content_hash"), "change_key": d.get("change_key"), "signals": list(d.get("signals") or [])}
            for d in legacy.handle()["pair_quality"].find({"pair_key": {"$in": list(pair_keys)}})}


def set_attribution(updates) -> None:
    from app.backend.pipeline.db.operations import UpdateOne
    db = legacy.handle()
    ops = [UpdateOne({"pair_key": pair_key}, {"$set": {"attribution.basis": basis}}) for basis, _score, pair_key in updates]
    if ops:
        db["code_pairs"].bulk_write(ops, ordered=False)
        legacy.touch(db)


def _split_filter(ecosystems: list[str] | None, unsplit_only: bool) -> dict:
    flt: dict = {"unit.granularity": "file", "flags.available": True, "origin.kind": {"$ne": "manual"}, "pair_key": {"$regex": r":\*$"}}
    if ecosystems is not None:
        flt["subject.ecosystem"] = {"$in": list(ecosystems)}
    if unsplit_only:
        flt["extraction.parse.status"] = None                      # missing or null: the splitter has not seen the pair yet
    return flt


def file_pair_ids_to_split(ecosystems: list[str] | None = None, unsplit_only: bool = False) -> list[int]:
    return [d["_id"] for d in legacy.handle()["code_pairs"].find(_split_filter(ecosystems, unsplit_only), {"_id": 1}).sort("_id", 1)]


def count_file_pairs_to_split(ecosystems: list[str] | None = None, unsplit_only: bool = False) -> int:
    return legacy.handle()["code_pairs"].count_documents(_split_filter(ecosystems, unsplit_only))


def pairs_by_ids(ids) -> list[dict]:
    if not ids:
        return []
    db = legacy.handle()
    return legacy.pair_rows(db, list(db["code_pairs"].find({"_id": {"$in": list(ids)}}).sort("_id", 1)))


def derived_pair_keys(prefix: str, *, commit_key: str | None = None) -> list[str]:
    flt = {"pair_key": {"$regex": "^" + re.escape(prefix)}, "edited_at": {"$exists": False}}
    if commit_key:
        # The unchanged literal prefix still decides which keys may be removed.
        # Membership first limits regex work to this exact snapshot's pairs.
        flt['commit_key'] = {'$in': [commit_key]}
    return [d["pair_key"] for d in legacy.handle()["code_pairs"].find(flt, {"pair_key": 1}) if not d["pair_key"].endswith(":*")]


def delete_pairs_by_keys(keys) -> int:
    if not keys:
        return 0
    return _drop_pairs(legacy.handle(), {"pair_key": {"$in": list(keys)}})


def set_parse_status(updates) -> None:
    from app.backend.pipeline.db.operations import UpdateOne
    db = legacy.handle()
    ops = []
    for status, reason, pair_key in updates:
        op: dict = {"$set": {}, "$unset": {}}
        (op["$set"] if status else op["$unset"])["extraction.parse.status"] = status or ""
        (op["$set"] if reason else op["$unset"])["extraction.parse.reason"] = reason or ""
        ops.append(UpdateOne({"pair_key": pair_key}, {k: v for k, v in op.items() if v}))
    if ops:
        db["code_pairs"].bulk_write(ops, ordered=False)
        legacy.touch(db)


def file_pair_rows(commit_keys) -> list[dict]:
    db = legacy.handle()
    cols = ("pair_key", *_cs().PAIR_FIELDS)
    out: list[dict] = []
    for ch in _chunks(commit_keys):
        docs = list(db["code_pairs"].find({"commit_key": {"$in": ch}, "unit.granularity": "file", "flags.available": True}).sort("_id", 1))
        out += [{c: r.get(c) for c in cols} for r in legacy.pair_rows(db, docs)]
    return out


def set_file_meta(updates) -> None:
    from app.backend.pipeline.db.operations import UpdateOne
    db = legacy.handle()
    ops = [UpdateOne({"pair_key": pair_key}, {"$set": {"extraction.meta": json.loads(meta)}} if meta else {"$unset": {"extraction.meta": ""}}) for meta, pair_key in updates]
    if ops:
        db["code_pairs"].bulk_write(ops, ordered=False)
        legacy.touch(db)


def function_pair_counts(commit_keys) -> dict[str, int]:
    db = legacy.handle()
    return {g["_id"]: g["n"] for g in db["code_pairs"].aggregate([
        {"$match": {"commit_key": {"$in": list(commit_keys)}, "unit.granularity": "function", "flags.available": True, "flags.is_test": {"$ne": True}}},
        {"$group": {"_id": "$commit_key", "n": {"$sum": 1}}}])}


def set_tangled(commit_keys, tangled_keys) -> None:
    db = legacy.handle()
    if commit_keys:
        db["code_pairs"].update_many({"commit_key": {"$in": list(commit_keys)}, "edited_at": {"$exists": False}}, {"$set": {"flags.tangled": False}})
    if tangled_keys:
        db["code_pairs"].update_many({"commit_key": {"$in": list(tangled_keys)}, "flags.is_test": {"$ne": True}}, {"$set": {"flags.tangled": True}})
    legacy.touch(db)


# â”€â”€ pair and commit reads of the dataset builders, the leakage graph and the reviewed build â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_CODE_COLUMNS = {"code_before", "code_after", "diff"}


def _chunks(items, size: int = 500):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def commits_basic(commit_keys, columns) -> list[dict]:
    db = legacy.handle()
    out: list[dict] = []
    for ch in _chunks(commit_keys):
        out += [{c: r.get(c) for c in columns} for r in legacy.commit_rows(db, list(db["fix_commits"].find({"commit_key": {"$in": ch}})))]
    return out


def usable_pairs_of_commits(commit_keys, columns) -> list[dict]:
    db = legacy.handle()
    with_code = bool(set(columns) & _CODE_COLUMNS)
    out: list[dict] = []
    for ch in _chunks(commit_keys):
        docs = list(db["code_pairs"].find({"commit_key": {"$in": ch}, "flags.available": True}))
        out += [{c: r.get(c) for c in columns} for r in legacy.pair_rows(db, docs, with_code=with_code)]
    return sorted(out, key=lambda r: r["pair_key"]) if out and "pair_key" in out[0] else out


def pair_code_of(ids, columns) -> dict[int, dict]:
    db = legacy.handle()
    with_code = bool(set(columns) & _CODE_COLUMNS)
    out: dict[int, dict] = {}
    for ch in _chunks(ids):
        for r in legacy.pair_rows(db, list(db["code_pairs"].find({"_id": {"$in": ch}})), with_code=with_code):
            out[r["id"]] = {"id": r["id"], **{c: r.get(c) for c in columns}}
    return out


def file_pairs_of_commits(commit_keys) -> list[dict]:
    db = legacy.handle()
    out: list[dict] = []
    for ch in _chunks(commit_keys):
        docs = list(db["code_pairs"].find({"commit_key": {"$in": ch}, "unit.granularity": "file"}))
        out += [{c: r.get(c) for c in ("commit_key", "file_path", "language", "code_before", "code_after")} for r in legacy.pair_rows(db, docs)]
    return out


def best_pair_candidates() -> list[dict]:
    flt = {"flags.available": True, "flags.is_test": {"$ne": True}, "flags.tangled": {"$ne": True}, "subject.cve_id": {"$exists": True},
           "link.fix_check": {"$ne": "suspect"}}
    proj = {"subject.cve_id": 1, "unit.granularity": 1, "extraction.changed_lines": 1, "unit.file": 1, "unit.function": 1}
    return [{"id": d["_id"], "cve_id": d["subject"]["cve_id"], "granularity": d["unit"].get("granularity"),
             "changed_lines": d.get("extraction", {}).get("changed_lines") or 0, "file_path": d["unit"].get("file"),
             "function_name": d["unit"].get("function")} for d in legacy.handle()["code_pairs"].find(flt, proj).sort("_id", 1)]


def _light_rows(db, docs: list[dict]) -> list[dict]:
    out = []
    for d, r in zip(docs, legacy.pair_rows(db, docs, with_code=False)):
        ext, msg = d.get("extraction", {}), r.get("commit_message")
        out.append({"id": r["id"], "pair_key": r["pair_key"], "commit_key": r.get("commit_key"), "cve_id": r.get("cve_id"), "language": r.get("language"),
                    "granularity": r.get("granularity"), "is_test": r.get("is_test"), "tangled": r.get("tangled") or 0,
                    "file_path": r.get("file_path"), "function_name": r.get("function_name"), "changed_lines": r.get("changed_lines") or 0,
                    "content_hash": r.get("content_hash"), "commit_date": r.get("commit_date"), "repo_url": r.get("repo_url"),
                    "fix_commit": r.get("fix_commit"), "parent_commit": r.get("parent_commit"), "source": r.get("source"),
                    "quarantine_reason": r.get("quarantine_reason"), "lines": ext.get("lines"), "has_msg": int(bool((msg or "").strip())),
                    "enriched": int(r.get("root_cause_llm") is not None), "span": r.get("span"), "msg_head": None if msg is None else msg[:300],
                    "llm_prompt_version": r.get("llm_prompt_version"), "llm_model": r.get("llm_model"), "has_code": int(bool(ext.get("has_code")))})
    return out


def light_pair_inputs(commit_keys, cve_ids) -> tuple[dict, dict, dict]:
    db = legacy.handle()
    checks: dict = {}
    by_commit: dict = {}
    manual: dict = {}
    for ch in _chunks(commit_keys):
        for r in legacy.commit_rows(db, list(db["fix_commits"].find({"commit_key": {"$in": ch}}))):
            checks[r["commit_key"]] = {"fix_check": r.get("fix_check"), "via": r.get("via"), "fix_check_package": r.get("fix_check_package")}
        for p in _light_rows(db, list(db["code_pairs"].find({"commit_key": {"$in": ch}, "flags.available": True}))):
            by_commit.setdefault(p["commit_key"], []).append(p)
    for ch in _chunks(cve_ids):
        for p in _light_rows(db, list(db["code_pairs"].find({"subject.cve_id": {"$in": ch}, "origin.kind": "manual", "flags.available": True}))):
            manual.setdefault(p["cve_id"], []).append(p)
    return checks, by_commit, manual


def reconcile_missing_fix_commits(ecosystems=None, *, should_stop=lambda:False, progress=None):
    """Repair orphan links with bounded indexed reads and cooperative safe points.

    A nested document lookup used to rebuild the joined payload repeatedly for
    every saved link. Resume needs only commit membership and the few eligible
    advisory fields, never whole commit or advisory bodies.
    """
    from itertools import islice
    import time
    from app.backend.pipeline import stopping
    stopping.check(should_stop)
    db=legacy.handle()
    advisory_filter={'kind':'vulnerability','withdrawn_at':{'$in':[None]}}
    if ecosystems is not None:advisory_filter['ecosystem']={'$in':list(ecosystems)}
    count,scanned,last_report=0,0,0.0
    # Track only keys restored by this call, so their later advisory aliases
    # still receive scope tags when links span more than one page.
    restored=set()

    def report(force=False):
        nonlocal last_report
        now=time.monotonic()
        if progress is not None and (force or now-last_report>=1):
            progress(state='reconciling_queue',phase='reconciling_queue',
                     label=f'Checking saved fix links: {scanned} checked, {count} restored')
            last_report=now

    report(force=True)
    cursor=db.fix_links.find({}, {'commit_key':1,'advisory_id':1,'via':1,'sources':1}).batch_size(100)
    source=iter(cursor)
    try:
        while True:
            stopping.check(should_stop)
            links=list(islice(source,100))
            if not links:break
            keys=list(dict.fromkeys(link['commit_key'] for link in links))
            present={doc['commit_key'] for doc in db.fix_commits.find(
                {'commit_key':{'$in':keys}}, {'commit_key':1})}
            missing=[link for link in links if link['commit_key'] not in present or link['commit_key'] in restored]
            if missing:
                stopping.check(should_stop)
                advisories={doc['_id']:doc for doc in db.advisories.find(
                    {**advisory_filter,'_id':{'$in':list(dict.fromkeys(link['advisory_id'] for link in missing))}},
                    {'osv_id':1,'cve_id':1,'ecosystem':1})}
                batch=[]
                for link in missing:
                    advisory=advisories.get(link['advisory_id'])
                    if advisory is None:continue
                    pointer=m._pointer(link)
                    batch.append({**pointer,'ecosystem':advisory['ecosystem'],
                                  'advisory_id':advisory.get('osv_id') or advisory.get('cve_id'),
                                  'cve_id':advisory.get('cve_id')})
                if batch:
                    stopping.check(should_stop)
                    count+=queue_fix_commits(batch)[0]
                    restored.update(row['commit_key'] for row in batch)
            scanned+=len(links)
            report()
    finally:
        source.close()
    report(force=True)
    return count
