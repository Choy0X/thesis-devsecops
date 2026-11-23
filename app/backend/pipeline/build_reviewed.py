"""The reviewed-dataset build: working store -> a versioned folder of records, groups, views and a manifest.

Every unique mined pair becomes a candidate record (``reviewed-record/1``): nothing is dropped to control volume,
weight is controlled later by tier, family and task. The build

1. takes each function or hunk pair (a file pair only when it has none) and keeps one per identical content;
2. builds the leakage graph and the split manifest, so every record carries its issue group and split;
3. builds the local context of every unit (``deterministic_local_snapshot_v1``);
4. decides each record's tier: the deterministic gate (``review.gate``) with all its inputs, then the latest
   review decision; unreviewed rows keep weight 0;
5. makes the subjects (vulnerable side, fixed twin) and the views T1 to T7 for the eligible ones;
6. checks the pools (an empty core task fails the build) and only then writes ``records.parquet``,
   ``views.parquet``, ``edges.parquet``, ``groups.parquet``, ``split_manifest.json``, ``stats.json`` (with the
   coverage table) and ``manifest.json``, which hashes the others.

``dataset_preview/`` is mutable; a freeze takes the next unused version (``dataset_v<N>``) and registers its hashes, after which
it is not written again by the builder. The output has no timestamps:
the same data and code give the same bytes.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import uuid
from collections import Counter, defaultdict

from app.backend import compute, project_config
from app.backend.pipeline import (codestore, context_builder, derived, ecosystems, leakage, negatives, review,
                                  splitmanifest as sm, versions)
from app.backend.pipeline import views as vw
from app.backend.pipeline import fsio
from app.backend.pipeline import build_common
from app.backend.pipeline.schemas import reviewed
from app.backend.pipeline.views import catalog, pools, rendering
from training.finetune import serialization as sz
from app.backend.pipeline.db import pipeline_mongo as _pipeline_mongo

VERSION = "build-reviewed-3"
MANIFEST_SCHEMA = "dataset-manifest/1"
FILES = ("records.parquet", "views.parquet", "edges.parquet", "groups.parquet", "split_manifest.json", "stats.json")
AUGMENTATION_POLICY = "harmless_variants_v1"
MIN_PARALLEL_FILES = 8              # fewer files than this are done in this process: starting workers would cost more than it saves
_RECORD_COLS = ("key, osv_id, cve_id, ecosystem, package, aliases, fix_commits, osv_cwe, cwe, cvss_vector, "
                "cvss_v4_vector, cvss_score, published, modified, source_urls, source_record_hash")
_VIEW_SCHEMA = [("view_id", "string"), ("task", "string"), ("subject_id", "string"), ("record_id", "string"),
                ("issue_group_id", "string"), ("family_id", "string"), ("pool", "string"), ("split", "string"),
                ("val_role", "string"), ("weight", "float64"), ("review_status", "string"), ("eligible", "bool"),
                ("context_hash", "string"), ("flags_json", "string"), ("lineage_json", "string"),
                ("context_text", "string"), ("response_json", "string"), ("prompt_chars", "int64"),
                ("completion_chars", "int64")]
_EDGE_SCHEMA = [("src", "string"), ("dst", "string"), ("type", "string"), ("confidence", "float64"),
                ("evidence_json", "string"), ("review_only", "bool")]
_GROUP_SCHEMA = [("node_id", "string"), ("group_id", "string"), ("split", "string"), ("role", "string"),
                 ("fold", "int64"), ("reason", "string"), ("group_size", "int64")]


def _noop(**_):
    return None


# -- reading the store ---------------------------------------------------------------------------------------------------------

def _units(pairs: list[dict]):
    """(units, whole-file texts, duplicates): a function or hunk pair, or a file pair that has none; one unit per
    identical content (the others are duplicates, whose advisories are merged into the kept one)."""
    by_file: dict[tuple, list[dict]] = defaultdict(list)
    for p in pairs:
        by_file[(p.get("commit_key") or p["pair_key"], p.get("file_path"))].append(p)
    chosen, files = [], {}
    for key in sorted(by_file, key=lambda k: (str(k[0]), str(k[1]))):
        rows = by_file[key]
        for r in rows:
            if r["granularity"] == "file":
                files[key] = r["code_before"]
        subs = [r for r in rows if r["granularity"] in ("function", "hunk")]
        chosen += subs if subs else [r for r in rows if r["granularity"] == "file"]
    seen: dict[str, str] = {}
    units, duplicates = [], {}
    rank = {"function": 0, "hunk": 1, "file": 2}                                  # the more precise unit is the one kept
    for u in sorted(chosen, key=lambda r: (rank.get(r["granularity"], 3), r["pair_key"])):
        h = u.get("content_hash") or hashlib.sha1(f"{u.get('code_before')}\0{u.get('code_after')}".encode()).hexdigest()
        if h in seen:
            duplicates[u["pair_key"]] = seen[h]
            continue
        seen[h] = u["pair_key"]
        units.append(u)
    return sorted(units, key=lambda r: r["pair_key"]), files, duplicates


# -- small derivations --------------------------------------------------------------------------------------------------------------

def _list(x) -> list[str]:
    if x is None:
        return []
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            return [x] if x else []
    return [str(i) for i in (x if isinstance(x, list) else [x]) if i not in (None, "")]


def _urls(raw) -> list[str]:
    out = []
    try:
        for item in json.loads(raw or "[]"):
            url = item.get("url") if isinstance(item, dict) else item
            if isinstance(url, str) and url:
                out.append(url)
    except ValueError:
        pass
    return sorted(set(out))


def _cwes(rec: dict | None, pair: dict) -> tuple[list[str], str]:
    for text, src in (((rec or {}).get("osv_cwe"), "osv"), ((rec or {}).get("cwe"), "nvd"), (pair.get("cwe"), "nvd")):
        found = sorted(set(re.findall(r"CWE-\d+", str(text or ""))))
        if found:
            return found, src
    return [], "missing"


def _severity(rec: dict | None, pair: dict) -> tuple[float | None, str | None, str | None, str | None]:
    """(score, label, source, CVSS version): kept apart from discovery, never its ground truth."""
    v3, v4 = (rec or {}).get("cvss_vector") or pair.get("cvss_vector"), (rec or {}).get("cvss_v4_vector")
    score, source = build_common.severity_score(v3=v3, v4=v4, record=(rec or {}).get("cvss_score"))
    if source is None:
        return None, None, None, None
    m = re.match(r"CVSS:(\d(?:\.\d)?)", str(v3 if source == "v3" else v4)) if source in ("v3", "v4") else None
    return score, build_common.label(score), build_common.CODE_SOURCES[source], m.group(1) if m else None


def _year(*dates) -> str:
    for d in dates:
        if isinstance(d, str) and re.match(r"\d{4}", d):
            return d[:4]
    return "unknown"


def _node_ids(u: dict, commit_nodes: dict, cve_node: dict) -> list[str]:
    nodes = commit_nodes.get(u.get("commit_key")) or ([cve_node[u["cve_id"]]] if u.get("cve_id") in cve_node else [])
    return sorted(set(nodes)) or [f"pair:{u['pair_key']}"]


def _git() -> dict:
    try:
        root = pathlib.Path(__file__).resolve().parents[3]
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=15).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture_output=True,
                               text=True, timeout=15).stdout.strip()
        return {"commit": head or None, "dirty": bool(dirty) if head else None}
    except Exception:  # ignored: git may be missing; the commit is recorded as unknown
        return {"commit": None, "dirty": None}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: pathlib.Path, data: bytes) -> None:
    fsio.write_bytes(path, data)


def _json_bytes(obj, indent=None) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=indent, ensure_ascii=False, separators=None if indent else (",", ":"))
            + "\n").encode("utf-8")


def _table_bytes(rows: list[dict], schema_spec) -> bytes:
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq
    types = {"string": pa.string(), "float64": pa.float64(), "bool": pa.bool_(), "int64": pa.int64()}
    schema = pa.schema([(n, types[t]) for n, t in schema_spec])
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), buf)
    return buf.getvalue()


# -- the CPU-heavy part of a record, computable in a worker process -------------------------------------------------------------------------

def _derive_unit(file_text, task):
    """The local context, the hidden-fact check and the evidence lines of one unit. A pure function of its arguments."""
    unit_args, probe_args, explanation, draft, before, after, row, after_text = task[:8]
    from app.backend.pipeline import security_snapshots
    pair = task[8] if len(task) > 8 else security_snapshots.build_pair(row, before_text=file_text, after_text=after_text)
    bundle = pair['before']
    summary_text = vw.first_sentences(explanation, 400) or None
    text, _ = vw.target_summary(draft)
    hidden = (vw.hidden_facts(text, vw.build_input("discovery", negatives.Subject(**probe_args), [i.as_dict() for i in bundle.items]).context_text,
                              ignore=vw.name_tokens(draft)) if explanation else [])
    return bundle, hidden, text, summary_text, vw.evidence_lines(before, after), pair['after']


def _derive_file(group):
    """One file's units with bounded caller facts reused for this group only."""
    from app.backend.pipeline import security_snapshots
    file_text, tasks = group
    with security_snapshots.preparation():
        return [_derive_unit(file_text, t) for t in tasks]


# -- the build -------------------------------------------------------------------------------------------------------------------------------

def build(tag: str | None = None, *, freeze: bool = False, root=None, injection_rate: float = 0.1,
          min_tier: str = "auto_checked", progress=None, workers=None, quality_profile: str = 'strict', temporal_cutoff=None,
          snapshot=None, profile=None, reserved_tag=None, reservation_owner=None, admission_policy=None,
          publication_guard=None) -> dict:
    """Build the reviewed dataset. Raises ``pools.EmptyCorePool`` (writing nothing) when a core task has no eligible view.

    ``workers`` is the number of worker processes for the CPU-heavy part of the records (None: the Settings value, or the machine's automatic
    choice); the output is the same bytes for any number of workers."""
    prog = progress or _noop
    if quality_profile not in ('strict', 'candidate'):
        raise ValueError('quality_profile must be strict or candidate')
    from app.backend.pipeline import security_quality
    from app.backend.pipeline.automatic_evidence import POLICY, CURRENT_POLICY, admit_record
    from app.backend.pipeline import automatic_admission
    legacy_policy = 'legacy-independent/1'
    admission_policy = admission_policy or (profile.get('admission_policy', legacy_policy) if profile else
                                          POLICY if quality_profile == 'strict' else legacy_policy)
    if admission_policy not in (POLICY, CURRENT_POLICY, automatic_admission.POLICY, legacy_policy):
        raise ValueError('unknown security admission policy')
    input_rendering = rendering.identity() if admission_policy == CURRENT_POLICY else None
    if (input_rendering is not None and snapshot is not None
            and snapshot.get('input_rendering') not in (None, input_rendering)):
        raise ValueError('input rendering changed after source capture')
    automatic = quality_profile == 'strict' and admission_policy in (POLICY, CURRENT_POLICY, automatic_admission.POLICY)
    if admission_policy == automatic_admission.POLICY:
        admit_record = automatic_admission.admit_record
    elif admission_policy == CURRENT_POLICY:
        from functools import partial
        admit_record = partial(admit_record, policy=CURRENT_POLICY)
    root = pathlib.Path(root or versions.PROC)
    if not freeze and tag and versions.parse_name('dataset_' + tag):
        raise ValueError('versioned dataset tags require an immutable frozen build')
    owner = reservation_owner or uuid.uuid4().hex
    if profile and (not freeze or not snapshot or quality_profile != 'strict' or min_tier != profile['min_tier'] or injection_rate != .1):
        raise ValueError('profile builds require their captured frozen strict security policy')
    tag = (reserved_tag or versions.reserve_version("dataset", root, owner=owner)) if freeze else (tag or "preview")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", tag):
        raise ValueError('invalid dataset tag')
    out_dir = (root / ".build_staging" / uuid.uuid4().hex / f"dataset_{tag}") if freeze else root / f"dataset_{tag}"
    if snapshot is not None:
        from training.finetune.config import ProjectConfig
        cfg = ProjectConfig.model_validate(snapshot['config'])
    else:
        cfg = project_config.config()
    languages = profile['languages'] if profile else None

    prog(state="load", label="Reading the working store")
    records, commits, pairs, licenses = snapshot['inputs'] if snapshot is not None else _load()
    leakage_pairs = pairs
    if profile:
        from app.backend.pipeline.build_catalog import select_source
        pairs = select_source(snapshot, profile)
    units, files, duplicates = _units(pairs)
    receipt_map, receipt_omissions = {}, []
    if automatic:
        if snapshot is not None:
            receipt_map = snapshot.get('verification_receipts', {})
            receipt_omissions = snapshot.get('verification_receipt_omissions', [])
        else:
            from app.backend.pipeline.build_catalog import verification_receipts
            receipt_map, receipt_omissions = verification_receipts({reviewed.record_id_of(u['pair_key']) for u in units})
    after_files = {(p.get('commit_key') or p['pair_key'], p.get('file_path')): p.get('code_after')
                   for p in pairs if p.get('granularity') == 'file'}
    rec_by_key = {r["key"]: r for r in records}
    commit_nodes: dict[str, list[str]] = defaultdict(list)
    for r in records:
        for ck in codestore._record_commit_keys(r):
            commit_nodes[ck].append(r["key"])
    cve_node = {r["cve_id"]: r["key"] for r in records if r.get("cve_id")}

    # leakage graph and split manifest over every advisory plus the pairs that have none
    nodes, items = leakage.build_nodes(records, commits, leakage_pairs)
    known = {n.node_id for n in nodes}
    unit_nodes = {u["pair_key"]: _node_ids(u, commit_nodes, cve_node) for u in units}
    merged_nodes: dict[str, set] = defaultdict(set)                       # the advisories of a duplicate join the kept unit
    pair_by_key = {p["pair_key"]: p for p in pairs}
    for dup, kept in duplicates.items():
        merged_nodes[kept].update(_node_ids(pair_by_key[dup], commit_nodes, cve_node))
    extra_nodes, extra_items = [], []
    for u in units:
        for nid in unit_nodes[u["pair_key"]]:
            if nid not in known:
                known.add(nid)
                extra_nodes.append(leakage.Node(node_id=nid, ecosystem=u.get("ecosystem"),
                                                package_key=ecosystems.package_key(u.get("ecosystem"), u.get("package"))))
                extra_items.append(leakage.Item(node_id=nid, key=u["pair_key"], language=u.get("language"),
                                                before=u.get("code_before") or "", after=u.get("code_after") or "",
                                                repo=ecosystems.repository_identity(u.get("repo_url"))))
    graph = leakage.build_graph(nodes + extra_nodes, items + extra_items, version=tag, workers=workers)
    with_code = {n for u in units for n in unit_nodes[u["pair_key"]]} | {n for ns in merged_nodes.values() for n in ns}
    node_objs = {n.node_id: n for n in nodes + extra_nodes}
    conn = derived.connect()
    parent_version = snapshot.get('parent_version') if snapshot is not None else (sm.latest_version(conn) if freeze else None)
    parent = snapshot.get('parent') if snapshot is not None else (sm.load(conn, parent_version) if parent_version else None)
    split_version = f"split_{tag}" if snapshot is not None and freeze else (sm.next_version(conn) if freeze else tag)
    manifest = sm.build([node_objs[n] for n in sorted(with_code)], graph, parent=parent, parent_version=parent_version)
    leakage.save(conn, graph)

    # the CPU-heavy part of every record (context, hidden facts, evidence lines), in worker processes, a file's units together
    groups: dict[tuple, list[dict]] = {}
    cached_contexts = {}
    if automatic and admission_policy in (POLICY, CURRENT_POLICY):
        from app.backend.pipeline import build_context_cache
        prog(state='records', label='Validating cached source contexts', processed=0, total=len(units))
        cached_contexts = build_context_cache.load(root, units, before_files=files, after_files=after_files,
            provenance=(snapshot or {}).get('context_cache_provenance'),
            excluded_pairs=set((snapshot or {}).get('derived_evidence', {}).get('repairs', {})))
    for u in units:
        groups.setdefault((u.get("commit_key") or u["pair_key"], u.get("file_path")), []).append(u)
    payload, order = [], []
    for fkey, members in groups.items():
        file_text = files.get(fkey)
        if file_text is None and members[0]["granularity"] == "file":
            file_text = members[0]["code_before"]
        tasks = []
        for u in members:
            repo_id = ecosystems.repository_identity(u.get("repo_url"))
            pkg_id = ecosystems.package_key(u.get("ecosystem"), u.get("package"))
            explanation = u.get("explanation")
            draft = {"evidence_summary": vw.first_sentences(explanation, 400) or None, "explanation": explanation, "package_identity": pkg_id,
                     "repository_identity": repo_id, "ecosystem": u.get("ecosystem")}
            unit_args = dict(path=u.get("file_path") or "", code=u.get("code_before") or "", language=u.get("language"), ecosystem=u.get("ecosystem"),
                             name=u.get("function_name") if u["granularity"] == "function" else None)
            probe_args = dict(subject_id="probe", kind="vulnerable", role="vulnerable", code=u.get("code_before") or "", language=u.get("language"),
                              ecosystem=u.get("ecosystem"), path=u.get("file_path"), symbol=u.get("function_name"), label_state="unreviewed",
                              review_status="unreviewed", weight=0.0)
            task = (unit_args, probe_args, explanation, draft, u.get("code_before") or "", u.get("code_after") or "", u, after_files.get(fkey))
            tasks.append((*task, cached_contexts[u['pair_key']]) if u['pair_key'] in cached_contexts else task)
            order.append(u["pair_key"])
        payload.append((file_text, tasks))
    prog(state="records", label="Building contexts", processed=0, total=len(payload))
    derived_parts = dict(zip(order, (r for part in compute.pmap(_derive_file, payload, workers=workers, chunksize=4, min_items=MIN_PARALLEL_FILES,
                                                                 progress=lambda done, total: prog(state="records", label="Building contexts",
                                                                                                    processed=done, total=total))
                                     for r in part)))

    # records
    decisions = snapshot['decisions'] if snapshot is not None else derived.latest_decisions(conn)
    gate_failures: Counter = Counter()
    recs, bundles, evidence, after_bundles = [], {}, {}, {}
    for i, u in enumerate(units):
        if i % 200 == 0:
            prog(state="records", label="Building records", processed=i, total=len(units))
        nodes_of = unit_nodes[u["pair_key"]]
        primary = nodes_of[0]
        all_keys = sorted(set(nodes_of) | merged_nodes.get(u["pair_key"], set()))
        real_keys = [k for k in all_keys if k in rec_by_key]
        rec = rec_by_key.get(primary)
        commit = commits.get(u.get("commit_key")) or {}
        rid = reviewed.record_id_of(u["pair_key"])
        gid, split = manifest["nodes"][primary], sm.split_of(manifest, primary)
        bundle, hidden, text, summary_text, quotes, after_bundle = derived_parts[u["pair_key"]]
        bundles[rid] = bundle
        after_bundles[rid] = after_bundle
        repo_id = ecosystems.repository_identity(u.get("repo_url"))
        pkg_id = ecosystems.package_key(u.get("ecosystem"), u.get("package"))
        explanation = u.get("explanation")
        row = {**u, "fix_check": commit.get("fix_check"), "evidence_level": commit.get("evidence_level")}
        gate = review.gate(row, split=split, context_completeness=bundle.completeness, hidden_fact_risk=bool(hidden))
        gate_failures.update(gate.failed)
        decision = decisions.get(rid)
        status, label = review.effective_status(gate, decision)
        basis = ("human_review" if decision and status == "human_reviewed" else
                 "llm_adjudication" if decision and status == "llm_adjudicated" else
                 "advisory_fix_diff" if status == "auto_checked" else None)
        fields = (decision or {}).get("fields", {})
        cwe_ids, cwe_source = _cwes(rec, u)
        score, sev_label, sev_source, cvss_version = _severity(rec, u)
        quarantine = [r for r in (u.get("quarantine_reason"), "graph_quarantine" if split == "quarantine" else None) if r]
        exclusion = [r for r, on in (("test", u.get("is_test")), ("tangled", u.get("tangled"))) if on]
        category = ecosystems.categorize_path(u.get("file_path") or "", u.get("ecosystem"), u.get("language"))
        evidence[rid] = quotes
        synthetic = ["explanation"] if u.get("explanation_source") == "llm_fallback" else []
        lic = licenses.get(repo_id or "", {})
        rec_dict = reviewed.new_record(
            raw_pair_key=u["pair_key"], raw_record_keys=real_keys, record_id=rid, issue_group_id=gid,
            advisory_aliases=sorted({a for k in real_keys for a in leakage_aliases(rec_by_key[k])}),
            repository_identity=repo_id, package_identity=pkg_id, ecosystem=u.get("ecosystem") or "unknown",
            language=u.get("language"), source_urls=_urls((rec or {}).get("source_urls")),
            source_record_hashes=[rec["source_record_hash"]] if rec and rec.get("source_record_hash") else [],
            repository_license=lic.get("license_spdx"), data_usage_status=lic.get("data_usage_status"),
            advisory_first_published_at=u.get("published"), advisory_modified_at=(rec or {}).get("modified"),
            fix_commit=u.get("fix_commit"), relevant_parent_commit=u.get("parent_commit"),
            parent_selection_reason=u.get("parent_selection_reason"),
            file_blob_hashes=json.dumps({"before": u.get("before_blob_sha"), "after": u.get("after_blob_sha")}, sort_keys=True),
            collector_version=u.get("extraction_version"), extraction_version=u.get("extraction_version"),
            review_version=review.RUBRIC_VERSION if decision else None,
            vulnerable_code=u.get("code_before"), fixed_code=u.get("code_after"), diff=u.get("diff"),
            file_path_before=u.get("old_path") or u.get("file_path"), file_path_after=u.get("file_path"),
            symbol=u.get("function_name"), line_span_before=u.get("span"), changed_lines=u.get("changed_lines"),
            reviewed_security_spans=json.dumps(fields["reviewed_security_spans"]) if fields.get("reviewed_security_spans") else None,
            granularity=u["granularity"],
            context_before=json.dumps(bundle.to_dict(), sort_keys=True),
            context_after=json.dumps(after_bundle.to_dict(), sort_keys=True),
            context_manifest=json.dumps({"policy": bundle.policy, "bundle_hash": bundle.bundle_hash,
                                         "completeness": bundle.completeness, "omissions": bundle.omissions,
                                         "items": [{"kind": x.kind, "path": x.path, "name": x.name, "chars": len(x.text)}
                                                   for x in bundle.items]}, sort_keys=True),
            callers=[x.name for x in bundle.items if x.kind == "caller" and x.name],
            callees=[x.name for x in bundle.items if x.kind == "callee" and x.name],
            imports=[l for x in bundle.items if x.kind == "imports" for l in x.text.splitlines()],
            relevant_configuration=[x.path for x in bundle.items if x.kind == "manifest"],
            evidence_completeness=bundle.completeness, omitted_context_reasons=list(bundle.omissions),
            condition_scope=fields.get("condition_scope"), trust_boundary=fields.get("trust_boundary"),
            attacker_control=fields.get("attacker_control"), preconditions=_list(fields.get("preconditions")),
            violated_invariant=fields.get("violated_invariant"),
            evidence_spans=json.dumps(fields['evidence_spans']) if fields.get('evidence_spans') else (
                json.dumps([{"quote": q, "basis": "fix_diff_line", "reviewed": False} for q in quotes]) if quotes and quality_profile == 'candidate' else None),
            fixed_review=json.dumps({**fields['fixed_review'], 'review_status': status}) if fields.get('fixed_review') else None,
            behavioral_intent=fields.get('behavioral_intent'),
            decisive_safeguard=fields.get("decisive_safeguard"), supported_impact=fields.get("supported_impact"),
            missing_facts=_list(fields.get("missing_facts")), label_state=label, label_basis=basis,
            production_relevance="production" if category == "source" else category,
            cwe_ids=cwe_ids, cwe_source=cwe_source, advisory_cvss=score, cvss_version=cvss_version, severity_label=sev_label,
            severity_source=sev_source, severity_applicability="known_advisory_only",
            explanation=explanation, synthetic_fields=synthetic, explanation_source=u.get("explanation_source"),
            original_advisory_reference=u.get("original_advisory_reference"), teacher_model_id=u.get("llm_model"),
            teacher_prompt_hash=u.get("llm_prompt_version"), review_status=status,
            reviewer_or_verifier_identity=(decision or {}).get("reviewer"), evidence_summary=fields.get('violated_invariant') or summary_text,
            evidence_summary_provenance="extractive" if summary_text else None, provenance_status=commit.get("fix_check"),
            lineage_parent_ids=[], split=split, split_manifest_version=split_version,
            quarantine_reasons=quarantine, exclusion_reasons=exclusion, augmentation_policy_version=AUGMENTATION_POLICY,
            data_quality_status="excluded" if exclusion else "quarantined" if quarantine else "ok")
        if automatic and admission_policy in (POLICY, CURRENT_POLICY) and snapshot is not None and snapshot.get('derived_evidence', {}).get('repairs'):
            from app.backend.pipeline import evidence_recovery
            rec_dict = evidence_recovery.apply_repairs(rec_dict, snapshot['derived_evidence']['repairs'])
            bundle = context_builder.Bundle.from_dict(json.loads(rec_dict['context_before']))
            after_bundle = context_builder.Bundle.from_dict(json.loads(rec_dict['context_after']))
            bundles[rid], after_bundles[rid] = bundle, after_bundle
            rec_dict.update(evidence_completeness=bundle.completeness, omitted_context_reasons=list(bundle.omissions),
                            context_manifest=json.dumps({'policy': bundle.policy, 'bundle_hash': bundle.bundle_hash,
                                'completeness': bundle.completeness, 'omissions': bundle.omissions,
                                'items': [{'kind': item.kind, 'path': item.path, 'name': item.name, 'chars': len(item.text)} for item in bundle.items]}, sort_keys=True))
        if automatic:
            rec_dict = admit_record(rec_dict, decision=decision,
                                    gate={**gate.as_dict(), 'source': row, 'hidden_facts': hidden,
                                          'receipts': receipt_map.get(rid, [])})
        rec_dict["__hidden"] = (hidden, text)
        recs.append(rec_dict)
    security_partitions = None
    if quality_profile == 'strict':
        from app.backend.pipeline.security_partitions import assign
        previous = {}
        for u in units:
            primary = unit_nodes[u['pair_key']][0]
            if parent and primary in parent['nodes']:
                group = parent['groups'][parent['nodes'][primary]]
                previous[reviewed.record_id_of(u['pair_key'])] = {'split': sm.split_of(parent, primary), 'role': group.get('role')}
        security_partitions = assign(recs, previous=previous, temporal_cutoff=temporal_cutoff)
        group_assignments = {}
        for r in recs:
            assignment = security_partitions['records'][r['record_id']]
            r['split'] = assignment['split']
            group_assignments[r['issue_group_id']] = assignment
        for gid, group in manifest['groups'].items():
            assignment = group_assignments.get(gid, {'split': 'quarantine', 'role': None})
            group.update(split=assignment['split'], role=assignment['role'], overrides={})
        manifest['security_protocol'] = security_partitions['schema']
        manifest['summary'] = sm._summary(manifest, node_objs)
        manifest['summary']['nodes']['test'] = sum(sm.split_of(manifest, n) == 'test' for n in manifest['nodes'])
    prog(state="records", label="Records built", processed=len(units), total=len(units))

    # subjects and views for the records that are usable
    prog(state="views", label="Building views")
    usable = [r for r in recs if r["data_quality_status"] == "ok"]
    subjects = sorted(negatives.subjects_from_records(usable), key=lambda s: s.subject_id)
    rec_by_id = {r["record_id"]: r for r in recs}
    views_out, view_exclusions = [], Counter()
    subject_of: dict[str, negatives.Subject] = {}
    for s in subjects:
        if not negatives.eligible(s, min_tier):
            continue
        subject_of[s.subject_id] = s
        r = rec_by_id[s.parent_record_id]
        if quality_profile == 'strict':
            problems = security_quality.admission_problems(r, fixed_twin=s.kind == 'fixed_twin', languages=languages)
            if problems:
                view_exclusions.update('security_admission:' + p for p in problems)
                continue
        selected_context = (after_bundles if s.kind == 'fixed_twin' else bundles)[r['record_id']]
        ctx_items = [i.as_dict() for i in selected_context.items]
        ctx_hash = selected_context.bundle_hash
        view_record = r
        if quality_profile == 'strict' and s.kind == 'fixed_twin':
            from dataclasses import replace
            fixed_review = json.loads(r['fixed_review'])
            s = replace(s, condition_scope=fixed_review['scope'])
            view_record = {**r, 'condition_scope': fixed_review['scope'], 'decisive_safeguard': fixed_review['safeguard'],
                           'evidence_spans': fixed_review['evidence']}
        for task in ("discovery", "condition_assessment", "localization", "insufficient_context"):
            v = vw.build_view(task, s, view_record, ctx_items, context_hash=ctx_hash, strict=quality_profile == 'strict')
            (views_out.append(v) if isinstance(v, vw.View) else view_exclusions.update([f"{task}:{v.reason.split(':')[0]}"]))
        if s.kind == "vulnerable" and s.role == "vulnerable":
            report = r.get("evidence_summary")
            v = vw.build_view("repair", s, r, ctx_items, extra={"report": report} if report else None, context_hash=ctx_hash, strict=quality_profile == 'strict')
            (views_out.append(v) if isinstance(v, vw.View) else view_exclusions.update([f"repair:{v.reason.split(':')[0]}"]))
    families = sorted(catalog.ATTACK_FAMILIES)
    for pv in [v for v in views_out if v.task == "discovery" and v.pool == "supported"]:
        h = int(hashlib.sha1(f"inject|{pv.view_id}".encode()).hexdigest(), 16)
        if (h % 10_000) / 10_000 >= injection_rate:
            continue
        s = subject_of[pv.subject_id]
        inj = vw.build_injection(pv, s, [i.as_dict() for i in bundles[pv.record_id].items],
                                 family=families[(h // 10_000) % len(families)], seed=tag)
        (views_out.append(inj) if isinstance(inj, vw.View) else view_exclusions.update([f"instruction_integrity:{inj.reason}"]))
    views_out.sort(key=lambda v: v.view_id)

    counts = pools.count_pools(views_out)
    tier_mix = review.tier_mix(r["review_status"] for r in recs)
    try:
        if quality_profile == 'candidate':
            pools.check_core(counts)  # Legacy candidate protocol; strict builds keep a blocked readiness artifact.
    except pools.EmptyCorePool as e:
        raise pools.EmptyCorePool(f"{e}. {len(recs)} records, tiers {tier_mix}; most common gate failures: "
                                  f"{dict(gate_failures.most_common(6))}") from None
    task_plan = pools.plan_task_shares(counts, cfg.sampling.task_shares)
    decision_plan = pools.plan_decision_pools(counts.get("discovery", {}), cfg.sampling.decision_pool_targets)

    # eligible tasks per record, then the rows
    tasks_of: dict[str, set] = defaultdict(set)
    for v in views_out:
        tasks_of[v.record_id].add(v.task)
    hidden_flagged = Counter()
    for r in recs:
        r["eligible_tasks"] = sorted(tasks_of.get(r["record_id"], ()))
        hidden, _ = r.pop("__hidden")
        hidden_flagged["risk" if hidden else "clear"] += 1
    group_of = manifest["groups"]
    view_rows = []
    for v in views_out:
        r = rec_by_id[v.record_id] if v.record_id in rec_by_id else {}
        grp = group_of.get(r.get("issue_group_id") or "", {})
        completion = sz.render_completion(v.task, v.response)[0]
        view_rows.append({"view_id": v.view_id, "task": v.task, "subject_id": v.subject_id, "record_id": v.record_id,
                          "issue_group_id": v.issue_group_id, "family_id": v.family_id, "pool": v.pool, "split": r.get("split"),
                          "val_role": grp.get("role"), "weight": float(v.weight), "review_status": v.review_status,
                          "eligible": bool(v.eligible), "context_hash": v.context_hash,
                          "flags_json": json.dumps(v.flags, sort_keys=True, default=str),
                          "lineage_json": json.dumps(v.lineage, sort_keys=True),
                          "context_text": v.context_text, "response_json": json.dumps(v.response, ensure_ascii=False),
                          "prompt_chars": len(v.student_text()), "completion_chars": len(completion)})

    stats = _stats(recs, view_rows, manifest, graph, gate_failures, view_exclusions, hidden_flagged, task_plan, decision_plan,
                   duplicates, units)
    readiness = security_quality.readiness(recs, view_rows, languages=languages, admission_policy=admission_policy,
        partition_groups={rid:row['component'] for rid,row in security_partitions['records'].items()} if security_partitions else None)
    admission_counts = Counter(r.get('admission_outcome') for r in recs)
    suggestions = [security_quality.unpack(r.get('context_suggestions'), {}) for r in recs] if automatic else []
    automatic_counts = {'automatically_eligible': admission_counts['admitted'] + admission_counts['insufficient_context'],
                        'insufficient_context': admission_counts['insufficient_context'],
                        'excluded': admission_counts['excluded'],
                        'context_suggestions': sum(s.get('quality') != 'strong' and bool(s.get('reasons')) for s in suggestions)} if automatic else {}
    if admission_policy in (POLICY, CURRENT_POLICY):
        automatic_counts.update(security_quality.evidence_counts(recs, view_rows))
    if automatic:
        stats['admission'] = {'policy': admission_policy, 'outcomes': dict(admission_counts), **automatic_counts,
                              'receipt_capture_omissions': receipt_omissions}

    # write everything (the checks are over)
    prog(state="write", label="Writing the dataset")
    for r in recs:
        r.pop("__hidden", None)
    recs.sort(key=lambda r: r["raw_pair_key"])
    records_path = reviewed.write_parquet(recs, out_dir / "records.parquet")
    edges = [{"src": e.src, "dst": e.dst, "type": e.type, "confidence": float(e.confidence),
              "evidence_json": json.dumps(e.evidence, sort_keys=True), "review_only": e.type == leakage.REVIEW_TYPE}
             for e in (*graph.edges, *graph.review)]
    groups = [{"node_id": n, "group_id": gid, "split": sm.split_of(manifest, n), "role": group_of[gid]["role"],
               "fold": group_of[gid]["fold"], "reason": group_of[gid]["reason"], "group_size": len(group_of[gid]["members"])}
              for n, gid in manifest["nodes"].items()]
    blobs = {"views.parquet": _table_bytes(view_rows, _VIEW_SCHEMA), "edges.parquet": _table_bytes(edges, _EDGE_SCHEMA),
             "groups.parquet": _table_bytes(groups, _GROUP_SCHEMA), "split_manifest.json": sm.canonical_json(manifest).encode("utf-8"),
             "stats.json": _json_bytes(stats, indent=1), 'security_readiness.json': _json_bytes(readiness, indent=1)}
    if security_partitions:
        blobs['security_partitions.json'] = _json_bytes(security_partitions, indent=1)
    if freeze:
        sm.save(conn, split_version, manifest)
    if parent:
        report = sm.contamination_report(conn, parent, manifest, graph, split_version)
        blobs["contamination_report.json"] = _json_bytes(report, indent=1)
    for name, data in blobs.items():
        _write(out_dir / name, data)
    file_info = {"records.parquet": {"sha256": _sha(records_path.read_bytes()), "bytes": records_path.stat().st_size,
                                     "rows": len(recs)}}
    for name, data in blobs.items():
        file_info[name] = {"sha256": _sha(data), "bytes": len(data)}
    file_info["views.parquet"]["rows"] = len(view_rows)
    manifest_doc = {
        "schema": MANIFEST_SCHEMA, "tag": tag, "dataset_status": "frozen" if freeze else "unfrozen",
        "source": {"collection_at": snapshot.get('meta', {}).get('collection_at') if snapshot is not None else _meta("collection_at"),
                   "collection_since": snapshot.get('meta', {}).get('collection_since') if snapshot is not None else _meta("collection_since"),
                   "records": len(records), "code_pairs": len(pairs), "units": len(units), "duplicates": len(duplicates)},
        "git": snapshot.get('git', {}) if snapshot is not None else _git(),
        "project": snapshot.get('project', {}) if snapshot is not None else project_config.run_fields(),
        "versions": {"build": VERSION, "reviewed_record": reviewed.SCHEMA_VERSION, "split_manifest": sm.SCHEMA,
                     "leakage": leakage.VERSION, "context_policy": context_builder.POLICY,
                     "serialization": sz.SERIALIZATION_ID, "serialization_hash": sz.serialization_hash(),
                     "rubric": review.RUBRIC_VERSION, "augmentation": AUGMENTATION_POLICY,
                     "extraction": sorted({u.get("extraction_version") for u in units if u.get("extraction_version")})},
        "counts": {"records": len(recs), "duplicates": len(duplicates), "groups": len(set(manifest["nodes"].values())),
                   "views": len(view_rows), "views_by_task": dict(sorted(Counter(v["task"] for v in view_rows).items())),
                   **automatic_counts},
        "tier_mix": tier_mix, "min_review_tier_for_training": min_tier, "unreviewed_supervision_weight": 0.0,
        "split_manifest_version": split_version, "split_manifest_sha256": file_info["split_manifest.json"]["sha256"],
        "task_share_plan": task_plan, "decision_pool_plan": decision_plan,
        "blocked": _blocked(counts, automatic=automatic), "teacher": {"enabled": bool(cfg.mining.teacher_enabled)},
        'quality_profile': quality_profile, 'security_readiness': readiness, 'admission_policy': admission_policy,
        "files": dict(sorted(file_info.items()))}
    if profile:
        from app.backend.pipeline.build_catalog import _json
        manifest_doc['build_profile'] = profile
        from app.backend.pipeline.build_catalog import canonical_snapshot_sha256
        manifest_doc['source']['snapshot_sha256'] = canonical_snapshot_sha256(snapshot)
    if admission_policy in (POLICY, CURRENT_POLICY):
        from app.backend.pipeline import build_context_cache
        manifest_doc['context_cache_signature'] = build_context_cache.signature()
    if input_rendering is not None:
        if rendering.identity() != input_rendering:
            raise ValueError('input rendering changed during dataset construction')
        manifest_doc['input_rendering'] = input_rendering
        manifest_doc['versions']['input_rendering'] = input_rendering['policy']
    _write(out_dir / "manifest.json", _json_bytes(manifest_doc, indent=1))
    if freeze:
        out_dir = versions.publish_reserved(out_dir, tag, root, owner=owner, publication_guard=publication_guard)
    from app.backend.pipeline import build_catalog
    build_catalog.register_artifact(out_dir, manifest_doc, root=root)
    prog(state="done", label="Dataset built", processed=len(recs), total=len(recs))
    return {"path": str(out_dir), "tag": tag, "counts": {"records": len(recs), "duplicates": len(duplicates),
                                                           "views": len(view_rows), "groups": manifest_doc["counts"]["groups"], **automatic_counts},
            "tier_mix": tier_mix, "files": sorted(file_info) + ["manifest.json"], "split_manifest_version": split_version}


def leakage_aliases(rec: dict) -> list[str]:
    return [a for a in [*_list(rec.get("aliases")), rec.get("cve_id"), rec.get("osv_id")] if a]


def _meta(key: str):
    from app.backend.pipeline import store
    return store.get_meta(key)


def _blocked(counts: dict, *, automatic=False) -> dict:
    def avail(task: str, why: str) -> dict:
        n = sum(counts.get(task, {}).values())
        return {"status": "available", "views": n} if n else {"status": "empty_until_admitted" if automatic else "empty_until_reviewed", "reason": why}
    return {"general_code_replay": pools.replay_status(None),
            "localization": avail("localization", "needs source-grounded security spans" if automatic else "needs reviewed security spans"),
            "insufficient_context": avail("insufficient_context", "needs specific missing evidence" if automatic else "needs reviewed omissions"),
            "verification": {"status": "unavailable", "reason": "no verification receipts: grades G2 and G3 are not claimed"},
            "gpu_training": {"status": "blocked", "reason": "not run by the build"}}


def _stats(recs, view_rows, manifest, graph, gate_failures, view_exclusions, hidden, task_plan, decision_plan, duplicates, units) -> dict:
    cov: dict[tuple, Counter] = {}
    groups_of: dict[tuple, set] = defaultdict(set)
    views_by_rec = Counter(v["record_id"] for v in view_rows)
    repos: dict[str, Counter] = {"train": Counter(), "val": Counter(), "quarantine": Counter()}
    for r in recs:
        key = (r["ecosystem"], r["language"] or "unknown", _year(r["advisory_first_published_at"]), review.mechanism(r["cwe_ids"]))
        c = cov.setdefault(key, Counter())
        c["records"] += 1
        c["auto_checked"] += r["review_status"] == "auto_checked"
        c["reviewed"] += r["review_status"] in ("human_reviewed", "llm_adjudicated")
        c["views"] += views_by_rec.get(r["record_id"], 0)
        c[f"split_{r['split']}"] += 1
        groups_of[key].add(r["issue_group_id"])
        if r["split"] in repos and r["repository_identity"]:
            repos[r["split"]][r["repository_identity"]] += 1
    coverage = [{"ecosystem": k[0], "language": k[1], "year": k[2], "mechanism": k[3], "groups": len(groups_of[k]),
                 "records": c["records"], "auto_checked": c["auto_checked"], "reviewed": c["reviewed"], "views": c["views"],
                 "train": c["split_train"], "val": c["split_val"], "quarantine": c["split_quarantine"]}
                for k, c in sorted(cov.items())]

    def share(c: Counter) -> dict:
        total = sum(c.values())
        return {"largest_repository_share": round(max(c.values()) / total, 4) if total else 0.0,
                "independent_repositories": len(c),
                "effective_repositories": round(1 / sum((n / total) ** 2 for n in c.values()), 2) if total else 0.0}
    top = Counter()
    for c in repos.values():
        top.update(c)
    total = sum(top.values())
    return {"coverage": coverage,
            "concentration": {"top_repositories": [{"repository": k, "records": n, "share": round(n / total, 4)}
                                                   for k, n in sorted(top.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
                              "largest_repository_share": round(max(top.values()) / total, 4) if total else 0.0,
                              **{f"{s}": share(c) for s, c in repos.items()}},
            "exclusions": {"data_quality": dict(sorted(Counter(r["data_quality_status"] for r in recs).items())),
                           "gate_failures": dict(sorted(gate_failures.items())),
                           "view_exclusions": dict(sorted(view_exclusions.items())),
                           "duplicates": len(duplicates), "hidden_fact_risk": dict(hidden)},
            "label_states": dict(sorted(Counter(r["label_state"] for r in recs).items())),
            "splits": manifest["summary"], "val_roles": manifest["val_roles"],
            "graph": graph.stats, "oversized_groups": graph.oversized[:10],
            "by_ecosystem": dict(sorted(Counter(r["ecosystem"] for r in recs).items())),
            "by_language": dict(sorted(Counter(r["language"] or "unknown" for r in recs).items())),
            "task_shares": task_plan, "decision_pools": decision_plan, "units": len(units)}


# -- embedded data layer (compatibility modules pipeline/db/*_mongo.py), bound under the established public names ---------------------
_load = _pipeline_mongo.reviewed_inputs
