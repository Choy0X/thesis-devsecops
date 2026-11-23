"""Durable dataset requests, fixed profiles and an indexed build inventory.

Only explicit creation/retry starts work. Lists read small indexed summaries, not
Parquet files or every historical manifest. Writers keep the inventory current.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from app.backend.pipeline import fsio, versions, util

PROFILE_VERSION = "security-build-profile/5"
STATES = ("queued", "building", "blocked", "failed", "interrupted", "ready", "historical")
_INIT_LOCK = threading.RLock()
_INITIALIZED = set()


def _root(root=None):
    return pathlib.Path(root if root is not None else versions.PROC).resolve()


_now = util.now_utc


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _snapshot_chunks(value):
    """Canonical UTF-8 JSON plus LF, without constructing the complete JSON text."""
    encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    pending, chars = [], 0
    for fragment in encoder.iterencode(value):
        for offset in range(0, len(fragment), 65536):
            piece = fragment[offset:offset + 65536]
            pending.append(piece)
            chars += len(piece)
            if chars >= 65536:
                yield ''.join(pending).encode('utf-8')
                pending, chars = [], 0
    pending.append('\n')
    yield ''.join(pending).encode('utf-8')


def canonical_snapshot_sha256(value):
    digest = hashlib.sha256()
    for chunk in _snapshot_chunks(value):
        digest.update(chunk)
    return digest.hexdigest()


def write_snapshot(path, value):
    """Atomically stream the exact canonical snapshot bytes and return their hash/size."""
    digest, size = hashlib.sha256(), 0
    def writer(part):
        nonlocal size
        with part.open('wb') as stream:
            for chunk in _snapshot_chunks(value):
                stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    fsio.write_with(path, writer)
    return {'sha256': digest.hexdigest(), 'bytes': size}


def read_manifest(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@contextmanager
def _connect(root=None):
    root = _root(root)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / ".build_catalog.sqlite3", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("CREATE TABLE IF NOT EXISTS builds (id TEXT PRIMARY KEY, tag TEXT UNIQUE NOT NULL, profile TEXT, state TEXT NOT NULL, created_at TEXT, number INTEGER NOT NULL, body TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS builds_state ON builds(state,number DESC)")
    conn.commit()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _put(conn, row):
    match = re.fullmatch(r"v(\d+)", row["tag"])
    conn.execute("INSERT INTO builds VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, body=excluded.body",
                 (row["id"], row["tag"], row.get("profile"), row["state"], row.get("created_at"), int(match[1]) if match else -1, _json(row)))


def training_summary(row):
    """Expose data suitability separately from immutable publication/build lifecycle."""
    if not row or not row.get('readiness'):
        return row
    from training.finetune.quality_gate import assess_manifest
    header = dict(row.get('training_manifest') or {})
    header.update(quality_profile=row.get('quality_profile', 'strict'),
                  admission_policy=row.get('admission_policy'), security_readiness=row['readiness'])
    assessment = assess_manifest(header)
    return {**row, 'full_training': assessment,
            'readiness_label': 'Built, insufficient evidence for full training' if not assessment['allowed'] else 'Dataset evidence checks passed'}


def _historical(path, manifest):
    tag = manifest.get("tag") or path.name.removeprefix("dataset_")
    return {"id": "external:" + tag, "tag": tag, "profile": None, "profile_version": None,
            "state": "historical", "status": manifest.get("dataset_status", "unfrozen"),
            "origin": "external", "path": path.as_posix(), "created_at": None, "job_id": None,
            "progress": {}, "blockers": ["historical_build_requires_explicit_readiness_validation"],
            "counts": manifest.get("counts", {}), "tier_mix": manifest.get("tier_mix", {}),
            "versions": manifest.get("versions", {}), "blocked": manifest.get("blocked", {}),
            "quality_profile": manifest.get('quality_profile'), 'admission_policy': manifest.get('admission_policy'),
            'readiness': manifest.get('security_readiness'),
            'training_manifest': {key: manifest.get(key) for key in
                ('dataset_status', 'serialization_protocol', 'counts', 'tokenizer', 'token_audit', 'coverage_policy')}
                if manifest.get('quality_profile') == 'sft' else {},
            "files": {k: v.get("rows") for k, v in (manifest.get("files") or {}).items()}, "source": manifest.get("source", {})}


def ensure_catalog(root=None):
    """One reconciliation per process/root. Thereafter publication uses writer hooks."""
    root = _root(root)
    with _INIT_LOCK:
        if root in _INITIALIZED and (root / ".build_catalog.sqlite3").is_file():
            return
        with _connect(root) as conn:
            known = {r[0] for r in conn.execute("SELECT tag FROM builds")}
            for directory in sorted(root.glob("dataset_*")):
                if not directory.is_dir():
                    continue
                manifest = read_manifest(directory / "manifest.json")
                if manifest and (manifest.get("tag") or directory.name[8:]) not in known:
                    row = _historical(directory, manifest)
                    _put(conn, row)
                    known.add(row["tag"])
            # Old single-file datasets are retained in the same catalogue.
            for name, entry in versions.load_registry(root)["frozen"].items():
                parsed = versions.parse_name(name)
                if not parsed or parsed[0] != "dataset" or f"v{parsed[1]}" in known:
                    continue
                row = _historical(root / name, {"tag": f"v{parsed[1]}", "dataset_status": "frozen"})
                row["files"] = {name: None for name in entry.get("files", {})}
                _put(conn, row)
                known.add(row['tag'])
            for path in sorted(root.glob('dataset_*')):
                parsed = versions.parse_name(path.name)
                if not path.is_file() or not parsed or parsed[0] != 'dataset' or f'v{parsed[1]}' in known:
                    continue
                row = _historical(root / f'dataset_v{parsed[1]}', {'tag': f'v{parsed[1]}'})
                row['files'] = {p.name: None for p in root.glob(f'dataset_v{parsed[1]}.*') if p.is_file()}
                _put(conn, row)
                known.add(row['tag'])
        _INITIALIZED.add(root)


def register_artifact(path, manifest=None, *, root=None):
    """Publication hook for ordinary CLI/campaign builds, including mutable previews."""
    root = _root(root)
    ensure_catalog(root)
    path = pathlib.Path(path).resolve()
    manifest = manifest or read_manifest(path / "manifest.json")
    if not manifest:
        return
    with _connect(root) as conn:
        prior = conn.execute("SELECT body FROM builds WHERE tag=?", (manifest.get("tag", path.name[8:]),)).fetchone()
        if prior and json.loads(prior[0]).get("origin") == "profile":
            return
        _put(conn, _historical(path, manifest))


def register_version(entry, *, root=None):
    """Compatibility hook for frozen single-file datasets written outside the reviewed builder."""
    parsed = versions.parse_name(entry.get('name', ''))
    if not parsed or parsed[0] != 'dataset':
        return
    root = _root(root)
    tag = f'v{parsed[1]}'
    directory = root / ('dataset_' + tag)
    if (directory / 'manifest.json').is_file():
        register_artifact(directory, root=root)
        return
    ensure_catalog(root)
    with _connect(root) as conn:
        prior = conn.execute('SELECT body FROM builds WHERE tag=?', (tag,)).fetchone()
        if prior and json.loads(prior[0]).get('origin') == 'profile':
            return
        row = _historical(directory, {'tag': tag, 'dataset_status': 'frozen'})
        row['files'] = {name: None for name in entry.get('files', {})}
        _put(conn, row)


def get(identity, *, root=None):
    ensure_catalog(root)
    with _connect(root) as conn:
        found = conn.execute("SELECT body FROM builds WHERE id=? OR tag=?", (identity, identity)).fetchone()
    return json.loads(found[0]) if found else None


def update(identity, *, root=None, expected_states=None, **fields):
    ensure_catalog(root)
    with _connect(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = conn.execute("SELECT body FROM builds WHERE id=? OR tag=?", (identity, identity)).fetchone()
        if not found:
            raise KeyError(identity)
        row = json.loads(found[0])
        if expected_states is not None and row['state'] not in expected_states:
            return row
        row.update(fields)
        row["updated_at"] = _now()
        _put(conn, row)
    return row


def list_builds(*, limit=100, offset=0, search="", state="", root=None):
    ensure_catalog(root)
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    where, args = [], []
    if state:
        if state not in STATES:
            raise ValueError("unknown build state")
        where.append("state=?")
        args.append(state)
    if search:
        where.append("(instr(lower(tag),lower(?))>0 OR instr(lower(coalesce(profile,'')),lower(?))>0 OR instr(lower(id),lower(?))>0)")
        args.extend([search] * 3)
    clause = " WHERE " + " AND ".join(where) if where else ""
    with _connect(root) as conn:
        conn.execute('BEGIN')  # The count and page describe the same database snapshot.
        total = conn.execute("SELECT count(*) FROM builds" + clause, args).fetchone()[0]
        found = conn.execute("SELECT body FROM builds" + clause + " ORDER BY number DESC,tag ASC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return {"builds": [training_summary(json.loads(r[0])) for r in found], "total": total, "limit": limit, "offset": offset}


def definitions():
    from app.backend.pipeline import ecosystems, funcsplit
    from app.backend.pipeline.automatic_evidence import CURRENT_POLICY as POLICY
    languages = sorted(({"python"} | set(funcsplit._TS)) - {"tsx"})
    common = {"version": PROFILE_VERSION, "quality_profile": "strict", "min_tier": "auto_checked", "injection_rate": .10,
              "admission_policy": POLICY, "qualification_policy": "qualified-npm/1"}
    return [{**common, "id": "js_ts_security", "label": "JavaScript and TypeScript security", "default": True,
             "description": "Automatically admitted npm JavaScript and TypeScript security evidence.",
             "languages": ["javascript", "typescript"], "ecosystems": ["npm"]},
            {**common, "id": "all_language_security", "label": "All-language security", "default": False,
             "description": "Automatically admitted security evidence across supported collected languages and ecosystems.",
             "languages": languages, "ecosystems": sorted(a.name for a in ecosystems.ADAPTERS)}]


def definition(profile):
    for item in definitions():
        if item["id"] == profile:
            return item
    raise ValueError("unknown dataset build profile")


def _captured_definition(profile):
    """Previously captured profiles resume their exact historical policy."""
    expected = definition(profile['id'])
    if profile.get('version') in ('security-build-profile/3', 'security-build-profile/4'):
        expected.update(version=profile['version'], admission_policy='automatic-evidence/2')
    if profile.get('version') in ('security-build-profile/1', 'security-build-profile/2', 'security-build-profile/3'):
        expected.pop('qualification_policy', None)
    if profile.get('version') == 'security-build-profile/3':
        expected.update(version='security-build-profile/3')
    if profile.get('version') == 'security-build-profile/2':
        expected.update(version='security-build-profile/2', admission_policy='automatic-evidence/1')
    if profile.get('version') == 'security-build-profile/1':
        expected = {key: value for key, value in expected.items() if key != 'admission_policy'}
        expected.update(version='security-build-profile/1', min_tier='human_reviewed', description=(
            'Reviewed npm JavaScript and TypeScript security supervision.' if profile['id'] == 'js_ts_security' else
            'Reviewed security supervision across supported collected languages and ecosystems.'))
    if profile != expected:
        raise ValueError('profile version differs from this worker; retry with the matching version')
    return profile


def capture_source():
    """Copy source rows, decisions, parent assignments and configuration at request time."""
    from app.backend import project_config
    from app.backend.pipeline import build_reviewed, derived, splitmanifest, store
    with derived.connect() as conn, conn.db.transaction(write=False):
        inputs = build_reviewed._load()
        decisions = derived.latest_decisions(conn)
        parent_version = splitmanifest.latest_version(conn)
        parent = splitmanifest.load(conn, parent_version) if parent_version else None
        meta = {k: store.get_meta(k) for k in ("collection_at", "collection_since")}
    from app.backend.pipeline.schemas import reviewed
    receipts, receipt_omissions = verification_receipts({reviewed.record_id_of(p['pair_key']) for p in inputs[2]})
    from .views import rendering
    return {"schema": "dataset-build-source/1", "inputs": inputs, "decisions": decisions,
            "input_rendering": rendering.identity(),
            "verification_receipts": receipts, "verification_receipt_omissions": receipt_omissions,
            "parent": parent, "parent_version": parent_version,
            "meta": meta,
            "project": project_config.run_fields(), "config": project_config.config().model_dump(mode="json"),
            "git": build_reviewed._git()}


def verification_receipts(record_ids, *, root=pathlib.Path('experiments')):
    """Capture only exact-case receipts from managed verifier folders, without extraction or alias guessing."""
    root = pathlib.Path(root).resolve()
    folders = [root / 'verify_pilot']
    if root.is_dir():
        folders.extend(sorted(path for path in root.glob('verify_case*') if path.is_dir()))
    found, omissions = {}, []
    for folder in folders:
        directory = folder / 'receipts'
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob('*.json')):
            relative = path.relative_to(root).as_posix()
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                omissions.append({'path': relative, 'reason': 'linked_receipt_path'})
                continue
            try:
                with path.open('rb') as stream:
                    data = stream.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    omissions.append({'path': relative, 'reason': 'receipt_exceeds_capture_limit'})
                    continue
                document = json.loads(data)
            except (OSError, ValueError) as error:
                omissions.append({'path': relative, 'reason': 'unreadable_receipt', 'detail': str(error)})
                continue
            if isinstance(document, dict) and document.get('case_id') in record_ids:
                found.setdefault(document['case_id'], []).append(document)
    return found, omissions


def select_source(source, profile):
    """Filter units, while retaining all advisories/pairs for conservative leakage grouping."""
    languages = set(profile["languages"])
    ecos = {s.lower() for s in profile["ecosystems"]}
    aliases = {"js": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript"}
    pairs = []
    for pair in source["inputs"][2]:
        language = str(pair.get("language") or "").lower()
        language = aliases.get(language, language)
        if language in languages and str(pair.get("ecosystem") or "").lower() in ecos:
            pairs.append({**pair, "language": language})
    return pairs


def summary(source, profile):
    if source.get('schema') == 'frozen-evidence-source/1':
        count = source['parent_counts']['records'] + sum(item['records'] for item in source.get('candidate_source', {}).get('shards', []))
        return {'source_pairs':count,'units':count,'automatically_eligible':None,'supported':None,
                'protected':None,'insufficient_context':None,'excluded':None}, ([] if count else ['no_supported_source_units'])
    from app.backend.pipeline import build_reviewed
    pairs = select_source(source, profile)
    units, _, _ = build_reviewed._units(pairs)
    blockers = []
    if not units:
        blockers.append("no_supported_source_units")
    # Source metadata cannot prove source-grounded supervision. Only the actual
    # immutable build evaluates admission; unknown counts must not look exact.
    return {"source_pairs": len(pairs), "units": len(units), "automatically_eligible": None,
            "supported": None, "protected": None, "insufficient_context": None, "excluded": None}, blockers


def evidence_state(tag, *, root=None):
    """Read the current evidence controls without loading the captured raw source."""
    from app.backend.pipeline import evidence_recovery
    return evidence_recovery.catalog_state(tag, root=root)


def _evidence_fingerprint(*, root=None):
    from app.backend.pipeline import evidence_recovery
    return evidence_recovery.evidence_fingerprint(root=root)


def _retry_fingerprint(source_sha256, profile, evidence):
    return hashlib.sha256(_json({'source': source_sha256, 'profile': profile, 'evidence': evidence}).encode()).hexdigest()


def _legacy_context_provenance(row, *, root=None):
    """The audited v7 exception binds the original source to registered bytes."""
    from app.backend.pipeline.build_context_cache import LEGACY_V7_MANIFEST_SHA256
    if row.get('tag') != 'v7':
        return None
    root = _root(root)
    entry = versions.load_registry(root)['frozen'].get('dataset_v7') or {}
    expected = (entry.get('files') or {}).get('manifest.json')
    path = root / 'dataset_v7' / 'manifest.json'
    if (entry.get('base') != 'dataset_v7' or expected != LEGACY_V7_MANIFEST_SHA256
            or not path.is_file() or versions._sha256(path) != expected):
        return None
    manifest = read_manifest(path)
    original_sha = row.get('source', {}).get('sha256')
    if not original_sha or manifest.get('source', {}).get('snapshot_sha256') != original_sha:
        return None
    return {'tag': 'v7', 'source_sha256': original_sha, 'manifest_sha256': expected}


def profile_metadata():
    """Read only unit identity/hash metadata. Never fetch code, contexts or review fields."""
    from app.backend.pipeline import derived, splitmanifest
    projection = {key: 1 for key in ('pair_key', 'commit_key', 'unit.file', 'unit.granularity', 'unit.language',
                                     'subject.ecosystem', 'extraction.content_hash', 'overrides.ecosystem', 'overrides.language')}
    pairs = []
    with derived.connect() as conn, conn.db.transaction(write=False):
        for doc in conn.db['code_pairs'].find({'flags.available': True}, projection):
            unit, subject = doc.get('unit', {}), doc.get('subject', {})
            digest = doc.get('extraction', {}).get('content_hash')
            pairs.append({'pair_key': doc['pair_key'], 'commit_key': doc.get('commit_key'), 'file_path': unit.get('file'),
                          'granularity': unit.get('granularity'), 'language': doc.get('overrides', {}).get('language', unit.get('language')),
                          'ecosystem': doc.get('overrides', {}).get('ecosystem', subject.get('ecosystem')),
                          'content_hash': digest or ('unhashed:' + doc['pair_key']), 'code_before': '',
                          '_count_hash_missing': not bool(digest)})
        parent_version = splitmanifest.latest_version(conn)
    return {'inputs': [[], {}, pairs, {}], 'decisions': {}, 'parent_version': parent_version}


def profiles():
    source = profile_metadata()
    out = []
    for profile in definitions():
        counts, blockers = summary(source, profile)
        estimated = True  # Admission and deduplication require the captured source/context.
        out.append({**profile, "counts": counts, "blockers": blockers, 'counts_estimated': estimated, 'readiness_evaluated': False})
    return {"profiles": out, "default_profile": "js_ts_security"}


def snapshot(identity, *, root=None):
    row = get(identity, root=root)
    if not row or row.get("origin") != "profile":
        raise ValueError("this build has no captured profile source")
    path = _root(root) / ".build_requests" / row["id"] / "source.json"
    try:
        with path.open('rb') as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            if digest.hexdigest() != row['source']['sha256']:
                raise ValueError('captured build source hash mismatch')
            stream.seek(0)
            # pydantic-core is an existing application dependency. Parse UTF-8
            # directly so one astral character cannot widen the entire source
            # into a four-byte-per-character Python string. The source object
            # and one binary buffer still coexist; this is not a streaming parser.
            from pydantic_core import from_json
            return from_json(stream.read(), allow_inf_nan=True, cache_strings='keys', allow_partial=False)
    except OSError as exc:
        raise ValueError('captured source is unavailable; create a new profile build') from exc


def create(profile, *, root=None, start_job=None, captured=None, retry_of=None):
    profile = definition(profile)  # Reject unknown identities before reading the working store.
    root = _root(root)
    ensure_catalog(root)
    build_id = uuid.uuid4().hex
    preflight = captured if captured is not None else profile_metadata()
    parent = re.fullmatch(r"split_v(\d+)", preflight.get("parent_version") or "")
    tag = versions.reserve_version("dataset", root, owner=build_id, minimum=int(parent[1]) + 1 if parent else 1)
    row = {"id": build_id, "tag": tag, "profile": profile["id"], "profile_version": profile["version"],
           "profile_snapshot": profile, "admission_policy": profile.get('admission_policy'), "origin": "profile", "state": "queued",
           "status": "unfrozen", "created_at": _now(), "job_id": None,
           "progress": {'state': 'snapshot', 'label': 'Capturing source evidence'},
           "blockers": [], "counts": {}, "source": {'state': 'capturing'}, "path": (root / f"dataset_{tag}").as_posix()}
    if retry_of:
        row["retry_of"] = retry_of
    with _connect(root) as conn:
        _put(conn, row)
    try:
        source = capture_source() if captured is None else captured
        if captured is None:
            from app.backend.pipeline import evidence_recovery
            source = evidence_recovery.refresh_source(source, root=root)
        counts, blockers = summary(source, profile)
        written = write_snapshot(root / '.build_requests' / build_id / 'source.json', source)
        evidence = _evidence_fingerprint(root=root)
        row = update(build_id, root=root, state='blocked' if blockers else 'queued', counts=counts, blockers=blockers,
                     source={'sha256': written['sha256'], 'bytes': written['bytes'], 'schema': source.get('schema', 'dataset-build-source/1')},
                     evidence_fingerprint=evidence,
                     retry_fingerprint=_retry_fingerprint(written['sha256'], profile, evidence),
                     progress={'state': 'blocked' if blockers else 'queued', 'label': 'Build blocked' if blockers else 'Waiting for build worker'})
    except Exception as exc:
        return update(build_id, root=root, state='failed', error=str(exc), blockers=['source_snapshot_failed'])
    if not blockers:
        if start_job is None:
            from app.backend import jobs
            start_job = jobs.start
        try:
            job = start_job("build_reviewed_dataset", {"build_id": build_id, "build_root": str(root)})
            row = update(build_id, root=root, job_id=job.id)
        except Exception as exc:
            row = update(build_id, root=root, state="failed", blockers=["job_start_failed"], error=str(exc))
    return row


def retry(identity, *, root=None, start_job=None):
    from app.backend.pipeline import oslock
    lock = oslock.FileLock(_root(root) / '.build_requests' / '.retry.lock', note=False)
    lock.acquire(wait=30)
    try:
        return _retry_locked(identity, root=root, start_job=start_job)
    finally:
        lock.release()


def _retry_locked(identity, *, root=None, start_job=None):
    row = get(identity, root=root)
    if not row or row.get("origin") != "profile":
        raise ValueError("only a captured profile build can be retried")
    if row["state"] not in ("blocked", "failed", "interrupted"):
        raise ValueError("only a blocked, failed or interrupted build can be retried")
    profile = definition(row['profile'])
    evidence = _evidence_fingerprint(root=root)
    fingerprint = _retry_fingerprint(row['source']['sha256'], profile, evidence)
    if row['state'] == 'blocked' and row.get('retry_fingerprint') == fingerprint:
        raise ValueError('build evidence and policy are unchanged; repair or verify evidence before rebuilding')
    if row['state'] == 'blocked' and row.get('profile_snapshot') == profile and not row.get('retry_fingerprint'):
        raise ValueError('build evidence is unchanged; repair or verify evidence before rebuilding')
    with _connect(root) as conn:
        repeated = [json.loads(item[0]) for item in conn.execute("SELECT body FROM builds WHERE profile=?", (row['profile'],))]
    if row['state'] == 'blocked' and any((item.get('retry_fingerprint') == fingerprint or item.get('retry_basis_fingerprint') == fingerprint)
                                       and item['id'] != row['id'] for item in repeated):
        raise ValueError('an unchanged evidence and policy rebuild already exists')
    if row.get('status') == 'frozen':
        from app.backend.pipeline import frozen_evidence_build
        captured = frozen_evidence_build.capture(row['tag'],root=root)
        created = create(row['profile'],root=root,captured=captured,start_job=start_job,retry_of=row['id'])
        return update(created['id'],root=root,retry_basis_fingerprint=fingerprint)
    captured = snapshot(row['id'], root=root)
    provenance = _legacy_context_provenance(row, root=root)
    if provenance:
        captured = {**captured, 'context_cache_provenance': provenance}
    from app.backend.pipeline import evidence_recovery
    if hasattr(evidence_recovery, 'refresh_source') and (row.get('evidence_fingerprint') != evidence or row.get('profile_snapshot') != profile):
        captured = evidence_recovery.refresh_source(captured, root=root)
    created = create(row["profile"], root=root, captured=captured, start_job=start_job, retry_of=row["id"])
    return update(created['id'], root=root, retry_basis_fingerprint=fingerprint)


def sync_job(job):
    params = job.get("params") or {}
    build_id = params.get("build_id")
    if not build_id:
        return
    root = params.get("build_root")
    row = get(build_id, root=root)
    if not row:
        return
    if row.get('job_id') and row['job_id'] != job.get('id'):
        return
    status = job.get("status")
    state = row["state"]
    if state in ("ready", "blocked", "failed", "interrupted"):
        return
    if status == "running":
        state = "building"
    elif status in ("cancelled", "interrupted"):
        state = "interrupted"
    elif status in ("done", "error"):
        # execute() sets ready/blocked only after successful, verified publication.
        state = "failed"
    fields = {"state": state, "job_id": job.get("id"), "progress": job.get("progress") or row.get("progress", {})}
    if job.get("error"):
        fields["error"] = job["error"]
    if status == "done":
        fields["blockers"] = ["job_finished_without_dataset_publication"]
    update(build_id, root=root, expected_states=('queued', 'building'), **fields)


def recover_requests(*, root=None):
    """Called by API startup after persisted jobs load. Never runs in a worker or starts work."""
    from app.backend import jobs
    root = _root(root)
    if not (root / '.build_catalog.sqlite3').is_file():
        return
    ensure_catalog(root)
    with _connect(root) as conn:
        pending = [json.loads(r[0]) for r in conn.execute("SELECT body FROM builds WHERE state IN ('queued','building')")]
    for row in pending:
        job = jobs.get(row.get('job_id')) if row.get('job_id') else None
        if job and job.status == 'running':
            continue
        update(row['id'], root=root, expected_states=('queued', 'building'), state='interrupted', error='The app stopped before this build completed.',
               blockers=['build_interrupted_requires_explicit_retry'])


def execute(identity, *, root=None, progress=None, publication_guard=None):
    """Run one captured request. The builder publishes atomically to its reserved tag."""
    from app.backend.pipeline import oslock
    row = get(identity, root=root)
    if not row or row.get('origin') != 'profile':
        raise ValueError('unknown profile build request')
    lock = oslock.FileLock(_root(root) / '.build_requests' / row['id'] / '.execute.lock', note=False)
    lock.acquire(wait=0)
    try:
        return _execute_locked(identity, root=root, progress=progress, publication_guard=publication_guard)
    finally:
        lock.release()


def resume_owned(identity, *, root=None):
    """Restart an interrupted frozen-source build in its existing reservation.

    Partial output is archived, never deleted. Published or unrecognized output
    cannot be replaced, and a changed captured source requires a new request.
    """
    from . import frozen_evidence_build, oslock
    root = _root(root)
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise ValueError('invalid managed build identity')
    workspace = root / '.build_requests' / identity
    if not workspace.resolve().is_relative_to(root.resolve()) or workspace.is_symlink():
        raise ValueError('build workspace escapes managed storage')
    with oslock.FileLock(workspace / '.execute.lock', note=False):
        row = get(identity, root=root)
        if not row or row.get('origin') != 'profile' or row['state'] not in ('failed', 'interrupted'):
            raise ValueError('build is not an interrupted owned request')
        if (root / ('dataset_' + row['tag'])).exists():
            raise ValueError('existing publication requires receipt reconciliation before resume')
        claim = read_manifest(root / ('.dataset_' + row['tag'] + '.reserved'))
        if claim.get('owner') != identity:
            raise ValueError('build reservation ownership differs')
        source = snapshot(identity, root=root)
        if source.get('schema') != 'frozen-evidence-source/1':
            raise ValueError('owned resume requires a bounded frozen-source build')
        frozen_evidence_build._validated_parent(source, root)
        partial = workspace / 'artifact'
        if partial.exists():
            if partial.is_symlink() or not partial.resolve().is_relative_to(workspace.resolve()):
                raise ValueError('partial build artifact escapes owned storage')
            partial.rename(workspace / ('interrupted-artifact-' + uuid.uuid4().hex))
        return update(identity, root=root, expected_states=('failed', 'interrupted'), state='queued', error=None,
                      blockers=[], progress={'state': 'queued', 'label': 'Resuming the same dataset reservation'})


def _execute_locked(identity, *, root=None, progress=None, publication_guard=None):
    from app.backend.pipeline import build_reviewed
    row = get(identity, root=root)
    if not row or row["state"] not in ("queued", "building"):
        raise ValueError("build request is not queued")
    update(identity, root=root, expected_states=('queued', 'building'), state="building")
    try:
        source = snapshot(identity, root=root)
        profile = _captured_definition(row["profile_snapshot"])
        if source.get('schema') == 'frozen-evidence-source/1':
            from app.backend.pipeline import frozen_evidence_build
            out = frozen_evidence_build.build(source,root=_root(root),reserved_tag=row['tag'],owner=row['id'],
                                             profile=profile,progress=progress,publication_guard=publication_guard)
        else:
            out = build_reviewed.build(freeze=True, root=_root(root), reserved_tag=row["tag"], reservation_owner=row["id"],
                                       snapshot=source, profile=profile, min_tier=profile['min_tier'], quality_profile="strict",
                                       injection_rate=.1, progress=progress,publication_guard=publication_guard)
        manifest = read_manifest(pathlib.Path(out["path"]) / "manifest.json")
        readiness = manifest.get("security_readiness") or {}
        if not manifest or manifest.get("dataset_status") != "frozen":
            raise ValueError("build returned without a published frozen manifest")
        verify_publication(row, manifest, root=root)
        from training.finetune.quality_gate import assess_manifest
        full_training = assess_manifest(manifest)
        state = 'ready' if full_training['allowed'] else 'blocked'
        return update(identity, root=root, expected_states=('queued', 'building'), state=state, status="frozen", counts=manifest.get("counts", {}),
                      tier_mix=manifest.get("tier_mix", {}), blocked=manifest.get('blocked') or {},
                      versions=manifest.get('versions') or {},
                      files={name: info.get('rows') for name, info in (manifest.get('files') or {}).items()},
                      blockers=full_training['blockers'], full_training=full_training,
                      readiness=readiness, admission_policy=manifest.get('admission_policy'), completed_at=_now(),
                      progress={'state': 'done', 'label': 'Dataset built' if state == 'ready' else 'Dataset built with readiness blockers',
                                'processed': manifest.get('counts', {}).get('records', 0), 'total': manifest.get('counts', {}).get('records', 0)})
    except InterruptedError as exc:
        update(identity, root=root, expected_states=('queued', 'building'), state='interrupted', error=str(exc),
               blockers=['build_interrupted_before_publication'])
        raise
    except Exception as exc:
        update(identity, root=root, expected_states=('queued', 'building'), state="failed", error=str(exc), blockers=["build_failed"])
        raise


def verify_publication(row, manifest, *, root=None):
    """Readiness is meaningful only for the completed, hash-registered artifact."""
    root = _root(root)
    directory = root / f"dataset_{row['tag']}"
    entry = versions.load_registry(root)["frozen"].get("dataset_" + row["tag"], {})
    if not entry and directory.exists():
        # Reconcile only an exact backend-owned write-ahead publication. Never
        # adopt a directory merely because its name looks like a dataset.
        if (manifest.get('tag') != row['tag'] or manifest.get('build_profile') != row['profile_snapshot'] or
                manifest.get('source', {}).get('snapshot_sha256') != row['source']['sha256']):
            raise ValueError('interrupted publication source/profile mismatch')
        versions.recover_publication(row['tag'], root, owner=row['id'])
        entry = versions.load_registry(root)['frozen'].get('dataset_' + row['tag'], {})
    required = {"manifest.json", "records.parquet", "views.parquet", "edges.parquet", "groups.parquet",
                "stats.json", "split_manifest.json", "security_readiness.json"}
    files = entry.get("files", {})
    if not required <= set(files) or entry.get("base") != directory.name:
        raise ValueError("dataset publication is incomplete or unregistered")
    for name, expected in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file() or versions._sha256(path) != expected:
            raise ValueError("dataset publication hash mismatch: " + name)
    if manifest.get("tag") != row["tag"] or manifest.get("build_profile") != row["profile_snapshot"]:
        raise ValueError("dataset publication profile/version mismatch")
    policy = row['profile_snapshot'].get('admission_policy')
    schema = manifest.get('security_readiness', {}).get('schema')
    expected_schemas = ('security-readiness/4',) if row['profile_snapshot'].get('qualification_policy') else (
        ('security-readiness/3', 'security-readiness/4') if policy in ('automatic-evidence/2', 'automatic-evidence/3') else ('security-readiness/2',))
    if policy and (manifest.get('admission_policy') != policy or
                   manifest.get('security_readiness', {}).get('admission_policy') != policy or
                   schema not in expected_schemas):
        raise ValueError('dataset publication admission policy/readiness mismatch')
    if manifest.get("source", {}).get("snapshot_sha256") != row["source"]["sha256"]:
        raise ValueError("dataset publication source mismatch")
    if read_manifest(directory / "security_readiness.json") != manifest.get("security_readiness"):
        raise ValueError("dataset publication readiness report mismatch")
