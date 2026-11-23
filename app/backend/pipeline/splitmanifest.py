"""Split manifest v4: the stable package hash, applied to whole issue groups.

The base rule is the one the project always had (``split.py``): a package goes to ``val`` when
``sha1("<ecosystem>:<package>") / 2^160`` is below the val share (5%), otherwise to ``train``; the
salt is "none" and the values are pinned by tests. What is new is that the rule is applied to a
leakage group (``leakage.py``), never to a single record:

* members that agree keep the base assignment;
* a group whose packages disagree goes to ``val`` when it is small (few packages, few records) and to
  ``quarantine`` otherwise, so no group is ever split and no large group inflates validation;
* the reason of every group is kept in the manifest;
* validation groups get a stable role (``selection`` 80 percent, ``calibration`` 20 percent, by
  ``sha1("role:" + group id)``); with too few groups in a role the manifest says
  ``insufficient_data`` and offers grouped cross-fitting folds instead.

A manifest is frozen once saved. A later build with ``parent=`` keeps every known record where it was
(never silently reassigned): a new record joins the split of a clean group, goes to quarantine when it
joins a group that now mixes splits, and a link between a validation group and a training group is a
contamination, reported per model that trained on the linked family (``contamination_report``).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from app.backend.pipeline import derived

SCHEMA = "split-manifest/4"
SALT = "none"                  # pinned: the project's rule never salted the hash
VAL_SHARE = 0.05
SELECTION_FRACTION = 0.80
FOLDS = 5
SMALL_COMPONENT_MAX_GROUPS = 5      # packages (hash groups) a disagreeing component may join and still go to val
SMALL_COMPONENT_MAX_NODES = 50      # records a disagreeing component may hold and still go to val
MIN_GROUPS_PER_ROLE = 10
ROUTING = "val_if_small_else_quarantine"


class FrozenManifest(Exception):
    """A saved manifest version cannot be given other content."""


def bucket(key: str) -> float:
    """Where a key falls in [0, 1): sha1 of the key itself (SALT "none" means nothing is prepended)."""
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) / float(1 << 160)


def base_split(key: str, val_share: float = VAL_SHARE) -> str:
    return "val" if bucket(key) < float(val_share) else "train"


def node_key(node) -> str:
    """The key the base rule hashes: the canonical package of the record, else the record itself."""
    return node.package_key or f"node:{node.node_id}"


# -- reading a manifest ----------------------------------------------------------------------------------------

def group_of(manifest: dict, node_id: str) -> str | None:
    return manifest["nodes"].get(node_id)


def split_of(manifest: dict, node_id: str) -> str | None:
    gid = group_of(manifest, node_id)
    if gid is None:
        return None
    g = manifest["groups"][gid]
    return g["overrides"].get(node_id, g["split"]) if g["split"] == "mixed" else g["split"]


def canonical_json(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def digest(manifest: dict) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


# -- building --------------------------------------------------------------------------------------------------

def _routed(keys: list[str], size: int, val_share: float, max_groups: int, max_nodes: int) -> tuple[str, str]:
    bases = {base_split(k, val_share) for k in keys}
    if len(bases) == 1:
        return bases.pop(), "base"
    if len(keys) <= max_groups and size <= max_nodes:
        return "val", "disagreement_routed_to_val"
    return "quarantine", "disagreement_quarantined"


def build(nodes, graph, *, val_share: float = VAL_SHARE, small_component_max_groups: int = SMALL_COMPONENT_MAX_GROUPS,
          small_component_max_nodes: int = SMALL_COMPONENT_MAX_NODES, min_groups_per_role: int = MIN_GROUPS_PER_ROLE,
          selection_fraction: float = SELECTION_FRACTION, parent: dict | None = None,
          parent_version: str | None = None) -> dict:
    """The manifest of ``nodes`` under the leakage ``graph``. Deterministic: the same records and graph
    give the same bytes in any order. With ``parent`` a record the parent knows is never moved."""
    by_id = {n.node_id: n for n in nodes}
    missing = sorted(set(by_id) - set(graph.groups))
    if missing:
        raise ValueError(f"the leakage graph does not cover {len(missing)} records, e.g. {missing[:3]}")
    members: dict[str, list[str]] = defaultdict(list)
    for nid in sorted(by_id):
        members[graph.groups[nid]].append(nid)

    groups: dict[str, dict] = {}
    for gid in sorted(members):
        ms = members[gid]
        keys = sorted({node_key(by_id[m]) for m in ms})
        known = {m: split_of(parent, m) for m in ms if parent is not None and group_of(parent, m)}
        overrides: dict[str, str] = {}
        contaminated = False
        if known:
            splits = set(known.values())
            if len(splits) == 1:
                split, reason = splits.pop(), "inherited"
                parent_groups = {group_of(parent, m) for m in known}
                if len(parent_groups) == 1 and set(parent["groups"][next(iter(parent_groups))]["members"]) == set(ms):
                    reason = parent["groups"][next(iter(parent_groups))]["reason"]    # the group did not change
            else:                                              # mixed: nobody is reassigned, newcomers wait
                contaminated = "val" in splits
                split = "mixed"
                reason = "contaminated_mixed" if contaminated else "mixed_inherited"
                overrides = {m: known.get(m, "quarantine") for m in ms}
        else:
            split, reason = _routed(keys, len(ms), val_share, small_component_max_groups, small_component_max_nodes)
        entry = {"split": split, "reason": reason, "members": ms, "keys": keys, "overrides": overrides,
                 "role": None, "fold": None}
        if contaminated:
            entry["contaminated"] = True
        if split == "val":
            entry["role"] = "selection" if bucket(f"role:{gid}") < selection_fraction else "calibration"
            entry["fold"] = min(FOLDS - 1, int(bucket(f"fold:{gid}") * FOLDS))
        groups[gid] = entry

    manifest = {
        "schema": SCHEMA, "parent": parent_version,
        "algorithm": {"base": "sha1(<ecosystem>:<canonical package>) / 2^160 < val_share -> val, else train",
                      "salt": SALT, "val_share": val_share, "routing": ROUTING,
                      "small_component_max_groups": small_component_max_groups,
                      "small_component_max_nodes": small_component_max_nodes,
                      "role": "sha1('role:' + group id) / 2^160 < selection_fraction -> selection",
                      "selection_fraction": selection_fraction, "crossfit_folds": FOLDS},
        "graph": {"version": graph.version, "params": graph.params, "stats": graph.stats},
        "groups": groups,
        "nodes": {m: gid for gid, g in groups.items() for m in g["members"]},
    }
    manifest["nodes"] = dict(sorted(manifest["nodes"].items()))
    manifest["val_roles"] = _roles(groups, min_groups_per_role)
    manifest["summary"] = _summary(manifest, by_id)
    return manifest


def _roles(groups: dict, minimum: int) -> dict:
    roles = Counter(g["role"] for g in groups.values() if g["split"] == "val")
    out = {"selection_groups": roles["selection"], "calibration_groups": roles["calibration"],
           "min_groups_per_role": minimum, "status": "ok", "fallback": None}
    if min(roles["selection"], roles["calibration"]) < minimum:
        out["status"] = "insufficient_data"
        out["fallback"] = "grouped_crossfit"
    return out


def _share(counts: Counter) -> float:
    total = sum(counts.values())
    return round(max(counts.values()) / total, 4) if total else 0.0


def _summary(manifest: dict, by_id: dict) -> dict:
    nodes: Counter = Counter()
    by_eco: dict[str, Counter] = defaultdict(Counter)
    repos: dict[str, Counter] = {"train": Counter(), "val": Counter()}
    for nid in manifest["nodes"]:
        s = split_of(manifest, nid)
        nodes[s] += 1
        eco = by_id[nid].ecosystem or "unknown"
        by_eco[eco][s] += 1
        if s in repos:
            for r in set(by_id[nid].repos):
                repos[s][r] += 1
    groups = Counter(g["split"] for g in manifest["groups"].values())
    n_assigned = sum(nodes.values())
    n_nodes = {k: nodes.get(k, 0) for k in ("train", "val", "quarantine")}
    node_repo_counts = {s: sum(1 for nid in manifest["nodes"] if split_of(manifest, nid) == s and by_id[nid].repos)
                        for s in repos}
    return {
        "nodes": n_nodes,
        "groups": {k: groups.get(k, 0) for k in ("train", "val", "quarantine", "mixed")},
        "val_share_nodes": round(n_nodes["val"] / n_assigned, 4) if n_assigned else 0.0,
        "val_independent_repositories": len(repos["val"]),
        "train_independent_repositories": len(repos["train"]),
        "val_largest_repository_share": round(max(repos["val"].values()) / node_repo_counts["val"], 4)
        if repos["val"] else 0.0,
        "train_largest_repository_share": round(max(repos["train"].values()) / node_repo_counts["train"], 4)
        if repos["train"] else 0.0,
        "by_ecosystem": {e: dict(sorted(c.items())) for e, c in sorted(by_eco.items())},
        "reasons": dict(sorted(Counter(g["reason"] for g in manifest["groups"].values()).items())),
        "contaminated_groups": sum(1 for g in manifest["groups"].values() if g.get("contaminated")),
    }


# -- saving, freezing ----------------------------------------------------------------------------------------------

def _versions(conn) -> list[str]:
    mg = derived.mongo_of(conn)
    return mg.manifest_versions(conn)


def next_version(conn) -> str:
    n = 0
    for v in _versions(conn):
        if v.startswith("split_v") and v[7:].isdigit():
            n = max(n, int(v[7:]))
    return f"split_v{n + 1}"


def latest_version(conn) -> str | None:
    """The highest numbered saved version, or None when nothing is saved yet."""
    n = 0
    for v in _versions(conn):
        if v.startswith("split_v") and v[7:].isdigit():
            n = max(n, int(v[7:]))
    return f"split_v{n}" if n else None


def save(conn, version: str, manifest: dict) -> None:
    """Keep a manifest under ``version``. Saving the same content again is a no-op; other content under a
    saved version is refused: a frozen manifest is never silently reassigned."""
    sha = digest(manifest)
    mg = derived.mongo_of(conn)
    have = mg.manifest_sha(conn, version)
    if have:
        if have == sha:
            return
        raise FrozenManifest(f"{version} is frozen; a changed split needs a new version (next: {next_version(conn)})")
    return mg.manifest_insert(conn, version, manifest["schema"], manifest.get("parent"), sha, manifest["summary"], canonical_json(manifest),
                              manifest.get("groups") or {})


def load(conn, version: str) -> dict:
    body = derived.mongo_of(conn).manifest_body(conn, version)
    if not body:
        raise KeyError(f"no split manifest {version}")
    return json.loads(body)


def record_exposure(conn, model_id: str, manifest_version: str, group_ids) -> None:
    """Remember that a model trained on these groups under a manifest version."""
    mg = derived.mongo_of(conn)
    return mg.exposure_add(conn, model_id, manifest_version, sorted(set(group_ids)))


# -- contamination of a frozen validation set -----------------------------------------------------------------------

def contamination_report(conn, parent: dict, new: dict, graph, new_version: str) -> dict:
    """What a new link does to the parent manifest's validation set. A validation group of the parent
    that the new graph ties to a group some model trained on is contaminated for that model: the report
    names the model, the two groups and the edges that tie them, and the contamination is recorded in
    ``model_contamination``. Nothing is reassigned."""
    parent_version = new.get("parent")
    exposure: dict[str, set[str]] = defaultdict(set)
    mg = derived.mongo_of(conn)
    for model_id, gid in mg.exposure_of(conn, parent_version):
        exposure[model_id].add(gid)
    models: dict[str, dict] = {}
    contaminated, edges_out = [], []
    for gid, g in new["groups"].items():
        old = {group_of(parent, m) for m in g["members"] if group_of(parent, m)}
        if len(old) < 2:
            continue
        val_groups = sorted(p for p in old if parent["groups"][p]["split"] == "val")
        if not val_groups:
            continue
        contaminated.append(gid)
        member_set = set(g["members"])
        for e in graph.edges:
            if e.src in member_set and e.dst in member_set and group_of(parent, e.src) != group_of(parent, e.dst):
                edges_out.append({"src": e.src, "dst": e.dst, "type": e.type, "confidence": e.confidence})
        for model_id, trained in sorted(exposure.items()):
            linked = sorted((old & trained) - set(val_groups))
            if not linked:
                continue
            entry = models.setdefault(model_id, {"contaminated_val_groups": [], "linked_trained_groups": []})
            entry["contaminated_val_groups"] = sorted(set(entry["contaminated_val_groups"]) | set(val_groups))
            entry["linked_trained_groups"] = sorted(set(entry["linked_trained_groups"]) | set(linked))
            rows = [(model_id, new_version, v, l, json.dumps({"new_group": gid}, sort_keys=True)) for v in val_groups for l in linked]
            mg.contamination_add(conn, rows)
    unique_edges = sorted({json.dumps(e, sort_keys=True) for e in edges_out})
    return {"parent": parent_version, "new": new_version, "contaminated_groups": sorted(contaminated),
            "models": models, "linking_edges": [json.loads(e) for e in unique_edges]}
