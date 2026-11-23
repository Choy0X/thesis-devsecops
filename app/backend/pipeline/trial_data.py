"""Build a bounded, immutable source-learning derivative of a frozen corpus.

Security labels, record identities and supervision are never promoted or rewritten.
Only derived split/role columns change. Full view payloads are read in bounded
batches and retained only for the selected IDs, not for the entire parent corpus.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq


TASKS = ("code_completion", "upstream_patch", "change_analysis")
POLICY = "portable-source-trial/2"
_TOKEN = re.compile(r"[A-Za-z_$][\w$]*|\d+|[^\s]", re.ASCII)
_RECORD_COLUMNS = ("record_id", "repository_identity", "package_identity", "language",
    "issue_group_id", "advisory_aliases", "lineage_parent_ids", "fix_family_id",
    "clone_group_ids", "fix_commit", "relevant_parent_commit", "raw_pair_key",
    "fork_family_id", "repository_family_id", "vulnerable_code", "fixed_code")
_VIEW_COLUMNS = ("view_id", "record_id", "family_id", "subject_id", "task", "eligible",
    "weight", "split", "context_text", "response_json", "lineage_json")


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 << 20), b""):
            result.update(chunk)
    return result.hexdigest()


def _batches(path, columns=None):
    parquet = pq.ParquetFile(path)
    names = parquet.schema_arrow.names
    columns = [name for name in columns if name in names] if columns else None
    yield from parquet.iter_batches(batch_size=256, columns=columns)


def _repository(value):
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    return (parsed.netloc + parsed.path).rstrip("/").removesuffix(".git")


def _code_keys(code):
    # Keep identifiers/literals intact. Tiny idioms and punctuation-only fragments
    # provide no cross-repository ancestry evidence.
    tokens = _TOKEN.findall(code or "")
    if len(tokens) < 96:
        return
    def meaningful(part):
        return len({t for t in part if len(t) >= 4 and t[0].isalpha()}) >= 12
    if meaningful(tokens):
        yield "full:" + hashlib.sha256("\0".join(tokens).encode()).hexdigest()
    # Every offset contributes a 96-token rolling hash, but only content-defined
    # minima are retained. Winnowing 32 consecutive grams is position invariant:
    # a shared 128-token block has two wholly shared 32-gram windows. If any gram
    # meets the identifier filter, one of those windows emits a common anchor.
    # The rolling hash selects anchors only; SHA256 authenticates their tokens.
    width, span, mask, base = 96, 32, (1 << 64) - 1, 1_000_003
    token_hashes, cache = [], {}
    for token in tokens:
        if token not in cache:
            cache[token] = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8,
                                                       person=b"trial-code-v2").digest(), "big")
        token_hashes.append(cache[token])
    leading_power = pow(base, width - 1, 1 << 64)
    rolling = 0
    for value in token_hashes[:width]:
        rolling = (rolling * base + value) & mask
    identifiers = Counter(t for t in tokens[:width] if len(t) >= 4 and t[0].isalpha())
    minima, last_emitted = deque(), -1
    grams = len(tokens) - width + 1
    for start in range(grams):
        if start:
            old, new = tokens[start - 1], tokens[start + width - 1]
            if len(old) >= 4 and old[0].isalpha():
                identifiers[old] -= 1
                if not identifiers[old]:
                    del identifiers[old]
            if len(new) >= 4 and new[0].isalpha():
                identifiers[new] += 1
            rolling = ((rolling - token_hashes[start - 1] * leading_power) * base
                       + token_hashes[start + width - 1]) & mask
        while minima and minima[0][1] <= start - span:
            minima.popleft()
        if len(identifiers) >= 12:
            while minima and minima[-1][0] >= rolling:
                minima.pop()
            minima.append((rolling, start))
        if minima and (start >= span - 1 or start == grams - 1):
            anchor = minima[0][1]
            if anchor != last_emitted:
                yield "anchor96:" + hashlib.sha256("\0".join(tokens[anchor:anchor + width]).encode()).hexdigest()
                last_emitted = anchor


def _group_records(records, source_dir, check=None):
    parent = {rid: rid for rid in records}
    evidence = Counter()
    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    def union(left, right, reason):
        if left not in parent or right not in parent:
            return
        left, right = find(left), find(right)
        if left != right:
            first, second = sorted((left, right))
            parent[second] = first
            evidence[reason] += 1
    owners = {}
    def bind(rid, category, identity):
        if not identity:
            return
        key = (category, str(identity).lower())
        other = owners.setdefault(key, rid)
        union(rid, other, category)
    for number, (rid, record) in enumerate(sorted(records.items())):
        if check and number % 256 == 0:
            check()
        repo = _repository(record.get("repository_identity"))
        bind(rid, "repository", repo)
        for field in ("issue_group_id", "fix_family_id", "fork_family_id", "repository_family_id"):
            bind(rid, field, record.get(field))
        for field in ("advisory_aliases", "clone_group_ids"):
            for value in record.get(field) or []:
                bind(rid, field, value)
        for field in ("fix_commit", "relevant_parent_commit"):
            if repo and record.get(field):
                bind(rid, "repository_revision", repo + ":" + record[field])
        for ancestor in record.get("lineage_parent_ids") or []:
            # Also bind unknown parents so sibling derivatives are joined.
            bind(rid, "lineage_parent", ancestor)
            union(rid, ancestor, "lineage_parent")
        for field in ("vulnerable_code", "fixed_code"):
            for key in _code_keys(record.get(field)):
                bind(rid, "meaningful_code", key)
    source_dir = Path(source_dir)
    if (source_dir / "groups.parquet").exists():
        for batch in _batches(source_dir / "groups.parquet", ("node_id", "group_id")):
            for row in batch.to_pylist():
                if row["node_id"] in records:
                    bind(row["node_id"], "inherited_group", row["group_id"])
    if (source_dir / "edges.parquet").exists():
        for batch in _batches(source_dir / "edges.parquet"):
            for row in batch.to_pylist():
                if not row.get("review_only"):
                    union(row["src"], row["dst"], "inherited_edge:" + row.get("type", "unknown"))
    groups = {rid: find(rid) for rid in records}
    sizes = Counter(groups.values())
    return groups, {"policy": "repository_alias_ancestry_meaningful_code/2",
        "edge_counts": dict(sorted(evidence.items())), "independent_groups": len(sizes),
        "largest_group_records": max(sizes.values(), default=0),
        "duplicate_policy": {"normalization": "lexical_tokens_preserving_identifiers_literals",
            "minimum_tokens": 96, "anchor_tokens": 96, "winnowing_grams": 32,
            "anchor_selection": "rolling64_minimum_rightmost_tie_sha256_identity",
            "minimum_distinct_identifiers": 12, "tiny_common_fragments": "ignored",
            "overlap_guarantee": "shared_128_tokens_containing_an_eligible_96_token_gram",
            "eligibility": "at_least_12_distinct_identifiers_with_length_at_least_4_in_anchor"}}


def _select(candidates, count, seed, *, cover_groups=False):
    selected, used = [], set()
    counters = {name: Counter() for name in ("task", "language", "length_bin", "group", "repository")}
    def rank(row):
        return tuple(counters[name][row[name]] for name in counters) + (row["rank"],)
    def choose(rows):
        row = min(rows, key=rank)
        selected.append(row)
        used.add(row["view_id"])
        for name in counters:
            counters[name][row[name]] += 1
    if cover_groups:
        by_group = defaultdict(list)
        for row in candidates:
            by_group[row["group"]].append(row)
        for group in sorted(by_group, key=lambda value: hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()):
            if len(selected) < count:
                choose(by_group[group])
    while len(selected) < count:
        remaining = [row for row in candidates if row["view_id"] not in used]
        if not remaining:
            raise ValueError(f"not enough eligible views: requested {count}, found {len(selected)}")
        choose(remaining)
    if set(row["task"] for row in selected) != set(TASKS):
        raise ValueError("requested sample cannot cover all three source tasks")
    return selected


def _coverage(rows):
    result = {field: dict(sorted(Counter(str(row[field]) for row in rows).items()))
              for field in ("task", "language", "length_bin")}
    result.update(independent_families=len({r["group"] for r in rows}),
                  repositories=len({r["repository"] for r in rows}), views=len(rows))
    return result


def capability(dataset):
    """Cheap UI/preflight eligibility; build performs the authoritative hash checks."""
    blockers = []
    try:
        manifest = dataset if isinstance(dataset, dict) else json.loads(
            (Path(dataset) / "manifest.json").read_bytes())
    except (OSError, ValueError, TypeError):
        manifest = {}
        blockers.append("dataset_manifest_unavailable")
    if manifest.get("dataset_status") != "frozen":
        blockers.append("source_dataset_must_be_frozen")
    coverage = manifest.get("coverage_policy", {})
    if coverage.get("schema") != "mined-corpus/1":
        blockers.append("source_learning_corpus_required")
    declared = coverage.get("tasks") or list(TASKS)
    if not set(TASKS).issubset(declared):
        blockers.append("all_three_source_learning_tasks_required")
    return {"allowed": not blockers, "blockers": blockers, "dataset_version": manifest.get("tag"),
            "trial_policy": POLICY, "tasks": list(TASKS), "fresh_adapter_required": True}


def build(source_dir, out_dir, *, seed=17, train_views=256, eval_views=24,
          target_families=64, check=None):
    """Create a new version, refusing to replace any existing output directory.

    ``check`` is a cancellation callback that may raise. No network, model loading,
    global registry allocation, or modification of parent files occurs.
    """
    source_dir, out_dir = Path(source_dir).resolve(), Path(out_dir).resolve()
    if out_dir.exists():
        raise FileExistsError(f"immutable trial already exists: {out_dir}")
    if source_dir == out_dir or source_dir in out_dir.parents:
        raise ValueError("trial output must be outside the frozen source directory")
    if min(train_views, eval_views) < 3 or target_families < 1:
        raise ValueError("each split needs at least three views and one family")
    if check:
        check()
    source_bytes = (source_dir / "manifest.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("dataset_status") != "frozen":
        raise ValueError("source dataset must be frozen")
    if source.get("coverage_policy", {}).get("schema") != "mined-corpus/1":
        raise ValueError("source must contain mined-corpus/1 learning views")
    verified = {}
    for name in ("records.parquet", "views.parquet", "groups.parquet", "edges.parquet"):
        if not (source_dir / name).exists():
            continue
        actual = _sha(source_dir / name)
        expected = source.get("files", {}).get(name, {}).get("sha256")
        if expected and expected != actual:
            raise ValueError(f"frozen source hash mismatch: {name}")
        verified[name] = {"sha256": actual, "bytes": (source_dir / name).stat().st_size}
    records = {}
    for batch in _batches(source_dir / "records.parquet", _RECORD_COLUMNS):
        records.update((r["record_id"], r) for r in batch.to_pylist())
    groups, grouping = _group_records(records, source_dir, check)
    candidates, exclusions = [], Counter()
    for batch in _batches(source_dir / "views.parquet", _VIEW_COLUMNS):
        if check:
            check()
        for row in batch.to_pylist():
            if row.get("task") not in TASKS:
                exclusions["not_source_learning_task"] += 1
                continue
            if not row.get("eligible") or (row.get("weight") or 0) <= 0:
                exclusions["ineligible_or_zero_weight"] += 1
                continue
            if row.get("split") != "train":
                exclusions["not_parent_training_partition"] += 1
                continue
            rid = row["record_id"]
            if rid not in records:
                exclusions["missing_source_record"] += 1
                continue
            # Compute lexical token lengths per batch, then discard source text.
            # This is a sampling measure, not a claim about model tokenizer cost.
            lexical_length = len(_TOKEN.findall(row.get("context_text") or "")) + len(
                _TOKEN.findall(row.get("response_json") or ""))
            candidates.append({"view_id": row["view_id"], "record_id": rid,
                "task": row["task"], "language": records[rid].get("language") or "unknown",
                "repository": _repository(records[rid].get("repository_identity")) or rid,
                "group": groups[rid], "lexical_tokens": lexical_length,
                "rank": hashlib.sha256(f"{seed}:{row['view_id']}".encode()).hexdigest()})
    lengths = sorted(r["lexical_tokens"] for r in candidates)
    if not lengths:
        raise ValueError("source has no eligible source learning views")
    cuts = [lengths[min(len(lengths) - 1, len(lengths) * q // 4)] for q in (1, 2, 3)]
    for row in candidates:
        row["length_bin"] = sum(row["lexical_tokens"] > cut for cut in cuts)
    by_group = defaultdict(list)
    for row in candidates:
        by_group[row["group"]].append(row)
    # Every selected group has enough varied supervision to support all tasks.
    complete = {gid: rows for gid, rows in by_group.items() if {r["task"] for r in rows} == set(TASKS)}
    group_order = sorted(complete, key=lambda gid: hashlib.sha256(f"{seed}:group:{gid}".encode()).hexdigest())
    train_group_count = min(target_families, train_views)
    eval_group_count = min(max(1, math.ceil(eval_views / 3)), eval_views)
    if len(complete) < train_group_count + eval_group_count:
        raise ValueError(f"need {train_group_count + eval_group_count} independent all-task families; found {len(complete)}")
    # Balance the language mixture among families before balancing individual views.
    chosen_groups, language_counts = [], Counter()
    def choose_group():
        available = [gid for gid in group_order if gid not in chosen_groups]
        gid = min(available, key=lambda g: (min(language_counts[r["language"]] for r in complete[g]), group_order.index(g)))
        chosen_groups.append(gid)
        language_counts.update(set(r["language"] for r in complete[gid]))
        return gid
    eval_groups = {choose_group() for _ in range(eval_group_count)}
    train_groups = {choose_group() for _ in range(train_group_count)}
    train = _select([r for gid in train_groups for r in complete[gid]], train_views, seed, cover_groups=True)
    evaluation = _select([r for gid in eval_groups for r in complete[gid]], eval_views, seed, cover_groups=True)
    selected_meta = train + evaluation
    selected_ids = {r["view_id"] for r in selected_meta}
    split_of = {r["record_id"]: ("train" if r["group"] in train_groups else "val") for r in selected_meta}
    selected_views, selected_records = [], []
    for batch in _batches(source_dir / "views.parquet"):
        if check:
            check()
        for row in batch.to_pylist():
            if row["view_id"] in selected_ids:
                row["split"] = split_of[row["record_id"]]
                row["val_role"] = "source_trial" if row["split"] == "val" else "train"
                if "role" in row:
                    row["role"] = row["val_role"]
                selected_views.append(row)
    for batch in _batches(source_dir / "records.parquet"):
        for row in batch.to_pylist():
            if row["record_id"] in split_of:
                row["split"] = split_of[row["record_id"]]
                selected_records.append(row)
    selected_views.sort(key=lambda r: r["view_id"])
    selected_records.sort(key=lambda r: r["record_id"])
    coverage = {"train": _coverage(train), "source_trial": _coverage(evaluation),
                "source": _coverage(candidates), "lexical_token_quantile_cuts": cuts,
                "length_measure": "lexical_tokens_of_context_and_response_not_model_tokens"}
    all_strata = {(r["task"], r["language"], r["length_bin"]) for r in candidates}
    represented = {(r["task"], r["language"], r["length_bin"]) for r in selected_meta}
    coverage["omitted_strata"] = [list(s) for s in sorted(all_strata - represented)]
    counts = {"records": len(selected_records), "eligible_records": len(selected_records),
              "eligible_views": len(selected_views), "train_views": len(train), "eval_views": len(evaluation),
              "independent_families": len(train_groups | eval_groups),
              "training_records": sum(s == "train" for s in split_of.values()),
              "held_out_records": sum(s == "val" for s in split_of.values())}
    grouping["selected_records"] = [{"record_id": rid, "group_id": groups[rid], "split": split_of[rid]}
                                     for rid in sorted(split_of)]
    grouping["overlap_audit"] = {"passed": train_groups.isdisjoint(eval_groups),
        "cross_split_group_overlap": sorted(train_groups & eval_groups),
        "scope": "full_parent_record_graph_before_sampling",
        "unseen_scope": "fresh_trial_adapter_only_not_previously_exposed_v29_adapters"}
    identity = {"policy": POLICY, "parent_manifest_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "seed": seed, "train_views": train_views, "eval_views": eval_views,
                "target_families": target_families, "selected": [(r["view_id"], split_of[r["record_id"]])
                    for r in sorted(selected_meta, key=lambda r: r["view_id"])]}
    manifest = {"schema": "dataset-manifest/1", "dataset_status": "frozen",
        "source": {"tag": source.get("tag"), "manifest_sha256": hashlib.sha256(source_bytes).hexdigest(),
                   "verified_inputs": verified, "parent_file_identities": source.get("files", {}),
                   "original_source": source.get("source", {})},
        "selection": identity, "counts": counts, "coverage": coverage, "grouping": grouping,
        "coverage_policy": {"schema": "mined-corpus/1", "trial_policy": POLICY,
            "partition_policy": "independent_source_trial/1", "tasks": list(TASKS),
            "security_labels": "unchanged_not_inferred_from_mining",
            "evaluation": "source_learning_only_not_security_selection"},
        "evaluation": {"available": True, "role": "source_trial", "security_evaluation": False,
            "requires_fresh_adapter": True, "held_out_records": counts["held_out_records"]},
        "source_exclusions": {"view_reasons": dict(exclusions),
            "eligible_views_not_selected": len(candidates) - len(selected_meta),
            "records_not_selected": len(records) - len(selected_records),
            "all_task_families_available": len(complete),
            "reason": "bounded_stratified_trial_sample_not_quality_or_security_rejection"},
        "serialization_protocol": source.get("serialization_protocol", "sft_native_chat_v1"),
        "tokenizer": source.get("tokenizer", {}), "execution_verification": "not_run",
        "model_performance": "unmeasured"}
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="." + out_dir.name + "-", dir=out_dir.parent))
    try:
        for name, rows in (("records.parquet", selected_records), ("views.parquet", selected_views)):
            schema = pq.ParquetFile(source_dir / name).schema_arrow
            pq.write_table(pa.Table.from_pylist(rows, schema=schema), stage / name, compression="zstd")
        with (stage / "instruction_views.jsonl").open("wb") as handle:
            for row in selected_views:
                item = dict(row)
                item["messages"] = json.loads(item.get("messages_json") or "[]")
                item["response"] = json.loads(item["response_json"])
                handle.write(_json(item))
        manifest["files"] = {p.name: {"sha256": _sha(p), "bytes": p.stat().st_size}
                             for p in sorted(stage.iterdir())}
        suffix = hashlib.sha256(_json({"selection": identity, "files": manifest["files"]})).hexdigest()[:16]
        manifest["tag"] = "trial_" + suffix
        manifest["trial_version"] = "portable-source-trial-" + suffix
        (stage / "manifest.json").write_bytes(_json(manifest))
        if check:
            check()
        # Detect source edits during the build before publishing the derivative.
        if (source_dir / "manifest.json").read_bytes() != source_bytes:
            raise ValueError("source manifest changed during trial preparation")
        for name, expected in verified.items():
            if _sha(source_dir / name) != expected["sha256"]:
                raise ValueError(f"source file changed during trial preparation: {name}")
        stage.rename(out_dir)
    finally:
        if stage.exists():
            if stage.resolve().parent != out_dir.parent:
                raise RuntimeError("refusing cleanup outside trial parent")
            shutil.rmtree(stage)
    return {"data_dir": str(out_dir), "manifest_sha256": _sha(out_dir / "manifest.json"),
            "trial_version": manifest["trial_version"], "dataset_version": manifest["tag"],
            "counts": counts, "coverage": coverage,
            "bytes": sum(p.stat().st_size for p in out_dir.iterdir())}
