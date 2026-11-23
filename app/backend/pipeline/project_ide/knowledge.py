"""Offline primary advisory retrieval, with explicit provenance and omissions.

This deliberately does not load the training RAG index or initialize storage,
embeddings, models, network clients, or a project-derived knowledge collection.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from . import workspace

MAX_SOURCES = 32
MAX_ROWS = 10000
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_PARQUET_BYTES = 256 * 1024 * 1024
FIELDS = {"osv_id", "cve_id", "summary", "details", "nvd_description", "osv_cwe", "cwe", "package",
          "ecosystem", "source_urls", "fix_commits", "source_record_hash", "modified", "published", "withdrawn"}
PRIMARY_HOSTS = {"api.osv.dev", "services.nvd.nist.gov"}


def _safe(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Knowledge source escapes its configured local cache.")
    for parent in (path, *path.parents):
        if parent.exists() and workspace.linked(parent):
            raise ValueError("Linked knowledge sources are excluded.")
    return path


def _primary_url(value):
    try:
        part = urlsplit(str(value))
        return part.scheme == "https" and part.hostname in PRIMARY_HOSTS and not part.username and not part.password and part.port in (None, 443)
    except ValueError:
        return False


def _https(value):
    try:
        part = urlsplit(value)
        return part.scheme == "https" and bool(part.hostname) and not part.username and not part.password
    except ValueError:
        return False


def _strings(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, (list, dict)):
                return _strings(decoded)
        except ValueError:
            pass
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _record(row, source):
    identifier = row.get("id") or row.get("osv_id") or row.get("cve_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,150}", identifier):
        return None
    # Only source fields are repeated. No model-generated explanation or fix.
    text = "\n".join(str(row.get(k) or "") for k in ("summary", "details", "nvd_description"))[:12000]
    cwes = sorted(set(re.findall(r"CWE-\d+", " ".join(_strings(row.get("cwe")) + _strings(row.get("osv_cwe")) + _strings(row.get("database_specific"))))))
    refs = row.get("references", row.get("source_urls", []))
    fixes = _strings(row.get("fix_commits"))
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, dict) and ref.get("type") == "FIX" and isinstance(ref.get("url"), str):
            fixes.append(ref["url"])
    fixes = [url for url in fixes if _https(url)][:20]
    canonical = json.dumps(row, sort_keys=True, default=str, ensure_ascii=True).encode()
    return {"id": identifier, "document": text, "cwe_ids": cwes,
            "fix_references": fixes, "repair_validation": "not_verified",
            "withdrawn": row.get("withdrawn"), "package": row.get("package"), "ecosystem": row.get("ecosystem"),
            "source": source, "sha256": hashlib.sha256(canonical).hexdigest(),
            "source_record_hash": row.get("source_record_hash"), "modified": row.get("modified"),
            "record_kind": "primary_advisory", "content_trust": "untrusted_reference_data"}


class LocalKnowledge:
    def __init__(self, root: Path, cache_root=None, interim_root=None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("An existing managed workspace is required.")
        self.cache = Path(cache_root).absolute() if cache_root is not None else None
        if interim_root is None:
            from app.backend import paths
            interim_root = paths.INTERIM
        self.interim = Path(interim_root).absolute()

    def retrieve(self, query: str, limit: int = 5) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000 or "\x00" in query:
            raise ValueError("A nonempty knowledge query of at most 1000 characters is required.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Knowledge result limit must be between 1 and 20.")
        tokens = set(re.findall(r"[a-z0-9][a-z0-9_.-]*", query.lower()))
        if not tokens:
            raise ValueError("The knowledge query must contain a searchable term.")
        sources, matches, omitted, seen = [], [], [], set()
        candidates = []
        if self.cache is not None:
            snapshots = _safe(self.cache / "snapshots", self.cache)
            if snapshots.is_dir():
                candidates.extend((p, self.cache, "snapshot") for p in sorted(snapshots.glob("*.json")))
        if self.interim.is_dir():
            candidates.extend((p, self.interim, "parquet") for p in sorted(self.interim.glob("osv_*.parquet")))
            if (self.interim / "nvd.parquet").exists():
                candidates.append((self.interim / "nvd.parquet", self.interim, "parquet"))
        rows_read, truncated = 0, len(candidates) > MAX_SOURCES
        for path, boundary, kind in candidates[:MAX_SOURCES]:
            if rows_read >= MAX_ROWS:
                truncated = True
                break
            try:
                path = _safe(path, boundary)
                maximum = MAX_SNAPSHOT_BYTES if kind == "snapshot" else MAX_PARQUET_BYTES
                before = path.stat()
                if before.st_size > maximum:
                    omitted.append({"path": str(path), "reason": "source_byte_limit"})
                    truncated = True
                    continue
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    hashed = 0
                    for chunk in iter(lambda: stream.read(min(1024 * 1024, maximum + 1 - hashed)), b""):
                        hashed += len(chunk)
                        if hashed > maximum:
                            raise ValueError("Knowledge source exceeded its byte limit during read.")
                        hasher.update(chunk)
                source = {"path": str(path), "sha256": hasher.hexdigest(), "kind": kind,
                          "snapshot_age_seconds": max(0, time.time() - before.st_mtime),
                          "age_basis": "local_file_mtime", "freshness": "offline_snapshot",
                          "workspace": str(self.root)}
                if kind == "snapshot":
                    raw = path.read_bytes()
                    envelope = json.loads(raw)
                    if not isinstance(envelope, dict) or not _primary_url(envelope.get("source")):
                        omitted.append({"path": str(path), "reason": "not_primary_advisory_snapshot"})
                        continue
                    source["url"] = envelope["source"]
                    fetched = envelope.get("fetched_at")
                    if isinstance(fetched, (int, float)):
                        source.update(snapshot_age_seconds=max(0, time.time() - fetched), age_basis="fetched_at", fetched_at=fetched)
                    rows = self._snapshot_records(envelope.get("data"))
                else:
                    source["url"] = "https://nvd.nist.gov/" if path.name == "nvd.parquet" else "https://osv.dev/"
                    rows = self._parquet_records(path, MAX_ROWS - rows_read)
                sources.append(source)
                for row in rows:
                    if rows_read >= MAX_ROWS:
                        truncated = True
                        break
                    rows_read += 1
                    if not isinstance(row, dict):
                        continue
                    record = _record(row, source)
                    if record is None:
                        continue
                    haystack = (record["id"] + " " + record["document"] + " " + " ".join(record["cwe_ids"]) + " " + str(record["package"] or "")).lower()
                    score = sum(token in haystack for token in tokens)
                    if score and (record["id"], record["sha256"]) not in seen:
                        record["score"] = score
                        matches.append(record)
                        seen.add((record["id"], record["sha256"]))
                        # Retrieval memory remains bounded independently of corpus size.
                        matches.sort(key=lambda r: (-r["score"], r["id"], r["sha256"]))
                        del matches[limit:]
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    matches = [m for m in matches if m["source"] is not source]
                    sources.remove(source)
                    omitted.append({"path": str(path), "reason": "source_changed_during_read"})
            except (OSError, ValueError, TypeError, ImportError, KeyError) as exc:
                omitted.append({"path": str(path), "reason": type(exc).__name__ + ": " + str(exc)[:180]})
        return {"workspace": str(self.root), "query": query, "status": "available" if matches else "unavailable",
                "reason": None if matches else "No matching primary advisory evidence was available in the bounded local sources.",
                "results": matches, "sources": sources, "omissions": omitted,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "coverage": {"offline_only": True, "network_queried": False, "records_read": rows_read,
                             "truncated": truncated, "max_sources": MAX_SOURCES, "max_records": MAX_ROWS,
                             "cwe_catalog": "unavailable; advisory CWE associations only",
                             "repair_patterns": "unavailable; recorded fix references only",
                             "advisory_applicability": "not_checked", "execution_verified": False}}

    @staticmethod
    def _snapshot_records(data):
        if not isinstance(data, dict):
            return []
        if isinstance(data.get("vulns"), list):
            return data["vulns"]
        if isinstance(data.get("vulnerabilities"), list):
            result = []
            for entry in data["vulnerabilities"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("cve"), dict):
                    continue
                cve = entry["cve"]
                result.append({"id": cve.get("id"), "details": "\n".join(d.get("value", "") for d in cve.get("descriptions", []) if isinstance(d, dict) and d.get("lang") == "en"),
                               "cwe": _strings(cve.get("weaknesses")), "references": cve.get("references", []), "modified": cve.get("lastModified")})
            return result
        return [data] if data.get("id") else []

    @staticmethod
    def _parquet_records(path, remaining):
        import pyarrow.parquet as pq
        source = pq.ParquetFile(path)
        columns = sorted(FIELDS & set(source.schema_arrow.names))
        if not columns:
            return
        # One extra row signals a row budget hit without reading an unbounded file.
        read = 0
        for batch in source.iter_batches(batch_size=128, columns=columns):
            for row in batch.to_pylist():
                yield row
                read += 1
                if read > remaining:
                    return


def retrieve(root: Path, query: str, limit: int = 5, *, cache_root=None, interim_root=None) -> dict:
    return LocalKnowledge(root, cache_root, interim_root).retrieve(query, limit)
