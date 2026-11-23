"""Matching the functions of two versions of a file (used by ``funcsplit``).

Input is two tables of units (anything with ``key``, ``qual``, ``name``, ``scope``, ``signature``, ``arity``, ``lines``, ``shash`` and ``nhash``) and a way to get the content tokens of a unit.
The result is a list of matches with a kind, a confidence and the signals it rests on, the non-bijective cases kept as explicit links (a function split into several, several merged into
one, candidates that cannot be told apart), and the units left without a counterpart (added or removed).

Stages, cheapest evidence first:

1. **Same qualified name, once on each side**: the same function (``same``), whatever its signature or body did.
2. **A name that repeats** (overloads, property setters, a function defined twice): every before x after combination of the group is scored on signature, body and position, and the best
   non-conflicting assignment is kept. A pair that has a close rival is marked with its alternatives and a lower confidence.
3. **What is left**: first the units whose syntax tree is identical once their own name is masked (a pure rename or move), then units with alike bodies. Candidates come from an inverted
   index over the rarer content tokens (no all-pairs scan), the score is a Dice coefficient of the content tokens weighted by their rarity in the file (idf), with the function's own name masked,
   and a pair is accepted only when it is the best for both units, scores at least ``RENAME_MIN`` and leads its rivals by ``AMBIG_MARGIN``; otherwise it is recorded as an ``ambiguous`` link.
4. **Splits and merges**: a leftover unit whose tokens are largely contained in two or more leftover units on the other side (or the reverse) is a ``split`` (or ``merge``) link, not a pair.

Nothing here decides what enters a dataset: the confidence and the signals are inputs of the quality policy (``ingest/splitquality.py``). The numbers below were set with the mutation benchmark
(``tools/funcsplit_mutation_bench.py``).
"""
from __future__ import annotations

import difflib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable

RENAME_MIN = 0.62        # body similarity below which two units are never taken for one function under two names
STRONG = 0.80            # from here a different signature does not stand in the way of a rename
AMBIG_MARGIN = 0.08      # a pair must lead its closest rival by this much, else the choice is recorded as ambiguous
REPEAT_MIN = 0.30        # repeated names: the lowest combined score that still pairs two occurrences
MAX_TOKENS = 4000        # tokens of a unit that count (a larger unit is compared by its first ones)
MAX_REPEAT_PAIRS = 400   # most before x after combinations scored for one repeated name; beyond it occurrences are paired by position
MAX_CANDIDATES = 8       # most candidates scored per unit
SPLIT_PART = 0.15        # share of a unit's tokens a part must contain to be considered a part of a split (each part must then also add SPLIT_GAIN)
SPLIT_COVER = 0.50       # share of a unit's tokens the parts of a split together must contain (for a merge: the share of each old unit the new one contains is at least 0.75)
MERGE_PART = 0.75
SPLIT_FIRST = 0.85       # a split or merge that explains this much of the code is reported before any one-to-one pairing, unless some single unit already matches this well
MOVED_MIN = 0.35         # a unit that kept its name and parameter list in another scope needs only this much body similarity...
MOVED_COVER = 0.80       # ...or this much of its smaller body contained in the larger (code added around the old body)
MIN_SPLIT_TOKENS = 8     # smaller units are not tested for splits and merges (everything is contained in everything)
SPLIT_GAIN = 0.15        # each part of a split or merge must add at least this share of the unit's tokens (two copies of one body are not a split)
MIN_BODY_TOKENS = 5      # a body with fewer content tokens is matched only when its syntax tree is identical: similarity of a few tokens means nothing
TRIVIAL_TOKENS = 3       # a body this short says little about identity


@dataclass
class Match:
    before: str                              # key in the before table
    after: str
    kind: str                                # same | same_repeated | positional | renamed | moved
    confidence: float
    signals: dict = field(default_factory=dict)
    alternatives: list[dict] = field(default_factory=list)      # [{"before", "after", "score"}] that scored within the margin of this pair


@dataclass
class Link:
    kind: str                                # split | merge | ambiguous
    before: list[str]
    after: list[str]
    confidence: float
    signals: dict = field(default_factory=dict)


@dataclass
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    unmatched_before: list[str] = field(default_factory=list)
    unmatched_after: list[str] = field(default_factory=list)


# ── similarity ───────────────────────────────────────────────────────────────────────────────

def idf_weights(docs: list[list[str]]) -> dict[str, float]:
    """``log(1 + N / df)`` for every token of ``docs`` (df: in how many documents it occurs): a token that is in every function says little, a rare one says much."""
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    return {t: math.log(1 + n / c) for t, c in df.items()}


_NO_WEIGHTS: dict[str, float] = {}          # every token weighs 1


def dice_many(pairs, weights):
    """Batch integer overlap on CUDA; sum the original floats in original order."""
    from app.backend.pipeline.acceleration import runtime
    overlaps = runtime.overlaps(pairs)
    values = []
    for (a, b), intersections in zip(pairs, overlaps):
        ca, cb = Counter(a), Counter(b)
        total = sum(c * weights.get(t, 1.0) for t, c in ca.items()) + sum(c * weights.get(t, 1.0) for t, c in cb.items())
        inter = sum(c * weights.get(t, 1.0) for (t, _), c in zip(ca.items(), intersections) if t in cb)
        values.append(2 * inter / total if total else (1.0 if not a and not b else 0.0))
    return values


def dice(a: list[str], b: list[str], w: dict[str, float]) -> float:
    """The weighted Dice coefficient of two token multisets, in [0, 1]; two empty lists are equal."""
    ca, cb = Counter(a), Counter(b)
    total = sum(c * w.get(t, 1.0) for t, c in ca.items()) + sum(c * w.get(t, 1.0) for t, c in cb.items())
    if total == 0:
        return 1.0 if not a and not b else 0.0
    inter = sum(min(c, cb[t]) * w.get(t, 1.0) for t, c in ca.items() if t in cb)
    return 2 * inter / total


def _cover(part: Counter, whole: Counter, w: dict[str, float]) -> float:
    """The share of the weight of ``part`` that ``whole`` contains."""
    total = sum(c * w.get(t, 1.0) for t, c in part.items())
    if total == 0:
        return 0.0
    return sum(min(c, whole[t]) * w.get(t, 1.0) for t, c in part.items() if t in whole) / total


class _Tokens:
    """Token lists of units, computed when first asked for."""

    def __init__(self, units: dict, get: Callable[[str], list[str]]):
        self._get, self._cache = get, {}
        self.units = units

    def __call__(self, key: str) -> list[str]:
        t = self._cache.get(key)
        if t is None:
            t = self._cache[key] = self._get(key)[:MAX_TOKENS]
        return t


def _sig_relation(x, y) -> str:
    """``same`` (the parameter lists are equal), ``arity`` (they differ but have as many parameters) or ``changed``."""
    if x.signature == y.signature:
        return "same"
    return "arity" if x.arity is not None and x.arity == y.arity else "changed"


def _sig_signals(rel: str) -> dict:
    return {"signature": "same" if rel == "same" else "changed", "arity_same": rel != "changed"}


def match_units(b_units: dict, a_units: dict, b_tokens: Callable[[str], list[str]], a_tokens: Callable[[str], list[str]]) -> MatchResult:
    """Match the units of two versions. ``b_units`` / ``a_units`` map keys to unit descriptions in file order; the token functions give the content tokens of a unit."""
    res = MatchResult()
    tb, ta = _Tokens(b_units, b_tokens), _Tokens(a_units, a_tokens)
    state = {"w": None}

    def weights() -> dict[str, float]:
        if state["w"] is None:
            state["w"] = idf_weights([tb(k) for k in b_units] + [ta(k) for k in a_units])
        return state["w"]

    group_b: dict[str, list[str]] = defaultdict(list)
    group_a: dict[str, list[str]] = defaultdict(list)
    for k, u in b_units.items():
        group_b[u.qual].append(k)
    for k, u in a_units.items():
        group_a[u.qual].append(k)
    used_b: set[str] = set()
    used_a: set[str] = set()

    # stage 1: one on each side
    for qual, bs in group_b.items():
        as_ = group_a.get(qual)
        if as_ and len(bs) == 1 and len(as_) == 1:
            res.matches.append(_same(b_units[bs[0]], a_units[as_[0]], tb, ta, weights))
            used_b.add(bs[0])
            used_a.add(as_[0])

    # stage 2: a name that repeats
    for qual, bs in group_b.items():
        as_ = group_a.get(qual)
        if as_ and (len(bs) > 1 or len(as_) > 1):
            for m in _repeated(b_units, a_units, bs, as_, tb, ta, weights):
                res.matches.append(m)
                used_b.add(m.before)
                used_a.add(m.after)

    # stage 3 and 4: what is left
    left_b = [k for k in b_units if k not in used_b]
    left_a = [k for k in a_units if k not in used_a]
    if left_b and left_a:
        _rest(b_units, a_units, left_b, left_a, tb, ta, weights, res, used_b, used_a)
    res.unmatched_before = [k for k in b_units if k not in used_b]
    res.unmatched_after = [k for k in a_units if k not in used_a]
    border = {k: i for i, k in enumerate(b_units)}
    res.matches.sort(key=lambda m: (border[m.before], m.after))
    return res


def match_leftovers(b_units: dict, a_units: dict, b_tokens: Callable[[str], list[str]], a_tokens: Callable[[str], list[str]], allow: Callable[[str, str], bool] | None = None) -> MatchResult:
    """Stages 3 and 4 only, for units that have no counterpart in their own file: which of the removed units is which of the added ones, when they come from different files (a function
    moved between files in one commit). ``allow(removed key, added key)`` says which combinations may be paired at all; the units are told apart by an optional ``file`` attribute, and
    one with the same qualified name and parameter list as a unit of another file counts as a candidate even though the in-file matcher ignores that case."""
    res = MatchResult()
    tb, ta = _Tokens(b_units, b_tokens), _Tokens(a_units, a_tokens)
    state = {"w": None}

    def weights() -> dict[str, float]:
        if state["w"] is None:
            state["w"] = idf_weights([tb(k) for k in b_units] + [ta(k) for k in a_units])
        return state["w"]
    used_b: set[str] = set()
    used_a: set[str] = set()
    if b_units and a_units:
        _rest(b_units, a_units, list(b_units), list(a_units), tb, ta, weights, res, used_b, used_a, allow)
    res.unmatched_before = [k for k in b_units if k not in used_b]
    res.unmatched_after = [k for k in a_units if k not in used_a]
    return res


# ── stage 1 ───────────────────────────────────────────────────────────────────────────────────

def _same(bu, au, tb: _Tokens, ta: _Tokens, weights) -> Match:
    sig = _sig_relation(bu, au)
    signals: dict = {"name": True, **_sig_signals(sig), "identical_body": bu.shash == au.shash}
    conf = 1.0
    if bu.shash != au.shash:
        body = dice(tb(bu.key), ta(au.key), _NO_WEIGHTS)
        signals["body"] = round(body, 3)
        conf = {"same": 0.99, "arity": 0.95, "changed": 0.92}[sig]
        if body < 0.1:
            conf -= 0.08                 # the name and nothing else survived: a rewrite, or another function that reuses the name
    return Match(bu.key, au.key, "same", round(conf, 3), signals)


# ── stage 2 ───────────────────────────────────────────────────────────────────────────────────

def _repeated(b_units: dict, a_units: dict, bs: list[str], as_: list[str], tb: _Tokens, ta: _Tokens, weights) -> list[Match]:
    n, m = len(bs), len(as_)
    if n * m > MAX_REPEAT_PAIRS:
        out = []
        for i in range(min(n, m)):
            out.append(Match(bs[i], as_[i], "positional", 0.5, {"name": True, "reason": "too_many_occurrences"}))
        return out
    cands = []
    for i, bk in enumerate(bs):
        for j, ak in enumerate(as_):
            bu, au = b_units[bk], a_units[ak]
            rel = _sig_relation(bu, au)
            sig = {"same": 1.0, "arity": 0.5, "changed": 0.0}[rel]
            body = 1.0 if bu.shash == au.shash else dice(tb(bk), ta(ak), weights())
            pos = 1.0 - abs(i / n - j / m)
            score = 0.45 * sig + 0.40 * body + 0.15 * pos
            cands.append((score, sig, body, pos, i, j, rel))
    cands.sort(key=lambda c: (-c[0], abs(c[4] - c[5]), c[4], c[5]))
    taken_i, taken_j, out = set(), set(), []
    for score, sig, body, pos, i, j, rel in cands:
        if i in taken_i or j in taken_j or score < REPEAT_MIN:
            continue
        rivals = [c for c in cands if (c[4] == i) != (c[5] == j) and not (c[4] == i and c[5] == j)]
        runner = max((c[0] for c in rivals), default=0.0)
        margin = score - runner
        alts = [{"before": bs[c[4]], "after": as_[c[5]], "score": round(c[0], 3)} for c in rivals if score - c[0] < AMBIG_MARGIN][:3]
        conf = min(0.99, 0.5 + 0.5 * score)
        if body == 1.0 and rel == "same":
            conf = 0.99
        if margin < AMBIG_MARGIN:
            conf = min(conf, 0.7)
        kind = "same_repeated" if (n == m or margin >= AMBIG_MARGIN or rel == "same") else "positional"
        out.append(Match(bs[i], as_[j], kind, round(conf, 3), {"name": True, **_sig_signals(rel), "body": round(body, 3), "position": round(pos, 3), "score": round(score, 3),
                                                              "margin": round(margin, 3), "identical_body": body == 1.0}, alts))
        taken_i.add(i)
        taken_j.add(j)
    return out


# ── stage 3 and 4 ─────────────────────────────────────────────────────────────────────────────

def _rest(b_units, a_units, left_b, left_a, tb: _Tokens, ta: _Tokens, weights, res: MatchResult, used_b: set, used_a: set, allow: Callable[[str, str], bool] | None = None) -> None:
    ambiguous_units: set[str] = set()         # units named in an ambiguous link: no later stage pairs them
    ok = (lambda r, a: True) if allow is None else allow
    order = {k: i for i, k in enumerate(b_units)}
    aorder = {k: i for i, k in enumerate(a_units)}

    # 3a: the same syntax tree under another name (a pure rename or move)
    by_hash: dict[str, list[str]] = defaultdict(list)
    for a in left_a:
        by_hash[a_units[a].nhash].append(a)
    rem_by_hash: dict[str, list[str]] = defaultdict(list)
    for r in left_b:
        rem_by_hash[b_units[r].nhash].append(r)
    for h, rs in rem_by_hash.items():
        as_ = by_hash.get(h, [])
        if allow is not None:
            rs = [r for r in rs if any(ok(r, a) for a in as_)]
            as_ = [a for a in as_ if any(ok(r, a) for r in rs)]
        if not as_ or not rs:
            continue
        if len(rs) == 1 and len(as_) == 1:
            r, a = rs[0], as_[0]
            trivial = len(tb(r)) < TRIVIAL_TOKENS
            res.matches.append(_moved(b_units[r], a_units[a], 1.0, 0.55 if trivial else 0.97, {"identical_body": True, "trivial_body": trivial}))
            used_b.add(r)
            used_a.add(a)
        else:
            res.links.append(Link("ambiguous", list(rs), list(as_), 0.5, {"identical_body": True, "reason": "several units with the same body"}))
            ambiguous_units.update(rs, as_)
    left_b = [k for k in left_b if k not in used_b and k not in ambiguous_units]
    left_a = [k for k in left_a if k not in used_a and k not in ambiguous_units]

    # 3a': the same name and parameter list in another scope (a method moved to another class, a nested function lifted out), once on each side
    leaf_b: dict[tuple, list[str]] = defaultdict(list)
    leaf_a: dict[tuple, list[str]] = defaultdict(list)
    for r in left_b:
        leaf_b[(b_units[r].name, b_units[r].signature)].append(r)
    for a in left_a:
        leaf_a[(a_units[a].name, a_units[a].signature)].append(a)
    w = weights()
    for key, rs in leaf_b.items():
        as_ = leaf_a.get(key, [])
        if len(rs) == 1 and len(as_) == 1 and ok(rs[0], as_[0]) and (b_units[rs[0]].qual != a_units[as_[0]].qual or getattr(b_units[rs[0]], "file", None) != getattr(a_units[as_[0]], "file", None)):
            r, a = rs[0], as_[0]
            s = dice(tb(r), ta(a), w)
            small, large = (Counter(tb(r)), Counter(ta(a))) if len(tb(r)) <= len(ta(a)) else (Counter(ta(a)), Counter(tb(r)))
            contained = _cover(small, large, w) if sum(small.values()) >= 2 else 0.0
            if s >= MOVED_MIN or contained >= MOVED_COVER:
                # a short body says little, but a distinctive name and parameter list kept as they were say a lot; a generic name does not
                trivial = min(len(tb(r)), len(ta(a))) < TRIVIAL_TOKENS and (len(b_units[r].name) < 6 or b_units[r].name.lower() in _generic())
                score = max(s, 0.7 * contained)
                res.matches.append(_moved(b_units[r], a_units[a], s, 0.5 if trivial else round(min(0.9, 0.55 + 0.4 * score), 3),
                                          {"body": round(s, 3), "contained": round(contained, 3), "signature": "same", "arity_same": True, "identical_body": False, "trivial_body": trivial}))
                used_b.add(r)
                used_a.add(a)
    left_b = [k for k in left_b if k not in used_b]
    left_a = [k for k in left_a if k not in used_a]

    # 3a'': the callers say so: a function that is called in the old code is called under the new name in the new code, and no other function carries either name
    if left_b and left_a and allow is None:
        for r, a, n in _call_site_renames(b_units, a_units, left_b, left_a, tb, ta, res):
            bu, au = b_units[r], a_units[a]
            rel = _sig_relation(bu, au)
            if rel == "changed":
                continue
            s = dice(tb(r), ta(a), w) if len(tb(r)) >= 2 and len(ta(a)) >= 2 else 0.0
            res.matches.append(_moved(bu, au, s, round(min(0.9, 0.7 + 0.2 * s), 3), {"body": round(s, 3), **_sig_signals(rel), "call_site_rename": n, "identical_body": False}))
            used_b.add(r)
            used_a.add(a)
        left_b = [k for k in left_b if k not in used_b]
        left_a = [k for k in left_a if k not in used_a]

    left_b = [k for k in left_b if len(tb(k)) >= MIN_BODY_TOKENS]
    left_a = [k for k in left_a if len(ta(k)) >= MIN_BODY_TOKENS]
    if not left_b or not left_a:
        return

    # 3b: alike bodies. Candidates through an inverted index over the rarer tokens (every unit when there are few).
    index: dict[str, list[str]] = defaultdict(list)
    cap = max(6, int(0.15 * len(left_a)) + 3)
    ctr_a = {a: Counter(ta(a)) for a in left_a}
    ctr_b = {r: Counter(tb(r)) for r in left_b}
    for a, c in ctr_a.items():
        for t in c:
            index[t].append(a)
    few = len(left_a) <= 24
    cand_a: dict[str, list[str]] = {}
    for r, cr in ctr_b.items():
        votes: Counter = Counter()
        for t in cr:
            owners = index.get(t)
            if owners and len(owners) <= cap:
                for a in owners:
                    votes[a] += w.get(t, 1.0)
        pool = left_a if few else [a for a, _ in sorted(votes.items(), key=lambda kv: (-kv[1], aorder[kv[0]]))[:MAX_CANDIDATES]]
        cand_a[r] = [a for a in pool if ok(r, a)] if allow is not None else pool
    scores: dict[tuple[str, str], float] = {}
    candidates = [(r, a) for r, pool in cand_a.items() for a in pool]
    for (r, a), s in zip(candidates, dice_many([(tb(r), ta(a)) for r, a in candidates], w)):
        if s >= RENAME_MIN * 0.75:
            scores[(r, a)] = s
    by_r: dict[str, list[tuple[float, str]]] = defaultdict(list)
    by_a: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for (r, a), s in scores.items():
        by_r[r].append((s, a))
        by_a[a].append((s, r))
    # a split or merge that accounts for (nearly) all of the code beats a one-to-one pairing that would leave half of it unexplained
    ambiguous_units |= _split_merge(left_b, left_a, cand_a, ctr_b, ctr_a, w, order, aorder, res, scores, SPLIT_FIRST, SPLIT_FIRST, SPLIT_FIRST)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0][0]], aorder[kv[0][1]]))
    for (r, a), s in ranked:
        if s < RENAME_MIN:
            break
        if r in used_b or a in used_a or r in ambiguous_units or a in ambiguous_units:
            continue
        rivals_r = [(x, k) for x, k in by_r[r] if k != a and k not in used_a]
        rivals_a = [(x, k) for x, k in by_a[a] if k != r and k not in used_b]
        runner = max([x for x, _ in rivals_r] + [x for x, _ in rivals_a], default=0.0)
        margin = s - runner
        bu, au = b_units[r], a_units[a]
        rel = _sig_relation(bu, au)
        if rel != "same" and s < STRONG and not (rel == "arity" and s >= RENAME_MIN + 0.05):
            continue                       # a different parameter list and a body only somewhat alike: not enough
        close = [k for x, k in rivals_r if s - x < AMBIG_MARGIN]
        close_a = [k for x, k in rivals_a if s - x < AMBIG_MARGIN]
        if margin < AMBIG_MARGIN:
            res.links.append(Link("ambiguous", [r] + close_a, [a] + close, round(s, 3), {"score": round(s, 3), "margin": round(margin, 3)}))
            ambiguous_units.update([r, a, *close, *close_a])
            continue
        conf = min(0.95, 0.4 + 0.6 * s) if rel == "same" else min(0.9, 0.3 + 0.6 * s)
        alts = [{"before": r, "after": k, "score": round(x, 3)} for x, k in rivals_r[:2]]
        res.matches.append(_moved(bu, au, s, round(conf, 3), {"body": round(s, 3), **_sig_signals(rel), "margin": round(margin, 3), "identical_body": False}, alts))
        used_b.add(r)
        used_a.add(a)

    # 4: what the strict pass did not take: one unit spread over several (split), or several gathered in one (merge); candidates come from the same index, never from all pairs
    left_b = [k for k in left_b if k not in used_b and k not in ambiguous_units]
    left_a = [k for k in left_a if k not in used_a and k not in ambiguous_units]
    _split_merge(left_b, left_a, cand_a, ctr_b, ctr_a, w, order, aorder, res, scores, SPLIT_COVER, MERGE_PART, None)


def _split_merge(left_b, left_a, cand_a, ctr_b, ctr_a, w, order, aorder, res: MatchResult, scores: dict, split_cover: float, merge_part: float, single_cap: float | None) -> set[str]:
    """Add the split and merge links among the given units and return the units they name. ``single_cap`` (the strict pass) leaves a unit alone when some single partner matches it at
    least that well."""
    in_a, in_b = set(left_a), set(left_b)
    claimed: set[str] = set()
    cand_b: dict[str, list[str]] = defaultdict(list)
    for r in left_b:
        for a in cand_a.get(r, ()):
            if a in in_a:
                cand_b[a].append(r)
    for r in left_b:
        cr = ctr_b[r]
        if sum(cr.values()) < MIN_SPLIT_TOKENS:
            continue
        if single_cap is not None and max((scores.get((r, a), 0.0) for a in cand_a.get(r, ())), default=0.0) >= single_cap:
            continue
        parts = [(a, _cover(cr, ctr_a[a], w)) for a in cand_a.get(r, ()) if a in in_a and a not in claimed]
        parts = sorted([p for p in parts if p[1] >= SPLIT_PART], key=lambda p: (-p[1], aorder[p[0]]))
        chosen, cover = _complementary(cr, [(a, ctr_a[a]) for a, _ in parts], w)
        if len(chosen) >= 2 and cover >= split_cover:
            res.links.append(Link("split", [r], sorted(chosen, key=lambda k: aorder[k]), round(cover, 3), {"cover": round(cover, 3)}))
            claimed.update([r, *chosen])
    for a in left_a:
        ca = ctr_a[a]
        if sum(ca.values()) < MIN_SPLIT_TOKENS or a in claimed:
            continue
        # a merge: two or more different old units that are each largely contained in this new one (new code may have been added around them)
        inside = [(r, _cover(ctr_b[r], ca, w)) for r in cand_b.get(a, ()) if r in in_b and r not in claimed and sum(ctr_b[r].values()) >= MIN_SPLIT_TOKENS]
        inside = sorted([p for p in inside if p[1] >= merge_part], key=lambda p: (-p[1], order[p[0]]))
        kept: list[str] = []
        union: Counter = Counter()
        for r, _ in inside:
            if not kept or _cover(ctr_b[r], union, w) < merge_part:           # an old unit that is a copy of one already kept adds nothing
                kept.append(r)
                union += ctr_b[r]
        if len(kept) >= 2 and (single_cap is None or max(scores.get((r, a), 0.0) for r in kept) < single_cap):
            conf = sum(c for r, c in inside if r in kept) / len(kept)
            res.links.append(Link("merge", sorted(kept, key=lambda k: order[k]), [a], round(conf, 3), {"cover": round(conf, 3)}))
            claimed.update([a, *kept])
    return claimed


def _call_site_renames(b_units, a_units, left_b, left_a, tb: _Tokens, ta: _Tokens, res: MatchResult) -> list[tuple[str, str, int]]:
    """(removed key, added key, how many call sites) for names the changed code replaces one for one: ``old(x)`` became ``new(x)`` in a unit that exists in both versions."""
    removed: dict[str, list[str]] = defaultdict(list)
    added: dict[str, list[str]] = defaultdict(list)
    for r in left_b:
        removed[b_units[r].name].append(r)
    for a in left_a:
        added[a_units[a].name].append(a)
    subs: Counter = Counter()
    for m in res.matches:
        if b_units[m.before].shash == a_units[m.after].shash:
            continue
        x, y = tb(m.before), ta(m.after)
        if len(x) > 800 or len(y) > 800:
            continue
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
            if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1 and x[i1] in removed and y[j1] in added:
                subs[(x[i1], y[j1])] += 1
    targets: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)
    for (old, new) in subs:
        targets[old].add(new)
        sources[new].add(old)
    out = []
    for (old, new), n in sorted(subs.items()):
        if len(targets[old]) == 1 and len(sources[new]) == 1 and len(removed[old]) == 1 and len(added[new]) == 1:
            out.append((removed[old][0], added[new][0], n))
    return out


def _complementary(whole: Counter, parts: list[tuple[str, Counter]], w: dict[str, float]) -> tuple[list[str], float]:
    """The parts (best first) that each add at least ``SPLIT_GAIN`` of ``whole`` to what the earlier ones already contain, and how much of ``whole`` they contain together."""
    union: Counter = Counter()
    chosen: list[str] = []
    covered = 0.0
    for key, counter in parts:
        trial = union + counter
        gain = _cover(whole, trial, w) - covered
        if gain >= SPLIT_GAIN:
            chosen.append(key)
            union = trial
            covered += gain
    return chosen, covered


def _generic() -> frozenset:
    from app.backend.pipeline.ingest import attribution
    return frozenset(attribution.GENERIC)


def _moved(bu, au, score: float, conf: float, signals: dict, alts: list | None = None) -> Match:
    kind = "moved" if bu.name == au.name else "renamed"
    return Match(bu.key, au.key, kind, conf, {"name": False, "scope_changed": bu.scope != au.scope, **signals}, alts or [])
