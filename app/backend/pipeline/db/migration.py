"""Read-only source export and staged, verified import into a fresh local file.

No application bootstrap is called here. In particular, a source never receives
schema changes, collection creation, index creation, or application writes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import uuid
from contextlib import contextmanager
import re

FORMAT = "ztsa-typed-export/1"
OPTIONS_FORMAT = "ztsa-collection-options/1"


class MigrationError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def _sync_file(path):
    with Path(path).open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def _migration_lock(target):
    from app.backend.pipeline.oslock import FileLock, Busy
    lock = FileLock(target.with_name(target.name + ".migration.lock"), note=False)
    try:
        lock.acquire(wait=0)
    except Busy as exc:
        raise MigrationError("Destination migration lock is already in use") from exc
    try: yield
    finally: lock.release()


def _sidecars(path):
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _index_signature(index):
    keys = list(index.get("key", {}).items())
    return {"key": keys, "unique": keys == [("_id", 1)] or bool(index.get("unique", False)),
            **{k: index[k] for k in ("partialFilterExpression", "expireAfterSeconds", "sparse") if k in index}}


def _document_digest(doc):
    from .codec import canonical_bytes
    return hashlib.sha256(canonical_bytes(doc)).digest()


def _digest(hashes):
    """Order-independent digest; duplicates are retained, not collapsed."""
    digest = hashlib.sha256()
    for value in sorted(hashes):
        digest.update(value)
    return digest.hexdigest()


def _snapshot(collection):
    hashes = []
    for doc in collection.find({}):
        hashes.append(_document_digest(doc))
    return {"count": len(hashes), "digest": _digest(hashes)}


def _collection_options(source):
    """Read the live catalog. An absent catalog is never silently invented."""
    return {row["name"]: dict(row.get("options", {})) for row in source.list_collections()
            if not row["name"].startswith("system.")}


def _decode_options(entry):
    from .codec import decode_document
    try:
        envelope = entry["options"]
        options = decode_document(envelope["doc"], envelope["types"])
        if not isinstance(options, dict): raise ValueError("not a document")
        return options
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise MigrationError("Malformed typed collection options") from exc


def _check_options(options):
    from .collection_options import check_options
    from .operations import OperationFailure
    try: return check_options(options)
    except OperationFailure as exc: raise MigrationError(str(exc)) from exc


def reference_findings(db):
    """Report known hard references without rejecting pre-existing repair data."""
    from .codec import canonical_bytes
    names = set(db.list_collection_names())
    rules = (
        ("fix_links", "advisory_id", "advisories", "_id"),
        ("fix_links", "commit_key", "fix_commits", "commit_key"),
        ("pair_code", "_id", "code_pairs", "_id"),
        ("pair_enrichments", "_id", "code_pairs", "_id"),
        ("pair_quality", "_id", "code_pairs", "_id"),
        ("agent_messages", "sid", "agent_sessions", "_id"),
        ("agent_journal", "run_id", "agent_runs", "_id"),
        ("agent_runs", "session_id", "agent_sessions", "_id"),
        ("context_links", "bundle_hash", "context_bundles", "_id"),
    )
    findings = {}
    for child, field, parent, parent_field in rules:
        if child not in names:
            continue
        have = {canonical_bytes(d[parent_field]) for d in db[parent].find({}, {parent_field: 1}) if parent_field in d} if parent in names else set()
        missing = [d.get("_id") for d in db[child].find({}, {field: 1})
                   if field in d and canonical_bytes(d[field]) not in have]
        findings[f"{child}.{field}->{parent}.{parent_field}"] = {
            "missing": len(missing), "sample": sorted(str(x) for x in missing)[:10]}
    return findings


def counter_findings(db):
    names = set(db.list_collection_names())
    counters = {d["_id"]: d.get("seq", 0) for d in db["counters"].find({})} if "counters" in names else {}
    # These collections reserve IDs through db.mongo.next_id, not ObjectIds.
    numbered = {"advisories", "fix_commits", "code_pairs", "review_decisions", "leakage_edges", "campaign_events"}
    findings = {}
    for name in sorted(names & numbered):
        highest = max((int(d["_id"]) for d in db[name].find({}, {"_id": 1})
                       if isinstance(d.get("_id"), int) and not isinstance(d["_id"], bool)), default=0)
        if highest:
            value = counters.get(name, 0)
            if not isinstance(value, int) or value < highest:
                findings[name] = {"stored": value, "minimum": highest}
    return findings


def export_database(source, directory, *, writers_quiesced=False, batch_size=2000):
    """Export every non-system collection. Caller must have stopped all writers."""
    from .codec import encode_document
    if not writers_quiesced:
        raise MigrationError("Export requires an explicit assertion that all writers are quiesced")
    directory = Path(directory).resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise MigrationError("Export directory already exists; choose a fresh directory") from exc
    names = sorted(n for n in source.list_collection_names() if not n.startswith("system."))
    options = _collection_options(source)
    if set(options) != set(names): raise MigrationError("Source collection options inventory changed")
    before = {n: source[n].count_documents({}) for n in names}
    manifest = {"format": FORMAT, "export_id": uuid.uuid4().hex,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "source": {"database": source.name}, "writers_quiesced": True, "collections": {}}
    for name in names:
        filename = hashlib.sha256(name.encode("utf-8")).hexdigest() + ".jsonl"
        hashes = []
        cursor = source[name].find({}).batch_size(max(1, int(batch_size)))
        with (directory / filename).open("x", encoding="utf-8", newline="\n") as stream:
            for document in cursor:
                text, types = encode_document(document)
                stream.write(_json({"doc": text, "types": types}) + "\n")
                hashes.append(_document_digest(document))
            stream.flush()
            os.fsync(stream.fileno())
        indexes = []
        for index in source[name].list_indexes():
            text, types = encode_document(dict(index))
            indexes.append({"doc": text, "types": types})
        search_indexes = []
        search_error = None
        listing = getattr(source[name], "list_search_indexes", None)
        if callable(listing):
            try:
                for index in listing():
                    text, types = encode_document(dict(index))
                    search_indexes.append({"doc": text, "types": types})
            except Exception as exc:
                # Standalone MongoDB may not provide mongot. Preserve the fact
                # that its search metadata could not be read, without secrets.
                search_error = type(exc).__name__
        manifest["collections"][name] = {"file": filename, "count": len(hashes), "digest": _digest(hashes),
                                             "sha256": _sha(directory / filename), "indexes": indexes,
                                             "search_indexes": search_indexes, "search_metadata_error": search_error}
        text, types = encode_document(options[name])
        manifest["collections"][name]["options"] = {"doc": text, "types": types}
    after_names = sorted(n for n in source.list_collection_names() if not n.startswith("system."))
    after = {n: source[n].count_documents({}) for n in after_names}
    if before != after or any(before[n] != manifest["collections"][n]["count"] for n in names):
        raise MigrationError("Source counts changed during export; stop all writers and export again")
    manifest["references"] = reference_findings(source)
    manifest["counter_findings"] = counter_findings(source)
    # Snapshot sessions make Mongo document reads consistent; this additional
    # live scan checks that the operator's quiescence assertion still holds.
    live = getattr(source, "_migration_live_source", source)
    if sorted(n for n in live.list_collection_names() if not n.startswith("system.")) != names:
        raise MigrationError("Source collection inventory changed during export")
    if _collection_options(live) != options:
        raise MigrationError("Source collection options changed during export")
    from .codec import decode_document
    for name, entry in manifest["collections"].items():
        if _snapshot(live[name]) != {"count": entry["count"], "digest": entry["digest"]}:
            raise MigrationError(f"Source contents changed during export: {name}; quiesce all writers")
        expected = {decode_document(i["doc"], i["types"])["name"]:
                    _index_signature(decode_document(i["doc"], i["types"])) for i in entry["indexes"]}
        actual = {i["name"]: _index_signature(i) for i in live[name].list_indexes()}
        if actual != expected:
            raise MigrationError(f"Source indexes changed during export: {name}")
    manifest["consistency"] = "snapshot-and-live-recheck" if live is not source else "quiescence-and-live-recheck"
    _save(directory / "manifest.json", manifest)
    return manifest


def _load_export(directory, options_path=None):
    directory = Path(directory).resolve()
    try:
        raw_manifest = (directory / "manifest.json").read_bytes()
        manifest = json.loads(raw_manifest)
    except (OSError, ValueError) as exc:
        raise MigrationError("Export manifest is missing or unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("writers_quiesced") is not True:
        raise MigrationError("Unsupported export format or missing quiescence assertion")
    if not isinstance(manifest.get("collections"), dict) or not isinstance(manifest.get("export_id"), str):
        raise MigrationError("Malformed manifest collection inventory")
    for name, entry in manifest["collections"].items():
        if not isinstance(name, str) or not name or "\x00" in name or not isinstance(entry, dict):
            raise MigrationError("Malformed manifest collection entry")
        if any(not isinstance(entry.get(key), str) for key in ("file", "digest", "sha256")) or type(entry.get("count")) is not int or entry["count"] < 0:
            raise MigrationError("Malformed manifest collection hash or count")
        if any(not re.fullmatch("[0-9a-f]{64}", entry[key]) for key in ("digest", "sha256")):
            raise MigrationError("Malformed manifest collection digest")
        filename = entry["file"]
        if filename != hashlib.sha256(name.encode("utf-8")).hexdigest() + ".jsonl":
            raise MigrationError("Export contains an unsafe collection filename")
        path = directory / filename
        if path.is_symlink() or not path.is_file() or _sha(path) != entry["sha256"]:
            raise MigrationError(f"Export file hash mismatch for collection {name}")
        if "options" in entry: _decode_options(entry)
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    if options_path is not None:
        try:
            raw_options = Path(options_path).read_bytes()
            catalog = json.loads(raw_options)
        except (OSError, ValueError) as exc:
            raise MigrationError("Collection options receipt is missing or unreadable") from exc
        if (not isinstance(catalog, dict) or catalog.get("format") != OPTIONS_FORMAT
                or catalog.get("manifest_sha256") != manifest_hash
                or catalog.get("export_id") != manifest["export_id"]
                or catalog.get("source") != manifest.get("source")
                or catalog.get("writers_quiesced") is not True):
            raise MigrationError("Collection options receipt is not bound to this export manifest")
        entries = catalog.get("collections")
        if not isinstance(entries, dict) or set(entries) != set(manifest["collections"]):
            raise MigrationError("Collection options receipt inventory mismatch")
        for name, envelope in entries.items():
            _check_options(_decode_options({"options": envelope}))
            manifest["collections"][name]["options"] = envelope
        manifest["_options_sha256"] = hashlib.sha256(raw_options).hexdigest()
    return directory, manifest, manifest_hash


def _documents(path):
    from .codec import decode_document
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                envelope = json.loads(line)
                document = decode_document(envelope["doc"], envelope["types"])
                if "_id" not in document:
                    raise ValueError("missing _id")
                yield document
            except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
                raise MigrationError("Malformed typed document in export") from exc


def verify_export(directory, target, *, expected_manifest_hash=None, options_path=None, expected_options_hash=None):
    """Prove content equality, reference preservation, and safe sequence floors."""
    _, manifest, manifest_hash = _load_export(directory, options_path)
    if expected_manifest_hash is not None and expected_manifest_hash != manifest_hash:
        raise MigrationError("Export manifest changed during import")
    if expected_options_hash is not None and manifest.get("_options_sha256") != expected_options_hash:
        raise MigrationError("Collection options receipt changed during import")
    expected_names = set(manifest["collections"])
    actual_names = {n for n in target.list_collection_names() if not n.startswith("system.")}
    if actual_names != expected_names:
        raise MigrationError("Collection inventory mismatch")
    checked = {}
    target_options = _collection_options(target) if any("options" in e for e in manifest["collections"].values()) else {}
    source_only_options = {}
    for name, entry in manifest["collections"].items():
        actual = _snapshot(target[name])
        if actual["count"] != entry["count"] or actual["digest"] != entry["digest"]:
            raise MigrationError(f"Document count or digest mismatch for collection {name}")
        checked[name] = actual["count"]
        from .codec import decode_document
        expected_indexes = {decode_document(i["doc"], i["types"])["name"]:
                            _index_signature(decode_document(i["doc"], i["types"])) for i in entry.get("indexes", [])}
        actual_indexes = {i["name"]: _index_signature(i) for i in target[name].list_indexes()}
        if actual_indexes != expected_indexes:
            raise MigrationError(f"Collection index definition mismatch: {name}")
        if "options" in entry:
            from .collection_options import PHYSICAL, logical_signature
            options = _check_options(_decode_options(entry))
            if logical_signature(target_options[name]) != logical_signature(options):
                raise MigrationError(f"Collection options mismatch: {name}")
            physical = sorted(set(options) & PHYSICAL)
            if physical: source_only_options[name] = physical
    references = reference_findings(target)
    if references != manifest.get("references", {}):
        raise MigrationError("Reference findings differ from the source baseline")
    counters = counter_findings(target)
    if counters:
        raise MigrationError("Unsafe counter floors: " + _json(counters))
    receipt = {"verified": True, "export_id": manifest["export_id"], "manifest_sha256": manifest_hash,
            "collections": checked, "documents": sum(checked.values()), "references": references,
            "source_only_options": source_only_options}
    if "_options_sha256" in manifest: receipt["options_sha256"] = manifest["_options_sha256"]
    return receipt


def capture_collection_options(source, directory, output, *, writers_quiesced=False):
    """Bind a separately captured catalog to an immutable, fully rechecked export."""
    from .codec import encode_document
    if not writers_quiesced: raise MigrationError("Catalog capture requires quiesced source writers")
    output = Path(output).resolve()
    if output.exists() or output.is_symlink(): raise MigrationError("Catalog receipt already exists")
    _, manifest, manifest_hash = _load_export(directory)
    live = getattr(source, "_migration_live_source", source)
    if live.name != manifest.get("source", {}).get("database"):
        raise MigrationError("Source database identity differs from the export")
    options = _collection_options(live)
    if set(options) != set(manifest["collections"]): raise MigrationError("Source options inventory mismatch")
    for value in options.values(): _check_options(value)
    # Verify actual typed records, indexes, references, and sequence floors.
    # Matching counts alone cannot prove that a catalog belongs to the export.
    verify_export(directory, live, expected_manifest_hash=manifest_hash)
    if _collection_options(live) != options:
        raise MigrationError("Source collection options changed during catalog capture")
    entries = {}
    for name, value in options.items():
        text, types = encode_document(value)
        entries[name] = {"doc": text, "types": types}
    receipt = {"format": OPTIONS_FORMAT, "export_id": manifest["export_id"],
               "manifest_sha256": manifest_hash, "source": manifest.get("source"),
               "writers_quiesced": True, "collections": entries,
               "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_json(receipt) + "\n"); stream.flush(); os.fsync(stream.fileno())
    except FileExistsError as exc: raise MigrationError("Catalog receipt already exists") from exc
    _sync_directory(output.parent)
    return {"captured": True, "collections": len(entries), "manifest_sha256": manifest_hash,
            "options_sha256": _sha(output)}


def _close(db):
    close = getattr(db, "close", None)
    if callable(close):
        close()
    else:
        db._connection().close()


def copy_with_collection_options(directory, options_path, source_path, target_path):
    """Apply a bound catalog to a verified new SQLite copy, preserving its input."""
    import sqlite3
    from .embedded import Database
    directory, manifest, manifest_hash = _load_export(directory, options_path)
    options_hash = manifest.get("_options_sha256")
    target = Path(target_path).resolve()
    source = Path(source_path).resolve()
    if source == target or not source.is_file():
        raise MigrationError("Catalog copy requires an existing source and separate fresh destination")
    with _migration_lock(target):
        receipt_path = target.with_name(target.name + ".migration.json")
        if target.exists():
            try: previous = json.loads(receipt_path.read_bytes())
            except (OSError, ValueError) as exc: raise MigrationError("Destination already exists without ownership") from exc
            if previous.get("manifest_sha256") != manifest_hash or previous.get("options_sha256") != options_hash:
                raise MigrationError("Destination already belongs to a different catalog migration")
            db = Database(target, name=target.stem)
            try: return verify_export(directory, db, options_path=options_path,
                                      expected_manifest_hash=manifest_hash, expected_options_hash=options_hash)
            finally: _close(db)
        if receipt_path.exists() or any(p.exists() for p in _sidecars(target)):
            raise MigrationError("Destination ownership or recovery file already exists")
        stage = target.with_name(target.name + ".options-copying")
        if stage.exists() or stage.is_symlink() or any(p.exists() for p in _sidecars(stage)):
            raise MigrationError("Catalog copy staging file already exists; choose a fresh destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive reservation and readonly SQLite access keep the existing
        # staged database unchanged, including its catalogs and journal mode.
        with stage.open("xb"): pass
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(stage)
        try: original.backup(destination)
        finally: destination.close(); original.close()
        db = Database(stage, name=target.stem)
        try:
            # Check the copied bytes against the original export before adding
            # any rules. Only this new private file receives catalog updates.
            verify_export(directory, db, expected_manifest_hash=manifest_hash)
            with db.transaction():
                for name, entry in manifest["collections"].items():
                    db.command("collMod", name, **{"validator": {}, "validationLevel": "strict",
                        "validationAction": "error", **_decode_options(entry)})
            receipt = verify_export(directory, db, options_path=options_path,
                                    expected_manifest_hash=manifest_hash, expected_options_hash=options_hash)
            if db.integrity_check() != "ok":
                raise MigrationError("Catalog copy failed SQLite integrity verification")
            checkpoint = db._connection().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
                raise MigrationError("Catalog copy WAL checkpoint could not complete")
        finally: _close(db)
        if any(p.exists() and p.stat().st_size for p in _sidecars(stage) if p.name.endswith(("-wal", "-journal"))):
            raise MigrationError("Catalog copy recovery data remains after checkpoint")
        _sync_file(stage)
        _save(receipt_path, receipt)
        try: os.link(stage, target)
        except FileExistsError as exc: raise MigrationError("Catalog copy destination appeared; it was not overwritten") from exc
        except OSError as exc: raise MigrationError("Could not promote catalog copy without replacement") from exc
        stage.unlink(); _sync_directory(target.parent)
        return receipt


def import_export(directory, target_path, *, batch_size=2000, options_path=None):
    """Idempotently import into an owned staging file, then promote a new file."""
    target = Path(target_path).resolve()
    with _migration_lock(target):
        return _import_export(directory, target, batch_size=batch_size, options_path=options_path)


def _import_export(directory, target_path, *, batch_size=2000, options_path=None):
    from .codec import decode_document
    from .embedded import Database
    directory, manifest, manifest_hash = _load_export(directory, options_path)
    options_hash = manifest.get("_options_sha256")
    for entry in manifest["collections"].values():
        if "options" in entry: _check_options(_decode_options(entry))
    target = Path(target_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = target.with_name(target.name + ".migration.json")
    if receipt_path.exists():
        try:
            owner = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError("Destination receipt already exists without readable ownership") from exc
        if owner.get("manifest_sha256") != manifest_hash or owner.get("options_sha256") != options_hash:
            raise MigrationError("Destination receipt already belongs to a different migration")
    if target.exists():
        try:
            previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError("Destination already exists and is not owned by this migration") from exc
        if previous.get("manifest_sha256") != manifest_hash or previous.get("options_sha256") != options_hash:
            raise MigrationError("Destination already exists for a different migration")
        db = Database(target, name=target.stem)
        try:
            return verify_export(directory, db, expected_manifest_hash=manifest_hash, options_path=options_path,
                                 expected_options_hash=options_hash)
        finally:
            _close(db)
    stage = target.with_name(target.name + ".importing")
    if any(p.exists() for p in _sidecars(target)):
        raise MigrationError("Destination recovery sidecar already exists")
    if stage.is_symlink() or (stage.exists() and stage.stat().st_nlink != 1):
        raise MigrationError("Staging file must be an independently owned regular file")
    owner_path = stage.with_name(stage.name + ".owner.json")
    ownership = {"manifest_sha256": manifest_hash, "target": str(target)}
    if options_hash is not None: ownership["options_sha256"] = options_hash
    if stage.exists() or owner_path.exists():
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError("Staging file already exists without migration ownership") from exc
        if owner != ownership:
            raise MigrationError("Staging file belongs to a different migration")
    else:
        with owner_path.open("x", encoding="utf-8") as stream:
            stream.write(_json(ownership))
            stream.flush()
            os.fsync(stream.fileno())
    db = Database(stage, name=target.stem)
    try:
        for name, entry in manifest["collections"].items():
            if name not in db.list_collection_names():
                db.create_collection(name)
            if "options" in entry:
                # An interrupted import may already have installed a strict
                # validator. Replay original records before restoring rules.
                db.command("collMod", name, validator={}, validationLevel="off")
            batch = []
            for document in _documents(directory / entry["file"]):
                batch.append(document)
                if len(batch) >= max(1, int(batch_size)):
                    with db.transaction(write=True):
                        for doc in batch:
                            db[name].replace_one({"_id": doc["_id"]}, doc, upsert=True)
                    batch.clear()
            if batch:
                with db.transaction(write=True):
                    for doc in batch:
                        db[name].replace_one({"_id": doc["_id"]}, doc, upsert=True)
            for envelope in entry.get("indexes", []):
                index = decode_document(envelope["doc"], envelope["types"])
                keys = list(index.get("key", {}).items())
                if keys == [("_id", 1)]:
                    continue
                options = {k: index[k] for k in ("name", "unique", "partialFilterExpression", "expireAfterSeconds", "sparse") if k in index}
                db[name].create_index(keys, **options)
            for envelope in entry.get("search_indexes", []):
                from .operations import SearchIndexModel
                index = decode_document(envelope["doc"], envelope["types"])
                definition = index.get("latestDefinition", index.get("definition"))
                if definition is not None and index.get("name"):
                    db[name].create_search_index(SearchIndexModel(definition, index["name"], index.get("type", "search")))
            if "options" in entry:
                options = _decode_options(entry)
                # Explicit defaults undo the temporary replay exemption while
                # retaining source options as collection provenance.
                db.command("collMod", name, **{"validator": {}, "validationLevel": "strict",
                                                "validationAction": "error", **options})
        receipt = verify_export(directory, db, expected_manifest_hash=manifest_hash, options_path=options_path,
                                expected_options_hash=options_hash)
        checkpoint = db._connection().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
            raise MigrationError("WAL checkpoint could not complete; staging file was not promoted")
    finally:
        _close(db)
    if any(p.exists() and p.stat().st_size for p in _sidecars(stage) if p.name.endswith(("-wal", "-journal"))):
        raise MigrationError("Staging WAL recovery data remains after checkpoint")
    _sync_file(stage)
    # Save ownership before the final rename so a crash immediately after promotion
    # can be verified and resumed without ever treating an unrelated file as ours.
    _save(receipt_path, receipt)
    if target.exists():
        raise MigrationError("Destination appeared during import; it was not overwritten")
    try:
        os.link(stage, target)
    except FileExistsError as exc:
        raise MigrationError("Destination already exists; it was not overwritten") from exc
    except OSError as exc:
        raise MigrationError("Could not promote verified staging file without replacement") from exc
    stage.unlink()
    _sync_directory(target.parent)
    owner_path.unlink(missing_ok=True)
    return receipt
