"""Leakage graph: which issue records share a vulnerability, a fix, code or a repository.

Records that are connected must stay together when the data is split, otherwise the same
vulnerability (or the same code) sits in train and in val and the validation score is inflated.
The graph has one node per issue record and an edge only where there is evidence:

* ``alias``            a shared advisory or CVE id (also the same advisory in two ecosystems);
* ``fix_family``       the same fix commit, a cherry-picked backport, or an equal patch id;
* ``exact_hash``       identical code once comments and layout are ignored (either side of a pair);
* ``near_clone``       a near-identical function (token 5-gram MinHash, Jaccard at or above the
  configured threshold) that is fixed by the same changed lines;
* ``same_repository``  the same repository (forks are the same repository when they share a fix
  commit); this is what keeps a monorepo together;
* ``derivative``       a derived record (a variant) inherits its parent.

A shared CWE, a shared API name or a generic helper never links two records. A near clone that is
only similar, or similar without the same fix, is a review candidate (``near_clone_review``): it is
reported and never merges two groups. Code that appears in many repositories is boilerplate and
links nothing. The connected components are the issue groups; their ids depend only on membership.

The graph is built from plain ``Node`` and ``Item`` values (so it is testable without a database);
``load_store`` reads them from the database and ``save`` / ``load`` keep a graph in
the derived store under a version.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from app.backend import compute
from app.backend.pipeline import ecosystems, funcsplit

VERSION = "leakage-1"
EDGE_TYPES = ("alias", "fix_family", "exact_hash", "near_clone", "same_repository", "derivative")
REVIEW_TYPE = "near_clone_review"

NEAR_CLONE_JACCARD = 0.9      # contract default (leakage.near_clone_jaccard)
REVIEW_JACCARD = 0.7          # at or above this a near clone that is not accepted is a review candidate
MIN_EXACT_CHARS = 80          # shorter code is too generic to link two records
MIN_TOKENS = 40               # near-clone detection needs a function of some size
BOILERPLATE_REPOS = 8         # code seen in more repositories than this links nothing
MAX_BUCKET = 300              # an LSH bucket larger than this is boilerplate or duplicates
OVERSIZED = 25                # components with more records than this are listed in the audit
MIN_PATCH_CHARS = 40          # smaller patches are too generic for a patch id to mean anything
NUM_PERM, BANDS, ROWS, SHINGLE = 128, 32, 4, 5

_rng = random.Random(17)      # fixed: signatures and bucket keys are the same on every build
_A = np.array([_rng.getrandbits(64) | 1 for _ in range(NUM_PERM)], dtype=np.uint64)
_B = np.array([_rng.getrandbits(64) for _ in range(NUM_PERM)], dtype=np.uint64)
_P = np.array([_rng.getrandbits(64) | 1 for _ in range(SHINGLE)], dtype=np.uint64)


@dataclass(frozen=True)
class Node:
    node_id: str
    ecosystem: str | None = None
    package_key: str | None = None
    aliases: tuple[str, ...] = ()
    fix_shas: tuple[str, ...] = ()
    cherry_picks: tuple[str, ...] = ()      # shas the fix commit messages say they were picked from
    patch_ids: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()             # repository identities (ecosystems.repository_identity)
    parent: str | None = None               # a derived record names the record it derives from


@dataclass(frozen=True)
class Item:
    """One vulnerable / fixed code pair of a node."""
    node_id: str
    key: str
    language: str | None
    before: str
    after: str
    repo: str | None = None


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    type: str
    confidence: float
    evidence: dict

    def as_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "type": self.type, "confidence": self.confidence,
                "evidence": self.evidence}


@dataclass
class Graph:
    version: str = VERSION
    edges: list[Edge] = field(default_factory=list)
    review: list[Edge] = field(default_factory=list)     # near-clone candidates; never merge groups
    groups: dict[str, str] = field(default_factory=dict)  # node id -> issue group id
    oversized: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    @property
    def components(self) -> list[list[str]]:
        by: dict[str, list[str]] = defaultdict(list)
        for node, gid in self.groups.items():
            by[gid].append(node)
        return [sorted(by[g]) for g in sorted(by)]

    def to_json(self) -> str:
        return json.dumps({"version": self.version, "edges": [e.as_dict() for e in self.edges],
                           "review": [e.as_dict() for e in self.review], "groups": self.groups,
                           "oversized": self.oversized, "stats": self.stats, "params": self.params},
                          sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "Graph":
        d = json.loads(text)
        mk = lambda e: Edge(e["src"], e["dst"], e["type"], e["confidence"], e["evidence"])
        return cls(d.get("version", VERSION), [mk(e) for e in d.get("edges", [])], [mk(e) for e in d.get("review", [])],
                   d.get("groups", {}), d.get("oversized", []), d.get("stats", {}), d.get("params", {}))


# -- small evidence helpers -----------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_PICK = re.compile(r"(?:cherry[- ]picked(?:\s+from)?(?:\s+commit)?|back-?port(?:ed)?(?:\s+of|\s+from)?(?:\s+commit)?)"
                   r"\s*:?\s+([0-9a-f]{7,40})\b", re.I)


def cherry_picks_of(message: str | None) -> tuple[str, ...]:
    """The commits a fix says it was picked or backported from (7 to 40 hex digits, in order)."""
    out: list[str] = []
    for m in _PICK.finditer(message or ""):
        sha = m.group(1).lower()
        if sha not in out:
            out.append(sha)
    return tuple(out)


def patch_id(diff: str | None) -> str:
    """A stable id of what a patch changes: the file paths and the changed lines with whitespace
    removed; hunk headers, context lines and commit metadata do not count, so a backport with other
    line numbers or other surrounding code has the same id."""
    h = hashlib.sha1()
    for line in (diff or "").splitlines():
        if line.startswith(("--- ", "+++ ")):
            h.update(f"{line[:3]} {re.sub(r'^[ab]/', '', line[4:].strip())}\n".encode("utf-8"))
        elif line[:1] in ("+", "-") and not line.startswith(("+++", "---")):
            h.update((line[0] + _WS.sub("", line[1:]) + chr(10)).encode("utf-8"))
    return h.hexdigest()


def code_key(lang: str | None, code: str | None) -> str:
    """One key for code that differs only in comments and layout."""
    return hashlib.sha1("\x1f".join(funcsplit.tokens(lang, code)).encode("utf-8")).hexdigest()


_TRIVIAL = re.compile(r"^[\W_]*$")


def changed_lines(before: str, after: str) -> frozenset[int]:
    """Hashes of the non-trivial lines a fix adds or removes (whitespace collapsed)."""
    norm = lambda s: Counter(re.sub(r"\s+", " ", l).strip() for l in (s or "").splitlines())
    b, a = norm(before), norm(after)
    out = set()
    for sign, lines in (("+", a - b), ("-", b - a)):
        for line in lines:
            if len(line) >= 12 and not _TRIVIAL.match(line):
                out.add(zlib.crc32(f"{sign}{line}".encode("utf-8")))
    return frozenset(out)


def _share_fix(a: frozenset[int], b: frozenset[int]) -> int:
    """How many changed lines two fixes share, when that is at least half of the smaller fix (else 0)."""
    inter = len(a & b)
    return inter if inter and inter / min(len(a), len(b)) >= 0.5 else 0


# -- accumulating edges ----------------------------------------------------------------------------------

class _Edges:
    def __init__(self) -> None:
        self._by: dict[tuple[str, str, str], list] = {}

    def add(self, a: str, b: str, kind: str, confidence: float, **evidence) -> None:
        if a == b:
            return
        src, dst = sorted((a, b))
        slot = self._by.setdefault((src, dst, kind), [confidence, {}])
        slot[0] = max(slot[0], confidence)
        for k, v in evidence.items():
            vals = slot[1].setdefault(k, [])
            for x in (v if isinstance(v, (list, tuple, set)) else [v]):
                if x not in vals:
                    vals.append(x)
                    vals.sort()

    def chain(self, members, kind: str, confidence: float, **evidence) -> None:
        ms = sorted(set(members))
        for a, b in zip(ms, ms[1:]):
            self.add(a, b, kind, confidence, **evidence)

    def edges(self, kinds=None) -> list[Edge]:
        order = {t: i for i, t in enumerate((*EDGE_TYPES, REVIEW_TYPE))}
        return [Edge(s, d, k, c, ev) for (s, d, k), (c, ev) in
                sorted(self._by.items(), key=lambda kv: (order.get(kv[0][2], 99), kv[0][0], kv[0][1]))
                if kinds is None or k in kinds]


class _Union:
    def __init__(self, items) -> None:
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


# -- the edge builders -----------------------------------------------------------------------------------

def _alias_edges(nodes: list[Node], acc: _Edges) -> None:
    by: dict[str, set[str]] = defaultdict(set)
    for n in nodes:
        for a in (n.node_id.split("@")[0], *n.aliases):
            if a and a.strip():
                by[a.strip().upper()].add(n.node_id)
    for alias, members in by.items():
        if len(members) > 1:
            acc.chain(members, "alias", 1.0, alias=alias)


def _fix_edges(nodes: list[Node], acc: _Edges) -> None:
    by_sha: dict[str, set[str]] = defaultdict(set)
    by_patch: dict[str, set[str]] = defaultdict(set)
    for n in nodes:
        for s in n.fix_shas:
            by_sha[s.strip().lower()].add(n.node_id)
        for p in n.patch_ids:
            by_patch[p].add(n.node_id)
    for sha, members in by_sha.items():
        if len(members) > 1:
            acc.chain(members, "fix_family", 1.0, sha=sha)
    for pid, members in by_patch.items():
        if len(members) > 1:
            acc.chain(members, "fix_family", 0.9, patch_id=pid)
    full = sorted(by_sha)
    for n in nodes:                                  # a backport names the commit it was picked from
        for pick in n.cherry_picks:
            pick = pick.strip().lower()
            if len(pick) < 7:
                continue
            for sha in (s for s in full if s.startswith(pick)):
                for other in by_sha[sha] - {n.node_id}:
                    acc.add(n.node_id, other, "fix_family", 0.95, cherry_pick_of=sha)


def _repo_edges(nodes: list[Node], acc: _Edges) -> None:
    repos = {r.lower() for n in nodes for r in n.repos}
    union = _Union(repos)
    by_sha: dict[str, set[str]] = defaultdict(set)
    for n in nodes:
        for s in n.fix_shas:
            by_sha[s.strip().lower()].update(r.lower() for r in n.repos)
    for rs in by_sha.values():                      # a fork shares its commits with the original
        rs = sorted(rs)
        for other in rs[1:]:
            union.union(rs[0], other)
    members: dict[str, set[str]] = defaultdict(set)
    size: Counter = Counter(union.find(r) for r in repos)
    for n in nodes:
        for r in n.repos:
            members[union.find(r.lower())].add(n.node_id)
    for root, ms in members.items():
        if len(ms) > 1:
            acc.chain(ms, "same_repository", 0.8, repository=root,
                      via="shared_commit" if size[root] > 1 else "identity")


def _lineage_edges(nodes: list[Node], acc: _Edges, stats: Counter) -> None:
    ids = {n.node_id for n in nodes}
    for n in nodes:
        if n.parent:
            if n.parent in ids:
                acc.add(n.node_id, n.parent, "derivative", 1.0, parent=n.parent)
            else:
                stats["orphan_parent"] += 1


def _shingles(toks: list[str]) -> np.ndarray:
    h = np.fromiter((zlib.crc32(t.encode("utf-8")) for t in toks), dtype=np.uint64, count=len(toks))
    n = len(h) - SHINGLE + 1
    s = np.zeros(n, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for j in range(SHINGLE):
            s += (h[j:j + n] + np.uint64(1)) * _P[j]
    return np.unique(s)


def _signature(sh: np.ndarray) -> np.ndarray:
    out = np.empty(NUM_PERM, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for lo in range(0, NUM_PERM, 32):          # blocks of permutations bound the memory
            m = sh[None, :] * _A[lo:lo + 32, None] + _B[lo:lo + 32, None]
            m ^= m >> np.uint64(29)
            out[lo:lo + 32] = m.min(axis=1)
    return out


MIN_PARALLEL_ITEMS = 400            # fewer items than this are done in this process: starting workers would cost more than it saves


def _features(task):
    """Everything the code edges need from one item, each token stream computed once: the hash and size of each side's tokens, the token
    count of the vulnerable side, its MinHash signature (when it has enough tokens) and its changed lines. Pure, so workers can run it."""
    it, min_tokens, *options = task
    before_tokens = funcsplit.tokens(it.language, it.before)
    after_tokens = funcsplit.tokens(it.language, it.after)
    before_joined, after_joined = "\x1f".join(before_tokens), "\x1f".join(after_tokens)
    before_hash = hashlib.sha1(before_joined.encode("utf-8"))
    sig = ((np.fromiter((zlib.crc32(t.encode('utf-8')) for t in before_tokens), dtype=np.uint64)
            if options and options[0] else _signature(_shingles(before_tokens)))
           if len(before_tokens) >= min_tokens else None)
    return (before_hash.hexdigest(), len(before_joined), hashlib.sha1(after_joined.encode("utf-8")).hexdigest(), len(after_joined),
            len(before_tokens), before_hash.digest(), sig, changed_lines(it.before, it.after) if sig is not None else None)


def _code_edges(items: list[Item], acc: _Edges, review: _Edges, stats: Counter, *, near_clone_jaccard: float,
                review_jaccard: float, min_exact_chars: int, boilerplate_repos: int, min_tokens: int, workers=None) -> None:
    from app.backend.pipeline.acceleration import runtime
    accelerated = runtime.accelerates('minhash', len(items))
    feats = compute.pmap(_features, [(it, min_tokens, accelerated) for it in items], workers=workers, chunksize=32, min_items=MIN_PARALLEL_ITEMS)
    if accelerated:
        eligible = [i for i, feature in enumerate(feats) if feature[6] is not None]
        batch_size = runtime.batch_size('minhash')
        for start in range(0, len(eligible), batch_size):
            indices = eligible[start:start+batch_size]
            signatures = runtime.signatures([feats[i][6] for i in indices], _A, _B, powers=_P)
            for i, signature in zip(indices, signatures):
                feats[i] = (*feats[i][:6], signature, feats[i][7])
    # exact: identical code once comments and layout are ignored, either side of a pair
    seen: dict[str, dict[str, set]] = defaultdict(dict)
    repos_of: dict[str, set[str]] = defaultdict(set)
    for it, (before_key, before_chars, after_key, after_chars, _n, _d, _sig, _cl) in zip(items, feats):
        for side, key, chars in (("before", before_key, before_chars), ("after", after_key, after_chars)):
            if chars < min_exact_chars:
                continue
            seen[key].setdefault(it.node_id, set()).add(side)
            repos_of[key].add(it.repo or it.node_id)
    for key, nodes in sorted(seen.items()):
        if len(nodes) < 2:
            continue
        if len(repos_of[key]) > boilerplate_repos:
            stats["boilerplate_skipped"] += 1
            continue
        acc.chain(nodes, "exact_hash", 1.0, code_key=key[:16])

    # near clones of the vulnerable side: MinHash with LSH candidates, then the same changed lines
    sigs, meta, done = [], [], set()
    for it, (_bk, _bc, _ak, _ac, n_tokens, before_digest, sig, changed) in zip(items, feats):
        if n_tokens < min_tokens:
            stats["near_clone_too_small"] += 1
            continue
        sig_key = (it.node_id, before_digest)
        if sig_key in done:
            continue
        done.add(sig_key)
        sigs.append(sig)
        meta.append((it.node_id, it.repo or it.node_id, changed))
    buckets: dict[tuple[int, bytes], list[int]] = defaultdict(list)
    for i, sig in enumerate(sigs):
        for b in range(BANDS):
            buckets[(b, sig[b * ROWS:(b + 1) * ROWS].tobytes())].append(i)
    pairs: set[tuple[int, int]] = set()
    for _, idx in sorted(buckets.items()):
        if len(idx) < 2:
            continue
        if len(idx) > MAX_BUCKET or len({meta[i][1] for i in idx}) > boilerplate_repos:
            stats["boilerplate_skipped"] += 1
            continue
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                if meta[idx[x]][0] != meta[idx[y]][0]:
                    pairs.add((idx[x], idx[y]))
    ordered_pairs = sorted(pairs)
    estimates = []
    for start in range(0, len(ordered_pairs), 1024):
        batch = ordered_pairs[start:start+1024]
        estimates.extend(runtime.comparisons([sigs[i] for i, j in batch], [sigs[j] for i, j in batch]))
    for (i, j), equal_count in zip(ordered_pairs, estimates):
        est = round(equal_count / NUM_PERM, 3)
        if est < review_jaccard:
            continue
        shared = _share_fix(meta[i][2], meta[j][2])
        if est >= near_clone_jaccard and shared:
            acc.add(meta[i][0], meta[j][0], "near_clone", est, jaccard=est, shared_changed_lines=shared)
        else:
            review.add(meta[i][0], meta[j][0], REVIEW_TYPE, est, jaccard=est, shared_changed_lines=shared)


# -- the graph -------------------------------------------------------------------------------------------

def build_graph(nodes, items=(), *, version: str = VERSION, near_clone_jaccard: float = NEAR_CLONE_JACCARD,
                review_jaccard: float = REVIEW_JACCARD, same_repository_edges: bool = True,
                min_exact_chars: int = MIN_EXACT_CHARS, boilerplate_repos: int = BOILERPLATE_REPOS,
                oversized: int = OVERSIZED, min_tokens: int = MIN_TOKENS, workers=None) -> Graph:
    """The leakage graph of ``nodes`` (and the code ``items`` of their pairs). Deterministic: the
    same input in any order gives the same edges and the same group ids."""
    nodes = sorted(nodes, key=lambda n: n.node_id)
    items = sorted(items, key=lambda i: (i.node_id, i.key))
    stats: Counter = Counter()
    acc, review = _Edges(), _Edges()
    _alias_edges(nodes, acc)
    _fix_edges(nodes, acc)
    if same_repository_edges:
        _repo_edges(nodes, acc)
    _lineage_edges(nodes, acc, stats)
    _code_edges(items, acc, review, stats, near_clone_jaccard=near_clone_jaccard, review_jaccard=review_jaccard,
                min_exact_chars=min_exact_chars, boilerplate_repos=boilerplate_repos, min_tokens=min_tokens, workers=workers)
    edges, review_edges = acc.edges(), review.edges()

    union = _Union(n.node_id for n in nodes)
    for e in edges:                                  # review candidates never merge groups
        union.union(e.src, e.dst)
    members: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        members[union.find(n.node_id)].append(n.node_id)
    groups, big = {}, []
    by_id = {n.node_id: n for n in nodes}
    for ms in members.values():
        gid = "ig_" + hashlib.sha1("\n".join(sorted(ms)).encode("utf-8")).hexdigest()[:16]
        for m in ms:
            groups[m] = gid
        if len(ms) > oversized:
            big.append({"group_id": gid, "size": len(ms),
                        "repositories": sorted({r for m in ms for r in by_id[m].repos})[:10],
                        "ecosystems": sorted({by_id[m].ecosystem for m in ms if by_id[m].ecosystem})})
    big.sort(key=lambda d: (-d["size"], d["group_id"]))

    sizes = Counter(groups.values())
    summary = {"nodes": len(nodes), "items": len(items), "components": len(sizes),
               "singletons": sum(1 for v in sizes.values() if v == 1),
               "largest_component": max(sizes.values(), default=0),
               "edges": dict(sorted(Counter(e.type for e in edges).items())),
               "near_clone_review": len(review_edges), **dict(sorted(stats.items()))}
    params = {"near_clone_jaccard": near_clone_jaccard, "review_jaccard": review_jaccard,
              "same_repository_edges": same_repository_edges, "min_exact_chars": min_exact_chars,
              "boilerplate_repos": boilerplate_repos, "min_tokens": min_tokens, "oversized": oversized,
              "minhash": {"permutations": NUM_PERM, "bands": BANDS, "rows": ROWS, "shingle": SHINGLE, "seed": 17}}
    return Graph(version, edges, review_edges, dict(sorted(groups.items())), big, summary, params)


# -- persistence in the derived store -------------------------------------------------------------------------------

def save(conn, graph: Graph) -> None:
    """Store the graph under its version, replacing an earlier build of the same version."""
    from app.backend.pipeline import derived
    mg = derived.mongo_of(conn)
    return mg.leakage_save(conn, graph.version, [(e.src, e.dst, e.type, e.evidence, e.confidence) for e in (*graph.edges, *graph.review)],
                           graph.groups, {"stats": graph.stats, "params": graph.params, "oversized": graph.oversized})


def load(conn, version: str) -> Graph:
    from app.backend.pipeline import derived
    mg = derived.mongo_of(conn)
    rows, groups, extra = mg.leakage_load(conn, version)
    mk = lambda r: Edge(r[0], r[1], r[2], r[4], r[3])
    order = {t: i for i, t in enumerate((*EDGE_TYPES, REVIEW_TYPE))}
    edges = sorted((mk(r) for r in rows if r[2] != REVIEW_TYPE), key=lambda e: (order.get(e.type, 99), e.src, e.dst))
    review = sorted((mk(r) for r in rows if r[2] == REVIEW_TYPE), key=lambda e: (e.src, e.dst))
    return Graph(version, edges, review, dict(sorted(groups.items())), extra.get("oversized", []), extra.get("stats", {}), extra.get("params", {}))


# -- reading nodes and items from the working store -----------------------------------------------------------

def load_store(*, min_patch_chars: int = MIN_PATCH_CHARS) -> tuple[list[Node], list[Item]]:
    """Nodes (every advisory of kind vulnerability) and code items (every function or hunk pair, and a
    file pair when it has none, quarantined code included: it can leak just the same)."""
    from app.backend.pipeline import codestore, store
    recs = store.vulnerability_records(("key", "osv_id", "ecosystem", "package", "aliases", "cve_id", "fix_commits"))
    keys = sorted({k for r in recs for k in codestore._record_commit_keys(r)})
    commits = {c["commit_key"]: c for c in codestore.commits_basic(keys, ("commit_key", "sha", "repo_url", "commit_message"))}
    pairs = codestore.usable_pairs_of_commits(keys, ("pair_key", "commit_key", "granularity", "language", "file_path", "code_before", "code_after", "diff"))
    return build_nodes(recs, commits, pairs, min_patch_chars=min_patch_chars)


def build_nodes(recs: list[dict], commits: dict[str, dict], pairs: list[dict], *,
                min_patch_chars: int = MIN_PATCH_CHARS) -> tuple[list[Node], list[Item]]:
    """Nodes and code items from rows already read: ``recs`` (key, osv_id, ecosystem, package, aliases, cve_id,
    fix_commits), ``commits`` by commit key (sha, repo_url, commit_message) and the code ``pairs``."""
    from app.backend.pipeline import codestore
    by_commit: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        if p.get("commit_key"):
            by_commit[p["commit_key"]].append(p)

    def units(rows):
        sub = {r["file_path"] for r in rows if r["granularity"] in ("function", "hunk")}
        return [r for r in rows if r["granularity"] in ("function", "hunk")
                or (r["granularity"] == "file" and r["file_path"] not in sub)]

    def commit_patch(ck: str) -> str | None:
        diffs = [r["diff"] for r in sorted(by_commit.get(ck, []), key=lambda r: r["file_path"] or "")
                 if r["granularity"] == "file" and r["diff"]]
        text = "\n".join(diffs)
        changed = sum(len(re.sub(r"\s+", "", l[1:])) for l in text.splitlines()
                      if l[:1] in "+-" and not l.startswith(("+++", "---")))
        return patch_id(text) if changed >= min_patch_chars else None

    nodes, items = [], []
    for r in recs:
        cks = [k for k in codestore._record_commit_keys(r) if k in commits]
        aliases = [*json.loads(r["aliases"] or "[]"), r["cve_id"], r["osv_id"]]
        shas = tuple(sorted({(commits[k]["sha"] or "").lower() for k in cks if commits[k]["sha"]}))
        repos = tuple(sorted({i for k in cks if (i := ecosystems.repository_identity(commits[k]["repo_url"]))}))
        picks = tuple(sorted({p for k in cks for p in cherry_picks_of(commits[k]["commit_message"])}))
        pids = tuple(sorted({p for k in cks if (p := commit_patch(k))}))
        nodes.append(Node(node_id=r["key"], ecosystem=r["ecosystem"],
                          package_key=ecosystems.package_key(r["ecosystem"], r["package"]),
                          aliases=tuple(sorted({a for a in aliases if a})), fix_shas=shas, cherry_picks=picks,
                          patch_ids=pids, repos=repos))
        for k in cks:
            repo = ecosystems.repository_identity(commits[k]["repo_url"])
            for p in units(by_commit.get(k, [])):
                items.append(Item(node_id=r["key"], key=p["pair_key"], language=p["language"],
                                  before=p["code_before"] or "", after=p["code_after"] or "", repo=repo))
    return nodes, items
